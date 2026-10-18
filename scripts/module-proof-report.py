#!/usr/bin/env python3
"""Turn what module-proof.sh wrote into verdicts, or say the run is invalid.

    module-proof-report.py <out dir> <d12|u24>

Prints a markdown report (and writes it to <out dir>/report.md).

Exit status: 0 only when no verdict is a FAIL and the run is valid. 1 for a FAIL (the assistant or the core did not do what they say) and 1 for an INVALID RUN (the harness or the guest
broke: the run stopped early, the release has no assistant, a mirror or the network of the runner failed, the guest did not boot, an undo failed, a snapshot of the guest is missing).
An invalid run proves nothing and is never green. NOT TESTABLE is neutral and visible: a case whose precondition this guest could not reach, with the reason; it is never a pass.

What the runner did (module-proof.sh), on a fresh Debian 12 or Ubuntu 24.04 cloud image with the release of ReCasaOS under test installed by its installer, with the virtual machine module
built from a git ref of its repository, put in an archive of the shape of its release, named by a test catalogue, and installed with libvirt's test driver (no KVM), by hand and through
the core's API, with the guest asked for what only it can say (snapshots of the machine, the markers of the log, the units):

  M1  the release carries the assistant (`casaos -v` against the pin and the first core that has `casaos module`)
  M2  the empty state: list, plan and status, as root and as an unprivileged user: one JSON object each, no catalogue, nothing written
  M3  the plan: the checks, the packages against what apt itself says, the files of apt and dpkg unchanged, the same plan as nobody
  M4  the install by hand: the markers, the files with their hashes and modes, the lists, the manifest, the unit, the packages, the route, the host answer and the dashboard's card request
  M5  the API: the list, a second install refused, the plan, the status, a stale plan_id
  M6  the remove through the API: the job unit, the steps, what is gone and what stays, the other services untouched
  M7  the install through the API from clean: the properties of the job unit, the glob of the busy check, the states of the job
  M8  the dirty matrix: a corrupted archive, hostile archives, a unit of the same name, dpkg's lock, the flag of the upgrade, a full disk, a link, a held package, a package marked automatic
  M9  kills at each step, a stop of the unit, a SIGTERM
  M10 a restart of the guest with the module installed, and the remove after it
  M11 the invariance of everything the module does not own

A verdict fails with what was expected and what was recorded. What a verdict cannot be silent about: a case that did not run says so (NOT TESTABLE, with the reason the run stopped), a verdict
that depends on evidence that is missing is no verdict and makes the run INVALID, and any exception of this script, on evidence it did not expect, ends as an INVALID RUN that names it.
"""
import calendar
import json
import math
import os
import re
import sys
import time
from datetime import datetime

if len(sys.argv) != 3:
    print("usage: module-proof-report.py <out dir> <d12|u24>", file=sys.stderr)
    sys.exit(2)
out_dir, leg = sys.argv[1], sys.argv[2]

# ---- what the assistant and the core promise (docs/os-modules.md of the core, pkg/modulecmd, pkg/osmodule) -------------------------------------------------------

MIN_CORE = (0, 4, 75)                         # the first core that has `casaos module`
LEGS = {"d12": ("debian", "12"), "u24": ("ubuntu", "24.04")}
CHECKS = ("catalogue", "os", "arch", "recasaos", "already_installed", "foreign_unit", "busy", "disk", "apt")
INSTALL_STEPS = ("packages", "download", "verify", "unpack", "units", "manifest", "start", "route")
REMOVE_STEPS = ("stop", "files", "manifest", "reload")
ROLLBACK_STEPS = tuple("rollback_" + s for s in REMOVE_STEPS)
JOB_STATES = ("idle", "running", "succeeded", "refused", "failed", "interrupted")
PLAN_ID_RE = re.compile(r"^m1-[0-9a-f]{32}$")
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
TS = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ"
STEP_LINE = re.compile(r"^CASAOS_MODULE_STEP (-|[0-9a-f]{32}) ([a-z][a-z0-9-]*) ([a-z_]+) (begin|done|failed) (%s)$" % TS)
RESULT_LINE = re.compile(r"^CASAOS_MODULE_RESULT (-|[0-9a-f]{32}) ([a-z][a-z0-9-]*) (ok|refused|failed) ([a-z_-]+) ([a-z0-9_-]+) ([a-z_-]+) (%s)$" % TS)
FILES_HEADER = "casaos-module-files 1"
STOP_UNIT_SECONDS = 420                       # TimeoutStopSec of the unit of a job
CLOCK_SLACK = 5.0
ROW_FIELDS = 15
CORE_UNITS = ("casaos.service", "casaos-gateway.service", "casaos-user-service.service", "casaos-message-bus.service", "casaos-local-storage.service",
              "casaos-app-management.service", "rclone.service")
M8_CASES = (("checksum", "M8 checksum_mismatch"), ("dotdot", "M8 unpack_path ../"), ("systemctl", "M8 unpack_path usr/bin/systemctl"), ("varlib", "M8 unpack_path var/lib/casaos"),
            ("usrshare", "M8 unpack_path usr/share/casaos"), ("etcmod", "M8 unpack_path etc/casaos/modules"), ("foreign", "M8 foreign_unit"),
            ("busy_dpkg", "M8 busy (dpkg's lock)"), ("busy_osu", "M8 busy (the flag of the upgrade)"), ("disk", "M8 disk_short"), ("symlink", "M8 link in the area of the install"),
            ("held", "M8 held_package"), ("manual", "M8 a package marked automatic"))
M9_CASES = (("kill_packages", "kill", "packages"), ("kill_download", "kill", "download"), ("kill_verify", "kill", "verify"), ("kill_unpack", "kill", "unpack"),
            ("kill_units", "kill", "units"), ("kill_manifest", "kill", "manifest"), ("kill_start", "kill", "start"), ("kill_route", "kill", "route"),
            ("stop_packages", "stop", "packages"), ("stop_start", "stop", "start"), ("term_start", "term", "start"))

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
    return "%.1f MiB" % (n / (1 << 20))


def fnum(text):
    try:
        v = float(text.strip())
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def finish():
    fails = [v for v in verdicts if v[0] == "FAIL"]
    head = "FAIL and INVALID RUN" if problems and fails else "INVALID RUN" if problems else "FAIL" if fails else "PASS"
    text = ["# Module proof, %s leg: %s" % (leg, head), ""]
    if problems:
        text += ["Nothing below that depends on the missing or untrustworthy evidence can be believed:", ""]
        text += ["- INVALID: " + cell(p, 600) for p in problems] + [""]
    text += lines
    text += ["", "## Verdicts", ""]
    text += ["- **%s** %s%s" % (s, n, (": " + d) if d else "") for s, n, d in verdicts]
    if notes:
        text += ["", "## To read", ""] + ["- " + n for n in notes]
    if numbers:
        text += ["", "## The first real-guest numbers", ""] + ["- %s: %s" % (a, b) for a, b in numbers]
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
        extra = self.error_code
        return "HTTP %d%s" % (self.code, " " + cell(extra, 60) if extra else "")


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


class Gx:
    """a verb of the guest: <name>.out, .err, .rc (255 is ssh's own), .t0, .t1"""

    def __init__(self, name):
        self.name = name
        self.ran = exists(name + ".rc")
        rc = read(name + ".rc").strip()
        self.rc = int(rc) if re.fullmatch(r"\d+", rc) else -1
        self.out = read(name + ".out")
        self.err = read(name + ".err")
        self.t0, self.t1 = fnum(read(name + ".t0")), fnum(read(name + ".t1"))

    @property
    def answered(self):
        return self.ran and self.rc not in (-1, 255)

    @property
    def lines(self):
        return [ln for ln in self.out.split("\n") if ln.strip()]

    def obj(self):
        """the standard output as the one JSON object it must be, or None"""
        try:
            v = json.loads(self.out)
        except ValueError:
            return None
        return v if isinstance(v, dict) else None

    def one_object(self):
        """the standard output is one JSON object and nothing else, on one line"""
        return self.obj() is not None and self.out.strip().count("\n") == 0

    def shown(self):
        if not self.ran:
            return "no run"
        if not self.answered:
            return "no answer (rc %s %s)" % (self.rc, cell(self.err, 100))
        return "exit %d" % self.rc


def gxc(name, why=""):
    """a verb of the guest that the judgement needs: None, with the run made invalid, when it did not run or ssh itself failed"""
    g = Gx(name)
    if not g.ran:
        invalid("the guest verb %s%s did not run: no evidence" % (name, " (" + why + ")" if why else ""))
        return None
    if not g.answered:
        invalid("the guest verb %s%s got no answer: rc %s %s" % (name, " (" + why + ")" if why else "", g.rc, cell(g.err, 120)))
        return None
    return g


class Row:
    """a poll of the status of the module: t0, t1, HTTP, curl, job state, step, code, undo, kept packages, installed, version, unit active, unit enabled, route answers, the job unit"""

    def __init__(self, cells):
        (self.t0, self.t1, self.http, self.curl, self.state, self.step, self.code, self.undo, self.kept, self.installed, self.version, self.active, self.enabled,
         self.route, self.unit) = cells
        self.t0, self.t1 = fnum(self.t0), fnum(self.t1)

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
    """the job states the rows went through, one after the other, without repeats, the polls that got no answer left out"""
    out = []
    for r in rs:
        if r.answered() and r.state not in ("-",) and (not out or out[-1] != r.state):
            out.append(r.state)
    return out


def last_answered(rs):
    for r in reversed(rs):
        if r.answered():
            return r
    return None


def table(path):
    return [ln.split("\t") for ln in read(path).splitlines()]


# ---- the snapshots of the guest ---------------------------------------------------------------------------------------------------------

class Snap:
    """a snapshot of the guest: snap-<tag>.<section>"""

    def __init__(self, tag):
        self.tag = tag
        self.f = {}
        for ln in read("snap-%s.facts" % tag).splitlines():
            k, _, v = ln.partition(" ")
            if k:
                self.f[k] = v.strip()
        self.pkgs, self.auto = {}, set()
        for ln in read("snap-%s.pkgs" % tag).splitlines():
            w = ln.split()
            if len(w) == 3 and w[0] == "pkg":
                self.pkgs[w[1]] = w[2]
            elif len(w) == 2 and w[0] == "auto":
                self.auto.add(w[1])
        self.files = {}
        for ln in read("snap-%s.files" % tag).splitlines():
            w = ln.split("\t")
            if len(w) == 5:
                self.files[w[0]] = tuple(w[1:])
        self.lists_files = read("snap-%s.lists_files" % tag)
        self.lists_dirs = read("snap-%s.lists_dirs" % tag)
        self.lists_version = read("snap-%s.lists_version" % tag)
        self.tmp = [ln for ln in read("snap-%s.tmp" % tag).splitlines() if ln]
        self.state = {}
        for ln in read("snap-%s.state" % tag).splitlines():
            w = ln.split("\t")
            if w and w[0]:
                self.state[w[0]] = tuple(w[1:])
        self.apt = {}
        for ln in read("snap-%s.aptstate" % tag).splitlines():
            w = ln.split("\t")
            if w and w[0]:
                self.apt[w[0]] = tuple(w[1:])
        self.reserved = {}
        for ln in read("snap-%s.reserved" % tag).splitlines():
            w = ln.split("\t")
            if len(w) == 2:
                self.reserved[w[0]] = w[1]
        self.units, self.jobs, self.ulist = {}, [], {}
        for ln in read("snap-%s.units" % tag).splitlines():
            w = ln.split(" ")
            if w and w[0] == "unit" and len(w) >= 3:
                self.units[w[1]] = tuple(w[2:])
            elif w and w[0] == "job":
                self.jobs.append(ln)
            elif w and w[0] == "list" and len(w) == 4:
                self.ulist[w[1]] = (w[2], w[3])
        self.etccasaos = {}
        for ln in read("snap-%s.etccasaos" % tag).splitlines():
            w = ln.split("\t")
            if len(w) == 2:
                self.etccasaos[w[0]] = w[1]
        self.names = set(ln for ln in read("snap-%s.names" % tag).splitlines() if ln)
        self.dpkg = {}
        for ln in read("snap-%s.dpkg" % tag).splitlines():
            w = ln.split("\t")
            if len(w) == 3:
                self.dpkg[w[0]] = (w[1], w[2].strip())
        self.etc = {}
        for ln in read("snap-%s.etc" % tag).splitlines():
            w = ln.split("\t")
            if len(w) == 2:
                self.etc[w[0]] = w[1]

    def get(self, key, default=""):
        return self.f.get(key, default)

    def installed(self):
        return {n: v for n, (v, st) in self.dpkg.items() if st.startswith("ii")}

    def footprint(self):
        """what the module has on the box: its files, its lists, its manifest, the link that enables its unit (not the proof's own configuration)"""
        found = sorted(p for p in self.files if not p.endswith("/virt-management.conf"))
        for name, text in (("lists_files", self.lists_files), ("lists_dirs", self.lists_dirs), ("lists_version", self.lists_version)):
            if text.strip():
                found.append("<" + name + ">")
        return found


def snap(tag, why=""):
    if exists("snap-%s.failed" % tag):
        invalid("the snapshot %s%s could not be had: %s" % (tag, " (" + why + ")" if why else "", cell(read("snap-%s.failed" % tag).strip(), 200)))
        return None
    if not exists("snap-%s.facts" % tag):
        invalid("the snapshot %s%s is missing: no evidence" % (tag, " (" + why + ")" if why else ""))
        return None
    s = Snap(tag)
    if not s.f.get("boot_id") or not s.f.get("os_id") or not s.names or not s.apt or not s.pkgs:
        invalid("the snapshot %s%s is not a whole one (boot id, system, names, apt files or packages are missing)" % (tag, " (" + why + ")" if why else ""))
        return None
    return s


def one(text):
    """the first line of a recorded text, trimmed"""
    for ln in text.splitlines():
        if ln.strip():
            return ln.strip()
    return ""


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


# ---- the markers of the log ------------------------------------------------------------------------------------------------------------

class Markers:
    """the lines of a run of the assistant (its standard output, or its log): the STEP lines, the RESULT lines, and the sentences between them"""

    def __init__(self, text):
        self.lines = text.split("\n")
        if self.lines and self.lines[-1] == "":
            self.lines.pop()
        self.steps, self.results, self.order, self.bad = [], [], [], []
        for ln in self.lines:
            if ln.startswith("CASAOS_MODULE_STEP "):
                m = STEP_LINE.match(ln)
                if m:
                    self.steps.append(m.groups())
                    self.order.append(("step", m.groups()))
                else:
                    self.bad.append(ln)
            elif ln.startswith("CASAOS_MODULE_RESULT "):
                m = RESULT_LINE.match(ln)
                if m:
                    self.results.append(m.groups())
                    self.order.append(("result", m.groups()))
                else:
                    self.bad.append(ln)

    def sequence(self):
        return [(s[2], s[3]) for s in self.steps]

    def nonces(self):
        return sorted(set([s[0] for s in self.steps] + [r[0] for r in self.results]))

    def sentences(self):
        """the lines that are not markers and not the output of a tool"""
        return [ln for ln in self.lines if not ln.startswith("CASAOS_MODULE_") and not ln.startswith("| ")]

    def times(self):
        out = []
        for _, g in self.order:
            out.append(g[-1])
        return out


def parse_time(s):
    try:
        return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def markers_ok(c, m, label, steps, nonce):
    """the markers of a whole run that went through: every step begins and is done, in order, then `done`, and one RESULT ok at the end; the same name and nonce; the times in order"""
    want = []
    for s in steps:
        want += [(s, "begin"), (s, "done")]
    want += [("done", "done")]
    c.check(not m.bad, "%s: every marker line has the shape of the grammar" % label, "none other", few(m.bad, 3))
    c.check(m.sequence() == want, "%s: the steps go in order, each begun and done" % label, " ".join("%s:%s" % x for x in want), " ".join("%s:%s" % x for x in m.sequence()) or "no step line")
    c.check(len(m.results) == 1 and m.results[0][2:6] == ("ok", "done", "-", "-"), "%s: one RESULT line, ok at the step done" % label, "ok done - -",
            few(" ".join(r[2:6]) for r in m.results) or "none")
    c.check(bool(m.order) and m.order[-1][0] == "result", "%s: the RESULT line is the last marker" % label, "the last", m.order[-1][0] if m.order else "no marker")
    c.check(all(x[1] == "virt" for _, x in m.order), "%s: every marker names the module" % label, "virt", few(sorted(set(x[1] for _, x in m.order))))
    ns = m.nonces()
    if nonce == "-":
        c.check(ns == ["-"], "%s: the nonce is - (a run by hand)" % label, "-", few(ns))
    else:
        c.check(len(ns) == 1 and NONCE_RE.match(ns[0]) is not None, "%s: one nonce of 32 hexadecimal digits, the core's" % label, "one nonce", few(ns))
    ts = [parse_time(t) for t in m.times()]
    c.check(all(t is not None for t in ts) and ts == sorted(ts), "%s: the times of the markers do not go back" % label, "in order", few(m.times(), 4))
    return m


def result_is(c, m, label, verdict_, step, code, undo=None):
    got = m.results[0][2:6] if len(m.results) == 1 else ()
    ok = len(m.results) == 1 and got[0] == verdict_ and got[1] == step and got[2] == code and (undo is None or got[3] == undo)
    c.check(ok, "%s: the RESULT line says %s %s %s%s" % (label, verdict_, step, code, " " + undo if undo else ""), "one RESULT line", " ".join(got) if got else "%d RESULT lines" % len(m.results))
    return ok


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


def version_tuple(v):
    m = re.fullmatch(r"v?([0-9]+)\.([0-9]+)\.([0-9]+)", v.strip())
    return tuple(int(x) for x in m.groups()) if m else None


def jget(o, *path):
    for k in path:
        if isinstance(o, dict):
            o = o.get(k)
        else:
            return None
    return o


def hexsha(s):
    return re.fullmatch(r"[0-9a-f]{64}", s or "") is not None


def parse_files_list(text):
    """the list of the files of a module: the header and the entries (kind, want, path); None when it is not whole"""
    ls = text.split("\n")
    if ls and ls[-1] == "":
        ls.pop()
    if not ls or ls[0] != FILES_HEADER:
        return None
    entries = []
    for ln in ls[1:]:
        w = ln.split(" ")
        if len(w) != 3 or w[0] not in ("f", "l"):
            return None
        entries.append((w[0], w[1], w[2]))
    return entries


def plan_checks(plan):
    out = []
    for ck in (plan.get("checks") if isinstance(plan, dict) and isinstance(plan.get("checks"), list) else []):
        if isinstance(ck, dict):
            out.append((ck.get("code"), ck.get("ok"), ck.get("detail") if isinstance(ck.get("detail"), str) else ""))
    return out


def check_of(plan, code):
    for cd, ok, detail in plan_checks(plan):
        if cd == code:
            return ok, detail
    return None, ""


def module_entry(listing, name="virt"):
    mods = listing.get("modules") if isinstance(listing, dict) else None
    for m in mods if isinstance(mods, list) else []:
        if isinstance(m, dict) and m.get("name") == name:
            return m
    return None


try:
    if leg not in LEGS:
        invalid("the leg is d12 or u24, not %r" % leg)
        finish()
    marks, first_mark = timeline()
    aborted = " ".join(read("aborted").split())
    guest_aborted = " ".join(read("guest/aborted").split())
    kit = None
    try:
        kit = json.loads(read("kit.json")) if read("kit.json").strip() else None
    except ValueError:
        kit = None
    if guest_aborted:
        invalid("the guest's preparation stopped: " + guest_aborted[:300])
    elif not exists("guest/prepared") and not read("finished").strip():
        invalid("the guest's preparation left no `prepared` file: it did not reach its end")
    if aborted:
        invalid("the run stopped early: " + aborted[:400])
    for ln in read("invalid.txt").splitlines():
        r = ln.split("\t", 1)
        invalid("a step of the harness failed or could not be undone (%s): %s" % (r[0], r[1] if len(r) > 1 else ""))
    if not isinstance(kit, dict) or not kit.get("files") or not kit.get("packages") or not kit.get("tag"):
        invalid("kit.json is missing or is not the kit's: the module was not built, or the run did not copy it")
        kit = {"files": [], "packages": [], "tag": "", "unit": "", "route": "", "archive": {}, "name": "virt", "variants": {}}
    osr = osrelease("guest/os-release")
    want_os = LEGS.get(leg, ("", ""))
    if not osr.get("ID"):
        invalid("guest/os-release is missing or empty: the guest did not say what system it is")
    elif (osr.get("ID"), osr.get("VERSION_ID")) != want_os:
        invalid("the guest is %s %s, the leg %s needs %s %s" % (osr.get("ID"), osr.get("VERSION_ID"), leg, want_os[0], want_os[1]))
    if read("leg").strip() != leg:
        invalid("the runner says it ran leg %r, the report is asked about %r" % (read("leg").strip(), leg))
    finished = timestamp(read("finished"))
    if not read("finished").strip() and not aborted:
        invalid("the runner did not reach the end of its run (no `finished` file) and did not say why")
    if not marks:
        invalid("timeline.tsv is missing or empty: the run left no marker")
    elif finished is not None and first_mark is not None and not (first_mark - 86400 <= finished + CLOCK_SLACK):
        invalid("the markers and the end of the run are not of the same day")
    pins = {}
    for ln in read("guest/pins").splitlines():
        k, _, v = ln.partition("=")
        if k:
            pins[k.strip()] = v.strip().strip("\"'")
    tag = read("tag").strip()
    if tag:
        P("Installer release under test: `%s`." % cell(tag, 40))
    if kit.get("tag"):
        P("Module: `%s` from `%s`, built as `%s` (commit %s); archive %s, sha256 %s." % (kit.get("name"), kit.get("component"), kit.get("tag"), cell(kit.get("commit", "?"), 12),
                                                                                      mib(kit.get("archive", {}).get("bytes", 0)), cell(kit.get("archive", {}).get("sha256", "?"), 16)))
    bv = read("binary-version").strip()
    if bv and not re.match(r"^v?\d+\.\d+\.\d+", bv):
        invalid("the program that was built does not say its version when it is asked (`-v`): %s" % cell(bv, 100))

    PKGS = list(kit.get("packages", []))
    UNIT = kit.get("unit", "")
    TAGV = kit.get("tag", "")
    NOISE_NAME = re.compile(r"/session-[0-9a-z]+\.scope|^/var/tmp/systemd-private-")   # (those of the services are named by the boot, which M10 changes)

    def gate(cid, title):
        """whether the case ran to its end and is to be judged; a case that did not run says so, with the reason the run stopped"""
        if "case_%s_done" % cid in marks:
            return True
        if "case_%s_start" % cid in marks:
            invalid("%s (%s) began and did not end: the run stopped in it" % (cid, title))
            return False
        not_testable(title if "_" in cid else "%s %s" % (cid, title), "did not run: " + (("the run stopped early (%s)" % aborted[:200]) if aborted else "the run ended before it"))
        return False

    def noise_of(a, b):
        return set(n for n in (a.names ^ b.names) if not NOISE_NAME.search(n))

    def plan_of(g, label):
        """the plan object a recorded `casaos module plan` printed, or None with the run made invalid"""
        o = g.obj()
        if o is None:
            return None
        return o

    # ---- M1 ------------------------------------------------------------------------------------------------------------------------------

    def judge_m1():
        if not gate("M1", "the release carries the assistant"):
            return
        g = gxc("m1-version")
        if g is None:
            return
        ver = one(g.out)
        t = version_tuple(ver)
        pin = pins.get("CASAOS_TAG", "")
        if t is None:
            invalid("M1: `casaos -v` says %r, which is not a release of the core (vX.Y.Z)" % cell(ver, 60))
            return
        if pin and pin != ver:
            invalid("M1: the core on the guest (%s) is not the one the release pins (%s)" % (ver, pin))
            return
        if t < MIN_CORE:
            invalid("M1: the core of this release (%s) is older than v%d.%d.%d, the first that has `casaos module`: it has no assistant, and an older core given `module` starts as a server"
                    % (ver, MIN_CORE[0], MIN_CORE[1], MIN_CORE[2]))
            return
        verdicts.append(("PASS", "M1 the release carries the assistant", "core %s%s, v%d.%d.%d is the first with `casaos module`" %
                         (ver, " (the pin of the release)" if pin else " (no pin was recorded)", MIN_CORE[0], MIN_CORE[1], MIN_CORE[2])))

    # ---- M2 ------------------------------------------------------------------------------------------------------------------------------

    def judge_m2():
        if not gate("M2", "the empty state"):
            return
        ctl_a, ctl_b, before, after = snap("m2-ctl-a"), snap("m2-ctl-b"), snap("m2-before"), snap("m2-after")
        lr, ln_, pr, pn, sr, sa = (gxc(n) for n in ("m2-list-root", "m2-list-nobody", "m2-plan-root", "m2-plan-nobody", "m2-status-root", "m2-status-all"))
        if not all((ctl_a, ctl_b, before, after, lr, ln_, pr, pn, sr, sa)):
            return
        shipped = before.get("catalogue_file") == "yes"
        c = Case("M2 list")
        for g, who in ((lr, "root"), (ln_, "nobody")):
            c.check(g.rc == 0, "list as %s ends with status 0" % who, "exit 0", g.shown())
            c.check(g.one_object(), "list as %s prints one JSON object on the standard output and nothing else" % who, "one JSON object", cell(g.out, 120))
            o = g.obj() or {}
            cat = o.get("catalogue") if isinstance(o.get("catalogue"), dict) else {}
            c.check(isinstance(o.get("modules"), list) and "ok" in cat, "list as %s has `catalogue` and `modules`" % who, "both", few(sorted(o.keys())))
            if not shipped:
                c.check(cat.get("ok") is False and cat.get("error") == "none" and o.get("modules") == [], "list as %s says that there is no catalogue and no module" % who,
                        "catalogue.ok false, error none, modules []", "ok %s, error %s, modules %s" % (cat.get("ok"), cat.get("error"), cell(json.dumps(o.get("modules")), 60)))
        c.done()
        if shipped:
            not_testable("M2 no catalogue", "this release ships a catalogue (/usr/share/casaos/modules/catalogue.json): the answer of a release without one is not asked for")
        c = Case("M2 plan")
        for g, who in ((pr, "root"), (pn, "nobody")):
            o = g.obj() or {}
            c.check(g.rc == 0 and g.one_object(), "plan as %s prints one JSON object and ends with status 0" % who, "one object, exit 0", "%s, %s" % (g.shown(), cell(g.out, 80)))
            if not shipped:
                cks = plan_checks(o)
                c.check(o.get("refused") == "catalogue", "plan as %s is refused `catalogue`" % who, "refused catalogue", cell(o.get("refused")))
                c.check(bool(cks) and cks[0][0] == "catalogue" and cks[0][1] is False, "the first check of the plan as %s is `catalogue`, and it is not ok" % who, "catalogue, not ok", few("%s:%s" % (a, b) for a, b, _ in cks[:2]))
                c.check(not o.get("plan_id"), "a plan that is refused has no plan_id (as %s)" % who, "none", cell(o.get("plan_id")))
        c.done()
        c = Case("M2 status")
        so = sr.obj() or {}
        c.check(sr.rc == 0 and sr.one_object(), "status of the module prints one JSON object and ends with status 0", "one object, exit 0", "%s, %s" % (sr.shown(), cell(sr.out, 80)))
        c.check(so.get("installed") is False and jget(so, "unit", "active") is False and jget(so, "unit", "enabled") is False and jget(so, "route", "answers") is False,
                "a module that is not installed is not installed, not active, not enabled and does not answer", "all false", cell(sr.out, 120))
        ao = sa.obj() or {}
        c.check(sa.rc == 0 and ao.get("modules") == [], "status with no name lists no module", "{\"modules\": []}", cell(sa.out, 80))
        c.done()
        # nothing is written: the names of the file system that the three commands could have touched, against what the box changes by itself (the control)
        c = Case("M2 nothing written")
        noise = noise_of(ctl_a, ctl_b)
        delta = set(n for n in (before.names ^ after.names) if not NOISE_NAME.search(n))
        c.check(not (delta - noise), "no name of the file system changed that the control did not show changing by itself", "none", few(sorted(delta - noise)))
        c.check(before.state == after.state, "/etc/casaos/modules, /var/lib/casaos/modules and /var/log/casaos/module-* are what they were (names, times, sizes), or are still not there",
                "the same lines", few(sorted(set(before.state.items()) ^ set(after.state.items()))))
        c.check(not before.footprint() and not after.footprint(), "the box has nothing of the module before and after", "none", few(before.footprint() + after.footprint()))
        c.check(before.tmp == after.tmp, "the area of the install holds what it held", "the same", few(before.tmp + after.tmp))
        c.check(before.apt == after.apt, "the files of apt and dpkg (extended_states, history.log, status, eipp.log.xz) are as they were", "the same", few(sorted(set(before.apt.items()) ^ set(after.apt.items()))))
        c.note("what the box changes by itself (control): %d names" % len(noise))
        c.done()

    # ---- M3 ------------------------------------------------------------------------------------------------------------------------------

    def parse_aptsim(text):
        d = {"new": [], "upgrade": [], "held": [], "manual": [], "rc": None, "uris": None, "need": []}
        for ln in text.splitlines():
            w = ln.split(" ", 1)
            if w[0] in ("new", "upgrade", "held", "manual") and len(w) == 2:
                d[w[0]].append(w[1].strip())
            elif w[0] == "rc" and len(w) == 2 and w[1].strip().isdigit():
                d["rc"] = int(w[1])
            elif w[0] == "uris_total" and len(w) == 2 and w[1].strip().isdigit():
                d["uris"] = int(w[1])
            elif w[0] == "need":
                d["need"].append(ln[5:])
        return d

    def judge_m3():
        if not gate("M3", "the plan"):
            return
        before, after, sim_after = snap("m3-before"), snap("m3-after"), snap("m3-after-sim")
        pr, pn, lr, ap = (gxc(n) for n in ("m3-plan-root", "m3-plan-nobody", "m3-list-root", "m3-apt"))
        if not all((before, after, sim_after, pr, pn, lr, ap)):
            return
        sim = parse_aptsim(ap.out)
        if sim["rc"] != 0 or ap.rc != 0:
            invalid("M3: apt could not be asked what installing the packages would do (rc %s): %s" % (sim["rc"], cell(ap.out, 200)))
            return
        plan = pr.obj() or {}
        c = Case("M3 plan checks")
        c.check(pr.rc == 0 and pr.one_object(), "plan prints one JSON object and ends with status 0", "one object, exit 0", "%s, %s" % (pr.shown(), cell(pr.out, 80)))
        cks = plan_checks(plan)
        c.check([x[0] for x in cks] == list(CHECKS), "the checks are %s, in this order" % ", ".join(CHECKS), ", ".join(CHECKS), few(x[0] for x in cks))
        c.check(all(x[1] is True for x in cks) and bool(cks), "every check is ok", "all ok", few("%s:%s %s" % (a, b, d) for a, b, d in cks if b is not True))
        c.check(plan.get("refused") is None, "the plan is not refused", "refused null", cell(plan.get("refused")))
        c.check(isinstance(plan.get("plan_id"), str) and PLAN_ID_RE.match(plan.get("plan_id")) is not None, "plan_id is m1- and 32 hexadecimal digits", "m1-<32 hex>", cell(plan.get("plan_id")))
        c.check(plan.get("name") == kit.get("name") and plan.get("tag") == TAGV and plan.get("tier") == "community", "the plan is of the module, at the tag of the catalogue, tier community",
                "%s %s community" % (kit.get("name"), TAGV), "%s %s %s" % (plan.get("name"), plan.get("tag"), plan.get("tier")))
        c.check(jget(plan, "archive", "url") == kit.get("archive", {}).get("url"), "the archive is the one the catalogue names", cell(kit.get("archive", {}).get("url")), cell(jget(plan, "archive", "url")))
        c.done()
        pk = plan.get("packages") if isinstance(plan.get("packages"), dict) else {}
        c = Case("M3 plan packages")
        to_install = pk.get("to_install") if isinstance(pk.get("to_install"), list) else None
        to_upgrade = pk.get("to_upgrade") if isinstance(pk.get("to_upgrade"), list) else None
        c.check(to_install is not None and sorted(to_install) == sorted(sim["new"]), "to_install is what `apt-get -s install --no-install-recommends` says on the guest", few(sorted(sim["new"]), 6) or "nothing",
                few(sorted(to_install or []), 6) or "nothing")
        c.check(to_upgrade is not None and sorted(to_upgrade) == sorted(sim["upgrade"]), "to_upgrade is what apt says", few(sorted(sim["upgrade"]), 6) or "nothing", few(sorted(to_upgrade or []), 6) or "nothing")
        have = set(n for n, a in before.pkgs.items() if a.startswith("ii"))
        c.check(bool(PKGS) and set(PKGS) <= set(to_install or []) | have, "every package of the manifest is installed by the plan or already there", few(PKGS), few(sorted(set(PKGS) - set(to_install or []) - have)))
        c.check(pk.get("known") is True, "apt gave the sizes (known)", "known true", cell(pk.get("known")))
        dl, ex = pk.get("download_bytes"), pk.get("extra_bytes")
        c.check(isinstance(dl, int) and isinstance(ex, int) and (not to_install or (dl > 0 and ex > 0)), "the sizes are numbers, and not zero for packages to install", "download and extra above 0", "%s, %s" % (dl, ex))
        if isinstance(dl, int) and sim["uris"]:
            # (the plan reads the total that apt gives, which holds what the cache of apt has already; --print-uris lists what is missing: the plan's number is not less than that, give or take apt's rounding)
            c.check(dl >= sim["uris"] - max(0.02 * sim["uris"], 1 << 20), "download_bytes is at least what `apt-get --print-uris` says is to be fetched, within the rounding of apt's own words",
                    "%d bytes or more" % sim["uris"], "%d bytes" % dl)
        else:
            c.note("no size of --print-uris to hold download_bytes against")
        c.note("%d to install, %d to upgrade, %s to download, %s on the disk" % (len(to_install or []), len(to_upgrade or []), mib(dl) if isinstance(dl, int) else "?", mib(ex) if isinstance(ex, int) else "?"))
        c.done()
        if isinstance(dl, int):
            number("plan", "%d packages to install, %d to upgrade, %s to download, %s extra on the disk; archive %s" % (len(to_install or []), len(to_upgrade or []), mib(dl), mib(ex or 0),
                                                                                                                 mib(kit.get("archive", {}).get("bytes", 0))))
        if pr.t0 is not None and pr.t1 is not None:
            number("time of the plan", "%s as root" % secs(pr.t1 - pr.t0))
        c = Case("M3 plan writes nothing")
        for key in ("/var/lib/apt/extended_states", "/var/log/apt/history.log", "/var/lib/dpkg/status", "/var/log/apt/eipp.log.xz"):
            c.check(before.apt.get(key) is not None and before.apt.get(key) == after.apt.get(key), "%s is as it was after the plans" % key, cell(before.apt.get(key)), cell(after.apt.get(key)))
            c.check(before.apt.get(key) == sim_after.apt.get(key), "%s is as it was after the proof's own simulation, too (so that it proves the plan and not itself)" % key,
                    cell(before.apt.get(key)), cell(sim_after.apt.get(key)))
        c.check(before.get("dpkg_audit") == "0" and after.get("dpkg_audit") == "0", "dpkg --audit finds nothing", "0", "%s, %s" % (before.get("dpkg_audit"), after.get("dpkg_audit")))
        c.check({n for n in before.names if n.startswith("/var/tmp/")} == {n for n in after.names if n.startswith("/var/tmp/")}, "/var/tmp holds what it held (the sandbox of the plan is gone)", "the same",
                few(sorted({n for n in before.names ^ after.names if n.startswith("/var/tmp/")})))
        c.check(before.state == after.state, "the state of the assistant is as it was", "the same", few(sorted(set(before.state.items()) ^ set(after.state.items()))))
        c.check(not after.footprint(), "the box has nothing of the module after the plans", "none", few(after.footprint()))
        c.done()
        c = Case("M3 plan as nobody")
        po = pn.obj() or {}
        c.check(pn.rc == 0 and pn.one_object(), "plan as nobody prints one JSON object and ends with status 0", "one object, exit 0", "%s, %s" % (pn.shown(), cell(pn.out, 80)))
        ok, detail = check_of(po, "busy")
        c.check(ok is False and detail.startswith("not_checked"), "the check `busy` is marked as not made (it needs root)", "ok false, not_checked", "ok %s, %s" % (ok, cell(detail, 80)))
        c.check(po.get("refused") is None, "the rest of the plan is made and is not refused", "refused null", cell(po.get("refused")))
        c.check(po.get("plan_id") == plan.get("plan_id") and bool(plan.get("plan_id")), "the plan_id is the root's", cell(plan.get("plan_id")), cell(po.get("plan_id")))
        c.check(jget(po, "packages", "to_install") == to_install, "the packages are the root's", few(to_install or []), few(jget(po, "packages", "to_install") or []))
        c.done()
        c = Case("M3 list")
        lo = lr.obj() or {}
        me = module_entry(lo)
        c.check(lr.rc == 0 and lr.one_object(), "list with the test catalogue prints one JSON object", "one object, exit 0", "%s, %s" % (lr.shown(), cell(lr.out, 80)))
        c.check(jget(lo, "catalogue", "ok") is True, "the test catalogue is read", "catalogue.ok true", cell(jget(lo, "catalogue")))
        c.check(me is not None and me.get("installed") is None and jget(me, "system", "supported") is True and me.get("catalogue") is True and me.get("tier") == "community" and me.get("tag") == TAGV,
                "the module is listed: not installed, supported by this system, in the catalogue, community, at its tag", "all of them", cell(json.dumps(me), 200))
        c.check(me is not None and me.get("title") == kit.get("title"), "the title is the manifest's", cell(kit.get("title")), cell(me.get("title") if me else None))
        c.done()

    # ---- the module as an install leaves it -----------------------------------------------------------------------------------------------

    def judge_installed(c, s, label):
        """the files exactly as the archive says, with their modes and owners, the lists, the manifest, the version, the unit enabled and active, the packages, the area of the install empty"""
        name = kit.get("name", "virt")
        for f in kit.get("files", []):
            e = s.files.get(f["path"])
            c.check(e is not None and e[0] == "f", "%s: %s is a file" % (label, f["path"]), "a file", cell(e))
            if e is not None and e[0] == "f":
                c.check(re.fullmatch(r"[0-7]{3,4}", e[1]) is not None and int(e[1], 8) == int(f["mode"], 8) & 0o755, "%s: the mode of %s is the archive's" % (label, f["path"]), f["mode"], e[1])
                c.check(e[2] == "0:0", "%s: %s is root's" % (label, f["path"]), "0:0", e[2])
                c.check(e[3] == f["sha256"], "%s: the content of %s is the archive's" % (label, f["path"]), f["sha256"][:16], e[3][:16])
        link = s.files.get("/etc/systemd/system/multi-user.target.wants/" + UNIT)
        c.check(link is not None and link[0] == "l" and link[3].endswith("/" + UNIT), "%s: the unit is enabled by a link in multi-user.target.wants" % label, "a link to the unit", cell(link))
        entries = parse_files_list(s.lists_files)
        c.check(entries is not None, "%s: the list of the files is whole (its header, then one `f <sha256> <path>` to a line)" % label, "casaos-module-files 1 and its entries", cell(s.lists_files, 100))
        if entries is not None:
            got = {p: w for k, w, p in entries if k == "f"}
            want = {f["path"]: f["sha256"] for f in kit.get("files", [])}
            c.check(len(entries) == len(got) and got == want, "%s: the list names exactly the files of the archive, with their sha256" % label, few(sorted(want)), few(sorted(got)))
            c.check(bool(entries) and entries[-1][2].endswith("/" + UNIT), "%s: the unit is last in the list" % label, "the unit", cell(entries[-1][2] if entries else "none"))
        dirs = [ln for ln in s.lists_dirs.split("\n") if ln]
        okd = all(d.startswith("/") and d == os.path.normpath(d) for d in dirs) and len(dirs) == len(set(dirs))
        c.check(okd, "%s: the list of the directories (%d) holds absolute, clean paths, none twice" % (label, len(dirs)), "paths", few(dirs))
        c.check(s.lists_version == TAGV + "\n", "%s: the version file says %s" % (label, TAGV), TAGV, cell(s.lists_version))
        mf = s.files.get("/etc/casaos/modules/%s.json" % name)
        c.check(mf is not None and mf[0] == "f" and mf[1] == "644" and mf[2] == "0:0" and mf[3] == kit.get("manifest_sha256"), "%s: the manifest is the module.json of the archive, root's, 0644" % label,
                "module.json", cell(mf))
        for ext in ("files", "version"):
            e = s.files.get("/var/lib/casaos/modules/%s.%s" % (name, ext))
            c.check(e is not None and e[1] == "644" and e[2] == "0:0", "%s: the %s list is root's, 0644" % (label, ext), "0644 0:0", cell(e))
        conf = s.files.get(kit.get("conf_path", ""))
        c.check(conf is not None and conf[3] == kit.get("conf_sha256"), "%s: the configuration of the administrator (libvirt's test driver) was not touched" % label, cell(kit.get("conf_sha256", "")[:16]), cell(conf))
        c.check(s.get("unit_enabled") == "enabled" and s.get("unit_active") == "active", "%s: the unit is enabled and active" % label, "enabled, active", "%s, %s" % (s.get("unit_enabled"), s.get("unit_active")))
        c.check(bool(PKGS) and all(s.pkgs.get(p, "").startswith("ii") for p in PKGS), "%s: the packages of the manifest are installed" % label, few(PKGS), few("%s=%s" % (p, s.pkgs.get(p)) for p in PKGS if not s.pkgs.get(p, "").startswith("ii")))
        c.check(s.get("tmp_entries") == "0", "%s: the area of the install is empty" % label, "0", s.get("tmp_entries"))
        c.check(s.get("dpkg_audit") == "0", "%s: dpkg --audit finds nothing" % label, "0", s.get("dpkg_audit"))

    def step_seconds(m):
        """seconds from the begin to the done of each step, from the times of the markers (to the second)"""
        out, begun = [], {}
        for _, g in m.order:
            if len(g) == 5:
                _, _, step, phase, ts = g
                t = parse_time(ts)
                if phase == "begin":
                    begun[step] = t
                elif t is not None and begun.get(step) is not None:
                    out.append((step, t - begun[step]))
        return out

    # ---- M4 ------------------------------------------------------------------------------------------------------------------------------

    def judge_m4():
        if not gate("M4", "install by hand"):
            return
        conf, inst, log, st, lst = (gxc(n) for n in ("m4-conf", "m4-install", "m4-log", "m4-status", "m4-list"))
        before, after = snap("m4-before"), snap("m4-after")
        host, noauth, ghost = call("m4-host"), call("m4-host-noauth"), call("m4-ghost")
        if not all((conf, inst, log, st, lst, before, after, host, noauth, ghost)):
            return
        pid = one(read("m4-plan-id"))
        c = Case("M4 install markers")
        c.check(conf.rc == 0 and conf.out.startswith("ok"), "libvirt's test driver was put in the configuration before the unit started", "ok <sha256>", cell(conf.out + conf.err, 100))
        c.check(PLAN_ID_RE.match(pid) is not None, "the install was given the plan_id of the plan of M3 (--plan-id)", "m1-<32 hex>", pid)
        c.check(inst.rc == 0, "the install ends with status 0", "exit 0", "%s %s" % (inst.shown(), cell(inst.out[-160:])))
        m = markers_ok(c, Markers(inst.out), "install", INSTALL_STEPS, "-")
        ml = markers_ok(c, Markers(log.out), "log", INSTALL_STEPS, "-")
        c.check([ln for ln in ml.lines if not ln.startswith("| ")] == m.lines, "the log holds the lines of the standard output, and the output of the tools behind `| `", "the same lines", "differs")
        c.check("installed: %s %s" % (kit.get("name"), TAGV) in m.lines, "the run says what it installed", "installed: %s %s" % (kit.get("name"), TAGV), few(m.sentences(), 3))
        c.check(all(ln.startswith("| ") or ln.startswith("CASAOS_MODULE_") or re.fullmatch(r"[\x20-\x7e]*", ln) for ln in ml.lines), "the log is printable ASCII", "printable", "other")
        steps = step_seconds(m)
        if steps:
            number("install by hand, seconds per step", ", ".join("%s %d s" % (a, b) for a, b in steps))
        c.done()
        c = Case("M4 files and state")
        judge_installed(c, after, "after the install")
        c.check(not before.footprint(), "the box had nothing of the module before", "none", few(before.footprint()))
        c.done()
        c = Case("M4 status and list")
        so, lo = st.obj() or {}, lst.obj() or {}
        c.check(st.rc == 0 and so.get("installed") is True and so.get("version") == TAGV and jget(so, "unit", "active") is True and jget(so, "unit", "enabled") is True and jget(so, "route", "answers") is True,
                "status says installed at the version, unit active and enabled, the route answers", "all of them", cell(st.out, 160))
        me = module_entry(lo)
        c.check(me is not None and jget(me, "installed", "version") == TAGV, "list shows the module installed with its version", TAGV, cell(json.dumps(me.get("installed") if me else None)))
        c.done()
        c = Case("M4 gateway route and host")
        c.check(host.code == 200, "GET %s/host with the token is answered 200 (the route is in the gateway's table)" % kit.get("route"), "HTTP 200", host.shown())
        c.check(host.field("libvirt") == "available", "the host says that libvirt is available (the test driver)", "libvirt available", "%s, reason %s" % (cell(host.field("libvirt")), cell(host.field("reason"))))
        c.check(200 <= host.code < 300, "the request of the dashboard's card (GET %s/host) succeeds, so the card is shown" % kit.get("route"), "a 2xx", host.shown())
        c.check(noauth.code in (401, 403), "the same request without a token is refused by the module, not 404 (the route is registered)", "HTTP 401", noauth.shown())
        c.check(ghost.code == 404, "a route that is not registered is 404 through the same gateway (so that the 404 above would have shown)", "HTTP 404", ghost.shown())
        c.done()

    # ---- M5 ------------------------------------------------------------------------------------------------------------------------------

    def judge_m5():
        if not gate("M5", "the API"):
            return
        env = gxc("api-state")
        lst, second, plan, st, bad, unauth = (call(n) for n in ("m5-list", "m5-second", "m5-plan", "m5-status", "m5-badname", "m5-unauth"))
        if not all((env, lst, second, plan, st, bad, unauth)):
            return
        words = dict(w.split() for w in env.lines if len(w.split()) == 2)
        if words.get("manager") != "3" or words.get("core") != "3":
            invalid("M5: the test variables did not reach the manager and the core (manager %s, core %s of 3): the API cases prove nothing" % (words.get("manager"), words.get("core")))
            return
        if lst.code == 404:
            invalid("M5: the core has no %s: the release does not carry the Modules API" % "/v1/sys/modules")
            return
        c = Case("M5 list")
        me = module_entry(lst.data if isinstance(lst.data, dict) else {})
        c.check(lst.code == 200 and jget(lst.data, "catalogue", "ok") is True, "GET /v1/sys/modules answers 200 with the catalogue read", "HTTP 200, catalogue.ok true", "%s %s" % (lst.shown(), cell(jget(lst.data, "catalogue"))))
        c.check(me is not None and jget(me, "installed", "version") == TAGV, "the module is listed as installed, with its version", TAGV, cell(json.dumps(me.get("installed") if me else None)))
        c.check(me is not None and me.get("tier") == "community" and me.get("catalogue") is True and jget(me, "system", "supported") is True, "with its tier, in the catalogue and supported", "community", cell(json.dumps(me), 120))
        c.check(unauth.code == 401, "the same call without a token is refused 401", "HTTP 401", unauth.shown())
        c.done()
        c = Case("M5 second install refused")
        c.check(second.code == 409 and second.error_code == "already_installed", "a second install is refused already_installed", "HTTP 409, error_code already_installed", "%s" % second.shown())
        cks = second.detail.get("checks") if isinstance(second.detail.get("checks"), list) else []
        c.check(any(isinstance(x, dict) and x.get("code") == "already_installed" and x.get("ok") is False for x in cks), "the refusal carries the checks, with already_installed not ok", "checks", few(x.get("code") for x in cks if isinstance(x, dict)))
        c.done()
        c = Case("M5 plan")
        c.check(plan.code == 200 and isinstance(plan.data, dict) and plan.data.get("refused") == "already_installed", "the plan by API is a plan, refused already_installed (200)", "HTTP 200, refused already_installed",
                "%s %s" % (plan.shown(), cell(plan.field("refused"))))
        c.check([x[0] for x in plan_checks(plan.data)] == list(CHECKS), "the plan by API has the checks, in order", ", ".join(CHECKS), few(x[0] for x in plan_checks(plan.data)))
        c.check(not plan.field("plan_id"), "a plan that is refused has no plan_id", "none", cell(plan.field("plan_id")))
        c.done()
        c = Case("M5 status")
        job = jget(st.data, "job") or {}
        sts = jget(st.data, "status") or {}
        c.check(st.code == 200 and job.get("state") == "idle", "the status of a module installed by hand has no job (a run by hand is not a job)", "job idle", "%s, state %s" % (st.shown(), cell(job.get("state"))))
        c.check(sts.get("installed") is True and sts.get("version") == TAGV and jget(sts, "unit", "active") is True and jget(sts, "unit", "enabled") is True and jget(sts, "route", "answers") is True,
                "status: installed at its version, unit active and enabled, the route answers", "all of them", cell(json.dumps(sts), 160))
        c.check(isinstance(job.get("log_tail"), list) and job.get("kept_packages") is None, "the job has the shape of the contract (log_tail a list, kept_packages null)", "log_tail list", cell(json.dumps(job), 120))
        c.check(bad.code == 400 and bad.error_code == "bad_request" and bad.detail.get("field") == "name", "a name that is not a module's is a 400 bad_request on the field name", "HTTP 400 bad_request name", "%s %s" % (bad.shown(), cell(bad.detail)))
        c.done()
        stale = Call("m7-stale")
        if "case_M7_done" in marks and stale.answered:
            c = Case("M5 plan_changed")
            c.check(stale.code == 409 and stale.error_code == "plan_changed", "an install with a plan_id that is not the plan's is refused plan_changed (asked on the box of M7, where nothing is installed)", "HTTP 409, error_code plan_changed", stale.shown())
            c.done()
        else:
            not_testable("M5 plan_changed", "a stale plan_id is only refused plan_changed on a box that has nothing installed (M7 asks it); M7 did not run")

    # ---- M6 ------------------------------------------------------------------------------------------------------------------------------

    def judge_job(c, tag, steps, label):
        """the states of a job as the page sees them: running, then its verdict; the step goes forward; nothing the page must not see"""
        rs = rows(tag)
        if not rs:
            invalid("polls-%s.tsv is missing or empty: no evidence of the %s" % (tag, label))
            return rs
        seq = states_of(rs)
        c.check(bool(seq) and seq[-1] == "succeeded", "%s: the job ends succeeded" % label, "succeeded", "%s%s" % (" > ".join(seq) or "no answer", ", the time ran out" if exists(tag + ".timeout") else ""))
        c.check(all(x in ("idle", "running", "succeeded") for x in seq) and "succeeded" not in seq[:-1], "%s: the states it goes through are idle, running, succeeded" % label, "running > succeeded", " > ".join(seq))
        run_steps = [r.step for r in rs if r.answered() and r.state == "running" and r.step not in ("-",)]
        idx = [steps.index(x) for x in run_steps if x in steps]
        c.check(idx == sorted(idx) and all(x in steps or x == "done" for x in run_steps), "%s: the step only goes forward" % label, "in the order of the steps", " ".join(run_steps))
        final = last_answered(rs)
        c.check(final is not None and final.step == "done" and final.code == "-" and final.undo == "-", "%s: at the end the step is done, with no code and no undo" % label, "done - -",
                "%s %s %s" % (final.step, final.code, final.undo) if final else "no answer")
        return rs

    def judge_m6():
        if not gate("M6", "remove through the API"):
            return
        before, after = snap("m6-before"), snap("m6-after")
        rm, st = call("m6-remove"), call("m6-status")
        if not all((before, after, rm, st)):
            return
        if rm.code != 202:
            c = Case("M6 remove through the API")
            c.check(False, "POST remove is accepted", "HTTP 202", "%s %s" % (rm.shown(), cell(rm.detail)))
            c.done()
            return
        log, journal = gxc("m6-log"), gxc("m6-journal")
        if not (log and journal):
            return
        c = Case("M6 the job")
        c.check(jget(rm.data, "job", "state") in ("running", "succeeded"), "the answer of the start says the job is running (or done, for a job so quick that it is over when the answer is made)", "running", cell(jget(rm.data, "job", "state")))
        rs = judge_job(c, "m6-job", REMOVE_STEPS, "remove")
        ml = markers_ok(c, Markers(log.out), "log", REMOVE_STEPS, "n")
        ran = any(r.unit == "active" for r in rs) or "module remove" in journal.out
        c.check(ran, "the job ran as %s (the unit was seen active, or the journal has its start)" % "casaos-module-virt.service", "the unit", cell(journal.out, 100))
        n = ml.nonces()
        c.check(len(n) == 1 and NONCE_RE.match(n[0]) is not None, "the markers carry the nonce the core passed", "32 hex", few(n))
        blob = read("m6-remove.json") + read("m6-status.json")
        c.check(not (n and n[0] != "-" and n[0] in blob), "the nonce is in no answer of the core", "absent", "present")
        tail = jget(st.data, "job", "log_tail")
        c.check(isinstance(tail, list) and not any("CASAOS_MODULE_" in str(x) for x in tail) and len(tail) <= 20, "the log_tail has no marker line and at most 20 lines", "no marker", few(tail or []))
        c.done()
        c = Case("M6 what is gone and what stays")
        c.check(not after.footprint(), "the files of the list, the manifest, the lists, the directories the install made and the link of the unit are gone", "nothing of the module", few(after.footprint()))
        c.check(after.get("unit_known") == "0" and after.get("unit_active") in ("inactive", "none") and after.get("unit_enabled") in ("none", "disabled", "not-found"), "the unit is gone, stopped and not enabled", "gone",
                "known %s, %s, %s" % (after.get("unit_known"), after.get("unit_active"), after.get("unit_enabled")))
        made = [ln for ln in before.lists_dirs.split("\n") if ln]
        c.check(not [d for d in made if d in after.names], "the directories the install made are gone", "none", few([d for d in made if d in after.names]))
        c.check(all(after.pkgs.get(p, "").startswith("ii") for p in PKGS), "the packages stay", "installed", few("%s=%s" % (p, after.pkgs.get(p)) for p in PKGS if not after.pkgs.get(p, "").startswith("ii")))
        c.check(before.get("witness") == "yes" and after.get("witness") == "yes" and after.get("vm_dir") == "yes", "/DATA/VM and the file the proof left in it stay", "yes", "witness %s, vm_dir %s" % (after.get("witness"), after.get("vm_dir")))
        sentences = ml.sentences()
        c.check("removed: %s" % kit.get("name") in sentences, "the run says what it removed", "removed: virt", few(sentences, 3))
        left = [x for x in sentences if x.startswith("left in place:")]
        c.check(any("/DATA/VM" in x for x in left), "the last lines say that /DATA/VM stays", "left in place: /DATA/VM...", few(left, 3))
        c.check(any("the packages" in x and all(p in x for p in PKGS) for x in left), "the last lines say that the packages stay, and name them", few(PKGS), few(left, 3))
        c.check(bool(sentences) and sentences[-1] == "nothing above was run", "the last line says that none of the commands it printed was run", "nothing above was run", cell(sentences[-1] if sentences else "none"))
        c.check(st.code == 200 and jget(st.data, "job", "state") == "succeeded" and jget(st.data, "status", "installed") is False, "the status after the job: succeeded, and the module is not installed", "succeeded, not installed",
                "%s %s" % (cell(jget(st.data, "job", "state")), cell(jget(st.data, "status", "installed"))))
        c.done()
        c = Case("M6 the rest of the box is untouched")
        for u in CORE_UNITS:
            a, b = before.units.get(u), after.units.get(u)
            c.check(a is not None and a == b, "%s is the same invocation, still running" % u, cell(a), cell(b))
        others = lambda s: {k: v for k, v in s.ulist.items() if k != UNIT}
        c.check(bool(before.ulist) and others(before) == others(after), "the list of the units of ReCasaOS (casaos*, rclone) is what it was, the module's own unit apart", few("%s=%s" % kv for kv in sorted(others(before).items()))[:200],
                few("%s=%s" % kv for kv in sorted(others(after).items()))[:200])
        # the module's own files in /etc/casaos: its manifest and the sample of its configuration (the configuration itself stays); the samples of the others are the others'
        mine = lambda p: "/modules/" in p or p.endswith("/virt-management.conf.sample")
        keep = {p: h for p, h in before.etccasaos.items() if not mine(p)}
        c.check({p: h for p, h in after.etccasaos.items() if not mine(p)} == keep and not any(mine(p) for p in after.etccasaos),
                "every file of /etc/casaos other than the module's is as it was", "the same hashes", few(sorted(set(before.etccasaos.items()) ^ set(after.etccasaos.items()))))
        c.done()
        t = [r for r in rs if r.answered() and r.state == "succeeded"]
        if rm.t0 is not None and t:
            number("remove through the API", "%s from POST remove to succeeded" % secs(t[0].t0 - rm.t0))

    # ---- M7 ------------------------------------------------------------------------------------------------------------------------------

    def glob_parts(text):
        sec, vals, props, rcs = None, [], [], []
        for ln in text.split("\n"):
            if ln == "== values":
                sec = vals
                continue
            if ln == "== properties":
                sec = props
                continue
            m = re.fullmatch(r"rc=(\d+)", ln)
            if m:
                rcs.append(int(m.group(1)))
                continue
            if sec is not None and ln.strip():
                sec.append(ln.strip())
        return vals, props, rcs

    def timespan_seconds(v):
        """a time as systemd prints it (7min, 420s, 1min 30s, 420000ms); None when it is not one"""
        units = {"us": 1e-6, "ms": 1e-3, "s": 1.0, "min": 60.0, "h": 3600.0, "d": 86400.0, "w": 604800.0}
        total, any_ = 0.0, False
        for num, unit in re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*(us|ms|min|s|h|d|w)\b", v.strip()):
            total += float(num) * units[unit]
            any_ = True
        return total if any_ else None

    def judge_m7():
        if not gate("M7", "install through the API from clean"):
            return
        before, after = snap("m7-before"), snap("m7-after")
        g_none, g_one, g_two, g_after = (gxc(n) for n in ("m7-glob-none", "m7-glob-one", "m7-glob-two", "m7-glob-after"))
        busy, plan, stale, inst, st = (call(n) for n in ("m7-busy-dummy", "m7-plan", "m7-stale", "m7-install", "m7-status"))
        if not all((before, after, g_none, g_one, g_two, g_after, busy, plan, stale, inst, st)):
            return
        c = Case("M7 the glob of the busy check")
        for g, label, n, ids in ((g_none, "no unit", 0, []), (g_one, "one unit", 1, ["casaos-module-mpdummy1.service"]), (g_two, "two units", 2, ["casaos-module-mpdummy1.service", "casaos-module-mpdummy2.service"]),
                                 (g_after, "no unit again", 0, [])):
            vals, props, rcs = glob_parts(g.out)
            found = sorted(p[3:] for p in props if p.startswith("Id="))
            c.check(rcs == [0, 0], "`systemctl show 'casaos-module-*.service'` with %s ends with status 0" % label, "rc 0 and 0", cell(rcs))
            c.check(vals == ["active"] * n, "with %s the values of ActiveState are %s" % (label, "nothing" if n == 0 else "active" if n == 1 else "active, active"), "%d x active" % n, cell(vals))
            c.check(found == ids, "with %s the properties name %s" % (label, "no unit" if not ids else few(ids)), cell(ids), cell(found))
            c.note("%s: %s" % (label, cell(" / ".join(vals) or "nothing", 60)))
        c.check(busy.code == 409 and busy.error_code == "busy" and isinstance(busy.detail.get("reason"), str) and busy.detail.get("reason"), "an install is refused busy, with its reason, while a unit named like a job runs", "HTTP 409 busy with a reason",
                "%s %s" % (busy.shown(), cell(busy.detail.get("reason"))))
        c.done()
        props = Gx("m7-run-props")
        if props.ran and props.answered:
            c = Case("M7 job unit properties")
            kv = {}
            for ln in props.out.split("\n"):
                k, eq, v = ln.partition("=")
                if eq:
                    kv.setdefault(k, v.strip())
            timeout = timespan_seconds(kv.get("TimeoutStopUSec", kv.get("TimeoutStopSec", "")))
            c.check(kv.get("KillMode") == "mixed", "KillMode is mixed (the assistant alone is asked to stop; apt and dpkg are asked to leave by it)", "mixed", cell(kv.get("KillMode")))
            c.check(timeout == STOP_UNIT_SECONDS, "the unit has 420 s to stop (7min)", "420 s", cell(kv.get("TimeoutStopUSec", kv.get("TimeoutStopSec")), 40))
            c.check(kv.get("Type") == "exec", "Type is exec", "exec", cell(kv.get("Type")))
            c.check(kv.get("LoadState") == "loaded", "the unit is loaded while it runs", "loaded", cell(kv.get("LoadState")))
            cat = props.out.split("== cat", 1)[1] if "== cat" in props.out else ""
            c.check("CASAOS_MODULE_TEST" not in cat and "CASAOS_MODULE_CATALOGUE" not in cat, "the unit's own definition does not carry the test variables (they come from the manager's environment)", "absent", "present")
            end = Gx("m7-props-end")
            if end.ran and end.answered:
                loaded = [ln.strip() for ln in end.out.split("== loadstate", 1)[1].split("== cat")[0].split("\n") if ln.strip()] if "== loadstate" in end.out else []
                c.check(loaded[:1] == ["not-found"], "once the job has ended the unit is collected (LoadState not-found)", "not-found", cell(loaded))
            c.done()
        else:
            not_testable("M7 job unit properties", "the unit of the job was never seen active by a poll of the proof (it ran for less than a poll)")
        c = Case("M7 install through the API")
        c.check(plan.code == 200 and plan.data.get("refused") is None and PLAN_ID_RE.match(str(plan.field("plan_id"))) is not None, "the plan by API on a clean box is not refused and has a plan_id", "HTTP 200, refused null",
                "%s %s" % (plan.shown(), cell(plan.field("refused"))))
        c.check(stale.code == 409 and stale.error_code == "plan_changed", "a plan_id that is not the plan's is refused plan_changed", "HTTP 409 plan_changed", stale.shown())
        c.check(inst.code == 202 and jget(inst.data, "job", "state") in ("running", "succeeded"), "the install is accepted, and the job is running (or done, for a job so quick that it is over when the answer is made)", "HTTP 202, running",
                "%s %s" % (inst.shown(), cell(jget(inst.data, "job", "state"))))
        if inst.code != 202:
            c.done()
            return
        log = gxc("m7-log")
        if log is None:
            return
        rs = judge_job(c, "m7-job", INSTALL_STEPS, "install")
        ml = markers_ok(c, Markers(log.out), "log", INSTALL_STEPS, "n")
        nonce = ml.nonces()
        blob = read("m7-install.json") + read("m7-status.json")
        c.check(not (nonce and nonce[0] != "-" and nonce[0] in blob), "the nonce is in no answer of the core", "absent", "present")
        job = jget(st.data, "job") or {}
        c.check(job.get("state") == "succeeded" and job.get("step") == "done" and job.get("code") == "" and job.get("undo") == "" and job.get("kept_packages") is None,
                "the status of the finished job: succeeded at done, no code, no undo, no kept packages", "succeeded done", cell(json.dumps({k: job.get(k) for k in ("state", "step", "code", "undo", "kept_packages")})))
        t0, t1 = parse_time(str(job.get("started_at", ""))), parse_time(str(job.get("ended_at", "")))
        c.check(t0 is not None and t1 is not None and t1 >= t0, "started_at and ended_at are times, in order", "RFC 3339", "%s %s" % (cell(job.get("started_at")), cell(job.get("ended_at"))))
        tail = job.get("log_tail")
        c.check(isinstance(tail, list) and len(tail) <= 20 and not any("CASAOS_MODULE_" in str(x) for x in tail), "the log_tail has at most 20 lines and no marker line", "no marker", few(tail or []))
        judge_installed(c, after, "after the install through the API")
        host = Call("m7-host")
        c.check(host.answered and host.code == 200 and host.field("libvirt") == "available", "the host answers, libvirt available", "HTTP 200, available", host.shown())
        c.done()
        done = [r for r in rs if r.answered() and r.state == "succeeded"]
        if inst.t0 is not None and done:
            number("install through the API", "%s from POST install to succeeded" % secs(done[0].t0 - inst.t0))
        steps = step_seconds(ml)
        if steps:
            number("install through the API, seconds per step", ", ".join("%s %d s" % (a, b) for a, b in steps))

    # ---- M8 ------------------------------------------------------------------------------------------------------------------------------

    def nothing_left(c, s, label):
        c.check(not s.footprint(), "%s: nothing of the module is on the box (no file, no list, no manifest, no link of the unit)" % label, "none", few(s.footprint()))
        c.check(s.get("tmp_entries") == "0", "%s: the area of the install is empty" % label, "0", s.get("tmp_entries"))
        c.check(s.get("unit_active") in ("inactive", "none") and s.get("unit_enabled") in ("none", "disabled", "not-found"), "%s: the unit is neither active nor enabled" % label, "inactive, not enabled",
                "%s, %s" % (s.get("unit_active"), s.get("unit_enabled")))

    def expected_sequence(step, rollback):
        want = []
        for s in INSTALL_STEPS[:INSTALL_STEPS.index(step)]:
            want += [(s, "begin"), (s, "done")]
        want += [(step, "begin"), (step, "failed")]
        if rollback:
            for s in ROLLBACK_STEPS:
                want += [(s, "begin"), (s, "done")]
        return want

    def judge_failed_install(cid, title, p, step, code, rollback, extra=None):
        """an install that fails at a step: refused before it began is not what is judged here; the plan passes, the steps up to the failing one, the rollback when the unpack had begun, the
        RESULT, the sentences, and nothing left on the box"""
        if not gate(cid, title):
            return
        pl, inst, log = gxc(p + "-plan"), gxc(p + "-install"), gxc(p + "-log")
        before, after = snap(p + "-before"), snap(p + "-after")
        if not all((pl, inst, log, before, after)):
            return
        c = Case(title)
        plan = pl.obj() or {}
        c.check(pl.rc == 0 and plan.get("refused") is None and bool(plan.get("plan_id")), "the plan passes (the catalogue names the archive, and the checks are about the box, not about the bytes)", "refused null", "%s, refused %s" % (pl.shown(), cell(plan.get("refused"))))
        c.check(inst.rc == 3, "the install ends with status 3 (failed, rolled back)", "exit 3", "%s %s" % (inst.shown(), cell(inst.out[-120:])))
        m = Markers(inst.out)
        c.check(not m.bad, "every marker line has the shape of the grammar", "none other", few(m.bad, 3))
        c.check(m.sequence() == expected_sequence(step, rollback), "the steps go up to %s and fail there%s" % (step, ", then the rollback runs the steps of a remove" if rollback else ", and nothing is rolled back (nothing was put on the box)"),
                " ".join("%s:%s" % x for x in expected_sequence(step, rollback)), " ".join("%s:%s" % x for x in m.sequence()))
        result_is(c, m, "install", "failed", step, code, "rolled_back")
        c.check("failed at step %s: %s" % (step, code) in m.lines, "the run says where it failed and why", "failed at step %s: %s" % (step, code), few(m.sentences(), 4))
        c.check("rolled back" in m.lines if rollback else "rolled back" not in m.lines, "the run says it rolled back" if rollback else "the run does not claim a rollback it did not make", "rolled back" if rollback else "no such line", few(m.sentences(), 5))
        c.check("kept_packages: %s" % " ".join(sorted(PKGS)) in m.lines, "the last sentence before the result says that the packages are kept", "kept_packages: " + " ".join(sorted(PKGS)), few([x for x in m.sentences() if "kept" in x], 2))
        ml = Markers(log.out)
        c.check([ln for ln in ml.lines if not ln.startswith("| ")] == m.lines, "the log holds the lines of the standard output", "the same", "differs")
        nothing_left(c, after, "after the install")
        c.check(before.reserved == after.reserved, "the programs the assistant runs (systemctl, apt-get, dpkg, env, casaos) are what they were", "the same hashes", few(sorted(set(before.reserved.items()) ^ set(after.reserved.items()))))
        if extra:
            extra(c, before, after)
        c.done()

    def judge_m8_hostile(variant, title):
        entry = kit.get("variants", {}).get(variant, {}).get("entry", "")
        paths = {"dotdot": None, "systemctl": "/usr/bin/systemctl", "varlib": "/var/lib/casaos/mp-var.txt", "usrshare": "/usr/share/casaos/mp-share.txt", "etcmod": "/etc/casaos/modules/mp-mod.json"}

        def extra(c, before, after):
            where = paths.get(variant)
            if variant == "dotdot":
                esc = Gx("m8-dotdot-escape")
                c.check(esc.answered and [x for x in esc.lines if x != "done"] == [], "nothing named mp-escape was written anywhere the archive's `../` could reach", "none", few([x for x in esc.lines if x != "done"]))
            elif where:
                c.check(where not in after.names or where in before.names, "%s was not written" % where, "absent", "present")
        judge_failed_install("M8_" + variant, title, "m8-" + variant, "unpack", "unpack_path", True, extra)
        if entry:
            pass

    def judge_m8_checksum():
        judge_failed_install("M8_checksum", "M8 checksum_mismatch", "m8-checksum", "verify", "checksum_mismatch", False)

    def refused_by_hand(c, inst, code, label):
        c.check(inst.rc == 2, "%s: the install by hand ends with status 2 (refused, nothing changed)" % label, "exit 2", "%s %s" % (inst.shown(), cell(inst.out[-120:])))
        m = Markers(inst.out)
        c.check(m.sequence() == [], "%s: no step began" % label, "no STEP line", " ".join("%s:%s" % x for x in m.sequence()))
        result_is(c, m, label, "refused", "-", code, "-")
        c.check("refused: %s" % code in m.lines, "%s: the run says it refused, and why" % label, "refused: %s" % code, few(m.sentences(), 3))

    def judge_plan_refusal(c, pl, code, check, detail_start, label):
        plan = pl.obj() or {}
        c.check(pl.rc == 0 and plan.get("refused") == code, "%s: the plan is refused %s" % (label, code), "refused %s" % code, "%s, refused %s" % (pl.shown(), cell(plan.get("refused"))))
        ok, detail = check_of(plan, check)
        c.check(ok is False and detail.startswith(detail_start), "%s: the check %s is not ok, and says %s" % (label, check, detail_start), "%s..." % detail_start, "ok %s, %s" % (ok, cell(detail, 80)))
        c.check(not plan.get("plan_id"), "%s: a plan that is refused has no plan_id" % label, "none", cell(plan.get("plan_id")))

    def judge_m8_foreign():
        cid, p = "M8_foreign", "m8-foreign"
        if not gate(cid, "M8 foreign_unit"):
            return
        pl, inst, log, undo = (gxc(p + n) for n in ("-plan", "-install", "-log", "-undo"))
        before, after = snap(p + "-before"), snap(p + "-after")
        if not all((pl, inst, log, undo, before, after)):
            return
        c = Case("M8 foreign_unit")
        judge_plan_refusal(c, pl, "foreign_unit", "foreign_unit", "not_owned", "plan")
        refused_by_hand(c, inst, "foreign_unit", "install")
        c.check(Markers(log.out).sequence() == [], "the log has no step", "none", "some")
        nothing_left(c, after, "after the undo")
        c.done()
        if after.get("unit_known") != before.get("unit_known") or "off file=no" not in undo.out:
            invalid("M8 foreign_unit: the unit file that was put there could not be taken away again (%s)" % cell(undo.out, 60))

    def judge_m8_busy(cid, title, p, detail, name_flag):
        if not gate(cid, title):
            return
        pl, inst, undo = gxc(p + "-plan"), gxc(p + "-install"), gxc(p + "-undo")
        dirt = gxc(p + "-dirt")
        before, after = snap(p + "-before"), snap(p + "-after")
        api = Call(p + "-api")
        if not all((pl, inst, undo, dirt, before, after)):
            return
        if not api.ran:
            invalid("%s: the call through the API was not made" % title)
            return
        c = Case(title)
        if name_flag == "lock":
            during = gxc(p + "-during")
            c.check(dirt.out.strip() == "held" and during is not None and during.out.strip() == "held", "the proof held dpkg's lock while it asked", "held", cell(dirt.out + (during.out if during else ""), 60))
        else:
            c.check(dirt.out.strip() == "on", "the flag of the upgrade of the operating system was in place while it asked", "on", cell(dirt.out, 60))
        judge_plan_refusal(c, pl, "busy", "busy", detail, "plan")
        refused_by_hand(c, inst, "busy", "install")
        c.check(api.code == 409 and api.error_code == "busy" and bool(api.detail.get("reason")), "the install through the API is refused busy, with its reason", "HTTP 409 busy", "%s %s" % (api.shown(), cell(api.detail.get("reason"), 80)))
        nothing_left(c, after, "after the undo")
        c.done()
        if name_flag == "lock":
            if undo.out.strip() != "free" or after.get("dpkg_lock") != "free":
                invalid("M8 dpkg's lock: the lock was not given back (%s, %s)" % (cell(undo.out, 60), after.get("dpkg_lock")))
        elif after.get("osflag") != "no" or "off flag=no" not in undo.out:
            invalid("M8 the flag of the upgrade: the flag was not taken away again (%s)" % cell(undo.out, 60))

    def judge_m8_disk():
        cid, p = "M8_disk", "m8-disk"
        if not gate(cid, "M8 disk_short"):
            return
        pl, inst, undo, dirt = (gxc(p + n) for n in ("-plan", "-install", "-undo", "-dirt"))
        before, after = snap(p + "-before"), snap(p + "-after")
        if not all((pl, inst, undo, dirt, before, after)):
            return
        c = Case("M8 disk_short")
        c.check("mounted yes" in dirt.out, "a tmpfs of 1 MiB was over the area of the install while it asked", "mounted yes", cell(dirt.out, 60))
        judge_plan_refusal(c, pl, "disk_short", "disk", "need ", "plan")
        refused_by_hand(c, inst, "disk_short", "install")
        nothing_left(c, after, "after the undo")
        c.done()
        if "unmounted no" not in undo.out or after.get("tmp_mount") != "no":
            invalid("M8 disk_short: the tmpfs could not be taken away again (%s, tmp_mount %s)" % (cell(undo.out, 40), after.get("tmp_mount")))

    def judge_m8_symlink():
        cid, p = "M8_symlink", "m8-symlink"
        if not gate(cid, "M8 link in the area of the install"):
            return
        inst, log, undo, dirt, cb, ca = (gxc(p + n) for n in ("-install", "-log", "-undo", "-dirt", "-canary-before", "-canary-after"))
        before, after = snap(p + "-before"), snap(p + "-after")
        if not all((inst, log, undo, dirt, cb, ca, before, after)):
            return
        c = Case("M8 link in the area of the install")
        c.check(dirt.out.startswith("planted"), "a link to a file of the proof was planted at the archive's name in the area of the install", "planted", cell(dirt.out, 60))
        c.check(cb.out.startswith("canary ") and cb.out.strip() == ca.out.strip(), "the file the link points to is what it was (content, mode, time): the assistant did not follow the link", cell(cb.out.strip(), 80), cell(ca.out.strip(), 80))
        m = Markers(inst.out)
        code = m.results[0][5] if len(m.results) == 1 else ""
        ok = (inst.rc == 0 and len(m.results) == 1 and m.results[0][2] == "ok") or (inst.rc == 2 and len(m.results) == 1 and m.results[0][2] == "refused" and code in ("state_unsafe", "tmp_unusable"))
        c.check(ok, "the install went through (the link was swept away, not followed) or was refused state_unsafe", "exit 0, or exit 2 state_unsafe", "%s %s" % (inst.shown(), " ".join(m.results[0][2:6]) if m.results else "no RESULT"))
        c.note("the assistant %s" % ("installed the module over the area it swept" if inst.rc == 0 else "refused: " + code))
        c.done()
        if "off link=no" not in undo.out:
            invalid("M8 link in the area of the install: the link could not be taken away again (%s)" % cell(undo.out, 60))

    def judge_m8_held():
        cid, p = "M8_held", "m8-held"
        if not gate(cid, "M8 held_package"):
            return
        unseat, dirt, pl, inst, undo, reseat = (gxc(p + n) for n in ("-unseat", "-dirt", "-plan", "-install", "-undo", "-reseat"))
        before, after = snap(p + "-before"), snap(p + "-after")
        if not all((unseat, dirt, pl, inst, undo, reseat, before, after)):
            return
        pkg = "ovmf" if "ovmf" in PKGS else (PKGS[-1] if PKGS else "")
        if "rc=0" not in unseat.out or "hold ok" not in dirt.out:
            invalid("M8 held_package: the held package could not be put in place (unseat: %s; hold: %s)" % (cell(unseat.out, 80), cell(dirt.out, 80)))
            return
        sim = Gx("m8-held-aptsim")
        if not sim.answered:
            invalid("M8 held_package: apt was not asked what it says of the held package (m8-held-aptsim)")
            return
        if pkg not in parse_aptsim(sim.out)["held"]:
            not_testable("M8 held_package", "apt itself does not say that %s is held when it is asked to install the packages (%s): the case needs a hold that apt sees" % (pkg, cell(sim.out.replace("\n", " "), 80)))
            return
        c = Case("M8 held_package")
        plan = pl.obj() or {}
        c.check(pl.rc == 0 and plan.get("refused") == "held_package", "plan: the plan is refused held_package", "refused held_package", "%s, refused %s" % (pl.shown(), cell(plan.get("refused"))))
        ok, detail = check_of(plan, "apt")
        c.check(ok is False and (pkg in detail or "held" in detail.lower()), "plan: the check apt is not ok, and says that a package is held", "mentions %s" % pkg, "ok %s, %s" % (ok, cell(detail, 100)))
        refused_by_hand(c, inst, "held_package", "install")
        nothing_left(c, after, "after the undo")
        c.done()
        if "unheld" not in undo.out or "rc=0" not in reseat.out or not after.pkgs.get(pkg, "").startswith("ii"):
            invalid("M8 held_package: %s could not be put back as it was (%s; %s; %s)" % (pkg, cell(undo.out, 40), cell(reseat.out, 30), after.pkgs.get(pkg)))

    def judge_m8_manual():
        cid, p = "M8_manual", "m8-manual"
        if not gate(cid, "M8 a package marked automatic"):
            return
        dirt, pl, undo = gxc(p + "-dirt"), gxc(p + "-plan"), gxc(p + "-undo")
        before, after = snap(p + "-before"), snap(p + "-after")
        if not all((dirt, pl, undo, before, after)):
            return
        pkg = "qemu-utils" if "qemu-utils" in PKGS else (PKGS[0] if PKGS else "")
        if pkg not in before.auto:
            invalid("M8 a package marked automatic: %s was not marked automatically installed when the plan was asked (%s)" % (pkg, cell(dirt.out, 80)))
            return
        sim = Gx("m8-manual-aptsim")
        if not sim.answered:
            invalid("M8 a package marked automatic: apt was not asked what it says of the package (m8-manual-aptsim)")
            return
        if pkg not in parse_aptsim(sim.out)["manual"]:
            not_testable("M8 a package marked automatic", "apt does not say `set to manually installed` for %s when it is asked to install the packages (%s): the case needs that line" % (pkg, cell(sim.out.replace("\n", " "), 80)))
            return
        c = Case("M8 a package marked automatic")
        plan = pl.obj() or {}
        c.check(pl.rc == 0 and plan.get("refused") is None and bool(plan.get("plan_id")), "the plan passes (apt says `set to manually installed`, which the plan reads and does not act on)", "refused null", "%s, refused %s" % (pl.shown(), cell(plan.get("refused"))))
        c.check(before.apt == after.apt, "the files of apt and dpkg (extended_states, history.log, status, eipp.log.xz) are as they were", "the same", few(sorted(set(before.apt.items()) ^ set(after.apt.items()))))
        c.check(pkg in after.auto, "%s is still marked automatically installed" % pkg, "auto", few(sorted(after.auto)))
        c.done()
        if re.fullmatch(r"manual auto=", undo.out.strip()) is None:
            invalid("M8 a package marked automatic: the mark could not be put back as it was (%s)" % cell(undo.out, 60))

    def judge_m8():
        judge_m8_checksum()
        for variant, title in M8_CASES[1:6]:
            judge_m8_hostile(variant, title)
        judge_m8_foreign()
        judge_m8_busy("M8_busy_dpkg", "M8 busy (dpkg's lock)", "m8-busy-dpkg", "package_manager", "lock")
        judge_m8_busy("M8_busy_osu", "M8 busy (the flag of the upgrade)", "m8-busy-osu", "os_upgrade", "flag")
        judge_m8_disk()
        judge_m8_symlink()
        judge_m8_held()
        judge_m8_manual()

    # ---- M9 ------------------------------------------------------------------------------------------------------------------------------

    def parse_watch(text):
        d = {}
        for ln in text.splitlines():
            if ln.startswith("never saw"):
                d["never"] = ln
            elif ln.startswith("no process"):
                d["noproc"] = ln
            elif ln.startswith("signalled "):
                d["signalled"] = dict(kv.split("=", 1) for kv in ln.split()[1:] if "=" in kv)
            elif ln.startswith("stopped "):
                d["stopped"] = dict(kv.split("=", 1) for kv in ln.split()[1:] if "=" in kv)
            elif ln.startswith("dpkg_seen="):
                d["dpkg_seen"] = ln.split("=", 1)[1]
            elif ln.startswith("seen="):
                d["seen"] = ln[5:]
            elif ln.startswith("after="):
                d["after"] = ln[6:]
            elif ln.startswith("dpkg_audit="):
                d["audit"] = ln.split("=", 1)[1]
        return d

    def landed_in(line):
        """(step, phase) of the STEP line a text is, ('result', verdict) for a RESULT line, None for anything else"""
        m = STEP_LINE.match(line or "")
        if m:
            return m.group(3), m.group(4)
        m = RESULT_LINE.match(line or "")
        if m:
            return "result", m.group(3)
        return None

    def kept_list(text):
        try:
            v = json.loads(text)
        except ValueError:
            return None
        return sorted(v) if isinstance(v, list) and all(isinstance(x, str) for x in v) else None

    def judge_m9_case(cid, mode, step):
        title = {"kill": "M9 SIGKILL at the step %s", "stop": "M9 systemctl stop during the step %s", "term": "M9 SIGTERM at the step %s"}[mode] % step
        if not gate("M9_" + cid, title):
            return
        p = "m9-" + cid.replace("_", "-")
        start, plan = call(p + "-start"), call(p + "-plan")
        if start is None or plan is None:
            return
        if start.code != 202:
            c = Case(title)
            c.check(False, "POST install is accepted", "HTTP 202", "%s %s" % (start.shown(), cell(start.detail.get("reason") or start.detail, 100)))
            c.done()
            return
        watch_res, wait, log, journal = (gxc(p + n) for n in ("-watch-result", "-wait", "-log", "-journal"))
        dead, clean, fresh = snap(p + "-dead"), snap(p + "-clean"), snap(p + "-fresh")
        rem, fp, fr, rm2, fp2 = (gxc(p + n) for n in ("-remove", "-fp", "-fresh", "-rm2", "-fp2"))
        st = Call(p + "-status")
        if not all((watch_res, wait, log, journal, dead, clean, fresh, rem, fp, fr, rm2, fp2)):
            return
        rs = rows(p + "-job")
        seq = states_of(rs)
        w = parse_watch(watch_res.out)
        ml = Markers(log.out)
        last_log = [x for x in ml.order]
        final = last_answered(rs)
        c = Case(title)
        within, why = True, ""
        if "noproc" in w:
            if final is not None and final.state == "succeeded":
                not_testable(title, "the install was over when the watcher went for the assistant (it saw the step begin, and no process was left to signal): the step is shorter than the watcher's look")
            else:
                invalid("%s: %s" % (title, w["noproc"]))
            return
        if "never" in w:
            if final is not None and final.state == "succeeded":
                not_testable(title, "the install went through step %s without the watcher (a look every 20 ms) seeing it begin: the step is shorter than that" % step)
            else:
                invalid("%s: the install ended (%s%s) without reaching the step: %s" % (title, final.state if final else "no answer", " " + final.code if final and final.code != "-" else "", w["never"]))
            return
        if mode == "stop" and "stopped" not in w:
            invalid("%s: the watcher did not stop the unit: %s" % (title, cell(watch_res.out, 120)))
            return
        if mode in ("kill", "term") and w.get("signalled", {}).get("rc") != "0":
            invalid("%s: the signal could not be sent: %s" % (title, cell(watch_res.out, 120)))
            return
        seen = landed_in(w.get("seen"))
        after_ = landed_in(w.get("after"))
        if mode == "kill":
            where = after_
            if where is not None and where[0] == "result":
                not_testable(title, "the install had ended (%s) when the signal reached the assistant" % where[1])
                return
            within = where == (step, "begin")
            why = "the log ended at %s after the signal, not in %s" % (" ".join(where) if where else cell(w.get("after"), 60), step)
        else:
            within = seen == (step, "begin")
            why = "the watcher saw %s when it acted, not %s" % (" ".join(seen) if seen else cell(w.get("seen"), 60), step)
        # what the page is told
        c.check("succeeded" not in seq, "the job never reads succeeded", "never", " > ".join(seq))
        c.check(not exists(p + "-job.timeout"), "the job reaches a final state in the time given", "final", "still %s after %s s" % (final.state if final else "?", read(p + "-job.timeout").strip()))
        if mode == "kill":
            c.check(bool(seq) and seq[-1] == "interrupted", "the job reads interrupted in the end", "interrupted", " > ".join(seq))
            c.check(final is not None and final.undo == "-" and final.kept == "-", "an interrupted job has no undo and no kept packages", "none", "%s %s" % (final.undo, final.kept) if final else "no answer")
            lastlog = [g for k, g in ml.order if k == "step"]
            if lastlog and final is not None:
                c.check(final.step == lastlog[-1][2], "the step of the interrupted job is the step of the last marker of the log", lastlog[-1][2], final.step)
            c.check(not ml.results, "the log has no RESULT line (nothing said the end)", "none", few(" ".join(r[2:6]) for r in ml.results))
        elif mode == "term":
            c.check(bool(seq) and seq[-1] == "failed", "the job reads failed in the end", "failed", " > ".join(seq))
            c.check(final is not None and final.code == "interrupted" and final.undo == "rolled_back", "failed with the code interrupted, rolled back", "interrupted rolled_back",
                    "%s %s" % (final.code, final.undo) if final else "no answer")
            c.check(final is not None and final.step in ("start", "route") and within, "it failed at the step it was in", step, final.step if final else "?")
            c.check(final is not None and kept_list(final.kept) == sorted(PKGS), "the packages are kept, and the job says which", few(sorted(PKGS)), final.kept if final else "?")
        else:
            c.check(bool(seq) and seq[-1] in ("failed", "interrupted"), "the job reads failed or interrupted in the end", "failed or interrupted", " > ".join(seq))
            if final is not None and final.state == "failed":
                c.check(final.code == "interrupted" and final.undo in ("rolled_back",), "failed with the code interrupted, rolled back", "interrupted rolled_back", "%s %s" % (final.code, final.undo))
            elif final is not None:
                c.check(final.undo == "-", "an interrupted job has no undo", "none", final.undo)
            sp = w.get("stopped", {})
            c.check(sp.get("rc") == "0", "systemctl stop of the unit ends with status 0", "rc=0", cell(sp))
            secs_ = int(sp["seconds"]) if sp.get("seconds", "").isdigit() else None
            c.check(secs_ is not None and secs_ < STOP_UNIT_SECONDS, "the unit stopped before systemd's 420 s ran out (the assistant answered the SIGTERM)", "under 420 s", cell(sp.get("seconds")))
            if secs_ is not None:
                number("systemctl stop during %s" % step, "%d s until the unit was stopped" % secs_)
            c.check(w.get("audit") == "0", "dpkg --audit finds nothing a second after the stop (apt and dpkg were let finish)", "0", cell(w.get("audit")))
            if step == "start":
                got = [s for s, _ in ml.sequence()]
                c.check(all(r in got for r in ROLLBACK_STEPS), "the log has the steps of the rollback (rollback_stop... rollback_reload)", "all four", few(got))
                result_is(c, ml, "log", "failed", "start", "interrupted", "rolled_back")
            if step == "packages" and w.get("dpkg_seen") != "yes":
                within, why = False, "dpkg was not running when the stop came (the watcher waited for it for three minutes)"
        c.check(wait.rc == 0 and wait.out.startswith("clean after"), "no assistant, apt or dpkg is at work, the job unit is gone and dpkg's lock is free", "clean", cell(wait.out, 100))
        m_ = re.match(r"clean after (\d+) s", wait.out)
        if m_:
            number("%s: the box quiet after" % title[3:], "%s s" % m_.group(1))
        c.check(dead.get("dpkg_audit") == "0", "dpkg --audit finds nothing once the box is quiet", "0", dead.get("dpkg_audit"))
        c.check(st.answered and not any("CASAOS_MODULE_" in str(x) for x in (jget(st.data, "job", "log_tail") or [])), "the status of the job has no marker line in its log_tail", "no marker", cell(jget(st.data, "job", "log_tail")))
        # what remains is finished by the assistant's own remove, completely
        had = bool(dead.footprint())
        mr = Markers(rem.out)
        if had:
            c.check(rem.rc == 0 and mr.sequence() == [x for s in REMOVE_STEPS for x in ((s, "begin"), (s, "done"))] + [("done", "done")], "remove finishes what the interrupted install left", "exit 0, the steps of a remove", "%s %s" % (rem.shown(), " ".join("%s:%s" % x for x in mr.sequence())))
            result_is(c, mr, "remove", "ok", "done", "-", "-")
        else:
            c.check(rem.rc == 2, "nothing of the module was on the box, so remove refuses not_installed", "exit 2", rem.shown())
            result_is(c, mr, "remove", "refused", "-", "not_installed", "-")
        nothing_left(c, clean, "after the remove")
        c.check(fp.out.strip() == "", "nothing of the module is found by the footprint (files, lists, manifest, link of the unit)", "none", few(fp.lines))
        c.check(clean.get("dpkg_audit") == "0", "dpkg --audit finds nothing after the remove", "0", clean.get("dpkg_audit"))
        # and a fresh install succeeds
        c.check(fr.rc == 0, "a fresh install succeeds", "exit 0", "%s %s" % (fr.shown(), cell(fr.out[-160:])))
        markers_ok(c, Markers(fr.out), "fresh install", INSTALL_STEPS, "-")
        judge_installed(c, fresh, "after the fresh install")
        c.check(rm2.rc == 0 and fp2.out.strip() == "", "the module is taken off again for the next case", "exit 0, nothing left", "%s %s" % (rm2.shown(), few(fp2.lines)))
        if dead.footprint():
            c.note("the kill left %d things on the box" % len(dead.footprint()))
        c.note("the signal landed: %s" % (cell(w.get("after") if mode == "kill" else w.get("seen"), 90)))
        if c.fails:
            c.done()
        elif not within:
            not_testable(title, "%s; what was checked held (%d checks), for the step it did land in" % (why, c.passed))
        else:
            c.done()

    def judge_m9():
        for cid, mode, step in M9_CASES:
            judge_m9_case(cid, mode, step)

    # ---- M10 -----------------------------------------------------------------------------------------------------------------------------

    def judge_m10():
        if not gate("M10", "the restart"):
            return
        if exists("m10-reboot.failed"):
            invalid("M10: " + one(read("m10-reboot.failed")))
            return
        before, after, removed = snap("m10-before"), snap("m10-after"), snap("m10-removed")
        inst, status, rm, fp = (gxc(n) for n in ("m10-install", "m10-status", "m10-remove", "m10-fp"))
        host = call("m10-host")
        if not all((before, after, removed, inst, status, rm, fp, host)):
            return
        if inst.rc != 0 or before.get("unit_active") != "active":
            invalid("M10: the module was not installed and running before the restart (install: %s, unit %s)" % (inst.shown(), before.get("unit_active")))
            return
        c = Case("M10 the restart")
        old = one(read("m10-reboot.boot-old"))
        bt = [r for r in table("boot-m10-reboot.tsv") if len(r) == 3]
        new = bt[-1][2] if bt else "-"
        c.check(old not in ("", "-") and new not in ("", "-") and old != new, "the guest went away and came back with another boot id", "a boot id other than %s" % old, new)
        c.check(after.get("unit_enabled") == "enabled" and after.get("unit_active") == "active", "the unit is enabled and active again after the restart", "enabled, active", "%s, %s" % (after.get("unit_enabled"), after.get("unit_active")))
        so = status.obj() or {}
        c.check(so.get("installed") is True and so.get("version") == TAGV and jget(so, "unit", "active") is True and jget(so, "route", "answers") is True, "status after the restart: installed, active, the route answers", "all of them", cell(status.out, 140))
        c.check(host.code == 200 and host.field("libvirt") == "available", "the host answers through the gateway, libvirt available", "HTTP 200 available", "%s %s" % (host.shown(), cell(host.field("libvirt"))))
        c.check(after.get("manager_env") == "0" and after.get("core_env") == "0", "the test variables are in neither the environment of the manager nor the core's (they do not survive, and were taken out before)", "0 and 0",
                "manager %s, core %s" % (after.get("manager_env"), after.get("core_env")))
        c.check(after.get("dpkg_audit") == "0", "dpkg --audit finds nothing after the restart", "0", after.get("dpkg_audit"))
        c.done()
        c = Case("M10 remove after the restart")
        mr = Markers(rm.out)
        c.check(rm.rc == 0 and mr.sequence() == [x for s in REMOVE_STEPS for x in ((s, "begin"), (s, "done"))] + [("done", "done")], "remove after the restart works", "exit 0, the steps of a remove", "%s %s" % (rm.shown(), cell(rm.out[-120:])))
        result_is(c, mr, "remove", "ok", "done", "-", "-")
        nothing_left(c, removed, "after the remove")
        c.check(fp.out.strip() == "", "nothing of the module is left", "none", few(fp.lines))
        c.done()
        away = [r for r in bt if r[2] == "-"]
        newr = [r for r in bt if r[2] == new]
        if away and newr and fnum(away[0][0]) is not None and fnum(newr[0][1]) is not None:
            number("restart", "the guest was away %s (first unanswered look to the first answer with another boot id)" % secs(fnum(newr[0][1]) - fnum(away[0][0])))

    # ---- M11 -----------------------------------------------------------------------------------------------------------------------------

    # what a package of the module leaves that dpkg does not list: the accounts and the groups it makes, the alternatives, the links that enable its units, the policy of the security
    # modules, its settings of libvirt and qemu; and what the assistant keeps (its state area, its log) and the data of the module
    ALLOWED_ETC = [r"^/etc/(passwd|group|shadow|gshadow|subuid|subgid)-?$", r"^/etc/alternatives(/|$)", r"^/etc/ld\.so\.cache$", r"^/etc/\.pwd\.lock$", r"^/etc/systemd/system/[^/]*\.wants(/|$)",
                   r"^/etc/systemd/system/[^/]*\.requires(/|$)", r"^/etc/libvirt(/|$)", r"^/etc/qemu(/|$)", r"^/etc/apparmor\.d(/|$)", r"^/etc/apparmor(/|$)", r"^/etc/dbus-1(/|$)",
                   r"^/etc/ssl(/|$)", r"^/etc/sasl2(/|$)", r"^/etc/default(/|$)", r"^/etc/logrotate\.d(/|$)", r"^/etc/sysctl\.d(/|$)", r"^/etc/modprobe\.d(/|$)", r"^/etc/modules-load\.d(/|$)",
                   r"^/etc/security(/|$)", r"^/etc/polkit-1(/|$)", r"^/etc/ssh(/|$)", r"^/etc/mailcap(\.order)?$", r"^/etc/(passwd|group)\.lock$", r"^/etc/casaos/modules$"]
    ALLOWED_NAMES = [r"^/DATA/VM(/|$)", r"^/var/lib/casaos/modules(/|$)", r"^/etc/casaos/modules$", r"^/var/log/casaos/(module-virt|virt-management)", r"^/var/(lib/dpkg|lib/apt|cache/apt|log/apt)(/|$)",
                     r"^/etc(/|$)"]

    def judge_m11():
        if not gate("M11", "invariance"):
            return
        a, b, e = snap("m2-ctl-a", "the box before anything of the module"), snap("m2-ctl-b", "the control"), snap("m11-end")
        po = Gx("m3-plan-root")
        owned = Gx("m11-owned")
        if not all((a, b, e)) or not po.answered or not owned.answered:
            if po.answered is False or owned.answered is False:
                invalid("M11: the plan of M3 or the files of the packages (m11-owned) are missing: what the module's packages own cannot be told")
            return
        plan = po.obj() or {}
        planned = set(jget(plan, "packages", "to_install") or []) | set(jget(plan, "packages", "to_upgrade") or [])
        own = set(x for x in owned.out.split("\n") if x.startswith("/"))
        if not own:
            invalid("M11: dpkg lists no file of the module's packages (%s)" % cell(owned.err, 100))
            return
        # what the proof's own `apt-get remove` (the cases that put the module's packages out of the way) took along with them: their dependents (qemu-utils: cloud-image-utils and what hangs on it);
        # it is the proof's doing, not the install's, and says so in the output of those removals
        unseated = set()
        for f in sorted(os.listdir(out_dir)):
            if f.endswith("-unseat.out"):
                for ln in read(f).split("\n"):
                    if ln.startswith("will_remove "):
                        unseated |= set(ln.split()[1:])
        own |= set(x for x in Gx("m11-unseated").out.split("\n") if x.startswith("/"))
        # dpkg lists the files of a merged /usr by the old names (/bin/machinectl), the walk of the file system by the real ones
        own |= {"/usr" + x for x in own if re.match(r"^/(bin|sbin|lib|lib32|lib64|libx32)/", x)}
        # /etc/casaos: nothing changes but what the control shows changing by itself
        c = Case("M11 /etc/casaos")
        noise_ec = {p for p in set(a.etccasaos) | set(b.etccasaos) if a.etccasaos.get(p) != b.etccasaos.get(p)}
        diff = {p for p in set(a.etccasaos) | set(e.etccasaos) if a.etccasaos.get(p) != e.etccasaos.get(p)} - noise_ec
        c.check(not diff, "no file of /etc/casaos is added, changed or gone, once the module is removed", "none", few(sorted(diff)))
        c.note("the control shows %d files of /etc/casaos change by themselves" % len(noise_ec))
        c.done()
        c = Case("M11 dpkg")
        ia, ie = a.installed(), e.installed()
        removed = sorted(n for n in ia if n not in ie and n not in unseated)
        changed = sorted(n for n in ia if n in ie and ia[n] != ie[n])
        added = sorted(n for n in ie if n not in ia)
        noise_pk = {n for n in set(a.dpkg) | set(b.dpkg) if a.dpkg.get(n) != b.dpkg.get(n)}
        c.check(not removed, "no package was removed", "none", few(removed))
        c.check(set(changed) - noise_pk <= planned, "the packages whose version changed are the ones the plan said it would upgrade", few(sorted(planned & set(changed))) or "none", few(sorted(set(changed) - noise_pk - planned)))
        c.check(set(added) - noise_pk <= planned, "the packages that are new are the ones the plan said it would install", few(sorted(planned)[:6]), few(sorted(set(added) - noise_pk - planned)))
        c.check(set(PKGS) <= set(ie), "the packages of the manifest are installed", few(PKGS), few(sorted(set(PKGS) - set(ie))))
        c.note("%d new packages, %d upgraded" % (len(added), len(changed)))
        if unseated & set(ia):
            c.note("the proof's own removals took along: %s" % few(sorted(unseated & set(ia))))
        c.done()
        c = Case("M11 /etc")
        noise_hash = {p for p in set(a.etc) | set(b.etc) if a.etc.get(p) != b.etc.get(p)}
        moved = {p for p in set(a.etc) | set(e.etc) if a.etc.get(p) != e.etc.get(p)} - noise_hash
        explained = {p for p in moved if p in own or any(re.search(rx, p) for rx in ALLOWED_ETC)}
        c.check(bool(a.etc) and bool(e.etc), "the hash of /etc was taken before and after", "both", "%d and %d files" % (len(a.etc), len(e.etc)))
        c.check(not (moved - explained), "every file of /etc that is new, changed or gone belongs to the packages the install added, or is what their scripts make", "none unexplained", few(sorted(moved - explained)))
        c.note("%d files of /etc differ, %d explained by the packages, %d by the control" % (len(moved), len(explained), len(noise_hash)))
        c.done()
        c = Case("M11 the rest of the file system")
        noise = noise_of(a, b)
        delta = set(n for n in (a.names ^ e.names) if not NOISE_NAME.search(n)) - noise
        # the core's own state areas change by themselves over hours (the control is minutes long): their names are counted, and not judged; what the module could write there is held by M8
        core_own = {n for n in delta if (n.startswith("/var/lib/casaos/") and not n.startswith("/var/lib/casaos/modules")) or (n.startswith("/var/log/casaos/") and not re.match(r"^/var/log/casaos/(module-virt|virt-management)", n))}
        unexplained = {n for n in delta if n not in own and n not in core_own and not any(re.search(rx, n) for rx in ALLOWED_NAMES)}
        c.check(not unexplained, "every name that is new or gone is a file of the packages, the module's data or what the assistant keeps", "none unexplained", few(sorted(unexplained)))
        if core_own:
            c.note("%d names of the core's own state (/var/lib/casaos, /var/log/casaos) differ and are not judged here" % len(core_own))
        # (the private directories of the services are named by the boot: a restart of the machine renames all of them)
        tmp_of = lambda s: {n for n in s if n.startswith("/var/tmp/") and not n.startswith("/var/tmp/systemd-private-")}
        c.check(tmp_of(a.names) == tmp_of(e.names), "/var/tmp holds what it held", "the same", few(sorted(tmp_of(a.names) ^ tmp_of(e.names))))
        left = sorted(n for n in e.names if n.startswith("/var/lib/casaos/modules"))
        c.note("the assistant keeps: %s" % few(left, 4))
        c.done()

    # ---- the order of the report -------------------------------------------------------------------------------------------------------

    def run_judge(fn, cid=None):
        try:
            fn()
        except Exception as ex:      # the evidence was not what a judge expected: the run cannot be trusted, and the exception is named
            invalid("the judgement of %s broke on its evidence: %s: %s" % (cid or fn.__name__, type(ex).__name__, cell(ex, 200)))

    for fn, cid in ((judge_m1, "M1"), (judge_m2, "M2"), (judge_m3, "M3"), (judge_m4, "M4"), (judge_m5, "M5"), (judge_m6, "M6"), (judge_m7, "M7"), (judge_m8, "M8"), (judge_m9, "M9"),
                    (judge_m10, "M10"), (judge_m11, "M11")):
        run_judge(fn, cid)

    box = {}
    for ln in read("guest/box-facts").splitlines():
        k, _, v = ln.partition(" ")
        if k:
            box[k] = v.strip()
    if box:
        P("Guest: %s, kernel %s, apt %s, systemd %s, Docker %s, %s CPUs, %s MB." % (cell(read("guest/os").strip() or "?", 40), cell(box.get("kernel", "?"), 40), cell(box.get("apt", "?"), 40),
                                                                                  cell(box.get("systemd", "?"), 40), cell(box.get("docker", "?"), 20), cell(box.get("cpus", "?"), 4), cell(box.get("memory_mb", "?"), 8)))
    finish()
except SystemExit:
    raise
except Exception as e:     # evidence this script did not expect (a number of four hundred digits, an answer nested a hundred thousand deep): the run cannot be trusted, and the exception is named
    invalid("the report broke on its evidence: %s: %s" % (type(e).__name__, cell(e, 300)))
    finish()
