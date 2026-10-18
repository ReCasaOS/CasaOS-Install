#!/usr/bin/env python3
"""The module catalogue builder has to be able to say no.

    python3 scripts/test-build-modules-catalogue.py

Each case builds the release of a module the way its repository publishes it (a git repository with a tag, a checksums.txt, an archive for each
architecture) in a state that must come out a given way, and runs scripts/build-modules-catalogue.py on it, and then scripts/build-release-bundle.sh's own
functions for it (the ones that fetch with curl from a file:// tree, as the script can be pointed at one, and the one that writes the digest into install.sh).
A good release gives a catalogue that the reader of the box takes; a release whose archive is not the one checksums.txt names, whose tag is at another
commit, whose archives do not carry the same module.json, whose files are not all there or whose manifest the reader would refuse gives no catalogue at all,
and the reason. The rules the reader of the box applies are the core's (pkg/osmodule); the builder has a copy of them, and the cases that refuse a manifest are
the ones that were also run through the core's own ParseCatalogue when the copy was made.

BUILDER=<path> and BUILD_SCRIPT=<path> run the cases against other copies, to see that a broken one is caught. TEST_BASH names a bash where plain `bash` is not
the one (on Windows: the MSYS bash of Git, with a PATH that has no WindowsApps stub of python3). CASES=<regex> runs the cases whose name matches, FAILFAST=1 stops at the first that fails.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
BUILDER = os.environ.get("BUILDER") or os.path.join(HERE, "build-modules-catalogue.py")
BUILD_SCRIPT = os.environ.get("BUILD_SCRIPT") or os.path.join(HERE, "build-release-bundle.sh")
INSTALL_SH = os.path.join(HERE, "..", "install.sh")
BASH = os.environ.get("TEST_BASH") or "bash"
ONLY = os.environ.get("CASES")
sys.dont_write_bytecode = True

loading = importlib.util.spec_from_file_location("builder", BUILDER)
builder = importlib.util.module_from_spec(loading)
loading.loader.exec_module(builder)

COMPONENT = "CasaOS-VirtManagement"
ARCHIVE = "linux-%s-casaos-virt-management-v0.1.1.tar.gz"
BRAILLE_BLANK = chr(0x2800)

GOOD_MANIFEST = {
    "schema": 1,
    "name": "virt",
    "title": "Virtual machines",
    "description": "Create and run virtual machines with libvirt and QEMU.",
    "component": COMPONENT,
    "unit": "casaos-virt-management.service",
    "gateway_route": "/v2/virt_management",
    "packages": {"common": ["qemu-utils", "libvirt-daemon-system"], "amd64": ["qemu-system-x86", "ovmf"], "arm64": ["qemu-system-arm", "qemu-efi-aarch64"]},
    "min_recasaos": "0.5.23",
    "os": ["debian:12", "debian:13", "ubuntu:24.04", "ubuntu:26.04"],
    "os_upgrade": {
        "stop_units": ["casaos-virt-management.service"],
        "stop_timeout_seconds": 300,
        "maintenance": {
            "prepare": "/v2/virt_management/maintenance/prepare",
            "status": "/v2/virt_management/maintenance/status",
            "resume": "/v2/virt_management/maintenance/resume",
        },
    },
}


def sh(path):
    """A path as the MSYS bash and the curl of a Windows machine take it."""
    return os.path.abspath(path).replace("\\", "/")


def file_url(path):
    p = sh(path)
    return "file://" + ("" if p.startswith("/") else "/") + p  # (file:///C:/dir on Windows, file:///dir elsewhere)


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"] + list(args),
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def rm_rf(path):
    def writable(func, target, _):
        os.chmod(target, 0o700)  # (git's objects are read-only, and Windows will not delete those)
        func(target)

    shutil.rmtree(path, onerror=writable) if os.path.exists(path) else None


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def jbytes(obj):
    return json.dumps(obj, indent=2).encode("utf-8")


def tarball(path, members):
    """members: (name, bytes) for a file, (name, None) for a directory, (name, ('link', target)) for a symbolic link."""
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members:
            ti = tarfile.TarInfo(name)
            if data is None:
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            elif isinstance(data, tuple):
                ti.type, ti.linkname = tarfile.SYMTYPE, data[1]
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))


class Release:
    """The release of one module as its repository publishes it: <root>/<component> is a git repository with the tag, and the assets of the release are in
    <root>/<component>/releases/download/<tag>/ (which is where a file:// tree has them for curl)."""

    def __init__(self, root, component=COMPONENT, tag="v0.1.1", stem="virt-management", annotated=True):
        self.root, self.component, self.tag, self.stem = root, component, tag, stem
        self.repo = os.path.join(root, component)
        os.makedirs(self.repo)
        git(self.repo, "init", "-q")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "release")
        self.commit = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, *(["tag", "-a", "-m", tag, tag] if annotated else ["tag", tag]))
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "later")
        self.later = git(self.repo, "rev-parse", "HEAD")
        self.dir = os.path.join(self.repo, "releases", "download", tag)
        os.makedirs(self.dir)

    def archive_name(self, arch):
        return "linux-%s-casaos-%s-%s.tar.gz" % (arch, self.stem, self.tag)

    def clear(self):
        for entry in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, entry))
        return self

    def publish(self, manifest=GOOD_MANIFEST, manifest_arm=None, members=None, members_arm=None, sums=None):
        """Writes the archives and checksums.txt. manifest is a dict or the bytes of module.json; members replace the whole content of an archive."""
        raw = manifest if isinstance(manifest, bytes) else jbytes(manifest)
        raw_arm = raw if manifest_arm is None else (manifest_arm if isinstance(manifest_arm, bytes) else jbytes(manifest_arm))
        lines = []
        for arch, text in (("amd64", raw), ("arm64", raw_arm)):
            path = os.path.join(self.dir, self.archive_name(arch))
            given = members if arch == "amd64" else (members_arm if members_arm is not None else members)
            tarball(path, given if given is not None else [("module.json", text), ("build/", None), ("build/sysroot/usr/bin/casaos-virt-management", b"#!/bin/sh\n")])
            with open(path, "rb") as f:
                lines.append("%s  %s" % (sha256(f.read()), self.archive_name(arch)))
        lines.append("%s  %s" % ("0" * 64, "other-file.txt"))
        with open(os.path.join(self.dir, "checksums.txt"), "w") as f:
            f.write("\n".join(lines if sums is None else sums(lines)) + "\n")
        return self

    def stage(self, stage, name="virt"):
        folder = os.path.join(stage, name)
        os.makedirs(folder, exist_ok=True)
        for entry in os.listdir(self.dir):
            shutil.copy(os.path.join(self.dir, entry), folder)


def env_text(name="VIRT", tag="v0.1.1", commit="", component=COMPONENT, tier="community"):
    return "# modules\nMODULE_%s_TAG=%s\nMODULE_%s_COMMIT=%s\nMODULE_%s_COMPONENT=%s\nMODULE_%s_TIER=%s\n" % (name, tag, name, commit, name, component, name, tier)


def manifest_cases():
    """The manifests (a dict, or the bytes of the file) that the reader of the box refuses."""
    def without(key):
        return {k: v for k, v in GOOD_MANIFEST.items() if k != key}

    text = json.dumps(GOOD_MANIFEST)
    bare = without("os_upgrade")  # (the cases that change the unit or the route must not be refused by its calls and its stop unit instead)
    cases = {
        "another name than the module's": dict(GOOD_MANIFEST, name="other"),
        "another component than the pin's": dict(GOOD_MANIFEST, component="CasaOS-Else"),
        "schema 2": dict(GOOD_MANIFEST, schema=2),
        "schema as text": dict(GOOD_MANIFEST, schema="1"),
        "schema as a float": dict(GOOD_MANIFEST, schema=1.0),
        "schema as a boolean": dict(GOOD_MANIFEST, schema=True),
        "no schema": without("schema"),
        "a field the reader does not know": dict(GOOD_MANIFEST, extra="x"),
        "a field spelled with a capital": dict(without("title"), Title="Virtual machines"),
        "an unknown field inside packages": dict(GOOD_MANIFEST, packages=dict(GOOD_MANIFEST["packages"], extra=[])),
        "a null": dict(GOOD_MANIFEST, description=None),
        "no title": without("title"),
        "an empty title": dict(GOOD_MANIFEST, title=""),
        "a blank title": dict(GOOD_MANIFEST, title=BRAILLE_BLANK * 3),
        "a title of spaces": dict(GOOD_MANIFEST, title="   "),
        "a title of 61 characters": dict(GOOD_MANIFEST, title="t" * 61),
        "a control character in the title": dict(GOOD_MANIFEST, title="Virtual\nmachines"),
        "a replacement character": dict(GOOD_MANIFEST, description="x" + chr(0xFFFD)),
        "a description of 301 characters": dict(GOOD_MANIFEST, description="d" * 301),
        "a control character in the description": dict(GOOD_MANIFEST, description="a\tb"),
        "a unit that starts with a hyphen": dict(bare, unit="-casaos.service"),
        "a unit with a space": dict(bare, unit="casaos virt.service"),
        "a unit name of 65 characters": dict(bare, unit="a" * 65 + ".service"),
        "a unit that is not a service": dict(bare, unit="casaos-virt-management.timer"),
        "a route without a version": dict(bare, gateway_route="/virt_management"),
        "a route name of 41 characters": dict(bare, gateway_route="/v2/" + "a" * 41),
        "a route with a capital": dict(bare, gateway_route="/v2/Virt"),
        "a reserved route": dict(bare, gateway_route="/v2/sys"),
        "a reserved route under another version": dict(bare, gateway_route="/v9/app_management"),
        "a minimum with two numbers": dict(GOOD_MANIFEST, min_recasaos="0.5"),
        "a minimum with a prefix": dict(GOOD_MANIFEST, min_recasaos="v0.5.23"),
        "no os": dict(GOOD_MANIFEST, os=[]),
        "an os that is not debian or ubuntu": dict(GOOD_MANIFEST, os=["fedora:40"]),
        "an os version with a decimal of one digit": dict(GOOD_MANIFEST, os=["ubuntu:24.4"]),
        "an os twice": dict(GOOD_MANIFEST, os=["debian:12", "debian:12"]),
        "17 systems": dict(GOOD_MANIFEST, os=["debian:%d" % i for i in range(10, 27)]),
        "a package with a capital": dict(GOOD_MANIFEST, packages={"common": ["Qemu"]}),
        "a package that ends in a hyphen": dict(GOOD_MANIFEST, packages={"common": ["qemu-"]}),
        "a package that ends in a plus": dict(GOOD_MANIFEST, packages={"common": ["g++"]}),
        "a package that is a file": dict(GOOD_MANIFEST, packages={"common": ["x.deb"]}),
        "a package of one character": dict(GOOD_MANIFEST, packages={"common": ["q"]}),
        "a package twice": dict(GOOD_MANIFEST, packages={"amd64": ["ovmf", "ovmf"]}),
        "41 packages": dict(GOOD_MANIFEST, packages={"common": ["pkg%02d" % i for i in range(20)], "amd64": ["amd%02d" % i for i in range(21)]}),
        "33 packages in one list": dict(GOOD_MANIFEST, packages={"common": ["pkg%02d" % i for i in range(33)]}),
        "a stop unit that is not the module's": dict(GOOD_MANIFEST, os_upgrade=dict(GOOD_MANIFEST["os_upgrade"], stop_units=["ssh.service"])),
        "no stop unit": dict(GOOD_MANIFEST, os_upgrade=dict(GOOD_MANIFEST["os_upgrade"], stop_units=[])),
        "a stop timeout of 0": dict(GOOD_MANIFEST, os_upgrade=dict(GOOD_MANIFEST["os_upgrade"], stop_timeout_seconds=0)),
        "a stop timeout of a day": dict(GOOD_MANIFEST, os_upgrade=dict(GOOD_MANIFEST["os_upgrade"], stop_timeout_seconds=86400)),
        "a call outside the route": dict(GOOD_MANIFEST, os_upgrade=dict(GOOD_MANIFEST["os_upgrade"], maintenance={"prepare": "/v1/x", "status": "/v2/virt_management/s", "resume": "/v2/virt_management/r"})),
        "a call in another version of the route": dict(GOOD_MANIFEST, os_upgrade=dict(GOOD_MANIFEST["os_upgrade"], maintenance={"prepare": "/v9/virt_management/p", "status": "/v2/virt_management/s", "resume": "/v2/virt_management/r"})),
        "a call that goes up": dict(GOOD_MANIFEST, os_upgrade=dict(GOOD_MANIFEST["os_upgrade"], maintenance={"prepare": "/v2/virt_management/../x", "status": "/v2/virt_management/s", "resume": "/v2/virt_management/r"})),
        "a call that asks for a query": dict(GOOD_MANIFEST, os_upgrade=dict(GOOD_MANIFEST["os_upgrade"], maintenance={"prepare": "/v2/virt_management/p?x=1", "status": "/v2/virt_management/s", "resume": "/v2/virt_management/r"})),
        "no resume call": dict(GOOD_MANIFEST, os_upgrade=dict(GOOD_MANIFEST["os_upgrade"], maintenance={"prepare": "/v2/virt_management/p", "status": "/v2/virt_management/s"})),
        "a manifest that is a list": [GOOD_MANIFEST],
        "a manifest that is text": "virt",
        "a key twice": ('{"schema": 1, ' + text[1:]).encode(),
        "text that is not JSON": b"module",
        "an empty file": b"",
        "NaN": text.replace('"schema": 1', '"schema": NaN').encode(),
        "a byte order mark": b"\xef\xbb\xbf" + text.encode(),
        "bytes that are not UTF-8": text.encode().replace(b"Create", b"Cr\xe9ate"),
        "a manifest of 64 KiB and more": (text[:-1] + ', "description": "' + "d" * (64 << 10) + '"}').encode(),
    }
    return cases


def catalogue_cases():
    """A good catalogue, a good entry of it, and the catalogues that the reader of the box refuses."""
    good = {"schema": 1, "modules": []}
    entry = {
        "name": "virt", "component": COMPONENT, "tier": "core", "tag": "v0.1.1", "commit": "a" * 40,
        "assets": {"amd64": {"url": "https://github.com/ReCasaOS/%s/releases/download/v0.1.1/%s" % (COMPONENT, ARCHIVE % "amd64"), "sha256": "b" * 64}},
        "manifest": GOOD_MANIFEST,
    }
    url = entry["assets"]["amd64"]["url"]

    def with_asset(**kw):
        return dict(entry, assets={"amd64": dict(entry["assets"]["amd64"], **kw)})

    cases = {
        "schema 2": dict(good, schema=2),
        "no modules key": {"schema": 1},
        "modules null": {"schema": 1, "modules": None},
        "a field the reader does not know": dict(good, extra=1),
        "an entry with a field the reader does not know": dict(good, modules=[dict(entry, extra="x")]),
        "an entry twice": dict(good, modules=[entry, entry]),
        "65 entries": dict(good, modules=[dict(entry, name="m%d" % i, manifest=dict(GOOD_MANIFEST, name="m%d" % i)) for i in range(65)]),
        "a tier": dict(good, modules=[dict(entry, tier="official")]),
        "a tag": dict(good, modules=[dict(entry, tag="v0.1")]),
        "a commit": dict(good, modules=[dict(entry, commit="a" * 41)]),
        "no assets": dict(good, modules=[dict(entry, assets={})]),
        "an asset that is another organisation's": dict(good, modules=[with_asset(url=url.replace("ReCasaOS", "Someone"))]),
        "an asset that is another repository's": dict(good, modules=[with_asset(url=url.replace("/%s/" % COMPONENT, "/CasaOS-Else/"))]),
        "an asset that is another tag's": dict(good, modules=[with_asset(url=url.replace("download/v0.1.1", "download/v0.1.0"))]),
        "an asset whose archive is of another version": dict(good, modules=[with_asset(url=url.replace("casaos-virt-management-v0.1.1", "casaos-virt-management-v0.1.0"))]),
        "an asset of the other architecture": dict(good, modules=[with_asset(url=url.replace("amd64", "arm64"))]),
        "an asset with a port": dict(good, modules=[with_asset(url=url.replace("github.com", "github.com:443"))]),
        "an asset over http": dict(good, modules=[with_asset(url=url.replace("https", "http"))]),
        "an asset with a query": dict(good, modules=[with_asset(url=url + "?x=1")]),
        "an asset with an upper case digest": dict(good, modules=[with_asset(sha256="B" * 64)]),
        "an asset with a short digest": dict(good, modules=[with_asset(sha256="b" * 63)]),
        "an asset of 513 characters": dict(good, modules=[with_asset(url=url.replace("virt-management", "v" * 460))]),
        "a manifest of another name": dict(good, modules=[dict(entry, manifest=dict(GOOD_MANIFEST, name="other"))]),
        "a manifest of another component": dict(good, modules=[dict(entry, manifest=dict(GOOD_MANIFEST, component="CasaOS-Else"))]),
    }
    return good, entry, cases


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # the repositories of two modules, made once: git is slow on some machines, and a case changes only what the release published
        cls.net = tempfile.mkdtemp(prefix="modnet-")
        cls.virt = Release(cls.net)
        cls.other = Release(cls.net, component="CasaOS-Other", stem="other")

    @classmethod
    def tearDownClass(cls):
        rm_rf(cls.net)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="modcat-")
        self.addCleanup(rm_rf, self.tmp)
        self.virt.clear()
        self.other.clear()
        self.stage_dir = os.path.join(self.tmp, "stage")
        self.out = os.path.join(self.tmp, "out", "modules-catalogue-v9.9.9.json")
        os.makedirs(os.path.dirname(self.out))
        self.env = os.path.join(self.tmp, "modules.env")

    def release(self, **kw):
        """A repository of its own, for a case that changes the repository."""
        return Release(os.path.join(self.tmp, "own"), **kw)

    def write_env(self, text):
        with open(self.env, "w", newline="\n") as f:
            f.write(text)

    def build(self, git_base=None):
        builder.build(self.env, self.stage_dir, self.out, git_base or file_url(self.net))

    def refused(self, reason, git_base=None):
        """The build is refused with a reason that has the text, and leaves no catalogue."""
        with self.assertRaises(builder.Refused) as raised:
            self.build(git_base)
        self.assertIn(reason, str(raised.exception))
        self.assertFalse(os.path.exists(self.out), "a catalogue was left behind")
        self.assertFalse(os.path.exists(self.out + ".tmp"), "a temporary catalogue was left behind")

    def good(self, tier="community"):
        r = self.virt.publish()
        r.stage(self.stage_dir)
        self.write_env(env_text(commit=r.commit, tier=tier))
        return r

    def offer(self, manifest=GOOD_MANIFEST, **kw):
        """What a release holds, staged and pinned."""
        r = self.virt.clear().publish(manifest=manifest, **kw)
        shutil.rmtree(self.stage_dir, ignore_errors=True)
        r.stage(self.stage_dir)
        self.write_env(env_text(commit=r.commit))
        return r

    def catalogue(self):
        with open(self.out, "rb") as f:
            return f.read()


class ModulesEnv(Base):
    def test_comments_only_is_no_module(self):
        self.write_env("# nothing offered\n\n   \n# MODULE_X_TAG=v1.0.0\n")
        self.assertEqual(builder.read_modules_env(self.env), [])
        with self.assertRaises(builder.Refused):
            self.build()

    def test_a_module_and_its_name(self):
        self.write_env(env_text(name="VIRT_MANAGEMENT", commit="a" * 40) + env_text(name="ABC9", commit="b" * 40, tier="core", tag="v2.0.10"))
        mods = builder.read_modules_env(self.env)
        self.assertEqual([m["name"] for m in mods], ["abc9", "virt-management"])
        self.assertEqual(mods[0]["tier"], "core")
        self.assertEqual(mods[1]["component"], COMPONENT)

    def test_crlf_and_blank_lines(self):
        self.write_env(env_text(commit="a" * 40).replace("\n", "\r\n") + "\r\n")
        self.assertEqual(len(builder.read_modules_env(self.env)), 1)

    def test_refused(self):
        good = env_text(commit="a" * 40)
        cases = {
            "a key that is none of the four": good + "MODULE_VIRT_OWNER=x\n",
            "a line that is not an assignment": good + "MODULE_VIRT\n",
            "a stray word": good + "hello\n",
            "a key twice": good + "MODULE_VIRT_TAG=v0.1.2\n",
            "a missing key": "MODULE_VIRT_TAG=v0.1.1\nMODULE_VIRT_COMMIT=" + "a" * 40 + "\nMODULE_VIRT_TIER=core\n",
            "a lower case name": good.replace("VIRT", "virt"),
            "a name that starts with a digit": good.replace("VIRT", "1VIRT"),
            "a name that starts with an underscore": good.replace("MODULE_VIRT", "MODULE__VIRT"),
            "a name too long": env_text(name="A" * 33, commit="a" * 40),
            "a tag without v": env_text(tag="0.1.1", commit="a" * 40),
            "a tag with two numbers": env_text(tag="v0.1", commit="a" * 40),
            "a prerelease tag": env_text(tag="v0.1.1-rc1", commit="a" * 40),
            "a short commit": env_text(commit="a" * 39),
            "an upper case commit": env_text(commit="A" * 40),
            "a commit with a branch name": env_text(commit="main"),
            "a component with a slash": env_text(commit="a" * 40, component="Other/Repo"),
            "a component with a dot dot": env_text(commit="a" * 40, component="a/../b"),
            "a component that starts with a hyphen": env_text(commit="a" * 40, component="-x"),
            "a tier that is not one": env_text(commit="a" * 40, tier="official"),
            "a value with a space": env_text(commit="a" * 40, tier="core x"),
            "a comment after a value": env_text(commit="a" * 40, tier="core # ok"),
            "a quoted value": env_text(commit="a" * 40, tier='"core"'),
            "a command substitution": env_text(commit="a" * 40, component="$(id)"),
        }
        for what, text in cases.items():
            with self.subTest(what):
                self.write_env(text)
                with self.assertRaises(builder.Refused):
                    builder.read_modules_env(self.env)


class Checksums(unittest.TestCase):
    def lines(self, *names, digest="a" * 64):
        return "\n".join("%s  %s" % (digest, n) for n in names) + "\n"

    def test_good_and_the_forms_of_a_line(self):
        text = self.lines(ARCHIVE % "amd64", "other.txt") + "%s *%s\n" % ("b" * 64, ARCHIVE % "arm64") + "\n  \nsingle\n"
        found = builder.find_archives(text, "v0.1.1")
        self.assertEqual(found["amd64"], (ARCHIVE % "amd64", "a" * 64))
        self.assertEqual(found["arm64"], (ARCHIVE % "arm64", "b" * 64))

    def test_another_tag_is_not_a_candidate(self):
        text = self.lines(ARCHIVE % "amd64", ARCHIVE % "arm64", "linux-amd64-casaos-virt-management-v0.1.0.tar.gz", "linux-arm64-casaos-virt-management-v0.1.2.tar.gz")
        self.assertEqual(len(builder.find_archives(text, "v0.1.1")), 2)

    def test_refused(self):
        a, b = ARCHIVE % "amd64", ARCHIVE % "arm64"
        cases = {
            "no arm64": (self.lines(a), "arm64"),
            "no amd64": (self.lines(b), "amd64"),
            "nothing for this tag": (self.lines(a, b), None),
            "two for amd64": (self.lines(a, b, "linux-amd64-casaos-other-v0.1.1.tar.gz"), "not exactly one"),
            "two names": (self.lines(a, "linux-arm64-casaos-other-v0.1.1.tar.gz"), "not of the same name"),
            "an upper case digest": (self.lines(a, b, digest="A" * 64), "not 64 lower case"),
            "a short digest": (self.lines(a, b, digest="a" * 63), "not 64 lower case"),
            "two digests for one name": (self.lines(a, b) + self.lines(a, digest="c" * 64), "two digests"),
        }
        for what, (text, reason) in cases.items():
            with self.subTest(what):
                with self.assertRaises(builder.Refused) as raised:
                    builder.find_archives(text, "v0.1.2" if what == "nothing for this tag" else "v0.1.1")
                if reason:
                    self.assertIn(reason, str(raised.exception))


class Build(Base):
    def test_a_good_release(self):
        r = self.good()
        self.build()
        raw = self.catalogue()
        cat = builder.parse_catalogue(raw)
        self.assertEqual(raw, (json.dumps(json.loads(raw), indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        (entry,) = cat["modules"]
        self.assertEqual(entry["name"], "virt")
        self.assertEqual((entry["component"], entry["tier"], entry["tag"], entry["commit"]), (COMPONENT, "community", "v0.1.1", r.commit))
        self.assertEqual(entry["manifest"], GOOD_MANIFEST)
        for arch in ("amd64", "arm64"):
            with open(os.path.join(r.dir, r.archive_name(arch)), "rb") as f:
                digest = sha256(f.read())
            self.assertEqual(entry["assets"][arch], {"url": "https://github.com/ReCasaOS/%s/releases/download/v0.1.1/%s" % (COMPONENT, r.archive_name(arch)), "sha256": digest})
        self.assertEqual(list(entry), ["name", "component", "tier", "tag", "commit", "assets", "manifest"])
        self.assertFalse(os.path.exists(self.out + ".tmp"))

    def test_the_same_inputs_give_the_same_bytes(self):
        self.good()
        self.build()
        first = self.catalogue()
        os.remove(self.out)
        self.build()
        self.assertEqual(first, self.catalogue())

    def test_a_lightweight_tag(self):
        r = self.release(annotated=False).publish()
        r.stage(self.stage_dir)
        self.write_env(env_text(commit=r.commit))
        self.build(git_base=file_url(os.path.join(self.tmp, "own")))
        self.assertEqual(len(builder.parse_catalogue(self.catalogue())["modules"]), 1)

    def test_the_tier_is_the_pin(self):
        self.good(tier="core")
        self.build()
        self.assertEqual(builder.parse_catalogue(self.catalogue())["modules"][0]["tier"], "core")

    def test_two_modules(self):
        r1 = self.good()
        second = dict(GOOD_MANIFEST, name="other", component="CasaOS-Other", unit="casaos-other.service", gateway_route="/v1/other_thing")
        second.pop("os_upgrade")
        r2 = self.other.publish(manifest=second)
        r2.stage(self.stage_dir, "other")
        self.write_env(env_text(name="VIRT", commit=r1.commit) + env_text(name="OTHER", commit=r2.commit, component="CasaOS-Other"))
        self.assertEqual([m["name"] for m in builder.read_modules_env(self.env)], ["other", "virt"])
        self.write_env(env_text(name="OTHER", commit=r2.commit, component="CasaOS-Other") + env_text(name="VIRT", commit=r1.commit))
        self.build()
        self.assertEqual([e["name"] for e in builder.parse_catalogue(self.catalogue())["modules"]], ["other", "virt"])
        first = self.catalogue()
        self.write_env(env_text(name="VIRT", commit=r1.commit) + env_text(name="OTHER", commit=r2.commit, component="CasaOS-Other"))
        self.build()
        self.assertEqual(first, self.catalogue(), "the order of modules.env changes the catalogue")

    def test_one_bad_module_is_no_catalogue_at_all(self):
        r1 = self.good()
        second = dict(GOOD_MANIFEST, name="other", component="CasaOS-Other", unit="casaos-other.service", gateway_route="/v1/other_thing", title="")
        r2 = self.other.publish(manifest=second)
        r2.stage(self.stage_dir, "other")
        self.write_env(env_text(commit=r1.commit) + env_text(name="OTHER", commit=r2.commit, component="CasaOS-Other"))
        self.refused("module other")

    def test_the_archive_is_not_the_one_checksums_names(self):
        r = self.good()
        path = os.path.join(self.stage_dir, "virt", r.archive_name("amd64"))
        tarball(path, [("module.json", jbytes(GOOD_MANIFEST)), ("build/sysroot/usr/bin/evil", b"x")])
        self.refused("is not the archive that checksums.txt names")

    def test_the_arm64_archive_is_not_the_one_checksums_names(self):
        r = self.good()
        with open(os.path.join(self.stage_dir, "virt", r.archive_name("arm64")), "ab") as f:
            f.write(b"\0")
        self.refused("is not the archive that checksums.txt names")

    def test_an_archive_that_was_not_fetched(self):
        r = self.good()
        os.remove(os.path.join(self.stage_dir, "virt", r.archive_name("arm64")))
        self.refused("was not fetched")

    def test_no_checksums(self):
        self.good()
        os.remove(os.path.join(self.stage_dir, "virt", "checksums.txt"))
        self.refused("checksums.txt")

    def test_no_stage_for_the_module(self):
        self.good()
        shutil.rmtree(os.path.join(self.stage_dir, "virt"))
        self.refused("checksums.txt")

    def test_the_tag_is_at_another_commit(self):
        r = self.good()
        self.write_env(env_text(commit=r.later))
        self.refused("is at %s, not at the pinned %s" % (r.commit, r.later))

    def test_the_pinned_commit_is_not_the_tag(self):
        self.good()
        self.write_env(env_text(commit="d" * 40))
        self.refused("not at the pinned " + "d" * 40)

    def test_the_tag_does_not_exist(self):
        r = self.release().publish()
        r.stage(self.stage_dir)
        self.write_env(env_text(commit=r.commit))
        git(r.repo, "tag", "-d", "v0.1.1")
        self.refused("has no tag v0.1.1", git_base=file_url(os.path.join(self.tmp, "own")))

    def test_the_repository_cannot_be_read(self):
        self.good()
        self.refused("cannot read the tags", git_base=file_url(os.path.join(self.tmp, "nowhere")))

    def test_the_two_archives_do_not_carry_the_same_manifest(self):
        self.offer(manifest_arm=dict(GOOD_MANIFEST, description="Another text."))
        self.refused("module.json of the amd64 archive and of the arm64 archive are not the same")

    def test_a_manifest_that_differs_only_in_its_bytes_is_not_the_same_either(self):
        self.offer(manifest_arm=jbytes(GOOD_MANIFEST) + b"\n")
        self.refused("are not the same")

    def archives_with(self, members, **kw):
        self.offer(members=members, **kw)

    def test_the_content_of_an_archive(self):
        good = jbytes(GOOD_MANIFEST)
        cases = {
            "no module.json": ([("build/sysroot/usr/bin/x", b"x")], "no module.json at the root"),
            "module.json in a directory": ([("build/module.json", good)], "no module.json at the root"),
            "module.json with ./": ([("./module.json", good)], "no module.json at the root"),
            "module.json twice": ([("module.json", good), ("module.json", good)], "twice"),
            "module.json a link": ([("module.json", ("link", "build/x"))], "not a regular file"),
            "module.json a directory": ([("module.json", None)], "not a regular file"),
            "module.json too large": ([("module.json", good + b" " * (64 << 10))], "too large"),
        }
        for what, (members, reason) in cases.items():
            with self.subTest(what):
                self.archives_with(members)
                self.refused(reason)

    def test_an_archive_that_is_not_a_tarball(self):
        r = self.virt.publish()
        for arch in ("amd64", "arm64"):
            path = os.path.join(r.dir, r.archive_name(arch))
            with open(path, "wb") as f:
                f.write(b"not a tarball at all")
        lines = ["%s  %s" % (sha256(b"not a tarball at all"), r.archive_name(a)) for a in ("amd64", "arm64")]
        with open(os.path.join(r.dir, "checksums.txt"), "w") as f:
            f.write("\n".join(lines) + "\n")
        r.stage(self.stage_dir)
        self.write_env(env_text(commit=r.commit))
        self.refused("not a readable tarball")

    def test_a_truncated_archive(self):
        r = self.virt.publish()
        path = os.path.join(r.dir, r.archive_name("amd64"))
        with open(path, "rb") as f:
            whole = f.read()
        with open(path, "wb") as f:
            f.write(whole[: len(whole) // 2])
        with open(os.path.join(r.dir, "checksums.txt"), "w") as f:
            f.write("%s  %s\n" % (sha256(whole[: len(whole) // 2]), r.archive_name("amd64")))
            with open(os.path.join(r.dir, r.archive_name("arm64")), "rb") as g:
                f.write("%s  %s\n" % (sha256(g.read()), r.archive_name("arm64")))
        r.stage(self.stage_dir)
        self.write_env(env_text(commit=r.commit))
        self.refused("not a readable tarball")

    def test_the_manifest(self):
        for what, manifest in manifest_cases().items():
            with self.subTest(what):
                self.offer(manifest if isinstance(manifest, bytes) else json.dumps(manifest).encode())
                self.refused("module.json")

    def test_a_manifest_that_the_reader_takes(self):
        def accepted(manifest):
            self.offer(manifest)
            self.build()
            self.assertEqual(builder.parse_catalogue(self.catalogue())["modules"][0]["manifest"], manifest)

        accepted({k: v for k, v in GOOD_MANIFEST.items() if k not in ("os_upgrade", "description", "packages")})
        title = "V" + chr(0xE9) + "rtualisation " + chr(0x1F5A5)
        accepted(dict(GOOD_MANIFEST, title=title))
        self.assertIn(title.encode("utf-8"), self.catalogue(), "text that is not ASCII is not written as it is")
        accepted(dict(GOOD_MANIFEST, os=["ubuntu:24.04", "debian:12"], packages={"common": ["libstdc++6", "a.b", "x-y+z"]}))
        accepted({k: v for k, v in GOOD_MANIFEST.items() if k != "os_upgrade"} | {"gateway_route": "/v10/a_b9"})

    def test_the_catalogue_is_judged_as_the_reader_judges_it(self):
        good, entry, cases = catalogue_cases()
        builder.parse_catalogue(json.dumps(good).encode())
        builder.parse_catalogue(json.dumps(dict(good, modules=[entry])).encode())
        for what, cat in cases.items():
            with self.subTest(what):
                with self.assertRaises(builder.Refused):
                    builder.parse_catalogue(json.dumps(cat).encode())
        with self.assertRaises(builder.Refused):
            builder.parse_catalogue(json.dumps(good).encode() + b" " * (1 << 20))
        builder.parse_catalogue(json.dumps(good).encode() + b" " * ((1 << 20) - len(json.dumps(good))))  # (1 MiB exactly is the most)


class Units(Base):
    def test_the_json_reader(self):
        for raw in (b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}', b'{"a": 1, "a": 2}', b'\xef\xbb\xbf{}', b'{"a": "\xff"}', b'{"a": ', b"", b"[1] x"):
            with self.subTest(raw):
                with self.assertRaises(builder.Refused):
                    builder.load_json(raw)
        self.assertEqual(builder.load_json(b' {"a": [1, 2.5, "x", null, true]} '), {"a": [1, 2.5, "x", None, True]})

    def test_the_size_of_a_manifest(self):
        whole = jbytes(GOOD_MANIFEST)
        most = whole + b" " * ((64 << 10) - len(whole))
        over = most + b" "
        builder.parse_manifest(most, "virt")
        with self.assertRaises(builder.Refused):
            builder.parse_manifest(over, "virt")
        for name, data, ok in (("most.tar.gz", most, True), ("over.tar.gz", over, False)):
            path = os.path.join(self.tmp, name)
            tarball(path, [("module.json", data)])
            if ok:
                self.assertEqual(builder.read_module_json(path), data)
            else:
                with self.assertRaises(builder.Refused) as raised:
                    builder.read_module_json(path)
                self.assertIn("too large", str(raised.exception))

    def test_the_number_of_modules(self):
        def entry(module, stage, git_base):
            name = module["name"]
            return {
                "name": name, "component": COMPONENT, "tier": "community", "tag": "v0.1.1", "commit": module["commit"],
                "assets": {arch: {"url": "https://github.com/ReCasaOS/%s/releases/download/v0.1.1/%s" % (COMPONENT, ARCHIVE % arch), "sha256": "b" * 64} for arch in ("amd64", "arm64")},
                "manifest": dict(GOOD_MANIFEST, name=name),
            }

        original = builder.entry_of
        builder.entry_of = entry
        self.addCleanup(setattr, builder, "entry_of", original)
        self.write_env("".join(env_text(name="M%02d" % i, commit="a" * 40) for i in range(64)))
        self.build()
        self.assertEqual(len(builder.parse_catalogue(self.catalogue())["modules"]), 64)
        os.remove(self.out)
        self.write_env("".join(env_text(name="M%02d" % i, commit="a" * 40) for i in range(65)))
        self.refused("too many")


class CommandLine(Base):
    def run_builder(self, *args):
        p = subprocess.run([sys.executable, "-I", BUILDER] + list(args), capture_output=True)
        return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")  # (bytes: a new line is one character here, whatever the system)

    def test_list(self):
        self.write_env(env_text(commit="a" * 40))
        self.assertEqual(self.run_builder("list", self.env), (0, "virt %s v0.1.1 %s community\n" % (COMPONENT, "a" * 40), ""))
        self.write_env("# none\n")
        self.assertEqual(self.run_builder("list", self.env), (0, "", ""))

    def test_list_refused(self):
        self.write_env("MODULE_VIRT_TAG=v1\n")
        code, out, err = self.run_builder("list", self.env)
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("error: "), err)

    def test_archives(self):
        r = self.virt.publish()
        code, out, err = self.run_builder("archives", os.path.join(r.dir, "checksums.txt"), "v0.1.1")
        self.assertEqual((code, out.split(), err), (0, [r.archive_name("amd64"), r.archive_name("arm64")], ""))
        self.assertEqual(self.run_builder("archives", os.path.join(r.dir, "checksums.txt"), "v0.2.0")[0], 1)
        self.assertEqual(self.run_builder("archives", os.path.join(r.dir, "nothing"), "v0.1.1")[0], 1)

    def test_build_and_its_refusal(self):
        r = self.good()
        base = file_url(self.net)
        code, out, err = self.run_builder("build", "--git-base", base, "--out", self.out, self.env, self.stage_dir)
        self.assertEqual((code, out, err), (0, "", ""))
        builder.parse_catalogue(self.catalogue())
        os.remove(self.out)
        self.write_env(env_text(commit=r.later))
        code, out, err = self.run_builder("build", "--git-base", base, "--out", self.out, self.env, self.stage_dir)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("error: module virt: tag v0.1.1", err)
        self.assertFalse(os.path.exists(self.out))

    def test_usage(self):
        for args in ([], ["build"], ["list"], ["list", "a", "b"], ["archives", "a"], ["frobnicate"], ["build", "--out", "x", "--git-base", "y", "a", "b"]):
            with self.subTest(args):
                self.assertEqual(self.run_builder(*args)[0], 2)


# ---- the script that fetches (scripts/build-release-bundle.sh) ---------------------------------------------------------------------------------------------


class BuildScript(Base):
    """The functions of build-release-bundle.sh that fetch the modules and write the digest, run for a tree of releases that curl and git read as files."""

    def setUp(self):
        super().setUp()
        self.output = os.path.join(self.tmp, "dist")
        os.makedirs(self.output)

    def run_functions(self, body, python=None):
        script = """set -euo pipefail
SCRIPT_DIR=%(scripts)s
STAGING_ROOT=%(staging)s
OUTPUT_DIR=%(output)s
MODULES_LOCK=%(env)s
CHECKSUMS_BASE_URL=%(base)s
MODULES_GIT_BASE_URL=%(base)s
RELEASE_TAG=v9.9.9
PYTHON=%(python)s
for f in fail compute_checksum build_modules_catalogue fill_catalogue_digest write_checksums; do
    eval "$(tr -d '\\r' <%(script)s | sed -n "/^${f}() {\\$/,/^}\\$/p")"
    declare -F "${f}" >/dev/null || { echo "build-release-bundle.sh defines no ${f}()" >&2; exit 97; }
done
%(body)s
""" % {
            "scripts": sh(HERE), "staging": sh(os.path.join(self.tmp, "staging")), "output": sh(self.output), "env": sh(self.env), "base": file_url(self.net),
            "python": sh(python or sys.executable), "script": sh(BUILD_SCRIPT), "body": body,
        }
        os.makedirs(os.path.join(self.tmp, "staging"), exist_ok=True)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", GIT_TERMINAL_PROMPT="0")
        p = subprocess.run([BASH, "-c", script], capture_output=True, text=True, env=env)
        return p.returncode, p.stdout, p.stderr

    def made(self):
        return sorted(f for f in os.listdir(self.output) if f.startswith("modules-catalogue-"))

    def test_no_module_no_catalogue_and_no_python_needed(self):
        self.write_env("# nothing\n\n")
        with open(os.path.join(self.output, "modules-catalogue-v0.0.1.json"), "w") as f:
            f.write("a catalogue of an earlier run\n")
        code, out, err = self.run_functions('build_modules_catalogue\necho "file=[${MODULES_CATALOGUE_FILE}]"', python="/nonexistent/python")
        self.assertEqual((code, out.strip()), (0, "file=[]"), err)
        self.assertEqual(self.made(), [], "a catalogue of an earlier run is left in the output directory")

    def test_the_digest_of_no_catalogue_is_empty(self):
        self.write_env("# nothing\n")
        target = os.path.join(self.tmp, "install.sh")
        shutil.copy(INSTALL_SH, target)
        code, out, err = self.run_functions('build_modules_catalogue\nfill_catalogue_digest %s' % sh(target))
        self.assertEqual(code, 0, err)
        with open(target, newline="") as f:
            text = f.read().replace("\r", "")
        self.assertIn('\nreadonly CASAOS_MODULE_CATALOGUE_SHA256=""\n', text)
        self.assertNotIn("__CASAOS_MODULE_CATALOGUE_SHA256__", text)

    def test_a_good_release_gives_a_catalogue_and_its_digest(self):
        r = self.good()
        shutil.rmtree(self.stage_dir)  # the script fetches it itself
        target = os.path.join(self.tmp, "install.sh")
        shutil.copy(INSTALL_SH, target)
        code, out, err = self.run_functions('build_modules_catalogue\necho "file=[${MODULES_CATALOGUE_FILE##*/}]"\nfill_catalogue_digest %s' % sh(target))
        self.assertEqual(code, 0, err)
        self.assertIn("file=[modules-catalogue-v9.9.9.json]", out)
        self.assertEqual(self.made(), ["modules-catalogue-v9.9.9.json"])
        with open(os.path.join(self.output, "modules-catalogue-v9.9.9.json"), "rb") as f:
            raw = f.read()
        entry = builder.parse_catalogue(raw)["modules"][0]
        self.assertEqual(entry["commit"], r.commit)
        with open(target, newline="") as f:
            filled = f.read().replace("\r", "")
        self.assertIn('\nreadonly CASAOS_MODULE_CATALOGUE_SHA256="%s"\n' % sha256(raw), filled)
        with open(INSTALL_SH, newline="") as f:
            original = f.read().replace("\r", "")
        self.assertEqual(filled.replace(sha256(raw), "__CASAOS_MODULE_CATALOGUE_SHA256__"), original, "the digest is not the only thing written into install.sh")

    def test_the_digest_of_the_catalogue_is_in_checksums_txt_when_there_is_one(self):
        def checksums(with_module):
            self.write_env(env_text(commit=self.virt.publish().commit) if with_module else "# nothing\n")
            for name in ("overlay.tar.gz", "seed.tar.gz", "install.sh", "casaos-uninstall", "components.lock", "version.json"):
                with open(os.path.join(self.output, name), "w") as f:
                    f.write(name)
            body = 'OVERLAY_FILE=overlay.tar.gz\nAPPSTORE_SEED_FILE=seed.tar.gz\nbuild_modules_catalogue\nwrite_checksums'
            code, out, err = self.run_functions(body)
            self.assertEqual(code, 0, err)
            with open(os.path.join(self.output, "checksums.txt")) as f:
                return [line.split()[1].lstrip("*") for line in f.read().splitlines()]

        self.assertEqual(checksums(False), ["overlay.tar.gz", "seed.tar.gz", "install.sh", "casaos-uninstall", "components.lock", "version.json"])
        shutil.rmtree(os.path.join(self.tmp, "staging"), ignore_errors=True)
        self.assertEqual(checksums(True), ["overlay.tar.gz", "seed.tar.gz", "install.sh", "casaos-uninstall", "components.lock", "version.json", "modules-catalogue-v9.9.9.json"])
        with open(os.path.join(self.output, "checksums.txt")) as f:
            line = [l for l in f.read().splitlines() if "modules-catalogue" in l][0]
        with open(os.path.join(self.output, "modules-catalogue-v9.9.9.json"), "rb") as g:
            self.assertEqual(line.split()[0], sha256(g.read()))

    def test_the_catalogue_is_built_before_install_sh_is_filled(self):
        with open(BUILD_SCRIPT, newline="") as f:
            text = f.read().replace("\r", "")
        order = [text.index(marker) for marker in ("\nbuild_modules_catalogue\n", '\nfill_installer "${OUTPUT_DIR}/install.sh"\n', "\nwrite_checksums\n")]
        self.assertEqual(order, sorted(order), "the catalogue is not built before install.sh is filled, and the checksums written last")
        self.assertEqual(text.count("\n    fill_catalogue_digest \"${target}\"\n"), 1, "fill_installer does not write the digest of the catalogue")
        self.assertEqual(text.count("__CASAOS_MODULE_CATALOGUE_SHA256__"), 1)

    def test_a_catalogue_that_cannot_be_read_is_not_an_empty_digest(self):
        target = os.path.join(self.tmp, "install.sh")
        shutil.copy(INSTALL_SH, target)
        with open(target, "rb") as f:
            before = f.read()
        code, out, err = self.run_functions('MODULES_CATALOGUE_FILE=%s/nothing\nfill_catalogue_digest %s' % (sh(self.output), sh(target)))
        self.assertNotEqual(code, 0, "an empty digest was written for a catalogue that is there")
        with open(target, "rb") as f:
            self.assertEqual(f.read(), before)

    def broken(self, why, reason):
        code, out, err = self.run_functions("build_modules_catalogue")
        self.assertNotEqual(code, 0, "%s: the build went on" % why)
        self.assertIn(reason, err, why)
        self.assertEqual(self.made(), [], "%s: a catalogue was written" % why)

    def test_a_release_that_cannot_be_fetched_or_does_not_hold(self):
        def release(**kw):
            shutil.rmtree(os.path.join(self.tmp, "staging"), ignore_errors=True)
            r = self.virt.clear().publish(**kw)
            self.write_env(env_text(commit=r.commit))
            return r

        r = release()
        os.remove(os.path.join(r.dir, "checksums.txt"))
        self.broken("no checksums.txt", "Failed to download the checksums of module virt")

        r = release()
        os.remove(os.path.join(r.dir, r.archive_name("arm64")))
        self.broken("an archive that is not published", "Failed to download")

        r = release(sums=lambda lines: lines[:1])
        self.broken("a checksums.txt that lists one architecture", "its checksums.txt names no usable archives")

        r = release()
        tarball(os.path.join(r.dir, r.archive_name("amd64")), [("module.json", jbytes(GOOD_MANIFEST)), ("build/sysroot/usr/bin/evil", b"x")])
        self.broken("an archive that is not the one listed", "is not the archive that checksums.txt names")

        r = release()
        self.write_env(env_text(commit=r.later))
        self.broken("a tag at another commit", "not at the pinned")

        release(manifest_arm=dict(GOOD_MANIFEST, title="Another"))
        self.broken("an arm64 manifest that differs", "are not the same")

        release(manifest=dict(GOOD_MANIFEST, unit="Bad"))
        self.broken("a manifest the reader refuses", "module.json: ")

        r = release()
        self.write_env(env_text(commit=r.commit, tag="v1"))
        self.broken("a pin that is not valid", "release/modules.env is not valid")


def main():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (ModulesEnv, Checksums, Build, Units, CommandLine, BuildScript):
        for name in loader.getTestCaseNames(case):
            if not ONLY or re.search(ONLY, "%s.%s" % (case.__name__, name)):
                suite.addTest(case(name))
    result = unittest.TextTestRunner(verbosity=2, failfast=bool(os.environ.get("FAILFAST"))).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
