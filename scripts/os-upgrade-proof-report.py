#!/usr/bin/env python3
"""Turn what os-upgrade-proof.sh wrote into verdicts, or say the run is invalid.

    os-upgrade-proof-report.py <out dir> <core|faults>

Prints a markdown report (and writes it to <out dir>/report.md).

Exit status: 0 only when no verdict is a FAIL and the run is valid. 1 for a FAIL (the engine did not do what it says) and 1 for an INVALID RUN (the harness or the guest broke: the run
stopped early, a mirror or the network of the runner failed, the guest did not boot, an undo failed, a snapshot of the guest is missing, the release under test has no engine). An invalid run
proves nothing and is never green. NOT TESTABLE is neutral and visible: a case whose precondition this guest could not reach, with the reason; it is never a pass.

What the runner did (os-upgrade-proof.sh), on a fresh Debian 11 cloud image with ReCasaOS, Docker 28.0.4 and four witness containers, through the gateway with the token of a login of the
box's own, and with the guest asked for what only it can say (snapshots of the machine, the busy flag, the boot id):

  leg core
    C1  the flag file absent: a CONTROL window (two snapshots), then the status, a start and a refresh: 409 beta_off twice, the status idle with beta_enabled false, no folder of the engine,
        no unit of the engine ever started (PID 1's journal), no name of the file system that changed and that the control did not show changing by itself
    C2  POST beta: the file is there, the status says so
    C3  the refresh (and the restart it asks for, when it does): refreshing, then refreshed; the sources and the timers of apt as they were, no package removed, the busy flag there while it
        works and gone when it is done
    C4  the plan, then a rehearsal: rehearsing, rehearsed (the sources of Debian 12 in place, the timers stopped, held for at most 30 minutes), the discard, rehearsal_done, the close: the machine as before
    C5  the refusals: hostname_mismatch, acks_missing, a start while dpkg's lock is held, run_open, and the page's restart while the unit works (os_upgrade_open, and the guest does not restart)
    C6  the hop started and called off in block 3 or 4: the machine put back, the run failed with the class abort, the close
    C7  the hop: the blocks in their order, awaiting_reboot with its checks green, the restart, resuming, verifying, the verdict, the close: Debian 12, a newer kernel, the services, the witnesses
  leg faults (each from a snapshot of the whole guest taken after C4)
    F1  the unit killed in block 3 or 4: interrupted, the core unwinds by itself, failed, the machine as before
    F2  the unit killed after the point of no return, with dpkg at work: interrupted, no repair by itself, resume, awaiting_reboot, the restart, succeeded
    F3  the network cut after it: stopped_at_waypoint of the class network, the network back, resume, awaiting_reboot, the restart, succeeded
    F4  a power cut (the QEMU monitor's reset) in block 6, with dpkg at work: interrupted (power), half upgraded, resume, and then what nobody has seen: printed
        (each fault starts with the clock of the restored guest set from the runner's; a fault that was not delivered in the middle of dpkg is NOT TESTABLE)

A verdict fails with what was expected and what was recorded. What a verdict cannot be silent about: a case that did not run says so (NOT TESTABLE, with the reason the run stopped), a verdict
that depends on evidence that is missing is no verdict and makes the run INVALID, and any exception of this script, on evidence it did not expect, ends as an INVALID RUN that names it.
"""
import json
import math
import os
import re
import sys
from datetime import datetime, timezone

if len(sys.argv) != 3:
    print("usage: os-upgrade-proof-report.py <out dir> <core|faults>", file=sys.stderr)
    sys.exit(2)
out_dir, leg = sys.argv[1], sys.argv[2]

# ---- the contract of the engine (docs/os-upgrade-beta.md, pkg/osrun/vocab.go) -----------------------------------------------------------------

STATES = ("idle", "refreshing", "reboot_first", "refreshed", "rehearsing", "rehearsed", "rehearsal_done", "running", "unwinding", "recovering", "awaiting_reboot", "rebooting",
          "reboot_stalled", "resuming", "verifying", "succeeded", "succeeded_with_findings", "failed", "interrupted", "stopped_at_waypoint", "needs_attention", "manual", "manual_done")
CLASSES = ("busy", "network", "apt_failed", "dpkg_broken", "unknown", "disk", "plan", "guard", "converge", "barrier_kernel", "barrier_initrd", "barrier_grub", "barrier_sshd",
           "barrier_resolver", "barrier_other", "identity", "tools", "unit", "integrity", "abort")
HOP_BLOCKS = ("verify", "timers", "snapshot", "sources", "download", "minimal", "full", "prereboot", "after")
REFRESH_BLOCKS = ("rcheck", "rsources", "rupgrade", "rdone")
EARLY_BLOCKS = ("verify", "timers", "snapshot", "sources", "download")      # blocks 0 to 4 of the docs: before the point of no return
WORK_STATES = ("refreshing", "rehearsing", "running", "unwinding", "recovering", "verifying", "resuming")
TERMINAL_BAD = ("failed", "needs_attention", "stopped_at_waypoint", "interrupted", "manual", "manual_done")
FINDING_SEVERITIES = ("error", "warn", "info")
TIMERS = ("apt-daily.timer", "apt-daily-upgrade.timer", "unattended-upgrades.service")
WITNESSES = ("p-always", "p-db", "p-unless-stopped", "p-web")
REHEARSAL_TTL = 30 * 60               # RehearsalTTL of pkg/osrun/timing.go
FLAG_WITHIN = 120                     # the core lets go of the busy flag at its next look, every 30 s (SettleEvery): the proof waits this long
OBSERVE_MIN = 170                     # a stopped hop is looked at for this long (more than the 2 minutes of backoff of a repair that is allowed) before it is said that nothing repairs it by itself
CLOCK_SLACK = 5.0
ROW_FIELDS = 19
BK_GLOB = "casaos-os-upgrade-"

CASES_CORE = (("C1", "flag off"), ("C2", "switch on"), ("C3", "refresh"), ("C4", "plan and rehearsal"), ("C5", "refusals"), ("C6", "call-off before the point of no return"), ("C7", "the hop"))
CASES_FAULTS = (("F1", "the unit killed before the point of no return"), ("F2", "the unit killed after the point of no return"), ("F3", "the network cut after the point of no return"),
                ("F4", "a power cut during block 6"))

lines = []
P = lines.append
verdicts = []      # (status, name, detail)
problems = []      # the run cannot be trusted: reasons
numbers = []       # (label, text)
notes = []         # things worth reading that are no verdict


def read(name, default=""):
    try:
        with open(os.path.join(out_dir, name), encoding="utf-8", errors="replace", newline="") as f:
            return f.read().replace("\r\n", "\n")
    except OSError:
        return default


def exists(name):
    return os.path.exists(os.path.join(out_dir, name))


def verdict(name, ok, detail=""):
    verdicts.append(("PASS" if ok else "FAIL", name, detail))


def not_testable(name, why):
    verdicts.append(("NOT TESTABLE", name, why))


def invalid(reason):
    if reason not in problems:
        problems.append(reason)


def number(label, text):
    numbers.append((label, text))


URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\]]+")


def mask_text(s):
    def one(m):
        u = m.group(0)
        scheme, _, rest = u.partition("://")
        if "?" in rest:
            rest = rest[:rest.index("?")] + "?***"
        authority, slash, tail = rest.partition("/")
        if "@" in authority:
            authority = "***" + authority[authority.rindex("@"):]
        return scheme + "://" + authority + slash + tail
    return URL_IN_TEXT.sub(one, s)


def cell(x, n=160):
    """a value of the box's own answer, for a sentence: URLs masked (this text goes to the public summary of a run), printable ASCII, cut short"""
    return re.sub(r"[^\x20-\x7e]", "?", mask_text(str(x))).replace("|", "/")[:n]


def few(items, n=8):
    items = list(items)
    return ", ".join(cell(i, 100) for i in items[:n]) + (" and %d more" % (len(items) - n) if len(items) > n else "")


def secs(x):
    return "%d s" % round(x) if x < 120 else "%d min %d s" % (x // 60, round(x) % 60)


def mib(n):
    return "%.0f MiB" % (n / (1 << 20))


def finish():
    fails = [v for v in verdicts if v[0] == "FAIL"]
    head = "FAIL and INVALID RUN" if problems and fails else "INVALID RUN" if problems else "FAIL" if fails else "PASS"
    text = ["# OS upgrade proof, %s leg: %s" % (leg, head), ""]
    if problems:
        text += ["Nothing below that depends on the missing or untrustworthy evidence can be believed:", ""]
        text += ["- INVALID: " + cell(p, 600) for p in problems] + [""]
    text += lines
    text += ["", "## Verdicts", ""]
    text += ["- **%s** %s%s" % (s, n, (": " + d) if d else "") for s, n, d in verdicts]
    if notes:
        text += ["", "## To read", ""] + ["- " + n for n in notes]
    if numbers:
        text += ["", "## The first real-guest numbers of the engine", ""] + ["- %s: %s" % (a, b) for a, b in numbers]
    skipped = [v[1] for v in verdicts if v[0] == "NOT TESTABLE"]
    passes = sum(1 for v in verdicts if v[0] == "PASS")
    text += ["", "%d of %d verdicts pass%s%s." % (passes, passes + len(fails), "; failing: " + "; ".join(v[1] for v in fails) if fails else "",
                                                "; %d not testable on this guest: %s" % (len(skipped), "; ".join(skipped)) if skipped else "")]
    out = "\n".join(text) + "\n"
    try:
        with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
            f.write(out)
    except OSError:
        pass
    print(out)
    sys.exit(0 if not problems and not fails else 1)


# ---- the evidence -----------------------------------------------------------------------------------------------------------------------

class Call:
    """a request of the runner and the answer: <name>.req, .json, .code, .t0, .t1, .curl, .err"""

    def __init__(self, name):
        self.name = name
        self.ran = exists(name + ".code") and exists(name + ".curl")
        code, rc = read(name + ".code").strip(), read(name + ".curl").strip()
        self.code = int(code) if code.isdigit() else 0
        self.rc = int(rc) if rc.isdigit() else -1
        self.t0, self.t1 = fnum(read(name + ".t0")), fnum(read(name + ".t1"))
        self.err = read(name + ".err").strip()
        try:
            self.body = json.loads(read(name + ".json")) if read(name + ".json").strip() else None
        except ValueError:
            self.body = None
        self.data = self.body.get("data") if isinstance(self.body, dict) else None
        self.req = read(name + ".req").split("\n", 1)[0].strip()

    @property
    def answered(self):
        return self.ran and self.rc == 0 and self.code != 0

    @property
    def error_code(self):
        return self.data.get("error_code") if isinstance(self.data, dict) else None

    @property
    def detail(self):
        d = self.data.get("data") if isinstance(self.data, dict) else None
        return d if isinstance(d, dict) else {}

    def field(self, key, default=None):
        return self.data.get(key, default) if isinstance(self.data, dict) else default

    def shown(self):
        if not self.ran:
            return "no call"
        if not self.answered:
            return "no answer (curl %s%s)" % (self.rc, ": " + cell(self.err, 100) if self.err else "")
        extra = self.error_code or self.field("state")
        return "HTTP %d%s" % (self.code, " " + cell(extra, 60) if extra else "")


def fnum(text):
    try:
        v = float(text.strip())
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def call(name, why=""):
    """a call that the judgement needs: None, with the run made invalid, when it did not run or got no answer"""
    c = Call(name)
    if not c.ran:
        invalid("the call %s%s did not run: no evidence" % (name, " (" + why + ")" if why else ""))
        return None
    if not c.answered:
        invalid("the call %s%s got no answer: curl %s %s" % (name, " (" + why + ")" if why else "", c.rc, cell(c.err, 120)))
        return None
    return c


class Row:
    """a poll of the status: t0, t1, HTTP, curl, state, reason, cause, block index, block id, pnr, half_upgraded, busy, attempt, failure class, hint, flag, run id, beta_enabled, actions"""

    def __init__(self, cells):
        (self.t0, self.t1, self.http, self.curl, self.state, self.reason, self.cause, self.bidx, self.bid, self.pnr, self.half, self.busy, self.attempt, self.fclass, self.hint,
         self.flag, self.run, self.beta, self.actions) = cells
        self.t0, self.t1 = fnum(self.t0), fnum(self.t1)
        self.acts = [] if self.actions == "-" else self.actions.split(",")

    def answered(self):
        return self.http.isdigit() and self.http != "000" and self.curl == "0"


def rows(tag):
    """the rows of polls-<tag>.tsv; [] when the file is not there (the caller says so)"""
    out = []
    for ln in read("polls-%s.tsv" % tag).splitlines():
        cells = ln.split("\t")
        if len(cells) != ROW_FIELDS:
            invalid("polls-%s.tsv has a row of %d fields, not %d" % (tag, len(cells), ROW_FIELDS))
            continue
        r = Row(cells)
        if r.t0 is None or r.t1 is None or r.t1 < r.t0:
            invalid("polls-%s.tsv has a row whose times are not times of a call" % tag)
            continue
        out.append(r)
    return out


def states_of(rs):
    """the states the rows went through, one after the other, without repeats, the polls that got no answer left out"""
    out = []
    for r in rs:
        if r.answered() and r.state != "-" and (not out or out[-1] != r.state):
            out.append(r.state)
    return out


def is_subsequence(want, have):
    it = iter(have)
    return all(any(w == h for h in it) for w in want)


def table(path):
    out = []
    for ln in read(path).splitlines():
        out.append(ln.split("\t"))
    return out


class Snap:
    """a snapshot of the guest: snap-<tag>.{facts,services,dpkg,aptfiles,names,units,pid1}"""

    def __init__(self, tag):
        self.tag = tag
        self.f, self.timers = {}, {}
        for ln in read("snap-%s.facts" % tag).splitlines():
            k, _, v = ln.partition(" ")
            if k == "timer":
                parts = v.split(" ")
                if len(parts) == 3:
                    self.timers[parts[0]] = (parts[1], parts[2])
            elif k:
                self.f[k] = v.strip()
        self.services = {}
        for ln in read("snap-%s.services" % tag).splitlines():
            parts = ln.split()
            if len(parts) >= 2:
                self.services[parts[0]] = parts[1]
        self.dpkg = {}
        for ln in read("snap-%s.dpkg" % tag).splitlines():
            parts = ln.split("\t")
            if len(parts) == 3:
                self.dpkg[parts[0]] = (parts[1], parts[2].strip())
        self.apt = {}
        for ln in read("snap-%s.aptfiles" % tag).splitlines():
            parts = ln.split("\t")
            if len(parts) == 2:
                self.apt[parts[0]] = parts[1]
        self.names = set(ln for ln in read("snap-%s.names" % tag).splitlines() if ln)
        # the sections that are empty when all is well say that they were asked ("# asked", "# lines <n>"): an empty section that nothing wrote is no evidence
        units = [ln for ln in read("snap-%s.units" % tag).splitlines() if ln.strip()]
        self.units_asked = "# asked" in units
        self.units = [ln for ln in units if not ln.startswith("# ")]
        pid1 = [ln for ln in read("snap-%s.pid1" % tag).splitlines() if ln.strip()]
        n = next((re.fullmatch(r"# lines (\d+)", ln) for ln in pid1 if ln.startswith("# lines ")), None)
        self.pid1_lines = int(n.group(1)) if n else 0
        self.pid1 = [ln for ln in pid1 if not ln.startswith("# ")]
        # the kept runs: (run id, the folder of its backup or -, whether it is still there, where it was moved: bk-done, bk-aborted or -)
        self.runs = [tuple(ln.split("\t")) for ln in read("snap-%s.runs" % tag).splitlines() if len(ln.split("\t")) == 4]

    def installed(self):
        return {n: v for n, (v, st) in self.dpkg.items() if st.startswith("ii")}

    def sources(self):
        return {p: h for p, h in self.apt.items() if p == "/etc/apt/sources.list" or p.startswith("/etc/apt/sources.list.d/")}

    def num(self, key):
        v = self.f.get(key, "")
        return int(v) if re.fullmatch(r"\d+", v) else None

    def bk_dirs(self):
        return [d for d in self.f.get("bk_dirs", "").split() if d]

    def run_bk(self):
        return [d for d in self.f.get("run_bk", "").split() if d]

    def witnesses(self):
        return sorted(w for w in self.f.get("witnesses", "").split(",") if w)

    def failed_units(self):
        return sorted(u for u in self.f.get("failed_units", "").split() if u)


def snap(tag, why=""):
    if exists("snap-%s.failed" % tag):
        invalid("the snapshot %s%s could not be had: %s" % (tag, " (" + why + ")" if why else "", cell(read("snap-%s.failed" % tag).strip(), 200)))
        return None
    if not exists("snap-%s.facts" % tag):
        invalid("the snapshot %s%s is missing: no evidence" % (tag, " (" + why + ")" if why else ""))
        return None
    s = Snap(tag)
    if not s.f.get("boot_id") or not s.f.get("uname") or not s.dpkg or not s.apt or len(s.timers) != len(TIMERS):
        invalid("the snapshot %s%s is not a whole one (boot id, kernel, packages, apt files or timers are missing)" % (tag, " (" + why + ")" if why else ""))
        return None
    empty = [w for w, ok in (("the names of the file system", bool(s.names)), ("the units of the engine (systemd was not asked)", s.units_asked), ("the journal of PID 1 (it was not read)", s.pid1_lines > 0)) if not ok]
    if empty:
        invalid("the snapshot %s%s is not a whole one: %s missing, and a section that is empty because it was not asked proves nothing" % (tag, " (" + why + ")" if why else "", " and ".join(empty)))
        return None
    return s


def natural(v):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", v) if p != ""]


def newer(a, b):
    """version a is later than b, by the order a human reads kernel releases in (5.10.0-35-cloud-amd64 < 6.1.0-40-cloud-amd64)"""
    ka, kb = natural(a), natural(b)
    try:
        return ka > kb
    except TypeError:
        return [str(x) for x in ka] > [str(x) for x in kb]


def dpkg_diff(a, b):
    """what changed between two snapshots' packages: (removed, added, changed): removed is installed in a and not in b"""
    ia, ib = a.installed(), b.installed()
    removed = sorted(n for n in ia if n not in ib)
    added = sorted(n for n in ib if n not in ia)
    changed = sorted(n for n in ia if n in ib and ia[n] != ib[n])
    return removed, added, changed


def apt_diff(a, b):
    names = sorted(set(a.apt) | set(b.apt))
    return [n for n in names if a.apt.get(n) != b.apt.get(n)]


class Case:
    """the checks of one case: a verdict that fails lists each check that did, with what was expected and what was recorded"""

    def __init__(self, name):
        self.name, self.fails, self.passed, self.notes = name, [], 0, []

    def check(self, ok, what, expected="", recorded=""):
        if ok:
            self.passed += 1
        else:
            self.fails.append("%s (expected %s; recorded %s)" % (what, cell(expected, 200), cell(recorded, 300)) if expected or recorded else what)
        return ok

    def note(self, text):
        self.notes.append(text)

    def done(self):
        extra = (" [" + "; ".join(self.notes) + "]") if self.notes else ""
        if self.fails:
            verdicts.append(("FAIL", self.name, "; ".join(self.fails) + extra))
        else:
            verdicts.append(("PASS", self.name, "%d checks%s" % (self.passed, extra)))


# ---- the run -----------------------------------------------------------------------------------------------------------------------------

def timeline():
    marks, first = {}, None
    for ln in read("timeline.tsv").splitlines():
        r = ln.split("\t")
        if len(r) == 3 and r[0] == "#":
            t = fnum(r[1])
            if t is not None:
                marks.setdefault(r[2], t)
                first = t if first is None else min(first, t)
    return marks, first


def timestamp(s):
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)Z?$", s.strip())
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1) + "+00:00").timestamp()
    except ValueError:
        return None


def osrelease(name):
    out = {}
    for line in read(name).splitlines():
        m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip().strip("\"'")
    return out


try:
    if leg not in ("core", "faults"):
        invalid("the leg is core or faults, not %r" % leg)
        finish()
    CASES = CASES_CORE if leg == "core" else CASES_FAULTS
    marks, first_mark = timeline()
    aborted = " ".join(read("aborted").split())
    guest_aborted = " ".join(read("guest/aborted").split())

    if guest_aborted:
        invalid("the guest's preparation stopped: " + guest_aborted[:300])
    elif not exists("guest/prepared") and not read("finished").strip():
        invalid("the guest's preparation left no `prepared` file: it did not reach its end")
    if aborted:
        # first: it is why the rest of the evidence is missing
        invalid("the run stopped early: " + aborted[:400])
    for ln in read("invalid.txt").splitlines():
        r = ln.split("\t", 1)
        invalid("a step of the harness failed or could not be undone (%s): %s" % (r[0], r[1] if len(r) > 1 else ""))
    osr = osrelease("guest/os-release")
    if not osr.get("ID"):
        invalid("guest/os-release is missing or empty: the guest did not say what system it is")
    elif (osr.get("ID"), osr.get("VERSION_ID")) != ("debian", "11"):
        invalid("the guest is %s %s, the proof needs Debian 11" % (osr.get("ID"), osr.get("VERSION_ID")))
    for ln in read("guest/timers-on").splitlines():
        w = ln.split()
        if w[:1] in (["apt-daily.timer"], ["apt-daily-upgrade.timer"]) and w[1:2] != ["active"]:
            invalid("the timers of apt were not on when the guest was prepared (%s): what the cases hold about them proves nothing" % cell(ln, 80))
    if read("leg").strip() != leg:
        invalid("the runner says it ran leg %r, the report is asked about %r" % (read("leg").strip(), leg))
    finished = timestamp(read("finished"))
    started = timestamp(read("started"))
    if not read("finished").strip() and not aborted:
        invalid("the runner did not reach the end of its run (no `finished` file) and did not say why")
    if not marks:
        invalid("timeline.tsv is missing or empty: the run left no marker")
    elif finished is not None and first_mark is not None and not (first_mark - 86400 <= finished + CLOCK_SLACK):
        invalid("the markers and the end of the run are not of the same day")
    if exists("probe-status.code") and Call("probe-status").code == 404:
        invalid("the release under test has no /v1/sys/os/upgrade/status: it does not carry the engine (v0.5.29 or later is needed)")
    elif exists("probe-status.code"):
        pc = Call("probe-status")
        if not pc.answered:
            invalid("the first call of the engine's routes got no answer: curl %s %s" % (pc.rc, cell(pc.err, 120)))
        elif pc.code != 200 or not isinstance(pc.data, dict) or pc.data.get("state") not in STATES:
            invalid("the status of the engine, asked first, is not an answer of it: HTTP %d %s" % (pc.code, cell(json.dumps(pc.data), 120)))
    elif not aborted:
        invalid("the first call of the engine's routes (probe-status) is missing")
    tag = read("tag").strip()
    if tag:
        P("Installer release under test: `%s`." % cell(tag, 40))

    # ---- shared judgements -----------------------------------------------------------------------------------------------------------

    def gate(cid, title):
        """whether the case ran to its end and is to be judged; a case that did not run says so, with the reason the run stopped"""
        if "case_%s_done" % cid in marks:
            return True
        if "case_%s_start" % cid in marks:
            invalid("%s (%s) began and did not end: the run stopped in it" % (cid, title))
            return False
        not_testable("%s %s" % (cid, title), "did not run: " + (("the run stopped early (%s)" % aborted[:200]) if aborted else "the leg ended before it"))
        return False

    def stopped_for_network(tag_, where):
        """a run that stopped with the class network, in a case that does not cut the network: a mirror or the runner's network failed, and that is the harness's"""
        rs = rows(tag_)
        for r in rs:
            if r.answered() and r.state in ("failed", "stopped_at_waypoint", "needs_attention", "interrupted") and r.fclass == "network":
                invalid("%s: a download or an update did not go through (class network): a mirror or the network of the runner failed, not a verdict on the engine" % where)
                return True
        return False

    def seen_states(rs):
        return states_of(rs)

    def last_answered(rs):
        for r in reversed(rs):
            if r.answered():
                return r
        return None

    def timed_out(tag_):
        return exists("%s.timeout" % tag_)

    def reached(c, tag_, wanted, what, allowed=None):
        """the poll of <tag_> ended in a wanted state: a check; the states it went through are all among the allowed ones"""
        rs = rows(tag_)
        if not rs:
            invalid("polls-%s.tsv is missing or empty: no evidence of %s" % (tag_, what))
            return None
        seq = states_of(rs)
        last = last_answered(rs)
        end = last.state if last else "-"
        ok = end in wanted
        why = ""
        if not ok:
            why = "ended in %s%s" % (end, " (class %s)" % last.fclass if last and last.fclass != "-" else "")
            if timed_out(tag_):
                why += ", the time ran out"
        c.check(ok, what, "the state %s" % " or ".join(wanted), why + " after " + (" > ".join(seq) or "nothing"))
        if allowed is not None:
            odd = [s for s in seq if s not in allowed]
            c.check(not odd, "%s: the states it went through are the engine's for this" % what, "only " + ", ".join(allowed), "also " + ", ".join(odd))
        return rs

    def flag_up(c, rs, working, label):
        """the busy flag was there while the run worked (a sample of the guest, in the rows of the polls)"""
        samples = [r.flag for r in rs if r.answered() and r.state in working]
        if samples and all(f == "-" for f in samples):
            invalid("%s: the guest could not be asked for the busy flag during the run" % label)
        else:
            c.check("yes" in samples, "%s: the busy flag is there while the run works" % label, "a sample `yes` among %d" % len(samples), "samples %s" % (few(sorted(set(samples))) or "none"))

    def flag_check(c, rs, working, tag_, label):
        """the busy flag was there while the run worked and is gone within FLAG_WITHIN seconds of the end"""
        flag_up(c, rs, working, label)
        ft = table("flag-%s.tsv" % tag_)
        if not ft:
            invalid("flag-%s.tsv is missing: the flag after the run was not asked for" % tag_)
            return
        last = ft[-1][1] if len(ft[-1]) > 1 else "-"
        if last == "-":
            invalid("%s: the guest could not be asked for the busy flag after the run" % label)
        else:
            c.check(last == "no", "%s: the busy flag is gone when the run is done" % label, "gone within %d s" % FLAG_WITHIN, "still there after %d samples" % len(ft))

    def machine_as_before(c, before, after, strict_apt=True, label=""):
        """the sources and the timers of apt, the packages: as they were"""
        sd = [n for n in sorted(set(before.sources()) | set(after.sources())) if before.sources().get(n) != after.sources().get(n)]
        c.check(not sd, "%sthe sources of apt are as they were" % label, "the same files, byte for byte", "changed: " + few(sd))
        if strict_apt:
            ad = apt_diff(before, after)
            c.check(not ad, "%sthe hash of /etc/apt is as it was" % label, "the same files, byte for byte", "changed: " + few(ad))
        c.check(before.timers == after.timers, "%sthe timers of apt are as they were" % label, dict_str(before.timers), dict_str(after.timers))
        c.check(not after.f.get("timers_state_file") == "yes", "%sthe file of the timers' state is gone" % label, "no /var/backups/casaos-os-upgrade-timers.state", "it is there")

    def dict_str(d):
        return ", ".join("%s=%s/%s" % (k, v[0], v[1]) for k, v in sorted(d.items()))

    def run_id_of(rs):
        return next((r.run for r in rs if r.answered() and r.run not in ("-", "")), None)

    def bk_left(c, after, label):
        """no kept run has its backup folder still in /var/backups: each one's is moved into its run folder or is not there"""
        c.check(not after.bk_dirs(), "%sno backup folder is left in /var/backups" % label, "none", few(after.bk_dirs()))
        stay = [r[0] for r in after.runs if r[2] == "yes"]
        c.check(not stay, "%seach kept run's backup folder is moved into its run folder or is not there" % label, "no run with its folder still in /var/backups", "still there for the runs " + few(stay))

    def bk_put_away(c, after, which, label, run_id):
        """the backup folder of the run that was judged was moved into its own run folder (bk-done or bk-aborted), whatever was moved of the runs before it"""
        bk_left(c, after, label)
        mine = [r for r in after.runs if r[0] == run_id]
        c.check(bool(mine) and mine[0][3] == which, "%sthe backup folder of this run was moved into its run folder (%s)" % (label, which), "run %s: %s" % (run_id, which),
                "run %s: %s" % (run_id, mine[0][3]) if mine else "run %s is not among the kept runs (%s)" % (run_id, few(r[0] for r in after.runs) or "none"))

    def request_body(name):
        parts = read(name + ".req").split("\n", 1)
        try:
            body = json.loads(parts[1]) if len(parts) > 1 and parts[1].strip() else {}
        except ValueError:
            body = {}
        return body if isinstance(body, dict) else {}

    # the plan of C4, for the cases that need it
    def plan_state():
        pc = Call("c4-plan-get")
        if not pc.answered:
            return None, "no plan was made"
        d = pc.data if isinstance(pc.data, dict) else {}
        plan = d.get("plan") if isinstance(d.get("plan"), dict) else {}
        if d.get("state") != "done":
            f = d.get("error") if isinstance(d.get("error"), dict) else {}
            return None, "the plan job ended %s%s" % (d.get("state"), " with %s" % f.get("code") if f.get("code") else "")
        if plan.get("verdict") == "blocked":
            return None, "the plan is blocked: " + few("%s" % p.get("code") for p in plan.get("problems") or [] if isinstance(p, dict))
        return plan, ""

    def plan_network_failure():
        """a plan whose update could not reach a mirror: the mirror's, not the engine's"""
        pc = Call("c4-plan-get")
        d = pc.data if pc.answered and isinstance(pc.data, dict) else {}
        err = d.get("error") if isinstance(d.get("error"), dict) else {}
        kind = err.get("data", {}).get("kind") if isinstance(err.get("data"), dict) else None
        return d.get("state") == "failed" and err.get("code") == "sandbox_update_failed" and kind in ("network", "not_found")

    # ---- C1 ----------------------------------------------------------------------------------------------------------------------------

    def judge_c1():
        if not gate("C1", "flag off"):
            return
        c = Case("C1 flag off")
        a, b, before, after = snap("c1-ctl-a"), snap("c1-ctl-b"), snap("c1-before"), snap("c1-after")
        st0, st1 = call("c1-status-before"), call("c1-status-after")
        start, refresh = call("c1-start"), call("c1-refresh")
        if not all((a, b, before, after, st0, st1, start, refresh)):
            return
        for s in (a, b, before, after):
            c.check(s.f.get("beta_file") == "no", "%s: /etc/casaos/os-upgrade.beta is absent" % s.tag, "absent", s.f.get("beta_file"))
            c.check(s.f.get("run_dir") == "no", "%s: /var/lib/casaos/os-upgrade has not been created" % s.tag, "absent", s.f.get("run_dir"))
        for call_, what in ((st0, "before"), (st1, "after")):
            d = call_.data if isinstance(call_.data, dict) else {}
            c.check(call_.code == 200 and d.get("state") == "idle" and d.get("beta_enabled") is False and not d.get("run_id"),
                    "the status %s the calls is idle, with beta_enabled false and no run" % what, "HTTP 200, idle, beta_enabled false", "%s, state %s, beta_enabled %s, run %s" %
                    (call_.shown(), d.get("state"), d.get("beta_enabled"), d.get("run_id")))
        for call_, what in ((start, "POST start"), (refresh, "POST refresh")):
            c.check(call_.code == 409 and call_.error_code == "beta_off", "%s answers beta_off" % what, "HTTP 409, error_code beta_off", "%s, error_code %s" % (call_.shown(), call_.error_code))
        c.check(not before.pid1 and not after.pid1, "no unit casaos-os-upgrade* was ever started (PID 1's journal since boot)", "no line", few(before.pid1 + after.pid1))
        c.check(not before.units and not after.units, "no unit casaos-os-upgrade* is loaded", "none", few(before.units + after.units))
        forbidden = sorted(n for n in after.names if "casaos-os-upgrade" in n or n == "/var/lib/casaos/os-upgrade" or n.startswith("/var/lib/casaos/os-upgrade/"))
        c.check(not forbidden, "no file or folder of the engine was made", "none", few(forbidden))
        noise = a.names ^ b.names
        delta = before.names ^ after.names
        unexplained = sorted(delta - noise)
        c.check(not unexplained, "no name of the file system changed that the control did not show changing by itself", "none", few(unexplained))
        c.note("what the box changes by itself (control): %d names" % len(noise))
        c.done()

    # ---- C2 ----------------------------------------------------------------------------------------------------------------------------

    def judge_c2():
        if not gate("C2", "switch on"):
            return
        c = Case("C2 switch on")
        beta, status, after = call("c2-beta"), call("c2-status"), snap("c2-after")
        if not all((beta, status, after)):
            return
        c.check(beta.code == 200 and isinstance(beta.data, dict) and beta.data.get("beta_enabled") is True, "POST beta {enabled:true} answers beta_enabled true", "HTTP 200, beta_enabled true",
                "%s, %s" % (beta.shown(), cell(json.dumps(beta.data), 80)))
        c.check(after.f.get("beta_file") == "yes", "/etc/casaos/os-upgrade.beta exists", "present", after.f.get("beta_file"))
        d = status.data if isinstance(status.data, dict) else {}
        c.check(status.code == 200 and d.get("beta_enabled") is True and d.get("state") == "idle", "the status says beta_enabled true, idle", "HTTP 200, beta_enabled true, idle",
                "%s, beta_enabled %s, state %s" % (status.shown(), d.get("beta_enabled"), d.get("state")))
        c.done()

    # ---- C3 ----------------------------------------------------------------------------------------------------------------------------

    def judge_c3():
        if not gate("C3", "refresh"):
            return
        c = Case("C3 refresh")
        before, refresh = snap("c3-before"), call("c3-refresh")
        if not (before and refresh):
            return
        if refresh.code != 202:
            c.check(False, "POST refresh is accepted", "HTTP 202", "%s %s" % (refresh.shown(), cell(refresh.error_code, 60)))
            c.done()
            return
        rs = rows("c3-run")
        if stopped_for_network("c3-run", "C3"):
            return
        seq = reached(c, "c3-run", ("refreshed", "reboot_first"), "the refresh ends refreshed or asking for a restart", ("refreshing", "reboot_first", "refreshed", "idle"))
        if seq is None:
            return
        last = last_answered(rows("c3-run"))
        if last is None or last.state not in ("refreshed", "reboot_first"):
            c.done()
            return
        after = snap("c3-after")
        if after is None:
            return
        if last.state == "reboot_first":
            rb = call("c3-reboot-post", "the restart that the refresh asked for")
            if rb is None:
                return
            c.check(rb.code == 202, "POST reboot is accepted when the refresh asks for a restart", "HTTP 202", rb.shown())
            if exists("boot-c3-reboot.timeout"):
                invalid("C3: the guest did not come back with another boot id after the restart the refresh asked for")
                return
            old = read("c3-reboot.boot-old").strip()
            bt = table("boot-c3-reboot.tsv")
            new = bt[-1][2] if bt and len(bt[-1]) > 2 else "-"
            c.check(old not in ("", "-") and new not in ("", "-") and old != new, "the guest came back with another boot id", "a boot id other than %s" % old, new)
            again = reached(c, "c3-after-reboot", ("refreshed",), "after the restart the refresh is refreshed", ("reboot_first", "refreshed", "idle"))
            c.note("the refresh asked for a restart")
            end = last_answered(again or [])
            if end is None or end.state != "refreshed":
                c.done()
                return
        runs = rows("c3-run") + rows("c3-after-reboot")
        flag_check(c, runs, ("refreshing", "reboot_first"), "c3", "C3")
        refreshed = [r for r in runs if r.answered() and r.state == "refreshed"]
        c.check(bool(refreshed) and "acknowledge" in refreshed[0].acts, "refreshed offers `acknowledge`", "acknowledge among the actions", refreshed[0].actions if refreshed else "never refreshed")
        c.check(before.timers == after.timers, "the timers of apt are as they were", dict_str(before.timers), dict_str(after.timers))
        sd = [n for n in sorted(set(before.sources()) | set(after.sources())) if before.sources().get(n) != after.sources().get(n)]
        c.check(not sd, "the sources of apt are as they were", "the same files, byte for byte", "changed: " + few(sd))
        other = [n for n in apt_diff(before, after) if n not in sd]
        if other:
            c.note("the rest of /etc/apt changed with the packages: " + few(other, 4))
        removed, added, changed = dpkg_diff(before, after)
        c.check(not removed, "no package was removed", "none", few(removed))
        c.check(not after.f.get("timers_state_file") == "yes", "the file of the timers' state is gone", "absent", "present")
        bk_left(c, after, "")
        ack, closed = call("c3-ack"), call("c3-status-closed")
        if ack and closed:
            c.check(ack.code == 200, "acknowledge is accepted", "HTTP 200", ack.shown())
            c.check(isinstance(closed.data, dict) and closed.data.get("state") == "idle", "after the close the status is idle", "idle", closed.shown())
        c.note("%d packages changed version, %d new" % (len(changed), len(added)))
        c.done()
        t_start = refresh.t0
        ended = [r for r in runs if r.answered() and r.state in ("refreshed", "reboot_first")]
        if ended and t_start is not None:
            number("refresh", "%s from POST refresh to %s; %d packages upgraded, %d new; kernel packages %s" % (secs(ended[0].t0 - t_start), ended[0].state, len(changed), len(added),
                                                                                                                  few(sorted(n for n in added if n.startswith("linux-image")))))

    # ---- C4 ----------------------------------------------------------------------------------------------------------------------------

    def judge_c4():
        if not gate("C4", "plan and rehearsal"):
            return
        c = Case("C4 plan and rehearsal")
        pp = call("c4-plan-post", "the plan")
        if pp is None:
            return
        plan, why = plan_state()
        c.check(pp.code in (200, 202), "POST plan is accepted", "HTTP 202", pp.shown())
        if plan is None and plan_network_failure():
            invalid("C4: the update of the plan's sandbox could not reach a mirror (%s): the mirror or the network of the runner failed, not a verdict on the engine" % cell(why, 100))
            return
        if plan is None:
            c.check(False, "the plan of the clean guest can be started from", "a plan that is ok or needs acceptance", why)
            c.done()
            return
        c.note("plan %s, verdict %s" % (cell(plan.get("plan_id"), 40), plan.get("verdict")))
        before, start = snap("c4-before"), call("c4-start")
        if not (before and start):
            return
        if start.code != 202:
            c.check(False, "POST start {rehearsal:true} is accepted", "HTTP 202", "%s %s" % (start.shown(), few(sorted(start.detail.keys()))))
            c.done()
            return
        c.check(request_body("c4-start").get("safety_net") == "", "the rehearsal is started the way the page starts one (an empty safety net)", "safety_net empty", "safety_net %r" % request_body("c4-start").get("safety_net"))
        if stopped_for_network("c4-run", "C4"):
            return
        rs = reached(c, "c4-run", ("rehearsed",), "the rehearsal ends rehearsed", ("rehearsing", "rehearsed", "idle"))
        if rs is None:
            return
        last = last_answered(rs)
        if last is None or last.state != "rehearsed":
            c.done()
            return
        rehearsed = call("c4-status-rehearsed")
        reh = snap("c4-rehearsed")
        if not (rehearsed and reh):
            return
        d = rehearsed.data if isinstance(rehearsed.data, dict) else {}
        exp, since = timestamp(str(d.get("expires_at", ""))[:19]), timestamp(str(d.get("since", ""))[:19])
        c.check(exp is not None and since is not None and 0 < exp - since <= REHEARSAL_TTL + CLOCK_SLACK, "the rehearsal is held for at most 30 minutes (expires_at - since)", "at most %d s" % REHEARSAL_TTL,
                "since %s, expires_at %s" % (d.get("since"), d.get("expires_at")))
        c.check("discard" in last.acts, "rehearsed offers `discard`", "discard among the actions", last.actions)
        c.check((reh.num("sources_bookworm") or 0) > 0 and reh.num("sources_bullseye") == 0, "the sources of Debian 12 are in place", "bookworm lines, no bullseye line",
                "bookworm %s, bullseye %s" % (reh.f.get("sources_bookworm"), reh.f.get("sources_bullseye")))
        c.check(bool(apt_diff(before, reh)), "/etc/apt is not what it was", "changed", "unchanged")
        c.check(all(v[0] != "active" for v in reh.timers.values()), "the timers of apt are stopped", "none active", dict_str(reh.timers))
        c.check(reh.f.get("busy_flag") == "yes", "the busy flag is there while the rehearsal is held", "yes", reh.f.get("busy_flag"))
        discard = call("c4-discard")
        if discard is None:
            return
        c.check(discard.code == 200, "ack discard is accepted", "HTTP 200", discard.shown())
        reached(c, "c4-unwind", ("rehearsal_done",), "after the discard the rehearsal is done", ("rehearsed", "unwinding", "rehearsal_done", "idle"))
        un = rows("c4-unwind")
        done = [r for r in un if r.answered() and r.state == "rehearsal_done"]
        c.check(bool(done) and done[0].busy == "false", "rehearsal_done lets go of the machine (busy false)", "busy false", done[0].busy if done else "never rehearsal_done")
        wait = rows("c4-run") + un
        flag_check(c, wait, ("rehearsing", "rehearsed", "unwinding"), "c4", "C4")
        after = snap("c4-after")
        if after is None:
            return
        machine_as_before(c, before, after)
        removed, added, changed = dpkg_diff(before, after)
        c.check(not (removed or added or changed), "no package was installed, removed or changed", "none", few(removed + added + changed))
        bk_put_away(c, after, "bk-aborted", "", run_id_of(rs))
        ack, closed = call("c4-ack"), call("c4-status-closed")
        if ack and closed:
            c.check(ack.code == 200, "acknowledge is accepted", "HTTP 200", ack.shown())
            c.check(isinstance(closed.data, dict) and closed.data.get("state") == "idle", "after the close the status is idle", "idle", closed.shown())
        c.done()
        if rs and start.t0 is not None:
            reached_t = next((r.t0 for r in rs if r.answered() and r.state == "rehearsed"), None)
            number("plan", "%s from POST plan to done" % (secs(plan_seconds()) if plan_seconds() is not None else "unknown"))
            if reached_t is not None:
                number("rehearsal", "%s from POST start to rehearsed; %s downloaded into the cache of apt; the unwind took %s" % (
                    secs(reached_t - start.t0), mib(max(0, (reh.num("cache_bytes") or 0) - (before.num("cache_bytes") or 0))),
                    secs(done[0].t0 - discard.t0) if done else "unknown"))

    def plan_seconds():
        pr = [r for r in table("polls-c4-plan.tsv") if len(r) == 6]
        pp = Call("c4-plan-post")
        if not pr or pp.t0 is None:
            return None
        t = [fnum(r[0]) for r in pr if r[4] == "done"]
        return (t[0] - pp.t0) if t and t[0] is not None else None

    # ---- C5 ----------------------------------------------------------------------------------------------------------------------------

    def judge_c5():
        if "case_C5_done" not in marks and "case_C5_start" in marks and exists("c5-restart.rebooted"):
            # the page's restart was carried out while the unit worked: the guest went away and the run ended there (the abort says so); this is a failure of the engine
            c = Case("C5 os_upgrade_open")
            r = Call("c5-restart")
            c.check(False, "the page's PUT /v1/sys/state/restart is refused while the unit works", "HTTP 409, error_code os_upgrade_open", "%s, and the guest restarted (its boot id changed)" % r.shown())
            c.done()
            return
        if not gate("C5", "refusals"):
            return
        plan, why = plan_state()
        host = read("hostname").strip()
        hn = Call("c5-hostname")
        if plan is None:
            for n in ("hostname_mismatch", "acks_missing", "dpkg lock", "run_open", "os_upgrade_open"):
                not_testable("C5 " + n, "no startable plan to start from (%s)" % why)
            return
        before, after = snap("c5-before"), snap("c5-after")
        if not (before and after):
            return
        hn, ak = call("c5-hostname"), call("c5-acks")
        if hn:
            c = Case("C5 hostname_mismatch")
            c.check(hn.code == 409 and hn.error_code == "hostname_mismatch", "a start with another hostname is refused", "HTTP 409, error_code hostname_mismatch", "%s, error_code %s" % (hn.shown(), hn.error_code))
            c.check(host + "x" not in read("c5-hostname.json"), "the answer does not repeat the name that was typed", "absent", "present")
            c.done()
        if ak:
            c = Case("C5 acks_missing")
            missing = ak.detail.get("missing") if isinstance(ak.detail.get("missing"), list) else []
            c.check(ak.code == 409 and ak.error_code == "acks_missing", "a start without its acknowledgements is refused", "HTTP 409, error_code acks_missing", "%s, error_code %s" % (ak.shown(), ak.error_code))
            c.check({"no_interruption", "no_way_back"} <= set(missing), "the refusal names the acknowledgements that are missing", "no_interruption and no_way_back", few(missing) or "none")
            c.done()
        if exists("c5-lock.code"):
            c = Case("C5 dpkg lock")
            lock = call("c5-lock", "a start while dpkg's lock is held")
            if lock:
                checks = lock.detail.get("checks") if isinstance(lock.detail.get("checks"), list) else []
                busy_check = any(isinstance(x, dict) and x.get("code") == "busy" for x in checks)
                ok = lock.code == 409 and (lock.error_code in ("busy", "dpkg_locked") or (lock.error_code == "readiness_refuses" and busy_check))
                during = read("c5-lock.during").split()
                c.check("held" in during, "the proof held dpkg's lock while it asked", "held", " ".join(during) or "no sample")
                c.check(ok, "a start while dpkg's lock is held is refused (busy, dpkg_locked, or readiness_refuses with the check busy)", "HTTP 409 and one of those",
                        "%s, error_code %s%s" % (lock.shown(), lock.error_code, ", checks " + few(x.get("code") for x in checks if isinstance(x, dict)) if checks else ""))
                c.note("the core said %s" % (lock.error_code or lock.shown()))
                if "free" not in read("c5-lock.undone").split():
                    invalid("C5: dpkg's lock was not given back (%s)" % (" ".join(read("c5-lock.undone").split()) or "no sample"))
                c.done()
        else:
            invalid("C5: the lock case left no call: dpkg's lock could not be held, or the case did not get there")
        op = Call("c5-open")
        if op.ran:
            before2 = call("c5-status-before-second")
            second = call("c5-second")
            restart = call("c5-restart")
            after_r = call("c5-status-after-restart")
            if not all((before2, second, restart, after_r)):
                pass
            elif not (op.code == 202 and before2.field("state") == "rehearsing"):
                not_testable("C5 run_open", "the rehearsal that holds the run open was not working when the second start was asked (%s)" % (op.shown() if op.code != 202 else before2.field("state")))
                not_testable("C5 os_upgrade_open", "the unit was not known to work when the restart was asked (%s)" % (op.shown() if op.code != 202 else before2.field("state")))
            else:
                c = Case("C5 run_open")
                c.check(request_body("c5-open").get("safety_net") == "snapshot" and request_body("c5-open").get("rehearsal") is True, "the rehearsal that holds the run open is started with the safety net of a hop",
                        "a rehearsal with safety_net snapshot", "%s, safety_net %r" % (request_body("c5-open").get("rehearsal"), request_body("c5-open").get("safety_net")))
                c.check(second.code == 409 and second.error_code == "run_open", "a second start while a run is open is refused", "HTTP 409, error_code run_open",
                        "%s, error_code %s" % (second.shown(), second.error_code))
                c.check(second.detail.get("state") in WORK_STATES + ("rehearsed",), "the refusal says in which state the open run is", "a state of the open run", cell(second.detail.get("state")))
                c.done()
                c = Case("C5 os_upgrade_open")
                working = after_r.field("state") in WORK_STATES
                bb, ba = read("c5-restart.boot-before").strip(), read("c5-restart.boot-after").strip()
                refused = restart.code == 409 and restart.error_code == "os_upgrade_open"
                if not refused and not working:
                    not_testable("C5 os_upgrade_open", "the unit had stopped working when the answer was read (state %s): a restart is allowed then" % after_r.field("state"))
                else:
                    c.check(refused, "the page's PUT /v1/sys/state/restart is refused while the unit works", "HTTP 409, error_code os_upgrade_open",
                            "%s, error_code %s" % (restart.shown(), restart.error_code))
                    c.check(bb not in ("", "-") and ba == bb and not exists("c5-restart.rebooted"), "the guest did not restart", "the boot id %s again" % bb, "boot id %s" % (ba or "none"))
                    c.done()
        else:
            not_testable("C5 run_open", "the rehearsal that holds the run open was not started (no call)")
            not_testable("C5 os_upgrade_open", "the rehearsal that holds the run open was not started (no call)")
        c = Case("C5 nothing left behind")
        machine_as_before(c, before, after, label="")
        removed, added, changed = dpkg_diff(before, after)
        c.check(not (removed or added or changed), "no package was installed, removed or changed", "none", few(removed + added + changed))
        bk_left(c, after, "")
        for n in ("c5-open", "c5-lock"):
            if exists(n + ".closed"):
                c.check(read(n + ".closed").strip() == "idle", "the run that %s opened is closed" % n, "idle", read(n + ".closed").strip())
        c.done()

    # ---- C6 ----------------------------------------------------------------------------------------------------------------------------

    def judge_c6():
        if not gate("C6", "call-off before the point of no return"):
            return
        plan, why = plan_state()
        start = Call("c6-start")
        if plan is None:
            not_testable("C6 call-off before the point of no return", "no startable plan to start from (%s)" % why)
            return
        c = Case("C6 call-off before the point of no return")
        before, start = snap("c6-before"), call("c6-start")
        if not (before and start):
            return
        if start.code != 202:
            c.check(False, "POST start (the hop) is accepted", "HTTP 202", "%s %s" % (start.shown(), few(sorted(start.detail.keys()))))
            c.done()
            return
        c.check(request_body("c6-start").get("safety_net") == "snapshot" and request_body("c6-start").get("rehearsal") is False, "the hop is started with the safety net snapshot", "a hop with safety_net snapshot",
                "rehearsal %s, safety_net %r" % (request_body("c6-start").get("rehearsal"), request_body("c6-start").get("safety_net")))
        run = rows("c6-run")
        early = [r for r in run if r.answered() and r.state == "running" and r.bid in EARLY_BLOCKS]
        if not early:
            invalid("C6: the hop was never seen working in block 1 to 5")
            return
        abort = call("c6-abort")
        if abort is None:
            return
        c.check("abort" in early[-1].acts, "running offers `abort` before the point of no return", "abort among the actions", early[-1].actions)
        c.check(abort.code == 202, "POST abort is accepted while block %s works" % early[-1].bid, "HTTP 202", "%s %s" % (abort.shown(), few(sorted(abort.detail.items()))))
        if stopped_for_network("c6-after", "C6"):
            return
        rs = reached(c, "c6-after", ("failed",), "the called-off run is failed", ("running", "interrupted", "unwinding", "failed"))
        if rs is None:
            return
        failed = [r for r in rs if r.answered() and r.state == "failed"]
        st = call("c6-status-failed")
        if st and failed:
            f = st.field("failure") if isinstance(st.field("failure"), dict) else {}
            c.check(f.get("class") == "abort" and failed[0].fclass == "abort", "the run is failed with the class abort", "failure.class abort", "class %s, row %s" % (f.get("class"), failed[0].fclass))
            c.check(st.field("pnr") is False and not st.field("half_upgraded"), "the point of no return was not passed, nothing is half upgraded", "pnr false, half_upgraded false",
                    "pnr %s, half_upgraded %s" % (st.field("pnr"), st.field("half_upgraded")))
            c.check(failed[0].busy == "false", "the failed run lets go of the machine before the close (busy false)", "busy false", failed[0].busy)
            c.check("acknowledge" in failed[0].acts, "failed offers `acknowledge`", "acknowledge among the actions", failed[0].actions)
        flag_check(c, run + rs, ("running", "unwinding"), "c6", "C6")
        after = snap("c6-after")
        if after is None:
            return
        machine_as_before(c, before, after)
        removed, added, changed = dpkg_diff(before, after)
        c.check(not (removed or added or changed), "no package was installed, removed or changed", "none", few(removed + added + changed))
        bk_put_away(c, after, "bk-aborted", "", run_id_of(run))
        ack, closed = call("c6-ack"), call("c6-status-closed")
        if ack and closed:
            c.check(ack.code == 200, "acknowledge closes the run", "HTTP 200", ack.shown())
            c.check(isinstance(closed.data, dict) and closed.data.get("state") == "idle", "after the close the status is idle", "idle", closed.shown())
        c.done()
        t_abort = abort.t0
        done_t = next((r.t0 for r in failed), None)
        if t_abort is not None and done_t is not None:
            number("call-off", "%s from POST abort (in block %s) to failed" % (secs(done_t - t_abort), early[-1].bid))

    # ---- the hop, and the tail it has in common with the faults -------------------------------------------------------------------------

    def block_order_ok(rs, order=HOP_BLOCKS):
        seen = [r.bid for r in rs if r.answered() and r.state in ("running", "recovering") and r.bid != "-"]
        idx = [order.index(b) for b in seen if b in order]
        unknown = sorted(set(b for b in seen if b not in order and b != "forward"))
        return all(a <= b for a, b in zip(idx, idx[1:])), unknown

    def block_seconds(rs, end_t):
        """seconds per block, from the first poll that saw it to the first poll that saw the next one (poll resolution)"""
        firsts = []
        for r in rs:
            if r.answered() and r.state in ("running", "recovering") and r.bid != "-" and (not firsts or firsts[-1][0] != r.bid):
                firsts.append((r.bid, r.t0))
        out = []
        for i, (b, t) in enumerate(firsts):
            nxt = firsts[i + 1][1] if i + 1 < len(firsts) else end_t
            if nxt is not None:
                out.append((b, nxt - t))
        return out

    def hop_tail_checks(c, p, before, label, kernel_before=None):
        """awaiting_reboot to the close: the machine as it waits, the restart, the check, the machine as it is. <p> is the prefix of the calls."""
        awaiting = call("%s-status-awaiting" % p)
        aw = snap("%s-awaiting" % p)
        if not (awaiting and aw):
            return None
        bw = rows("%s-barrier" % p)
        offered = next((r for r in bw if r.answered() and "reboot" in r.acts), None)
        first = next((r for r in bw if r.answered() and r.state == "awaiting_reboot"), None)
        c.check(offered is not None, "%sthe barrier is computed and `reboot` is offered" % label, "within %d s of awaiting_reboot" % 240, "never offered (%s)" % (" > ".join(states_of(bw)) or "no poll"))
        if offered is not None and first is not None:
            c.note("%sthe core offered the restart %s after the last block" % (label, secs(offered.t0 - first.t0)))
        d = awaiting.data if isinstance(awaiting.data, dict) else {}
        barrier = d.get("barrier") if isinstance(d.get("barrier"), dict) else {}
        c.check(d.get("state") == "awaiting_reboot", "%sthe status says awaiting_reboot" % label, "awaiting_reboot", cell(d.get("state")))
        c.check(barrier.get("ok") is True, "%sthe barrier is green" % label, "barrier.ok true", "barrier %s" % cell(json.dumps(barrier), 200))
        red = [x.get("code") for x in barrier.get("checks") or [] if isinstance(x, dict) and x.get("ok") is False]
        c.check(not red, "%sno check before the restart is red" % label, "none", few(red))
        c.check(d.get("pnr") is True and not d.get("half_upgraded") and d.get("busy") is True, "%sthe point of no return is behind, nothing is half upgraded, the machine is held" % label, "pnr true, half_upgraded false, busy true",
                "pnr %s, half_upgraded %s, busy %s" % (d.get("pnr"), d.get("half_upgraded"), d.get("busy")))
        c.check("reboot" in (d.get("actions") or []), "%sawaiting_reboot offers `reboot`" % label, "reboot among the actions", few(d.get("actions") or []))
        c.check(aw.f.get("busy_flag") == "yes", "%sthe busy flag stays up while the machine waits" % label, "yes", aw.f.get("busy_flag"))
        c.check((aw.num("sources_bookworm") or 0) > 0 and aw.num("sources_bullseye") == 0, "%sthe sources are those of Debian 12" % label, "bookworm lines, no bullseye line",
                "bookworm %s, bullseye %s" % (aw.f.get("sources_bookworm"), aw.f.get("sources_bullseye")))
        c.check(aw.f.get("uname") == before.f.get("uname"), "%sthe old kernel still runs until the restart" % label, "kernel %s" % before.f.get("uname"), "kernel %s" % aw.f.get("uname"))
        rb = call("%s-reboot-post" % p)
        if rb is None:
            return None
        c.check(rb.code == 202, "%sPOST reboot is accepted" % label, "HTTP 202", "%s %s" % (rb.shown(), cell(rb.error_code, 40)))
        if exists("boot-%s-reboot.timeout" % p):
            invalid("%s: the guest did not come back with another boot id after the restart (its serial log is in the artifacts)" % (label.strip() or p))
            return None
        old = read("%s-reboot.boot-old" % p).strip()
        bt = [r for r in table("boot-%s-reboot.tsv" % p) if len(r) == 3]
        new = bt[-1][2] if bt else "-"
        c.check(old not in ("", "-") and new not in ("", "-") and old != new and not exists("boot-%s-reboot.timeout" % p), "%sthe guest went away and came back with another boot id" % label,
                "a boot id other than %s" % old, "%s%s" % (new, ", the time ran out" if exists("boot-%s-reboot.timeout" % p) else ""))
        vr = rows("%s-verify" % p)
        hop_run = run_id_of(vr) or run_id_of(bw)
        if stopped_for_network("%s-verify" % p, label.strip() or p):
            return None
        reached(c, "%s-verify" % p, ("succeeded", "succeeded_with_findings"), "%sthe check after the restart ends in a success" % label,
                ("rebooting", "awaiting_reboot", "reboot_stalled", "resuming", "verifying", "succeeded", "succeeded_with_findings"))
        order = ["awaiting_reboot", "rebooting", "reboot_stalled", "resuming", "verifying", "succeeded", "succeeded_with_findings"]
        seq = [x for x in states_of(vr) if x in order]
        idx = [order.index(x) for x in seq]
        c.check(all(a <= b for a, b in zip(idx, idx[1:])), "%sthe states after the restart come in their order (rebooting, resuming, verifying, the verdict)" % label, " > ".join(order[1:]), " > ".join(seq))
        if not {"resuming", "verifying"} & set(seq):
            c.note("%sneither resuming nor verifying was seen by the polls" % label)
        last = last_answered(vr)
        if last is None or last.state not in ("succeeded", "succeeded_with_findings"):
            return None
        final = call("%s-status-final" % p)
        after, final_snap = snap("%s-after" % p), snap("%s-final" % p)
        if not (final and after and final_snap):
            return None
        f = final.data if isinstance(final.data, dict) else {}
        c.check(not f.get("half_upgraded") and not f.get("busy"), "%sthe verdict is not half upgraded and lets go of the machine" % label, "half_upgraded false, busy false",
                "half_upgraded %s, busy %s" % (f.get("half_upgraded"), f.get("busy")))
        c.check(not f.get("not_returned"), "%sthe containers that ran before are back" % label, "none missing", cell(json.dumps(f.get("not_returned")), 200))
        findings = [x for x in (f.get("findings") or []) if isinstance(x, dict)]
        errs = [x for x in findings if x.get("severity") == "error"]
        c.check(not errs, "%sthe judge found no finding of severity error" % label, "no finding of severity error (warn and info are for whoever reads the report)", few("%s%s" % (x.get("code"), "(" + str(x.get("arg")) + ")" if x.get("arg") else "") for x in errs))
        if findings:
            by = {sev: sum(1 for x in findings if x.get("severity") == sev) for sev in FINDING_SEVERITIES}
            notes.append("%sfindings of the judge, %s (%s): %s" % (label or p + " ", f.get("state"), ", ".join("%d %s" % (n, sev) for sev, n in by.items() if n),
                                                                  "; ".join(cell("%s %s%s" % (x.get("severity") or "no severity", x.get("code"), " " + str(x.get("arg")) if x.get("arg") else ""), 120) for x in findings)))
        if f.get("not_judged"):
            notes.append("%snot judged: %s" % (label or p + " ", few(f.get("not_judged"))))
        c.check(after.f.get("version_id") == "12", "%sos-release says VERSION_ID=12" % label, "12", after.f.get("version_id"))
        c.check(newer(after.f.get("uname", ""), before.f.get("uname", "")), "%sa kernel newer than before runs" % label, "later than %s" % before.f.get("uname"), after.f.get("uname"))
        svc_before = {n for n, st in before.services.items() if st == "active"}
        down = sorted(n for n in svc_before if after.services.get(n) != "active")
        c.check(not down, "%ssshd and every ReCasaOS service that ran before are active" % label, "all active", "not active: " + few(down))
        c.check(any(n in ("ssh.service", "sshd.service") and st == "active" for n, st in after.services.items()), "%ssshd is active" % label, "ssh.service active", dict_str_services(after.services, "ssh"))
        c.check(after.witnesses() == before.witnesses() and len(after.witnesses()) >= len(WITNESSES), "%sthe witness containers returned" % label, ",".join(before.witnesses()), ",".join(after.witnesses()) or "none")
        c.check(after.timers == before.timers, "%sthe timers of apt are back" % label, dict_str(before.timers), dict_str(after.timers))
        new_failed = sorted(set(after.failed_units()) - set(before.failed_units()))
        c.check(not new_failed, "%sno unit failed that was not failed before" % label, "none", few(new_failed))
        c.check(after.f.get("dpkg_audit") == "0", "%sdpkg --audit finds nothing" % label, "0 lines", after.f.get("dpkg_audit"))
        ft = table("flag-%s.tsv" % p)
        c.check(bool(ft) and ft[-1][1:2] == ["no"], "%sthe busy flag is gone at the verdict" % label, "gone within %d s" % FLAG_WITHIN, "still there after %d samples" % len(ft))
        ack, closed = call("%s-ack" % p), call("%s-status-closed" % p)
        if ack and closed:
            c.check(ack.code == 200, "%sacknowledge closes the run" % label, "HTTP 200", ack.shown())
            c.check(isinstance(closed.data, dict) and closed.data.get("state") == "idle", "%safter the close the status is idle" % label, "idle", closed.shown())
        bk_put_away(c, final_snap, "bk-done", label, hop_run)
        return dict(aw=aw, after=after, final_snap=final_snap, verify_rows=vr, last=last, reboot_post=rb, boot_table=bt, old_boot=old, state=f.get("state"))

    def dict_str_services(services, prefix):
        return ", ".join("%s=%s" % (k, v) for k, v in sorted(services.items()) if k.startswith(prefix)) or "none"

    def hop_numbers(p, before, tail, run_rows, start_call, label, begin="POST start"):
        aw, after = tail["aw"], tail["after"]
        awaiting_t = next((r.t0 for r in run_rows if r.answered() and r.state == "awaiting_reboot"), None)
        blocks = block_seconds(run_rows, awaiting_t)
        if blocks:
            number(label + "seconds per block", ", ".join("%s %s" % (b, secs(s)) for b, s in blocks) + " (to the resolution of the poll)")
        if start_call.t0 is not None and awaiting_t is not None:
            number(label + "hop", "%s from %s to awaiting_reboot" % (secs(awaiting_t - start_call.t0), begin))
        rx = (aw.num("rx_bytes") or 0) - (before.num("rx_bytes") or 0)
        if rx >= 0:
            number(label + "downloaded", "%s received by the guest between the start and the wait for the restart (all traffic of the interface, the proof's own polls included)" % mib(rx))
        else:
            number(label + "downloaded", "not known: the counter of the interface started again with the restart that the fault made")
        number(label + "free space", "root: %s before, %s when it waits, %s after; /boot: %s before, %s after" % (
            mib((before.num("df_root_kb") or 0) * 1024), mib((aw.num("df_root_kb") or 0) * 1024), mib((after.num("df_root_kb") or 0) * 1024),
            mib((before.num("df_boot_kb") or 0) * 1024), mib((after.num("df_boot_kb") or 0) * 1024)))
        number(label + "kernel", "%s before, %s after" % (before.f.get("uname"), after.f.get("uname")))
        bt = tail["boot_table"]
        gone = [fnum(r[1]) for r in bt if r[2] != "-" and r[2] == tail["old_boot"] and fnum(r[1]) is not None]
        back = [fnum(r[0]) for r in bt if r[2] not in ("-", tail["old_boot"]) and fnum(r[0]) is not None]
        if gone and back:
            number(label + "away at the restart", "%s from the last answer of the old boot to the first of the new (to the resolution of the sampling)" % secs(back[0] - gone[-1]))
        done_t = next((r.t0 for r in tail["verify_rows"] if r.answered() and r.state in ("succeeded", "succeeded_with_findings")), None)
        if tail["reboot_post"].t0 is not None and done_t is not None:
            number(label + "restart to the verdict", "%s from POST reboot to %s" % (secs(done_t - tail["reboot_post"].t0), tail["state"]))

    def judge_c7():
        if not gate("C7", "the hop"):
            return
        plan, why = plan_state()
        start = Call("c7-start")
        if plan is None:
            not_testable("C7 the hop", "no startable plan to start from (%s)" % why)
            return
        c = Case("C7 the hop")
        before, start = snap("c7-before"), call("c7-start")
        if not (before and start):
            return
        if start.code != 202:
            c.check(False, "POST start (the hop) is accepted", "HTTP 202", "%s %s" % (start.shown(), few(sorted(start.detail.keys()))))
            c.done()
            return
        c.check(request_body("c7-start").get("safety_net") == "snapshot" and request_body("c7-start").get("rehearsal") is False, "the hop is started with the safety net snapshot", "a hop with safety_net snapshot",
                "rehearsal %s, safety_net %r" % (request_body("c7-start").get("rehearsal"), request_body("c7-start").get("safety_net")))
        if stopped_for_network("c7-run", "C7"):
            return
        rs = reached(c, "c7-run", ("awaiting_reboot",), "the blocks end with the machine waiting for its restart", ("running", "awaiting_reboot", "idle"))
        if rs is None:
            return
        order_ok, unknown = block_order_ok(rs)
        c.check(order_ok and not unknown, "the blocks are seen in their order (%s)" % ", ".join(HOP_BLOCKS[:-1]), "a non-decreasing order of known blocks", "blocks seen: %s" % few(r.bid for r in rs if r.bid != "-"))
        c.check(any(r.answered() and r.pnr == "true" for r in rs), "the point of no return is seen passed", "pnr true in a poll", "never")
        flag_up(c, rs, ("running",), "C7")
        last = last_answered(rs)
        if last is None or last.state != "awaiting_reboot":
            c.done()
            return
        tail = hop_tail_checks(c, "c7", before, "")
        c.done()
        if tail:
            hop_numbers("c7", before, tail, rs, start, "")

    # ---- the faults --------------------------------------------------------------------------------------------------------------------

    def fault_base(cid):
        """the machine as the snapshot restored it: the snapshot's own boot (that of the last snapshot of the prelude)"""
        base = snap("%s-base" % cid, "the guest restored from the snapshot")
        prelude = snap("c4-after", "the last snapshot before the machine's own was taken")
        if base is None:
            return None
        if prelude is not None and base.f.get("boot_id") != prelude.f.get("boot_id"):
            invalid("%s: the restored guest is not the snapshotted one (boot id %s, the snapshot's %s)" % (cid, base.f.get("boot_id"), prelude.f.get("boot_id")))
            return None
        if not clock_set(cid, cid):
            return None
        return base

    def clock_lines(name):
        return [ln.strip() for ln in read(name + ".clock").splitlines() if ln.strip() and not ln.startswith("#")]

    def clock_set(cid, name):
        """the clock of a guest that was restored from a snapshot (or that was reset) is that of the snapshot, behind the Release files of the mirrors: it was set from the runner's before anything polled"""
        lines = clock_lines(name)
        if "set" not in lines:
            invalid("%s: the clock of the restored guest was not set from the runner's (%s): its first apt-get update would meet a Release file dated in its future" % (cid, cell(" ".join(lines), 120) or "no record"))
            return False
        skew = next((int(ln.split()[1]) for ln in lines if re.fullmatch(r"skew -?\d+", ln)), None)
        if skew is not None:
            number("%s clock" % name.replace(".", " "), "the restored guest was %s %s the runner's clock, and was set from it before anything polled" % (secs(abs(skew)), "behind" if skew >= 0 else "ahead of"))
        return True

    def dpkg_seen(name):
        """True: dpkg was seen at work (three looks in a row) before the cut; False: it was not within the time; None: the runner left no record"""
        lines = [ln.strip() for ln in read(name + ".dpkg").splitlines() if ln.strip() and not ln.startswith("#")]
        if any(ln.startswith("seen ") for ln in lines):
            return True
        return False if any(ln.startswith("never ") for ln in lines) else None

    def dpkg_at_work(cid, c_title):
        """the verdict of a fault that has to land in the middle of dpkg waits for the evidence that it did: not seen is NOT TESTABLE, no record is no evidence. True: judge it"""
        seen = dpkg_seen(cid)
        if seen is None:
            invalid("%s: there is no record of dpkg at work when the fault was delivered (%s.dpkg)" % (cid, cid))
            return False
        if not seen:
            not_testable("%s %s" % (cid, c_title), "dpkg was not seen running (%s)" % cell(read(cid + ".dpkg").strip().splitlines()[-1] if read(cid + ".dpkg").strip() else "", 60))
            return False
        number("%s dpkg" % cid, "seen at work (%s) before the fault was delivered, which was %s" % (cell([ln for ln in read(cid + ".dpkg").splitlines() if ln.startswith("seen ")][0], 40),
                                                                                             "the kill of the unit, once dpkg had been at work for three looks in a row" if cid == "F2" else "the reset of the machine, once dpkg had been at work for three looks in a row"))
        return True

    def untestable(cid, title):
        if exists("%s.untestable" % cid):
            not_testable("%s %s" % (cid, title), read("%s.untestable" % cid).strip())
            return True
        return False

    def kill_taken(cid, c):
        """the unit was killed: the invocation the runner saw is gone, however many tries it took (a try that was cut short may have done it, and the next says gone)"""
        lines = [ln.strip() for ln in read("%s.kill" % cid).splitlines() if ln.strip() and not ln.startswith("#")]
        tries = sum(1 for ln in read("%s.kill" % cid).splitlines() if ln.startswith("# try "))
        c.check("gone" in lines, "systemctl kill casaos-os-upgrade took effect (the invocation of the unit that was seen is gone)", "gone", cell(" ".join(lines), 100) or "no record")
        if tries > 1:
            c.note("the kill took %d tries (a missed ssh)" % tries)

    def judge_f1():
        if not gate("F1", CASES_FAULTS[0][1]):
            return
        if untestable("F1", CASES_FAULTS[0][1]):
            return
        base, start = fault_base("F1"), call("f1-start")
        if not (base and start):
            return
        c = Case("F1 " + CASES_FAULTS[0][1])
        if start.code != 202:
            c.check(False, "POST start (the hop) is accepted", "HTTP 202", "%s %s" % (start.shown(), few(sorted(start.detail.keys()))))
            c.done()
            return
        run = rows("f1-run")
        early = [r for r in run if r.answered() and r.state == "running" and r.bid in EARLY_BLOCKS]
        c.check(bool(early), "the unit was killed while block 1 to 5 worked", "a poll in a block before the point of no return", "none seen")
        kill_taken("F1", c)
        if stopped_for_network("f1-after", "F1"):
            return
        rs = reached(c, "f1-after", ("failed",), "the core puts the machine back by itself and the run is failed", ("running", "interrupted", "unwinding", "failed", "-"))
        if rs is None:
            return
        seq = states_of(rs)
        failed = [r for r in rs if r.answered() and r.state == "failed"]
        # `interrupted` lasts until the next look of the core, which launches the unwind then: a poll can miss it. The run was seen working before the kill (the check above), and a failure with the reason
        # interrupted_killed is what the unwind of an interruption by a kill leaves, and nothing else: that is the proof of it when the polls missed it (and `unwinding`, if seen, comes before `failed`)
        by_road = "interrupted" not in seq and bool(failed) and failed[0].reason == "interrupted_killed" and ("unwinding" not in seq or is_subsequence(["unwinding", "failed"], seq))
        c.check("interrupted" in seq or by_road, "the run is seen interrupted before it is put back, or fails with the reason interrupted_killed, which only an interruption by a kill leaves (it lasts one look of the core, which a poll can miss)",
                "interrupted in the states seen, or failed(interrupted_killed)", " > ".join(seq) + (" (reason %s)" % failed[0].reason if failed else ""))
        if by_road:
            c.note("the polls did not catch `interrupted`, which lasts one look of the core (they saw %s); the reason interrupted_killed of the failure is the proof of it" % (" > ".join(seq) or "nothing"))
        else:
            c.check(is_subsequence(["interrupted", "failed"], seq) and (("unwinding" not in seq) or is_subsequence(["interrupted", "unwinding", "failed"], seq)), "interrupted, then (unwinding), then failed", "that order", " > ".join(seq))
            inter = [r for r in rs if r.answered() and r.state == "interrupted"]
            c.check(bool(inter) and all(r.cause == "killed" for r in inter) and all(r.pnr == "false" for r in inter), "interrupted by a kill, before the point of no return", "cause killed, pnr false",
                    few("cause %s pnr %s" % (r.cause, r.pnr) for r in inter[:2]) or "never interrupted")
        c.check(bool(failed) and failed[0].reason == "interrupted_killed" and failed[0].pnr == "false" and failed[0].half == "false", "failed with the reason interrupted_killed, nothing half upgraded",
                "reason interrupted_killed, pnr false, half_upgraded false", few("reason %s pnr %s half %s" % (r.reason, r.pnr, r.half) for r in failed[:1]) or "never failed")
        flag_check(c, run + rs, ("running", "interrupted", "unwinding"), "f1", "F1")
        after = snap("f1-after")
        if after is None:
            return
        machine_as_before(c, base, after)
        removed, added, changed = dpkg_diff(base, after)
        c.check(not (removed or added or changed), "no package was installed, removed or changed", "none", few(removed + added + changed))
        bk_put_away(c, after, "bk-aborted", "", run_id_of(run))
        ack, closed = call("f1-ack"), call("f1-status-closed")
        if ack and closed:
            c.check(ack.code == 200 and isinstance(closed.data, dict) and closed.data.get("state") == "idle", "acknowledge closes the run", "HTTP 200, idle", "%s, %s" % (ack.shown(), closed.shown()))
        c.done()
        t_kill = next((r.t0 for r in rs if r.answered()), None)
        done_t = next((r.t0 for r in failed), None)
        if t_kill is not None and done_t is not None:
            number("F1 kill to failed", secs(done_t - t_kill))

    def judge_f2():
        if not gate("F2", CASES_FAULTS[1][1]):
            return
        if untestable("F2", CASES_FAULTS[1][1]):
            return
        base, start = fault_base("F2"), call("f2-start")
        if not (base and start):
            return
        c = Case("F2 " + CASES_FAULTS[1][1])
        if start.code != 202:
            c.check(False, "POST start (the hop) is accepted", "HTTP 202", "%s %s" % (start.shown(), few(sorted(start.detail.keys()))))
            c.done()
            return
        if not dpkg_at_work("F2", CASES_FAULTS[1][1]):
            return
        if "dpkg_at_kill no" in read("F2.kill") and "dpkg_at_kill yes" not in read("F2.kill"):
            not_testable("F2 " + CASES_FAULTS[1][1], "dpkg was not running at the moment of the kill (it ended in the second between the look and the kill)")
            return
        kill_taken("F2", c)
        if stopped_for_network("f2-gone", "F2"):
            return
        gone = reached(c, "f2-gone", ("interrupted",), "the killed unit leaves the run interrupted", ("running", "interrupted", "-"))
        if gone is None:
            return
        st = call("f2-status-interrupted")
        if st:
            c.check(st.field("state") == "interrupted" and st.field("pnr") is True and st.field("cause") == "killed", "interrupted by a kill after the point of no return", "interrupted, pnr true, cause killed",
                    "%s, pnr %s, cause %s" % (st.field("state"), st.field("pnr"), st.field("cause")))
            c.check("resume" in (st.field("actions") or []), "interrupted offers `resume`", "resume among the actions", few(st.field("actions") or []))
        obs = rows("f2-observe")
        if not obs:
            invalid("polls-f2-observe.tsv is missing: nothing was observed after the kill")
            return
        span = obs[-1].t1 - obs[0].t0
        bad = sorted(set(r.state for r in obs if r.answered() and r.state != "interrupted"))
        c.check(span >= OBSERVE_MIN, "the stopped run was looked at long enough (more than the 2 minutes of backoff that a repair that is allowed waits)", "at least %d s" % OBSERVE_MIN, secs(span))
        c.check(not bad, "no automatic repair in the %s that the stopped run was watched: it stays interrupted (this proves that the shipped setting of zero tries holds, and nothing of what a repair would do)" % secs(span),
                "interrupted throughout", "also " + few(bad))
        resume = call("f2-resume")
        if resume is None:
            return
        c.check(resume.code == 202, "POST resume is accepted", "HTTP 202", "%s %s" % (resume.shown(), cell(resume.error_code, 40)))
        if resume.code != 202:
            c.done()
            return
        if stopped_for_network("f2-recover", "F2"):
            return
        rec = reached(c, "f2-recover", ("awaiting_reboot",), "resume completes the hop up to the restart", ("interrupted", "recovering", "awaiting_reboot", "-"))
        if rec is None:
            return
        c.check("recovering" in states_of(rec), "the repair runs as a unit of its own (recovering)", "recovering in the states seen", " > ".join(states_of(rec)))
        last = last_answered(rec)
        if last is not None and last.state == "awaiting_reboot":
            tail = hop_tail_checks(c, "f2", base, "")
            if tail:
                hop_numbers("f2", base, tail, rec, resume, "F2 ", "POST resume")
        c.done()

    def judge_f3():
        if not gate("F3", CASES_FAULTS[2][1]):
            return
        if untestable("F3", CASES_FAULTS[2][1]):
            return
        base, start = fault_base("F3"), call("f3-start")
        if not (base and start):
            return
        run = rows("f3-stopped")
        last = last_answered(run)
        if last is not None and last.state == "awaiting_reboot":
            not_testable("F3 " + CASES_FAULTS[2][1], "the run reached awaiting_reboot with the network cut: it had nothing left to download (%s)" % " > ".join(states_of(run)))
            return
        c = Case("F3 " + CASES_FAULTS[2][1])
        if start.code != 202:
            c.check(False, "POST start (the hop) is accepted", "HTTP 202", "%s %s" % (start.shown(), few(sorted(start.detail.keys()))))
            c.done()
            return
        cut = [ln.strip() for ln in read("f3.cut").splitlines() if ln.strip() and not ln.startswith("#")]
        c.check(bool(cut) and cut[-1] == "cut", "the routes of the guest were taken away after the point of no return, and a request from the guest failed (the proof that it is cut)", "cut", " ".join(cut) or "no record")
        stopped = reached(c, "f3-stopped", ("stopped_at_waypoint",), "a download that cannot go through stops the run at a waypoint", ("running", "stopped_at_waypoint", "-"))
        if stopped is None:
            return
        st = call("f3-status-stopped")
        if st:
            f = st.field("failure") if isinstance(st.field("failure"), dict) else {}
            c.check(f.get("class") == "network", "the stop has the class network", "failure.class network", "class %s, stage %s" % (f.get("class"), f.get("stage")))
            c.check(st.field("pnr") is True and "resume" in (st.field("actions") or []), "the stop is after the point of no return and offers `resume`", "pnr true, resume among the actions",
                    "pnr %s, actions %s" % (st.field("pnr"), few(st.field("actions") or [])))
            c.check(st.field("half_upgraded") is not None, "the status says whether the machine is half upgraded", "half_upgraded said", "absent")
        if not (("restored" in read("f3.restore")) or ("present" in read("f3.restore"))):
            invalid("F3: the network was not given back (%s)" % (cell(read("f3.restore").strip(), 120) or "no record"))
        resume = call("f3-resume")
        if resume is None:
            return
        c.check(resume.code == 202, "POST resume is accepted once the network is back", "HTTP 202", "%s %s" % (resume.shown(), cell(resume.error_code, 40)))
        if resume.code != 202:
            c.done()
            return
        if stopped_for_network("f3-recover", "F3 (after the network was given back)"):
            return
        rec = reached(c, "f3-recover", ("awaiting_reboot",), "resume completes the hop up to the restart", ("stopped_at_waypoint", "recovering", "awaiting_reboot", "-"))
        if rec is None:
            return
        last = last_answered(rec)
        if last is not None and last.state == "awaiting_reboot":
            tail = hop_tail_checks(c, "f3", base, "")
            if tail:
                hop_numbers("f3", base, tail, rec, resume, "F3 ", "POST resume")
        c.done()

    def judge_f4():
        if not gate("F4", CASES_FAULTS[3][1]):
            return
        if untestable("F4", CASES_FAULTS[3][1]):
            return
        base, start = fault_base("F4"), call("f4-start")
        if not (base and start):
            return
        c = Case("F4 " + CASES_FAULTS[3][1])
        if start.code != 202:
            c.check(False, "POST start (the hop) is accepted", "HTTP 202", "%s %s" % (start.shown(), few(sorted(start.detail.keys()))))
            c.done()
            return
        if not dpkg_at_work("F4", CASES_FAULTS[3][1]):
            return
        if exists("boot-f4-reset.timeout") or (exists("f4.reset") and not exists("f4-status-interrupted.code")):
            invalid("F4: the guest did not come back (or the core did not answer) after the power cut: its serial log is in the artifacts")
            return
        if not clock_set("F4", "F4-reset"):
            return
        old = read("f4.boot-old").strip()
        bt = [r for r in table("boot-f4-reset.tsv") if len(r) == 3]
        new = bt[-1][2] if bt else "-"
        c.check(old not in ("", "-") and new not in ("", "-") and old != new, "the guest restarted with another boot id after the reset", "a boot id other than %s" % old, new)
        gone = reached(c, "f4-gone", ("interrupted",), "after the power cut the run is found interrupted", ("running", "interrupted", "-", "resuming", "needs_attention"))
        if gone is None:
            return
        st = call("f4-status-interrupted")
        if st:
            c.check(st.field("state") == "interrupted" and st.field("cause") == "power", "interrupted by a power cut", "interrupted, cause power", "%s, cause %s" % (st.field("state"), st.field("cause")))
            c.check(st.field("pnr") is True and st.field("half_upgraded") is True, "the point of no return is behind and the machine is half upgraded", "pnr true, half_upgraded true",
                    "pnr %s, half_upgraded %s" % (st.field("pnr"), st.field("half_upgraded")))
            c.check("resume" in (st.field("actions") or []), "interrupted offers `resume`", "resume among the actions", few(st.field("actions") or []))
        resume = call("f4-resume")
        if resume is None:
            return
        c.check(resume.code == 202, "POST resume is accepted", "HTTP 202", "%s %s" % (resume.shown(), cell(resume.error_code, 40)))
        if resume.code != 202:
            c.done()
            return
        if stopped_for_network("f4-recover", "F4"):
            return
        rec = rows("f4-recover")
        if not rec:
            invalid("polls-f4-recover.tsv is missing: nothing was observed after resume")
            return
        last = last_answered(rec)
        end = last.state if last else "-"
        if end == "awaiting_reboot":
            tail = hop_tail_checks(c, "f4", base, "")
            c.note("the repair after the power cut completed")
            if tail:
                hop_numbers("f4", base, tail, rec, resume, "F4 ", "POST resume")
        else:
            end_status = call("f4-status-end")
            f = end_status.field("failure") if end_status and isinstance(end_status.field("failure"), dict) else {}
            precise = end in TERMINAL_BAD and f.get("class") in CLASSES and bool(f.get("stage"))
            c.check(precise, "the repair that does not complete stops with a precise class and the block that stopped", "a state of the engine's, a class of its vocabulary, a stage",
                    "state %s, class %s, stage %s%s" % (end, f.get("class"), f.get("stage"), ", the time ran out" if timed_out("f4-recover") else ""))
            c.note("FINDING: the repair after a power cut stopped as %s, class %s, block %s: %s" % (end, f.get("class"), f.get("stage"), cell(f.get("detail_line") or "no detail line", 200)))
            notes.append("F4: the repair after the power cut did not complete: %s, class %s, block %s, rc %s; %s" % (end, f.get("class"), f.get("stage"), f.get("rc"), cell(f.get("detail_line") or "no detail line", 240)))
        c.done()

    # ---- the prelude of the faults: reached, not judged -------------------------------------------------------------------------------------

    def judge_prelude():
        for cid in ("C2", "C3", "C4"):
            if "case_%s_done" % cid not in marks:
                invalid("the start state of the faults was not reached: %s did not end" % cid)
                return
        closed = Call("c4-status-closed")
        st = closed.data if isinstance(closed.data, dict) else {}
        if not (closed.answered and closed.code == 200 and st.get("state") == "idle"):
            invalid("the start state of the faults was not reached: after C4 the status is %s, not idle" % (st.get("state") if st else closed.shown()))
            return
        plan, why = plan_state()
        if plan is None:
            invalid("the start state of the faults was not reached: %s" % why)
            return
        refreshed = Call("c3-status-closed")
        rd = refreshed.data if isinstance(refreshed.data, dict) else {}
        if not (refreshed.answered and rd.get("state") == "idle"):
            invalid("the start state of the faults was not reached: the refresh was not closed")
            return
        if not exists("savevm.ok"):
            invalid("the snapshot of the whole machine was not taken: %s" % cell(read("savevm.txt").strip(), 200))
            return
        P("Start state of the faults: Debian 11, refreshed, rehearsed and put back, plan %s live; snapshot taken." % cell(plan.get("plan_id"), 40))

    # ---- the order of the report -----------------------------------------------------------------------------------------------------

    def run_judge(fn, cid=None):
        try:
            fn()
        except Exception as e:      # the evidence was not what a judge expected: the run cannot be trusted, and the exception is named
            invalid("the judgement of %s broke on its evidence: %s: %s" % (cid or fn.__name__, type(e).__name__, cell(e, 200)))

    if leg == "core":
        for fn, cid in ((judge_c1, "C1"), (judge_c2, "C2"), (judge_c3, "C3"), (judge_c4, "C4"), (judge_c5, "C5"), (judge_c6, "C6"), (judge_c7, "C7")):
            run_judge(fn, cid)
    else:
        run_judge(judge_prelude, "the start state")
        if not problems or "case_C4_done" in marks:
            for fn, cid in ((judge_f1, "F1"), (judge_f2, "F2"), (judge_f3, "F3"), (judge_f4, "F4")):
                run_judge(fn, cid)
        else:
            for cid, title in CASES_FAULTS:
                not_testable("%s %s" % (cid, title), "the start state was not reached")

    box = {}
    for ln in read("guest/box-facts").splitlines():
        k, _, v = ln.partition(" ")
        if k:
            box[k] = v.strip()
    if box:
        P("Guest: %s, kernel %s, apt %s, systemd %s, Docker %s, %s CPUs, %s MB." % (cell(read("guest/os").strip() or "Debian 11", 40), cell(box.get("kernel", "?"), 40), cell(box.get("apt", "?"), 40),
                                                                                  cell(box.get("systemd", "?"), 40), cell(box.get("docker", "?"), 20), cell(box.get("cpus", "?"), 4), cell(box.get("memory_mb", "?"), 8)))
    finish()
except SystemExit:
    raise
except Exception as e:     # evidence this script did not expect (a number of four hundred digits, an answer nested a hundred thousand deep): the run cannot be trusted, and the exception is named
    invalid("the report broke on its evidence: %s: %s" % (type(e).__name__, cell(e, 300)))
    finish()
