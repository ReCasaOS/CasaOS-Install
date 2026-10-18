#!/usr/bin/env python3
"""Builds the catalogue of the modules of a release (modules-catalogue-<tag>.json) from release/modules.env and from what each module's own release published.

    build-modules-catalogue.py list     MODULES_ENV
    build-modules-catalogue.py archives CHECKSUMS TAG
    build-modules-catalogue.py build    [--git-base URL] --out FILE MODULES_ENV STAGE
    build-modules-catalogue.py proof    MODULES_ENV OUTPUT

The network is not used here, except by `git fetch` of the tag of a module. scripts/build-release-bundle.sh fetches, exactly as it fetches the checksums of
the components, <stage>/<name>/checksums.txt and the two archives that checksums.txt names; this script judges them.

`list` prints one line per module of modules.env: name, component, tag, commit, tier, and nothing when the file holds none (this script is the one reader of that
file, so that no other tool can read it otherwise). `archives` prints the two archive names that a module's checksums.txt lists for a tag. `build` writes the
catalogue, and writes nothing unless every module passes: a module whose files are not all there, whose archive is not the one checksums.txt names, whose tag is
not at the pinned commit, whose module.json is not, byte for byte, the module.json that the repository has at that commit, whose two archives do not carry the
same module.json or hold a path that a box would not unpack, or whose manifest and entry would not be accepted by the reader of the box (pkg/osmodule of the
core: ParseCatalogue and Parse), is an error and there is no catalogue at all; so is a module that shares its unit, its route or its component with another.
`proof` judges what the core's own `casaos module list` printed when it was run on the catalogue that was built. Exit status: 0 done, 1 refused (the reason on
the standard error), 2 usage.

The rules of the reader of a box are copied here, to fail early with a sentence that says what is wrong; the core is the judge, and `proof` has it read the
catalogue that is shipped. scripts/test-build-modules-catalogue.py holds this copy to the Go source of the core. Run this with Python 3.12: its Unicode tables
(15.0) are those of the Go that builds the core, and the tables of 3.10 (13.0) refuse characters that the core takes.

release/modules.env: MODULE_<NAME>_TAG, MODULE_<NAME>_COMMIT, MODULE_<NAME>_COMPONENT and MODULE_<NAME>_TIER for each module. <NAME> is upper case letters, digits and
underscores, starts with a letter, and gives the module's name in lower case with each underscore a hyphen (VIRT is virt, VIRT_MANAGEMENT is virt-management). The file
holds printable ASCII, tabs and line ends and nothing else: another character could end a line for one reader and not for another.
"""
import hashlib

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import zlib

# The rules below are those of the core's reader (pkg/osmodule/catalogue.go and manifest.go), which is what decides on a box. A rule here that is looser than the
# core's ships a catalogue that every box refuses whole; a stricter one only stops a build. When the reader changes, this changes with it.
CATALOGUE_SCHEMA = 1
MANIFEST_SCHEMA = 1
MAX_MANIFEST_BYTES = 64 << 10
MAX_PACKAGES = 32
MAX_PACKAGES_ALL = 40
MAX_OS = 16
MAX_STOP_UNITS = 8
MAX_STOP_SECONDS = 3600
MAX_TITLE = 60
MAX_DESCRIPTION = 300
MAX_CATALOGUE_BYTES = 1 << 20
MAX_CATALOGUE_MODULES = 64
MAX_URL_BYTES = 512
ORG_URL = "https://github.com/ReCasaOS/"
ARCHES = ("amd64", "arm64")

NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
COMPONENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,63}")
UNIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.@-]{0,63}\.service")
ROUTE_RE = re.compile(r"/v[0-9]{1,2}/[a-z][a-z0-9_]{0,39}")
CALL_RE = re.compile(r"/[a-z0-9_.-]+(/[a-z0-9_.-]+){0,7}")
PACKAGE_RE = re.compile(r"[a-z0-9][a-z0-9+.-]{1,63}")
VERSION_RE = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")
OS_RE = re.compile(r"(debian|ubuntu):[0-9]{1,2}(\.[0-9]{2})?")
TAG_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
ARCHIVE_RE = re.compile(r"linux-(amd64|arm64)-casaos-[a-z-]+-(v[0-9.]+)\.tar\.gz")
ENV_KEY_RE = re.compile(r"MODULE_([A-Z][A-Z0-9_]*)_(TAG|COMMIT|COMPONENT|TIER)")
TIERS = ("core", "community")
RESERVED_ROUTES = (
    "sys", "users", "usb", "disks", "storage", "cloud", "recover", "other", "zt", "samba", "file", "folder", "image", "notify", "gateway", "casaos",
    "app_management", "message_bus", "local_storage", "port", "driver", "batch", "test", "health", "apps", "container",
)
BLANKS = "\u2800\u115f\u1160\u3164\uffa0"

# the units of the box that a module's unit cannot be, and the two prefixes of those that cannot either (pkg/osmodule reservedUnits and reservedUnitPrefixes)
RESERVED_UNITS = (
    "casaos-update.service", "casaos-package-update.service", "casaos-docker-update.service", "casaos-os-upgrade.service", "casaos-os-upgrade-measure.service",
    "casaos.service", "casaos-gateway.service", "casaos-user-service.service", "casaos-message-bus.service", "casaos-local-storage.service", "casaos-app-management.service",
    "rclone.service", "casaos-wsdd.service",
    "apt-daily.service", "apt-daily-upgrade.service",
)
RESERVED_UNIT_PREFIXES = ("casaos-module-", "usb-mount@")
# what an archive may put on a box (pkg/osmodule files.go and unpack.go): the directories, directly in which the files go, the programs that usr/bin may not hold,
# and the bounds of an archive; the largest archive is what a box downloads
ALLOWED_DIRS = ("usr/bin", "usr/lib/systemd/system", "lib/systemd/system", "etc/casaos")
UNIT_DIRS = ("usr/lib/systemd/system", "lib/systemd/system")
SAMPLE_SUFFIX = ".sample"
RESERVED_PROGRAMS = (
    "systemctl", "systemd-run", "journalctl", "apt-get", "apt", "apt-cache", "apt-config", "apt-key", "apt-mark", "dpkg", "dpkg-query", "dpkg-deb", "dpkg-divert", "dpkg-statoverride",
    "update-rc.d", "invoke-rc.d", "deb-systemd-helper", "deb-systemd-invoke", "sh", "dash", "bash", "env",
)
MAX_ARCHIVE_BYTES = 200 << 20
MAX_TOTAL_BYTES = 400 << 20
MAX_ENTRIES = 5000
MAX_PATH_BYTES = 1024
MAX_ELEMENT_BYTES = 255
SAFE_ELEMENT_RE = re.compile(r"[A-Za-z0-9._+@:-]+")
# the shape of a manifest: a type, a list of a shape, or the keys that an object may have
MANIFEST_SHAPE = {
    "schema": int, "name": str, "title": str, "description": str, "component": str, "unit": str, "gateway_route": str,
    "packages": {"common": [str], "amd64": [str], "arm64": [str]},
    "min_recasaos": str, "os": [str],
    "os_upgrade": {"stop_units": [str], "stop_timeout_seconds": int, "maintenance": {"prepare": str, "status": str, "resume": str}},
}


class Refused(Exception):
    """Why something is not taken; the text is what the build prints."""


def invalid(field, reason):
    return Refused("%s %s" % (field, reason))


# ---- the manifest ----------------------------------------------------------------------------------------------------------------------------------------


def load_json(raw):
    """The JSON text raw as the core's reader takes it: UTF-8, no key twice, no NaN."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("not UTF-8")

    def pairs(items):
        d = {}
        for k, v in items:
            if k in d:
                raise Refused("a key comes twice")
            d[k] = v
        return d

    def constant(c):
        raise Refused("not JSON")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError):
        raise Refused("not JSON")


def exact_shape(value, shape, at):
    """Every key of every object is one the shape has, spelled as it is, and every value has the type the shape says (null is no value)."""
    if isinstance(shape, dict):
        if not isinstance(value, dict):
            raise invalid(at, "is not an object")
        for key, child in value.items():
            if key not in shape:
                raise invalid(at + "." + key, "is not a field")
            exact_shape(child, shape[key], at + "." + key)
    elif isinstance(shape, list):
        if not isinstance(value, list):
            raise invalid(at, "is not a list")
        for child in value:
            exact_shape(child, shape[0], at)
    elif type(value) is not shape:  # (a bool is not an int, 1.0 is not an int)
        raise invalid(at, "has the wrong type")


def printable(s):
    return all((unicodedata.category(c)[0] in "LMNPS" or c == " ") and c != "\ufffd" for c in s)


def shows(s):
    return any(unicodedata.category(c)[0] in "LNPS" and c not in BLANKS for c in s)


def valid_unit(s):
    """pkg/osmodule ValidUnit(): a service by the pattern of the manifest that is none of the reserved ones."""
    return UNIT_RE.fullmatch(s) is not None and s not in RESERVED_UNITS and not s.startswith(RESERVED_UNIT_PREFIXES)


def valid_package(s):
    return PACKAGE_RE.fullmatch(s) is not None and not s.endswith(("-", "+", ".deb"))


def list_of(field, values, ok, most):
    if len(values) > most:
        raise invalid(field, "too many")
    for i, v in enumerate(values):
        if not ok(v) or values.index(v) != i:
            raise invalid(field, "value")


def validate_manifest(m, name):
    """pkg/osmodule validate(): m is a manifest that has the shape of one."""
    if m.get("schema") != MANIFEST_SCHEMA:
        raise invalid("schema", "schema")
    if m.get("name") != name:
        raise invalid("name", "value")
    title, desc = m.get("title", ""), m.get("description", "")
    if len(title) > MAX_TITLE or not printable(title) or not shows(title):
        raise invalid("title", "value")
    if len(desc) > MAX_DESCRIPTION or not printable(desc):
        raise invalid("description", "value")
    for field, rx in (("component", COMPONENT_RE), ("unit", UNIT_RE), ("gateway_route", ROUTE_RE), ("min_recasaos", VERSION_RE)):
        value = m.get(field, "")
        if value == "":
            raise invalid(field, "missing")
        if rx.fullmatch(value) is None:
            raise invalid(field, "value")
    if m["gateway_route"].lstrip("/").partition("/")[2] in RESERVED_ROUTES:
        raise invalid("gateway_route", "value")
    if not valid_unit(m["unit"]):
        raise invalid("unit", "value")
    packages = m.get("packages", {})
    for arch in ("common", "amd64", "arm64"):
        list_of("packages." + arch, packages.get(arch, []), valid_package, MAX_PACKAGES)
    if sum(len(packages.get(a, [])) for a in ("common", "amd64", "arm64")) > MAX_PACKAGES_ALL:
        raise invalid("packages", "too many")
    systems = m.get("os", [])
    if not systems:
        raise invalid("os", "missing")
    list_of("os", systems, lambda s: OS_RE.fullmatch(s) is not None, MAX_OS)
    if "os_upgrade" in m:
        validate_os_upgrade(m["os_upgrade"], m["gateway_route"], m["unit"])


def validate_os_upgrade(u, route, unit):
    units = u.get("stop_units", [])
    if not units:
        raise invalid("os_upgrade.stop_units", "missing")
    list_of("os_upgrade.stop_units", units, lambda s: s == unit, MAX_STOP_UNITS)
    if not 1 <= u.get("stop_timeout_seconds", 0) <= MAX_STOP_SECONDS:
        raise invalid("os_upgrade.stop_timeout_seconds", "value")
    for field in ("prepare", "status", "resume"):
        value = u.get("maintenance", {}).get(field, "")
        at = "os_upgrade.maintenance." + field
        if value == "":
            raise invalid(at, "missing")
        rest = value[len(route):]
        if not value.startswith(route) or CALL_RE.fullmatch(rest) is None or ".." in value or "//" in value:
            raise invalid(at, "value")


def parse_manifest(raw, name):
    """The manifest in the text raw, if the core's reader would take it for the module name."""
    if len(raw) > MAX_MANIFEST_BYTES:
        raise invalid("module.json", "too large")
    m = load_json(raw)
    exact_shape(m, MANIFEST_SHAPE, "manifest")
    validate_manifest(m, name)
    return m


# ---- the catalogue ---------------------------------------------------------------------------------------------------------------------------------------

CATALOGUE_SHAPE = {
    "schema": int,
    "modules": [{
        "name": str, "component": str, "tier": str, "tag": str, "commit": str,
        "assets": {"amd64": {"url": str, "sha256": str}, "arm64": {"url": str, "sha256": str}},
        "manifest": MANIFEST_SHAPE,
    }],
}


def asset_url(component, tag, archive):
    return "%s%s/releases/download/%s/%s" % (ORG_URL, component, tag, archive)


def validate_entry(e, at):
    """pkg/osmodule Entry.validate()."""
    for field, ok in (
        ("name", NAME_RE.fullmatch(e["name"]) is not None),
        ("component", COMPONENT_RE.fullmatch(e["component"]) is not None),
        ("tier", e["tier"] in TIERS),
        ("tag", TAG_RE.fullmatch(e["tag"]) is not None),
        ("commit", COMMIT_RE.fullmatch(e["commit"]) is not None),
    ):
        if not ok:
            raise invalid(at + "." + field, "value")
    if not e["assets"]:
        raise invalid(at + ".assets", "missing")
    for arch, a in e["assets"].items():
        url, prefix = a.get("url", ""), asset_url(e["component"], e["tag"], "")
        m = ARCHIVE_RE.fullmatch(url[len(prefix):])
        if not (url.startswith(prefix) and len(url) <= MAX_URL_BYTES and SHA256_RE.fullmatch(a.get("sha256", "")) and m and m.group(1) == arch and m.group(2) == e["tag"]):
            raise invalid(at + ".assets." + arch, "value")
    try:
        validate_manifest(e["manifest"], e["name"])
    except Refused as err:
        raise invalid(at + ".manifest", str(err))
    if e["manifest"]["component"] != e["component"]:
        raise invalid(at + ".manifest.component", "value")


def parse_catalogue(raw):
    """pkg/osmodule ParseCatalogue(): the catalogue in the text raw, or why it is not one."""
    if len(raw) > MAX_CATALOGUE_BYTES:
        raise invalid("catalogue", "too large")
    c = load_json(raw)
    exact_shape(c, {"schema": int, "modules": CATALOGUE_SHAPE["modules"]}, "catalogue")
    if c.get("schema") != CATALOGUE_SCHEMA:
        raise invalid("schema", "schema")
    if "modules" not in c:
        raise invalid("modules", "missing")
    if len(c["modules"]) > MAX_CATALOGUE_MODULES:
        raise invalid("modules", "too many")
    seen = set()
    for i, e in enumerate(c["modules"]):
        at = "modules.%d" % i
        for key in ("name", "component", "tier", "tag", "commit", "assets", "manifest"):
            if key not in e:
                raise invalid(at + "." + key, "missing")
        validate_entry(e, at)
        if e["name"] in seen:
            raise invalid(at + ".name", "value")
        seen.add(e["name"])
    return c


# ---- modules.env -----------------------------------------------------------------------------------------------------------------------------------------


def env_lines(raw):
    """The lines of modules.env as bytes, split at a line feed and nowhere else. The file may hold printable ASCII, tabs, and a carriage return in front of a line
    feed: with nothing else in it every reader (grep, bash, this one) ends a line in the same places. A byte of another kind, such as the separators of Unicode
    that Python's splitlines() takes for line ends and grep does not, could hide a module behind a comment for one of them."""
    allowed = frozenset(range(0x20, 0x7F)) | {0x09}
    segments = raw.split(b"\n")
    for n, segment in enumerate(segments, 1):
        body = segment[:-1] if segment.endswith(b"\r") and n < len(segments) else segment
        bad = next((b for b in body if b not in allowed), None)
        if bad is not None:
            raise Refused("modules.env line %d: a byte 0x%02x that is not printable ASCII or a tab (a carriage return is taken only in front of a line feed)" % (n, bad))
        yield n, body.decode("ascii")


def read_modules_env(path):
    """The modules of release/modules.env, sorted by name: [{name, component, tag, commit, tier}]. No entry is a catalogue of nothing, which the caller does not build."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as err:
        raise Refused("%s cannot be read (%s)" % (path, err.strerror or type(err).__name__))
    found = {}
    for n, line in env_lines(raw):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, eq, value = line.partition("=")
        m = ENV_KEY_RE.fullmatch(key)
        if not eq or not m:
            raise Refused("modules.env line %d: %r is not a MODULE_<NAME>_(TAG|COMMIT|COMPONENT|TIER) assignment" % (n, line[:60]))
        module, field = m.group(1), m.group(2).lower()
        if field in found.setdefault(module, {}):
            raise Refused("modules.env line %d: %s is set twice" % (n, key))
        found[module][field] = value
    modules = []
    for module in sorted(found):
        name = module.lower().replace("_", "-")
        e = found[module]
        for field in ("tag", "commit", "component", "tier"):
            if field not in e:
                raise Refused("modules.env: MODULE_%s_%s is missing" % (module, field.upper()))
        for field, ok in (
            ("name", NAME_RE.fullmatch(name) is not None),
            ("tag", TAG_RE.fullmatch(e["tag"]) is not None),
            ("commit", COMMIT_RE.fullmatch(e["commit"]) is not None),
            ("component", COMPONENT_RE.fullmatch(e["component"]) is not None),
            ("tier", e["tier"] in TIERS),
        ):
            if not ok:
                raise Refused("modules.env: the %s of module %s is not valid" % (field, module))
        modules.append(dict(e, name=name))
    return modules


# ---- what a module's release published -------------------------------------------------------------------------------------------------------------------


def find_archives(checksums_text, tag):
    """{arch: (archive, sha256)} from the text of a checksums.txt: the archives of this tag, exactly one for each architecture, from one name."""
    listed = {}
    for line in checksums_text.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        digest, name = fields[0], fields[1].lstrip("*")
        m = ARCHIVE_RE.fullmatch(name)
        if not m or m.group(2) != tag:
            continue
        if SHA256_RE.fullmatch(digest) is None:
            raise Refused("checksums.txt: the digest of %s is not 64 lower case hexadecimal digits" % name)
        if listed.setdefault(name, digest) != digest:
            raise Refused("checksums.txt: %s is listed with two digests" % name)
    found = {}
    for arch in ARCHES:
        names = [n for n in listed if n.startswith("linux-%s-" % arch)]
        if len(names) != 1:
            raise Refused("checksums.txt lists %d archives for %s at %s, not exactly one" % (len(names), arch, tag))
        found[arch] = (names[0], listed[names[0]])
    stems = {a: n.split("-casaos-", 1)[1] for a, (n, _) in found.items()}
    if len(set(stems.values())) != 1:
        raise Refused("checksums.txt: the two archives are not of the same name (%s, %s)" % (found["amd64"][0], found["arm64"][0]))
    return found


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def archive_kind(member):
    """What a box reads an entry of an archive as: file, dir, link, or other (a hard link, a device, a fifo, a sparse file: refused)."""
    if any(key.startswith("GNU.sparse") for key in member.pax_headers):
        return "other"
    if member.type in (tarfile.REGTYPE, tarfile.AREGTYPE):
        return "file"
    if member.type == tarfile.DIRTYPE:
        return "dir"
    if member.type == tarfile.SYMTYPE:
        return "link"
    return "other"


def read_archive(path):
    """(the bytes of module.json at the root of the archive, [(name, kind, linkname)] for every entry). The archive is read as the hostile data it is: no larger than a box
    downloads, no more entries or bytes than a box unpacks, and module.json a regular file, once, not larger than a manifest may be."""
    try:
        size = os.path.getsize(path)
    except OSError:
        raise Refused("not a readable file")
    if size > MAX_ARCHIVE_BYTES:
        raise Refused("the archive is %d bytes, more than the %d that a box downloads (download_too_large)" % (size, MAX_ARCHIVE_BYTES))
    found, members, total = None, [], 0
    try:
        with tarfile.open(path, "r:gz") as tf:
            for member in tf:
                if len(members) >= MAX_ENTRIES:
                    raise Refused("the archive has more than %d entries (unpack_limits)" % MAX_ENTRIES)
                kind = archive_kind(member)
                members.append((member.name, kind, member.linkname))
                if kind == "file":
                    if member.size > MAX_TOTAL_BYTES - total:
                        raise Refused("the files of the archive come to more than %d bytes (unpack_limits)" % MAX_TOTAL_BYTES)
                    total += member.size
                if member.name != "module.json":
                    continue
                if found is not None:
                    raise Refused("module.json is in the archive twice")
                if kind != "file":
                    raise Refused("module.json is not a regular file")
                if member.size > MAX_MANIFEST_BYTES:
                    raise Refused("module.json is too large")
                found = tf.extractfile(member).read()
    except (tarfile.TarError, OSError, EOFError, zlib.error) as err:
        raise Refused("not a readable tarball (%s)" % type(err).__name__)
    if found is None:
        raise Refused("no module.json at the root of the archive")
    return found, members


def read_module_json(path):
    """The bytes of module.json at the root of the archive."""
    return read_archive(path)[0]


def safe_element(el):
    """pkg/osmodule safeElement(): the characters of a file name of a program, a unit or a sample, and never only dots."""
    return 0 < len(el) <= MAX_ELEMENT_BYTES and el.strip(".") != "" and SAFE_ELEMENT_RE.fullmatch(el) is not None


def safe_rel(rel):
    return 0 < len(rel) <= MAX_PATH_BYTES and all(safe_element(el) for el in rel.split("/"))


def safe_link_target(target):
    """A link points only below the directory it is in: relative, made of safe names, never "..", never an empty part."""
    return len(target) <= MAX_ELEMENT_BYTES and all(el == "." or safe_element(el) for el in target.split("/"))


def allowed_dir_of(rel):
    return next((d for d in ALLOWED_DIRS if rel == d or rel.startswith(d + "/")), "")


def above_allowed(rel):
    return any(d == rel or d.startswith(rel + "/") for d in ALLOWED_DIRS)


def place_ok(rel, kind, unit):
    """pkg/osmodule placeOK(): only a direct child of an allowed directory; in a directory of units the unit of the manifest as a file, in etc/casaos a sample, in usr/bin
    a program (not a directory)."""
    d = allowed_dir_of(rel)
    name = rel[len(d) + 1:]
    if not d or "/" in name:
        return False
    if d in UNIT_DIRS:
        return kind == "file" and name == unit
    if d == "etc/casaos":
        return kind == "file" and name.endswith(SAMPLE_SUFFIX)
    return kind != "dir"


def classify(name, kind, linkname, unit):
    """pkg/osmodule classify(): (the path under the sysroot, whether it is written) for an entry of the archive, (None, False) for one that is read and not written, and
    Refused for the entry that makes a box refuse the whole archive."""
    def refuse(why="unpack_path"):
        return Refused("%r: a box would refuse the archive for this entry (%s)" % (name[:80], why))

    if kind == "other":
        raise refuse()
    if name in ("module.json", "LICENSE", "NOTICE"):
        if kind != "file":
            raise refuse()
        return None, False
    if kind == "dir" and name in ("build", "build/sysroot"):
        return None, False
    rel = name[len("build/sysroot/"):] if name.startswith("build/sysroot/") else ""
    if not safe_rel(rel):
        raise refuse()
    d = allowed_dir_of(rel)
    if d and rel != d:
        if not place_ok(rel, kind, unit) or rel[len("usr/bin/"):] in RESERVED_PROGRAMS and rel.startswith("usr/bin/"):
            raise refuse()
        written = True
    elif kind == "dir" and above_allowed(rel):
        return rel, False
    else:
        raise refuse()
    if kind == "link" and not safe_link_target(linkname):
        raise refuse()
    return rel, written


def merged(rel):
    """A path with lib/ spelled usr/lib/, as it is on a system with a merged /usr where the one is a link to the other."""
    return "usr/lib/" + rel[len("lib/"):] if rel.startswith("lib/") else rel


def check_members(members, unit):
    """What a box would unpack of the entries of an archive, whose unit the manifest names: nothing outside the allowed places, no entry twice (two names that differ
    only by the case, or by lib/ and usr/lib/, are one)."""
    seen = set()
    for name, kind, linkname in members:
        rel, written = classify(name, kind, linkname, unit)
        if rel is None:
            continue
        key = (merged(rel) if written else rel).lower()
        if key in seen:
            raise Refused("%r: a box would refuse the archive for this entry (a name that is in it twice: unpack_path)" % name[:80])
        seen.add(key)


def rm_tree(path):
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chmod(os.path.join(root, name), 0o700)  # (git's objects are read-only, and Windows will not delete those)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def fetch_tag(git_base, component, tag):
    """(commit, module.json): the commit that the tag of the module's repository names, as the repository says it now, and the bytes of module.json at the root of the
    repository at that commit. The tag is fetched alone, and shallow, into an empty repository of our own: nothing of what the repository holds is checked out or run."""
    url = "%s/%s" % (git_base.rstrip("/"), component)
    ref = "refs/tags/" + tag
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    work = tempfile.mkdtemp(prefix="modrepo-")

    def git(*args):
        try:
            return subprocess.run(["git"] + list(args), cwd=work, capture_output=True, env=env, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise Refused("cannot read %s (%s)" % (url, type(err).__name__))

    try:
        if git("init", "-q").returncode != 0:
            raise Refused("cannot make a repository to fetch %s into" % url)
        got = git("fetch", "-q", "--depth", "1", "--no-tags", url, ref + ":" + ref)
        if got.returncode != 0:
            if b"couldn't find remote ref" in got.stderr:
                raise Refused("%s has no tag %s" % (url, tag))
            raise Refused("cannot read the tags of %s (git fetch exit %d)" % (url, got.returncode))
        named = git("rev-parse", "--verify", "-q", ref + "^{commit}")
        commit = named.stdout.decode("ascii", "replace").strip()
        if named.returncode != 0 or COMMIT_RE.fullmatch(commit) is None:
            raise Refused("%s has no tag %s" % (url, tag))
        shown = git("show", commit + ":module.json")
        if shown.returncode != 0:
            raise Refused("%s has no module.json at %s, the commit of tag %s" % (url, commit, tag))
        return commit, shown.stdout
    finally:
        rm_tree(work)


def entry_of(module, stage, git_base):
    name, component, tag = module["name"], module["component"], module["tag"]
    folder = os.path.join(stage, name)
    try:
        with open(os.path.join(folder, "checksums.txt"), encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        raise Refused("no readable checksums.txt of %s %s was fetched" % (component, tag))
    listed = find_archives(text, tag)
    found, repository_manifest = fetch_tag(git_base, component, tag)
    if found != module["commit"]:
        raise Refused("tag %s of %s is at %s, not at the pinned %s" % (tag, component, found, module["commit"]))
    manifests, members = {}, {}
    for arch, (archive, digest) in listed.items():
        path = os.path.join(folder, archive)
        if not os.path.isfile(path):
            raise Refused("%s was not fetched" % archive)
        if sha256_of(path) != digest:
            raise Refused("%s is not the archive that checksums.txt names (its sha256 is not %s)" % (archive, digest))
        try:
            manifests[arch], members[arch] = read_archive(path)
        except Refused as err:
            raise Refused("%s: %s" % (archive, err))
    if manifests["amd64"] != manifests["arm64"]:
        raise Refused("the module.json of the amd64 archive and of the arm64 archive are not the same")
    try:
        manifest = parse_manifest(manifests["amd64"], name)
    except Refused as err:
        raise Refused("module.json: %s" % err)
    if manifest["component"] != component:
        raise Refused("module.json: component is %s, the pin says %s" % (manifest["component"], component))
    if manifests["amd64"] != repository_manifest:
        raise Refused("the module.json of the archives is not, byte for byte, the module.json that %s has at the pinned commit %s" % (component, found))
    for arch, (archive, _) in listed.items():
        try:
            check_members(members[arch], manifest["unit"])
        except Refused as err:
            raise Refused("%s: %s" % (archive, err))
    return {
        "name": name, "component": component, "tier": module["tier"], "tag": tag, "commit": module["commit"],
        "assets": {arch: {"url": asset_url(component, tag, archive), "sha256": digest} for arch, (archive, digest) in listed.items()},
        "manifest": manifest,
    }


def check_distinct(entries):
    """No two modules of a catalogue share a unit, a route or a component (a repository: GitHub's names do not tell the case): the second would be the service, the gateway
    route or the code of the first."""
    for what, get in (
        ("unit", lambda e: e["manifest"]["unit"]),
        ("gateway route", lambda e: e["manifest"]["gateway_route"]),
        ("component", lambda e: e["component"].lower()),
    ):
        seen = {}
        for e in entries:
            first = seen.setdefault(get(e), e["name"])
            if first != e["name"]:
                raise Refused("modules %s and %s share the %s %s" % (first, e["name"], what, get(e)))


def build(env_path, stage, out, git_base):
    modules = read_modules_env(env_path)
    if not modules:
        raise Refused("%s has no module: there is no catalogue to build" % env_path)
    entries = []
    for module in modules:
        try:
            entries.append(entry_of(module, stage, git_base))
        except Refused as err:
            raise Refused("module %s: %s" % (module["name"], err))
    check_distinct(entries)
    text = (json.dumps({"schema": CATALOGUE_SCHEMA, "modules": entries}, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    parse_catalogue(text)  # what is written is what the box will read
    with open(out + ".tmp", "wb") as f:
        f.write(text)
    os.replace(out + ".tmp", out)


def check_list_output(raw, modules):
    """The standard output of the core's `casaos module list`, run on the catalogue that was built: ONE JSON object, whose catalogue is ok and that lists every module of
    modules.env at the tag and the tier that were pinned. That is the reader of the box, the real one, saying it takes what is shipped."""
    if len(raw) > MAX_CATALOGUE_BYTES:
        raise Refused("the output of the core is larger than 1 MiB")
    try:
        listed = load_json(raw)
    except Refused:
        raise Refused("the core did not print one JSON object")
    if not isinstance(listed, dict):
        raise Refused("the core did not print one JSON object")
    catalogue = listed.get("catalogue")
    if not isinstance(catalogue, dict) or catalogue.get("ok") is not True:
        raise Refused("the core does not take the catalogue (%s)" % json.dumps(catalogue)[:200])
    modules_listed = listed.get("modules")
    entries = {m["name"]: m for m in (modules_listed if isinstance(modules_listed, list) else []) if isinstance(m, dict) and isinstance(m.get("name"), str)}
    for module in modules:
        got = entries.get(module["name"])
        if got is None:
            raise Refused("the core does not list module %s" % module["name"])
        if got.get("tag") != module["tag"] or got.get("tier") != module["tier"]:
            raise Refused("the core lists module %s at %s/%s, not at the pinned %s/%s" % (module["name"], got.get("tag"), got.get("tier"), module["tag"], module["tier"]))


def proof(env_path, output_path):
    modules = read_modules_env(env_path)
    if not modules:
        raise Refused("%s has no module: there is nothing to prove" % env_path)
    try:
        with open(output_path, "rb") as f:
            raw = f.read(MAX_CATALOGUE_BYTES + 1)
    except OSError:
        raise Refused("the output of the core cannot be read")
    check_list_output(raw, modules)


USAGE = "usage: build-modules-catalogue.py list MODULES_ENV | archives CHECKSUMS TAG | build --git-base URL --out FILE MODULES_ENV STAGE | proof MODULES_ENV OUTPUT"


def main(argv):
    # a line ends with a new line and nothing else, also on Windows: bash reads these lines
    sys.stdout.reconfigure(newline="\n")
    try:
        if len(argv) == 2 and argv[0] == "list":
            for m in read_modules_env(argv[1]):
                print(m["name"], m["component"], m["tag"], m["commit"], m["tier"])
        elif len(argv) == 3 and argv[0] == "archives":
            try:
                with open(argv[1], encoding="utf-8") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError):
                raise Refused("%s cannot be read" % argv[1])
            for archive, _ in find_archives(text, argv[2]).values():
                print(archive)
        elif len(argv) == 7 and argv[0] == "build" and argv[1] == "--git-base" and argv[3] == "--out":
            build(argv[5], argv[6], argv[4], argv[2])
        elif len(argv) == 3 and argv[0] == "proof":
            proof(argv[1], argv[2])
        else:
            print(USAGE, file=sys.stderr)
            return 2
    except Refused as err:
        print("error: %s" % err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
