#!/usr/bin/env python3
"""Turn what os-readiness-proof.sh wrote into verdicts, or say the run is invalid.

    os-readiness-proof-report.py <out dir> <d11|u22>

Prints a markdown report (and writes it to <out dir>/report.md).

Exit status: 0 only when no verdict is a FAIL and the run is valid. 1 for a FAIL (the readiness report changed something, answered wrongly, blocked, or
did not fire a check that it should) and 1 for an INVALID RUN (the harness could not do what it set out to: a step did not run, a call got no answer
of the route, the box could not be dirtied or could not be put back, the evidence is missing). An invalid run proves nothing and is never green. A
step that did not run is INVALID, not a verdict: there is no skipped check that stays quiet. A step whose undo failed makes the leg INVALID, never a
FAIL of the feature. NOT TESTABLE is neutral and visible: what these guests cannot do, with the reason; it is never a pass.

What the guest did (os-readiness-proof.sh), in this order, on a fresh cloud image with ReCasaOS and Docker and a few witness containers:
  - PackageKit stopped (on an Ubuntu the apt run that ends the preparation wakes it, and it leaves by itself five minutes later: in the middle of the window, a
    unit that changed), and a wait until AppManagement has made its own check of the images (it renames a new /var/lib/casaos/image_updates.json into place);
  - a control: two snapshots of the box a few seconds apart with two calls to the core in between (GET /v1/sys/version/current, which answers the bare version
    as text and not the envelope: it is judged on its status alone), to know what changes by itself: those paths and units are not held against the readiness
    report, and the report names them;
  - the INVARIANCE window: a snapshot (the name, size, times, mode, owner and content hash of every file of /etc, /var/lib/dpkg, /var/lib/apt,
    /var/cache/apt, /var/lib/casaos and /var/log/casaos; every service unit with its state, main pid and invocation id; the listening ports), then
    the FIRST call of GET /v1/sys/os/readiness there is (ready-route: the one most likely to write something lazily, and the one that finds out whether the
    release has the route at all: a 404 ends the run there), GET /v1/sys/os, GET /v1/sys/os/readiness three times, and once more while another process holds
    dpkg's lock, then the same snapshot. Only /var/log/casaos may differ. Nothing else is looked at: not /tmp, not /run, not /home or /root (a tool that
    writes there, as `dkms status` may in $TMPDIR, is not seen). The call under the lock has to answer 200 inside the core's budget, say `busy`, and leave
    the lock to its holder;
  - the CLEAN box: the answers of that window (and one more) have no refuse; every warn and info is a fact of the report. A `busy` that names a unit (apt-daily)
    on the clean box is a box that a package manager was working on: the run is INVALID for it, and is not a FAIL of the feature. The box has smartctl (the
    installer puts smartmontools on every box), so `smart_failed` is never `no_smartctl` there: a virtio disk does not answer SMART, and that is `unavailable`;
  - the DIRTY MATRIX: one way of dirtying the guest at a time (a hold, an unfinished dpkg journal, a source of each kind, a foreign architecture, a
    reboot flag, a lock, PackageKit running, a missing fstab device, a container with no restart policy, a nearly full disk, AppManagement stopped, Docker
    stopped with its socket and Docker's service stopped alone with its socket listening...), a call, the undo, a call: the exact code with the exact severity
    has to appear, nothing else may appear with it, and after the undo the answer has to be the clean one again. A step may be asked more (expect): a Docker
    on hold with a newer one on offer is an info that says held true on d11 (the d11 guest is built to have that newer Docker on offer: a clean box that does
    not say so did not get its premise, and the run is INVALID, never a NOT TESTABLE that leaves the job green), a Docker that is not running leaves three
    checks saying why not_running; and
    after the call (after) the guest says what it sees: docker.service is still inactive when only the service was stopped, which a report that asked the
    daemon through the listening socket would have started. The calls of the steps that watch (an unfinished journal, a Default-Release, a foreign
    architecture, Docker stopped) are also looked at for what they write: dpkg's status, apt's lists and apt's caches before and after the call, which the
    invariance window, made of the clean calls, cannot see.

What these guests cannot do is NOT TESTABLE, with the reason: uptime_long (180 days), container, virt_xen, unproven_arch (the guest is a KVM amd64 machine),
an AppManagement operation in progress (none can be held open cheaply).
"""
import fnmatch
import json
import os
import re
import sys
from datetime import datetime

if len(sys.argv) != 3:
    print("usage: os-readiness-proof-report.py <out dir> <d11|u22>", file=sys.stderr)
    sys.exit(2)
out_dir, leg = sys.argv[1], sys.argv[2]

LEGS = {
    "d11": dict(family="debian", version="11", codename="bullseye", to_version="12", to_codename="bookworm"),
    "u22": dict(family="ubuntu", version="22.04", codename="jammy", to_version="24.04", to_codename="noble"),
}
STATES = ("ready", "attention", "blocked", "unsupported", "latest")
MAX_TEXT = 2000            # the longest title or detail
MAX_LIST = 50              # the longest list of strings in a check's data
MAX_STRING = 200           # the longest string of a check's data, alone or in a list
SUPPORTED_STATES = ("ready", "attention", "blocked")
SEVERITIES = ("refuse", "warn", "ok", "info")
BAD = ("refuse", "warn")
RANK = {"refuse": 0, "warn": 1}   # how bad a severity is (info and ok are the same here, the rest of the ladder)
ACTIONS = ("restart_machine", "finish_dpkg", "wait_for_package_manager", "wait_for_apps", "free_disk_space", "release_holds", "review_sources", "none")
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
# every code of the spec's table (os-readiness-spec.md, item 6): what is not among them is a code the core added, and the report says so
CANONICAL = (
    "unsupported_os", "vendor_os", "unproven_arch", "container", "virt_xen", "no_systemd", "not_root",
    "busy", "apps", "reboot_first", "uptime_long", "broken_dpkg", "held", "held_docker",
    "sources_cdrom", "sources_alias", "sources_pin", "sources_proposed", "sources_format", "sources_third_party", "sources_backports", "sources_firmware",
    "sources_network_checked_later",
    "foreign_arch", "keyring_old", "openssh_old", "host_services", "dkms",
    "merged_usr", "cgroup1", "bootloader", "root_zfs", "root_luks", "md_degraded", "smart_failed", "fstab_unresolved", "fstab_network", "log_on_tmpfs",
    "disk_root", "disk_archives", "disk_boot", "ram", "power",
    "network_uplink", "network_names",
    "docker_swarm", "docker_origin", "docker_down", "docker_update_pending", "containers_policy", "containers_ports", "backup_age", "snapshot_reminder",
    "latest", "not_offered", "hold_until",
)
TEXT_MAX = 8 * 1024        # Text(report) is at most 8 KiB
BUDGET = 20.0              # the core's budget for the whole call, in seconds
ALLOW = 25.0               # what a call may take before it counts as having blocked
CLOCK_SLACK = 3.0          # seconds between the guest's clock at the call and the core's checked_at
CURL_TIMEOUT = 28
NO_ANSWER = (0, 401, 403, 404, 502, 503, 504)   # the route gave no answer: the harness could not ask, or the release has no such route, or the core is not up
EXEMPT = "/var/log/casaos"                       # the one place the readiness report may write
TREES = ("/etc", "/var/lib/dpkg", "/var/lib/apt", "/var/cache/apt", "/var/lib/casaos", "/var/log/casaos")
COLS = ("type", "size", "mtime", "ctime", "mode", "owner", "digest")
MAX_NAMED = 12


class Step:
    """a way of dirtying the guest: id; the code it must make fire (or, mode quiet, leave quiet) and its severity; mode fires (the code is not in the clean
    answer), names (the code is in the clean answer already, and what the step adds must be named in it: needle) or quiet; forbid: codes that must not
    fire (a hold on Docker is not a hold); extras: codes (patterns) that may become worse with it because the dirt has that effect too; expect: rows
    (leg, code, severity, key, value, clean) of what the dirty answer also says: the code has this severity, and its data has this value under the key (no key: the
    severity alone), on this leg (* for both); clean is the severity the same code has on the clean box, which the row needs: a clean box that says another is a
    box on which the dirt cannot be told from what is there, NOT TESTABLE for a row of every leg (*), and an INVALID RUN for a row of ONE leg, which is that leg's
    premise (the d11 guest is built to have a newer Docker on offer: a guest that does not is a harness that did not get what the step needs, which a job that
    stays green would hide); after: what the guest saw once the call was over (after_<id> of the guest script), and
    after_what says what that means; watch: the guest took dpkg's status, apt's lists and apt's caches before and after the call, and the call may not have changed them"""

    def __init__(self, id, code, mode="fires", sev="refuse", needle="", forbid=(), extras=(), what="", see="", expect=(), after="", after_what="", watch=False):
        self.id, self.code, self.mode, self.sev, self.needle = id, code, mode, sev, needle
        self.forbid, self.extras, self.what, self.see = forbid, extras, what, see
        self.expect, self.after, self.after_what, self.watch = expect, after, after_what, watch


# what a box says of Docker when its daemon is not asked: nothing was looked at, and the check says so (the daemon is not asked, since asking would start it)
NOT_RUNNING = (("*", "docker_swarm", "info", "why", "not_running", "ok"), ("*", "containers_policy", "info", "why", "not_running", "ok"),
               ("*", "containers_ports", "info", "why", "not_running", "ok"))


STEPS = [
    Step("held", "held", what="apt-mark hold libc6", see="libc6"),
    # the guest does not hold Docker on the clean box: only this step does. A newer Docker on offer (the d11 guest has 28.0.4 with a 29) is a warning there, and an
    # info that says the packages are held and that Docker can be updated from the dashboard with the hold (held true) here
    Step("held_docker", "held_docker", sev="info", forbid=("held",), what="apt-mark hold docker-ce (harmless, the source is paused: not a hold)",
         expect=(("d11", "docker_update_pending", "info", "held", True, "warn"),)),
    Step("broken_dpkg", "broken_dpkg", what="a file of digits only in /var/lib/dpkg/updates", watch=True),
    Step("sources_cdrom", "sources_cdrom", what="a cdrom: source"),
    Step("sources_file", "sources_cdrom", what="a file: source"),
    Step("sources_alias", "sources_alias", what="a source that says `stable` (Debian) or `devel` (Ubuntu) where the box is on a codename"),
    Step("sources_pin", "sources_pin", what="APT::Default-Release naming the box's own release (apt fails on one that no source offers)", watch=True),
    Step("sources_pin_prefs", "sources_pin", what="a preferences file pinning another release"),
    Step("sources_proposed", "sources_proposed", what="a proposed-updates source"),
    Step("sources_third_party", "sources_third_party", mode="names", sev="warn", needle="osr-third-party", what="a third-party source file"),
    Step("foreign_arch_empty", "foreign_arch", mode="quiet", what="dpkg --add-architecture i386 and no package of it"),
    Step("foreign_arch", "foreign_arch", what="dpkg --add-architecture i386 and a package installed from it", watch=True),
    Step("reboot_first", "reboot_first", what="/var/run/reboot-required"),
    Step("fstab_unresolved", "fstab_unresolved", what="an fstab line, not nofail, for a device that is not there (the core reads fstab and /dev/disk, it asks no tool)"),
    Step("containers_policy", "containers_policy", sev="warn", needle="p-no", what="a running container with restart policy no"),
    Step("containers_ports_host", "containers_ports", sev="warn", what="a running container on the host network"),
    Step("containers_ports_publish", "containers_ports", sev="warn", what="a running container publishing host port 53 on a loopback alias (the gateway holds 80 and 443)"),
    Step("busy_lock", "busy", what="dpkg's lock held by another process"),
    Step("busy_unit", "busy", what="a unit of the package update running"),
    Step("busy_packagekit", "busy", mode="quiet", what="PackageKit running (the real unit on an Ubuntu, a unit of its name on a Debian): it is no package operation, and `busy` stays quiet"),
    Step("disk_boot", "disk_boot", extras=("disk_root", "disk_archives"), what="a file that leaves 200 MiB free on the filesystem of /boot (the root one on these images)"),
    Step("apps", "apps", what="AppManagement stopped (the core cannot ask: it fails closed)"),
    # AppManagement follows Docker's event stream and, when it breaks, connects again within a second: to a socket that is listening, which starts Docker. It is stopped
    # first, so that nothing but the report can be the one that starts it again; its `apps` check then cannot ask it, which this dirt brings with it
    Step("docker_down_service", "docker_down", sev="warn", extras=("apps",), expect=NOT_RUNNING,
         what="Docker's service stopped and its socket left listening, and AppManagement stopped (asking the daemon would start it)",
         after="inactive", after_what="docker.service is still inactive after the call: asking did not start Docker through its socket"),
    Step("docker_down", "docker_down", sev="warn", expect=NOT_RUNNING, watch=True, what="Docker stopped, and its socket (the core asks the service, and does not start it by asking the daemon)"),
]
# what these guests cannot do, and why (the report lists them: NOT TESTABLE is visible and never a pass)
NOT_TESTABLE = [
    ("uptime_long", "a guest that has run for 180 days is not something a job can make"),
    ("container", "the guest is a KVM machine, not a container, LXC or WSL"),
    ("virt_xen", "the guest is a KVM machine, not a Xen guest"),
    ("unproven_arch", "the guest is amd64: the only architecture the report accepts"),
    ("apps (an operation in progress)", "no AppManagement operation can be held open cheaply: the install of a small app is over in seconds, and the core lists operations only while they run; "
                                         "the fail-closed side (AppManagement does not answer) is tested by the step `apps`"),
]
INV_CALLS = ("ready-inv-1", "ready-inv-2", "ready-inv-3")
CLEAN_CALLS = INV_CALLS + ("ready-base",)
# the calls the guest makes between the first and the second snapshot of the invariance window, in its order: the first call of the route, GET /v1/sys/os, three more calls
# of the route, and the one made while another process holds dpkg's lock (os-readiness-proof.sh, invariance(): the harness checks that this is the list of the guest)
WINDOW_CALLS = ("ready-route", "sysos-inv") + INV_CALLS + ("ready-inv-lock",)

lines = []
P = lines.append
verdicts = []   # (status, name, detail)
problems = []   # the run cannot be trusted: reasons


def read(name, default=""):
    try:
        with open(os.path.join(out_dir, name), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def verdict(name, ok, detail=""):
    """ok None: the evidence is missing, which `invalid` has already said; no verdict is better than a made-up one"""
    if ok is not None:
        verdicts.append(("PASS" if ok else "FAIL", name, detail))


def not_testable(name, why):
    """what this box cannot do: named in the report, neither a pass nor a fail"""
    verdicts.append(("NOT TESTABLE", name, why))


def invalid(reason):
    if reason not in problems:
        problems.append(reason)


def finish():
    fails = [v for v in verdicts if v[0] == "FAIL"]
    head = "FAIL and INVALID RUN" if problems and fails else "INVALID RUN" if problems else "FAIL" if fails else "PASS"
    text = ["# OS readiness proof, %s leg: %s" % (leg, head), ""]
    if problems:
        text += ["Nothing below that depends on the missing or untrustworthy evidence can be believed:", ""]
        text += ["- INVALID: " + p for p in problems] + [""]
    text += lines
    text += ["", "## Verdicts", ""]
    text += ["- **%s** %s%s" % (s, n, (": " + d) if d else "") for s, n, d in verdicts]
    skipped = [v[1] for v in verdicts if v[0] == "NOT TESTABLE"]
    passes = sum(1 for v in verdicts if v[0] == "PASS")
    text += ["", "%d of %d verdicts pass%s%s." % (passes, passes + len(fails), "; failing: " + "; ".join(v[1] for v in fails) if fails else "",
                                                "; %d not testable on these guests: %s" % (len(skipped), "; ".join(skipped)) if skipped else "")]
    out = "\n".join(text) + "\n"
    try:
        with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
            f.write(out)
    except OSError:
        pass
    print(out)
    sys.exit(0 if not problems and not fails else 1)


def cell(x, n=100):
    """a value of the box's own answer, for a cell of a table or a sentence: printable ASCII, no bar, cut short"""
    return re.sub(r"[^\x20-\x7e]", "?", str(x)).replace("|", "/")[:n]


def few(items, n=MAX_NAMED):
    items = list(items)
    return ", ".join(cell(i, 160) for i in items[:n]) + (" and %d more" % (len(items) - n) if len(items) > n else "")


def timestamp(s):
    """seconds since the epoch of an RFC 3339 time (no zone: UTC), or None"""
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$", s or "")
    if not m:
        return None
    base, frac, zone = m.groups()
    try:
        return datetime.fromisoformat(base + ("." + (frac + "000000")[:6] if frac else "") + ("+00:00" if zone in (None, "Z") else zone)).timestamp()
    except ValueError:
        return None


if leg not in LEGS:
    invalid("the leg is d11 or u22, not %r" % leg)
    finish()
L = LEGS[leg]


# ---- the guest and what it recorded of itself ----------------------------------------------------------------------------------------

def osrelease():
    out = {}
    for line in read("os-release").splitlines():
        m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip().strip("\"'")
    return out


OSREL = osrelease()
FACTS = {}
for _line in read("box-facts").splitlines():
    _k, _, _v = _line.partition(" ")
    if _k:
        FACTS[_k] = _v.strip()

if read("aborted").strip():
    # first: it is why the rest of the evidence is missing
    invalid("the guest stopped early: " + " ".join(read("aborted").split())[:300])
if not OSREL.get("ID"):
    invalid("os-release is missing or empty: the guest did not say what system it is")
elif (OSREL.get("ID"), OSREL.get("VERSION_ID")) != (L["family"], L["version"]):
    invalid("the guest is %s %s, the %s leg needs %s %s" % (OSREL.get("ID"), OSREL.get("VERSION_ID"), leg, L["family"], L["version"]))
if read("leg").strip() != leg:
    invalid("the guest says it ran leg %r, the report is asked about %r" % (read("leg").strip(), leg))
if not read("finished").strip():
    invalid("the guest did not reach the end of its run (no `finished` file)")
if not read("timeline.tsv").strip():
    invalid("timeline.tsv is missing or empty: the run left no marker")


def needles():
    """[(kind, value)] the guest says must not be in the text: its own name, addresses and users"""
    out = []
    for line in read("leak-needles.tsv").splitlines():
        kind, _, value = line.partition("\t")
        if kind and value.strip():
            out.append((kind, value.strip()))
    if not any(k == "hostname" for k, _ in out) or not any(k == "ipv4" for k, _ in out) or not any(k == "user" for k, _ in out):
        invalid("leak-needles.tsv has no hostname, no IPv4 address or no user name: what the text must not say is not known")
    return out


NEEDLES = needles()


# ---- the calls ---------------------------------------------------------------------------------------------------------------------

def number(name):
    try:
        return float(read(name).strip())
    except ValueError:
        return None


def call(name, plain=False):
    """what the guest kept of one call, or None when it left no usable record of itself (INVALID): dict(name, http, curl, t0, t1, secs, kind, body, data) where
    kind is '' for an answer that was read, 'timeout' (curl gave up on it: the call blocked), 'cut' (curl got a status and then an error), 'unreadable' (200 and
    not a JSON object), 'error' (any other status of the route). plain is a call whose answer is text and not the envelope (GET /v1/sys/version/current says the
    bare version, route/v1.go: ctx.String(200, common.VERSION)): it is read as it is, the text is its body, and it has no data"""
    code, curl = read(name + ".code").strip(), read(name + ".curl").strip()
    if not code:
        invalid("no answer was recorded for %s: that step did not run" % name)
        return None
    t0, t1 = number(name + ".t0"), number(name + ".t1")
    if not curl.isdigit() or not code.isdigit() or t0 is None or t1 is None:
        invalid("%s.curl, .code, .t0 or .t1 is missing or unreadable: the call left no usable record of itself" % name)
        return None
    c = dict(name=name, http=int(code), curl=int(curl), t0=t0, t1=t1, secs=t1 - t0, kind="", body=None, data=None)
    if c["curl"] == CURL_TIMEOUT:
        c["kind"] = "timeout"
        return c
    if c["http"] in NO_ANSWER:
        err = " ".join(read(name + ".err").split())[:120]
        invalid("%s got no answer of the route: curl exit %s, HTTP %s%s%s" % (name, c["curl"], c["http"], " (%s)" % err if err else "",
                                                                             ": the release under test has no such route" if c["http"] == 404 and not plain else ""))
        return None
    if c["curl"] != 0:
        c["kind"] = "cut"
        return c
    if c["http"] != 200:
        c["kind"] = "error"
        return c
    if plain:
        c["body"] = read(name + ".json")
        return c
    try:
        body = json.loads(read(name + ".json"))
    except ValueError:
        body = None
    if not isinstance(body, dict):
        c["kind"] = "unreadable"
        return c
    c["body"] = body
    c["data"] = body.get("data") if isinstance(body.get("data"), dict) else None
    return c


def checks_of(c):
    """the checks of an answer that was read, those that are objects with a code that is a string (a check of any other shape is the shape verdict's)"""
    checks = (c["data"] or {}).get("checks") if c else None
    return [k for k in checks if isinstance(k, dict) and isinstance(k.get("code"), str)] if isinstance(checks, list) else []


def sev_map(c):
    """{code: severity} of the checks of an answer that was read (the first of a code: a code twice is a verdict of its own)"""
    out = {}
    for k in checks_of(c):
        out.setdefault(k.get("code"), k.get("severity"))
    return out


def bad_codes(c):
    return {code for code, sev in sev_map(c).items() if sev in BAD}


def non_ok(c):
    return {code: sev for code, sev in sev_map(c).items() if sev != "ok"}


def check_of(c, code):
    return next((k for k in checks_of(c) if k.get("code") == code), None)


CALLS = {}      # name -> call (readiness answers, the ones that left a record)
ANSWERS = {}    # name -> call, those that were read (kind '')


def get(name, plain=False):
    if name not in CALLS:
        CALLS[name] = call(name, plain)
        if CALLS[name] and not CALLS[name]["kind"]:
            ANSWERS[name] = CALLS[name]
    return CALLS[name]


def short(x):
    """a string of the data of a check: not longer than MAX_STRING, and with no control character in it"""
    return isinstance(x, str) and len(x) <= MAX_STRING and not re.search(r"[\x00-\x1f\x7f]", x)


def judge_answer(c):
    """{category: [what is wrong]} of one answer of the readiness route that was read"""
    bad = {k: [] for k in ("shape", "counts", "hop", "fields", "text", "time", "budget")}
    d, text = c["data"], None
    if c["body"].get("success") != 200 or not isinstance(c["body"].get("message"), str) or d is None:
        bad["shape"].append("not the envelope of the other routes (success 200, message, data object)")
        return bad
    if d.get("state") not in STATES:
        bad["shape"].append("state is %s, not one of %s" % (cell(d.get("state")), "/".join(STATES)))
    if not isinstance(d.get("checks"), list) or not all(isinstance(k, dict) for k in d.get("checks", [])):
        bad["shape"].append("checks is not a list of objects")
    counts = d.get("counts")
    if not isinstance(counts, dict) or sorted(counts) != sorted(SEVERITIES) or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in counts.values()):
        bad["shape"].append("counts is not four whole numbers (refuse, warn, ok, info)")
        counts = None
    for key in ("from", "to"):
        h = (d.get("hop") or {}).get(key) if isinstance(d.get("hop"), dict) else None
        if not isinstance(h, dict) or not all(isinstance(h.get(f), str) and h.get(f) for f in ("id", "version_id", "codename", "pretty_name")):
            bad["shape"].append("hop.%s is not an object with id, version_id, codename and pretty_name" % key)
    text = d.get("text")
    if not isinstance(text, str) or not text.strip():
        bad["shape"].append("text is missing or empty")
        text = None
    if not isinstance(d.get("checked_at"), str) or timestamp(d.get("checked_at")) is None:
        bad["shape"].append("checked_at is not an RFC 3339 time")
    if bad["shape"]:
        return bad
    # counts and state
    tally = {s: 0 for s in SEVERITIES}
    for k in d["checks"]:
        if isinstance(k.get("severity"), str) and k.get("severity") in tally:
            tally[k.get("severity")] += 1
    if counts is not None and counts != tally:
        bad["counts"].append("counts say %s, the checks are %s" % (counts, tally))
    want = "blocked" if tally["refuse"] else "attention" if tally["warn"] else "ready"
    if d["state"] in SUPPORTED_STATES and d["state"] != want:
        bad["counts"].append("state %s with %d refuse and %d warn (it is %s)" % (d["state"], tally["refuse"], tally["warn"], want))
    # the hop
    if d["state"] not in SUPPORTED_STATES:
        bad["hop"].append("state is %s: the box is a supported hop (%s %s to %s)" % (d["state"], L["family"], L["version"], L["to_version"]))
    frm, to = d["hop"]["from"], d["hop"]["to"]
    for what, got, expect in (("from.id", frm["id"], OSREL.get("ID")), ("from.version_id", frm["version_id"], OSREL.get("VERSION_ID")),
                              ("from.codename", frm["codename"], OSREL.get("VERSION_CODENAME")), ("to.id", to["id"], L["family"]),
                              ("to.version_id", to["version_id"], L["to_version"]), ("to.codename", to["codename"], L["to_codename"])):
        if got != expect:
            bad["hop"].append("hop.%s is %s, it is %s" % (what, cell(got), cell(expect)))
    # the checks
    seen = set()
    for k in d["checks"]:
        code = k.get("code")
        if not isinstance(code, str) or not CODE_RE.match(code):
            bad["fields"].append("a code that is not a code: %s" % cell(code))
            continue
        if code in seen:
            bad["fields"].append("%s twice" % code)
        seen.add(code)
        if k.get("severity") not in SEVERITIES:
            bad["fields"].append("%s: severity %s" % (code, cell(k.get("severity"))))
        # the title is always there; the detail is not (the core leaves it out of a check that has nothing to say), and when it is there it is a string
        if not isinstance(k.get("title"), str) or not k.get("title").strip() or len(k.get("title")) > MAX_TEXT:
            bad["fields"].append("%s: title is missing, empty or longer than %d" % (code, MAX_TEXT))
        if "detail" in k and (not isinstance(k.get("detail"), str) or len(k.get("detail")) > MAX_TEXT):
            bad["fields"].append("%s: detail is not a string, or is longer than %d" % (code, MAX_TEXT))
        if k.get("action") not in (None, "") + ACTIONS:
            bad["fields"].append("%s: action %s" % (code, cell(k.get("action"))))
        data = k.get("data")
        if not isinstance(data, dict):
            bad["fields"].append("%s: data is not an object" % code)
            continue
        for key, v in data.items():
            if isinstance(v, bool) or isinstance(v, (int, float)):
                continue
            if isinstance(v, list):
                if len(v) > MAX_LIST or not all(short(x) for x in v):
                    bad["fields"].append("%s: data.%s is a list of more than %d items, or has an item that is not a short string" % (code, cell(key, 30), MAX_LIST))
            elif not short(v):
                bad["fields"].append("%s: data.%s is not a short string, a number, a boolean or a list of short strings (raw tool output?)" % (code, cell(key, 30)))
        if "unknown" in data and not isinstance(data["unknown"], bool):
            bad["fields"].append("%s: data.unknown is not true or false" % code)
    # the text
    if text is not None:
        if len(text.encode("utf-8")) > TEXT_MAX:
            bad["text"].append("text is %d bytes, 8192 at most" % len(text.encode("utf-8")))
        if re.search(r"[^\x20-\x7e\n]", text):
            bad["text"].append("text is not plain ASCII (a control character, a tab, an escape or a non-ASCII letter)")
        if re.search(r"<[A-Za-z/!][^>]*>", text):
            bad["text"].append("text has markup")
        for kind, value in NEEDLES:
            if re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(value) + r"(?![A-Za-z0-9_-])", text, re.I):
                bad["text"].append("text says the box's %s (%s)" % (kind, cell(value, 40)))
        for what, rx in (("a MAC address", r"(?i)(?<![0-9a-f:])[0-9a-f]{2}(?::[0-9a-f]{2}){5}(?![0-9a-f:])"),
                         ("an IPv4 address", r"(?<![\w.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\w.])"),
                         ("a URL with a user name or password in it", r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s@]+@"),
                         ("a URL with a query", r"[A-Za-z][A-Za-z0-9+.-]*://[^\s]*\?")):
            m = re.search(rx, text)
            if m:
                bad["text"].append("text has %s (%s)" % (what, cell(m.group(0), 40)))
    # the time and the budget
    at = timestamp(d["checked_at"])
    if at is not None and not (c["t0"] - CLOCK_SLACK <= at <= c["t1"] + CLOCK_SLACK):
        bad["time"].append("checked_at %s is %+.0f s from the call (%.0f to %.0f)" % (d["checked_at"], at - (c["t0"] if at < c["t0"] else c["t1"]), c["t0"], c["t1"]))
    if c["secs"] > ALLOW:
        bad["budget"].append("took %.1f s" % c["secs"])
    return bad


# ---- the verdicts' names ----------------------------------------------------------------------------------------------------------

V_ROUTE = "GET /v1/sys/os/readiness answers 200 with a JSON envelope on every call, and none of them hangs or is cut off"
V_SHAPE = "every answer has the contract's shape: state, hop, counts, checks, text, checked_at"
V_COUNTS = "counts are the tally of the checks, and state follows them (blocked with a refuse, attention with a warn, ready with neither)"
V_HOP = "hop.from is this box's os-release, hop.to is the release the leg goes to, and the state is that of a supported hop"
V_FIELDS = "every check has a code (once), a severity, a title (and a detail when it has one), an action of the list when it has one, and data of short strings, numbers, booleans and lists of short strings"
V_TEXT = "text is plain ASCII, at most 8 KiB, and says nothing of the box's hostname, addresses, MAC or users, nor a URL with userinfo or a query"
V_TIME = "checked_at is the time of the call"
V_BUDGET = "every call answered within the core's 20 s budget (25 s allowed)"
V_SYSOS = "GET /v1/sys/os answers the box's own identity and names the release the leg goes to as next"
V_INV_FILES = "invariance: the calls created, removed or changed no file of /etc, /var/lib/dpkg, /var/lib/apt, /var/cache/apt or /var/lib/casaos (only /var/log/casaos may differ)"
V_INV_UNITS = "invariance: the calls started, stopped or restarted no service"
V_INV_PORTS = "invariance: the listening ports are the same after the calls"
V_LOCK_FAST = "with dpkg's lock held by another process the call answers 200 inside the budget (it did not wait for the lock)"
V_LOCK_BUSY = "with dpkg's lock held the answer says `busy` (refuse)"
V_LOCK_KEPT = "with dpkg's lock held, the lock was still its holder's after the call (the call did not take it)"
V_CLEAN_NO_REFUSE = "a clean cloud image with ReCasaOS and Docker has no refuse"
V_CLEAN_STATE = "the state of the clean box is ready or attention"
V_CLEAN_AGREE = "the four answers on the unchanged box say the same thing (same codes, same severities)"
V_CLEAN_DOCKER = "Docker's apt source is listed as a third-party source (warn), named"
V_CLEAN_SNAPSHOT = "the reminder to take a snapshot is always there (snapshot_reminder, info)"
V_CLEAN_SMART = "the clean box has smartctl, as the installer puts smartmontools on every box, and the core finds it: smart_failed is not `no_smartctl` (a virtio disk says `unavailable`, an info)"
F_FIRES = "step %s: the dirty box makes `%s` %s"
F_QUIET = "step %s: the dirty box leaves `%s` quiet"
F_UNDONE = "step %s: after the undo the answer is the clean one again (the code is gone, nothing else is left)"
F_ALONE = "step %s: nothing else fires with it"
F_EXPECT = "step %s: `%s` is %s%s"
F_AFTER = "step %s: %s"
F_WROTE = "step %s: the call changed nothing of dpkg's status, of apt's lists or of apt's caches"
F_FORBID = "step %s: `%s` does not fire"


# ---- the box ---------------------------------------------------------------------------------------------------------------------

P("## The box")
P("")
P("| | |")
P("|---|---|")
P("| system | %s |" % cell(read("os").strip() or OSREL.get("PRETTY_NAME", "?")))
P("| leg | %s: %s %s to %s %s |" % (leg, L["family"], L["version"], L["family"], L["to_version"]))
for k in ("kernel", "cpus", "memory_mb", "boot", "root", "boot_on", "virt", "docker", "docker_offer", "installer", "apt", "systemd", "smartctl", "packagekit_after_apt", "packagekit"):
    if FACTS.get(k):
        P("| %s | %s |" % (k, cell(FACTS[k])))
P("")


# ---- the calls and the contract of the answers ---------------------------------------------------------------------------------------

ctl = [get("ctl-core-%d" % i, plain=True) for i in (1, 2)]
for c in ctl:
    # the real route answers the bare version as text: what is asked of the control is that the core answered it (status 200, something said), not the shape of the envelope
    if c and (c["kind"] or c["http"] != 200):
        invalid("%s answered HTTP %s: the control of the invariance window did not run" % (c["name"], c["http"]))
    elif c and not c["body"].strip():
        invalid("%s answered HTTP 200 with no text: the control of the invariance window did not run (the core says its version)" % c["name"])
sysos = get("sysos-inv")
for n in ("ready-route",) + INV_CALLS + ("ready-inv-lock", "ready-base"):
    get(n)
for s in STEPS:
    if not os.path.exists(os.path.join(out_dir, "step-%s.skipped" % s.id)):
        get("step-%s-dirty" % s.id)
        get("step-%s-clean" % s.id)

readiness = [c for n, c in CALLS.items() if n.startswith(("ready-", "step-")) and c]
route_bad = ["%s: %s" % (c["name"], {"timeout": "curl gave up on it (it blocked)", "cut": "cut off (curl exit %d)" % c["curl"], "unreadable": "200 and not a JSON object",
                                      "error": "HTTP %d" % c["http"]}[c["kind"]]) for c in readiness if c["kind"]]
if readiness:
    verdict(V_ROUTE, not route_bad, few(route_bad, 4) if route_bad else "%d calls" % len(readiness))

problems_of = {k: [] for k in ("shape", "counts", "hop", "fields", "text", "time", "budget")}
for n, c in ANSWERS.items():
    if n == "sysos-inv" or n.startswith("ctl-"):
        continue
    for k, v in judge_answer(c).items():
        problems_of[k] += ["%s: %s" % (n, p) for p in v]
if any(n.startswith(("ready-", "step-")) for n in ANSWERS):
    for k, name in (("shape", V_SHAPE), ("counts", V_COUNTS), ("hop", V_HOP), ("fields", V_FIELDS), ("text", V_TEXT), ("time", V_TIME), ("budget", V_BUDGET)):
        verdict(name, not problems_of[k], few(problems_of[k], 4) if problems_of[k] else "%d answers" % len([n for n in ANSWERS if n.startswith(("ready-", "step-"))]))

if sysos is not None:
    if sysos["kind"] or sysos["data"] is None:
        verdict(V_SYSOS, False, "no usable answer (%s)" % (sysos["kind"] or "no data object"))
    else:
        d, bad = sysos["data"], []
        for k, want in (("id", OSREL.get("ID")), ("version_id", OSREL.get("VERSION_ID")), ("codename", OSREL.get("VERSION_CODENAME"))):
            if d.get(k) != want:
                bad.append("%s is %s, os-release says %s" % (k, cell(d.get(k)), cell(want)))
        nxt = d.get("next") if isinstance(d.get("next"), dict) else {}
        if (nxt.get("id"), nxt.get("version_id"), nxt.get("codename")) != (L["family"], L["to_version"], L["to_codename"]):
            bad.append("next is %s %s %s, the leg goes to %s %s %s" % (cell(nxt.get("id")), cell(nxt.get("version_id")), cell(nxt.get("codename")), L["family"], L["to_version"], L["to_codename"]))
        verdict(V_SYSOS, not bad, few(bad, 4) if bad else "%s %s %s, next %s" % (d.get("id"), d.get("version_id"), d.get("codename"), nxt.get("version_id")))


# ---- invariance --------------------------------------------------------------------------------------------------------------------------

def manifest(tag):
    text = read("inv-%s.tsv" % tag)
    if not text.strip():
        invalid("inv-%s.tsv is missing or empty: the snapshot of the files did not run" % tag)
        return None
    rows = {}
    for line in text.splitlines():
        f = line.split("\t")
        try:
            path = json.loads(f[0])
        except ValueError:
            path = None
        if len(f) != 8 or not isinstance(path, str):
            invalid("inv-%s.tsv has a row that is not a manifest row: %s" % (tag, cell(line, 80)))
            return None
        rows[path] = tuple(f[1:])
    for tree in TREES:
        if tree not in rows:
            invalid("inv-%s.tsv has no row for %s: that tree was not looked at" % (tag, tree))
            return None
    return rows


def exempt(path):
    return path == EXEMPT or path.startswith(EXEMPT + "/")


def diff_rows(a, b):
    """(changed [(path, [columns])], created [path], removed [path]) between two manifests"""
    changed = [(p, [COLS[i] for i in range(len(COLS)) if a[p][i] != b[p][i]]) for p in sorted(a.keys() & b.keys()) if a[p] != b[p]]
    return changed, sorted(b.keys() - a.keys()), sorted(a.keys() - b.keys())


def units(tag):
    text = read("units-%s.tsv" % tag)
    if not text.strip():
        invalid("units-%s.tsv is missing or empty: the list of the units did not run" % tag)
        return None
    rows = {}
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) != 5 or not f[0]:
            invalid("units-%s.tsv has a row that is not a unit row: %s" % (tag, cell(line, 80)))
            return None
        rows[f[0]] = tuple(f[1:])
    return rows


def ports(tag):
    rows = [ln.strip() for ln in read("listen-%s.txt" % tag).splitlines() if ln.strip()]
    if not rows:
        invalid("listen-%s.txt is missing or empty: no listening port was recorded (ss did not run)" % tag)
        return None
    return set(rows)


def apt_rows(sid, which):
    """{path: what the guest saw of it} of step-<sid>.state-<which>: dpkg's status and apt's caches by their sha256, apt's lists by size and time. None, and an
    INVALID RUN, when the file is missing, has a row that is not of the snapshot, or has no row for dpkg's status (a snapshot that was not made)"""
    rows = {}
    for line in read("step-%s.state-%s" % (sid, which)).splitlines():
        m = re.match(r"^list (\S+) (\d+) (\d+(?:\.\d+)?)$", line)
        if m:
            rows[m.group(1)] = "size %s, time %s" % (m.group(2), m.group(3))
            continue
        m = re.match(r"^([0-9a-f]{64})  (\S+)$", line)
        if not m:
            invalid("step %s: step-%s.state-%s has a row that is not one of the snapshot of dpkg and apt: %s" % (sid, sid, which, cell(line, 80)))
            return None
        rows[m.group(2)] = "sha256 " + m.group(1)
    if not any(p.endswith("/status") for p in rows):
        invalid("step %s: step-%s.state-%s is missing, or has no row for dpkg's status: the snapshot of dpkg and apt around the call was not made" % (sid, sid, which))
        return None
    return rows


ma, mb, m1, m2 = manifest("ctl-a"), manifest("ctl-b"), manifest("before"), manifest("after")
ua, ub, u1, u2 = units("ctl-a"), units("ctl-b"), units("before"), units("after")
pa, pb, p1, p2 = ports("ctl-a"), ports("ctl-b"), ports("before"), ports("after")
timers = read("timer-units.txt", None)
if timers is None:
    invalid("timer-units.txt is missing: the units that a timer starts are not known")
    timers = ""
TIMER_UNITS = {ln.strip() for ln in timers.splitlines() if ln.strip()}

noisy_files = noisy_units = noisy_ports = set()
if ma is not None and mb is not None:
    ch, cr, rm = diff_rows(ma, mb)
    noisy_files = {p for p, _ in ch} | set(cr) | set(rm)
if ua is not None and ub is not None:
    noisy_units = {u for u in ua.keys() | ub.keys() if ua.get(u) != ub.get(u)}
if pa is not None and pb is not None:
    noisy_ports = pa ^ pb

if m1 is not None and m2 is not None:
    ch, cr, rm = diff_rows(m1, m2)
    ch = [(p, c) for p, c in ch if not exempt(p) and p not in noisy_files]
    cr = [p for p in cr if not exempt(p) and p not in noisy_files]
    rm = [p for p in rm if not exempt(p) and p not in noisy_files]
    said = ["changed %s (%s)" % (cell(p, 100), "/".join(c)) for p, c in ch] + ["created %s" % cell(p, 100) for p in cr] + ["removed %s" % cell(p, 100) for p in rm]
    verdict(V_INV_FILES, not said, few(said) if said else "%d paths looked at (these six trees only, not /tmp, /run or the rest of the machine), %d of them changed by themselves in the control and left out" % (len(m1), len(noisy_files)))

if u1 is not None and u2 is not None:
    said = []
    for u in sorted(u1.keys() | u2.keys()):
        # a unit that a timer starts may run in the window by itself (systemd-tmpfiles-clean does, fifteen minutes after the boot)
        if u in noisy_units or u in TIMER_UNITS:
            continue
        a, b = u1.get(u), u2.get(u)
        if a is None or b is None:
            said.append("%s %s" % (u, "appeared" if a is None else "disappeared"))
        elif a != b:
            said.append("%s went from %s/%s (pid %s, invocation %s) to %s/%s (pid %s, invocation %s)" % (u, a[0], a[1], a[2], a[3][:8], b[0], b[1], b[2], b[3][:8]))
    def diag(name, lines, tail=False):
        # what the guest wrote down to tell who started a service (timedate1's callers, its journal): printed only when the verdict fails
        text = [ln.strip() for ln in read(name).splitlines() if ln.strip()]
        if not text:
            return ""
        text = text[-lines:] if tail else text[:lines]
        return " | %s: %s" % (name, " / ".join(text)[:700])
    verdict(V_INV_UNITS, not said, (few(said) + diag("timedate-monitor.txt", 14) + diag("timedated-journal.txt", 6, tail=True)) if said
            else "%d units looked at, %d changed by themselves in the control and left out" % (len(u1), len(noisy_units)))

if p1 is not None and p2 is not None:
    said = ["%s %s" % ("stopped listening" if x in p1 else "started listening", x) for x in sorted((p1 ^ p2) - noisy_ports)]
    verdict(V_INV_PORTS, not said, few(said) if said else "%d listening sockets, the same before and after" % len(p1))

lock = get("ready-inv-lock")
LOCK_DURING, LOCK_AFTER = read("lock-probe-during").strip(), read("lock-probe-after").strip()
if LOCK_DURING != "held":
    invalid("the guest could not hold dpkg's lock for the call (lock-probe-during says %r): the call under the lock proves nothing" % LOCK_DURING)
if lock is not None and LOCK_DURING == "held":
    verdict(V_LOCK_FAST, lock["kind"] == "" and lock["http"] == 200 and lock["secs"] <= ALLOW,
            "HTTP %d%s in %.1f s" % (lock["http"], " (%s)" % lock["kind"] if lock["kind"] else "", lock["secs"]))
    if lock["kind"] == "":
        k = check_of(lock, "busy")
        verdict(V_LOCK_BUSY, k is not None and k.get("severity") == "refuse", "busy is %s" % (k.get("severity") if k else "not in the answer"))
    verdict(V_LOCK_KEPT, LOCK_AFTER == "held", "the lock was %s after the call" % (LOCK_AFTER or "not looked at"))

P("## What the invariance window looks at")
P("")
P("The names, sizes, times, mode, owner and content of every file of %s; every service unit with its state, main pid and invocation id; the listening sockets. "
  "It does not look at /tmp, /run, /var/tmp, /home, /root, /proc or /sys, nor at anything else: what a tool writes there (`dkms status` may create and remove a file in $TMPDIR) is not seen by this proof." % ", ".join(TREES))
P("")
P("## Which calls the invariance window covers")
P("")
P("- inside the window (compared before and after, for the six trees, the units and the listening sockets): %s" % ", ".join(WINDOW_CALLS))
P("- not inside the window: ctl-core-1 and ctl-core-2 (the control's own calls: what changed during them is what changes by itself), ready-base (the answer of the unchanged box "
  "that the steps are compared with, made after the window), and every call of the dirty matrix, step-<id>-dirty and step-<id>-clean")
P("")
P("The first call of the route (ready-route) is made inside the invariance window, after its first snapshot, so that what only the very first call writes is seen (a cache or a state "
  "file made lazily, a service it starts, a port it opens).")
P("")
P("Of the calls that are not inside the window, no more than this is looked at: the dirty calls of the steps %s are compared for dpkg's status, apt's lists and apt's caches (hashed "
  "or listed before and after the call), and the guest looks at docker.service after the call of %s. A file of /etc, /var/lib/casaos or anywhere else written, a service started "
  "or a port opened by any other call outside the window is not seen by this proof." % (", ".join("`%s`" % s.id for s in STEPS if s.watch), ", ".join("`%s`" % s.id for s in STEPS if s.after)))
P("")
P("## What changed by itself (the control)")
P("")
P("Two snapshots a few seconds apart with two calls of `GET /v1/sys/version/current` (which answers the bare version, as text) between them. What differs there is left out of the verdicts above, and named here.")
P("")
P("- files: %s" % (few(sorted(noisy_files)) if noisy_files else "none"))
P("- units: %s" % (few(sorted(noisy_units)) if noisy_units else "none"))
P("- listening sockets: %s" % (few(sorted(noisy_ports)) if noisy_ports else "none"))
P("- units a timer starts (their invocation ids are not held against the calls): %d" % len(TIMER_UNITS))
P("")


# ---- the clean box -----------------------------------------------------------------------------------------------------------------

GOOD = {n: c for n, c in ANSWERS.items() if c["data"] is not None}   # the answers that are an envelope with a data object: the rest is the shape verdict's
clean = [GOOD.get(n) for n in CLEAN_CALLS]
base = GOOD.get("ready-base")
CLEAN_TEXT = base["data"].get("text") if base is not None and isinstance(base["data"].get("text"), str) else ""
def busy_unit(c):
    """the unit that the `busy` of an answer names, when it refuses because a unit of the package manager runs (apt-daily); None for any other busy, which is the
    core saying that dpkg's lock is held, or that it could not ask"""
    k = check_of(c, "busy")
    d = k.get("data") if k is not None and isinstance(k.get("data"), dict) else {}
    if k is None or k.get("severity") != "refuse" or d.get("source") != "unit":
        return None
    return d.get("unit") if isinstance(d.get("unit"), str) else "a unit"


if all(clean):
    # a clean box that a package manager is working on is the box of the harness, and not the feature: the verdicts that need a quiet box are not made
    not_quiet = sorted({busy_unit(c) for c in clean if busy_unit(c)})
    if not_quiet:
        invalid("the clean box was not quiet: `busy` names %s as running when the clean calls were made (a package manager was at work on the box, and the guest did not wait for it): "
                "the verdicts of the clean box are not made, and this says nothing against the feature" % few(not_quiet))
    else:
        refused = sorted({k for c in clean for k, v in sev_map(c).items() if v == "refuse"})
        verdict(V_CLEAN_NO_REFUSE, not refused, "refuse: " + few(refused) if refused else "no refuse in %d answers" % len(clean))
        verdict(V_CLEAN_STATE, all(c["data"].get("state") in ("ready", "attention") for c in clean), "state %s" % "/".join(sorted({cell(c["data"].get("state")) for c in clean})))
        maps = [sev_map(c) for c in clean]
        verdict(V_CLEAN_AGREE, all(m == maps[0] for m in maps), "same %d codes" % len(maps[0]) if all(m == maps[0] for m in maps) else
                "they differ in: " + few(sorted({k for m in maps for k in m if any(o.get(k) != m.get(k) for o in maps)})))
    third = check_of(base, "sources_third_party")
    verdict(V_CLEAN_DOCKER, third is not None and third.get("severity") == "warn" and "docker" in json.dumps(third).lower(),
            "sources_third_party is %s%s" % (third.get("severity") if third else "not in the answer", "" if not third or "docker" in json.dumps(third).lower() else ", and does not name Docker's source"))
    snap = check_of(base, "snapshot_reminder")
    verdict(V_CLEAN_SNAPSHOT, snap is not None and snap.get("severity") == "info", "snapshot_reminder is %s" % (snap.get("severity") if snap else "not in the answer"))
    # the guest has smartctl (install.sh puts smartmontools on every box), so a core that says it has none does not find it. A box that has none is not the box the proof is for
    if "smartctl" not in FACTS:
        invalid("box-facts says nothing of smartctl: the guest did not say whether the box has it, and what the clean box says of the health of its disk proves nothing")
    elif FACTS["smartctl"] in ("", "none"):
        invalid("the guest has no smartctl: the installer puts smartmontools on every box, so this is not the box the proof is for, and what the clean box says of the health of its disk proves nothing")
    else:
        smart = check_of(base, "smart_failed")
        why = (smart.get("data") or {}).get("why") if smart is not None and isinstance(smart.get("data"), dict) else None
        verdict(V_CLEAN_SMART, smart is not None and smart.get("severity") in ("info", "ok") and why != "no_smartctl",
                "smart_failed is %s%s" % (smart.get("severity") if smart else "not in the answer", ", why %s" % cell(why, 30) if why else ""))

if base is not None:
    P("## The clean box, check by check (the first real-guest data of the report)")
    P("")
    counts_ = base["data"].get("counts") if isinstance(base["data"].get("counts"), dict) else {}
    P("State **%s**, %s, answered in %.1f s. Every warn and info is a fact, not a verdict.\n" % (cell(base["data"].get("state")), ", ".join("%s %s" % (cell(k, 10), cell(v, 10)) for k, v in counts_.items()), base["secs"]))
    P("| code | severity | title |")
    P("|---|---|---|")
    order = {s: i for i, s in enumerate(SEVERITIES)}
    for k in sorted(checks_of(base), key=lambda k: (order.get(k.get("severity"), 9), str(k.get("code")))):
        unknown = " (could not find out)" if isinstance(k.get("data"), dict) and k["data"].get("unknown") is True else ""
        P("| %s | %s | %s%s |" % (cell(k.get("code"), 40), cell(k.get("severity"), 10), cell(k.get("title"), 110), unknown))
    P("")
    added = sorted(c for c in sev_map(base) if c not in CANONICAL)
    P("Codes the core added to the spec's table: %s." % (few(added) if added else "none"))
    P("")
    P("Text of the clean answer (`text`, %d bytes):" % len(CLEAN_TEXT.encode("utf-8")))
    P("")
    P("```")
    P(CLEAN_TEXT.replace("```", "'''")[:TEXT_MAX].rstrip())
    P("```")
    P("")

P("## How long each call took")
P("")
P("| call | HTTP | seconds |")
P("|---|---|---|")
for n, c in CALLS.items():
    if c and n.startswith(("ready-", "sysos-")):
        P("| %s | %d | %.1f |" % (n, c["http"], c["secs"]))
P("")


# ---- the dirty matrix ---------------------------------------------------------------------------------------------------------------

def allowed(step, code):
    return code == step.code or any(fnmatch.fnmatch(code, p) for p in step.extras)


P("## The dirty matrix")
P("")
P("| step | what it does | dirty | after the undo |")
P("|---|---|---|---|")
exercised = set()
tainted_by = ""    # the first step whose undo failed: what comes after it was made on a box that is not the clean one, and is not held against the feature
for s in STEPS:
    skip = read("step-%s.skipped" % s.id).strip()
    if skip:
        invalid("step %s: the guest skipped it (%s), and it exists on this system" % (s.id, cell(skip, 100)))
        continue
    dirty_rc, undo_rc, undone = read("step-%s.dirty-rc" % s.id).strip(), read("step-%s.undo-rc" % s.id).strip(), read("step-%s.undone" % s.id).strip()
    if not dirty_rc or not undo_rc or not undone:
        invalid("step %s: it did not run to its end (dirty-rc, undo-rc or undone is missing)" % s.id)
        continue
    after_state, after_rc = read("step-%s.after" % s.id).strip(), read("step-%s.after-rc" % s.id).strip()
    if s.after and (after_rc != "0" or not after_state):
        invalid("step %s: the guest did not say what it saw once the call was over (step-%s.after is missing or empty, or its command failed: exit %s)" % (s.id, s.id, after_rc or "none"))
        continue
    trusted = True
    if dirty_rc != "0":
        invalid("step %s: the box could not be dirtied (exit %s): %s" % (s.id, dirty_rc, cell(" ".join(read("step-%s.dirty.log" % s.id).split())[-160:], 160)))
        trusted = False
    undo_failed = undo_rc != "0" or undone != "yes"
    was_tainted = tainted_by
    if undo_failed:
        invalid("step %s: the undo failed (exit %s, box back: %s): the leg is invalid, and the later steps start from a box that is not the clean one" % (s.id, undo_rc, undone))
        tainted_by = tainted_by or s.id
    dirty, after = GOOD.get("step-%s-dirty" % s.id), GOOD.get("step-%s-clean" % s.id)
    if was_tainted:
        P("| %s | %s | not judged: the undo of step %s failed | |" % (s.id, cell(s.what, 70), was_tainted))
        continue
    if not trusted or base is None:
        P("| %s | %s | not run | |" % (s.id, cell(s.what, 70)))
        continue
    if dirty is None or after is None:
        P("| %s | %s | no answer | |" % (s.id, cell(s.what, 70)))
        continue
    exercised.add(s.code)
    base_sev, now_sev = sev_map(base), sev_map(dirty)
    now_bad = bad_codes(dirty)
    sev = now_sev.get(s.code)
    if s.mode == "quiet":
        verdict(F_QUIET % (s.id, s.code), s.code not in now_bad, "%s is %s" % (s.code, sev or "not in the answer"))
    elif s.mode == "names":
        k, k0 = check_of(dirty, s.code), check_of(base, s.code)
        had = k0 is not None and s.needle in json.dumps(k0)
        ok = k is not None and k.get("severity") == s.sev and s.needle in json.dumps(k)
        verdict(F_FIRES % (s.id, s.code, "%s and name %s" % (s.sev, s.needle)), None if had else ok,
                "%s is %s%s" % (s.code, sev or "not in the answer", "" if k is None or s.needle in json.dumps(k) else ", and does not name %s" % s.needle))
        if had:
            invalid("step %s: the clean answer already names %s in %s: the step cannot tell it from the dirt" % (s.id, s.needle, s.code))
    else:
        k = check_of(dirty, s.code)
        if non_ok(base).get(s.code) == s.sev:
            not_testable(F_FIRES % (s.id, s.code, s.sev), "the clean box already has `%s` as %s: the step cannot tell it from the dirt" % (s.code, s.sev))
        else:
            ok = sev == s.sev and (not s.needle or (k is not None and s.needle in json.dumps(k)))
            verdict(F_FIRES % (s.id, s.code, s.sev), ok, "%s is %s%s" % (s.code, sev or "not in the answer", "" if not s.needle or k is None or s.needle in json.dumps(k) else ", and does not name %s" % s.needle))
    for code in s.forbid:
        verdict(F_FORBID % (s.id, code), code not in now_bad, "%s is %s" % (code, sev_map(dirty).get(code, "not in the answer")))
    for leg_, code, want, key, value, clean_sev in s.expect:
        if leg_ not in ("*", leg):
            continue
        name = F_EXPECT % (s.id, code, want, " with %s %s" % (key, json.dumps(value)) if key else "")
        if sev_map(base).get(code) != clean_sev:
            said = sev_map(base).get(code, "absent")
            if leg_ == "*":
                not_testable(name, "the clean box says `%s` is %s, not %s: the dirt cannot be told from what is there" % (code, said, clean_sev))
            else:
                # a row of one leg is that leg's premise: the d11 guest is built to have a newer Docker on offer (28.0.4 installed, the repository's 29 as the candidate), and a box that does
                # not say so did not get what the step needs. That is a harness that did not do its job, never a neutral result that leaves the job green
                k0 = check_of(base, code)
                d0 = k0.get("data") if k0 is not None and isinstance(k0.get("data"), dict) else {}
                what = ", ".join("%s %s" % (w, cell(d0[w], 60)) for w in ("installed", "candidate") if isinstance(d0.get(w), str))
                seen = "the guest saw: %s" % cell(FACTS["docker_offer"], 120) if code == "docker_update_pending" and FACTS.get("docker_offer") else ""
                invalid("step %s: the %s leg is built so that its clean box says `%s` is %s, and it says `%s` is %s%s: the premise of the leg is missing (the box did not get what the step "
                        "needs%s), and the ruling it proves was not exercised. It says nothing against the feature"
                        % (s.id, leg_, code, clean_sev, code, said, " (%s)" % what if what else "", "; " + seen if seen else ""))
            continue
        k = check_of(dirty, code)
        d = k.get("data") if k is not None and isinstance(k.get("data"), dict) else {}
        has = key in d and d[key] == value and isinstance(d[key], bool) == isinstance(value, bool)
        verdict(name, k is not None and k.get("severity") == want and (not key or has),
                "%s is %s%s" % (code, k.get("severity") if k is not None else "not in the answer", (" and %s is %s" % (key, cell(json.dumps(d.get(key))))) if key and k is not None else ""))
    if s.after:
        verdict(F_AFTER % (s.id, s.after_what), after_state == s.after, "after the call the guest saw %s, wanted %s" % (cell(after_state, 30), s.after))
    if s.watch:
        # what the clean calls of the invariance window cannot show: a write that only a call on a dirty box makes. What changed by itself in the control is left out
        before_rows, after_rows = apt_rows(s.id, "before"), apt_rows(s.id, "after")
        if before_rows is not None and after_rows is not None:
            def alone(p):
                return p not in noisy_files
            said = (["changed %s (%s, then %s)" % (cell(p, 100), before_rows[p][:19], after_rows[p][:19]) for p in sorted(before_rows.keys() & after_rows.keys()) if before_rows[p] != after_rows[p] and alone(p)]
                    + ["created %s" % cell(p, 100) for p in sorted(after_rows.keys() - before_rows.keys()) if alone(p)]
                    + ["removed %s" % cell(p, 100) for p in sorted(before_rows.keys() - after_rows.keys()) if alone(p)])
            verdict(F_WROTE % s.id, not said, few(said) if said else "%d files looked at, the same before and after the call" % len(before_rows))
    # a code is worse when its severity went down the ladder refuse, warn, anything else: a warn of the clean box that is a refuse with the dirt is one
    worse = sorted(c for c, v in now_sev.items() if RANK.get(v, 2) < RANK.get(base_sev.get(c), 2))
    extra = [c for c in worse if not allowed(s, c)]
    brought = [c for c in worse if c != s.code]
    verdict(F_ALONE % s.id, not extra, "also fires: " + few(extra) if extra else "%s%s" % (s.code, " and what the dirt brings with it, which is allowed: " + few(brought) if brought else " only"))
    back_ok = non_ok(after) == non_ok(base) and after["data"].get("state") == base["data"].get("state") and \
        (s.mode != "names" or s.needle not in json.dumps(check_of(after, s.code) or {}))
    if back_ok:
        det = "the same %d codes, state %s" % (len(non_ok(base)), base["data"].get("state"))
    else:
        left = sorted(c for c in set(non_ok(after)) | set(non_ok(base)) if non_ok(after).get(c) != non_ok(base).get(c))
        det = "differs in %s, state %s (clean: %s)" % (few(left) or s.code, cell(after["data"].get("state")), cell(base["data"].get("state")))
    if not undo_failed:
        verdict(F_UNDONE % s.id, back_ok, det)
    named = " (names %s: %s)" % (s.see, "yes" if s.see in json.dumps(check_of(dirty, s.code) or {}) else "no") if s.see else ""
    P("| %s | %s | %s %s%s | %s |" % (s.id, cell(s.what, 70), s.code, cell(sev or "absent", 10), named, "the undo failed" if undo_failed else "clean again" if back_ok else "NOT clean"))
P("")
for what, why in NOT_TESTABLE:
    not_testable(what, why)

P("## What this proof did not exercise")
P("")
rest = sorted(set(CANONICAL) - exercised - {"uptime_long", "container", "virt_xen", "unproven_arch"})
P("Codes of the spec's table that no step of this proof makes fire (the core's own tests are their proof; the clean box shows whether they stay quiet): %s." % few(rest, 80))
P("")

finish()
