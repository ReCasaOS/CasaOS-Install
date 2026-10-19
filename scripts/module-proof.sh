#!/usr/bin/env bash
# Proof that the module assistant of ReCasaOS (`casaos module list|plan|install|update|remove|status`, the Modules API of the core, and the transient unit the core starts for an install, an
# update and a remove) installs, runs, updates and takes off a real module on a real machine that boots, restarts and is made dirty on purpose. The module is the virtual machine module
# (CasaOS-VirtManagement); libvirt answers through its test driver (URI = test:///default), so no KVM is needed inside the guest.
#
# Unlike os-plan-proof.sh this script runs ON THE CI RUNNER and drives the guest: the guest is restarted, and a case kills the process that is installing and the unit it runs in, and the
# evidence has to be on the side that survives it. The calls to the core go through the gateway (QEMU forwards a port of the runner to the guest's port 80) with the token of a login
# of the box's own, the way the page makes them; what only the guest can say is asked of it one verb at a time (ssh, a small POSIX sh tool that the preparation writes on the guest:
# dash is what runs there). Nothing is judged here: it writes what it saw into $OUT, and module-proof-report.py turns that into PASS, FAIL, NOT TESTABLE (with its reason) or INVALID RUN.
# It stops early (and says why, in $OUT/aborted) only when it cannot go on; a step whose undo failed is recorded as such (invalid.txt), never as a failure of the assistant.
#
#   module-proof.sh build-kit MODULE_DIR KIT_DIR    on the runner: the program of the module is built the way its release builds it (CGO_ENABLED=0, the same ldflags), and the kit is
#                                                   made of it: the archive (build/sysroot/..., module.json, LICENSE, NOTICE), the test catalogue that names it (the manifest of the
#                                                   archive in it, a url of the release form, tier community), a corrupted copy, hostile archives with a catalogue each
#   module-proof.sh guest-prepare                   IN THE GUEST, as root: the published ReCasaOS is installed, the tools of the proof are written, the kit is put in place
#   module-proof.sh prepare                         on the runner: pushes the files, runs guest-prepare, fetches what it wrote, makes the box's user and logs in
#   module-proof.sh run                             on the runner: the cases below, in this order
#
# The cases (each leaves the machine as the next one needs it):
#   M1  the release carries the assistant: `casaos -v` against the release's pin and the first core that has `casaos module` (never `module` on a core of unknown version: an old core
#       given the word starts as a server)
#   M2  the empty state: list, plan and status, as root and as an unprivileged user: one JSON object each, no catalogue, nothing created (a CONTROL window of the box's own changes first)
#   M3  the plan, with the test catalogue: the checks, the packages against what apt itself says on the guest, the files of apt and dpkg unchanged, the same plan as `nobody`
#   M4  the install by hand with libvirt's test driver in /etc/casaos/virt-management.conf: the markers, the files with their hashes and modes, the lists, the manifest, the unit, the
#       gateway route, the host answer, the dashboard's card request
#   M5  the API: the list, a second install refused, the plan, the status
#   M6  the remove through the API: the job unit, the steps, what is gone and what stays, the other services untouched
#   M7  the install through the API from clean: the stale plan_id, the properties of the job unit and the glob the core's busy check asks systemd about
#   M8  the dirty matrix, each with its undo and a check that nothing is left
#   M9  kills of the assistant at each step, a stop of the unit during the packages and during the start, a SIGTERM
#   M10 a restart of the guest with the module installed, and the remove after it
#   M11 the invariance of everything the module does not own
#   U1..U9 the update of an installed module (after M11, and only on a core that has `casaos module update`, MODULE_UPDATE_MIN_CORE): what the list says of a newer tag, the plan of an
#       update (and that it writes nothing), the update by hand and through the API (the files, the lists, the manifest and the version swapped, the unit started again), a file the
#       administrator changed refusing it, a failure putting the old version back byte for byte, a kill leaving a backup that the next run puts back (never half of each version), the
#       invariance of what the module does not own, and a restart of the guest after it
#
# How the test variables reach the core. The assistant honours CASAOS_MODULE_CATALOGUE and CASAOS_MODULE_ARCHIVE_FILE only with CASAOS_MODULE_TEST=1. The commands run by hand get the three
# in their own environment. The core runs the plan and the list in its own process and the install in a transient unit started by systemd-run, and neither passes anything of its own
# environment on: for the API cases the three variables are put in the environment of systemd's manager (`systemctl set-environment`), which every unit that starts afterwards has,
# and the core is restarted so that its own process has them. This is done for the cases from M5 to M9 only, and undone (unset, restart, and the environment of the manager and of the
# core process read back) before the restart of the guest of M10. The unit of a job is transient and has no drop-in, and nothing is written to disk for it.
#
# Needs, on the runner: bash 5 (EPOCHREALTIME), curl, python3, ssh, scp, timeout, git and go (build-kit). On the guest: what the preparation installs.
# Environment (runner): OUT (default ./mp-out), WORK (default ./mp-work: the token, never in OUT), KIT (default ./kit), SSH_CONFIG (ssh_config) and SSH_HOST (vm), GW_PORT (8080: the runner's
# end of the forward to the guest's port 80), CASES (a comma list of the cases to run, m1..m11 and u; all by default; M8_ONLY and M9_ONLY pick among the dirty cases and the kills the same way, U_ONLY among u1..u9, U7_ONLY among the kills of the update), MP_FAST and MP_TICK (the tests of this script skip the sleeps and tell
# their stand-in of the guest how much time went by), PROOF_MODULE_VERSION (0.1.1), PROOF_UPDATE_VERSION (0.1.2), MODULE_MIN_CORE (the first core with `casaos module`), MODULE_UPDATE_MIN_CORE (the first core with `casaos module update`), and the waits below.

# shellcheck disable=SC2034
# (the fields of a poll are read into J_* whether or not a case looks at each of them)
MP_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C

MODULE=virt
MODULE_UNIT=casaos-virt-management.service
MODULE_ROUTE=/v2/virt_management
JOB_UNIT=casaos-module-virt.service
PROOF_VERSION="${PROOF_MODULE_VERSION:-0.1.1}"
MIN_CORE="${MODULE_MIN_CORE:-v0.4.75}"
GUEST_DIR=/usr/local/lib/mp
GUEST_KIT=/usr/local/lib/mp/kit
GUEST_TOOLS_DIR=/usr/local/lib/mp
PROOF_USER=mpproof
LIST_PATH=/v1/sys/modules
PROOF_UPDATE_VERSION="${PROOF_UPDATE_VERSION:-0.1.2}"
UPD_MIN_CORE="${MODULE_UPDATE_MIN_CORE:-v0.4.78}"

# write_mp_py <file>: the JSON of the core's answers and the kit, read and written by Python (the runner has no jq the proof can rely on). Subcommands:
#   get FILE PATH                         a value of a JSON file by its dotted path ("-" when it is not there)
#   jobrow FILE HTTP CURL UNIT T0 T1      the row of a poll of the status of a module
#   snapsplit FILE PREFIX                 the sections of a snapshot into PREFIX.<section> (status 1 when it has no end)
#   version_ge A B                        status 0 when release A is B or later
#   kit MODULE_DIR OUT VERSION COMMIT UPDATE_VERSION   the archive, the catalogues, the old and the new archive of the update and kit.json
#   plan_packages FILE                    the packages a plan (a file with its JSON) installs and upgrades, on one line
write_mp_py() {
    cat >"$1" <<'MP_PY'
import sys
# ---- the kit: the archive of the module, the catalogues that name it, and the hostile archives ---------------------------------------------------------------------
# The rules below are those of the core's reader (pkg/osmodule/catalogue.go and manifest.go), copied from scripts/build-modules-catalogue.py of the installer, which states them
# for the release build: a catalogue that this proof wrote and the box refuses whole would turn every case below into the same refusal, so the proof reads what it wrote first.
import gzip
import hashlib
import io
import json
import os
import re
import tarfile
import unicodedata

CATALOGUE_SCHEMA = 1
MANIFEST_SCHEMA = 1
MAX_MANIFEST_BYTES = 64 << 10
MAX_CATALOGUE_BYTES = 1 << 20
MAX_URL_BYTES = 512
ORG_URL = "https://github.com/ReCasaOS/"
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
TIERS = ("core", "community")
RESERVED_ROUTES = (
    "sys", "users", "usb", "disks", "storage", "cloud", "recover", "other", "zt", "samba", "file", "folder", "image", "notify", "gateway", "casaos",
    "app_management", "message_bus", "local_storage", "port", "driver", "batch", "test", "health", "apps", "container",
)
BLANKS = "⠀ᅟᅠㅤﾠ"
MANIFEST_SHAPE = {
    "schema": int, "name": str, "title": str, "description": str, "component": str, "unit": str, "gateway_route": str,
    "packages": {"common": [str], "amd64": [str], "arm64": [str]},
    "min_recasaos": str, "os": [str],
    "os_upgrade": {"stop_units": [str], "stop_timeout_seconds": int, "maintenance": {"prepare": str, "status": str, "resume": str}},
}
CATALOGUE_ENTRY_SHAPE = {
    "name": str, "component": str, "tier": str, "tag": str, "commit": str,
    "assets": {"amd64": {"url": str, "sha256": str}, "arm64": {"url": str, "sha256": str}},
    "manifest": MANIFEST_SHAPE,
}


class Refused(Exception):
    """why something is not taken"""


def invalid(field, reason):
    return Refused("%s %s" % (field, reason))


def load_json(raw):
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
    elif type(value) is not shape:
        raise invalid(at, "has the wrong type")


def printable(s):
    return all((unicodedata.category(c)[0] in "LMNPS" or c == " ") and c != "�" for c in s)


def shows(s):
    return any(unicodedata.category(c)[0] in "LNPS" and c not in BLANKS for c in s)


def valid_package(s):
    return PACKAGE_RE.fullmatch(s) is not None and not s.endswith(("-", "+", ".deb"))


def list_of(field, values, ok, most):
    if len(values) > most:
        raise invalid(field, "too many")
    for i, v in enumerate(values):
        if not ok(v) or values.index(v) != i:
            raise invalid(field, "value")


def validate_manifest(m, name):
    if m.get("schema") != MANIFEST_SCHEMA:
        raise invalid("schema", "schema")
    if m.get("name") != name:
        raise invalid("name", "value")
    title, desc = m.get("title", ""), m.get("description", "")
    if len(title) > 60 or not printable(title) or not shows(title):
        raise invalid("title", "value")
    if len(desc) > 300 or not printable(desc):
        raise invalid("description", "value")
    for field, rx in (("component", COMPONENT_RE), ("unit", UNIT_RE), ("gateway_route", ROUTE_RE), ("min_recasaos", VERSION_RE)):
        value = m.get(field, "")
        if value == "":
            raise invalid(field, "missing")
        if rx.fullmatch(value) is None:
            raise invalid(field, "value")
    if m["gateway_route"].lstrip("/").partition("/")[2] in RESERVED_ROUTES:
        raise invalid("gateway_route", "value")
    packages = m.get("packages", {})
    for arch in ("common", "amd64", "arm64"):
        list_of("packages." + arch, packages.get(arch, []), valid_package, 32)
    if sum(len(packages.get(a, [])) for a in ("common", "amd64", "arm64")) > 40:
        raise invalid("packages", "too many")
    systems = m.get("os", [])
    if not systems:
        raise invalid("os", "missing")
    list_of("os", systems, lambda s: OS_RE.fullmatch(s) is not None, 16)
    if "os_upgrade" in m:
        u = m["os_upgrade"]
        units = u.get("stop_units", [])
        if not units:
            raise invalid("os_upgrade.stop_units", "missing")
        list_of("os_upgrade.stop_units", units, lambda s: s == m["unit"], 8)
        if not 1 <= u.get("stop_timeout_seconds", 0) <= 3600:
            raise invalid("os_upgrade.stop_timeout_seconds", "value")
        for field in ("prepare", "status", "resume"):
            value = u.get("maintenance", {}).get(field, "")
            rest = value[len(m["gateway_route"]):]
            if value == "" or not value.startswith(m["gateway_route"]) or CALL_RE.fullmatch(rest) is None or ".." in value or "//" in value:
                raise invalid("os_upgrade.maintenance." + field, "value")


def parse_manifest(raw, name):
    if len(raw) > MAX_MANIFEST_BYTES:
        raise invalid("module.json", "too large")
    m = load_json(raw)
    exact_shape(m, MANIFEST_SHAPE, "manifest")
    validate_manifest(m, name)
    return m


def asset_url(component, tag, archive):
    return "%s%s/releases/download/%s/%s" % (ORG_URL, component, tag, archive)


def validate_entry(e, at):
    for field, ok in (("name", NAME_RE.fullmatch(e["name"]) is not None), ("component", COMPONENT_RE.fullmatch(e["component"]) is not None), ("tier", e["tier"] in TIERS),
                      ("tag", TAG_RE.fullmatch(e["tag"]) is not None), ("commit", COMMIT_RE.fullmatch(e["commit"]) is not None)):
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
    if len(raw) > MAX_CATALOGUE_BYTES:
        raise invalid("catalogue", "too large")
    c = load_json(raw)
    exact_shape(c, {"schema": int, "modules": [CATALOGUE_ENTRY_SHAPE]}, "catalogue")
    if c.get("schema") != CATALOGUE_SCHEMA:
        raise invalid("schema", "schema")
    if "modules" not in c:
        raise invalid("modules", "missing")
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


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


# the archive as the module's goreleaser makes it: the tree build/, module.json, LICENSE and NOTICE, entries named from the root of the archive; the files that are programs
# (usr/bin) are 0755, the others 0644, the directories 0755, nobody's but root's
EPOCH = 1700000000


def entry_dir(name):
    return {"name": name, "type": "d", "mode": 0o755}


def entry_file(name, data, mode=0o644):
    return {"name": name, "type": "f", "mode": mode, "data": data}


def tree_entries(module_dir):
    entries = []
    base = os.path.join(module_dir, "build")
    if not os.path.isdir(base):
        raise Refused("the module has no build/ tree")

    def walk(rel):
        full = os.path.join(module_dir, rel)
        entries.append(entry_dir(rel))
        for name in sorted(os.listdir(full)):
            child = rel + "/" + name
            path = os.path.join(module_dir, child)
            if os.path.isdir(path):
                walk(child)
            elif os.path.isfile(path):
                with open(path, "rb") as f:
                    data = f.read()
                entries.append(entry_file(child, data, 0o755 if child.startswith("build/sysroot/usr/bin/") else 0o644))
    walk("build")
    for name in ("module.json", "LICENSE", "NOTICE"):
        path = os.path.join(module_dir, name)
        if not os.path.isfile(path):
            raise Refused("the module has no %s" % name)
        with open(path, "rb") as f:
            entries.append(entry_file(name, f.read()))
    return entries


def tarball(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tf:
        for e in entries:
            ti = tarfile.TarInfo(e["name"])
            ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname, ti.mode = EPOCH, 0, 0, "root", "root", e["mode"]
            if e["type"] == "d":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            elif e["type"] == "l":
                ti.type, ti.linkname = tarfile.SYMTYPE, e["link"]
                tf.addfile(ti)
            else:
                ti.size = len(e["data"])
                tf.addfile(ti, io.BytesIO(e["data"]))
    return gzip.compress(buf.getvalue(), 9, mtime=0)


def catalogue_text(manifest, project, component, tag, commit, archive_sha):
    archive = "linux-amd64-casaos-%s-%s.tar.gz" % (project, tag)
    entry = {"name": manifest["name"], "component": component, "tier": "community", "tag": tag, "commit": commit,
             "assets": {"amd64": {"url": asset_url(component, tag, archive), "sha256": archive_sha}}, "manifest": manifest}
    text = (json.dumps({"schema": CATALOGUE_SCHEMA, "modules": [entry]}, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    parse_catalogue(text)       # what is written is what the box will read
    return text


# the hostile archives: the good one with one more entry, which the checking of the whole archive has to refuse before a single file is written
HOSTILE = {
    "dotdot": ("build/sysroot/usr/bin/../../../../tmp/mp-escape", b"escape\n", 0o644),
    "systemctl": ("build/sysroot/usr/bin/systemctl", b"#!/bin/sh\necho owned\n", 0o755),
    "varlib": ("build/sysroot/var/lib/casaos/mp-var.txt", b"var\n", 0o644),
    "usrshare": ("build/sysroot/usr/share/casaos/mp-share.txt", b"share\n", 0o644),
    "etcmod": ("build/sysroot/etc/casaos/modules/mp-mod.json", b"{}\n", 0o644),
}


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def project_of(component):
    """CasaOS-VirtManagement -> virt-management: the name the archives of a component carry"""
    stem = re.sub(r"^CasaOS-", "", component)
    return "".join(("-" + c.lower()) if c.isupper() else c for c in stem).lstrip("-")


# the update: two archives of the module that is the same but for what the update has to swap. The old one (the proof's version) has a second sample that the new one drops; the new one
# (the update version) has another second sample, a unit file that is not byte for byte the old one's, and a manifest that names one package more. A third archive is the new one with a
# unit file the system cannot load. Each has a test catalogue (tier community) that names it.
UPDATE_PACKAGE = "hello"
LEGACY_SAMPLE = "build/sysroot/etc/casaos/mp-legacy.conf.sample"
NEXT_SAMPLE = "build/sysroot/etc/casaos/mp-next.conf.sample"


def with_data(entries, name, fn):
    hit = [e for e in entries if e["name"] == name and e["type"] == "f"]
    if len(hit) != 1:
        raise Refused("the module has no %s" % name)
    return [dict(e, data=fn(e["data"])) if e is hit[0] else e for e in entries]


def version_files(entries):
    return [{"path": "/" + e["name"][len("build/sysroot/"):], "sha256": sha256_bytes(e["data"]), "mode": "%o" % e["mode"], "bytes": len(e["data"])}
            for e in entries if e["type"] == "f" and e["name"].startswith("build/sysroot/")]


def update_kit(entries, raw, manifest, project, component, commit, old_tag, new_tag, out):
    unit_entry = "build/sysroot/usr/lib/systemd/system/" + manifest["unit"]
    pk = manifest.get("packages", {})
    if UPDATE_PACKAGE in pk.get("common", []) + pk.get("amd64", []) + pk.get("arm64", []):
        raise Refused("the module already names %s, which the new version is to add" % UPDATE_PACKAGE)
    new_manifest = json.loads(json.dumps(manifest))
    new_manifest.setdefault("packages", {}).setdefault("common", []).append(UPDATE_PACKAGE)
    new_raw = (json.dumps(new_manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    parse_manifest(new_raw, manifest["name"])
    old_entries = entries + [entry_file(LEGACY_SAMPLE, ("# the sample of %s, which the next version no longer has\n" % old_tag).encode())]
    new_entries = with_data(entries, "module.json", lambda _: new_raw)
    new_entries = with_data(new_entries, unit_entry, lambda d: d + ("# casaos module proof %s\n" % new_tag).encode())
    new_entries = new_entries + [entry_file(NEXT_SAMPLE, ("# the sample that %s adds\n" % new_tag).encode())]
    bad_entries = with_data(new_entries, unit_entry, lambda d: re.sub(rb"(?m)^ExecStart=.*$", b"ExecStart=casaos-module-proof-not-absolute", d))
    if bad_entries == new_entries:
        raise Refused("the unit file has no `ExecStart=` line to spoil")
    infos, variants = {}, {}
    for key, name, tag, ents, man, man_raw in (("old", "uold", old_tag, old_entries, manifest, raw), ("new", "unew", new_tag, new_entries, new_manifest, new_raw),
                                               ("bad", "ubad", new_tag, bad_entries, new_manifest, new_raw)):
        blob = tarball(ents)
        arc, cat = "upd-%s.tar.gz" % key, "catalogue-upd-%s.json" % key
        write(os.path.join(out, arc), blob)
        write(os.path.join(out, cat), catalogue_text(man, project, component, tag, commit, sha256_bytes(blob)))
        mp = man.get("packages", {})
        infos[key] = {"tag": tag, "archive": {"sha256": sha256_bytes(blob), "bytes": len(blob), "url": asset_url(component, tag, "linux-amd64-casaos-%s-%s.tar.gz" % (project, tag))},
                      "files": version_files(ents), "manifest_sha256": sha256_bytes(man_raw), "packages": sorted(set(mp.get("common", []) + mp.get("amd64", []))), "catalogue": cat, "archive_file": arc}
        variants[name] = {"archive": arc, "catalogue": cat}
    write(os.path.join(out, "upd-packages.list"), (UPDATE_PACKAGE + "\n").encode())
    infos.update(variants=variants, added=[UPDATE_PACKAGE], legacy_path="/" + LEGACY_SAMPLE[len("build/sysroot/"):], next_path="/" + NEXT_SAMPLE[len("build/sysroot/"):])
    return infos


def cmd_kit(module_dir, out, version, commit, update_version):
    """kit MODULE_DIR OUT VERSION COMMIT UPDATE_VERSION: the archive of the module as its release makes it, the test catalogue that names it, a corrupted copy, the hostile archives with a
    catalogue for each, the old and the new archive of the update with a catalogue each, the list of what the archives put on a box, the module's packages, the configuration for libvirt's
    test driver, and kit.json, which the report reads."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", update_version) or not COMMIT_RE.fullmatch(commit):
        raise Refused("the version or the commit is not one")
    if tuple(int(x) for x in update_version.split(".")) <= tuple(int(x) for x in version.split(".")):
        raise Refused("the version of the update (%s) is not later than the proof's (%s)" % (update_version, version))
    tag = "v" + version
    with open(os.path.join(module_dir, "module.json"), "rb") as f:
        raw = f.read()
    manifest = parse_manifest(raw, load_json(raw).get("name", ""))
    name, component = manifest["name"], manifest["component"]
    project = project_of(component)
    os.makedirs(out, exist_ok=True)
    entries = tree_entries(module_dir)
    good = tarball(entries)
    write(os.path.join(out, "archive.tar.gz"), good)
    sha = sha256_bytes(good)
    write(os.path.join(out, "catalogue.json"), catalogue_text(manifest, project, component, tag, commit, sha))
    middle = len(good) // 2
    write(os.path.join(out, "corrupt.tar.gz"), good[:middle] + bytes([good[middle] ^ 0xFF]) + good[middle + 1:])
    variants = {"good": {"archive": "archive.tar.gz", "catalogue": "catalogue.json"}, "corrupt": {"archive": "corrupt.tar.gz", "catalogue": "catalogue.json"}}
    for key, (path, data, mode) in sorted(HOSTILE.items()):
        bad = tarball(entries + [entry_file(path, data, mode)])
        write(os.path.join(out, key + ".tar.gz"), bad)
        write(os.path.join(out, "catalogue-%s.json" % key), catalogue_text(manifest, project, component, tag, commit, sha256_bytes(bad)))
        variants[key] = {"archive": key + ".tar.gz", "catalogue": "catalogue-%s.json" % key, "entry": path}
    files = version_files(entries)
    update = update_kit(entries, raw, manifest, project, component, commit, tag, "v" + update_version, out)
    packages = sorted(set(manifest.get("packages", {}).get("common", []) + manifest.get("packages", {}).get("amd64", [])))
    unit = manifest["unit"]
    footprint = [f["path"] for f in files] + [f["path"] for k in ("old", "new", "bad") for f in update[k]["files"]] + ["/lib/systemd/system/" + unit, "/usr/lib/systemd/system/" + unit, "/etc/systemd/system/multi-user.target.wants/" + unit,
                                                "/etc/casaos/modules/%s.json" % name, "/var/lib/casaos/modules/%s.files" % name, "/var/lib/casaos/modules/%s.dirs" % name,
                                                "/var/lib/casaos/modules/%s.version" % name]
    seen, uniq = set(), []
    for p in footprint:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    write(os.path.join(out, "footprint.list"), ("\n".join(uniq) + "\n").encode())
    write(os.path.join(out, "packages.list"), ("\n".join(packages) + "\n").encode())
    sample = [f for f in files if f["path"].endswith(".conf.sample")]
    if len(sample) != 1:
        raise Refused("the archive does not hold exactly one sample of the configuration")
    sample_text = [e for e in entries if e["name"] == "build/sysroot" + sample[0]["path"]][0]["data"].decode("utf-8")
    conf, n = re.subn(r"(?m)^URI[ \t]*=[ \t]*\r?$", "URI = test:///default", sample_text)
    if n != 1:
        raise Refused("the sample of the configuration has no empty `URI =` line to put libvirt's test driver in")
    write(os.path.join(out, "virt-management.conf"), conf.encode())
    info = {"version": version, "tag": tag, "name": name, "title": manifest["title"], "component": component, "unit": unit, "route": manifest["gateway_route"],
            "min_recasaos": manifest["min_recasaos"], "os": manifest["os"], "packages": packages, "commit": commit,
            "archive": {"sha256": sha, "bytes": len(good), "url": asset_url(component, tag, "linux-amd64-casaos-%s-%s.tar.gz" % (project, tag))},
            "conf": "virt-management.conf", "conf_path": "/etc/casaos/" + os.path.basename(sample[0]["path"])[:-len(".sample")], "files": files, "footprint": uniq, "variants": variants, "update": update}
    info["conf_sha256"] = sha256_bytes(conf.encode())
    info["manifest_sha256"] = sha256_bytes(raw)
    write(os.path.join(out, "kit.json"), (json.dumps(info, indent=2) + "\n").encode())
    return info


# ---- the small commands the runner asks of Python ----------------------------------------------------------------------------------------------------------------


def load(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return None


def show(v):
    if v is None:
        return "-"
    if isinstance(v, str):
        return v if v != "" else "-"
    return json.dumps(v, separators=(",", ":"))


def clean(s):
    return re.sub(r"[\t\r\n]+", " ", s)


def walk(d, path):
    for k in path.split("."):
        d = d[int(k)] if isinstance(d, list) else d[k]
    return d


def data_of(path):
    body = load(path)
    d = body.get("data") if isinstance(body, dict) else None
    return d if isinstance(d, dict) else {}


def sub(d, key):
    v = d.get(key)
    return v if isinstance(v, dict) else {}


def cmd_get(path, dotted):
    try:
        print(clean(show(walk(load(path), dotted))))
    except Exception:
        print("-")


def cmd_jobrow(path, http, curl, unit, t0, t1):
    """the row of a poll of GET /v1/sys/modules/<name>/status: t0, t1, HTTP, curl, job state, step, code, undo, kept packages, installed, version, unit active, unit enabled, route answers,
    and the job unit as the guest says it"""
    d = data_of(path)
    job, st = sub(d, "job"), sub(d, "status")
    unit_state, route = sub(st, "unit"), sub(st, "route")
    cells = [t0, t1, http, curl, job.get("state"), job.get("step"), job.get("code"), job.get("undo"), job.get("kept_packages"), st.get("installed"), st.get("version"),
             unit_state.get("active"), unit_state.get("enabled"), route.get("answers"), unit]
    print("\t".join(clean(show(c)) for c in cells))


def cmd_snapsplit(path, prefix):
    sections, name, seen_end = {}, None, False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("@@ "):
                name = line[3:].strip()
                sections.setdefault(name, [])
                seen_end = seen_end or name == "end"
            elif name is not None:
                sections[name].append(line)
    if not seen_end or "facts" not in sections:
        sys.exit(1)
    for name, lines in sections.items():
        if name != "end":
            with open("%s.%s" % (prefix, name), "w", encoding="utf-8", newline="\n") as f:
                f.write("".join(l + "\n" for l in lines))


def version_tuple(v):
    m = re.fullmatch(r"v?([0-9]+)\.([0-9]+)\.([0-9]+)", v.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def cmd_version_ge(a, b):
    """exit status 0 when release a is b or a later one, 1 when it is earlier, 3 when one of them is not a release"""
    ta, tb = version_tuple(a), version_tuple(b)
    if ta is None or tb is None:
        sys.exit(3)
    sys.exit(0 if ta >= tb else 1)


def cmd_plan_packages(path):
    """the names of the packages a plan says it installs and upgrades, on one line"""
    pk = (load(path) or {}).get("packages", {})
    names = [n for n in (pk.get("to_install") or []) + (pk.get("to_upgrade") or []) if isinstance(n, str) and re.fullmatch(r"[a-z0-9][a-z0-9+.-]{0,63}", n)]
    print(" ".join(names))


def cmd_kitcmd(module_dir, out, version, commit, update_version):
    try:
        cmd_kit(module_dir, out, version, commit, update_version)
    except Refused as err:
        sys.stderr.write("the kit could not be made: %s\n" % err)
        sys.exit(1)


COMMANDS = {"get": (cmd_get, 2), "jobrow": (cmd_jobrow, 6), "snapsplit": (cmd_snapsplit, 2), "version_ge": (cmd_version_ge, 2), "kit": (cmd_kitcmd, 5), "plan_packages": (cmd_plan_packages, 1)}
if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS or len(sys.argv) - 2 != COMMANDS[sys.argv[1]][1]:
    sys.stderr.write("usage: mp.py get|jobrow|snapsplit|version_ge|kit|plan_packages ...\n")
    sys.exit(2)
COMMANDS[sys.argv[1]][0](*sys.argv[2:])
MP_PY
}

# write_guest_tools <dir>: what the runner asks of the guest, one verb a call. POSIX sh (dash). The `snap` verb prints the machine in sections ("@@ <name>" lines, the last one "@@ end":
# a snapshot that was cut short has no end and is not believed).
write_guest_tools() {
    mkdir -p "$1"
    cat >"$1/guest.sh" <<'MP_GUEST'
#!/bin/sh
# What the proof asks of the guest, one verb a call. POSIX sh (dash).
LC_ALL=C; export LC_ALL
PATH=/usr/sbin:/usr/bin:/sbin:/bin; export PATH
MP=/usr/local/lib/mp
KIT=$MP/kit
OSR=/usr/local/lib/osr
MOD=virt
UNIT=casaos-virt-management.service
JOBUNIT=casaos-module-virt.service
LOG=/var/log/casaos/module-virt.log
TMPD=/var/lib/casaos/modules/tmp
STATE=/var/lib/casaos/modules
FLAGDIR=/var/lib/casaos/os-upgrade
CONF=/etc/casaos/virt-management.conf
CORE_UNITS="casaos.service casaos-gateway.service casaos-user-service.service casaos-message-bus.service casaos-local-storage.service casaos-app-management.service rclone.service"

sec() { printf '@@ %s\n' "$1"; }
yn() { if "$@" >/dev/null 2>&1; then echo yes; else echo no; fi; }
val() { x="$("$@" 2>/dev/null | sed -n 1p)"; printf '%s' "${x:-none}"; }
num() { "$@" 2>/dev/null | wc -l | tr -d ' '; }

variant_files() {
    case "$1" in
    good) VCAT=catalogue.json; VARC=archive.tar.gz ;;
    corrupt) VCAT=catalogue.json; VARC=corrupt.tar.gz ;;
    dotdot | systemctl | varlib | usrshare | etcmod) VCAT="catalogue-$1.json"; VARC="$1.tar.gz" ;;
    uold) VCAT=catalogue-upd-old.json; VARC=upd-old.tar.gz ;;
    unew) VCAT=catalogue-upd-new.json; VARC=upd-new.tar.gz ;;
    ubad) VCAT=catalogue-upd-bad.json; VARC=upd-bad.tar.gz ;;
    *) return 1 ;;
    esac
}

# mod <variant> <user> <arguments of casaos module>: the assistant, as root or as another user, with the three test variables for the variant ("none": without them, and with none of
# them in the environment it inherits). Its output and its exit status are the verb's.
mod() {
    variant="$1"; user="$2"; shift 2
    if [ "$variant" = none ]; then
        set -- env -u CASAOS_MODULE_TEST -u CASAOS_MODULE_CATALOGUE -u CASAOS_MODULE_ARCHIVE_FILE /usr/bin/casaos module "$@"
    else
        variant_files "$variant" || { echo "unknown variant: $variant" >&2; return 2; }
        set -- env CASAOS_MODULE_TEST=1 "CASAOS_MODULE_CATALOGUE=$KIT/$VCAT" "CASAOS_MODULE_ARCHIVE_FILE=$KIT/$VARC" /usr/bin/casaos module "$@"
    fi
    if [ "$user" = root ]; then exec "$@"; fi
    exec runuser -u "$user" -- "$@"
}

footprint() {
    for p in $(cat "$KIT/footprint.list"); do
        if [ -e "$p" ] || [ -L "$p" ]; then echo "$p"; fi
    done
    if systemctl is-enabled "$UNIT" >/dev/null 2>&1; then echo "enabled:$UNIT"; fi
}

snap() {
    full="$1"
    sec facts
    printf 'boot_id %s\n' "$(cat /proc/sys/kernel/random/boot_id)"
    printf 'uptime_s %s\n' "$(cut -d. -f1 /proc/uptime)"
    printf 'uname %s\n' "$(uname -r)"
    printf 'hostname %s\n' "$(hostname)"
    (. /etc/os-release 2>/dev/null; printf 'os_id %s\nos_version %s\n' "${ID:-}" "${VERSION_ID:-}")
    printf 'casaos_v %s\n' "$(val casaos -v)"
    printf 'catalogue_file %s\n' "$(yn test -e /usr/share/casaos/modules/catalogue.json)"
    printf 'osflag %s\n' "$(yn test -e "$FLAGDIR/busy")"
    printf 'osflag_dir %s\n' "$(yn test -e "$FLAGDIR")"
    printf 'dpkg_lock %s\n' "$(val python3 "$OSR/lockprobe.py" /var/lib/dpkg/lock-frontend)"
    printf 'dpkg_audit %s\n' "$(num dpkg --audit)"
    printf 'tmp_exists %s\n' "$(yn test -d "$TMPD")"
    printf 'tmp_entries %s\n' "$(num ls -A "$TMPD")"
    printf 'tmp_mount %s\n' "$(yn mountpoint -q "$TMPD")"
    printf 'unit_known %s\n' "$(num systemctl list-unit-files "$UNIT" --no-legend)"
    printf 'unit_active %s\n' "$(val systemctl is-active "$UNIT")"
    printf 'unit_enabled %s\n' "$(val systemctl is-enabled "$UNIT")"
    printf 'unit_pid %s\n' "$(val systemctl show -p MainPID --value "$UNIT")"
    printf 'unit_since %s\n' "$(val systemctl show -p ActiveEnterTimestampMonotonic --value "$UNIT")"
    printf 'job_unit %s\n' "$(val systemctl is-active "$JOBUNIT")"
    printf 'conf %s\n' "$(yn test -f "$CONF")"
    printf 'vm_dir %s\n' "$(yn test -d /DATA/VM)"
    printf 'witness %s\n' "$(yn test -e /DATA/VM/.mp-witness)"
    printf 'manager_env %s\n' "$(systemctl show-environment 2>/dev/null | grep -c '^CASAOS_MODULE_')"
    pid="$(systemctl show -p MainPID --value casaos.service 2>/dev/null)"
    if [ -n "$pid" ] && [ "$pid" != 0 ] && [ -r "/proc/$pid/environ" ]; then
        printf 'core_env %s\n' "$(tr '\0' '\n' <"/proc/$pid/environ" | grep -c '^CASAOS_MODULE_')"
    else
        printf 'core_env unknown\n'
    fi
    printf 'system_state %s\n' "$(val systemctl is-system-running)"
    sec pkgs
    for p in $(cat "$KIT/packages.list"); do
        a="$(dpkg-query -W -f='${db:Status-Abbrev}' "$p" 2>/dev/null | tr -d ' ')"
        printf 'pkg %s %s\n' "$p" "${a:-none}"
    done
    # the package the update adds is asked about like the module's own
    for p in $(cat "$KIT/upd-packages.list" 2>/dev/null); do
        a="$(dpkg-query -W -f='${db:Status-Abbrev}' "$p" 2>/dev/null | tr -d ' ')"
        printf 'pkg %s %s\n' "$p" "${a:-none}"
    done
    # shellcheck disable=SC2046
    apt-mark showauto $(cat "$KIT/packages.list") 2>/dev/null | sed 's/^/auto /'
    sec files
    for p in $(cat "$KIT/footprint.list") "$CONF"; do
        if [ -L "$p" ]; then
            printf '%s\tl\t-\t-\t%s\n' "$p" "$(readlink "$p")"
        elif [ -f "$p" ]; then
            printf '%s\tf\t%s\t%s\t%s\n' "$p" "$(stat -c %a "$p")" "$(stat -c %u:%g "$p")" "$(sha256sum <"$p" | cut -d' ' -f1)"
        fi
    done
    sec lists_files
    cat "$STATE/$MOD.files" 2>/dev/null
    sec lists_dirs
    cat "$STATE/$MOD.dirs" 2>/dev/null
    sec lists_version
    cat "$STATE/$MOD.version" 2>/dev/null
    sec tmp
    ls -A "$TMPD" 2>/dev/null
    sec state
    for d in /etc/casaos/modules /var/lib/casaos/modules; do
        if [ -d "$d" ]; then find "$d" -xdev -printf '%p\t%y\t%T@\t%s\n' 2>/dev/null | sort; else printf '%s\tmissing\n' "$d"; fi
    done
    for f in /var/log/casaos/module-*; do
        if [ -e "$f" ]; then printf '%s\tf\t%s\t%s\n' "$f" "$(stat -c %Y "$f")" "$(stat -c %s "$f")"; fi
    done
    sec aptstate
    for f in /var/lib/apt/extended_states /var/log/apt/history.log /var/lib/dpkg/status /var/log/apt/eipp.log.xz; do
        if [ -e "$f" ]; then printf '%s\t%s\t%s\t%s\n' "$f" "$(stat -c %s "$f")" "$(stat -c %Y "$f")" "$(sha256sum <"$f" | cut -d' ' -f1)"; else printf '%s\tmissing\n' "$f"; fi
    done
    sec reserved
    for f in /usr/bin/systemctl /usr/bin/apt-get /usr/bin/dpkg /usr/bin/casaos /usr/bin/env; do
        printf '%s\t%s\n' "$f" "$(sha256sum <"$f" 2>/dev/null | cut -d' ' -f1)"
    done
    sec units
    for u in $CORE_UNITS; do
        printf 'unit %s %s %s %s %s\n' "$u" "$(systemctl show -p ActiveState --value "$u" 2>/dev/null)" "$(systemctl show -p SubState --value "$u" 2>/dev/null)" \
            "$(systemctl show -p InvocationID --value "$u" 2>/dev/null)" "$(systemctl show -p NRestarts --value "$u" 2>/dev/null)"
    done
    systemctl list-units --all --plain --no-legend 'casaos-module-*' 2>/dev/null | sed 's/^/job /'
    systemctl list-units --all --plain --no-legend 'casaos*' 'rclone*' 2>/dev/null | awk '$1 !~ /^casaos-module-/ { print "list " $1 " " $3 " " $4 }' | sort
    sec etccasaos
    find /etc/casaos -type f -exec sha256sum {} + 2>/dev/null | awk '{h = $1; $1 = ""; sub(/^ /, ""); printf "%s\t%s\n", $0, h}' | sort
    sec names
    find /etc /usr/bin /usr/lib/systemd /var/lib/casaos /var/lib/dpkg /var/lib/apt /var/log/casaos /var/log/apt /var/cache/apt /usr/share/casaos /run/systemd/transient \
        /run/systemd/system /DATA /var/tmp -xdev 2>/dev/null | grep -v '/session-' | grep -v '^/var/lib/apt/lists/partial' | sort
    if [ "$full" = full ]; then
        sec dpkg
        dpkg-query -W -f='${Package}\t${Version}\t${db:Status-Abbrev}\n' 2>/dev/null | sort
        sec etc
        find /etc -xdev -type f -print0 2>/dev/null | xargs -0 sha256sum 2>/dev/null | awk '{h = $1; $1 = ""; sub(/^ /, ""); printf "%s\t%s\n", $0, h}' | sort
        find /etc -xdev -type l -printf '%p\tlink:%l\n' 2>/dev/null | sort
    fi
    sec end
}

conf_write() {
    cp "$KIT/virt-management.conf" "$CONF" && chmod 644 "$CONF" && echo "ok $(sha256sum <"$CONF" | cut -d' ' -f1)"
}

conf_remove() { rm -f "$CONF"; yn test -e "$CONF"; }

# purge_added: the packages that the new version adds are taken off (an update or a remove never does it), so that the next update has apt work to do; it says how apt ended and what is
# still installed after it
purge_added() {
    # shellcheck disable=SC2046
    DEBIAN_FRONTEND=noninteractive apt-get -y -q purge $(cat "$KIT/upd-packages.list") >"$MP/pkg.out" 2>&1
    echo "rc=$?"
    left=""
    for p in $(cat "$KIT/upd-packages.list"); do
        case "$(dpkg-query -W -f='${db:Status-Abbrev}' "$p" 2>/dev/null | tr -d ' ')" in ii | hi) left="$left $p" ;; esac
    done
    echo "still_installed=${left# }"
}

# the sample of the module's configuration that both versions carry, edited by the administrator (a file of the new version that is there and is not the module's own refuses an update);
# the undo puts the bytes back
SAMPLE=/etc/casaos/virt-management.conf.sample
sample_edit_on() {
    cp -p "$SAMPLE" "$MP/sample.orig" && printf '# edited by the administrator\n' >>"$SAMPLE" && echo "edited $(sha256sum <"$SAMPLE" | cut -d' ' -f1)"
}
sample_edit_off() {
    if [ -e "$MP/sample.orig" ]; then cat "$MP/sample.orig" >"$SAMPLE" && rm -f "$MP/sample.orig"; fi
    echo "off sha=$(sha256sum <"$SAMPLE" 2>/dev/null | cut -d' ' -f1)"
}

# a drop-in of the module's unit, the proof's own: failstart makes the start fail while the file that only the new version has is there (the old version starts), slowstop and slowstart make
# the steps stop and start of an update last long enough for a kill to land in them
DROPDIR="/etc/systemd/system/$UNIT.d"
dropin_on() {
    mkdir -p "$DROPDIR" || return 1
    case "$1" in
    failstart) printf '[Service]\nRestart=no\nExecStartPre=/bin/sh -c "test ! -e /etc/casaos/mp-next.conf.sample"\n' >"$DROPDIR/mp.conf" ;;
    slowstop) printf '[Service]\nExecStop=/bin/sleep 6\n' >"$DROPDIR/mp.conf" ;;
    slowstart) printf '[Service]\nExecStartPre=/bin/sleep 6\n' >"$DROPDIR/mp.conf" ;;
    *) echo "unknown drop-in: $1" >&2; return 2 ;;
    esac
    systemctl daemon-reload && echo "on $1"
}
dropin_off() {
    rm -f "$DROPDIR/mp.conf"
    rmdir "$DROPDIR" 2>/dev/null
    systemctl daemon-reload
    echo "off file=$(yn test -e "$DROPDIR/mp.conf") dir=$(yn test -e "$DROPDIR")"
}

witness_on() { mkdir -p /DATA/VM && echo witness >/DATA/VM/.mp-witness && echo ok; }
witness_off() { rm -f /DATA/VM/.mp-witness; echo off; }

lock_start() {
    nohup python3 "$OSR/lockhold.py" /var/lib/dpkg/lock-frontend 900 >"$MP/lock.out" 2>&1 </dev/null &
    echo $! >"$MP/lock.pid"
    i=0
    while [ "$i" -lt 20 ]; do
        if grep -q locked "$MP/lock.out" 2>/dev/null; then echo held; return 0; fi
        sleep 0.5
        i=$((i + 1))
    done
    echo "the lock could not be taken: $(cat "$MP/lock.out" 2>/dev/null)"
    return 1
}

lock_stop() {
    if [ -s "$MP/lock.pid" ]; then kill "$(cat "$MP/lock.pid")" 2>/dev/null; : >"$MP/lock.pid"; fi
    i=0
    while [ "$i" -lt 20 ]; do
        if [ "$(python3 "$OSR/lockprobe.py" /var/lib/dpkg/lock-frontend 2>/dev/null)" = free ]; then echo free; return 0; fi
        sleep 0.5
        i=$((i + 1))
    done
    echo "the lock is still held"
    return 1
}

lock_state() { python3 "$OSR/lockprobe.py" /var/lib/dpkg/lock-frontend; }

osflag_on() {
    if [ ! -e "$FLAGDIR" ]; then mkdir -p "$FLAGDIR" && : >"$MP/osflag.made"; fi
    : >"$FLAGDIR/busy" && echo on
}

osflag_off() {
    rm -f "$FLAGDIR/busy"
    if [ -e "$MP/osflag.made" ]; then rmdir "$FLAGDIR" 2>/dev/null; rm -f "$MP/osflag.made"; fi
    echo "off flag=$(yn test -e "$FLAGDIR/busy") dir=$(yn test -e "$FLAGDIR")"
}

unit_dirt_on() {
    printf '[Unit]\nDescription=a unit of the same name that the module does not own\n[Service]\nExecStart=/bin/true\n' >"/etc/systemd/system/$UNIT" && systemctl daemon-reload && echo on
}

unit_dirt_off() {
    rm -f "/etc/systemd/system/$UNIT"
    systemctl daemon-reload
    echo "off file=$(yn test -e "/etc/systemd/system/$UNIT")"
}

tmpfs_on() {
    mkdir -p "$TMPD" && mount -t tmpfs -o size=1m,mode=0755 tmpfs "$TMPD" && echo "mounted $(yn mountpoint -q "$TMPD")"
}

tmpfs_off() {
    umount "$TMPD"
    echo "unmounted $(yn mountpoint -q "$TMPD")"
}

symlink_on() {
    mkdir -p "$TMPD"
    echo canary >"$MP/canary" && chmod 600 "$MP/canary"
    ln -sfn "$MP/canary" "$TMPD/archive.tar.gz" && echo "planted $(readlink "$TMPD/archive.tar.gz")"
}

symlink_off() {
    rm -f "$TMPD/archive.tar.gz"
    echo "off link=$(yn test -L "$TMPD/archive.tar.gz")"
}

canary_state() {
    if [ -e "$MP/canary" ]; then printf 'canary %s %s %s\n' "$(sha256sum <"$MP/canary" | cut -d' ' -f1)" "$(stat -c %a "$MP/canary")" "$(stat -c %Y "$MP/canary")"; else echo "canary missing"; fi
}

# pkg_unseat <packages>: apt-get remove takes what depends on them along (qemu-utils: cloud-image-utils, which the guest image carries): the proof's own doing, so it says what went (will_remove)
# and keeps the files those packages owned (unseat.owned), for the comparison of the end
pkg_unseat() {
    gone="$(apt-get -s remove "$@" 2>/dev/null | awk '$1 == "Remv" { printf "%s ", $2 }')"
    for p in $gone; do dpkg-query -L "$p" 2>/dev/null; done >>"$MP/unseat.owned"
    DEBIAN_FRONTEND=noninteractive apt-get remove -y -q "$@" >"$MP/pkg.out" 2>&1
    echo "rc=$?"
    tail -n 3 "$MP/pkg.out"
    echo "will_remove $gone"
}
unseat_owned() { sort -u "$MP/unseat.owned" 2>/dev/null; echo done; }
pkg_unseat_all() { pkg_unseat $(cat "$KIT/packages.list"); }
pkg_reseat() { DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends "$@" >"$MP/pkg.out" 2>&1; echo "rc=$?"; tail -n 3 "$MP/pkg.out"; }
# dpkg takes a hold on a package that is not installed only when it knows the package: its available file is told of it first (apt-mark hold is the second try)
hold_on() {
    f="$(mktemp)"
    apt-cache show --no-all-versions "$1" >"$f" 2>/dev/null
    dpkg --merge-avail "$f" >/dev/null 2>&1
    rm -f "$f"
    echo "$1 hold" | dpkg --set-selections
    case "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)" in hold\ ok*) ;; *) apt-mark hold "$1" >/dev/null 2>&1 ;; esac
    echo "held $(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)"
}
hold_off() { echo "$1 install" | dpkg --set-selections && echo "unheld $(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)"; }
auto_on() { apt-mark auto "$1" >/dev/null 2>&1; echo "auto $(apt-mark showauto "$1" 2>/dev/null)"; }
auto_off() { apt-mark manual "$1" >/dev/null 2>&1; echo "manual auto=$(apt-mark showauto "$1" 2>/dev/null)"; }

# aptsim <packages>: what apt says of installing them, asked with every write of the simulation turned off (the box's list of automatically installed packages, the log of the planner,
# the locks, the cache), as lines: new <name>, upgrade <name>, held <name>, manual <name>, rc <n>, uris_total <bytes>, need <the line apt says>
aptsim() {
    d="$(mktemp -d /var/tmp/mp-aptsim.XXXXXX)"
    mkdir -p "$d/cache/archives/partial"
    # the list of the automatically installed packages is the box's own, copied: apt says `set to manually installed` for those and for no other
    cp /var/lib/apt/extended_states "$d/extended_states" 2>/dev/null || : >"$d/extended_states"
    opts="-o Dir::Cache=$d/cache -o Debug::NoLocking=true -o Dir::Log::Planner= -o Dir::State::extended_states=$d/extended_states"
    # shellcheck disable=SC2086
    apt-get $opts -s install --no-install-recommends "$@" >"$d/sim" 2>&1
    echo "rc $?"
    awk '$1 == "Inst" { if ($3 ~ /^\[/) print "upgrade " $2; else print "new " $2 }
         /^[a-z0-9][a-z0-9+.:~-]* set to manually installed\.$/ { print "manual " $1 }
         /^The following held packages will be changed:/ { h = 1; next }
         h && /^  / { for (i = 1; i <= NF; i++) print "held " $i; next }
         { h = 0 }' "$d/sim"
    # shellcheck disable=SC2086
    apt-get $opts --print-uris install --no-install-recommends "$@" 2>/dev/null | awk '/^'"'"'/ { s += $3 } END { print "uris_total " s + 0 }'
    grep -E '^(Need to get|After this operation)' "$d/sim" | sed 's/^/need /'
    rm -rf "$d"
}

# the watchers: one runs in the background on the guest, sees the step of the install begin in the log of the module and then kills the assistant (kill) or stops the unit (stop), and says
# where the log was and what became of it
begin_line() { grep -E "^CASAOS_MODULE_STEP [0-9a-f]{32} $MOD $1 begin " "$LOG" 2>/dev/null | tail -n 1; }
last_marker() { grep -E '^CASAOS_MODULE_(STEP|RESULT) ' "$LOG" 2>/dev/null | tail -n 1; }

# the marker of the step is looked for among the lines of the log, not at its end: what a tool prints comes right behind the marker of its step. It is a marker of the core's job (a nonce
# of 32 hexadecimal digits): the log of a run by hand, which an earlier case left, has the nonce -
watch_wait_step() {
    step="$1"
    i=0
    while [ "$i" -lt 60000 ]; do
        if [ -n "$(begin_line "$step")" ]; then return 0; fi
        sleep 0.02
        i=$((i + 1))
    done
    return 1
}

watch_run() {
    mode="$1"; step="$2"; arg="$3"
    if ! watch_wait_step "$step"; then echo "never saw the step $step begin"; return 0; fi
    seen="$(begin_line "$step")"
    case "$mode" in
    kill)
        pid="$(pgrep -o -f '^/usr/bin/casaos module (install|remove|update) ')"
        if [ -z "$pid" ]; then echo "no process of the assistant to signal"; return 0; fi
        kill -s "$arg" "$pid" 2>/dev/null
        printf 'signalled pid=%s signal=%s rc=%s\n' "$pid" "$arg" "$?"
        ;;
    stop)
        if [ "$arg" = dpkg ]; then
            i=0
            while [ "$i" -lt 9000 ] && ! pgrep -x dpkg >/dev/null 2>&1; do sleep 0.02; i=$((i + 1)); done
            printf 'dpkg_seen=%s\n' "$(if pgrep -x dpkg >/dev/null 2>&1; then echo yes; else echo no; fi)"
        fi
        a="$(date +%s)"
        systemctl stop "$JOBUNIT"
        printf 'stopped rc=%s seconds=%s\n' "$?" "$(($(date +%s) - a))"
        ;;
    esac
    printf 'seen=%s\n' "$seen"
    sleep 1
    printf 'after=%s\n' "$(last_marker)"
    printf 'dpkg_audit=%s\n' "$(num dpkg --audit)"
}

watch_start() {
    : >"$MP/watch.out"
    rm -f "$MP/watch.done"
    ( nohup sh "$MP/guest.sh" watch_run "$1" "$2" "$3" >"$MP/watch.out" 2>&1 </dev/null; : >"$MP/watch.done" ) >/dev/null 2>&1 &
    echo started
}

watch_state() { if [ -e "$MP/watch.done" ]; then echo done; else echo running; fi; }
watch_result() { cat "$MP/watch.out" 2>/dev/null; }

# wait_clean <seconds>: no assistant, no apt and no dpkg at work, the lock free and the unit of the job gone
# repair_dpkg: finish what an interrupted dpkg left (the product must do it by itself; the proof only needs the next case to start from a sound box)
repair_dpkg() {
    if [ "$(dpkg --audit 2>/dev/null | wc -l)" -gt 0 ]; then
        DEBIAN_FRONTEND=noninteractive dpkg --configure -a >/dev/null 2>&1
        DEBIAN_FRONTEND=noninteractive apt-get -f install -y >/dev/null 2>&1
        echo "repaired"
    else
        echo "sound"
    fi
}

wait_clean() {
    i=0
    max="${1:-600}"
    while :; do
        what=""
        if pgrep -f '^/usr/bin/casaos module' >/dev/null 2>&1; then what="$what assistant"; fi
        if pgrep -x apt-get >/dev/null 2>&1 || pgrep -x apt >/dev/null 2>&1; then what="$what apt"; fi
        if pgrep -x dpkg >/dev/null 2>&1; then what="$what dpkg"; fi
        case "$(systemctl is-active "$JOBUNIT" 2>/dev/null)" in active | activating | deactivating) what="$what unit" ;; esac
        if [ "$(python3 "$OSR/lockprobe.py" /var/lib/dpkg/lock-frontend 2>/dev/null)" = held ]; then what="$what lock"; fi
        if [ -z "$what" ]; then echo "clean after $i s"; return 0; fi
        if [ "$i" -ge "$max" ]; then echo "not clean after $i s:$what"; return 1; fi
        sleep 1
        i=$((i + 1))
    done
}

# dummy_start <n>: the unit number n (one call, one unit: a second call for 1 and 2 would meet the first and fail)
dummy_start() {
    systemd-run --quiet --no-block --collect "--unit=casaos-module-mpdummy$1.service" --property=Type=exec /bin/sleep 900 || return 1
    sleep 1
    systemctl list-units --all --plain --no-legend 'casaos-module-*'
}

dummy_stop() {
    for u in casaos-module-mpdummy1.service casaos-module-mpdummy2.service; do systemctl stop "$u" 2>/dev/null; done
    sleep 1
    echo "left=$(systemctl list-units --all --plain --no-legend 'casaos-module-*' 2>/dev/null | wc -l | tr -d ' ')"
}

glob_show() {
    echo "== values"
    systemctl show 'casaos-module-*.service' --property=ActiveState --value 2>&1
    echo "rc=$?"
    echo "== properties"
    systemctl show 'casaos-module-*.service' --property=Id --property=ActiveState --property=LoadState 2>&1
    echo "rc=$?"
}

unit_props() {
    echo "== show"
    systemctl show "$JOBUNIT" -p KillMode -p TimeoutStopUSec -p TimeoutStopSec -p Type -p LoadState -p ActiveState 2>&1
    echo "rc=$?"
    echo "== loadstate"
    systemctl show "$JOBUNIT" --property=LoadState --value 2>&1
    echo "== cat"
    systemctl cat "$JOBUNIT" 2>&1 | head -n 40
}

api_env_on() {
    variant_files "${1:-good}" || return 2
    systemctl set-environment CASAOS_MODULE_TEST=1 "CASAOS_MODULE_CATALOGUE=$KIT/$VCAT" "CASAOS_MODULE_ARCHIVE_FILE=$KIT/$VARC" || return 1
    systemctl restart casaos.service || return 1
    echo restarted
}

api_env_off() {
    systemctl unset-environment CASAOS_MODULE_TEST CASAOS_MODULE_CATALOGUE CASAOS_MODULE_ARCHIVE_FILE || return 1
    systemctl restart casaos.service || return 1
    echo restarted
}

api_env_state() {
    printf 'manager %s\n' "$(systemctl show-environment 2>/dev/null | grep -c '^CASAOS_MODULE_')"
    pid="$(systemctl show -p MainPID --value casaos.service 2>/dev/null)"
    if [ -n "$pid" ] && [ "$pid" != 0 ] && [ -r "/proc/$pid/environ" ]; then
        printf 'core %s\n' "$(tr '\0' '\n' <"/proc/$pid/environ" | grep -c '^CASAOS_MODULE_')"
    else
        printf 'core unknown\n'
    fi
}

pkg_files() { for p in "$@"; do dpkg-query -L "$p" 2>/dev/null; done | sort -u; }

escape_state() {
    find /tmp /var /usr /etc /root /home /opt /DATA -xdev -name 'mp-escape*' 2>/dev/null | head -n 5
    echo "done"
}

modlog() { cat "$LOG" 2>/dev/null; }
journal() { journalctl -u "$JOBUNIT" --no-pager -o cat 2>&1 | tail -n 40; }

diag() {
    systemctl --failed --no-pager 2>&1
    echo "== core"
    journalctl -u casaos.service -u casaos-gateway.service -u "$UNIT" --no-pager -o short-iso 2>&1 | tail -n 100
    echo "== job"
    journalctl -u "$JOBUNIT" --no-pager -o short-iso 2>&1 | tail -n 60
    echo "== log of the module"
    tail -n 60 "$LOG" 2>&1
    echo "== dpkg --audit"
    dpkg --audit 2>&1 | head -n 30
    echo "== df"
    df -h 2>&1
}

verb="${1:-}"
[ "$#" -eq 0 ] || shift
case "${verb}" in
boot_id) cat /proc/sys/kernel/random/boot_id ;;
hostname) hostname ;;
casaos_v) casaos -v ;;
mod) mod "$@" ;;
footprint) footprint ;;
snap) snap "${1:-}" ;;
conf_write) conf_write ;;
conf_remove) conf_remove ;;
purge_added) purge_added ;;
sample_edit_on) sample_edit_on ;;
sample_edit_off) sample_edit_off ;;
dropin_on) dropin_on "$@" ;;
dropin_off) dropin_off ;;
witness_on) witness_on ;;
witness_off) witness_off ;;
lock_start) lock_start ;;
lock_stop) lock_stop ;;
lock_state) lock_state ;;
osflag_on) osflag_on ;;
osflag_off) osflag_off ;;
unit_dirt_on) unit_dirt_on ;;
unit_dirt_off) unit_dirt_off ;;
tmpfs_on) tmpfs_on ;;
tmpfs_off) tmpfs_off ;;
symlink_on) symlink_on ;;
symlink_off) symlink_off ;;
canary_state) canary_state ;;
pkg_unseat) pkg_unseat "$@" ;;
pkg_unseat_all) pkg_unseat_all ;;
unseat_owned) unseat_owned ;;
pkg_reseat) pkg_reseat "$@" ;;
hold_on) hold_on "$@" ;;
hold_off) hold_off "$@" ;;
auto_on) auto_on "$@" ;;
auto_off) auto_off "$@" ;;
aptsim) aptsim "$@" ;;
watch_start) watch_start "$@" ;;
watch_run) watch_run "$@" ;;
watch_state) watch_state ;;
watch_result) watch_result ;;
wait_clean) wait_clean "$@" ;;
repair_dpkg) repair_dpkg ;;
dummy_start) dummy_start "$@" ;;
dummy_stop) dummy_stop ;;
glob_show) glob_show ;;
unit_props) unit_props ;;
job_unit) systemctl is-active "$JOBUNIT" 2>/dev/null || true ;;
api_env_on) api_env_on "$@" ;;
api_env_off) api_env_off ;;
api_env_state) api_env_state ;;
pkg_files) pkg_files "$@" ;;
escape_state) escape_state ;;
modlog) modlog ;;
journal) journal ;;
reboot) nohup sh -c 'sleep 2; systemctl reboot' >/dev/null 2>&1 </dev/null & echo scheduled ;;
diag) diag ;;
*) echo "unknown verb: ${verb}" >&2; exit 2 ;;
esac
MP_GUEST
    chmod 755 "$1/guest.sh"
}

# ---- the guest: its preparation ------------------------------------------------------------------------------------------------------

# mp_install_casaos: the installer of the release under test, the way install-check.yml's vm leg runs it (vm-smoke.sh): as root, without telemetry
mp_install_casaos() {
    local port
    # shellcheck disable=SC2086
    bash install.sh --no-telemetry ${INSTALLER_ARGS:-} 2>&1 | tee "${OUT}/install.log" >/dev/null
    test "${PIPESTATUS[0]}" -eq 0
    port="$(sed -nE 's/^[[:space:]]*HttpPort[[:space:]]*=[[:space:]]*([0-9]+).*/\1/p' /etc/casaos/gateway.ini | sed -n 1p)"
    CASA_PORT="${port:-80}"
    log "ReCasaOS is installed, its gateway is on port ${CASA_PORT}"
}

# guest_prepare: a fresh machine gets the release under test, the tools of the proof and the kit. The helpers of os-readiness-proof.sh are sourced (the waits for the services, the
# facts of the box, the dpkg lock holder), not rewritten.
guest_prepare() {
    [[ "${MP_DISPOSABLE:-}" == 1 ]] ||
        { echo "This installs ReCasaOS, installs and removes a module on it, kills the installer, holds dpkg's lock and restarts the machine: run it on a disposable machine (MP_DISPOSABLE=1)." >&2; exit 2; }
    [ "$(id -u)" -eq 0 ] || { echo "Run it as root." >&2; exit 2; }
    if [ -e /etc/casaos ] || [ -e /var/lib/casaos ]; then
        echo "This machine already runs ReCasaOS: not a fresh one, not touching it." >&2
        exit 2
    fi
    # shellcheck source=os-readiness-proof.sh
    . "${READINESS_LIB:-${MP_HERE}/os-readiness-proof.sh}"
    # shellcheck source=/dev/null
    . "${OS_RELEASE}"
    export LC_ALL=C
    OUT="${OUT:-${PWD}/mp-guest-out}"
    mkdir -p "${OUT}" "${AUTH_DIR}" "${LIBDIR}"
    chmod 700 "${AUTH_DIR}"
    : >"${OUT}/timeline.tsv"
    echo "${PRETTY_NAME}" >"${OUT}/os"
    PENDING_UNDO=""
    guard_run
    [ -n "${EPOCHREALTIME:-}" ] || abort "this bash has no EPOCHREALTIME (bash 5 is needed)"
    export DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none NEEDRESTART_SUSPEND=1
    APT=(env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3)
    marker prepare_start
    cloud-init status --wait >/dev/null 2>&1 || true
    # apt's own timers would take dpkg's lock in the middle of a case; they are off for the whole run, and so is what they run
    systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service >/dev/null 2>&1 || true
    printf 'APT::Periodic::Update-Package-Lists "0";\nAPT::Periodic::Download-Upgradeable-Packages "0";\nAPT::Periodic::AutocleanInterval "0";\nAPT::Periodic::Unattended-Upgrade "0";\n' >"${APT_DIR}/apt.conf.d/99mp-quiet"
    retry "${APT[@]}" update -qq
    "${APT[@]}" install -y -q curl jq ca-certificates python3 procps iproute2 util-linux >/dev/null
    write_lock_tools "${LIBDIR}"
    write_guest_tools "${GUEST_TOOLS_DIR}"
    # the kit the runner pushed is put where the assistant is run from, owned by root and written by nobody else
    [ -s "${KIT_SRC:-/home/casa/mp-kit}/kit.json" ] || abort "the kit was not pushed to the guest"
    rm -rf "${GUEST_KIT}"
    mkdir -p "${GUEST_KIT}"
    cp -r "${KIT_SRC:-/home/casa/mp-kit}/." "${GUEST_KIT}/"
    chown -R root:root "${GUEST_KIT}"
    chmod 755 "${GUEST_KIT}"
    find "${GUEST_KIT}" -type f -exec chmod 644 {} +
    mp_install_casaos
    start_services_wait
    cp /var/lib/casaos/fork-release "${OUT}/fork-release" 2>/dev/null || true
    if [ -s "${COMPONENTS_LOCK:-/home/casa/components.lock}" ]; then
        grep -E '^(CASAOS_RELEASE_TAG|RCLONE_TAG|CASAOS_UI_TAG|CASAOS_TAG)=' "${COMPONENTS_LOCK:-/home/casa/components.lock}" >"${OUT}/pins" || true
    fi
    marker prepare_done
    date -u +%FT%TZ >"${OUT}/prepared"
    log "prepared"
}

start_services_wait() {
    wait_services 240 || abort "the gateway, the core and AppManagement do not all answer after the installation"
    timeout 120 systemctl is-system-running --wait >/dev/null 2>&1 || true
    quiet_packagekit || true
    box_facts
    # AppManagement's check of the images lands in /var/lib/casaos three minutes after its start: it is over before the first snapshot of the cases
    wait_image_check 240 || not_proven "AppManagement did not write ${IMAGE_UPDATES} within 240 s: its own check of the images (3 minutes after its start) could fall in a window of the proof"
    [ "${CASA_PORT}" = 80 ] || abort "the gateway is on port ${CASA_PORT}: the runner forwards the guest's port 80 only"
    casaos -v >"${OUT}/casaos-v" 2>&1 || true
}

# ---- the runner: the clock, the guest, the core --------------------------------------------------------------------------------------

# the clock the evidence uses: the shell's own, plus the time a fast test skipped (MP_FAST: a test of this script has no guest to wait for)
VOFF=0
now() {
    local e="${EPOCHREALTIME/[.,]/}"
    e=$((e + VOFF))
    printf '%d.%06d' $((e / 1000000)) $((e % 1000000))
}
now_s() {
    local e="${EPOCHREALTIME/[.,]/}"
    e=$((e + VOFF))
    NOW_S=$((e / 1000000))
}
# nap <seconds> (whole): a sleep that a test can skip, telling its stand-in of the guest how much time went by
nap() {
    if [ -n "${MP_FAST:-}" ]; then
        VOFF=$((VOFF + $1 * 1000000))
        if [ -n "${MP_TICK:-}" ]; then "${MP_TICK}" "$1" >/dev/null 2>&1 || true; fi
    else
        sleep "$1"
    fi
}

log() { printf '[proof %(%H:%M:%S)T] %s\n' -1 "$*" | tee -a "${OUT}/steps.log" >&2; }
marker() { printf '#\t%s\t%s\n' "$(now)" "$1" >>"${OUT}/timeline.tsv"; }
# the run cannot go on: it says why and ends (status 0, so that the report is made); the report turns that into INVALID RUN
abort() { echo "$*" >>"${OUT}/aborted"; log "ABORTED: $*"; exit 0; }
# a step of the harness (not of the assistant) that failed, or whose undo failed: the report makes the run invalid, never a failure of the assistant
harness_invalid() { printf '%s\t%s\n' "$1" "$2" >>"${OUT}/invalid.txt"; log "INVALID: $1: $2"; }

py() { python3 "${WORK}/mp.py" "$@"; }

SSH_CONFIG="${SSH_CONFIG:-ssh_config}"
SSH_HOST="${SSH_HOST:-vm}"
SSH_LIMIT="${SSH_LIMIT:-300}"
# gssh <command...>: a command on the guest, as the login user (a hung guest costs SSH_LIMIT seconds, not the job)
gssh() { timeout -k 5 "${SSH_LIMIT}" ssh -n -F "${SSH_CONFIG}" -o ConnectTimeout=8 -o BatchMode=yes "${SSH_HOST}" "$@"; }
# g <verb> [words]: a verb of the guest's tool
g() { gssh "sudo sh ${GUEST_TOOLS_DIR}/guest.sh $*"; }
boot_id() { g boot_id 2>/dev/null | tr -d '\r\n'; }

# gx <name> <verb> [words]: a verb of the guest, with what it said recorded: <name>.out (standard output), .err, .rc (its exit status; 255 is ssh's own), .t0 and .t1. A verb that only
# reads (GX_RETRY=1) is asked again, twice, when ssh itself failed.
gx() {
    local name="$1" rc tries=0
    shift
    while :; do
        tries=$((tries + 1))
        now >"${OUT}/${name}.t0"
        g "$@" >"${OUT}/${name}.out" 2>"${OUT}/${name}.err"
        rc=$?
        now >"${OUT}/${name}.t1"
        if [ "${rc}" = 255 ] && [ "${GX_RETRY:-1}" = 1 ] && [ "${tries}" -lt 3 ]; then
            nap 5
            continue
        fi
        break
    done
    printf '%s\n' "${rc}" >"${OUT}/${name}.rc"
    GX_RC=${rc}
    return 0
}

# modrun <name> <variant> <user> <arguments of casaos module>: the assistant by hand. install and remove are not asked twice when ssh fails: they changed the box.
modrun() {
    local name="$1" variant="$2" user="$3" limit=300 retry=1
    shift 3
    case "$1" in install | remove | update) limit="${MOD_LIMIT:-2400}"; retry=0 ;; esac
    SSH_LIMIT="${limit}" GX_RETRY="${retry}" gx "${name}" mod "${variant}" "${user}" "$@"
}

# qmon is not needed: no snapshot of the whole machine is taken, the machine is undone with the assistant's own remove

GW_PORT="${GW_PORT:-8080}"
GW="http://127.0.0.1:${GW_PORT}"
auth_header() { cat "${WORK}/auth" 2>/dev/null || echo 'Authorization: none'; }

# login: the access token of the box's own user, kept for the header in $WORK (never in $OUT); status 1 when there is none
login() {
    local pw tok
    pw="$(cat "${WORK}/pw" 2>/dev/null)" || return 1
    curl -sS --max-time 30 -X POST "${GW}/v1/users/login" -H 'Content-Type: application/json' -d "$(printf '{"username":"%s","password":"%s"}' "${PROOF_USER}" "${pw}")" \
        -o "${WORK}/login.json" 2>/dev/null || return 1
    tok="$(py get "${WORK}/login.json" data.token.access_token)"
    [ -n "${tok}" ] && [ "${tok}" != - ] || return 1
    (umask 077; printf 'Authorization: %s\n' "${tok}" >"${WORK}/auth")
}

# call <name> <GET|POST|PUT> <path> [body] [seconds]: one request through the gateway with the token (none with CALL_NOAUTH=1). Written into $OUT: <name>.req (what was asked), .json (the
# body), .hdr (the headers), .code (the HTTP status, 000 when there was none), .err (what curl said), .t0 and .t1 (the time before and after, in seconds since the epoch), and .curl last
# (how curl ended: 0, or why it got no answer). A 401 (the token ran out, or the core was restarted) logs in again and asks once more. CALL_CODE has the status.
call() {
    local name="$1" method="$2" path="$3" body="${4:-}" max="${5:-45}" tries=0 hdr
    local opts
    while :; do
        tries=$((tries + 1))
        hdr="$(auth_header)"
        rm -f "${OUT}/${name}.json" "${OUT}/${name}.hdr"
        printf '%s %s\n%s\n' "${method}" "${path}" "${body}" >"${OUT}/${name}.req"
        now >"${OUT}/${name}.t0"
        opts=(-sS --max-time "${max}" -X "${method}" -D "${OUT}/${name}.hdr" -o "${OUT}/${name}.json" -w '%{http_code}')
        if [ -z "${CALL_NOAUTH:-}" ]; then opts+=(-H "${hdr}"); fi
        if [ -n "${body}" ]; then
            opts+=(-H 'Content-Type: application/json' --data-binary "${body}")
        elif [ "${method}" != GET ]; then
            opts+=(--data-binary '')
        fi
        CALL_RC=0
        CALL_CODE="$(curl "${opts[@]}" "${GW}${path}" 2>"${OUT}/${name}.err")" || CALL_RC=$?
        now >"${OUT}/${name}.t1"
        CALL_CODE="${CALL_CODE:-000}"
        printf '%s\n' "${CALL_CODE}" >"${OUT}/${name}.code"
        printf '%s\n' "${CALL_RC}" >"${OUT}/${name}.curl"
        if [ "${CALL_CODE}" = 401 ] && [ -z "${CALL_NOAUTH:-}" ] && [ "${tries}" -lt 2 ] && login; then
            echo relogin >>"${OUT}/${name}.notes"
            continue
        fi
        return 0
    done
}

# snap <tag> [full]: the machine as it is now (the guest's `snap` verb), split into snap-<tag>.<section>; three tries. A snapshot that could not be had says so (snap-<tag>.failed).
snap() {
    local tag="$1" full="${2:-}" try
    for try in 1 2 3; do
        if g snap "${full}" >"${OUT}/snap-${tag}.raw" 2>"${OUT}/snap-${tag}.err" && py snapsplit "${OUT}/snap-${tag}.raw" "${OUT}/snap-${tag}"; then
            rm -f "${OUT}/snap-${tag}.raw" "${OUT}/snap-${tag}.failed"
            return 0
        fi
        nap 10
    done
    echo "the guest's snapshot did not come back whole in three tries" >"${OUT}/snap-${tag}.failed"
    return 1
}

# wait_boot <tag> <old boot id> <seconds>: the guest's boot id every two seconds (boot-<tag>.tsv: t0, t1, the id or -) until it answers with another one. Status 1 when it does not in time.
wait_boot() {
    local tag="$1" old="$2" max="$3" end id t0
    : >"${OUT}/boot-${tag}.tsv"
    now_s
    end=$((NOW_S + max))
    while :; do
        t0="$(now)"
        id="$(boot_id)"
        printf '%s\t%s\t%s\n' "${t0}" "$(now)" "${id:--}" >>"${OUT}/boot-${tag}.tsv"
        if [ -n "${id}" ] && [ "${id}" != "${old}" ]; then return 0; fi
        now_s
        if [ "${NOW_S}" -ge "${end}" ]; then echo "${max}" >"${OUT}/boot-${tag}.timeout"; return 1; fi
        nap 2
    done
}

# wait_up <seconds>: the core answers through the gateway (an answer of its version, which needs no token)
wait_up() {
    local end code
    now_s
    end=$((NOW_S + $1))
    while :; do
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${GW}/v1/sys/version/current" 2>/dev/null || true)"
        if [ "${code}" = 200 ]; then return 0; fi
        now_s
        if [ "${NOW_S}" -ge "${end}" ]; then return 1; fi
        nap 3
    done
}

# the rows of the polls of a job: J_* of the latest poll
J_T0=0 J_T1=0 J_HTTP=000 J_CURL=0 J_STATE=- J_STEP=- J_CODE=- J_UNDO=- J_KEPT=- J_INSTALLED=- J_VERSION=- J_ACTIVE=- J_ENABLED=- J_ROUTE=- J_UNIT=-
# poll_job_once <tag>: GET of the status of the module, the unit of the job as the guest says it, one row in polls-<tag>.tsv (t0, t1, HTTP, curl, job state, step, code, undo, kept packages,
# installed, version, unit active, unit enabled, route answers, the job unit). With PROPS_TAG set, the first time the job unit is seen active its properties are recorded (<tag>-props,
# <tag>-glob).
poll_job_once() {
    local tag="$1" unit row t
    call poll-now GET "${LIST_PATH}/${MODULE}/status" "" 20
    unit="$(g job_unit 2>/dev/null | tr -d '\r\n')"
    case "${unit}" in active | inactive | failed | activating | deactivating) ;; *) unit="-" ;; esac
    row="$(py jobrow "${OUT}/poll-now.json" "${CALL_CODE}" "${CALL_RC}" "${unit}" "$(cat "${OUT}/poll-now.t0")" "$(cat "${OUT}/poll-now.t1")")"
    printf '%s\n' "${row}" >>"${OUT}/polls-${tag}.tsv"
    IFS=$'\t' read -r J_T0 J_T1 J_HTTP J_CURL J_STATE J_STEP J_CODE J_UNDO J_KEPT J_INSTALLED J_VERSION J_ACTIVE J_ENABLED J_ROUTE J_UNIT <<<"${row}"
    if [ -n "${PROPS_TAG:-}" ] && [ "${J_UNIT}" = active ]; then
        t="${PROPS_TAG}"
        PROPS_TAG=""
        gx "${t}-props" unit_props
        gx "${t}-glob" glob_show
    fi
}

# wait_job <tag> <seconds> <terminal states>: polls the status every POLL_EVERY seconds until the job is in one of the states: status 0. 1: the time ran out (<tag>.timeout says so).
wait_job() {
    local tag="$1" max="$2" terminal="$3" end
    : >"${OUT}/polls-${tag}.tsv"
    rm -f "${OUT}/${tag}.timeout"
    now_s
    end=$((NOW_S + max))
    while :; do
        poll_job_once "${tag}"
        case " ${terminal} " in *" ${J_STATE} "*) return 0 ;; esac
        now_s
        if [ "${NOW_S}" -ge "${end}" ]; then
            echo "${max}" >"${OUT}/${tag}.timeout"
            return 1
        fi
        nap "${POLL_EVERY:-2}"
    done
}

# wait_watch <tag> <seconds>: the watcher of the guest (a kill or a stop) is done, or the time is out; what it said is recorded in <tag>-watch-result
wait_watch() {
    local tag="$1" max="$2" end s
    now_s
    end=$((NOW_S + max))
    while :; do
        s="$(g watch_state 2>/dev/null | tr -d '\r\n')"
        if [ "${s}" = done ]; then break; fi
        now_s
        if [ "${NOW_S}" -ge "${end}" ]; then break; fi
        nap 2
    done
    gx "${tag}-watch-result" watch_result
}

# ensure_clean <tag>: the module is taken off by hand if anything of it is on the box, and the box is asked again; a module that cannot be taken off makes the run invalid
ensure_clean() {
    local tag="$1"
    # a case that left dpkg half-done must not hide the cases after it: the box is repaired first (after that case was judged, from what it recorded), as a fresh start
    gx "${tag}-repair" repair_dpkg
    gx "${tag}-fp" footprint
    if [ -s "${OUT}/${tag}-fp.out" ]; then
        modrun "${tag}-rm" good root remove "${MODULE}" --yes
        gx "${tag}-fp2" footprint
        if [ -s "${OUT}/${tag}-fp2.out" ] || [ "$(cat "${OUT}/${tag}-rm.rc")" != 0 ]; then
            harness_invalid "${tag}" "the module could not be taken off the box to start from a clean one"
            return 1
        fi
    fi
    return 0
}

# the api phase: the three test variables in the environment of systemd's manager, the core restarted so that its own process has them too, and what was done read back
# api_on [variant] [tag]: the test variables for the catalogue of the variant (the good one by default), recorded as <tag>-on and <tag>-state
API_VARIANT=""
api_on() {
    local v="${1:-}" t="${2:-api}"
    gx "${t}-on" api_env_on ${v:+"${v}"}
    if [ "${GX_RC}" != 0 ]; then
        harness_invalid api "the test variables could not be put in the environment of the manager: $(tr -d '\r' <"${OUT}/${t}-on.err" | tr '\n' ' ' | cut -c1-200)"
        return 1
    fi
    wait_up "${WAIT_CORE:-240}" || { harness_invalid api "the core does not answer after its restart"; return 1; }
    login || { harness_invalid api "the box's user could not log in after the restart of the core"; return 1; }
    gx "${t}-state" api_env_state
    API_VARIANT="${v:-good}"
    return 0
}

api_off() {
    local t="${1:-api}"
    gx "${t}-off" api_env_off
    if [ "${GX_RC}" != 0 ]; then
        harness_invalid api "the test variables could not be taken out of the environment of the manager: $(tr -d '\r' <"${OUT}/${t}-off.err" | tr '\n' ' ' | cut -c1-200)"
        return 1
    fi
    wait_up "${WAIT_CORE:-240}" || { harness_invalid api "the core does not answer after its restart"; return 1; }
    login || true
    gx "${t}-off-state" api_env_state
    API_VARIANT=""
    return 0
}

# ---- the cases -------------------------------------------------------------------------------------------------------------------

CONTROL_SECONDS="${CONTROL_SECONDS:-320}"
WAIT_JOB="${WAIT_JOB:-1800}"
WAIT_KILLED="${WAIT_KILLED:-240}"
WAIT_KILLED_APT="${WAIT_KILLED_APT:-900}"
TERMINAL_STATES="succeeded failed refused interrupted"

# pend <name> <verb and words>: the undo of a piece of dirt that is in place, kept in $WORK so that the end of the run makes it again if the case did not; unpend <name> once it is done
pend() { printf '%s\n' "$2" >"${WORK}/pending-$1"; }
unpend() { rm -f "${WORK}/pending-$1"; }
# undone <name> <file> <pattern>: the undo said what it should; if it did not, the run is invalid and the undo stays pending
undone() {
    if grep -Eq "$3" "${OUT}/$2.out" 2>/dev/null; then
        unpend "$1"
    else
        harness_invalid "$1" "the undo of the dirt did not say it was done: $(tr -d '\r' <"${OUT}/$2.out" 2>/dev/null | tr '\n' ' ' | cut -c1-200) $(tr -d '\r' <"${OUT}/$2.err" 2>/dev/null | tr '\n' ' ' | cut -c1-100)"
    fi
}

M3_PLAN_ID=""
# plan_id_of_m3: the id of the plan of M3 (made again when that case did not run in this run)
plan_id_of_m3() {
    if [ -z "${M3_PLAN_ID}" ]; then
        if [ ! -s "${OUT}/m3-plan-root.out" ]; then modrun m3-plan-root good root plan "${MODULE}"; fi
        M3_PLAN_ID="$(py get "${OUT}/m3-plan-root.out" plan_id)"
    fi
    printf '%s' "${M3_PLAN_ID}"
}

case_m1() {
    marker case_M1_start
    gx m1-version casaos_v
    printf '%s\n' "${MIN_CORE}" >"${OUT}/m1-min"
    py version_ge "$(tr -d '\r\n' <"${OUT}/m1-version.out")" "${MIN_CORE}"
    printf '%s\n' "$?" >"${OUT}/m1-cmp"
    marker case_M1_done
    # an old core given `module` starts as a server: nothing below runs on a core that has no such word
    if [ "$(cat "${OUT}/m1-cmp")" != 0 ]; then abort "M1: the core of this release ($(tr -d '\r\n' <"${OUT}/m1-version.out")) is older than ${MIN_CORE}, the first with \`casaos module\`: nothing more is run"; fi
}

case_m2() {
    marker case_M2_start
    snap m2-ctl-a full || return 1
    log "the control waits ${CONTROL_SECONDS} s: what the box changes by itself falls in it"
    nap "${CONTROL_SECONDS}"
    snap m2-ctl-b full || return 1
    marker m2_window
    snap m2-before || return 1
    modrun m2-list-root none root list
    modrun m2-list-nobody none nobody list
    modrun m2-plan-root none root plan "${MODULE}"
    modrun m2-plan-nobody none nobody plan "${MODULE}"
    modrun m2-status-root none root status "${MODULE}"
    modrun m2-status-all none root status
    snap m2-after || return 1
    marker case_M2_done
}

case_m3() {
    marker case_M3_start
    snap m3-before || return 1
    modrun m3-plan-root good root plan "${MODULE}"
    modrun m3-plan-nobody good nobody plan "${MODULE}"
    modrun m3-list-root good root list
    snap m3-after || return 1
    # the same question put to apt itself, with every write of its simulation turned off
    gx m3-apt aptsim $(tr '\n' ' ' <"${KIT}/packages.list")
    snap m3-after-sim || return 1
    M3_PLAN_ID="$(py get "${OUT}/m3-plan-root.out" plan_id)"
    marker case_M3_done
}

# the answers of the module's service, through the gateway, with the token: asked again for a while, since libvirt answers a moment after the unit does
host_call() {
    local name="$1" end
    now_s
    end=$((NOW_S + ${2:-120}))
    while :; do
        call "${name}" GET "${MODULE_ROUTE}/host" "" 30
        if [ "$(py get "${OUT}/${name}.json" data.libvirt)" = available ]; then return 0; fi
        now_s
        if [ "${NOW_S}" -ge "${end}" ]; then return 1; fi
        nap 3
    done
}

case_m4() {
    local pid
    marker case_M4_start
    pid="$(plan_id_of_m3)"
    printf '%s\n' "${pid}" >"${OUT}/m4-plan-id"
    # libvirt's test driver, in the configuration of the module, before the unit starts
    gx m4-conf conf_write
    snap m4-before || return 1
    modrun m4-install good root install "${MODULE}" --yes --plan-id "${pid}"
    gx m4-log modlog
    snap m4-after || return 1
    modrun m4-status good root status "${MODULE}"
    modrun m4-list good root list
    host_call m4-host 120 || true
    CALL_NOAUTH=1 call m4-host-noauth GET "${MODULE_ROUTE}/host" "" 30
    # the gateway matches the start of the path: a name that begins with the route would reach the module (and its 400); this one begins with no route
    call m4-ghost GET "${MODULE_ROUTE%/*}/ghost_${MODULE_ROUTE##*/}/host" "" 30
    marker case_M4_done
}

case_m5() {
    local pid
    marker case_M5_start
    pid="$(plan_id_of_m3)"
    api_on || { marker case_M5_done; return 0; }
    API_ON=1
    call m5-list GET "${LIST_PATH}" "" 60
    call m5-second POST "${LIST_PATH}/${MODULE}/install" "{\"plan_id\":\"${pid}\"}" 120
    call m5-plan POST "${LIST_PATH}/${MODULE}/plan" "" 120
    call m5-status GET "${LIST_PATH}/${MODULE}/status" "" 60
    call m5-badname GET "${LIST_PATH}/Virt/status" "" 30
    CALL_NOAUTH=1 call m5-unauth GET "${LIST_PATH}" "" 30
    marker case_M5_done
}

need_api() {
    if [ -z "${API_ON:-}" ]; then
        api_on || return 1
        API_ON=1
    fi
    return 0
}

case_m6() {
    marker case_M6_start
    need_api || { marker case_M6_done; return 0; }
    # a file of the module's data, which a remove must leave
    gx m6-witness witness_on
    snap m6-before || return 1
    call m6-remove POST "${LIST_PATH}/${MODULE}/remove" "{}" 120
    if [ "${CALL_CODE}" = 202 ]; then
        wait_job m6-job "${WAIT_JOB}" "${TERMINAL_STATES}" || true
        gx m6-log modlog
        gx m6-journal journal
    fi
    call m6-status GET "${LIST_PATH}/${MODULE}/status" "" 60
    snap m6-after || return 1
    marker case_M6_done
}

case_m7() {
    local pid
    marker case_M7_start
    need_api || { marker case_M7_done; return 0; }
    ensure_clean m7-pre || { marker case_M7_done; return 0; }
    snap m7-before || return 1
    # what systemd answers to the glob the core's busy check asks it, with no unit, with one and with several
    gx m7-glob-none glob_show
    pend dummy "dummy_stop"
    gx m7-dummy-1 dummy_start 1
    gx m7-glob-one glob_show
    call m7-busy-dummy POST "${LIST_PATH}/${MODULE}/install" "{\"plan_id\":\"$(plan_id_of_m3)\"}" 120
    if [ "${CALL_CODE}" = 202 ]; then
        # a start that the unit did not stop: it is waited for, and the box made clean again
        wait_job m7-busy-job "${WAIT_JOB}" "${TERMINAL_STATES}" || true
    fi
    gx m7-dummy-2 dummy_start 2
    gx m7-glob-two glob_show
    gx m7-dummy-stop dummy_stop
    gx m7-glob-after glob_show
    if grep -q '^left=0' "${OUT}/m7-dummy-stop.out" 2>/dev/null; then unpend dummy; else harness_invalid M7 "the units that stand for jobs could not be stopped"; fi
    ensure_clean m7-mid || { marker case_M7_done; return 0; }
    # the plan, a plan_id that is not the plan's, and then the real install
    call m7-plan POST "${LIST_PATH}/${MODULE}/plan" "" 120
    pid="$(py get "${OUT}/m7-plan.json" data.plan_id)"
    printf '%s\n' "${pid}" >"${OUT}/m7-plan-id"
    call m7-stale POST "${LIST_PATH}/${MODULE}/install" '{"plan_id":"m1-00000000000000000000000000000000"}' 120
    call m7-install POST "${LIST_PATH}/${MODULE}/install" "{\"plan_id\":\"${pid}\"}" 120
    if [ "${CALL_CODE}" = 202 ]; then
        PROPS_TAG=m7-run
        wait_job m7-job "${WAIT_JOB}" "${TERMINAL_STATES}" || true
        PROPS_TAG=""
        gx m7-log modlog
        gx m7-journal journal
    fi
    gx m7-glob-end glob_show
    gx m7-props-end unit_props
    snap m7-after || return 1
    call m7-status GET "${LIST_PATH}/${MODULE}/status" "" 60
    host_call m7-host 120 || true
    marker case_M7_done
}

# ---- the dirty matrix ---------------------------------------------------------------------------------------------------------------

# a hostile archive: the catalogue names it with its own sha256, so the plan passes; the checking of the whole archive refuses it before a file is written
m8_hostile() {
    local v="$1" id p
    id="M8_${v}"
    p="m8-${v}"
    marker "case_${id}_start"
    if ensure_clean "${p}-pre"; then
        snap "${p}-before" || return 1
        modrun "${p}-plan" "${v}" root plan "${MODULE}"
        modrun "${p}-install" "${v}" root install "${MODULE}" --yes
        gx "${p}-log" modlog
        gx "${p}-escape" escape_state
        snap "${p}-after" || return 1
    fi
    marker "case_${id}_done"
}

case_m8_checksum() {
    local id=M8_checksum p=m8-checksum
    marker "case_${id}_start"
    if ensure_clean "${p}-pre"; then
        snap "${p}-before" || return 1
        modrun "${p}-plan" corrupt root plan "${MODULE}"
        modrun "${p}-install" corrupt root install "${MODULE}" --yes
        gx "${p}-log" modlog
        snap "${p}-after" || return 1
    fi
    marker "case_${id}_done"
}

case_m8_foreign() {
    local id=M8_foreign p=m8-foreign
    marker "case_${id}_start"
    if ensure_clean "${p}-pre"; then
        snap "${p}-before" || return 1
        pend foreign unit_dirt_off
        gx "${p}-dirt" unit_dirt_on
        modrun "${p}-plan" good root plan "${MODULE}"
        modrun "${p}-install" good root install "${MODULE}" --yes
        gx "${p}-log" modlog
        gx "${p}-undo" unit_dirt_off
        undone foreign "${p}-undo" '^off file=no'
        snap "${p}-after" || return 1
    fi
    marker "case_${id}_done"
}

case_m8_busy_dpkg() {
    local id=M8_busy_dpkg p=m8-busy-dpkg
    marker "case_${id}_start"
    if ensure_clean "${p}-pre"; then
        snap "${p}-before" || return 1
        pend lock lock_stop
        gx "${p}-dirt" lock_start
        gx "${p}-during" lock_state
        modrun "${p}-plan" good root plan "${MODULE}"
        modrun "${p}-install" good root install "${MODULE}" --yes
        CALL_CODE=""
        need_api && call "${p}-api" POST "${LIST_PATH}/${MODULE}/install" "{\"plan_id\":\"$(plan_id_of_m3)\"}" 120
        gx "${p}-undo" lock_stop
        undone lock "${p}-undo" '^free'
        gx "${p}-undone" lock_state
        if [ "${CALL_CODE:-}" = 202 ]; then wait_job "${p}-job" "${WAIT_JOB}" "${TERMINAL_STATES}" || true; ensure_clean "${p}-post" || true; fi
        snap "${p}-after" || return 1
    fi
    marker "case_${id}_done"
}

case_m8_busy_osu() {
    local id=M8_busy_osu p=m8-busy-osu
    marker "case_${id}_start"
    if ensure_clean "${p}-pre"; then
        snap "${p}-before" || return 1
        pend osflag osflag_off
        gx "${p}-dirt" osflag_on
        modrun "${p}-plan" good root plan "${MODULE}"
        modrun "${p}-install" good root install "${MODULE}" --yes
        CALL_CODE=""
        need_api && call "${p}-api" POST "${LIST_PATH}/${MODULE}/install" "{\"plan_id\":\"$(plan_id_of_m3)\"}" 120
        gx "${p}-undo" osflag_off
        undone osflag "${p}-undo" '^off flag=no'
        if [ "${CALL_CODE:-}" = 202 ]; then wait_job "${p}-job" "${WAIT_JOB}" "${TERMINAL_STATES}" || true; ensure_clean "${p}-post" || true; fi
        snap "${p}-after" || return 1
    fi
    marker "case_${id}_done"
}

case_m8_disk() {
    local id=M8_disk p=m8-disk
    marker "case_${id}_start"
    if ensure_clean "${p}-pre"; then
        snap "${p}-before" || return 1
        pend tmpfs tmpfs_off
        gx "${p}-dirt" tmpfs_on
        modrun "${p}-plan" good root plan "${MODULE}"
        modrun "${p}-install" good root install "${MODULE}" --yes
        gx "${p}-undo" tmpfs_off
        undone tmpfs "${p}-undo" '^unmounted no'
        snap "${p}-after" || return 1
    fi
    marker "case_${id}_done"
}

case_m8_symlink() {
    local id=M8_symlink p=m8-symlink
    marker "case_${id}_start"
    if ensure_clean "${p}-pre"; then
        snap "${p}-before" || return 1
        pend symlink symlink_off
        gx "${p}-dirt" symlink_on
        gx "${p}-canary-before" canary_state
        modrun "${p}-install" good root install "${MODULE}" --yes
        gx "${p}-log" modlog
        gx "${p}-canary-after" canary_state
        gx "${p}-undo" symlink_off
        undone symlink "${p}-undo" '^off link=no'
        # the install may have gone through (the link was swept away, not followed): the box is made clean again
        ensure_clean "${p}-post" || true
        snap "${p}-after" || return 1
    fi
    marker "case_${id}_done"
}

HOLD_PKG=""
case_m8_held() {
    local id=M8_held p=m8-held
    HOLD_PKG="$(grep -x ovmf "${KIT}/packages.list" || tail -n 1 "${KIT}/packages.list")"
    marker "case_${id}_start"
    if ensure_clean "${p}-pre"; then
        snap "${p}-before" || return 1
        # a package of the module that is not there, and is held: apt cannot install it
        pend held "hold_off ${HOLD_PKG}"
        gx "${p}-unseat" pkg_unseat "${HOLD_PKG}"
        gx "${p}-dirt" hold_on "${HOLD_PKG}"
        # what apt itself says of installing the packages with that package held
        gx "${p}-aptsim" aptsim $(tr '\n' ' ' <"${KIT}/packages.list")
        modrun "${p}-plan" good root plan "${MODULE}"
        modrun "${p}-install" good root install "${MODULE}" --yes
        gx "${p}-undo" hold_off "${HOLD_PKG}"
        undone held "${p}-undo" '^unheld'
        gx "${p}-reseat" pkg_reseat "${HOLD_PKG}"
        snap "${p}-after" || return 1
    fi
    marker "case_${id}_done"
}

AUTO_PKG=""
case_m8_manual() {
    local id=M8_manual p=m8-manual
    AUTO_PKG="$(grep -x qemu-utils "${KIT}/packages.list" || sed -n 1p "${KIT}/packages.list")"
    marker "case_${id}_start"
    if ensure_clean "${p}-pre"; then
        # a package of the module that is installed as a dependency: apt says "set to manually installed" when it is asked to install it, and writes its list when it is not told not to
        pend manual "auto_off ${AUTO_PKG}"
        gx "${p}-dirt" auto_on "${AUTO_PKG}"
        # what apt itself says of installing the packages with that package marked automatic
        gx "${p}-aptsim" aptsim $(tr '\n' ' ' <"${KIT}/packages.list")
        snap "${p}-before" || return 1
        modrun "${p}-plan" good root plan "${MODULE}"
        snap "${p}-after" || return 1
        gx "${p}-undo" auto_off "${AUTO_PKG}"
        undone manual "${p}-undo" '^manual auto=$'
    fi
    marker "case_${id}_done"
}

# want_sub <the cases a test asked for> <id>: the cases of M8 and M9 that the tests of this script pick (M8_ONLY, M9_ONLY: a comma list); all of them when none is asked for
want_sub() {
    [ -z "$1" ] && return 0
    [[ ",$1," == *",$2,"* ]]
}

case_m8() {
    local only="${M8_ONLY:-}"
    marker case_M8_start
    want_sub "${only}" checksum && { case_m8_checksum || return 1; }
    local v
    for v in dotdot systemctl varlib usrshare etcmod; do
        want_sub "${only}" "${v}" && { m8_hostile "${v}" || return 1; }
    done
    want_sub "${only}" foreign && { case_m8_foreign || return 1; }
    want_sub "${only}" busy_dpkg && { case_m8_busy_dpkg || return 1; }
    want_sub "${only}" busy_osu && { case_m8_busy_osu || return 1; }
    want_sub "${only}" disk && { case_m8_disk || return 1; }
    want_sub "${only}" symlink && { case_m8_symlink || return 1; }
    want_sub "${only}" held && { case_m8_held || return 1; }
    want_sub "${only}" manual && { case_m8_manual || return 1; }
    marker case_M8_done
}

# ---- the kills ------------------------------------------------------------------------------------------------------------------------

# m9_case <id> <kill|term|stop> <step> <signal or dpkg|none> <unseat|keep> <seconds for the job to end>: the install is started through the core, a watcher on the guest sees the step begin in
# the log and signals the assistant or stops the unit, and the job is followed to its end; the box is asked what is left, `remove` finishes what was started, and a fresh install must go through
m9_case() {
    local id="$1" mode="$2" step="$3" arg="$4" pre="$5" within="$6" p pid
    want_sub "${M9_ONLY:-}" "${id}" || return 0
    p="m9-$(printf '%s' "${id}" | tr '_' '-')"
    marker "case_M9_${id}_start"
    need_api || { marker "case_M9_${id}_done"; return 0; }
    if ensure_clean "${p}-pre"; then
        if [ "${pre}" = unseat ]; then gx "${p}-unseat" pkg_unseat_all; fi
        # the log of the module is the log of a run by hand again (the watcher looks for the marker of the core's job): a remove of what is not there refuses, and writes it
        modrun "${p}-reset" good root remove "${MODULE}" --yes
        snap "${p}-before" || return 1
        call "${p}-plan" POST "${LIST_PATH}/${MODULE}/plan" "" 120
        pid="$(py get "${OUT}/${p}-plan.json" data.plan_id)"
        case "${mode}" in
        kill | term) gx "${p}-watch" watch_start kill "${step}" "${arg}" ;;
        stop) gx "${p}-watch" watch_start stop "${step}" "${arg}" ;;
        esac
        call "${p}-start" POST "${LIST_PATH}/${MODULE}/install" "{\"plan_id\":\"${pid}\"}" 120
        if [ "${CALL_CODE}" = 202 ]; then
            wait_job "${p}-job" "${within}" "${TERMINAL_STATES}" || true
            wait_watch "${p}" 120
            gx "${p}-wait" wait_clean "${WAIT_CLEAN:-700}"
            gx "${p}-log" modlog
            gx "${p}-journal" journal
            snap "${p}-dead" || return 1
            call "${p}-status" GET "${LIST_PATH}/${MODULE}/status" "" 60
        fi
        # what a kill left is finished by the assistant's own remove, which must leave nothing; and a fresh install must go through
        modrun "${p}-remove" good root remove "${MODULE}" --yes
        gx "${p}-fp" footprint
        snap "${p}-clean" || return 1
        modrun "${p}-fresh" good root install "${MODULE}" --yes
        snap "${p}-fresh" || return 1
        modrun "${p}-rm2" good root remove "${MODULE}" --yes
        gx "${p}-fp2" footprint
    fi
    marker "case_M9_${id}_done"
}

case_m9() {
    marker case_M9_start
    # the packages are taken off for the first of them: apt then has work to do, which a kill or a stop in the step interrupts
    m9_case kill_packages kill packages KILL unseat "${WAIT_KILLED_APT}" || return 1
    m9_case kill_download kill download KILL keep "${WAIT_KILLED}" || return 1
    m9_case kill_verify kill verify KILL keep "${WAIT_KILLED}" || return 1
    m9_case kill_unpack kill unpack KILL keep "${WAIT_KILLED}" || return 1
    m9_case kill_units kill units KILL keep "${WAIT_KILLED}" || return 1
    m9_case kill_manifest kill manifest KILL keep "${WAIT_KILLED}" || return 1
    m9_case kill_start kill start KILL keep "${WAIT_KILLED}" || return 1
    m9_case kill_route kill route KILL keep "${WAIT_KILLED}" || return 1
    m9_case stop_packages stop packages dpkg unseat "${WAIT_KILLED_APT}" || return 1
    m9_case stop_start stop start none keep "${WAIT_KILLED_APT}" || return 1
    m9_case term_start term start TERM keep "${WAIT_KILLED}" || return 1
    marker case_M9_done
}

# ---- the restart ------------------------------------------------------------------------------------------------------------------------

# reboot_cycle <tag>: the guest is asked to restart, goes away and comes back with another boot id, the core answers again and the box's user logs in. Status 1: the restart was not
# asked for; 2: the guest did not come back; 3: the core did not answer.
reboot_cycle() {
    local tag="$1" old
    old="$(boot_id)"
    printf '%s\n' "${old:--}" >"${OUT}/${tag}.boot-old"
    GX_RETRY=0 gx "${tag}-ask" reboot
    [ "$(cat "${OUT}/${tag}-ask.rc")" = 0 ] || return 1
    wait_boot "${tag}" "${old}" "${WAIT_BOOT:-900}" || return 2
    wait_up "${WAIT_CORE:-420}" || return 3
    login || true
    return 0
}

# wait_route <tag> <seconds>: the module's service answers on its route, as the assistant's status says it
wait_route() {
    local tag="$1" end
    now_s
    end=$((NOW_S + $2))
    while :; do
        modrun "${tag}" good root status "${MODULE}"
        if [ "$(py get "${OUT}/${tag}.out" route.answers)" = true ]; then return 0; fi
        now_s
        if [ "${NOW_S}" -ge "${end}" ]; then return 1; fi
        nap 5
    done
}

case_m10() {
    marker case_M10_start
    if [ -n "${API_ON:-}" ]; then api_off || true; API_ON=""; fi
    ensure_clean m10-pre || { marker case_M10_done; return 0; }
    modrun m10-install good root install "${MODULE}" --yes
    snap m10-before || return 1
    if reboot_cycle m10-reboot; then
        wait_route m10-status "${WAIT_ROUTE:-240}" || true
        snap m10-after || return 1
        host_call m10-host 120 || true
        modrun m10-remove good root remove "${MODULE}" --yes
        gx m10-fp footprint
        snap m10-removed || return 1
    else
        echo "the guest did not come back, or the core did not answer" >"${OUT}/m10-reboot.failed"
        gx m10-diag diag
    fi
    marker case_M10_done
}

case_m11() {
    marker case_M11_start
    # the proof's own files go, so that what is left to compare is the module's doing
    gx m11-conf conf_remove
    gx m11-witness witness_off
    # the files of the packages the plan said it would install and upgrade: what they own is theirs, not the module's doing
    # shellcheck disable=SC2046
    gx m11-owned pkg_files $(py plan_packages "${OUT}/m3-plan-root.out" 2>/dev/null)
    gx m11-unseated unseat_owned
    snap m11-end full || return 1
    marker case_M11_done
}

# ---- the update ------------------------------------------------------------------------------------------------------------------------------
# The update cases need a core that has `casaos module update`: the version of the core is compared with MODULE_UPDATE_MIN_CORE before the word is ever given (an old core given `module`
# starts as a server), and a core that has not got it ends the group there, which the report turns into an INVALID RUN of the update cases that says so; the cases before it have run.
# The old version is the proof's own (0.1.1: the archive of M4 plus a second sample), the new one 0.1.2 (another second sample, a unit file that is not the same, one package more).

U_STATE=""          # what the box has of the module for these cases: old (installed by hand, with the administrator's configuration), new, or "" (not known)
UPD_PKGS=""

# api_switch <variant> <tag>: the core runs with the test catalogue of the variant in its environment (restarted when it has another)
api_switch() {
    if [ -n "${API_ON:-}" ] && [ "${API_VARIANT:-}" = "$1" ]; then return 0; fi
    api_on "$1" "$2" || return 1
    API_ON=1
}

# u_old <tag>: the old version of the module, installed by hand over a box that has none, the administrator's own configuration in place (what the recordings of <tag>-* start from); status 1,
# and the run invalid, when it could not be had
u_old() {
    local t="$1"
    [ "${U_STATE}" = old ] && return 0
    U_STATE=""
    ensure_clean "${t}-pre" || return 1
    gx "${t}-conf" conf_write
    modrun "${t}-old" uold root install "${MODULE}" --yes
    if [ "$(cat "${OUT}/${t}-old.rc")" != 0 ]; then
        harness_invalid "${t}" "the old version of the module could not be installed to start from"
        return 1
    fi
    U_STATE=old
}

# u_new <tag>: the new version, by an update by hand of the old one
u_new() {
    local t="$1"
    [ "${U_STATE}" = new ] && return 0
    u_old "${t}-pre" || return 1
    modrun "${t}-update" unew root update "${MODULE}" --yes
    if [ "$(cat "${OUT}/${t}-update.rc")" != 0 ]; then
        harness_invalid "${t}" "the module could not be updated to start from the new version"
        U_STATE=""
        return 1
    fi
    U_STATE=new
}

# u_purge <tag>: the package that the update adds is off the box, as the case that follows needs it (recorded as <tag>-purge; the report makes the run invalid if it is still there)
u_purge() {
    gx "$1-purge" purge_added
}

case_u1() {
    marker case_U1_start
    if u_old u1; then
        modrun u1-list-old uold root list
        modrun u1-list-new unew root list
        modrun u1-list-nobody unew nobody list
    fi
    marker case_U1_done
}

case_u2() {
    marker case_U2_start
    if u_old u2; then
        snap u2-before || return 1
        # shellcheck disable=SC2086
        gx u2-aptsim aptsim ${UPD_PKGS}
        modrun u2-plan unew root plan "${MODULE}" --update
        modrun u2-plan-nobody unew nobody plan "${MODULE}" --update
        modrun u2-plan-same uold root plan "${MODULE}" --update
        snap u2-after || return 1
        # shellcheck disable=SC2086
        gx u2-aptsim-after aptsim ${UPD_PKGS}
        snap u2-after-sim || return 1
    fi
    marker case_U2_done
}

case_u3() {
    local pid
    marker case_U3_start
    if u_old u3; then
        modrun u3-plan unew root plan "${MODULE}" --update
        pid="$(py get "${OUT}/u3-plan.out" plan_id)"
        printf '%s\n' "${pid}" >"${OUT}/u3-plan-id"
        snap u3-before || return 1
        modrun u3-update unew root update "${MODULE}" --yes --plan-id "${pid}"
        gx u3-log modlog
        snap u3-after || return 1
        modrun u3-status unew root status "${MODULE}"
        modrun u3-list unew root list
        host_call u3-host 120 || true
        CALL_NOAUTH=1 call u3-host-noauth GET "${MODULE_ROUTE}/host" "" 30
        # now that the module is at the catalogue's tag, and then with a catalogue older than it
        modrun u3-same-plan unew root plan "${MODULE}" --update
        modrun u3-down-plan uold root plan "${MODULE}" --update
        U_STATE=new
    fi
    marker case_U3_done
}

# the update through the API: the plan of an install on a box that has none (its id must not do for an update), the old version, the plan of the update, the ids that are refused, the job
case_u4() {
    local ipid upid
    marker case_U4_start
    U_STATE=""
    if ensure_clean u4-pre && api_switch unew u4-api; then
        call u4-plan-install POST "${LIST_PATH}/${MODULE}/plan" "" 120
        ipid="$(py get "${OUT}/u4-plan-install.json" data.plan_id)"
        if u_old u4; then
            u_purge u4
            call u4-plan POST "${LIST_PATH}/${MODULE}/plan" '{"mode":"update"}' 120
            upid="$(py get "${OUT}/u4-plan.json" data.plan_id)"
            printf '%s\n' "${upid}" >"${OUT}/u4-plan-id"
            call u4-stale POST "${LIST_PATH}/${MODULE}/install" '{"plan_id":"m1-00000000000000000000000000000000","mode":"update"}' 120
            call u4-wrongmode POST "${LIST_PATH}/${MODULE}/install" "{\"plan_id\":\"${ipid}\",\"mode\":\"update\"}" 120
            call u4-badmode POST "${LIST_PATH}/${MODULE}/install" "{\"plan_id\":\"${upid}\",\"mode\":\"upgrade\"}" 60
            snap u4-before || return 1
            call u4-start POST "${LIST_PATH}/${MODULE}/install" "{\"plan_id\":\"${upid}\",\"mode\":\"update\"}" 120
            if [ "${CALL_CODE}" = 202 ]; then
                PROPS_TAG=u4-run
                wait_job u4-job "${WAIT_JOB}" "${TERMINAL_STATES}" || true
                PROPS_TAG=""
                gx u4-log modlog
                gx u4-journal journal
            fi
            snap u4-after || return 1
            call u4-status GET "${LIST_PATH}/${MODULE}/status" "" 60
            call u4-list GET "${LIST_PATH}" "" 60
            host_call u4-host 120 || true
            U_STATE=""
        fi
    fi
    marker case_U4_done
}

# a file of the new version that is there, and is not the module's, refuses the update: the sample both versions carry, edited by the administrator
case_u5() {
    marker case_U5_start
    if u_old u5; then
        u_purge u5
        pend sample sample_edit_off
        gx u5-dirt sample_edit_on
        snap u5-before || return 1
        modrun u5-update unew root update "${MODULE}" --yes
        gx u5-log modlog
        snap u5-after || return 1
        gx u5-undo sample_edit_off
        undone sample u5-undo '^off sha='
        if [ ! -e "${WORK}/pending-sample" ]; then U_STATE=old; else U_STATE=""; fi
    fi
    marker case_U5_done
}

# u6_run <id> <variant> <drop-in or ->: an update that fails after it has changed the box, and what the box is after it
u6_run() {
    local id="$1" variant="$2" dirt="$3" p="u6-$1"
    marker "case_U6_${id}_start"
    U_STATE=""
    if u_old "${p}"; then
        u_purge "${p}"
        if [ "${dirt}" != - ]; then
            pend dropin dropin_off
            gx "${p}-dirt" dropin_on "${dirt}"
        fi
        snap "${p}-before" || return 1
        modrun "${p}-update" "${variant}" root update "${MODULE}" --yes
        gx "${p}-log" modlog
        snap "${p}-after" || return 1
        if [ "${dirt}" != - ]; then
            gx "${p}-undo" dropin_off
            undone dropin "${p}-undo" '^off file=no dir=no'
        fi
    fi
    marker "case_U6_${id}_done"
}

case_u6() {
    marker case_U6_start
    u6_run start unew failstart || return 1
    u6_run unit ubad - || return 1
    marker case_U6_done
}

# u7_case <id> <kill|stop> <step> <signal or none> <drop-in or -> <seconds for the job to end>: the update is started through the core, a watcher on the guest sees the step begin in the log and
# kills the assistant or stops the unit, and the job is followed to its end; then the assistant is asked, with the catalogue of the old version, to recover (it puts the old version back,
# whatever the kill left, and finds nothing to update), and with the catalogue of the new one to update
u7_case() {
    local id="$1" mode="$2" step="$3" arg="$4" dirt="$5" within="$6" p pid started
    want_sub "${U7_ONLY:-}" "${id}" || return 0
    p="u7-$(printf '%s' "${id}" | tr '_' '-')"
    marker "case_U7_${id}_start"
    U_STATE=""
    if u_old "${p}" && api_switch unew "${p}-api"; then
        u_purge "${p}"
        snap "${p}-before" || return 1
        call "${p}-plan" POST "${LIST_PATH}/${MODULE}/plan" '{"mode":"update"}' 120
        pid="$(py get "${OUT}/${p}-plan.json" data.plan_id)"
        if [ "${dirt}" != - ]; then
            pend dropin dropin_off
            gx "${p}-dirt" dropin_on "${dirt}"
        fi
        gx "${p}-watch" watch_start "${mode}" "${step}" "${arg}"
        call "${p}-start" POST "${LIST_PATH}/${MODULE}/install" "{\"plan_id\":\"${pid}\",\"mode\":\"update\"}" 120
        started="${CALL_CODE}"
        if [ "${started}" = 202 ]; then
            wait_job "${p}-job" "${within}" "${TERMINAL_STATES}" || true
            wait_watch "${p}" 120
        fi
        if [ "${dirt}" != - ]; then
            gx "${p}-undo" dropin_off
            undone dropin "${p}-undo" '^off file=no dir=no'
        fi
        if [ "${started}" = 202 ]; then
            gx "${p}-wait" wait_clean "${WAIT_CLEAN:-700}"
            gx "${p}-log" modlog
            gx "${p}-journal" journal
            snap "${p}-dead" || return 1
            call "${p}-status" GET "${LIST_PATH}/${MODULE}/status" "" 60
        fi
        modrun "${p}-recover" uold root update "${MODULE}" --yes
        gx "${p}-recover-log" modlog
        snap "${p}-recovered" || return 1
        modrun "${p}-update" unew root update "${MODULE}" --yes
        snap "${p}-new" || return 1
    fi
    U_STATE=""
    marker "case_U7_${id}_done"
}

case_u7() {
    marker case_U7_start
    # (the steps that last milliseconds are shorter than any watcher: the report says so when a signal did not land in the step it was meant for; stop and start are made long by a drop-in)
    u7_case kill_backup kill backup KILL - "${WAIT_KILLED}" || return 1
    u7_case kill_stop kill stop KILL slowstop "${WAIT_KILLED}" || return 1
    u7_case kill_swap kill swap KILL - "${WAIT_KILLED}" || return 1
    u7_case kill_units kill units KILL - "${WAIT_KILLED}" || return 1
    u7_case kill_start kill start KILL slowstart "${WAIT_KILLED}" || return 1
    u7_case stop_swap stop swap none - "${WAIT_KILLED_APT}" || return 1
    u7_case stop_start stop start none slowstart "${WAIT_KILLED_APT}" || return 1
    marker case_U7_done
}

# U9: the guest restarts with the new version installed
case_u9() {
    marker case_U9_start
    if [ -n "${API_ON:-}" ]; then api_off u9-api || true; API_ON=""; API_VARIANT=""; fi
    if u_new u9; then
        snap u9-before || return 1
        if reboot_cycle u9-reboot; then
            wait_route u9-status "${WAIT_ROUTE:-240}" || true
            snap u9-after || return 1
            host_call u9-host 120 || true
        else
            echo "the guest did not come back, or the core did not answer" >"${OUT}/u9-reboot.failed"
            gx u9-diag diag
        fi
        U_STATE=new
    fi
    marker case_U9_done
}

# U8, the last: the module is taken off, the proof's own files go, and the box is compared with what it was before the update cases
case_u8() {
    marker case_U8_start
    if [ -n "${API_ON:-}" ]; then api_off u8-api || true; API_ON=""; API_VARIANT=""; fi
    if ensure_clean u8-pre; then
        u_purge u8
        gx u8-conf conf_remove
        # shellcheck disable=SC2086
        gx u8-owned pkg_files ${UPD_PKGS}
        gx u8-unseated unseat_owned
        snap u8-end full || return 1
    fi
    U_STATE=""
    marker case_U8_done
}

case_u() {
    marker case_U_start
    gx u-version casaos_v
    printf '%s\n' "${UPD_MIN_CORE}" >"${OUT}/u-min"
    py version_ge "$(tr -d '\r\n' <"${OUT}/u-version.out")" "${UPD_MIN_CORE}"
    printf '%s\n' "$?" >"${OUT}/u-cmp"
    if [ "$(cat "${OUT}/u-cmp")" != 0 ]; then
        marker case_U_done
        return 0
    fi
    UPD_PKGS="$(tr '\n' ' ' <"${KIT}/upd-packages.list")"
    if ensure_clean u-pre; then
        # the package the update adds is not on the box, so that apt has work to do when the module is updated
        # shellcheck disable=SC2086
        gx u-unseat pkg_unseat ${UPD_PKGS}
        snap u-start full || return 1
        want_sub "${U_ONLY:-}" u1 && { case_u1 || return 1; }
        want_sub "${U_ONLY:-}" u2 && { case_u2 || return 1; }
        want_sub "${U_ONLY:-}" u3 && { case_u3 || return 1; }
        want_sub "${U_ONLY:-}" u9 && { case_u9 || return 1; }
        want_sub "${U_ONLY:-}" u4 && { case_u4 || return 1; }
        want_sub "${U_ONLY:-}" u5 && { case_u5 || return 1; }
        want_sub "${U_ONLY:-}" u6 && { case_u6 || return 1; }
        want_sub "${U_ONLY:-}" u7 && { case_u7 || return 1; }
        want_sub "${U_ONLY:-}" u8 && { case_u8 || return 1; }
        want_sub "${U_ONLY:-}" u8 || ensure_clean u-end || true
    fi
    marker case_U_done
}

# ---- the runner's side of the run ------------------------------------------------------------------------------------------------

want_case() {
    [ -z "${CASES:-}" ] && return 0
    [[ ",${CASES}," == *",$1,"* ]]
}

run_case() {
    local id="$1" fn="$2"
    want_case "${id}" || return 0
    "${fn}" || abort "the harness could not finish the case ${id}: a snapshot of the guest could not be had (see snap-*.failed)"
}

init_work() {
    WORK="${WORK:-${PWD}/mp-work}"
    mkdir -p "${WORK}"
    chmod 700 "${WORK}"
    write_mp_py "${WORK}/mp.py"
}

init_out() {
    OUT="${OUT:-${PWD}/mp-out}"
    KIT="${KIT:-${PWD}/kit}"
    mkdir -p "${OUT}"
    init_work
}

cleanup() {
    local rc=$? f name
    if [ -n "${OUT:-}" ] && [ -d "${OUT}" ] && [ -d "${WORK:-/nonexistent}" ]; then
        # an undo that is pending is made once more: the guest is not left with dpkg's lock held, a tmpfs over the area of the install, a package held or a flag of the upgrade up
        for f in "${WORK}"/pending-*; do
            [ -e "${f}" ] || continue
            name="${f##*/pending-}"
            if g "$(cat "${f}")" >"${OUT}/pending-${name}.out" 2>&1; then rm -f "${f}"; fi
        done
        for f in "${WORK}"/pending-*; do
            [ -e "${f}" ] || continue
            harness_invalid run "an undo was still pending when the run ended (${f##*/pending-})"
        done
        if [ -n "${API_ON:-}" ]; then
            g api_env_off >"${OUT}/api-off-final.out" 2>&1 || harness_invalid run "the test variables are still in the environment of the manager"
        fi
        if [ "${rc}" -ne 0 ] && [ ! -s "${OUT}/aborted" ]; then
            echo "the runner script ended on exit ${rc} (its output says where)" >>"${OUT}/aborted"
        fi
    fi
    exit "${rc}"
}

runner_run() {
    local leg="${1:-}"
    case "${leg}" in d12 | u24 | u22) ;; *) echo "usage: $0 run d12|u24|u22" >&2; exit 2 ;; esac
    set -u
    init_out
    rm -f "${WORK}"/pending-*
    : >"${OUT}/timeline.tsv"
    printf '%s\n' "${leg}" >"${OUT}/leg"
    printf '%s\n' "${TAG:-}" >"${OUT}/tag"
    date -u +%FT%TZ >"${OUT}/started"
    trap cleanup EXIT
    [ -n "${EPOCHREALTIME:-}" ] || abort "this bash has no EPOCHREALTIME (bash 5 is needed)"
    cp "${KIT}/kit.json" "${OUT}/kit.json" 2>/dev/null || abort "the kit has no kit.json: build-kit was not run"
    cp "${KIT}/binary-version" "${OUT}/binary-version" 2>/dev/null || true
    login || abort "the box's user could not log in: no token"
    GUEST_HOST="$(g hostname | tr -d '\r\n')"
    [ -n "${GUEST_HOST}" ] || abort "the guest does not answer ssh"
    printf '%s\n' "${GUEST_HOST}" >"${OUT}/hostname"
    marker run_start
    run_case m1 case_m1
    run_case m2 case_m2
    run_case m3 case_m3
    run_case m4 case_m4
    run_case m5 case_m5
    run_case m6 case_m6
    run_case m7 case_m7
    run_case m8 case_m8
    run_case m9 case_m9
    run_case m10 case_m10
    run_case m11 case_m11
    run_case u case_u
    date -u +%FT%TZ >"${OUT}/finished"
    log "done"
    return 0
}

# runner_prepare: the files go to the guest, the guest prepares itself, what it wrote comes back, the box's user is made and logs in
runner_prepare() {
    local pw key
    set -u
    init_out
    : >"${OUT}/timeline.tsv"
    : "${INSTALL_SH:?INSTALL_SH names the published install.sh}"
    : "${COMPONENTS_LOCK_FILE:?COMPONENTS_LOCK_FILE names the published components.lock}"
    [ -s "${KIT}/kit.json" ] || abort "the kit has no kit.json: build-kit was not run"
    scp -F "${SSH_CONFIG}" "${INSTALL_SH}" "${COMPONENTS_LOCK_FILE}" "${MP_HERE}/os-readiness-proof.sh" "${MP_HERE}/module-proof.sh" "${SSH_HOST}:" ||
        abort "the files could not be copied to the guest"
    scp -r -F "${SSH_CONFIG}" "${KIT}" "${SSH_HOST}:mp-kit" || abort "the kit could not be copied to the guest"
    SSH_LIMIT=3600 gssh "sudo env MP_DISPOSABLE=1 INSTALLER_ARGS='${INSTALLER_ARGS:-}' KIT_SRC=/home/casa/mp-kit OUT=/home/casa/mp-guest-out bash module-proof.sh guest-prepare" || true
    mkdir -p "${OUT}/guest"
    gssh 'sudo tar -C /home/casa/mp-guest-out -cf - .' | tar -xf - -C "${OUT}/guest" || true
    [ ! -s "${OUT}/guest/aborted" ] || abort "the guest's preparation stopped: $(tr '\n' ' ' <"${OUT}/guest/aborted")"
    [ -s "${OUT}/guest/prepared" ] || abort "the guest's preparation did not reach its end"
    # the box's own user: the one login the page would make
    pw="Aa1-$(head -c12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    (umask 077; printf '%s' "${pw}" >"${WORK}/pw")
    curl -sS --max-time 30 "${GW}/v1/users/status" -o "${WORK}/status.json" || abort "the gateway does not answer through the forward (port ${GW_PORT})"
    key="$(py get "${WORK}/status.json" data.key)"
    [ "${key}" != - ] || abort "the box has no registration key: $(head -c 200 "${WORK}/status.json")"
    curl -sS --max-time 30 -X POST "${GW}/v1/users/register" -H 'Content-Type: application/json' \
        -d "$(printf '{"username":"%s","password":"%s","key":"%s"}' "${PROOF_USER}" "${pw}" "${key}")" -o "${WORK}/register.json" || abort "the box's user could not be made"
    login || abort "the box's user could not log in: $(head -c 200 "${WORK}/login.json" 2>/dev/null)"
    date -u +%FT%TZ >"${OUT}/prepared"
}

# runner_build_kit <module dir> <kit dir>: the program of the module is built the way its release builds it (the hooks of its goreleaser configuration, then CGO_ENABLED=0 for linux/amd64 with
# its ldflags), and the kit is made of it
runner_build_kit() {
    local module_dir="${1:-}" out="${2:-}" commit built
    if [ ! -d "${module_dir}" ] || [ -z "${out}" ]; then echo "usage: $0 build-kit MODULE_DIR KIT_DIR" >&2; exit 2; fi
    init_work
    commit="$(git -C "${module_dir}" rev-parse HEAD)" || { echo "${module_dir} is not a checkout of the module" >&2; exit 1; }
    built="$(date -u +%FT%TZ)"
    (
        set -e
        cd "${module_dir}"
        go generate
        go mod tidy
        CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -ldflags "-X main.commit=${commit} -X main.date=${built} -s -w" -o build/sysroot/usr/bin/casaos-virt-management .
    ) || { echo "the module could not be built" >&2; exit 1; }
    python3 "${WORK}/mp.py" kit "${module_dir}" "${out}" "${PROOF_VERSION}" "${commit}" "${PROOF_UPDATE_VERSION}" || exit 1
    "${module_dir}/build/sysroot/usr/bin/casaos-virt-management" -v >"${out}/binary-version" 2>&1 || true
    echo "the kit is in ${out}: $(python3 "${WORK}/mp.py" get "${out}/kit.json" archive.sha256)"
}

main() {
    case "${1:-}" in
    build-kit) shift; runner_build_kit "$@" ;;
    guest-prepare) guest_prepare ;;
    prepare) runner_prepare ;;
    run) shift; runner_run "$@" ;;
    *) echo "usage: $0 build-kit MODULE_DIR KIT_DIR | guest-prepare | prepare | run d12|u24|u22" >&2; exit 2 ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then main "$@"; fi
