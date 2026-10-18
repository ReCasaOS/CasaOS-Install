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

CORE_SOURCE=<path> is a checkout of the core (the one release/components.env pins): the rules the builder copies are held to its Go source. Without it the
sibling CasaOS directory is looked at; with no source the cases are skipped, unless release/modules.env lists a module, when the build of that release needs the source
and so do these. Run with Python 3.12: its Unicode tables (15.0) are the Go's, and the case about them fails on 3.10 (13.0), which refuses characters the core takes.
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
import shlex
import tarfile
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
BUILDER = os.environ.get("BUILDER") or os.path.join(HERE, "build-modules-catalogue.py")
BUILD_SCRIPT = os.environ.get("BUILD_SCRIPT") or os.path.join(HERE, "build-release-bundle.sh")
INSTALL_SH = os.path.join(HERE, "..", "install.sh")
UNINSTALL_SH = os.path.join(HERE, "..", "casaos-uninstall")
RELEASE_DIR = os.path.join(HERE, "..", "release")
BASH = os.environ.get("TEST_BASH") or "bash"
ONLY = os.environ.get("CASES")
sys.dont_write_bytecode = True

loading = importlib.util.spec_from_file_location("builder", BUILDER)
builder = importlib.util.module_from_spec(loading)
loading.loader.exec_module(builder)

# The oldest core that has `casaos module`, as the build script writes it (the one place it is written), and tags on both sides of it.
with open(BUILD_SCRIPT, newline="") as _f:
    _found = re.search(r'^readonly MODULES_MIN_CORE_TAG="(v[0-9]+\.[0-9]+\.[0-9]+)"\r?$', _f.read(), re.M)
MIN_CORE = _found.group(1) if _found else "v0.0.0"
MAJOR, MINOR, PATCH = [int(n) for n in MIN_CORE[1:].split(".")]


def core_tag(*numbers):
    return "v%d.%d.%d" % numbers


# (a number is a number and not a text: v0.10.0 is later than v0.4.75, v0.4.9 is earlier than it, though a text sort says the contrary of both)
CORES_FROM_THE_MINIMUM = [MIN_CORE, core_tag(MAJOR, MINOR, PATCH + 1), core_tag(MAJOR, MINOR + 1, 0), core_tag(MAJOR + 1, 0, 0), core_tag(MAJOR, 10 ** len(str(MINOR)), 0),
                          core_tag(MAJOR, MINOR, PATCH * 10 + 1)]
CORES_BELOW_THE_MINIMUM = [core_tag(0, 0, 1)] + ([core_tag(MAJOR, MINOR, PATCH - 1)] if PATCH > 0 else []) + ([core_tag(MAJOR, MINOR, 9)] if PATCH > 9 else []) + (
    [core_tag(MAJOR, MINOR - 1, 999)] if MINOR > 0 else [])
# the oldest core is a decision, not a computation: it is the tag the core release that brought `casaos module` was given
EXPECTED_MIN_CORE = "v0.4.75"

COMPONENT = "CasaOS-VirtManagement"
ARCHIVE = "linux-%s-casaos-virt-management-v0.1.1.tar.gz"
BRAILLE_BLANK = chr(0x2800)
SAME = object()  # (the module.json of the repository is the one the archives carry)
NO_MANIFEST = b"<no module.json>"
UNSET = b"<unset>"

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


def without_upgrade(manifest):
    return {k: v for k, v in manifest.items() if k != "os_upgrade"}


def sh(path):
    """A path as the MSYS bash and the curl of a Windows machine take it."""
    return os.path.abspath(path).replace("\\", "/")


def file_url(path):
    p = sh(path)
    return "file://" + ("" if p.startswith("/") else "/") + p  # (file:///C:/dir on Windows, file:///dir elsewhere)


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false"] + list(args),
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
    """members: (name, bytes) for a file, (name, None) for a directory, (name, ('link', target)) for a symbolic link, (name, ('hard', target)), (name, ('chr',)) and
    (name, ('fifo',)) for the entries that a box refuses, (name, ('sparse',)) for a file that the tar format holds as holes."""
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as tf:
        for name, data in members:
            ti = tarfile.TarInfo(name)
            if data is None:
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            elif isinstance(data, tuple) and data[0] == "sparse":
                ti.pax_headers = {"GNU.sparse.major": "1"}
                tf.addfile(ti)
            elif isinstance(data, tuple):
                ti.type = {"link": tarfile.SYMTYPE, "hard": tarfile.LNKTYPE, "chr": tarfile.CHRTYPE, "fifo": tarfile.FIFOTYPE}[data[0]]
                ti.linkname = data[1] if len(data) > 1 else ""
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))


class Release:
    """The release of one module as its repository publishes it: <root>/<component> is a git repository with the tag, and the assets of the release are in
    <root>/<component>/releases/download/<tag>/ (which is where a file:// tree has them for curl)."""

    def __init__(self, root, component=COMPONENT, tag="v0.1.1", stem="virt-management", annotated=True):
        self.root, self.component, self.tag, self.stem, self.annotated = root, component, tag, stem, annotated
        self.repo = os.path.join(root, component)
        os.makedirs(self.repo)
        git(self.repo, "init", "-q")
        self.repo_manifest = UNSET
        self.set_repo_manifest(jbytes(GOOD_MANIFEST))
        self.dir = os.path.join(self.repo, "releases", "download", tag)
        os.makedirs(self.dir)

    def set_repo_manifest(self, raw):
        """The module.json at the root of the repository at the tag: a commit that holds these bytes (None: no module.json at all), the tag on it, and a later commit. The
        commits are made only when the repository does not hold these bytes already."""
        want = NO_MANIFEST if raw is None else raw
        if want == self.repo_manifest:
            return
        if raw is None:
            git(self.repo, "rm", "-q", "--ignore-unmatch", "module.json")
            git(self.repo, "commit", "-q", "--allow-empty", "-m", "no module.json")
        else:
            with open(os.path.join(self.repo, "module.json"), "wb") as f:
                f.write(raw)
            git(self.repo, "add", "module.json")
            git(self.repo, "commit", "-q", "--allow-empty", "-m", "release")
        self.commit = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, *(["tag", "-f", "-a", "-m", self.tag, self.tag] if self.annotated else ["tag", "-f", self.tag]))
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "later")
        self.later = git(self.repo, "rev-parse", "HEAD")
        self.repo_manifest = want

    def archive_name(self, arch):
        return "linux-%s-casaos-%s-%s.tar.gz" % (arch, self.stem, self.tag)

    def clear(self):
        for entry in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, entry))
        return self

    def publish(self, manifest=GOOD_MANIFEST, manifest_arm=None, members=None, members_arm=None, sums=None, repo=SAME):
        """Writes the archives and checksums.txt. manifest is a dict or the bytes of module.json; members replace the whole content of an archive. The repository is
        made to hold the same module.json at the tag, unless repo says what it holds: the bytes, None for no module.json at all, or False for what it holds already."""
        raw = manifest if isinstance(manifest, bytes) else jbytes(manifest)
        if repo is SAME:
            self.set_repo_manifest(raw)
        elif repo is not False:
            self.set_repo_manifest(repo)
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


    def test_a_byte_that_is_not_printable_ascii_is_refused_where_it_is(self):
        good = env_text(commit="a" * 40)
        cases = {
            "U+2028 between a comment and a module": "# nothing\u2028" + good,
            "U+0085": "# nothing\u0085" + good,
            "a vertical tab": "# nothing\x0b" + good,
            "a form feed": "# nothing\x0c" + good,
            "a file separator": "# nothing\x1c" + good,
            "a group separator": "# nothing\x1d" + good,
            "a record separator": "# nothing\x1e" + good,
            "a lone carriage return": "# nothing\r" + good,
            "a carriage return in the middle of a line": good.replace("MODULE_VIRT_TAG", "MODULE_VIRT_T\rAG"),
            "a carriage return that ends the file": good + "\r",
            "a NUL": "# nothing\x00\n" + good,
            "a byte order mark": "\ufeff" + good,
            "a letter that is not ASCII, in a comment": "# \u00e9\n" + good,
            "DEL": "# x\x7f\n" + good,
            "a no-break space": "# a\u00a0b\n" + good,
        }
        for what, text in cases.items():
            with self.subTest(what):
                with open(self.env, "wb") as f:
                    f.write(text.encode("utf-8"))
                with self.assertRaises(builder.Refused) as raised:
                    builder.read_modules_env(self.env)
                self.assertIn("modules.env line", str(raised.exception))
        # and what is allowed: tabs, spaces, a carriage return before a line feed, a comment, a last line with no line end
        with open(self.env, "wb") as f:
            f.write(b"\t# a comment\r\n  \r\n" + good.replace("\n", "\r\n").encode() + b"\r\n# the last line")
        self.assertEqual(len(builder.read_modules_env(self.env)), 1)

    def test_a_file_that_cannot_be_read_is_refused(self):
        for what, path in (("a file that is not there", os.path.join(self.tmp, "nowhere")), ("a folder", self.tmp)):
            with self.subTest(what):
                with self.assertRaises(builder.Refused) as raised:
                    builder.read_modules_env(path)
                self.assertIn("cannot be read", str(raised.exception))


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
                self.offer(manifest if isinstance(manifest, bytes) else json.dumps(manifest).encode(), repo=False)
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


    def test_the_module_json_of_the_archives_is_the_one_of_the_repository_at_the_tag(self):
        different = jbytes(dict(GOOD_MANIFEST, description="Another text, in the repository."))
        cases = {
            "another text": different,
            "the same text with another line end": jbytes(GOOD_MANIFEST).replace(b"\n", b"\r\n"),
            "the same text and a line end more": jbytes(GOOD_MANIFEST) + b"\n",
            "the same text, compact": json.dumps(GOOD_MANIFEST).encode(),
        }
        for what, repo in cases.items():
            with self.subTest(what):
                r = self.virt.clear().publish(repo=repo)
                shutil.rmtree(self.stage_dir, ignore_errors=True)
                r.stage(self.stage_dir)
                self.write_env(env_text(commit=r.commit))
                self.refused("byte for byte")
        # (and the archives that are the repository's own are taken, as every other case of this file shows)

    def test_a_repository_with_no_module_json_at_the_tag(self):
        r = self.virt.clear().publish(repo=None)
        shutil.rmtree(self.stage_dir, ignore_errors=True)
        r.stage(self.stage_dir)
        self.write_env(env_text(commit=r.commit))
        self.refused("has no module.json at %s" % r.commit)

    def test_the_repository_is_looked_at_for_the_tag_and_not_for_a_branch_of_its_name(self):
        r = self.release().publish()
        r.stage(self.stage_dir)
        self.write_env(env_text(commit=r.commit))
        git(r.repo, "branch", "-f", "other", r.later)
        self.build(git_base=file_url(os.path.join(self.tmp, "own")))
        self.assertEqual(builder.parse_catalogue(self.catalogue())["modules"][0]["commit"], r.commit)

    def test_modules_may_not_share_a_unit_a_route_or_a_component(self):
        def entry(name, unit="casaos-%s.service", route="/v1/%s_x", component="CasaOS-%s"):
            return {"name": name, "component": component.replace("%s", name), "manifest": {"unit": unit.replace("%s", name), "gateway_route": route.replace("%s", name)}}

        builder.check_distinct([entry("a"), entry("b")])
        builder.check_distinct([])
        cases = {
            "a unit": ([entry("a"), entry("b", unit="casaos-a.service")], "share the unit casaos-a.service"),
            "a route": ([entry("a"), entry("b", route="/v1/a_x")], "share the gateway route /v1/a_x"),
            "a component": ([entry("a"), entry("b", component="CasaOS-a")], "share the component"),
            "a component that differs by its case only": ([entry("a"), entry("b", component="casaos-A")], "share the component"),
            "a unit, the second of three": ([entry("a"), entry("b"), entry("c", unit="casaos-b.service")], "modules b and c share the unit"),
        }
        for what, (entries, reason) in cases.items():
            with self.subTest(what):
                with self.assertRaises(builder.Refused) as raised:
                    builder.check_distinct(entries)
                self.assertIn(reason, str(raised.exception))

    def test_two_real_modules_that_share_a_unit_or_a_route_are_no_catalogue(self):
        r1 = self.good()
        base = without_upgrade(dict(GOOD_MANIFEST, name="other", component="CasaOS-Other"))
        cases = {
            "a unit": (dict(base, gateway_route="/v1/other_thing"), "share the unit"),
            "a route": (dict(base, unit="casaos-other.service"), "share the gateway route"),
        }
        for what, (manifest, reason) in cases.items():
            with self.subTest(what):
                r2 = self.other.clear().publish(manifest=manifest)
                shutil.rmtree(os.path.join(self.stage_dir, "other"), ignore_errors=True)
                r2.stage(self.stage_dir, "other")
                self.write_env(env_text(commit=r1.commit) + env_text(name="OTHER", commit=r2.commit, component="CasaOS-Other"))
                self.refused(reason)


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
            component = "CasaOS-" + name
            return {
                "name": name, "component": component, "tier": "community", "tag": "v0.1.1", "commit": module["commit"],
                "assets": {arch: {"url": "https://github.com/ReCasaOS/%s/releases/download/v0.1.1/%s" % (component, ARCHIVE % arch), "sha256": "b" * 64} for arch in ("amd64", "arm64")},
                "manifest": dict(without_upgrade(GOOD_MANIFEST), name=name, component=component, unit="casaos-%s.service" % name, gateway_route="/v1/%s_x" % name),
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


    def test_the_bounds_of_a_manifest_on_both_sides(self):
        bare = without_upgrade(GOOD_MANIFEST)

        def takes(manifest, name="virt"):
            builder.parse_manifest(jbytes(manifest), name)

        def refuses(manifest):
            with self.assertRaises(builder.Refused):
                builder.parse_manifest(jbytes(manifest), "virt")

        takes(dict(bare, title="t" * 60))
        refuses(dict(bare, title="t" * 61))
        takes(dict(bare, description="d" * 300))
        refuses(dict(bare, description="d" * 301))
        takes(dict(bare, description=""))
        takes(dict(bare, packages={"common": ["pkg%02d" % i for i in range(32)]}))
        refuses(dict(bare, packages={"common": ["pkg%02d" % i for i in range(33)]}))
        takes(dict(bare, packages={"common": ["pkg%02d" % i for i in range(20)], "amd64": ["amd%02d" % i for i in range(20)]}))
        refuses(dict(bare, packages={"common": ["pkg%02d" % i for i in range(20)], "amd64": ["amd%02d" % i for i in range(21)]}))
        takes(dict(bare, packages={"common": ["ab"]}))
        takes(dict(bare, packages={"common": ["a" + "b" * 63]}))
        refuses(dict(bare, packages={"common": ["a" + "b" * 64]}))
        takes(dict(bare, os=["debian:%d" % i for i in range(10, 26)]))
        refuses(dict(bare, os=["debian:%d" % i for i in range(10, 27)]))
        takes(dict(bare, os=["debian:99", "ubuntu:24.04"]))
        refuses(dict(bare, os=["debian:100"]))
        takes(dict(bare, min_recasaos="9999.9999.9999"))
        refuses(dict(bare, min_recasaos="10000.0.0"))
        takes(dict(bare, gateway_route="/v99/" + "a" * 40))
        refuses(dict(bare, gateway_route="/v100/a"))
        refuses(dict(bare, gateway_route="/v2/" + "a" * 41))
        takes(dict(bare, unit="a" * 64 + ".service"))
        refuses(dict(bare, unit="a" * 65 + ".service"))
        takes(dict(bare, component="C" * 64))
        refuses(dict(bare, component="C" * 65))
        for seconds in (1, 3600):
            takes(dict(GOOD_MANIFEST, os_upgrade=dict(GOOD_MANIFEST["os_upgrade"], stop_timeout_seconds=seconds)))
        for seconds in (0, 3601, -1):
            refuses(dict(GOOD_MANIFEST, os_upgrade=dict(GOOD_MANIFEST["os_upgrade"], stop_timeout_seconds=seconds)))
        takes(dict(bare, name="a" * 32), name="a" * 32)

    def test_a_unit_that_is_one_of_the_box(self):
        bare = without_upgrade(GOOD_MANIFEST)
        self.assertEqual(len(builder.RESERVED_UNITS), 15)
        for unit in builder.RESERVED_UNITS + ("casaos-module-virt.service", "casaos-module-.service", "usb-mount@sdb1.service", "usb-mount@.service"):
            with self.subTest(unit):
                self.assertFalse(builder.valid_unit(unit))
                with self.assertRaises(builder.Refused):
                    builder.parse_manifest(jbytes(dict(bare, unit=unit)), "virt")
        for unit in ("casaos-virt-management.service", "casaos-modules.service", "usb-mount.service", "casaos-gateway-x.service", "my-casaos.service", "Casaos.service"):
            with self.subTest(unit):
                self.assertTrue(builder.valid_unit(unit))
                builder.parse_manifest(jbytes(dict(bare, unit=unit)), "virt")

    def test_unicode_tables_are_those_of_the_core(self):
        # characters that Unicode 14 and 15 assigned, which the Go that builds the core (Unicode 15.0) takes in a title, and that Python 3.10 (Unicode 13.0) refuses
        for code in (0x1F6DD, 0x1FAE8, 0x1E030, 0x0CF3):
            with self.subTest(hex(code)):
                builder.parse_manifest(jbytes(dict(GOOD_MANIFEST, title="Virt " + chr(code))), "virt")


class Archives(Base):
    """What a box unpacks of an archive, and what makes it refuse the whole of it (pkg/osmodule unpack.go, files.go)."""

    UNIT = GOOD_MANIFEST["unit"]

    def members(self, *extra):
        """The entries of an archive made of module.json, the directories above the sysroot and these: as the tar reader reads them."""
        path = os.path.join(self.tmp, "members.tar.gz")
        tarball(path, [("module.json", jbytes(GOOD_MANIFEST)), ("build/", None), ("build/sysroot/", None)] + list(extra))
        return builder.read_archive(path)[1]

    def rejects(self, *extra, reason="a box would refuse the archive for this entry"):
        with self.assertRaises(builder.Refused) as raised:
            builder.check_members(self.members(*extra), self.UNIT)
        self.assertIn(reason, str(raised.exception))

    def test_what_a_box_takes(self):
        unit = self.UNIT
        accepted = {
            "a program": [("build/sysroot/usr/bin/casaos-virt-management", b"#!/bin/sh\n")],
            "the directories above it": [("build/sysroot/usr/", None), ("build/sysroot/usr/bin/", None), ("build/sysroot/usr/bin/x", b"x")],
            "every directory above the allowed ones": [("build/sysroot/%s/" % d, None) for d in ("usr", "usr/bin", "usr/lib", "usr/lib/systemd", "usr/lib/systemd/system", "lib", "lib/systemd", "lib/systemd/system", "etc", "etc/casaos")],
            "a link to a program beside it": [("build/sysroot/usr/bin/x", b"x"), ("build/sysroot/usr/bin/y", ("link", "x"))],
            "a link that says where it is": [("build/sysroot/usr/bin/y", ("link", "./x"))],
            "the unit under usr/lib": [("build/sysroot/usr/lib/systemd/system/" + unit, b"[Unit]\n")],
            "the unit under lib": [("build/sysroot/lib/systemd/system/" + unit, b"[Unit]\n")],
            "a sample": [("build/sysroot/etc/casaos/virt-management.conf.sample", b"x")],
            "the licence and the notice": [("LICENSE", b"x"), ("NOTICE", b"x")],
            "every character of a name": [("build/sysroot/usr/bin/aZ09._+@:-x", b"x")],
            "a name of 255 characters": [("build/sysroot/usr/bin/" + "a" * 255, b"x")],
            "nothing but the manifest": [],
        }
        for what, extra in accepted.items():
            with self.subTest(what):
                builder.check_members(self.members(*extra), unit)

    def test_what_makes_a_box_refuse_the_archive(self):
        unit = self.UNIT
        usr_unit = "build/sysroot/usr/lib/systemd/system/" + unit
        lib_unit = "build/sysroot/lib/systemd/system/" + unit
        refused = {
            "a path outside the sysroot": [("evil", b"x")],
            "a file at the root": [("README", b"x")],
            "a file under build": [("build/x", b"x")],
            "outside the allowed directories": [("build/sysroot/etc/passwd", b"x")],
            "the configuration itself, not a sample": [("build/sysroot/etc/casaos/virt-management.conf", b"x")],
            "a file in the state directory": [("build/sysroot/var/lib/casaos/x", b"x")],
            "a file in the shared directory": [("build/sysroot/usr/share/casaos/x", b"x")],
            "a file in usr": [("build/sysroot/usr/x", b"x")],
            "a subdirectory of an allowed directory": [("build/sysroot/usr/bin/sub/x", b"x")],
            "a directory in an allowed directory": [("build/sysroot/usr/bin/sub/", None)],
            "a directory in the sample directory": [("build/sysroot/etc/casaos/sub/", None)],
            "a directory in the state directory": [("build/sysroot/var/lib/casaos/", None)],
            "a directory in the shared directory": [("build/sysroot/usr/share/", None)],
            "a directory beside the units": [("build/sysroot/usr/lib/other/", None)],
            "a directory of another tree of units": [("build/sysroot/etc/systemd/", None)],
            "a directory of its own": [("build/sysroot/opt/", None)],
            "a drop-in of the unit": [("build/sysroot/usr/lib/systemd/system/%s.d/x.conf" % unit, b"x")],
            "a unit that is not the module's": [("build/sysroot/usr/lib/systemd/system/other.service", b"x")],
            "a timer": [("build/sysroot/usr/lib/systemd/system/casaos-virt-management.timer", b"x")],
            "the unit as a link": [(usr_unit, ("link", "x"))],
            "the unit as a directory": [(usr_unit, None)],
            "a sample as a link": [("build/sysroot/etc/casaos/x.sample", ("link", "y"))],
            "a program of the system's own: systemctl": [("build/sysroot/usr/bin/systemctl", b"x")],
            "a program of the system's own: env": [("build/sysroot/usr/bin/env", b"x")],
            "a program of the system's own: a link named sh": [("build/sysroot/usr/bin/sh", ("link", "x"))],
            "a path that goes up": [("build/sysroot/usr/bin/../../x", b"x")],
            "an absolute path": [("/etc/x", b"x")],
            "a name with a space": [("build/sysroot/usr/bin/a b", b"x")],
            "a name with a character outside ASCII": [("build/sysroot/usr/bin/\u00e9", b"x")],
            "a name with a backslash": [("build/sysroot/usr/bin/a" + chr(92) + "b", b"x")],
            "a name of 256 characters": [("build/sysroot/usr/bin/" + "a" * 256, b"x")],
            "a name of dots": [("build/sysroot/usr/bin/...", b"x")],
            "a name with an empty part": [("build/sysroot/usr//bin/x", b"x")],
            "a link to an absolute path": [("build/sysroot/usr/bin/y", ("link", "/etc/passwd"))],
            "a link that goes up": [("build/sysroot/usr/bin/y", ("link", "../x"))],
            "a link that goes up after going down": [("build/sysroot/usr/bin/y", ("link", "a/../b"))],
            "a link with an empty part": [("build/sysroot/usr/bin/y", ("link", "a//b"))],
            "a link with no target": [("build/sysroot/usr/bin/y", ("link", ""))],
            "a link of a target of 256 characters": [("build/sysroot/usr/bin/y", ("link", "a" * 256))],
            "a hard link": [("build/sysroot/usr/bin/y", ("hard", "x"))],
            "a device": [("build/sysroot/usr/bin/y", ("chr",))],
            "a fifo": [("build/sysroot/usr/bin/y", ("fifo",))],
            "a file made of holes": [("build/sysroot/usr/bin/y", ("sparse",))],
            "the licence as a directory": [("LICENSE", None)],
            "the notice as a link": [("NOTICE", ("link", "x"))],
            "the sysroot as a file": [("build/sysroot", b"x")],
            "the sysroot as a link": [("build/sysroot", ("link", "x"))],
            "a name twice": [("build/sysroot/usr/bin/x", b"x"), ("build/sysroot/usr/bin/x", b"y")],
            "names that differ only by the case": [("build/sysroot/usr/bin/Foo", b"x"), ("build/sysroot/usr/bin/foo", b"x")],
            "the unit under both directories": [(usr_unit, b"x"), (lib_unit, b"x")],
            "a directory twice": [("build/sysroot/usr/", None), ("build/sysroot/usr/", None)],
        }
        for what, extra in refused.items():
            with self.subTest(what):
                self.rejects(*extra)
        for program in builder.RESERVED_PROGRAMS:
            with self.subTest("the program " + program):
                self.rejects(("build/sysroot/usr/bin/" + program, b"x"))

    def test_the_kind_of_an_entry_is_what_the_tar_says(self):
        members = self.members(("build/sysroot/usr/bin/f", b"x"), ("build/sysroot/usr/bin/l", ("link", "f")), ("build/sysroot/usr/bin/h", ("hard", "f")),
                               ("build/sysroot/usr/bin/c", ("chr",)), ("build/sysroot/usr/bin/p", ("fifo",)), ("build/sysroot/usr/bin/s", ("sparse",)))
        self.assertEqual(
            [(n, k) for n, k, _ in members],
            [("module.json", "file"), ("build", "dir"), ("build/sysroot", "dir"), ("build/sysroot/usr/bin/f", "file"), ("build/sysroot/usr/bin/l", "link"),
             ("build/sysroot/usr/bin/h", "other"), ("build/sysroot/usr/bin/c", "other"), ("build/sysroot/usr/bin/p", "other"), ("build/sysroot/usr/bin/s", "other")],
        )

    def test_the_bounds_of_an_archive_on_both_sides(self):
        path = os.path.join(self.tmp, "bounds.tar.gz")
        good = jbytes(GOOD_MANIFEST)
        # 5000 entries are the most
        tarball(path, [("module.json", good)] + [("build/sysroot/usr/bin/f%04d" % i, b"") for i in range(4999)])
        self.assertEqual(len(builder.read_archive(path)[1]), 5000)
        tarball(path, [("module.json", good)] + [("build/sysroot/usr/bin/f%04d" % i, b"") for i in range(5000)])
        with self.assertRaises(builder.Refused) as raised:
            builder.read_archive(path)
        self.assertIn("more than 5000 entries", str(raised.exception))
        # the size of the archive, and the bytes of its files, are bounded by what a box downloads and unpacks
        tarball(path, [("module.json", good), ("build/sysroot/usr/bin/x", b"x" * 1000)])
        size = os.path.getsize(path)
        with mock.patch.object(builder, "MAX_ARCHIVE_BYTES", size):
            builder.read_archive(path)
        with mock.patch.object(builder, "MAX_ARCHIVE_BYTES", size - 1):
            with self.assertRaises(builder.Refused) as raised:
                builder.read_archive(path)
            self.assertIn("download_too_large", str(raised.exception))
        total = len(good) + 1000
        with mock.patch.object(builder, "MAX_TOTAL_BYTES", total):
            builder.read_archive(path)
        with mock.patch.object(builder, "MAX_TOTAL_BYTES", total - 1):
            with self.assertRaises(builder.Refused) as raised:
                builder.read_archive(path)
            self.assertIn("unpack_limits", str(raised.exception))

    def test_both_archives_are_looked_at_when_the_catalogue_is_built(self):
        good = jbytes(GOOD_MANIFEST)
        fine = [("module.json", good), ("build/", None), ("build/sysroot/usr/bin/casaos-virt-management", b"#!/bin/sh\n")]
        evil = fine + [("build/sysroot/etc/passwd", b"x")]
        self.offer(members=fine)
        self.build()
        os.remove(self.out)
        for what, amd64, arm64 in (("the amd64 archive", evil, fine), ("the arm64 archive", fine, evil)):
            with self.subTest(what):
                self.offer(members=amd64, members_arm=arm64)
                self.refused("a box would refuse the archive for this entry")
                with self.assertRaises(builder.Refused) as raised:
                    self.build()
                self.assertIn("linux-%s-" % ("amd64" if amd64 is evil else "arm64"), str(raised.exception))


class Proof(Base):
    """The check of what the core's own `casaos module list` printed on the catalogue that was built."""

    MODULES = [{"name": "virt", "component": COMPONENT, "tag": "v0.1.1", "commit": "a" * 40, "tier": "community"}]

    def output(self, **kw):
        listing = {"catalogue": {"ok": True}, "modules": [{"name": "virt", "title": "Virtual machines", "tag": "v0.1.1", "tier": "community", "installed": None}]}
        listing.update(kw)
        return json.dumps(listing).encode()

    def refused(self, raw, reason):
        with self.assertRaises(builder.Refused) as raised:
            builder.check_list_output(raw, self.MODULES)
        self.assertIn(reason, str(raised.exception))

    def test_one_object_that_says_the_catalogue_is_fine_and_lists_the_modules(self):
        builder.check_list_output(self.output(), self.MODULES)
        builder.check_list_output(self.output() + b"\n", self.MODULES)
        builder.check_list_output(self.output(modules=[{"name": "other"}, {"name": "virt", "tag": "v0.1.1", "tier": "community"}]), self.MODULES)  # (more is not less)
        two = self.MODULES + [dict(self.MODULES[0], name="other", tier="core", tag="v2.0.0")]
        builder.check_list_output(self.output(modules=[{"name": "virt", "tag": "v0.1.1", "tier": "community"}, {"name": "other", "tag": "v2.0.0", "tier": "core"}]), two)

    def test_what_is_not_that(self):
        ok = {"ok": True}
        cases = {
            "junk": (b"hello", "did not print one JSON object"),
            "nothing": (b"", "did not print one JSON object"),
            "a list": (b"[1]", "did not print one JSON object"),
            "text after the object": (self.output() + b" hello", "did not print one JSON object"),
            "two objects": (self.output() + b"\n" + self.output(), "did not print one JSON object"),
            "a key twice": (b'{"catalogue": {"ok": false}, "catalogue": {"ok": true}, "modules": []}', "did not print one JSON object"),
            "NaN": (self.output().replace(b"null", b"NaN"), "did not print one JSON object"),
            "bytes that are not UTF-8": (self.output().replace(b"Virtual", b"Virt\xe9"), "did not print one JSON object"),
            "a catalogue that is not ok": (self.output(catalogue={"ok": False, "error": "none"}), "does not take the catalogue"),
            "an ok that is text": (self.output(catalogue={"ok": "true"}), "does not take the catalogue"),
            "an ok that is one": (self.output(catalogue={"ok": 1}), "does not take the catalogue"),
            "no catalogue": (b'{"modules": []}', "does not take the catalogue"),
            "a catalogue that is a list": (self.output(catalogue=[ok]), "does not take the catalogue"),
            "no module": (self.output(modules=[]), "does not list module virt"),
            "modules that is null": (self.output(modules=None), "does not list module virt"),
            "modules that is an object": (self.output(modules={"virt": {}}), "does not list module virt"),
            "another module only": (self.output(modules=[{"name": "other"}]), "does not list module virt"),
            "an entry that is not an object": (self.output(modules=["virt"]), "does not list module virt"),
            "a name that is not text": (self.output(modules=[{"name": 5}]), "does not list module virt"),
            "another tag": (self.output(modules=[{"name": "virt", "tag": "v9.9.9", "tier": "community"}]), "not at the pinned"),
            "another tier": (self.output(modules=[{"name": "virt", "tag": "v0.1.1", "tier": "core"}]), "not at the pinned"),
            "no tag": (self.output(modules=[{"name": "virt", "tier": "community"}]), "not at the pinned"),
            "more than 1 MiB": (self.output() + b" " * (1 << 20), "larger than 1 MiB"),
        }
        for what, (raw, reason) in cases.items():
            with self.subTest(what):
                self.refused(raw, reason)

    def run_builder(self, *args):
        p = subprocess.run([sys.executable, "-I", BUILDER] + list(args), capture_output=True)
        return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")

    def test_the_command(self):
        self.write_env(env_text(commit="a" * 40))
        out = os.path.join(self.tmp, "out.json")
        with open(out, "wb") as f:
            f.write(self.output())
        self.assertEqual(self.run_builder("proof", self.env, out), (0, "", ""))
        with open(out, "wb") as f:
            f.write(b"hello")
        code, stdout, stderr = self.run_builder("proof", self.env, out)
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("error: the core did not print one JSON object", stderr)
        self.assertEqual(self.run_builder("proof", self.env, os.path.join(self.tmp, "nothing"))[0], 1)
        self.write_env("# nothing\n")
        self.assertEqual(self.run_builder("proof", self.env, out)[0], 1)
        self.assertEqual(self.run_builder("proof", self.env)[0], 2)


def lists_modules():
    with open(os.path.join(RELEASE_DIR, "modules.env"), "rb") as f:
        return any(line.strip() and not line.strip().startswith(b"#") for line in f.read().splitlines())


class AgainstTheCore(unittest.TestCase):
    """The rules that the builder copies are held to the Go source of the core they were copied from: a rule that changes there and not here fails this."""

    @classmethod
    def setUpClass(cls):
        cls.dir = os.path.join(os.environ.get("CORE_SOURCE") or os.path.join(HERE, "..", "..", "CasaOS"), "pkg", "osmodule")
        if not os.path.isfile(os.path.join(cls.dir, "manifest.go")):
            if lists_modules():
                raise AssertionError("release/modules.env lists modules, and there is no source of the core at %s (CORE_SOURCE)" % cls.dir)
            raise unittest.SkipTest("no source of the core at %s (CORE_SOURCE names one); it is needed once release/modules.env lists a module" % cls.dir)
        cls.go = {}
        for name in ("manifest.go", "catalogue.go", "files.go", "unpack.go", "plan.go"):
            with open(os.path.join(cls.dir, name), encoding="utf-8") as f:
                cls.go[name] = f.read()

    def text(self, name):
        return self.go[name]

    def strings(self, name, var):
        found = re.search(r"\b%s\s*=\s*\[\]string\{(.*?)\}" % var, self.text(name), re.S)
        self.assertIsNotNone(found, "%s is not in %s" % (var, name))
        return tuple(re.findall(r'"([^"]*)"', found.group(1)))

    def regex(self, name, var):
        found = re.search(r"\b%s\s*=\s*regexp\.MustCompile\(`([^`]*)`\)" % var, self.text(name))
        self.assertIsNotNone(found, "%s is not in %s" % (var, name))
        pattern = found.group(1)
        self.assertTrue(pattern.startswith("^") and pattern.endswith("$"), "%s is not anchored" % var)
        return pattern[1:-1]

    def number(self, name, var):
        found = re.search(r"\b%s\b(?:\s+int64)?\s*=\s*(\d+)(?:\s*<<\s*(\d+))?" % var, self.text(name))
        self.assertIsNotNone(found, "%s is not in %s" % (var, name))
        return int(found.group(1)) << int(found.group(2) or 0)

    def test_the_lists(self):
        for name, var, ours in (
            ("manifest.go", "reservedRoutes", builder.RESERVED_ROUTES),
            ("manifest.go", "reservedUnits", builder.RESERVED_UNITS),
            ("manifest.go", "reservedUnitPrefixes", builder.RESERVED_UNIT_PREFIXES),
            ("unpack.go", "ReservedPrograms", builder.RESERVED_PROGRAMS),
            ("files.go", "AllowedDirs", builder.ALLOWED_DIRS),
        ):
            with self.subTest(var):
                self.assertEqual(self.strings(name, var), tuple(ours))
        found = re.search(r'sampleSuffix\s*=\s*"([^"]*)"', self.text("files.go"))
        self.assertEqual(found.group(1), builder.SAMPLE_SUFFIX)

    def test_the_patterns(self):
        for name, var, ours in (
            ("manifest.go", "nameRe", builder.NAME_RE), ("manifest.go", "componentRe", builder.COMPONENT_RE), ("manifest.go", "unitRe", builder.UNIT_RE),
            ("manifest.go", "routeRe", builder.ROUTE_RE), ("manifest.go", "callRe", builder.CALL_RE), ("manifest.go", "packageRe", builder.PACKAGE_RE),
            ("manifest.go", "versionRe", builder.VERSION_RE), ("manifest.go", "osRe", builder.OS_RE),
            ("catalogue.go", "tagRe", builder.TAG_RE), ("catalogue.go", "commitRe", builder.COMMIT_RE), ("catalogue.go", "sha256Re", builder.SHA256_RE),
            ("catalogue.go", "archiveRe", builder.ARCHIVE_RE),
        ):
            with self.subTest(var):
                self.assertEqual(self.regex(name, var), ours.pattern)

    def test_the_bounds(self):
        for name, var, ours in (
            ("manifest.go", "MaxBytes", builder.MAX_MANIFEST_BYTES), ("manifest.go", "maxPackages", builder.MAX_PACKAGES), ("manifest.go", "maxPackagesAll", builder.MAX_PACKAGES_ALL),
            ("manifest.go", "maxOS", builder.MAX_OS), ("manifest.go", "maxStopUnits", builder.MAX_STOP_UNITS), ("manifest.go", "maxStopSeconds", builder.MAX_STOP_SECONDS),
            ("manifest.go", "maxTitleRunes", builder.MAX_TITLE), ("manifest.go", "maxDescRunes", builder.MAX_DESCRIPTION),
            ("catalogue.go", "MaxCatalogueBytes", builder.MAX_CATALOGUE_BYTES), ("catalogue.go", "MaxCatalogueModules", builder.MAX_CATALOGUE_MODULES), ("catalogue.go", "maxURLBytes", builder.MAX_URL_BYTES),
            ("files.go", "MaxEntries", builder.MAX_ENTRIES), ("files.go", "MaxPathBytes", builder.MAX_PATH_BYTES), ("files.go", "MaxElementBytes", builder.MAX_ELEMENT_BYTES),
            ("unpack.go", "MaxTotalBytes", builder.MAX_TOTAL_BYTES), ("plan.go", "MaxArchiveBytes", builder.MAX_ARCHIVE_BYTES),
        ):
            with self.subTest(var):
                self.assertEqual(self.number(name, var), ours)
        self.assertEqual(builder.CATALOGUE_SCHEMA, self.number("catalogue.go", "CatalogueSchema"))
        self.assertEqual(builder.MANIFEST_SCHEMA, self.number("manifest.go", "Schema"))
        self.assertEqual(builder.ORG_URL, "https://github.com/ReCasaOS/")
        self.assertIn('"https://github.com/ReCasaOS/"', self.text("catalogue.go"))

    def test_the_blank_characters(self):
        found = re.search(r'const blanks = "([^"]*)"', self.text("manifest.go"))
        self.assertIsNotNone(found)
        go_blanks = found.group(1).encode("ascii").decode("unicode_escape")
        self.assertEqual(go_blanks, builder.BLANKS)


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
        # the core that the release pins: a checkout that has pkg/modulecmd, a `go` that builds the stand-in core of the case, and the logs of both
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.workspace = os.path.join(self.tmp, "ws")
        os.makedirs(os.path.join(self.workspace, "CasaOS", "pkg", "modulecmd"))
        self.go_log, self.core_log, self.ran, self.pidfile = [os.path.join(self.tmp, n) for n in ("go.log", "core.log", "ran", "pid")]
        self.core_file = os.path.join(self.tmp, "core-stand-in")
        self.stand_in_go()
        self.stand_in_core("good")

    def script(self, name, text, directory=None):
        path = os.path.join(directory or self.bin, name)
        with open(path, "w", newline="\n") as f:
            f.write(text)
        os.chmod(path, 0o755)

    def stand_in_go(self, fails=False):
        """`go build ... -o OUT .` puts the stand-in core of the case at OUT."""
        self.script("go", """#!/bin/sh
[ "${1:-}" = generate ] && exit 0
{ echo "args=$*"; echo "cwd=$(pwd)"; echo "goos=${GOOS:-} goarch=${GOARCH:-} goarm=${GOARM:-} cgo=${CGO_ENABLED:-}"; } >> '@@LOG@@'
[ "${1:-}" = build ] || exit 3
@@FAILS@@
out=""
while [ $# -gt 0 ]; do
    if [ "$1" = -o ]; then out="$2"; fi
    shift
done
cp '@@CORE@@' "$out"
chmod +x "$out"
""".replace("@@LOG@@", sh(self.go_log)).replace("@@CORE@@", sh(self.core_file)).replace("@@FAILS@@", 'echo "compile error" >&2; exit 1' if fails else ""))

    def stand_in_core(self, kind, tag="v0.1.1", tier="community"):
        """The core that `go` builds. good reads what the probe gives it and prints what the core prints; the others are the ways a core is wrong."""
        listing = '{"catalogue":{"ok":true},"modules":[{"name":"virt","tag":"%s","tier":"%s"}]}' % (tag, tier)
        NOTOK = '{"catalogue":{"ok":false,"error":"none"},"modules":[]}'
        MISSING = '{"catalogue":{"ok":true},"modules":[]}'
        say = {
            "good": "echo '%s'" % listing,
            "nomarker": "echo '%s'" % listing,
            "wrongtag": "echo '%s'" % listing.replace(tag, "v9.9.9"),
            "wrongtier": "echo '%s'" % listing.replace(tier, "core" if tier != "core" else "community"),
            "junk": "echo hello",
            "empty": "true",
            "notok": "echo '" + NOTOK + "'",
            "missing": "echo '" + MISSING + "'",
            "two": "echo '%s'; echo '%s'" % (listing, listing),
            "array": "echo '[1]'",
            "fail": "echo 'boom: cannot start' >&2; exit 3",
            "hang": "sleep 600",
            "huge": "yes | head -c 3000000; echo '%s'" % listing,
        }[kind]
        marker = "" if kind == "nomarker" else "# CASAOS_MODULE_RESULT is a string that only a core that has the assistant holds\n"
        text = """#!/bin/sh
%(marker)secho ran >> '@@RAN@@'
{ echo "args=$*"; echo "test=${CASAOS_MODULE_TEST:-}"; echo "catalogue=${CASAOS_MODULE_CATALOGUE:-}"; echo "token=${GITHUB_TOKEN:-none}"
  echo "nonce=${CASAOS_MODULE_NONCE:-none}"; echo "cwd=$(pwd)"; echo "uid=$(id -u)"; } >> '@@LOG@@'
echo $$ > '@@PID@@'
[ "$1 $2" = "module list" ] || exit 8
[ "${CASAOS_MODULE_TEST:-}" = 1 ] || exit 8
[ -f "${CASAOS_MODULE_CATALOGUE:-/nonexistent}" ] || exit 8
if read -r line; then echo "input=$line" >> '@@LOG@@'; exit 9; fi
%(say)s
""" % {"marker": marker, "say": say}
        text = text.replace("@@RAN@@", sh(self.ran)).replace("@@LOG@@", sh(self.core_log)).replace("@@PID@@", sh(self.pidfile))
        with open(self.core_file, "w", newline="\n") as f:
            f.write(text)
        os.chmod(self.core_file, 0o755)

    def log(self, path):
        with open(path) as f:
            return f.read()

    def run_functions(self, body, python=None, core=None, probe_timeout=30, run_timeout=300):
        script = """set -euo pipefail
SCRIPT_DIR=%(scripts)s
STAGING_ROOT=%(staging)s
OUTPUT_DIR=%(output)s
MODULES_LOCK=%(env)s
CHECKSUMS_BASE_URL=%(base)s
MODULES_GIT_BASE_URL=%(base)s
RELEASE_TAG=v9.9.9
PYTHON=%(python)s
CASAOS_TAG=%(core)s
CASAOS_COMMIT=%(commit)s
WORKSPACE_ROOT=%(workspace)s
MODULES_PROBE_TIMEOUT=%(timeout)s
eval "$(tr -d '\\r' <%(script)s | sed -n '/^readonly MODULES_MIN_CORE_TAG=/p')"
[ -n "${MODULES_MIN_CORE_TAG:-}" ] || { echo "build-release-bundle.sh defines no MODULES_MIN_CORE_TAG" >&2; exit 97; }
for f in fail compute_checksum check_core_for_modules fill_modules_min_core_tag refuse_catalogue build_probe_core prove_modules_core build_modules_catalogue fill_catalogue_digest write_checksums; do
    eval "$(tr -d '\\r' <%(script)s | sed -n "/^${f}() {\\$/,/^}\\$/p")"
    declare -F "${f}" >/dev/null || { echo "build-release-bundle.sh defines no ${f}()" >&2; exit 97; }
done
%(body)s
""" % {
            "scripts": sh(HERE), "staging": sh(os.path.join(self.tmp, "staging")), "output": sh(self.output), "env": sh(self.env), "base": file_url(self.net),
            "python": sh(python or sys.executable), "script": sh(BUILD_SCRIPT), "body": body, "core": shlex.quote(MIN_CORE if core is None else core),
            "commit": "c" * 40, "workspace": sh(self.workspace), "timeout": probe_timeout,
        }
        os.makedirs(os.path.join(self.tmp, "staging"), exist_ok=True)
        # (a job's environment has things the probe must not be given, and a GOOS that the build of the core must not take)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", GIT_TERMINAL_PROMPT="0", GITHUB_TOKEN="secret", CASAOS_MODULE_NONCE="n", GOOS="windows", GOARCH="arm64",
                   PATH=self.bin + os.pathsep + os.environ["PATH"])
        # (what is left of a terminal on the input is not the probe's to read)
        out_path, err_path = os.path.join(self.tmp, "stdout.txt"), os.path.join(self.tmp, "stderr.txt")
        with open(out_path, "w") as out, open(err_path, "w") as err:
            p = subprocess.run([BASH, "-c", script], stdout=out, stderr=err, text=True, env=env, input="junk\n", timeout=run_timeout)
        return p.returncode, self.log(out_path), self.log(err_path)

    def made(self):
        return sorted(f for f in os.listdir(self.output) if f.startswith("modules-catalogue-"))

    def test_no_module_no_catalogue(self):
        self.write_env("# nothing\n\n")
        with open(os.path.join(self.output, "modules-catalogue-v0.0.1.json"), "w") as f:
            f.write("a catalogue of an earlier run\n")
        code, out, err = self.run_functions('build_modules_catalogue\necho "file=[${MODULES_CATALOGUE_FILE}]"')
        self.assertEqual((code, out.strip()), (0, "file=[]"), err)
        self.assertEqual(self.made(), [], "a catalogue of an earlier run is left in the output directory")
        self.assertFalse(os.path.exists(self.go_log), "a core was built for a release that lists no module")

    def test_a_modules_env_that_is_not_there_or_cannot_be_read_is_not_one_with_no_module(self):
        nowhere = os.path.join(self.tmp, "nowhere.env")
        folder = os.path.join(self.tmp, "folder.env")
        os.makedirs(folder)
        unreadable = os.path.join(self.tmp, "unreadable.env")
        with open(unreadable, "w") as f:
            f.write("# nothing\n")
        os.chmod(unreadable, 0)
        cases = {"a file that is not there": nowhere, "a folder": folder}
        if not os.access(unreadable, os.R_OK):  # (root, and Windows, read what has no mode)
            cases["a file that has no mode"] = unreadable
        for what, path in cases.items():
            with self.subTest(what):
                code, out, err = self.run_functions('MODULES_LOCK=%s\nbuild_modules_catalogue\necho "file=[${MODULES_CATALOGUE_FILE}]"' % sh(path))
                self.assertNotEqual(code, 0, "%s was taken for a release with no module: %s" % (what, out))
                self.assertIn("release/modules.env", err)
                self.assertNotIn("file=[", out)

    def test_a_modules_env_with_a_separator_that_grep_does_not_see_is_refused(self):
        # a module after a comment, behind a line end that python's splitlines() sees and grep does not: no reader may be left to see less than another
        for what, separator in (("U+2028", "\u2028"), ("U+0085", "\u0085"), ("a vertical tab", "\x0b"), ("a form feed", "\x0c"), ("a file separator", "\x1c"), ("a lone carriage return", "\r")):
            with self.subTest(what):
                with open(self.env, "wb") as f:
                    f.write(("# nothing" + separator + env_text(commit="a" * 40)).encode("utf-8"))
                code, out, err = self.run_functions('build_modules_catalogue\necho "file=[${MODULES_CATALOGUE_FILE}]"')
                self.assertNotEqual(code, 0, "the module behind %s was dropped: %s" % (what, out))
                self.assertIn("modules.env line 1", err)
                self.assertEqual(self.made(), [])

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

    def test_the_oldest_core_that_has_module_is_one_tag_in_one_place(self):
        with open(BUILD_SCRIPT, newline="") as f:
            text = f.read().replace("\r", "")
        self.assertEqual(text.count("\nreadonly MODULES_MIN_CORE_TAG=\"%s\"\n" % EXPECTED_MIN_CORE), 1, "the oldest core that has `casaos module` is not %s, once" % EXPECTED_MIN_CORE)
        self.assertEqual(len(re.findall(r"^[ \t]*(?:readonly )?MODULES_MIN_CORE_TAG=", text, re.M)), 1, "the oldest core is written in more than one place")
        self.assertEqual(MIN_CORE, EXPECTED_MIN_CORE)
        for path in (INSTALL_SH, UNINSTALL_SH):
            with open(path, newline="") as f:
                self.assertNotIn(MIN_CORE, f.read(), "%s writes the oldest core itself, and is not given it by the build" % os.path.basename(path))

    def test_a_release_with_modules_needs_a_core_that_has_module(self):
        for core in CORES_FROM_THE_MINIMUM:
            with self.subTest(core=core, hence="allowed"):
                code, out, err = self.run_functions("check_core_for_modules", core=core)
                self.assertEqual((code, out, err), (0, "", ""))
        for core in CORES_BELOW_THE_MINIMUM:
            with self.subTest(core=core, hence="refused"):
                code, out, err = self.run_functions("check_core_for_modules", core=core)
                self.assertNotEqual(code, 0, "%s is older than %s and was let through" % (core, MIN_CORE))
                for word in (core, MIN_CORE, "casaos module", "second server", "release/components.env"):
                    self.assertIn(word, err)

    def test_a_core_that_is_not_a_tag_cannot_be_checked_and_is_refused(self):
        for core in ("", "latest", "v1", "v1.2", MIN_CORE[1:], MIN_CORE + "-rc1", MIN_CORE + ".1", "v0.4.1234567", MIN_CORE + "\nv9.9.9", "v9.9.9\n" + MIN_CORE, "x" + MIN_CORE):
            with self.subTest(core=core):
                code, out, err = self.run_functions("check_core_for_modules", core=core)
                self.assertNotEqual(code, 0, "%r was let through" % core)
                self.assertIn("cannot be checked", err)

    def test_a_catalogue_is_not_built_for_a_core_that_has_no_module_and_nothing_is_fetched(self):
        self.good()
        shutil.rmtree(self.stage_dir)
        code, out, err = self.run_functions('CHECKSUMS_BASE_URL=file:///nonexistent\nMODULES_GIT_BASE_URL=file:///nonexistent\nbuild_modules_catalogue', core=CORES_BELOW_THE_MINIMUM[-1])
        self.assertNotEqual(code, 0, "the build went on")
        self.assertIn("casaos module", err)
        self.assertNotIn("Failed to download", err, "the checksums were fetched before the core was looked at")
        self.assertEqual(self.made(), [], "a catalogue was written")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "staging", "modules")), "something was staged before the core was looked at")
        # the same release for a core that has it
        code, out, err = self.run_functions("build_modules_catalogue", core=CORES_FROM_THE_MINIMUM[0])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.made(), ["modules-catalogue-v9.9.9.json"])

    def test_a_release_with_no_module_asks_nothing_of_its_core(self):
        self.write_env("# nothing\n\n")
        code, out, err = self.run_functions('build_modules_catalogue\necho "file=[${MODULES_CATALOGUE_FILE}]"', core=CORES_BELOW_THE_MINIMUM[0])
        self.assertEqual((code, out.strip()), (0, "file=[]"), err)

    def test_the_minimum_core_is_written_into_install_sh_and_the_uninstaller(self):
        for path in (INSTALL_SH, UNINSTALL_SH):
            with self.subTest(file=os.path.basename(path)):
                target = os.path.join(self.tmp, os.path.basename(path))
                shutil.copy(path, target)
                code, out, err = self.run_functions("fill_modules_min_core_tag %s" % sh(target))
                self.assertEqual(code, 0, err)
                with open(target, newline="") as f:
                    filled = f.read().replace("\r", "")
                with open(path, newline="") as f:
                    original = f.read().replace("\r", "")
                self.assertNotIn("__CASAOS_MODULES_MIN_CORE_TAG__", filled)
                self.assertIn('\nreadonly CASAOS_MODULES_MIN_CORE_TAG="%s"\n' % MIN_CORE, filled)
                self.assertEqual(filled.replace('"%s"' % MIN_CORE, '"__CASAOS_MODULES_MIN_CORE_TAG__"', 1), original, "the minimum is not the only thing written")

    def test_the_minimum_core_is_written_into_the_uninstaller_before_its_digest_is_taken(self):
        with open(BUILD_SCRIPT, newline="") as f:
            text = f.read().replace("\r", "")
        order = [text.index(marker) for marker in ('\ninstall -m 0755 "${INSTALLER_ROOT}/casaos-uninstall" "${OUTPUT_DIR}/casaos-uninstall"\n', '\nfill_modules_min_core_tag "${OUTPUT_DIR}/casaos-uninstall"\n',
                                                   '\nbuild_modules_catalogue\n', '\nfill_installer "${OUTPUT_DIR}/install.sh"\n')]
        self.assertEqual(order, sorted(order), "the uninstaller is not given the minimum before install.sh takes its digest")
        self.assertEqual(text.count('\nfill_modules_min_core_tag "${OUTPUT_DIR}/casaos-uninstall"\n'), 1)
        self.assertEqual(text.count('\n    fill_modules_min_core_tag "${target}"\n'), 1, "fill_installer does not write the minimum core into install.sh")
        self.assertEqual(text.count('\n    check_core_for_modules\n'), 1, "build_modules_catalogue does not look at the core")
        body = text[text.index("\nbuild_modules_catalogue() {"):]
        self.assertLess(body.index('build-modules-catalogue.py" list'), body.index("check_core_for_modules"), "the core is looked at for a release that lists no module")
        self.assertLess(body.index("check_core_for_modules"), body.index("curl"), "the core is looked at after something is fetched")
        self.assertNotIn("grep", body[:body.index("check_core_for_modules")], "modules.env is read by something other than the python script")

    def test_the_pins_of_this_tree_hold(self):
        # a release that lists a module has to pin a core that has `module`: the build of that release would fail, and so does this
        with open(os.path.join(RELEASE_DIR, "components.env")) as f:
            core = re.search(r"^CASAOS_TAG=(v[0-9]+\.[0-9]+\.[0-9]+)$", f.read(), re.M).group(1)
        with open(os.path.join(RELEASE_DIR, "modules.env")) as f:
            lists_modules = any(line.strip() and not line.strip().startswith("#") for line in f.read().splitlines())
        if lists_modules:
            self.assertGreaterEqual([int(n) for n in core[1:].split(".")], [MAJOR, MINOR, PATCH], "release/modules.env lists modules and CASAOS_TAG=%s is older than %s" % (core, MIN_CORE))

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

    # ---- the capability proof: the pinned core is built, and run on the catalogue ---------------------------------------------------------------------------

    def good_release(self, tier="community"):
        r = self.good(tier=tier)
        shutil.rmtree(self.stage_dir)  # the script fetches it itself
        return r

    def test_the_pinned_core_is_built_run_and_proved(self):
        self.good_release()
        code, out, err = self.run_functions("build_modules_catalogue")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.made(), ["modules-catalogue-v9.9.9.json"])
        self.assertIn("Proved: the core at %s reads the module catalogue" % ("c" * 40), out)
        # built in the checkout of the core, for the host (not for the GOOS and GOARCH of the job), without cgo, and nothing else was done to it
        built = self.log(self.go_log)
        self.assertRegex(built, r"(?m)^args=build -buildvcs=false -trimpath -tags musl netgo osusergo -o \S+/proof/casaos \.$")
        self.assertRegex(built, r"(?m)^cwd=\S+/ws/CasaOS$")
        self.assertIn("goos= goarch= goarm= cgo=0", built)
        self.assertEqual(built.count("args="), 1)
        # run once, as `module list` on the catalogue that was built, in the test mode that makes it read that file, with nothing of the job's environment
        ran = self.log(self.core_log)
        self.assertIn("args=module list\n", ran)
        self.assertIn("test=1\n", ran)
        self.assertRegex(ran, r"(?m)^catalogue=\S+/proof/catalogue\.json$")
        self.assertIn("token=none\n", ran)
        self.assertIn("nonce=none\n", ran)
        self.assertRegex(ran, r"(?m)^cwd=\S+/proof$")
        self.assertNotIn("input=", ran, "the probe was given something on its input")
        self.assertEqual(ran.count("args="), 1)
        with open(os.path.join(self.output, "modules-catalogue-v9.9.9.json"), "rb") as f:
            catalogue = f.read()
        self.assertEqual(builder.parse_catalogue(catalogue)["modules"][0]["name"], "virt")

    def refused_proof(self, why, reason, **kw):
        code, out, err = self.run_functions("build_modules_catalogue", **kw)
        self.assertNotEqual(code, 0, "%s: the build went on: %s" % (why, out))
        self.assertTrue(any(r in err for r in ((reason,) if isinstance(reason, str) else reason)), "%s: %s" % (why, err))
        self.assertEqual(self.made(), [], "%s: a catalogue was left" % why)
        self.assertNotIn("Proved", out)
        return err

    def test_a_core_without_the_assistant_is_never_run(self):
        self.good_release()
        self.stand_in_core("nomarker")
        err = self.refused_proof("a core that has no marker", "CASAOS_MODULE_RESULT")
        self.assertIn("starts as a second server", err)
        self.assertTrue(os.path.exists(self.go_log), "the core was not built")
        self.assertFalse(os.path.exists(self.ran), "a core that has no marker was run")

    def test_a_core_that_does_not_answer_is_killed(self):
        self.good_release()
        self.stand_in_core("hang")
        started = time.time()
        self.refused_proof("a core that hangs", "did not answer 'casaos module list' within 2 seconds and was killed", probe_timeout=2, run_timeout=120)
        self.assertLess(time.time() - started, 60, "the build waited for a core that hangs")
        with open(self.pidfile) as f:
            pid = f.read().strip()
        time.sleep(1)
        gone = subprocess.run([BASH, "-c", "kill -0 %s" % pid], capture_output=True)
        self.assertNotEqual(gone.returncode, 0, "the core that hangs is still there")

    def test_a_core_that_is_not_the_reader_of_this_catalogue(self):
        cases = {
            "junk": ("junk", "the core did not print one JSON object"),
            "nothing": ("empty", "the core did not print one JSON object"),
            "a list": ("array", "the core did not print one JSON object"),
            "two objects": ("two", "the core did not print one JSON object"),
            "a catalogue that is not ok": ("notok", "the core does not take the catalogue"),
            "no module": ("missing", "the core does not list module virt"),
            "another tag": ("wrongtag", "not at the pinned"),
            "another tier": ("wrongtier", "not at the pinned"),
            "more than it may print": ("huge", ("does not read the module catalogue", "failed 'casaos module list' with status")),
            "a status that is not 0": ("fail", "failed 'casaos module list' with status 3: boom: cannot start"),
        }
        for what, (kind, reason) in cases.items():
            with self.subTest(what):
                self.good_release()
                self.stand_in_core(kind)
                self.refused_proof(what, reason)

    def test_a_core_that_cannot_be_built_or_a_build_with_no_go(self):
        self.good_release()
        self.stand_in_go(fails=True)
        self.refused_proof("a core that does not build", "could not be built, so it could not be proved")
        self.stand_in_go()
        os.remove(self.go_log)
        # (go is looked for with `command -v`; the case makes it not be there)
        code, out, err = self.run_functions('command() { if [ "${1:-}" = -v ] && [ "${2:-}" = go ]; then return 1; fi; builtin command "$@"; }\nbuild_modules_catalogue')
        self.assertNotEqual(code, 0)
        self.assertIn("Go is needed", err)
        self.assertEqual(self.made(), [])
        self.assertFalse(os.path.exists(self.go_log))

    def test_a_checkout_of_the_core_that_has_no_assistant_is_refused_before_anything_is_fetched(self):
        self.good_release()
        shutil.rmtree(os.path.join(self.workspace, "CasaOS", "pkg", "modulecmd"))
        code, out, err = self.run_functions('CHECKSUMS_BASE_URL=file:///nonexistent\nMODULES_GIT_BASE_URL=file:///nonexistent\nbuild_modules_catalogue')
        self.assertNotEqual(code, 0)
        self.assertIn("pkg/modulecmd", err)
        self.assertIn("casaos module", err)
        self.assertNotIn("Failed to download", err)
        self.assertEqual(self.made(), [])
        self.assertFalse(os.path.exists(self.go_log))

    def test_as_nobody_when_root_and_as_the_user_otherwise(self):
        self.good_release()
        self.script("id", '#!/bin/sh\nif [ "${1:-}" = -u ]; then echo 0; else exec /usr/bin/id "$@"; fi\n')
        self.script("setpriv", '#!/bin/sh\necho "setpriv $*" >> \'%s\'\nshift 3\nexec "$@"\n' % sh(os.path.join(self.tmp, "setpriv.log")))
        code, out, err = self.run_functions("build_modules_catalogue")
        self.assertEqual(code, 0, err)
        with open(os.path.join(self.tmp, "setpriv.log")) as f:
            called = f.read().splitlines()
        self.assertEqual(len(called), 1)
        self.assertTrue(called[0].startswith("setpriv --reuid=65534 --regid=65534 --clear-groups env -i "), called[0])
        # not root: setpriv is not called
        os.remove(os.path.join(self.bin, "id"))
        os.remove(os.path.join(self.tmp, "setpriv.log"))
        code, out, err = self.run_functions("build_modules_catalogue")
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "setpriv.log")), "setpriv was called by a user who is not root")

    def test_the_proof_is_made_after_the_catalogue_and_before_the_digest_is_written(self):
        with open(BUILD_SCRIPT, newline="") as f:
            text = f.read().replace("\r", "")
        body = text[text.index("\nbuild_modules_catalogue() {"):text.index("\n}\n", text.index("\nbuild_modules_catalogue() {"))]
        self.assertLess(body.index('build-modules-catalogue.py" build'), body.index('MODULES_CATALOGUE_FILE="${file}"'), "the catalogue is named before it is built")
        self.assertLess(body.index('MODULES_CATALOGUE_FILE="${file}"'), body.index("prove_modules_core"), "the core is proved before the catalogue is built")
        self.assertEqual(body.count("prove_modules_core"), 1)
        order = [text.index(marker) for marker in ("\nbuild_modules_catalogue\n", '\nfill_installer "${OUTPUT_DIR}/install.sh"\n')]
        self.assertEqual(order, sorted(order))
        prove = text[text.index("\nprove_modules_core() {"):text.index("\n}\n", text.index("\nprove_modules_core() {"))]
        self.assertLess(prove.index("grep -ac 'CASAOS_MODULE_RESULT'"), prove.index("timeout"), "the core is run before the marker is looked for")
        self.assertLess(prove.index("build_probe_core"), prove.index("grep -ac"), "the marker is looked for in something that was not built here")
        self.assertIn("</dev/null", prove)
        self.assertIn("env -i", prove)
        self.assertIn("MODULES_PROBE_TIMEOUT:-30", prove)
        pin = text.index('readonly MODULES_MIN_CORE_TAG="')
        self.assertIn("prove_modules_core", text[pin - 1200:pin], "the tag number is not said to be the early check, with the proof the one that counts")

    # ---- the workflow ---------------------------------------------------------------------------------------------------------------------------------


WORKFLOW = os.environ.get("WORKFLOW") or os.path.join(HERE, "..", ".github", "workflows", "release.yml")


def workflow_steps():
    """The steps of the first job of release.yml: a list of the lines of each (comments between steps go with the step before them)."""
    with open(WORKFLOW, newline="") as f:
        lines = f.read().replace("\r", "").split("\n")
    steps = []
    lines = lines[lines.index("    steps:") + 1:]
    for line in lines:
        if line.startswith("      - "):
            steps.append([line])
        elif steps and (line.startswith("        ") or line.startswith("      #") or not line.strip()):
            steps[-1].append(line)
        elif steps and line.strip() and not line.startswith(" "):
            break
        elif steps and line.startswith("  ") and not line.startswith("    "):
            break
    return steps


def step_index(steps, name):
    found = [i for i, step in enumerate(steps) if step[0].strip() == "- name: " + name]
    if len(found) != 1:
        raise AssertionError("the workflow has %d steps named %r" % (len(found), name))
    return found[0]


def step_run(step):
    """The script of the run: | block of a step."""
    for i, line in enumerate(step):
        if line.rstrip() == "        run: |":
            block = []
            for rest in step[i + 1:]:
                if rest.startswith("          ") or not rest.strip():
                    block.append(rest[10:])
                else:
                    break
            return "\n".join(block).rstrip() + "\n"
    raise AssertionError("no run: | in %s" % step[0])


class Workflow(unittest.TestCase):
    def setUp(self):
        self.steps = workflow_steps()
        self.names = [step[0].strip()[len("- name: "):] for step in self.steps]

    def text(self, name):
        return "\n".join(self.steps[step_index(self.steps, name)])

    def test_python_is_pinned_before_anything_runs_it(self):
        i = step_index(self.steps, "Set up Python")
        step = self.text("Set up Python")
        self.assertRegex(step, r"uses: actions/setup-python@v[0-9]+\b")
        self.assertRegex(step, r'(?m)python-version: "?3\.12"?\s*$')
        for j, other in enumerate(self.steps[:i]):
            self.assertNotRegex("\n".join(other), r"python3?\b|\.py\b", "step %r runs python before it is pinned" % self.names[j])

    def test_go_is_set_up_from_the_core_it_builds_before_the_bundle_is_assembled(self):
        i = step_index(self.steps, "Set up Go for the proof of the module assistant")
        step = self.text("Set up Go for the proof of the module assistant")
        self.assertRegex(step, r"uses: actions/setup-go@v[0-9]+\b")
        self.assertIn("go-version-file: work/CasaOS/go.mod", step)
        self.assertGreater(i, step_index(self.steps, "Checkout CasaOS"))
        self.assertLess(i, step_index(self.steps, "Assemble the release bundle"))

    def test_the_builder_is_tested_against_the_source_of_the_core_it_pins(self):
        step = self.text("Check the module catalogue builder")
        self.assertIn("python3 scripts/test-build-modules-catalogue.py", step)
        self.assertRegex(step, r"CORE_SOURCE: \$\{\{ github\.workspace \}\}/work/CasaOS")
        self.assertGreater(step_index(self.steps, "Check the module catalogue builder"), step_index(self.steps, "Checkout CasaOS-AppManagement"))
        self.assertLess(step_index(self.steps, "Check the module catalogue builder"), step_index(self.steps, "Assemble the release bundle"))

    def test_the_bundle_script_is_run_with_the_core_and_python_it_needs(self):
        step = self.text("Assemble the release bundle")
        self.assertIn("WORKSPACE_ROOT: ${{ github.workspace }}/work", step)
        self.assertIn("run: scripts/build-release-bundle.sh dist", step)

    def catalogue_step(self, dist_files, digest, extra=()):
        """The script of the step that says whether there is a catalogue to publish, run on a dist/ with these files and an install.sh with this digest."""
        script = step_run(self.steps[step_index(self.steps, "Is there a module catalogue")])
        tmp = tempfile.mkdtemp(prefix="modwf-")
        self.addCleanup(rm_rf, tmp)
        os.makedirs(os.path.join(tmp, "dist"))
        with open(os.path.join(tmp, "dist", "install.sh"), "w", newline="\n") as f:
            f.write('#!/bin/bash\nreadonly CASAOS_UI_SHA256="%s"\nreadonly CASAOS_MODULE_CATALOGUE_SHA256="%s"\nreadonly CASAOS_MODULES_MIN_CORE_TAG="v0.4.75"\n' % ("1" * 64, digest))
        for name, data in dist_files.items():
            with open(os.path.join(tmp, "dist", name), "wb") as f:
                f.write(data)
        output = os.path.join(tmp, "github-output")
        p = subprocess.run([BASH, "-c", script], cwd=tmp, capture_output=True, text=True, env=dict(os.environ, GITHUB_OUTPUT=sh(output)))
        published = ""
        if os.path.exists(output):
            with open(output) as f:
                published = f.read()
        return p.returncode, p.stderr, published

    def test_what_is_built_is_what_is_published(self):
        data = b'{"schema": 1, "modules": []}\n'
        digest = sha256(data)
        # a catalogue and its digest: published, by the name it has
        code, err, published = self.catalogue_step({"modules-catalogue-v9.9.9.json": data}, digest)
        self.assertEqual(code, 0, err)
        self.assertEqual(published, "files<<END\ndist/modules-catalogue-v9.9.9.json\nEND\n")
        # neither: nothing to publish, and that is not an error
        code, err, published = self.catalogue_step({}, "")
        self.assertEqual(code, 0, err)
        self.assertEqual(published, "files<<END\n\nEND\n")
        # one without the other, in either way, or a digest that is not the file's, or two catalogues: the job fails and publishes nothing
        cases = {
            "a catalogue that install.sh has no digest of": ({"modules-catalogue-v9.9.9.json": data}, "", "carries no digest of"),
            "the digest of a catalogue that is not there": ({}, digest, "that was not built"),
            "a digest that is not the catalogue's": ({"modules-catalogue-v9.9.9.json": data}, "0" * 64, "is not the SHA-256 of"),
            "two catalogues": ({"modules-catalogue-v9.9.9.json": data, "modules-catalogue-v9.9.8.json": data}, digest, "more than one module catalogue"),
        }
        for what, (files, d, sentence) in cases.items():
            with self.subTest(what):
                code, err, published = self.catalogue_step(files, d)
                self.assertNotEqual(code, 0, "%s was let through" % what)
                self.assertNotIn("dist/modules-catalogue", published)
                self.assertIn(sentence, err, "the job does not say what is wrong")

    def test_the_publish_step_publishes_what_that_step_found(self):
        step = self.text("Publish release assets")
        self.assertIn("${{ steps.modules.outputs.files }}", step)
        self.assertIn("fail_on_unmatched_files: true", step)


def main():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (ModulesEnv, Checksums, Build, Units, Archives, Proof, AgainstTheCore, CommandLine, BuildScript, Workflow):
        for name in loader.getTestCaseNames(case):
            if not ONLY or re.search(ONLY, "%s.%s" % (case.__name__, name)):
                suite.addTest(case(name))
    result = unittest.TextTestRunner(verbosity=2, failfast=bool(os.environ.get("FAILFAST"))).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
