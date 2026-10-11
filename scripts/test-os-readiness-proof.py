#!/usr/bin/env python3
"""The proof has to be able to say no. Each case below builds the directory a leg would leave, in a state that must come out a given way, and checks what
os-readiness-proof-report.py says and its exit status:

    python3 scripts/test-os-readiness-proof.py

A healthy leg of each of the two (d11, u22) passes. Every verdict has a case in which only the thing it judges is wrong, and that case must fail by that
name; a run that did not get as far as it should (a step that did not run, a call that got no answer of the route, a box that could not be dirtied or put
back, evidence that is missing) must be INVALID and never green. What these guests cannot do is NOT TESTABLE, with its reason, and neither a pass nor a fail.

The core is not here: the answers of the cases are what the real core said, dumped from it (fixtures/readiness/<leg>/<step>.json, see the README there), the
clean box's and each dirty box's, and the cases that need a shape the core never makes edit them. The same fixtures, behind a stand-in for curl, answer the
END-TO-END cases, which run the REAL guest functions of os-readiness-proof.sh (the control, the invariance window, the dirty matrix, with the real manifest tool)
against stand-ins for systemctl, docker, apt-mark, dpkg and the rest, and hand what they wrote to the REAL report: the plumbing between the guest and the
report is tested as one, and a core that writes a file during the call, restarts a unit, opens a port, blocks, fails to fire a code or fires another one must
come out red by the name of the verdict that catches it. The text-only functions of the guest script, each step's dirt and undo, its runner and its traps are
tested too, when bash is here. No VM leg can run here: what the real tools, systemd, apt and Docker say is what the first dispatch of the workflow shows.

REPORT=<path>, READINESS_SCRIPT=<path>, WORKFLOW=<path> and FIXTURES=<dir> run the cases against other copies, to see that a broken one is caught. CASES=<regex> runs the cases
whose name matches. REPORT_ONLY=1 stops after the cases of the report, GUEST_ONLY=1 skips them. TEST_BASH=<path> names a bash where plain `bash` is not the one.
"""
import ast
import builtins
import contextlib
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.environ.get("REPORT") or os.path.join(HERE, "os-readiness-proof-report.py")
SCRIPT = os.environ.get("READINESS_SCRIPT") or os.path.join(HERE, "os-readiness-proof.sh")
WORKFLOW = os.environ.get("WORKFLOW") or os.path.join(HERE, "..", ".github", "workflows", "os-readiness-proof.yml")
LEGS_JSON = os.path.join(HERE, "os-readiness-proof-legs.json")
ONLY = os.environ.get("CASES")
VERDICT = os.environ.get("VERDICT")     # only the cases that expect this verdict by name (a text that their must_have has): what a mutation of one verdict is checked against
BASH = os.environ.get("TEST_BASH") or "bash"
T0 = 1790000000.0
ENV = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")

failures = []
count = [0]


def wanted(name):
    return not ONLY or re.search(ONLY, name) is not None


def check(label, ok, detail=""):
    if not wanted(label):
        return
    count[0] += 1
    if ok:
        print("ok: " + label)
    else:
        failures.append("%s: %s" % (label, detail))


# ---- the answers of the core: its own, dumped from it ------------------------------------------------------------------------------------------------------

FIXTURES = os.environ.get("FIXTURES") or os.path.join(HERE, "fixtures", "readiness")

ANSWERS_SRC = r'''
import json
import os

DOCKER_FAMILY = ("docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin", "docker-ce-rootless-extras")
SEVERITIES = ("refuse", "warn", "ok", "info")


def recount(data):
    data["counts"] = {s: sum(1 for c in data["checks"] if c["severity"] == s) for s in SEVERITIES}
    data["state"] = "blocked" if data["counts"]["refuse"] else "attention" if data["counts"]["warn"] else "ready"


def put(data, code, severity, title, unknown=False):
    """the check of the code has this severity (it is added when the answer has none)"""
    found = [c for c in data["checks"] if c["code"] == code]
    if not found:
        data["checks"].append(dict(code=code, severity=severity, title=title, data={}, action="none"))
        found = data["checks"][-1:]
    for c in found:
        c.update(severity=severity, title=title)
        c["data"]["unknown"] = unknown
        if unknown:
            c["data"]["reason"] = "unexpected_output"
    recount(data)


def answer(step, leg, at, mode=""):
    """the body of GET /v1/sys/os/readiness for the box of the leg, clean (step "") or dirtied by one step of the matrix: the answer that the core itself made
    (fixtures/readiness/<leg>/<step>.json), dated `at`. mode is a way for the stand-in core of the end-to-end cases to be wrong"""
    with open(os.path.join(FIXTURES_DIR, leg, (step or "clean") + ".json"), encoding="utf-8") as f:
        data = json.load(f)
    data["text"] = data["text"].replace(data["checked_at"], at)
    data["checked_at"] = at
    if mode.startswith("extra:"):
        put(data, mode.split(":", 1)[1], "refuse", "A check that should not fire")
    if mode == "nobusy":
        put(data, "busy", "ok", "busy is fine")
    if mode == "apt_fails":
        # apt says no to a Default-Release that no source offers, in every command that opens its cache: the checks that run two of them could not ask
        for code in ("broken_dpkg", "docker_origin", "docker_update_pending"):
            put(data, code, "refuse", "Could not check: apt failed", unknown=True)
    return {"success": 200, "message": "ok", "data": data}
'''
ANSWERS = {"FIXTURES_DIR": FIXTURES}
exec(ANSWERS_SRC, ANSWERS)
recount = ANSWERS["recount"]
# the four containers that come back by themselves, which the proof starts
WITNESSES = [dict(name="p-always", policy="always", network="bridge", ports=""), dict(name="p-unless-stopped", policy="unless-stopped", network="bridge", ports=""),
             dict(name="p-db", policy="unless-stopped", network="bridge", ports=""), dict(name="p-web", policy="unless-stopped", network="bridge", ports="18081")]


# ---- the dirty matrix of the report, as the harness knows it ---------------------------------------------------------------------------------------------

def report_steps():
    """the Step(...) rows of the report's STEPS, read from its source without running it: [dict]"""
    with open(REPORT, newline="") as f:
        tree = ast.parse(f.read().replace("\r\n", "\n"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "STEPS" for t in node.targets):
            rows = []
            for call in node.value.elts:
                row = dict(zip(("id", "code"), [ast.literal_eval(a) for a in call.args]))
                row.update({k.arg: ast.literal_eval(k.value) for k in call.keywords})
                rows.append(row)
            return rows
    raise SystemExit("the report has no STEPS")


STEPS = report_steps()
for s in STEPS:
    s.setdefault("mode", "fires")
    s.setdefault("sev", "refuse")
    s.setdefault("needle", "")
    s.setdefault("forbid", ())
    s.setdefault("extras", ())
STEP_BY_ID = {s["id"]: s for s in STEPS}

LEG = {"d11": dict(family="debian", version="11", codename="bullseye", pretty="Debian GNU/Linux 11 (bullseye)", to_version="12", to_codename="bookworm"),
       "u22": dict(family="ubuntu", version="22.04", codename="jammy", pretty="Ubuntu 22.04.5 LTS", to_version="24.04", to_codename="noble")}
HOST, IPV4, IPV6, MAC, USER = "osr-1a2b3c4d", "10.0.2.15", "fe80::5054:ff:fe12:3456", "52:54:00:12:34:56", "casa"

fixture_files = {leg_: sorted(f[:-5] for f in os.listdir(os.path.join(FIXTURES, leg_)) if f.endswith(".json")) for leg_ in LEG}
check("the core's answers are there for the clean box and for each step of the matrix, on both systems, and for nothing else",
      all(files == sorted(["clean"] + [s["id"] for s in STEPS]) for files in fixture_files.values()), fixture_files)
check("the report says what the pin step names (the box's own release, which apt does not fail on) and the publish step takes (host port 53 on a loopback alias: the gateway holds 80 and 443)",
      "own release" in STEP_BY_ID["sources_pin"]["what"] and "port 53" in STEP_BY_ID["containers_ports_publish"]["what"] and "443" in STEP_BY_ID["containers_ports_publish"]["what"]
      and "loopback" in STEP_BY_ID["containers_ports_publish"]["what"], (STEP_BY_ID["sources_pin"]["what"], STEP_BY_ID["containers_ports_publish"]["what"]))


# ---- the evidence directory a healthy leg leaves ----------------------------------------------------------------------------------------------------------

def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class VDir:
    """a directory of evidence that lives in memory: the cases of the report build hundreds of them, and the report is run on them in this process"""

    def __init__(self):
        self.files = {}


def put(d, name, text):
    if isinstance(d, VDir):
        d.files[name] = text
        return
    with open(os.path.join(d, name), "w", newline="\n") as f:
        f.write(text)


def slurp(d, name):
    if isinstance(d, VDir):
        return d.files[name].replace("\r\n", "\n")
    with open(os.path.join(d, name), newline="") as f:
        return f.read().replace("\r\n", "\n")


def exists(d, name):
    return name in d.files if isinstance(d, VDir) else os.path.exists(os.path.join(d, name))


def api(d, name, code=200, body=None, t0=None, secs=1.2, curl=0, err=None, raw=None):
    """the files of one call the way the guest writes them: body, status, how curl ended, the time before and after (.curl last, as the guest does)"""
    t0 = T0 if t0 is None else t0
    put(d, name + ".t0", "%.6f\n" % t0)
    put(d, name + ".t1", "%.6f\n" % (t0 + secs))
    if raw is not None:
        put(d, name + ".json", raw)
    elif body is not None:
        put(d, name + ".json", json.dumps(body))
    if err:
        put(d, name + ".err", err)
    put(d, name + ".code", "%03d\n" % code)
    put(d, name + ".curl", "%d\n" % curl)


def ready_body(leg, step, at, mode=""):
    """the answer of the core for the box of the leg with the dirt of a step (none: the clean box), at a time"""
    return ANSWERS["answer"](step, leg, iso(at), mode)


TREE_ROWS = {
    "/etc": ("d", "4096", "1790000000000000000", "1790000000000000000", "755", "0:0", "-"),
    "/etc/os-release": ("f", "260", "1780000000000000000", "1780000000000000000", "644", "0:0", "a" * 64),
    "/etc/fstab": ("f", "112", "1780000000000000000", "1780000000000000000", "644", "0:0", "b" * 64),
    "/etc/apt/sources.list": ("f", "300", "1780000000000000000", "1780000000000000000", "644", "0:0", "c" * 64),
    "/etc/casaos/gateway.ini": ("f", "90", "1790000100000000000", "1790000100000000000", "644", "0:0", "d" * 64),
    "/var/lib/dpkg": ("d", "4096", "1790000000000000000", "1790000000000000000", "755", "0:0", "-"),
    "/var/lib/dpkg/status": ("f", "800000", "1790000000000000000", "1790000000000000000", "644", "0:0", "e" * 64),
    "/var/lib/dpkg/lock-frontend": ("f", "0", "1780000000000000000", "1780000000000000000", "640", "0:0", "f" * 64),
    "/var/lib/apt": ("d", "4096", "1790000000000000000", "1790000000000000000", "755", "0:0", "-"),
    "/var/lib/apt/lists/deb.debian.org_Packages": ("f", "9000000", "1790000000000000000", "1790000000000000000", "644", "0:0", "1" * 64),
    "/var/cache/apt": ("d", "4096", "1790000000000000000", "1790000000000000000", "755", "0:0", "-"),
    "/var/cache/apt/pkgcache.bin": ("f", "50000000", "1790000000000000000", "1790000000000000000", "644", "0:0", "2" * 64),
    "/var/lib/casaos": ("d", "4096", "1790000000000000000", "1790000000000000000", "755", "0:0", "-"),
    "/var/lib/casaos/db/casaos.db": ("f", "65536", "1790000200000000000", "1790000200000000000", "644", "0:0", "3" * 64),
    "/var/log/casaos": ("d", "4096", "1790000300000000000", "1790000300000000000", "755", "0:0", "-"),
    "/var/log/casaos/casaos.log": ("f", "20000", "1790000300000000000", "1790000300000000000", "644", "0:0", "4" * 64),
}
UNITS = {"casaos.service": ("active", "running", "811", "inv-core"), "casaos-gateway.service": ("active", "running", "812", "inv-gw"),
         "casaos-app-management.service": ("active", "running", "813", "inv-am"), "casaos-message-bus.service": ("active", "running", "814", "inv-mb"),
         "docker.service": ("active", "running", "900", "inv-docker"), "ssh.service": ("active", "running", "700", "inv-ssh"),
         "logrotate.service": ("inactive", "dead", "0", "inv-logrotate"), "systemd-tmpfiles-clean.service": ("inactive", "dead", "0", "inv-tmpfiles")}
PORTS = ["tcp 0.0.0.0:22", "tcp *:80", "tcp *:443", "tcp 127.0.0.1:41873", "tcp 0.0.0.0:18081", "udp 0.0.0.0:68", "tcp 127.0.0.53:53", "udp 127.0.0.53:53"]
LISTEN_SORTED = "".join(p + "\n" for p in sorted(PORTS))   # what `sort` says of them, in the C locale
ALL_VERDICTS = None   # filled by the healthy cases


def manifest_text(rows):
    return "".join("\t".join([json.dumps(p)] + list(r)) + "\n" for p, r in sorted(rows.items()))


def build(d, leg):
    """the evidence of a healthy leg: the answers are the core's own (the fixtures), clean and with the dirt of each step"""
    L = LEG[leg]
    put(d, "leg", leg + "\n")
    put(d, "os", L["pretty"] + "\n")
    put(d, "os-release", 'PRETTY_NAME="%s"\nNAME="%s"\nVERSION_ID="%s"\nVERSION_CODENAME=%s\nID=%s\n' % (L["pretty"], L["family"].title(), L["version"], L["codename"], L["family"]))
    put(d, "box-facts", "kernel 5.10.0-46-cloud-amd64\ncpus 4\nmemory_mb 5943\nboot bios\nroot ext4 /dev/vda1\nboot_on /dev/vda1 (root: /dev/vda1)\nvirt kvm\ndocker 28.0.4\n"
                        "apt apt 2.2.4 (amd64)\n")
    put(d, "leak-needles.tsv", "hostname\t%s\nipv4\t%s\nipv4\t172.17.0.1\nipv6\t%s\nmac\t%s\nuser\t%s\n" % (HOST, IPV4, IPV6, MAC, USER))
    put(d, "timeline.tsv", "#\t%.6f\tprepare_start\n#\t%.6f\tinvariance_start\n" % (T0, T0 + 100))
    put(d, "finished", iso(T0 + 3000) + "\n")
    put(d, "timer-units.txt", "logrotate.service\nsystemd-tmpfiles-clean.service\n")
    base = dict(TREE_ROWS)
    after = dict(base)
    after["/var/log/casaos"] = ("d", "4096", "1790000900000000000", "1790000900000000000", "755", "0:0", "-")
    after["/var/log/casaos/casaos.log"] = ("f", "21000", "1790000900000000000", "1790000900000000000", "644", "0:0", "5" * 64)
    after["/var/log/casaos/readiness.log"] = ("f", "100", "1790000900000000000", "1790000900000000000", "644", "0:0", "6" * 64)
    for tag, rows in (("ctl-a", base), ("ctl-b", base), ("before", base), ("after", after)):
        put(d, "inv-%s.tsv" % tag, manifest_text(rows))
        put(d, "units-%s.tsv" % tag, "".join("%s\t%s\n" % (u, "\t".join(v)) for u, v in sorted(UNITS.items())))
        put(d, "listen-%s.txt" % tag, "\n".join(sorted(PORTS)) + "\n")
    n = [0]

    def at():
        n[0] += 1
        return T0 + 200 + n[0] * 5

    for name in ("ctl-core-1", "ctl-core-2"):
        api(d, name, body={"success": 200, "message": "ok", "data": "v0.5.20"}, t0=at())
    api(d, "sysos-inv", t0=at(), body={"success": 200, "message": "ok", "data": {"id": L["family"], "version_id": L["version"], "codename": L["codename"], "pretty_name": L["pretty"],
                                                                              "arch": "amd64", "known": True, "next": {"id": L["family"], "version_id": L["to_version"], "codename": L["to_codename"], "state": "report"}}})
    for name in ("ready-inv-1", "ready-inv-2", "ready-inv-3"):
        t = at()
        api(d, name, t0=t, body=ready_body(leg, "", t + 1))
    t = at()
    api(d, "ready-inv-lock", t0=t, secs=2.0, body=ready_body(leg, "busy_lock", t + 1))
    put(d, "lock-probe-during", "held\n")
    put(d, "lock-probe-after", "held\n")
    t = at()
    api(d, "ready-base", t0=t, body=ready_body(leg, "", t + 1))
    put(d, "steps.list", "\n".join(s["id"] for s in STEPS) + "\n")
    for s in STEPS:
        sid = s["id"]
        put(d, "step-%s.dirty-rc" % sid, "0\n")
        put(d, "step-%s.undo-rc" % sid, "0\n")
        put(d, "step-%s.undone" % sid, "yes\n")
        t = at()
        api(d, "step-%s-dirty" % sid, t0=t, body=ready_body(leg, sid, t + 1))
        t = at()
        api(d, "step-%s-clean" % sid, t0=t, body=ready_body(leg, "", t + 1))


def jedit(d, name, fn):
    j = json.loads(slurp(d, name))
    fn(j)
    put(d, name, json.dumps(j))


def jdata(d, call, fn):
    jedit(d, call + ".json", lambda j: fn(j["data"]))


def set_sev(d, call, code, sev):
    def fn(data):
        for c in data["checks"]:
            if c["code"] == code:
                c["severity"] = sev
        recount(data)
    jdata(d, call, fn)


def add_check(d, call, code, sev, **kw):
    """the check is added (a second one of the same code when there is one: that is a case of its own)"""
    def fn(data):
        data["checks"].append(dict(dict(code=code, severity=sev, title=code + " title", detail="detail", data={"unknown": False}, action="none"), **kw))
        recount(data)
    jdata(d, call, fn)


def make(d, call, code, sev):
    """the code has this severity in this answer: the check is changed when it is there, added when it is not"""
    def fn(data):
        have = [c for c in data["checks"] if c["code"] == code]
        for c in have:
            c["severity"] = sev
        if not have:
            data["checks"].append(dict(code=code, severity=sev, title=code + " title", detail="detail", data={"unknown": False}, action="none"))
        recount(data)
    jdata(d, call, fn)


def drop_check(d, call, code):
    def fn(data):
        data["checks"] = [c for c in data["checks"] if c["code"] != code]
        recount(data)
    jdata(d, call, fn)


def ledit(d, name, fn):
    put(d, name, fn(slurp(d, name)))


def replace(d, name, old, new, n=-1):
    def fn(t):
        assert old in t, "%r is not in %s" % (old, name)
        return t.replace(old, new, n)
    ledit(d, name, fn)


def remove(d, *names):
    for n in names:
        if isinstance(d, VDir):
            d.files.pop(n, None)
            continue
        try:
            os.remove(os.path.join(d, n))
        except OSError:
            pass


def all_calls(d):
    names = d.files if isinstance(d, VDir) else os.listdir(d)
    return sorted({f[:-5] for f in names if f.endswith(".json") and (f.startswith("ready-") or f.startswith("step-"))})


# ---- the cases of the report ---------------------------------------------------------------------------------------------------------------------------

VROOT = "/virtual-out"


def run_report(d, leg):
    """(exit status, what it printed). An evidence directory on disk is a run of the report as a process; one in memory is a run in this process, with the files
    of the report's own directory served from the dictionary: hundreds of cases, seconds instead of minutes (`open` and `os.path.exists` are the only things it asks of the disk)"""
    if not isinstance(d, VDir):
        p = subprocess.run([sys.executable, REPORT, d, leg], capture_output=True, text=True, env=ENV, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout + p.stderr
    real_open, real_exists = builtins.open, os.path.exists

    def name_of(path):
        p = os.fspath(path).replace("\\", "/")
        return p[len(VROOT) + 1:] if p.startswith(VROOT + "/") else None

    def fake_open(path, mode="r", *a, **k):
        n = name_of(path)
        if n is None:
            return real_open(path, mode, *a, **k)
        if "w" in mode:
            return io.StringIO()
        if n in d.files:
            return io.StringIO(d.files[n])
        raise FileNotFoundError(path)

    def fake_exists(path):
        n = name_of(path)
        return real_exists(path) if n is None else n in d.files
    buf, code, argv = io.StringIO(), 0, sys.argv
    sys.argv = [REPORT, VROOT, leg]
    builtins.open, os.path.exists = fake_open, fake_exists
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                runpy.run_path(REPORT, run_name="__main__")
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            except Exception:
                traceback.print_exc(file=buf)
                code = 1
    finally:
        builtins.open, os.path.exists, sys.argv = real_open, real_exists, argv
    return code, buf.getvalue()


def case(name, want_code, must_have=(), must_not_have=(), leg="d11", mutate=None, empty=False):
    if not wanted(name):
        return
    if VERDICT and not any(VERDICT in s for s in must_have):
        return
    count[0] += 1
    d = VDir()
    if not empty:
        build(d, leg)
    if mutate:
        mutate(d)
    code, text = run_report(d, leg)
    problems = []
    if code != want_code:
        problems.append("exit %d, wanted %d" % (code, want_code))
    problems += ["missing %r" % s for s in must_have if s not in text]
    problems += ["should not say %r" % s for s in must_not_have if s in text]
    if "Traceback" in text:
        problems.append("crashed")
    if os.environ.get("SHOW"):
        print(text)
    if problems:
        failures.append("%s: %s\n%s" % (name, "; ".join(problems), text[-1800:]))
    else:
        print("ok:", name)


def fails(name, verdict, mutate, leg="d11", extra=()):
    """a case in which one thing is wrong and the verdict of that name has to say FAIL (and the harness has nothing to say against the run)"""
    case(name, 1, ["**FAIL** " + verdict] + list(extra), ["INVALID"], leg=leg, mutate=mutate)


def invalid(name, reason, mutate, leg="d11", empty=False, silent=(), has=(), lacks=()):
    """a run that proves nothing; the verdicts named in silent must not be made up from the evidence that is not there"""
    case(name, 1, ["INVALID RUN", reason] + list(has), ["**FAIL** " + s for s in silent] + list(lacks), leg=leg, mutate=mutate, empty=empty)


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
GLOBAL_VERDICTS = [V_ROUTE, V_SHAPE, V_COUNTS, V_HOP, V_FIELDS, V_TEXT, V_TIME, V_BUDGET, V_SYSOS, V_INV_FILES, V_INV_UNITS, V_INV_PORTS, V_LOCK_FAST, V_LOCK_BUSY, V_LOCK_KEPT,
                   V_CLEAN_NO_REFUSE, V_CLEAN_STATE, V_CLEAN_AGREE, V_CLEAN_DOCKER, V_CLEAN_SNAPSHOT]


def f_fires(s):
    if s["mode"] == "names":
        return "step %s: the dirty box makes `%s` %s and name %s" % (s["id"], s["code"], s["sev"], s["needle"])
    return "step %s: the dirty box makes `%s` %s" % (s["id"], s["code"], s["sev"])


def f_quiet(s):
    return "step %s: the dirty box leaves `%s` quiet" % (s["id"], s["code"])


def f_undone(s):
    return "step %s: after the undo the answer is the clean one again (the code is gone, nothing else is left)" % s["id"]


def f_alone(s):
    return "step %s: nothing else fires with it" % s["id"]


def f_forbid(s, code):
    return "step %s: `%s` does not fire" % (s["id"], code)


def steps_of(leg):
    return list(STEPS)


def step_verdicts(s):
    out = [f_quiet(s) if s["mode"] == "quiet" else f_fires(s), f_alone(s), f_undone(s)] + [f_forbid(s, c) for c in s["forbid"]]
    return out


def healthy_verdicts(leg):
    v = list(GLOBAL_VERDICTS)
    for s in steps_of(leg):
        v += step_verdicts(s)
    return v


if os.environ.get("LIST_VERDICTS"):
    print(json.dumps(sorted({v for leg_ in ("d11", "u22") for v in healthy_verdicts(leg_)})))
    sys.exit(0)

NOT_TESTABLE_NAMES = ["uptime_long", "container", "virt_xen", "unproven_arch", "apps (an operation in progress)"]

if not os.environ.get("GUEST_ONLY"):
    # ---- healthy legs ----
    for leg_ in ("d11", "u22"):
        case("a healthy %s leg passes, and every verdict says so by name" % leg_, 0,
             ["# OS readiness proof, %s leg: PASS" % leg_] + ["**PASS** " + v for v in healthy_verdicts(leg_)] + ["**NOT TESTABLE** " + n for n in NOT_TESTABLE_NAMES],
             ["FAIL", "INVALID", "Traceback"], leg=leg_)
    case("a healthy u22 leg runs the alias step, as Debian does (devel is Ubuntu's alias)", 0, ["**PASS** " + f_fires(STEP_BY_ID["sources_alias"])], ["NOT TESTABLE** step sources_alias"], leg="u22")
    case("a healthy d11 leg runs the alias step", 0, ["**PASS** " + f_fires(STEP_BY_ID["sources_alias"])], ["NOT TESTABLE** step sources_alias"])
    case("the report lists the clean box check by check, with the text of the clean answer and the codes the core added", 0,
         ["## The clean box, check by check", "| snapshot_reminder | info |", "Codes the core added to the spec's table: none.", "This check changes nothing on the machine."])
    case("a core that added a code is told in the report", 0, ["Codes the core added to the spec's table: distro_quirk"],
         mutate=lambda d: [add_check(d, n, "distro_quirk", "info") for n in all_calls(d)])
    case("the leg is not one of the two", 1, ["INVALID RUN", "d11 or u22"], [], leg="sideways", empty=True)
    p_ = subprocess.run([sys.executable, REPORT], capture_output=True, text=True, env=ENV)
    check("no argument at all is a usage error and not a crash", p_.returncode == 2 and "usage" in p_.stderr and "Traceback" not in p_.stderr, p_.stderr[-200:])

    # ---- the answers of the route: every call is judged ----
    for call_ in ("ready-inv-2", "step-held-dirty", "step-docker_down-clean", "ready-base"):
        fails("an answer with an HTTP 500 (%s)" % call_, V_ROUTE, lambda d, c=call_: api(d, c, code=500, body={"success": 500, "message": "boom"}))
    fails("a call that blocked: curl gave up on it", V_ROUTE, lambda d: api(d, "step-apps-dirty", code=0, curl=28, raw="", secs=45.0, err="Operation timed out"))
    fails("a call that was cut off after the status", V_ROUTE, lambda d: api(d, "step-held-clean", code=200, curl=56, raw="{"))
    fails("an answer that says 200 and is not JSON", V_ROUTE, lambda d: api(d, "ready-inv-3", raw="<html>gateway</html>"))
    fails("an answer that is a JSON list", V_ROUTE, lambda d: api(d, "ready-inv-3", raw="[1]"))
    invalid("a release that has no such route (404) is an INVALID RUN, by name", "ready-inv-1 got no answer of the route", lambda d: api(d, "ready-inv-1", code=404, body={"message": "no"}),
            has=["the release under test has no such route"], silent=[V_ROUTE, V_SHAPE])
    for code_ in (0, 401, 403, 502, 503, 504):
        invalid("HTTP %03d on the first call is no answer of the route: INVALID" % code_, "got no answer of the route", lambda d, c=code_: api(d, "ready-inv-1", code=c, curl=7 if c == 0 else 0, raw=""))
    invalid("a call that left no status", "no answer was recorded for step-busy_unit-dirty", lambda d: remove(d, "step-busy_unit-dirty.code"), silent=[V_ROUTE])
    invalid("a call that left no record of how curl ended", "step-busy_unit-dirty.curl, .code, .t0 or .t1 is missing", lambda d: remove(d, "step-busy_unit-dirty.curl"))
    invalid("a call that left no time", "ready-inv-2.curl, .code, .t0 or .t1 is missing", lambda d: remove(d, "ready-inv-2.t1"))
    invalid("the control's calls of the core that answered 500: the control did not run", "the control of the invariance window did not run", lambda d: api(d, "ctl-core-1", code=500, body={}))

    # ---- the shape of an answer ----
    def shape(fn):
        return lambda d: jdata(d, "step-reboot_first-dirty", fn)

    fails("an answer without a state", V_SHAPE, shape(lambda x: x.pop("state")))
    fails("an answer with a state that is not one", V_SHAPE, shape(lambda x: x.update(state="great")))
    fails("an answer whose counts are a list", V_SHAPE, shape(lambda x: x.update(counts=[1, 2])))
    fails("an answer whose counts have a fifth severity", V_SHAPE, shape(lambda x: x["counts"].update(fatal=1)))
    fails("an answer whose counts are negative", V_SHAPE, shape(lambda x: x["counts"].update(info=-1)))
    fails("an answer whose checks are not a list", V_SHAPE, shape(lambda x: x.update(checks={})))
    fails("an answer with a check that is not an object", V_SHAPE, shape(lambda x: x["checks"].append("busy")))
    fails("an answer without a text", V_SHAPE, shape(lambda x: x.pop("text")))
    fails("an answer with an empty text", V_SHAPE, shape(lambda x: x.update(text="  \n")))
    fails("an answer whose checked_at is not a time", V_SHAPE, shape(lambda x: x.update(checked_at="yesterday")))
    fails("an answer without hop.from", V_SHAPE, shape(lambda x: x["hop"].pop("from")))
    fails("an answer whose hop.to has no codename", V_SHAPE, shape(lambda x: x["hop"]["to"].pop("codename")))
    fails("an answer that is not in the envelope (no data)", V_SHAPE, lambda d: jedit(d, "ready-inv-1.json", lambda j: j.update(data="x")))
    fails("an answer whose success is not 200", V_SHAPE, lambda d: jedit(d, "ready-inv-1.json", lambda j: j.update(success=0)))
    case("an answer with an unknown severity or a check of garbage never crashes the report", 1, ["**FAIL**"], ["Traceback"],
         mutate=lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"].extend([{"code": ["x"], "severity": {}}, {"code": "ok_code", "severity": ["refuse"]}, {"code": 5}])))

    # ---- counts and state ----
    fails("counts that do not tally the checks", V_COUNTS, lambda d: jdata(d, "step-held-clean", lambda x: x["counts"].update(ok=x["counts"]["ok"] + 1)))
    fails("a state `ready` with a refuse in the checks", V_COUNTS, lambda d: jdata(d, "step-held-dirty", lambda x: x.update(state="ready")))
    fails("a state `blocked` with nothing to refuse", V_COUNTS, lambda d: jdata(d, "step-held-clean", lambda x: x.update(state="blocked")))
    fails("a state `ready` with a warn", V_COUNTS, lambda d: jdata(d, "ready-inv-1", lambda x: x.update(state="ready")))
    fails("a state `attention` with a refuse", V_COUNTS, lambda d: jdata(d, "step-held-dirty", lambda x: x.update(state="attention")))

    # ---- the hop ----
    fails("a hop that goes to the wrong release", V_HOP, lambda d: jdata(d, "ready-inv-1", lambda x: x["hop"]["to"].update(version_id="13", codename="trixie")))
    fails("a hop that starts from another release than the box's", V_HOP, lambda d: jdata(d, "ready-inv-1", lambda x: x["hop"]["from"].update(codename="buster")))
    fails("a hop of another family", V_HOP, lambda d: jdata(d, "ready-inv-1", lambda x: x["hop"]["to"].update(id="ubuntu")), leg="d11")
    fails("a box that is `latest`", V_HOP, lambda d: jdata(d, "ready-inv-1", lambda x: x.update(state="latest")))
    fails("a box that is `unsupported`", V_HOP, lambda d: jdata(d, "ready-inv-1", lambda x: x.update(state="unsupported")))

    # ---- the fields of a check ----
    fails("a code twice", V_FIELDS, lambda d: add_check(d, "ready-inv-1", "busy", "ok"))
    fails("a code that is not a code", V_FIELDS, lambda d: add_check(d, "ready-inv-1", "Not A Code", "ok"))
    fails("a severity that is not one", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0].update(severity="fatal")))
    fails("an action that is not of the list", V_FIELDS, lambda d: jdata(d, "step-held-dirty", lambda x: [c.update(action="reboot_now") for c in x["checks"] if c["code"] == "held"]))
    fails("an action that is a word near the list (reboot)", V_FIELDS, lambda d: jdata(d, "step-held-dirty", lambda x: [c.update(action="reboot") for c in x["checks"] if c["code"] == "held"]))
    case("every action of the list is one", 0, ["**PASS** " + V_FIELDS], ["FAIL"],
         mutate=lambda d: jdata(d, "ready-inv-1", lambda x: [c.update(action=a) for c, a in zip(x["checks"], ("restart_machine", "finish_dpkg", "wait_for_package_manager", "wait_for_apps", "free_disk_space", "release_holds", "review_sources", "none"))]))
    case("a code of 40 characters is a code", 0, ["**PASS** " + V_FIELDS], ["FAIL"], mutate=lambda d: add_check(d, "step-held-dirty", "c" + "a" * 39, "ok"))
    fails("a code of 41 characters is not a code", V_FIELDS, lambda d: add_check(d, "ready-inv-1", "c" + "a" * 40, "ok"))
    fails("a code that starts with a digit", V_FIELDS, lambda d: add_check(d, "ready-inv-1", "1abc", "ok"))
    fails("a title that is empty", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0].update(title="")))
    fails("a title that is missing", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0].pop("title")))
    fails("a title that is not a string", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0].update(title=["t"])))
    fails("a title of 2001 characters", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0].update(title="t" * 2001)))
    case("a title of 2000 characters", 0, ["**PASS** " + V_FIELDS], ["FAIL"], mutate=lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0].update(title="t" * 2000)))
    fails("a detail that is not a string", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0].update(detail=5)))
    fails("a detail that is null", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0].update(detail=None)))
    fails("a detail of 2001 characters", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0].update(detail="d" * 2001)))
    case("a detail of 2000 characters", 0, ["**PASS** " + V_FIELDS], ["FAIL"], mutate=lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0].update(detail="d" * 2000)))
    case("a detail that is missing is what the core sends of a check with nothing to say, and a detail that is empty is fine", 0, ["**PASS** " + V_FIELDS], ["FAIL"],
         mutate=lambda d: [jdata(d, n, lambda x: (x["checks"][0].pop("detail", None), x["checks"][1].update(detail=""))) for n in all_calls(d)])
    fails("a data that is a list", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0].update(data=[1])))
    fails("a check without data", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0].pop("data")))
    case("a data value that is a list of short strings, as the core sends them, and an empty one", 0, ["**PASS** " + V_FIELDS], ["FAIL"],
         mutate=lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0]["data"].update(names=["a", "b"], none=[], files=["docker.list"], mixed="x")))
    case("a list of 50 strings", 0, ["**PASS** " + V_FIELDS], ["FAIL"], mutate=lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0]["data"].update(names=["n%d" % i for i in range(50)])))
    fails("a list of 51 strings (raw tool output?)", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0]["data"].update(names=["n%d" % i for i in range(51)])), extra=["more than 50 items"])
    case("a list with a string of 200 characters", 0, ["**PASS** " + V_FIELDS], ["FAIL"], mutate=lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0]["data"].update(names=["n" * 200])))
    fails("a list with a string of 201 characters", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0]["data"].update(names=["n" * 201])))
    fails("a list with a line break in a string", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0]["data"].update(names=["a", "b\nc"])))
    fails("a list with a control character in a string", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0]["data"].update(names=["a\x1bb"])))
    fails("a list with a number in it", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0]["data"].update(names=["a", 5])))
    fails("a list with a list in it", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0]["data"].update(names=[["a"]])))
    fails("a data value that is an object", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0]["data"].update(names={"a": "b"})))
    fails("a data value that is null", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0]["data"].update(names=None)))
    fails("a data value that is a long string", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0]["data"].update(out="x" * 500)))
    case("a data value that is a string of 200 characters", 0, ["**PASS** " + V_FIELDS], ["FAIL"], mutate=lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0]["data"].update(out="x" * 200)))
    fails("a data value that is a string of 201 characters", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0]["data"].update(out="x" * 201)))
    fails("a data value with a line break in it (raw tool output)", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0]["data"].update(out="a\nb")))
    fails("a data.unknown that is not true or false", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0]["data"].update(unknown="yes")))
    case("an action that is empty or `none`, and numbers and booleans in data, are fine", 0, ["**PASS** " + V_FIELDS], ["FAIL"],
         mutate=lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0].update(action="", data={"unknown": False, "n": 3, "f": 1.5})))

    # ---- the text ----
    def text(s):
        return lambda d: jdata(d, "step-reboot_first-dirty", lambda x: x.update(text=x["text"] + s))

    fails("a text longer than 8 KiB", V_TEXT, lambda d: jdata(d, "ready-base", lambda x: x.update(text=x["text"] + "word " * 2000)))
    case("a text of exactly 8 KiB is within the limit", 0, ["**PASS** " + V_TEXT], ["FAIL"],
         mutate=lambda d: jdata(d, "ready-base", lambda x: x.update(text=x["text"] + "a" * (8192 - len(x["text"].encode()) - 1) + "\n")))
    fails("a text with the box's hostname", V_TEXT, text("host " + HOST + "\n"))
    fails("a text with the box's hostname at the end of a sentence", V_TEXT, text("It is " + HOST + ".\n"))
    fails("a text with the box's address", V_TEXT, text("address " + IPV4 + "\n"))
    fails("a text with another address of the box (Docker's bridge)", V_TEXT, text("bridge 172.17.0.1\n"))
    fails("a text with the box's IPv6 address", V_TEXT, text("v6 " + IPV6 + "\n"))
    fails("a text with the box's MAC", V_TEXT, text("mac " + MAC + "\n"))
    fails("a text with a MAC that is not the box's", V_TEXT, text("mac 52:54:00:aa:bb:cc\n"))
    fails("a text with an address that is not the box's", V_TEXT, text("gateway 192.168.1.1\n"))
    fails("a text with the login user", V_TEXT, text("logged in as " + USER + "\n"))
    fails("a text with the user's name in a path", V_TEXT, text("see /home/" + USER + "/x\n"))
    fails("a text with a URL with a password", V_TEXT, text("repo https://admin:secret@repo.example.com/x\n"))
    fails("a text with a URL with a user name only", V_TEXT, text("repo https://token@repo.example.com/x\n"))
    fails("a text with a URL with a query", V_TEXT, text("repo https://repo.example.com/x?key=abc\n"))
    fails("a text that is not ASCII", V_TEXT, text("café\n"))
    fails("a text with a tab in it", V_TEXT, text("a\tb\n"))
    fails("a text with a terminal escape in it", V_TEXT, text("\x1b[31mred\x1b[0m\n"))
    fails("a text with markup in it", V_TEXT, text("<b>bold</b>\n"))
    case("a text with versions and the word casaos is not a leak", 0, ["**PASS** " + V_TEXT], ["FAIL"],
         mutate=text("docker-ce 5:28.0.4-1~debian.11~bullseye to 5:29.8.0-1, /var/lib/casaos, casaos-gateway, https://download.docker.com/linux/debian, kernel 5.10.0-46\n"))
    case("a URL reduced to scheme and host is fine", 0, ["**PASS** " + V_TEXT], ["FAIL"], mutate=text("source https://download.docker.com\n"))
    invalid("no needles to look for in the text: the leak check proves nothing", "leak-needles.tsv has no hostname", lambda d: put(d, "leak-needles.tsv", "mac\t" + MAC + "\n"))
    invalid("no needles file at all", "leak-needles.tsv has no hostname", lambda d: remove(d, "leak-needles.tsv"))

    # ---- the time and the budget ----
    fails("a checked_at an hour off the call", V_TIME, lambda d: jdata(d, "ready-inv-1", lambda x: x.update(checked_at=iso(T0 - 3600))))
    fails("a checked_at in the future", V_TIME, lambda d: jdata(d, "ready-inv-1", lambda x: x.update(checked_at=iso(T0 + 100000))))
    fails("a checked_at ten seconds after the call", V_TIME, lambda d: jdata(d, "ready-inv-1", lambda x: x.update(checked_at=iso(float(slurp(d, "ready-inv-1.t1")) + 10))))
    fails("a checked_at ten seconds before the call", V_TIME, lambda d: jdata(d, "ready-inv-1", lambda x: x.update(checked_at=iso(float(slurp(d, "ready-inv-1.t0")) - 10))))
    case("a checked_at two seconds off the call is the clock of two machines", 0, ["**PASS** " + V_TIME], ["FAIL"],
         mutate=lambda d: (jdata(d, "ready-inv-1", lambda x: x.update(checked_at=iso(float(slurp(d, "ready-inv-1.t1")) + 2))), jdata(d, "ready-inv-2", lambda x: x.update(checked_at=iso(float(slurp(d, "ready-inv-2.t0")) - 2)))))
    case("a checked_at with fractions of a second and an offset is read", 0, ["**PASS** " + V_TIME], ["FAIL"],
         mutate=lambda d: [jdata(d, "ready-inv-1", lambda x: x.update(checked_at=datetime.fromtimestamp(float(slurp(d, "ready-inv-1.t0")) + 0.5, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456+00:00")))])
    fails("a call that took 30 s and answered", V_BUDGET, lambda d: api(d, "step-held-dirty", t0=float(slurp(d, "step-held-dirty.t0")), secs=30.0, body=json.loads(slurp(d, "step-held-dirty.json"))))
    case("a call that took 24 s is inside the allowance", 0, ["**PASS** " + V_BUDGET], ["FAIL"],
         mutate=lambda d: api(d, "step-held-dirty", t0=float(slurp(d, "step-held-dirty.t0")), secs=24.0, body=json.loads(slurp(d, "step-held-dirty.json"))))

    # ---- what the mutation checks of the report found to be left alone by the cases above ----
    for what_, edit_ in (("from.id", lambda x: x["hop"]["from"].update(id="ubuntu")), ("from.version_id", lambda x: x["hop"]["from"].update(version_id="10")),
                         ("from.codename", lambda x: x["hop"]["from"].update(codename="buster")), ("to.id", lambda x: x["hop"]["to"].update(id="ubuntu")),
                         ("to.version_id", lambda x: x["hop"]["to"].update(version_id="13")), ("to.codename", lambda x: x["hop"]["to"].update(codename="trixie"))):
        fails("a hop that is wrong in %s only" % what_, V_HOP, lambda d, e=edit_: jdata(d, "ready-inv-1", e), extra=["hop.%s is" % what_])
    fails("a call that was cut off with a body that reads", V_ROUTE, lambda d: api(d, "step-held-clean", curl=56, body=json.loads(slurp(d, "step-held-clean.json"))), extra=["cut off (curl exit 56)"])
    fails("a text with the box's hostname in capitals", V_TEXT, lambda d: jdata(d, "step-reboot_first-dirty", lambda x: x.update(text=x["text"] + HOST.upper() + "\n")))
    case("a text with a word that ends in the user's name is not a leak", 0, ["**PASS** " + V_TEXT], ["FAIL"],
         mutate=lambda d: jdata(d, "step-reboot_first-dirty", lambda x: x.update(text=x["text"] + "mycasa casaos casa-gateway\n")))
    invalid("invariance: a row of the manifest that has too few columns", "inv-after.tsv has a row that is not a manifest row", lambda d: ledit(d, "inv-after.tsv", lambda t: t + '"/etc/x"\td\t0\n'), silent=[V_INV_FILES])
    invalid("invariance: a row of the manifest that has too many columns", "inv-after.tsv has a row that is not a manifest row", lambda d: ledit(d, "inv-after.tsv", lambda t: t + '"/etc/x"\td\t0\t0\t0\t0\t0:0\t-\textra\n'), silent=[V_INV_FILES])
    case("a checked_at in another time zone, at the time of the call, is the time of the call", 0, ["**PASS** " + V_TIME], ["FAIL"],
         mutate=lambda d: jdata(d, "ready-inv-1", lambda x: x.update(checked_at=datetime.fromtimestamp(float(slurp(d, "ready-inv-1.t0")) + 0.5, timezone(timedelta(hours=2))).strftime("%Y-%m-%dT%H:%M:%S+02:00"))))
    fails("a detail that is longer than 2000", V_FIELDS, lambda d: jdata(d, "ready-inv-1", lambda x: x["checks"][0].update(detail="x" * 2500)))
    fails("a count that is a boolean", V_SHAPE, lambda d: jdata(d, "ready-inv-1", lambda x: x["counts"].update(info=True)))
    fails("counts with a key that is not a severity and the four of them", V_SHAPE, lambda d: jdata(d, "ready-inv-1", lambda x: x["counts"].update(extra=1)))

    # ---- GET /v1/sys/os ----
    fails("GET /v1/sys/os that says another system", V_SYSOS, lambda d: jdata(d, "sysos-inv", lambda x: x.update(codename="buster")))
    fails("GET /v1/sys/os whose next is another release", V_SYSOS, lambda d: jdata(d, "sysos-inv", lambda x: x["next"].update(version_id="13", codename="trixie")))
    fails("GET /v1/sys/os with no next", V_SYSOS, lambda d: jdata(d, "sysos-inv", lambda x: x.pop("next")))
    fails("GET /v1/sys/os that is not an object", V_SYSOS, lambda d: api(d, "sysos-inv", raw="[]"))
    invalid("GET /v1/sys/os that left no record", "no answer was recorded for sysos-inv", lambda d: remove(d, "sysos-inv.code"), silent=[V_SYSOS])

    # ---- invariance: the files ----
    def inv(tag, fn):
        return lambda d: ledit(d, "inv-%s.tsv" % tag, fn)

    def row_edit(path, **kw):
        cols = ("type", "size", "mtime", "ctime", "mode", "owner", "digest")

        def fn(t):
            out = []
            for line in t.splitlines():
                f = line.split("\t")
                if json.loads(f[0]) == path:
                    for k, v in kw.items():
                        f[1 + cols.index(k)] = v
                out.append("\t".join(f))
            return "\n".join(out) + "\n"
        return fn

    for path_, what, kw in (("/etc/fstab", "its content changed with the same size and the same times", dict(digest="9" * 64)),
                            ("/etc/os-release", "its size changed", dict(size="261")),
                            ("/var/lib/dpkg/status", "its mtime changed", dict(mtime="1790000999000000000")),
                            ("/var/lib/apt/lists/deb.debian.org_Packages", "its ctime changed (a chmod, a rename over it)", dict(ctime="1790000999000000000")),
                            ("/var/cache/apt/pkgcache.bin", "its mode changed", dict(mode="600")),
                            ("/var/lib/casaos/db/casaos.db", "its owner changed", dict(owner="1000:1000")),
                            ("/var/lib/dpkg", "its directory time changed (something was created or removed in it)", dict(mtime="1790000999000000000")),
                            ("/etc/casaos/gateway.ini", "it became a link", dict(type="l", digest="->/tmp/x"))):
        fails("invariance: %s: %s" % (path_, what), V_INV_FILES, inv("after", row_edit(path_, **kw)), extra=[path_])
    fails("invariance: a file was created in /etc", V_INV_FILES, inv("after", lambda t: t + manifest_text({"/etc/cron.d/new": ("f", "5", "1790000999000000000", "1790000999000000000", "644", "0:0", "7" * 64)})),
          extra=["created /etc/cron.d/new"])
    fails("invariance: a file was created in /var/lib/dpkg/updates", V_INV_FILES,
          inv("after", lambda t: t + manifest_text({"/var/lib/dpkg/updates/0001": ("f", "0", "1790000999000000000", "1790000999000000000", "644", "0:0", "8" * 64)})), extra=["created /var/lib/dpkg/updates/0001"])
    fails("invariance: a file was removed from /var/lib/apt", V_INV_FILES,
          inv("after", lambda t: "".join(ln + "\n" for ln in t.splitlines() if "deb.debian.org_Packages" not in ln)), extra=["removed /var/lib/apt/lists/deb.debian.org_Packages"])
    fails("invariance: a file was created in /var/lib/casaos (nothing there either in this rung)", V_INV_FILES,
          inv("after", lambda t: t + manifest_text({"/var/lib/casaos/os-upgrade/run.json": ("f", "5", "1790000999000000000", "1790000999000000000", "600", "0:0", "7" * 64)})))
    case("invariance: /var/log/casaos may change (and the healthy leg's changes there are the proof)", 0, ["**PASS** " + V_INV_FILES], ["FAIL"],
         mutate=inv("after", lambda t: t + manifest_text({"/var/log/casaos/more.log": ("f", "5", "1790000999000000000", "1790000999000000000", "600", "0:0", "7" * 64)})))
    case("invariance: a path that is under /var/log/casaosX is not /var/log/casaos", 1, ["**FAIL** " + V_INV_FILES, "created /var/log/casaosX/y"], ["INVALID"],
         mutate=inv("after", lambda t: t + manifest_text({"/var/log/casaosX/y": ("f", "5", "1790000999000000000", "1790000999000000000", "600", "0:0", "7" * 64)})))

    def noisy(d):
        # a file that changes by itself: between the control's two snapshots and between the window's
        ledit(d, "inv-ctl-b.tsv", row_edit("/var/lib/casaos/db/casaos.db", mtime="1790000555000000000", digest="a1" * 32))
        ledit(d, "inv-after.tsv", row_edit("/var/lib/casaos/db/casaos.db", mtime="1790000666000000000", digest="a2" * 32))

    case("invariance: a file that changed by itself in the control is left out of the verdict, and named", 0, ["**PASS** " + V_INV_FILES, "- files: /var/lib/casaos/db/casaos.db"], ["FAIL"], mutate=noisy)
    case("invariance: the control that is quiet leaves the same change a FAIL", 1, ["**FAIL** " + V_INV_FILES], [], mutate=lambda d: ledit(d, "inv-after.tsv", row_edit("/var/lib/casaos/db/casaos.db", digest="a2" * 32)))
    invalid("invariance: a snapshot that is missing", "inv-before.tsv is missing or empty", lambda d: remove(d, "inv-before.tsv"), silent=[V_INV_FILES])
    invalid("invariance: a snapshot that is empty", "inv-after.tsv is missing or empty", lambda d: put(d, "inv-after.tsv", ""), silent=[V_INV_FILES])
    invalid("invariance: the control's snapshot is missing", "inv-ctl-a.tsv is missing or empty", lambda d: remove(d, "inv-ctl-a.tsv"))
    invalid("invariance: a row that is not a manifest row", "inv-before.tsv has a row that is not a manifest row", lambda d: ledit(d, "inv-before.tsv", lambda t: t + "garbage\n"), silent=[V_INV_FILES])
    invalid("invariance: a row whose path is not JSON", "inv-after.tsv has a row that is not a manifest row", lambda d: ledit(d, "inv-after.tsv", lambda t: t + "/etc/x\ta\tb\tc\td\te\tf\tg\n"))
    for tree_ in ("/etc", "/var/lib/dpkg", "/var/lib/apt", "/var/cache/apt", "/var/lib/casaos", "/var/log/casaos"):
        invalid("invariance: a snapshot with no row for %s: the tree was not looked at" % tree_, "has no row for %s" % tree_,
                inv("before", lambda t, p=tree_: "".join(ln + "\n" for ln in t.splitlines() if json.loads(ln.split("\t")[0]) != p)), silent=[V_INV_FILES])

    # ---- invariance: the units and the ports ----
    def unit(tag, name, **kw):
        cols = ("active", "sub", "pid", "inv")

        def fn(t):
            out = []
            for line in t.splitlines():
                f = line.split("\t")
                if f[0] == name:
                    for k, v in kw.items():
                        f[1 + cols.index(k)] = v
                out.append("\t".join(f))
            return "\n".join(out) + "\n"
        return lambda d: ledit(d, "units-%s.tsv" % tag, fn)

    fails("invariance: a service was started again (its invocation id changed)", V_INV_UNITS, unit("after", "casaos-gateway.service", inv="inv-new"), extra=["casaos-gateway.service"])
    fails("invariance: a service has another main pid", V_INV_UNITS, unit("after", "ssh.service", pid="9999"), extra=["ssh.service"])
    fails("invariance: a service was stopped", V_INV_UNITS, unit("after", "docker.service", active="inactive", sub="dead"), extra=["docker.service"])
    fails("invariance: a service that was not there appeared", V_INV_UNITS, lambda d: ledit(d, "units-after.tsv", lambda t: t + "apt-daily.service\tactive\trunning\t77\tnew\n"), extra=["apt-daily.service appeared"])
    fails("invariance: a service disappeared", V_INV_UNITS, lambda d: ledit(d, "units-after.tsv", lambda t: "".join(ln + "\n" for ln in t.splitlines() if not ln.startswith("ssh.service"))), extra=["ssh.service disappeared"])
    case("invariance: a unit that a timer starts may run by itself in the window", 0, ["**PASS** " + V_INV_UNITS], ["FAIL"], mutate=unit("after", "logrotate.service", inv="inv-ran", active="active", sub="running", pid="31"))
    case("invariance: a unit that a timer does NOT start is not excused by the others", 1, ["**FAIL** " + V_INV_UNITS], [],
         mutate=lambda d: (put(d, "timer-units.txt", ""), unit("after", "logrotate.service", inv="inv-ran")(d)))
    case("invariance: a unit that changed by itself in the control is left out, and named", 0, ["**PASS** " + V_INV_UNITS, "- units: casaos.service"], ["FAIL"],
         mutate=lambda d: (unit("ctl-b", "casaos.service", inv="inv-x")(d), unit("after", "casaos.service", inv="inv-y")(d)))
    invalid("invariance: the list of the units is missing", "units-before.tsv is missing or empty", lambda d: remove(d, "units-before.tsv"), silent=[V_INV_UNITS])
    invalid("invariance: a row of the units that is not one", "units-after.tsv has a row that is not a unit row", lambda d: ledit(d, "units-after.tsv", lambda t: t + "x\ty\n"))
    invalid("invariance: the timers' units are not known", "timer-units.txt is missing", lambda d: remove(d, "timer-units.txt"))
    fails("invariance: a port that was not open is", V_INV_PORTS, lambda d: ledit(d, "listen-after.txt", lambda t: t + "tcp 0.0.0.0:8080\n"), extra=["started listening tcp 0.0.0.0:8080"])
    fails("invariance: a port that was open is not", V_INV_PORTS, lambda d: ledit(d, "listen-after.txt", lambda t: t.replace("udp 0.0.0.0:68\n", "")), extra=["stopped listening udp 0.0.0.0:68"])
    case("invariance: a port that changed by itself in the control is left out, and named", 0, ["**PASS** " + V_INV_PORTS, "- listening sockets: tcp 0.0.0.0:9999"], ["FAIL"],
         mutate=lambda d: (ledit(d, "listen-ctl-b.txt", lambda t: t + "tcp 0.0.0.0:9999\n"), ledit(d, "listen-after.txt", lambda t: t + "tcp 0.0.0.0:9999\n")))
    invalid("invariance: no listening port recorded", "listen-before.txt is missing or empty", lambda d: put(d, "listen-before.txt", ""), silent=[V_INV_PORTS])

    case("invariance: the verdict says which trees it looked at, and that /tmp and /run are not among them", 0,
         ["**PASS** " + V_INV_FILES + ": %d paths looked at (these six trees only, not /tmp, /run or the rest of the machine)" % len(TREE_ROWS), "## What the invariance window looks at", "/etc, /var/lib/dpkg, /var/lib/apt, /var/cache/apt, /var/lib/casaos, /var/log/casaos",
          "It does not look at /tmp, /run, /var/tmp, /home, /root, /proc or /sys"], ["FAIL"])
    # ---- the call under dpkg's lock ----
    fails("lock: the call that waited for the lock and gave up", V_LOCK_FAST, lambda d: api(d, "ready-inv-lock", code=0, curl=28, raw="", secs=45.0), extra=["curl gave up"])
    fails("lock: the call that took 40 s and answered", V_LOCK_FAST, lambda d: api(d, "ready-inv-lock", t0=float(slurp(d, "ready-inv-lock.t0")), secs=40.0, body=json.loads(slurp(d, "ready-inv-lock.json"))))
    fails("lock: the call that answered 500", V_LOCK_FAST, lambda d: api(d, "ready-inv-lock", code=500, body={"success": 500}))
    fails("lock: busy is ok while another process holds the lock", V_LOCK_BUSY, lambda d: set_sev(d, "ready-inv-lock", "busy", "ok"))
    fails("lock: busy is only a warning while another process holds the lock", V_LOCK_BUSY, lambda d: set_sev(d, "ready-inv-lock", "busy", "warn"))
    fails("lock: no busy in the answer at all", V_LOCK_BUSY, lambda d: drop_check(d, "ready-inv-lock", "busy"))
    fails("lock: the lock was free after the call (the call took it, or the holder lost it)", V_LOCK_KEPT, lambda d: put(d, "lock-probe-after", "free\n"))
    fails("lock: the lock was not looked at after the call", V_LOCK_KEPT, lambda d: remove(d, "lock-probe-after"))
    invalid("lock: the guest could not hold the lock: the call under it proves nothing", "could not hold dpkg's lock", lambda d: put(d, "lock-probe-during", "free\n"), silent=[V_LOCK_FAST, V_LOCK_BUSY, V_LOCK_KEPT])
    invalid("lock: the lock file is missing", "could not hold dpkg's lock", lambda d: put(d, "lock-probe-during", "missing\n"))

    # ---- the clean box ----
    def clean_all(fn):
        return lambda d: [fn(d, n) for n in ("ready-inv-1", "ready-inv-2", "ready-inv-3", "ready-base")]

    fails("clean: a refuse on the clean box (in all four answers)", V_CLEAN_NO_REFUSE, clean_all(lambda d, n: set_sev(d, n, "bootloader", "refuse")), extra=["refuse: bootloader"])
    fails("clean: a refuse in one answer of the clean box", V_CLEAN_NO_REFUSE, lambda d: set_sev(d, "ready-inv-2", "reboot_first", "refuse"), extra=["reboot_first"])
    fails("clean: a refuse that says it could not find out (a tool is missing) is a refuse", V_CLEAN_NO_REFUSE,
          lambda d: [set_sev(d, "ready-base", "smart_failed", "refuse"), jdata(d, "ready-base", lambda x: [c["data"].update(unknown=True) for c in x["checks"] if c["code"] == "smart_failed"])])
    fails("clean: a state that is not ready or attention", V_CLEAN_STATE, lambda d: jdata(d, "ready-inv-1", lambda x: x.update(state="unsupported")))
    fails("clean: the four answers do not agree (a warning that comes and goes)", V_CLEAN_AGREE, lambda d: drop_check(d, "ready-inv-3", "backup_age"), extra=["backup_age"])
    fails("clean: the four answers do not agree (a severity that changed)", V_CLEAN_AGREE, lambda d: set_sev(d, "ready-base", "docker_update_pending", "info"), extra=["docker_update_pending"])
    fails("clean: the four answers do not agree (a warning that is an info in one of them)", V_CLEAN_AGREE, lambda d: set_sev(d, "ready-inv-2", "backup_age", "info"), extra=["backup_age"])
    fails("clean: Docker's source is not named in the third-party warning", V_CLEAN_DOCKER,
          lambda d: [jdata(d, n, lambda x: [c.update(title="Third-party sources", detail="one file", data={"unknown": False, "files": ["x.list"]}) for c in x["checks"] if c["code"] == "sources_third_party"]) for n in ("ready-base",)])
    fails("clean: the third-party source is only an info", V_CLEAN_DOCKER, lambda d: set_sev(d, "ready-base", "sources_third_party", "info"))
    fails("clean: no third-party check at all", V_CLEAN_DOCKER, lambda d: drop_check(d, "ready-base", "sources_third_party"))
    fails("clean: no reminder to take a snapshot", V_CLEAN_SNAPSHOT, lambda d: drop_check(d, "ready-base", "snapshot_reminder"))
    fails("clean: the reminder to take a snapshot is ok (it must always be said)", V_CLEAN_SNAPSHOT, lambda d: set_sev(d, "ready-base", "snapshot_reminder", "ok"))
    case("clean: a box with warnings is attention, and is clean", 0, ["**PASS** " + V_CLEAN_STATE, "State **attention**"], ["FAIL"])

    # ---- the dirty matrix, one step at a time ----
    for leg_ in ("d11", "u22"):
        for s in steps_of(leg_):
            sid, code_, sev_ = s["id"], s["code"], s["sev"]
            if leg_ == "u22" and s["id"] not in ("held", "sources_alias", "sources_third_party", "foreign_arch_empty", "containers_policy", "busy_lock", "disk_boot", "docker_down"):
                continue
            tag = "(%s)" % leg_
            D, C = "step-%s-dirty" % sid, "step-%s-clean" % sid
            if s["mode"] == "quiet":
                fails("step %s: the code fires on a dirt that should leave it quiet %s" % (sid, tag), f_quiet(s), lambda d, D=D, c=code_: make(d, D, c, "refuse"), leg=leg_)
                case("step %s: the quiet code that stays quiet passes %s" % (sid, tag), 0, ["**PASS** " + f_quiet(s)], ["FAIL"], leg=leg_)
            else:
                fails("step %s: the code does not fire %s" % (sid, tag), f_fires(s), lambda d, D=D, c=code_: drop_check(d, D, c), leg=leg_, extra=["not in the answer"])
                other = {"refuse": "warn", "warn": "refuse", "info": "warn"}[sev_]
                fails("step %s: the code fires with another severity (%s instead of %s) %s" % (sid, other, sev_, tag), f_fires(s), lambda d, D=D, c=code_, o=other: set_sev(d, D, c, o), leg=leg_)
                case("step %s: the code fires with the exact severity %s" % (sid, tag), 0, ["**PASS** " + f_fires(s)], ["FAIL"], leg=leg_)
            if s["mode"] == "names" or s["needle"]:
                # the name is in the data (the file, the container), in the title or in the detail: it is taken out of all of them
                fails("step %s: the code fires and does not name %s %s" % (sid, s["needle"], tag), f_fires(s),
                      lambda d, D=D, c=code_, n=s["needle"]: jdata(d, D, lambda x: [cc.update(json.loads(json.dumps(cc).replace(n, "x"))) for cc in x["checks"] if cc["code"] == c]), leg=leg_)
            for fcode in s["forbid"]:
                fails("step %s: %s fires where it must not %s" % (sid, fcode, tag), f_forbid(s, fcode), lambda d, D=D, c=fcode: make(d, D, c, "refuse"), leg=leg_)
            fails("step %s: a check nobody expected fires with it %s" % (sid, tag), f_alone(s), lambda d, D=D: make(d, D, "bootloader", "refuse"), leg=leg_, extra=["also fires: bootloader"])
            fails("step %s: a warning nobody expected fires with it %s" % (sid, tag), f_alone(s), lambda d, D=D: make(d, D, "ram", "warn"), leg=leg_, extra=["also fires: ram"])
            for extra_ in s["extras"]:
                ex = extra_.replace("*", "x")
                case("step %s: %s may fire with it, the dirt does that too %s" % (sid, ex, tag), 0, ["**PASS** " + f_alone(s)], ["FAIL"], leg=leg_, mutate=lambda d, D=D, c=ex: make(d, D, c, "refuse"))
            case("step %s: what fired before the dirt is not held against it %s" % (sid, tag), 0, ["**PASS** " + f_alone(s)], ["FAIL"], leg=leg_)
            if s["mode"] != "quiet":
                fails("step %s: the code is still there after the undo %s" % (sid, tag), f_undone(s), lambda d, C=C, c=code_, v=sev_: make(d, C, c, v) if s["mode"] != "names" else
                      jdata(d, C, lambda x: [cc.update(detail=cc["detail"] + " " + s["needle"]) for cc in x["checks"] if cc["code"] == c]), leg=leg_)
            fails("step %s: something else is left after the undo %s" % (sid, tag), f_undone(s), lambda d, C=C: make(d, C, "ram", "warn"), leg=leg_)
            fails("step %s: the state after the undo is not the clean one %s" % (sid, tag), f_undone(s), lambda d, C=C: jdata(d, C, lambda x: x.update(state="blocked")), leg=leg_, extra=[])
            invalid("step %s: the box could not be dirtied %s" % (sid, tag), "step %s: the box could not be dirtied (exit 1)" % sid, lambda d, sid=sid: put(d, "step-%s.dirty-rc" % sid, "1\n"), leg=leg_)
            invalid("step %s: the undo failed %s" % (sid, tag), "step %s: the undo failed (exit 1, box back: yes)" % sid, lambda d, sid=sid: put(d, "step-%s.undo-rc" % sid, "1\n"), leg=leg_, silent=[f_undone(s)])
            invalid("step %s: the undo did not put the box back %s" % (sid, tag), "step %s: the undo failed (exit 0, box back: no)" % sid, lambda d, sid=sid: put(d, "step-%s.undone" % sid, "no\n"), leg=leg_, silent=[f_undone(s)])
            invalid("step %s: the step did not run to its end %s" % (sid, tag), "step %s: it did not run to its end" % sid, lambda d, sid=sid: remove(d, "step-%s.undone" % sid), leg=leg_)
            invalid("step %s: the dirty call left no answer %s" % (sid, tag), "no answer was recorded for step-%s-dirty" % sid, lambda d, D=D: remove(d, D + ".code"), leg=leg_, silent=[f_fires(s)])
            invalid("step %s: the call after the undo left no answer %s" % (sid, tag), "no answer was recorded for step-%s-clean" % sid, lambda d, C=C: remove(d, C + ".code"), leg=leg_, silent=[f_undone(s)])
            invalid("step %s: skipped on a system where it exists %s" % (sid, tag), "step %s: the guest skipped it" % sid, lambda d, sid=sid: put(d, "step-%s.skipped" % sid, "no reason\n"), leg=leg_)
    fails("step held: a warning of the clean box that is a refuse with the dirt is a check that fires with it", f_alone(STEP_BY_ID["held"]), lambda d: make(d, "step-held-dirty", "sources_third_party", "refuse"),
          extra=["also fires: sources_third_party"])
    fails("step held: a warning of the clean box that is a refuse with the dirt (`could not find out`) is one too", f_alone(STEP_BY_ID["held"]),
          lambda d: [make(d, "step-held-dirty", "docker_update_pending", "refuse"), jdata(d, "step-held-dirty", lambda x: [c["data"].update(unknown=True) for c in x["checks"] if c["code"] == "docker_update_pending"])],
          extra=["also fires: docker_update_pending"])
    case("step held_docker: a warning of the clean box that becomes an info with the dirt is not held against it", 0, ["**PASS** " + f_alone(STEP_BY_ID["held_docker"])], ["FAIL"],
         mutate=lambda d: set_sev(d, "step-held_docker-dirty", "backup_age", "info"))
    fails("step held: after the undo a warning is an info (the same codes, another severity)", f_undone(STEP_BY_ID["held"]), lambda d: set_sev(d, "step-held-clean", "backup_age", "info"))
    fails("step held: after the undo a warning is a refuse (the same codes, another severity)", f_undone(STEP_BY_ID["held"]), lambda d: set_sev(d, "step-held-clean", "backup_age", "refuse"))
    fails("step fstab_unresolved: fstab_network fires with it (no tool is asked, nothing else is allowed to)", f_alone(STEP_BY_ID["fstab_unresolved"]), lambda d: make(d, "step-fstab_unresolved-dirty", "fstab_network", "warn"),
          extra=["also fires: fstab_network"])
    fails("step apps: backup_age that becomes a refuse with it is one that fires", f_alone(STEP_BY_ID["apps"]), lambda d: make(d, "step-apps-dirty", "backup_age", "refuse"), extra=["also fires: backup_age"])
    fails("step docker_down: containers_policy fires with it (the daemon is not asked when it does not run)", f_alone(STEP_BY_ID["docker_down"]), lambda d: make(d, "step-docker_down-dirty", "containers_policy", "refuse"),
          extra=["also fires: containers_policy"])
    check("the report knows which steps may bring a check with them: only the disk steps, whose dirt is on the filesystem of /", {s["id"]: tuple(s["extras"]) for s in STEPS if s["extras"]} == {"disk_boot": ("disk_root", "disk_archives")},
          {s["id"]: s["extras"] for s in STEPS if s["extras"]})
    # a step that is untestable because the clean box already fires its code is not a pass and not a fail
    case("step reboot_first: a clean box that already has the code cannot tell it from the dirt: NOT TESTABLE, and the clean box is a FAIL of its own", 1,
         ["**NOT TESTABLE** " + f_fires(STEP_BY_ID["reboot_first"]), "**FAIL** " + V_CLEAN_NO_REFUSE], ["INVALID"],
         mutate=lambda d: [set_sev(d, n, "reboot_first", "refuse") for n in all_calls(d) if n.startswith("ready-") or n in ("step-reboot_first-dirty", "step-reboot_first-clean")])
    case("step sources_third_party: the clean answer that already names the needle cannot tell it from the dirt (INVALID)", 1, ["INVALID RUN", "already names osr-third-party"], [],
         mutate=lambda d: jdata(d, "ready-base", lambda x: [c.update(detail=c["detail"] + " osr-third-party.list") for c in x["checks"] if c["code"] == "sources_third_party"]))
    case("a step whose undo failed taints the ones after it: they are not judged, never a FAIL of the feature", 1,
         ["INVALID RUN", "step held: the undo failed", "not judged: the undo of step held failed"], ["**FAIL** step", "**PASS** step broken_dpkg"],
         mutate=lambda d: [put(d, "step-held.undo-rc", "1\n"), make(d, "step-held-clean", "held", "refuse"), make(d, "step-broken_dpkg-dirty", "held", "refuse"), make(d, "step-broken_dpkg-clean", "held", "refuse")])
    invalid("step sources_alias: skipped on Debian, where the alias exists", "step sources_alias: the guest skipped it", lambda d: put(d, "step-sources_alias.skipped", "x\n"))
    invalid("step sources_alias: skipped on Ubuntu, where the alias exists too (devel)", "step sources_alias: the guest skipped it", lambda d: put(d, "step-sources_alias.skipped", "x\n"), leg="u22")
    case("step held: whether the answer names libc6 is a fact in the table", 0, ["(names libc6: yes)"], ["FAIL"])
    case("step held: an answer that does not name libc6 passes, and the table says so", 0, ["(names libc6: no)", "**PASS** " + f_fires(STEP_BY_ID["held"])], ["FAIL"],
         mutate=lambda d: jdata(d, "step-held-dirty", lambda x: [c.update(title="Packages are on hold", detail="some packages", data={"unknown": False, "count": 1}) for c in x["checks"] if c["code"] == "held"]))

    # ---- what the run says of itself ----
    invalid("a guest that stopped early says why", "the guest stopped early: the installer failed", lambda d: put(d, "aborted", "the installer failed\n"))
    invalid("a guest that did not reach its end", "the guest did not reach the end of its run", lambda d: remove(d, "finished"))
    invalid("a run with no timeline", "timeline.tsv is missing or empty", lambda d: remove(d, "timeline.tsv"))
    invalid("a guest that is not the system the leg needs", "the guest is ubuntu 22.04, the d11 leg needs debian 11", lambda d: put(d, "os-release", 'ID=ubuntu\nVERSION_ID="22.04"\nVERSION_CODENAME=jammy\n'))
    invalid("a guest that says it ran another leg", "the guest says it ran leg 'u22'", lambda d: put(d, "leg", "u22\n"))
    invalid("no os-release", "os-release is missing or empty", lambda d: remove(d, "os-release"))
    case("an empty directory is an invalid run and a pile of reasons, never a pass or a crash", 1, ["INVALID RUN"], ["Traceback", "PASS"], empty=True)
    case("a feature that is wrong and a run that stopped says FAIL and INVALID", 1, ["FAIL and INVALID RUN", "**FAIL** " + V_CLEAN_NO_REFUSE], [],
         mutate=lambda d: (set_sev(d, "ready-base", "bootloader", "refuse"), put(d, "aborted", "something\n")))

    # ---- the report is the same whatever the line ends of the guest's files ----
    case("files with Windows line ends are read the same", 0, ["PASS"], ["FAIL", "INVALID"],
         mutate=lambda d: [put(d, n, slurp(d, n).replace("\n", "\r\n")) for n in ("os-release", "leak-needles.tsv", "timer-units.txt", "listen-after.txt", "units-after.tsv") if exists(d, n)])

    if os.environ.get("REPORT_ONLY"):
        if failures:
            print("\n".join(failures), file=sys.stderr)
            print("%d of %d cases failed" % (len(failures), count[0]), file=sys.stderr)
            sys.exit(1)
        print("ok: the report can say no (%d cases)" % count[0])
        sys.exit(0)


# ---- the guest script: its text, its steps, its runner, with stand-ins for the tools -------------------------------------------------------------------------------

def have_bash():
    try:
        return subprocess.run([BASH, "-c", "true"], capture_output=True).returncode == 0
    except OSError:
        return False


def early_quitters(script):
    """what in a shell script leaves a producer writing into a pipe nobody reads any more, the class that ended the Debian 11 leg of the Docker proof (under pipefail
    the producer's SIGPIPE is status 141 and set -e ends the run): a consumer that quits at its first match. `| head`, `| grep -q` or `-m`, `| sed ... q`, an awk that
    exits outside its END block. Comment lines are left out. [what was found]"""
    code = "\n".join(ln for ln in script.replace("\r\n", "\n").split("\n") if not ln.lstrip().startswith("#"))
    found = re.findall(r"\|\s*head\b[^|\n]*|\|\s*grep\s+-[A-Za-z]*[qm][^|\n]*|\|\s*sed\s[^|\n]*?(?<![A-Za-z])q\b[^|\n]*", code)
    found += ["awk '%s'" % prog.strip()[:60] for prog in re.findall(r"\bawk\b[^'\n]*'([^']*)'", code) if re.search(r"\bexit\b", re.sub(r"END\s*\{[^}]*\}", "", prog))]
    return found


with open(SCRIPT, newline="") as f:
    guest_text = f.read().replace("\r\n", "\n")
OLD_FORMS = ("cprev=\"$(apt-cache madison containerd.io | awk '$3 ~ /^1\\.7\\./ {print $3; exit}')\"", "apt-cache policy docker-ce | awk '/Candidate:/ {print $2; exit}'", "dpkg --version 2>&1 | head -n1",
             "pgrep -af x | head -n1", "ps | grep -q x", "ps | grep -m1 x", "seq 1 9 | sed 1q", "seq 1 9 | sed -n '1q'", "awk '$1 == 1 {print; exit}' f")
READS_ALL = ("apt-cache madison containerd.io | awk '$3 ~ /^1\\.7\\./ && !found {print $3; found = 1}'", "dpkg --version 2>&1 | sed -n 1p", "# x | head -n1\ntrue", "awk 'END { exit 1 }' f",
             "awk '{ n++ } END { if (n) exit 0; exit 1 }' f", "grep -q x f", "grep -q x <<<\"$y\"")
check("the lint of pipelines whose consumer quits early sees the forms that ended the Debian 11 leg, and not the ones that read everything",
      all(early_quitters(s) for s in OLD_FORMS) and not any(early_quitters(s) for s in READS_ALL), [s for s in OLD_FORMS if not early_quitters(s)] + [s for s in READS_ALL if early_quitters(s)])
check("no pipeline of the guest script has a consumer that quits before its producer is done (head, grep -q, sed q, an awk that exits outside END)", not early_quitters(guest_text), early_quitters(guest_text))
check("the guest script says what its python tools are, and nothing in it is a heredoc that is not closed", guest_text.count("<<'PY'") == guest_text.count("\nPY\n"), "%d %d" % (guest_text.count("<<'PY'"), guest_text.count("\nPY\n")))

guest_ids = re.search(r"^STEP_IDS=\(([^)]*)\)", guest_text, re.M | re.S)
guest_ids = guest_ids.group(1).split() if guest_ids else []
check("the guest runs the steps the report judges, in the same order", guest_ids == [s["id"] for s in STEPS], (guest_ids, [s["id"] for s in STEPS]))
check("every step has its dirt, its undo and its check that the undo held", all(("dirty_%s() " % i in guest_text and "undo_%s() " % i in guest_text and "undone_%s() " % i in guest_text) for i in guest_ids),
      [i for i in guest_ids if not all(("%s_%s() " % (p, i)) in guest_text for p in ("dirty", "undo", "undone"))])
skip_ids = re.findall(r"^skip_(\w+)\(\)", guest_text, re.M)
check("the guest skips no step: every step of the matrix exists on both systems", skip_ids == [], skip_ids)
check("the guest waits for AppManagement's own check of the images, and says why when it did not come, before the control and the window",
      re.search(r'    wait_image_check 240 \|\| not_proven "[^"]*IMAGE_UPDATES[^"]*"\n    marker prepare_done\n[^\n]*\n    sleep 20\n\n    invariance\n', guest_text) is not None, "")
check("the file the wait looks for is the one AppManagement renames into /var/lib/casaos", 'IMAGE_UPDATES="${IMAGE_UPDATES:-/var/lib/casaos/image_updates.json}"' in guest_text, "")
check("the legs of the guest, of the report and of the workflow's file are the same", True)

if have_bash() and not os.environ.get("REPORT_ONLY"):
    # fakes of the tools the guest script calls, over a box that lives in files ($FAKE); the model above is the core behind the fake curl
    FAKECORE = r'''
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone
M = {"FIXTURES_DIR": os.environ["FIXTURES"]}
exec(open(os.path.join(os.environ["FAKE"], "model.py")).read(), M)
F = os.environ["FAKE"]
path, out = "/" + sys.argv[1], sys.argv[2]
leg = os.environ["LEG"]
L = json.loads(os.environ["LEG_JSON"])[leg]


def lines(path):
    try:
        return [x for x in open(path).read().split("\n") if x.strip()]
    except OSError:
        return []


def active(u):
    return os.path.exists(os.path.join(F, "active", u))


def reply(code, body=None, rc=0):
    if body is not None and out != "/dev/null":
        open(out, "w").write(body if isinstance(body, str) else json.dumps(body))
    print(code, rc)
    sys.exit(0)


mode = os.environ.get("FAKE_CORE_MODE", "")
if path == "/":
    reply(200 if active("casaos-gateway.service") else 0, "<html></html>", 0 if active("casaos-gateway.service") else 7)
if path == "/v1/sys/version/current":
    reply(200, {"success": 200, "message": "ok", "data": "v0.5.99"})
if path == "/v2/app_management/categories":
    reply(200, {"success": 200, "data": []}) if active("casaos-app-management.service") else reply(0, "", 7)
if path == "/v1/sys/os":
    reply(200, {"success": 200, "message": "ok", "data": {"id": L["family"], "version_id": L["version"], "codename": L["codename"], "pretty_name": L["pretty"], "arch": "amd64", "known": True,
                                                         "next": {"id": L["family"], "version_id": L["to_version"], "codename": L["to_codename"], "state": "report"}}})
if path != "/v1/sys/os/readiness":
    reply(404, {"success": 404})
if mode == "route404":
    reply(404, {"message": "no"})

# which step of the matrix has dirtied the box (the fixtures are of one dirt at a time): read from the files and the state the stand-in tools keep
steps = []
holds = lines(os.path.join(F, "holds"))
if any(h not in M["DOCKER_FAMILY"] for h in holds):
    steps.append("held")
if any(h in M["DOCKER_FAMILY"] for h in holds):
    steps.append("held_docker")
upd = os.path.dirname(os.environ["DPKG_JOURNAL"])
if any(n.isdigit() for n in (os.listdir(upd) if os.path.isdir(upd) else [])):
    steps.append("broken_dpkg")
sd = os.path.join(os.environ["APT_DIR"], "sources.list.d")
FILES = {"osr-cdrom.list": "sources_cdrom", "osr-file.list": "sources_file", "osr-alias.list": "sources_alias", "osr-proposed.list": "sources_proposed", "osr-third-party.list": "sources_third_party"}
for n in sorted(os.listdir(sd) if os.path.isdir(sd) else []):
    if n in FILES and lines(os.path.join(sd, n)):
        steps.append(FILES[n])
ac = os.path.join(os.environ["APT_DIR"], "apt.conf.d")
release = None
for n in (os.listdir(ac) if os.path.isdir(ac) else []):
    m = re.search(r'Default-Release\s+"([^"]*)"', open(os.path.join(ac, n)).read())
    release = m.group(1) if m else release
if release is not None:
    steps.append("sources_pin")
pr = os.path.join(os.environ["APT_DIR"], "preferences.d")
if any("Pin:" in open(os.path.join(pr, n)).read() for n in (os.listdir(pr) if os.path.isdir(pr) else [])):
    steps.append("sources_pin_prefs")
if [l for l in lines(os.path.join(F, "pkgs")) if not l.endswith(":amd64")]:
    steps.append("foreign_arch")
elif lines(os.path.join(F, "archs")):
    steps.append("foreign_arch_empty")
if os.path.exists(os.environ["REBOOT_REQUIRED"]):
    steps.append("reboot_first")
if os.environ.get("HAVE_FCNTL") == "1":
    lock = subprocess.run([sys.executable, os.path.join(os.environ["LIBDIR"], "lockprobe.py"), os.environ["DPKG_LOCK"]], capture_output=True, text=True).stdout.strip() == "held"
else:
    lock = os.path.exists(os.path.join(F, "lock.held"))
if lock:
    steps.append("busy_lock")
if active("casaos-package-update.service"):
    steps.append("busy_unit")
if any(l.startswith("UUID=") and "nofail" not in l and "root-uuid" not in l for l in lines(os.environ["FSTAB"])):
    steps.append("fstab_unresolved")
names = [l.split("|")[0] for l in lines(os.path.join(F, "containers"))]
for name, step in (("p-no", "containers_policy"), ("p-hostnet", "containers_ports_host"), ("p-pub53", "containers_ports_publish")):
    if name in names:
        steps.append(step)
fill = os.path.join(os.environ["BOOT_DIR"], "osr-fill")
if os.path.exists(fill) and int(open(fill).read().strip() or 0) > 0:
    steps.append("disk_boot")
if not active("casaos-app-management.service"):
    steps.append("apps")
if not active("docker.service"):
    steps.append("docker_down")
if len(steps) > 1:
    reply(500, {"success": 500, "message": "the stand-in core knows one dirt at a time: %s" % ", ".join(steps)})
at = datetime.fromtimestamp(time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
# the ways of being wrong
w = os.environ.get("FAKE_CORE_WRITE")
if w and mode == "write":
    open(w, "a").write("x")
if mode == "restart":
    open(os.path.join(F, "inv", os.environ["FAKE_CORE_UNIT"]), "w").write("restarted-%d" % time.time())
if mode == "listen":
    open(os.path.join(F, "ports"), "a").write("tcp 0.0.0.0:9999\n")
if mode == "hang" and lock:
    reply(0, "", 28)
if mode == "hang500":
    reply(500, {"success": 500, "message": "boom"})
wrong = "extra:" + os.environ["FAKE_CORE_CODE"] if mode == "extra" and steps else "nobusy" if mode == "nobusy" else ""
# a Default-Release that no source offers makes apt fail, and the core with it
if steps == ["sources_pin"] and release != L["codename"]:
    wrong = "apt_fails"
body = M["answer"](steps[0] if steps else "", leg, at, wrong)
if mode == "leak":
    body["data"]["text"] += "host %s\n" % open(os.path.join(F, "hostname")).read().strip()
reply(200, body)
'''
    FAKES = {
        "curl": r'''#!/bin/sh
out=""; fmt=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w) fmt="$2"; shift 2 ;;
    -H|--max-time) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
path="/${url#http://127.0.0.1:*/}"
echo "curl $path" >>"$FAKE/calls"
res="$(python3 "$FAKE/fakecore.py" "${path#/}" "${out:-/dev/null}")"
set -- $res
[ -z "$fmt" ] || printf '%03d' "$1"
exit "$2"
''',
        "systemctl": r'''#!/bin/sh
cmd="$1"; shift
case "$cmd" in
  stop) for u in "$@"; do case "$u" in -*) ;; *) rm -f "$FAKE/active/$u" ;; esac; done ;;
  start) for u in "$@"; do case "$u" in -*) ;; *) : >"$FAKE/active/$u" ;; esac; done ;;
  is-active) [ -e "$FAKE/active/$1" ] && echo active || { echo inactive; exit 3; } ;;
  is-system-running) echo running ;;
  list-units)
    all=""; for a in "$@"; do [ "$a" != "--all" ] || all=1; done
    while read -r u l a s rest; do [ -n "$all" ] || [ "$a" = active ] || continue; echo "$u $l $a $s $rest"; done <"$FAKE/units.list" ;;
  list-timers) printf 'Mon 2026-10-12 00:00:00 UTC 3h left Sun 2026-10-11 00:00:12 UTC 20h ago logrotate.timer logrotate.service\n' ;;
  show)
    while [ $# -gt 0 ]; do
      u="$1"; shift
      case "$u" in -p) shift; continue ;; -*) continue ;; esac
      st=inactive; sub=dead; pid=0
      if [ -e "$FAKE/active/$u" ]; then st=active; sub=running; pid=$(( $(printf '%s' "$u" | cksum | cut -d' ' -f1) % 30000 + 100 )); fi
      inv="inv-$u"; [ ! -e "$FAKE/inv/$u" ] || inv="$(cat "$FAKE/inv/$u")"
      printf 'Id=%s\nActiveState=%s\nSubState=%s\nMainPID=%s\nInvocationID=%s\n\n' "$u" "$st" "$sub" "$pid" "$inv"
    done ;;
  *) echo "systemctl $cmd $*" >>"$FAKE/calls" ;;
esac
''',
        "docker": r'''#!/bin/sh
cmd="$1"; shift
case "$cmd" in
  info|version) [ -e "$FAKE/active/docker.service" ] || exit 1; echo 28.0.4 ;;
  ps)
    [ -e "$FAKE/active/docker.service" ] || exit 1
    case "$*" in
      *--filter*) f=""; for a in "$@"; do case "$a" in name=*) f="${a#name=}" ;; esac; done
                  n="${f#^}"; n="${n%\$}"; while IFS='|' read -r name rest; do [ "$name" != "$n" ] || echo "id-$name"; done <"$FAKE/containers" ;;
      *) while IFS='|' read -r name rest; do [ -z "$name" ] || echo "$name"; done <"$FAKE/containers" ;;
    esac ;;
  run)
    name=""; policy="no"; net="bridge"; ports=""
    while [ $# -gt 0 ]; do case "$1" in --name) name="$2"; shift 2 ;; --restart) policy="$2"; shift 2 ;; --network) net="$2"; shift 2 ;;
      -p) spec="$2"; shift 2; ip=""; hp="${spec%%:*}"
          case "$spec" in *:*:*) ip="${spec%%:*}"; rest="${spec#*:}"; hp="${rest%%:*}" ;; esac
          # a port that something of the box listens on, on that address or on every one of them, cannot be bound again: Docker says so, and exits 125
          while read -r proto addr; do
            [ "$proto" = tcp ] && [ "${addr##*:}" = "$hp" ] || continue
            case "${addr%:*}:$ip" in "*:"*|"0.0.0.0:"*|*":"|*":0.0.0.0"|"$ip:$ip") echo "docker: Error response from daemon: failed to bind host port for $spec: address already in use." >&2; exit 125 ;; esac
          done <"$FAKE/ports"
          ports="$hp" ;;
      busybox|nginx:alpine) break ;; *) shift ;; esac; done
    echo "$name|$policy|$net|$ports" >>"$FAKE/containers"; echo "id-$name" ;;
  rm) shift; n="$1"; grep -v "^$n|" "$FAKE/containers" >"$FAKE/containers.new"; mv "$FAKE/containers.new" "$FAKE/containers" ;;
  *) ;;
esac
''',
        "apt-config": r'''#!/bin/sh
# apt-config dump: the Default-Release of apt.conf.d, and what apt does with one that no source of the box offers: it fails, as every command that opens its cache does
rel="$(cat "$APT_DIR"/apt.conf.d/* 2>/dev/null | sed -n 's/^APT::Default-Release "\([^"]*\)";$/\1/p' | sed -n '$p')"
if [ -n "$rel" ]; then
  if ! cat "$APT_DIR/sources.list" "$APT_DIR"/sources.list.d/* 2>/dev/null | grep -Eq "^deb[[:space:]].*[[:space:]]$rel(-[a-z-]+)?[[:space:]]"; then
    echo "E: The value '$rel' is invalid for APT::Default-Release as such a release is not available in the sources" >&2
    exit 100
  fi
  echo "APT::Default-Release \"$rel\";"
fi
''',
        "apt-mark": r'''#!/bin/sh
case "$1" in
  hold) echo "$2" >>"$FAKE/holds" ;;
  unhold) grep -vx "$2" "$FAKE/holds" >"$FAKE/holds.new" || true; mv "$FAKE/holds.new" "$FAKE/holds" ;;
  showhold) cat "$FAKE/holds" ;;
esac
''',
        "dpkg": r'''#!/bin/sh
case "$1" in
  --add-architecture) echo "$2" >>"$FAKE/archs" ;;
  --remove-architecture) if grep -q ":$2\$" "$FAKE/pkgs"; then echo "dpkg: error: cannot remove architecture '$2' currently in use" >&2; exit 2; fi
                         grep -vx "$2" "$FAKE/archs" >"$FAKE/archs.new" || true; mv "$FAKE/archs.new" "$FAKE/archs" ;;
  --print-foreign-architectures) cat "$FAKE/archs" ;;
  -i) pkg="$(sed -n 's/^Package: //p' "$2")"; arch="$(sed -n 's/^Architecture: //p' "$2")"; echo "$pkg:$arch" >>"$FAKE/pkgs" ;;
  --purge) grep -vx "$2" "$FAKE/pkgs" >"$FAKE/pkgs.new" || true; mv "$FAKE/pkgs.new" "$FAKE/pkgs" ;;
  --version) echo "Debian 'dpkg' package management program version 1.20.14 (amd64)." ;;
esac
''',
        "dpkg-deb": r'''#!/bin/sh
cp "$2/DEBIAN/control" "$3"
''',
        "dpkg-query": r'''#!/bin/sh
for a in "$@"; do last="$a"; done
if grep -q "^$last:" "$FAKE/pkgs" 2>/dev/null; then printf 'ii '; exit 0; fi
exit 1
''',
        "systemd-run": r'''#!/bin/sh
for a in "$@"; do case "$a" in --unit=*) : >"$FAKE/active/${a#--unit=}" ;; esac; done
''',
        "fallocate": r'''#!/bin/sh
size="${2%M}"; echo "$size" >"$3"
''',
        "df": r'''#!/bin/sh
fill=0; [ ! -e "$BOOT_DIR/osr-fill" ] || fill="$(cat "$BOOT_DIR/osr-fill")"
echo "Filesystem 1048576-blocks Used Available Capacity Mounted on"
echo "/dev/vda1 30000 2000 $((28000 - fill)) 7% /"
''',
        "ss": r'''#!/bin/sh
# -t and -u say which protocols: a line of the other one is not listed
t=""; u=""
case "$*" in *t*) t=1 ;; esac
case "$*" in *u*) u=1 ;; esac
while read -r proto rest; do
  case "$proto" in tcp) [ -n "$t" ] || continue ;; udp) [ -n "$u" ] || continue ;; esac
  echo "$proto LISTEN 0 4096 $rest 0.0.0.0:*"
done <"$FAKE/ports"
''',
        "ip": r'''#!/bin/sh
case "$*" in
  *"-4"*) printf '1: lo    inet 127.0.0.1/8 scope host lo\n2: ens3    inet 10.0.2.15/24 brd 10.0.2.255 scope global ens3\n3: docker0    inet 172.17.0.1/16 scope global docker0\n' ;;
  *"-6"*) printf '2: ens3    inet6 fe80::5054:ff:fe12:3456/64 scope link\n' ;;
  *link*) printf '1: lo: <LOOPBACK> mtu 65536 link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00\n2: ens3: <BROADCAST> mtu 1500 link/ether 52:54:00:12:34:56 brd ff:ff:ff:ff:ff:ff\n' ;;
esac
''',
        "free": "#!/bin/sh\nprintf '              total\\nMem:           5943\\n'\n",
        "findmnt": "#!/bin/sh\necho 'ext4 /dev/vda1'\n",
        "systemd-detect-virt": "#!/bin/sh\necho kvm\n",
        "nproc": "#!/bin/sh\necho 4\n",
        "uname": "#!/bin/sh\necho 5.10.0-46-cloud-amd64\n",
        "apt-get": "#!/bin/sh\necho 'apt 2.2.4 (amd64)'\n",
    }
    HAVE_FCNTL = subprocess.run([sys.executable, "-c", "import fcntl"], capture_output=True).returncode == 0

    def sandbox(tmp):
        """the box of the guest in a directory of its own: the fake tools in front of PATH, the files the guest script's variables point at, the state the tools keep"""
        j = lambda *p: os.path.join(tmp, *p).replace("\\", "/")
        for sub in ("fake", "fake/active", "fake/inv", "root/etc/apt/sources.list.d", "root/etc/apt/apt.conf.d", "root/etc/apt/preferences.d", "root/etc/casaos", "root/var/lib/dpkg/updates",
                    "root/var/lib/apt/lists", "root/var/cache/apt", "root/var/lib/casaos", "root/var/log/casaos", "root/boot", "root/run/casaos", "lib", "auth", "out"):
            os.makedirs(j(*sub.split("/")), exist_ok=True)
        for name, text in FAKES.items():
            with open(j("fake", name), "w", newline="\n") as f:
                f.write(text)
            os.chmod(j("fake", name), 0o755)
        for name, text in (("fakecore.py", FAKECORE), ("model.py", ANSWERS_SRC)):
            with open(j("fake", name), "w", newline="\n") as f:
                f.write(text)
        for unit in list(UNITS) + ["docker.socket", "containerd.service"]:
            if UNITS.get(unit, ("active",))[0] == "active" or unit in ("docker.socket", "containerd.service"):
                open(j("fake", "active", unit), "w").close()
        with open(j("fake", "units.list"), "w", newline="\n") as f:
            f.write("".join("%s loaded %s %s x\n" % (u, v[0], v[1]) for u, v in sorted(UNITS.items())))
        for name, text in (("holds", ""), ("archs", ""), ("pkgs", ""), ("ports", "\n".join(PORTS) + "\n"), ("hostname", HOST + "\n"),
                           ("containers", "".join("%s|%s|%s|%s\n" % (c["name"], c["policy"], c["network"], c["ports"]) for c in WITNESSES))):
            with open(j("fake", name), "w", newline="\n") as f:
                f.write(text)
        with open(j("root/etc/fstab"), "w", newline="\n") as f:
            f.write("UUID=root-uuid / ext4 defaults 0 1\n")
        with open(j("root/etc/os-release"), "w", newline="\n") as f:
            f.write('PRETTY_NAME="%s"\nID=%s\nVERSION_ID="%s"\nVERSION_CODENAME=%s\n' % (LEG["d11"]["pretty"], "debian", "11", "bullseye"))
        for name, text in (("root/etc/apt/sources.list", "deb http://deb.debian.org/debian bullseye main\n"), ("root/etc/apt/sources.list.d/docker.list", "deb https://download.docker.com/linux/debian bullseye stable\n"),
                           ("root/var/lib/dpkg/lock-frontend", ""), ("root/var/lib/dpkg/status", "Package: x\n"), ("root/run/casaos/internal.secret", "sekrit\n"),
                           ("root/var/log/casaos/casaos.log", "log\n"), ("root/var/lib/casaos/state", "s\n"), ("root/etc/casaos/gateway.ini", "HttpPort=80\n")):
            with open(j(*name.split("/")), "w", newline="\n") as f:
                f.write(text)
        env = dict(ENV, PATH=j("fake") + os.pathsep + os.environ["PATH"], FAKE=j("fake"), LEG="d11", HAVE_FCNTL="1" if HAVE_FCNTL else "0", APT_DIR=j("root/etc/apt"), FSTAB=j("root/etc/fstab"),
                   BOOT_DIR=j("root/boot"), OS_RELEASE=j("root/etc/os-release"), DPKG_JOURNAL=j("root/var/lib/dpkg/updates/0001"), DPKG_LOCK=j("root/var/lib/dpkg/lock-frontend"),
                   REBOOT_REQUIRED=j("root/run/reboot-required"), INTERNAL_SECRET_FILE=j("root/run/casaos/internal.secret"), LIBDIR=j("lib"), AUTH_DIR=j("auth"), OUT=j("out"),
                   IMAGE_UPDATES=j("root/var/lib/casaos/image_updates.json"), FIXTURES=FIXTURES.replace("\\", "/"), LEG_JSON=json.dumps(LEG),
                   INV_ROOT=j("root"), INV_TREES=j("root/etc"))
        return env, j

    PRELUDE = ('source "$1"; shift; export LC_ALL=C; FAMILY=debian; FROM_CODENAME=bullseye; TO_CODENAME=bookworm; CASA_PORT=80; PENDING_UNDO=""; '
               'APT=(env DEBIAN_FRONTEND=noninteractive apt-get); sleep() { command sleep 0.05; }; ')
    LOCK_SHIM = ('lock_hold_start() { : >"$FAKE/lock.held"; }; lock_hold_stop() { rm -f "$FAKE/lock.held"; }; lock_state() { if [ -e "$FAKE/lock.held" ]; then echo held; else echo free; fi; }; ')

    def run_bash(env, snippet, lock_shim=True, timeout=300):
        text = PRELUDE + ("" if HAVE_FCNTL or not lock_shim else LOCK_SHIM) + snippet
        return subprocess.run([BASH, "-c", text, "_", SCRIPT.replace("\\", "/")], capture_output=True, text=True, env=env, timeout=timeout, encoding="utf-8", errors="replace")

    def gcase(label, snippet, files=None, calls=(), no_calls=(), ordered=(), lock_shim=True, setup=None, returned=True, rc=None, in_stdout=(), prelude_env=None):
        """a snippet run in a bash that has sourced the guest script, with the stand-ins in front. files maps a file of the sandbox ($OUT, relative to the sandbox) to its exact content
        (a string ending in * is a prefix), None to its absence; calls and no_calls are what the stand-ins were called with (the file `calls` of the fake directory)"""
        if not wanted(label):
            return
        count[0] += 1
        with tempfile.TemporaryDirectory() as tmp:
            env, j = sandbox(tmp)
            env.update(prelude_env or {})
            if setup:
                setup(tmp, j)
            p = run_bash(env, snippet + ("; echo returned" if returned else ""), lock_shim)

            def get(path):
                try:
                    with open(os.path.join(tmp, path)) as f:
                        return f.read().replace("\r", "")
                except OSError:
                    return None
            seen = get("fake/calls") or ""
            problems = []
            if returned and "returned" not in p.stdout:
                problems.append("did not return: %r %r" % (p.stdout[-300:], p.stderr[-400:]))
            if rc is not None and p.returncode != rc:
                problems.append("exit %d, wanted %d" % (p.returncode, rc))
            for name, want in (files or {}).items():
                got = get(name)
                if want is None and got is not None:
                    problems.append("%s should not be there" % name)
                elif want is not None and (got is None or not (got.startswith(want[:-1]) if want.endswith("*") else got == want)):
                    problems.append("%s is %r, wanted %r" % (name, got, want))
            problems += ["missing call %r in %r" % (s, seen) for s in calls if s not in seen]
            problems += ["should not have called %r in %r" % (s, seen) for s in no_calls if s in seen]
            problems += ["stdout lacks %r: %r" % (s, p.stdout) for s in in_stdout if s not in p.stdout]
            if ordered and not all(seen.find(a) != -1 and seen.find(a) < seen.find(b) for a, b in zip(ordered, ordered[1:])):
                problems.append("the calls are not in the order %r: %r" % (ordered, seen))
            if problems:
                failures.append("%s: %s\n%s" % (label, "; ".join(problems), p.stderr[-400:]))
            else:
                print("ok: " + label)

    # ---- the text-only functions ----
    p_ = run_bash(sandbox(tempfile.mkdtemp())[0], "for l in d11 u22; do leg_facts $l; done; leg_facts d12 || echo no-leg")
    check("leg_facts says what each leg is, and refuses another", p_.stdout.split() == "debian 11 bullseye 12 bookworm ubuntu 22.04 jammy 24.04 noble no-leg".split(), p_.stdout + p_.stderr[-200:])
    p_ = run_bash(sandbox(tempfile.mkdtemp())[0], "major_of 5:29.8.2-1~ubuntu.24.04~noble; major_of 1.7.27-1")
    check("major_of reads the major of an apt version", p_.stdout.split() == ["29", "1"], p_.stdout)
    p_ = run_bash(sandbox(tempfile.mkdtemp())[0], "echo sourced")
    check("sourcing the guest script runs nothing", p_.stdout.strip() == "sourced" and p_.returncode == 0, p_.stdout + p_.stderr[-200:])
    env_ = {k: v for k, v in os.environ.items() if k != "READINESS_DISPOSABLE"}
    for label, args, env_extra, want in (("a machine that is not marked disposable", ["d11"], {}, "disposable machine"), ("a leg that is neither", ["sideways"], {"READINESS_DISPOSABLE": "1"}, "usage"),
                                         ("no leg at all", [], {"READINESS_DISPOSABLE": "1"}, "usage")):
        count[0] += 1
        p_ = subprocess.run([BASH, SCRIPT.replace("\\", "/")] + args, capture_output=True, text=True, env=dict(env_, **env_extra))
        check("the guest script refuses " + label, p_.returncode == 2 and want in p_.stderr, "%d %r" % (p_.returncode, p_.stderr[-300:]))
    APT_CACHE = r"""#!/bin/sh
awk 'BEGIN { for (i = 0; i < 2500; i++) printf " containerd.io | 1.%s.%d-1 | https://download.docker.com/linux/debian bullseye/stable amd64 Packages\n", ((i < 3 && ENVIRON["NO17"] == "") ? "7" : "6"), 100 - i }'
"""
    gcase("containerd_17 reads all of apt-cache's listing under pipefail, and says the newest containerd.io of the 1.7 line", 'set -euo pipefail; v="$(containerd_17)"; echo "$v" >"$OUT/said"',
          files={"out/said": "1.7.100-1\n"}, setup=lambda tmp, j: (open(j("fake", "apt-cache"), "w", newline="\n").write(APT_CACHE), os.chmod(j("fake", "apt-cache"), 0o755)))
    gcase("containerd_17 succeeds and says nothing when apt has no 1.7", 'set -euo pipefail; v="$(containerd_17)"; echo "$v" >"$OUT/said"', files={"out/said": "\n"},
          setup=lambda tmp, j: (open(j("fake", "apt-cache"), "w", newline="\n").write(APT_CACHE), os.chmod(j("fake", "apt-cache"), 0o755)), prelude_env={"NO17": "1"})

    # ---- in_subshell: the status of a step is the status of its FIRST failing command ----
    gcase("in_subshell: a step whose first command fails and whose last one works has the status of the first", 'f() { false; true; }; in_subshell f; echo "$STATUS" >"$OUT/s"', files={"out/s": "1\n"})
    gcase("in_subshell: a step that works says 0, and the function always returns 0 under set -e", 'set -e; f() { true; }; in_subshell f; echo "$STATUS" >"$OUT/s"; in_subshell false; echo "$STATUS" >"$OUT/t"', files={"out/s": "0\n", "out/t": "1\n"})
    gcase("in_subshell: what the step prints and what it leaves in variables stays in its subshell", 'f() { X=changed; echo said; }; X=kept; in_subshell f >"$OUT/o"; echo "$X" >"$OUT/x"', files={"out/o": "said\n", "out/x": "kept\n"})
    gcase("in_subshell: a failure inside it does not end the script, and a failure of the script's own still does", 'set -e; in_subshell false; echo alive >"$OUT/a"; false; echo after >"$OUT/b"',
          files={"out/a": "alive\n", "out/b": None}, returned=False)

    # ---- run_step: the dirt, a call, the undo, a check that the undo held, a call ----
    STUBS = ('ready_call() { echo "ready_call $1" >>"$FAKE/calls"; }; log() { :; }; marker() { :; }; dirty_x() { echo dirty >>"$FAKE/calls"; ${DIRTY_CMD:-true}; }; '
             'undo_x() { echo undo >>"$FAKE/calls"; ${UNDO_CMD:-true}; }; undone_x() { echo undone >>"$FAKE/calls"; ${UNDONE_CMD:-true}; }; ')
    gcase("run_step: the dirt, a call, the undo, the check that it held, a call, in that order, and what each said", STUBS + 'run_step x',
          files={"out/step-x.dirty-rc": "0\n", "out/step-x.undo-rc": "0\n", "out/step-x.undone": "yes\n", "out/steps.list": "x\n", "out/step-x.skipped": None},
          ordered=["dirty", "ready_call step-x-dirty", "undo", "undone", "ready_call step-x-clean"])
    gcase("run_step: a dirt that failed is recorded, the box is asked about and undone all the same", STUBS + 'run_step x', prelude_env={"DIRTY_CMD": "false"},
          files={"out/step-x.dirty-rc": "1\n", "out/step-x.undo-rc": "0\n", "out/step-x.undone": "yes\n"}, ordered=["dirty", "ready_call step-x-dirty", "undo", "undone", "ready_call step-x-clean"])
    gcase("run_step: a dirt whose first command failed is a failure whatever the last one did", STUBS + 'dirty_x() { false; true; }; run_step x', files={"out/step-x.dirty-rc": "1\n"})
    gcase("run_step: an undo that failed is recorded, stays pending for the trap, and the check says the box is not back", STUBS + 'run_step x; echo "pending=${PENDING_UNDO}" >"$OUT/p"',
          prelude_env={"UNDO_CMD": "false", "UNDONE_CMD": "false"}, files={"out/step-x.undo-rc": "1\n", "out/step-x.undone": "no\n", "out/p": "pending=x\n"})
    gcase("run_step: an undo that held leaves nothing pending", STUBS + 'run_step x; echo "pending=${PENDING_UNDO}" >"$OUT/p"', files={"out/p": "pending=\n"})
    gcase("run_step: the undo is pending while the dirt and the call are made", STUBS + 'dirty_x() { echo "pending=${PENDING_UNDO}" >>"$FAKE/calls"; }; run_step x', calls=["pending=x"])
    gcase("run_step: a step that does not exist on this system is skipped, with the reason, and nothing is made", STUBS + 'skip_x() { echo "not here"; }; run_step x',
          files={"out/step-x.skipped": "not here\n", "out/step-x.dirty-rc": None, "out/step-x.undone": None}, no_calls=["dirty", "undo", "ready_call"])
    gcase("run_step: a step with a skip function that says nothing is run", STUBS + 'skip_x() { true; }; run_step x', calls=["dirty", "undo"], files={"out/step-x.skipped": None})
    gcase("the trap of the script undoes the step that was in the middle of its dirt when the script died, and says why it died", STUBS + 'guard_run; PENDING_UNDO=x; false',
          files={"out/aborted": "the guest script ended on a failing command (exit 1)*", "out/step-x.undo-rc-exit": "0\n"}, calls=["undo"], returned=False, rc=1)
    gcase("the trap of the script does nothing when no step is pending", STUBS + 'guard_run; true', no_calls=["undo"], files={"out/aborted": None})
    gcase("the trap of the script lets go of dpkg's lock", STUBS + 'lock_hold_stop() { echo lock_hold_stop >>"$FAKE/calls"; }; guard_run; true', calls=["lock_hold_stop"])
    gcase("a run that stops on purpose says what it said, once, and does not add a failing command", STUBS + 'guard_run; abort "NOT PROVEN: no older release"',
          files={"out/aborted": "NOT PROVEN: no older release\n"}, returned=False)

    # ---- the dirt and the undo of every step, with stand-ins that keep the box in files ----
    gcase("held: apt-mark hold libc6, and unhold it", 'in_subshell dirty_held; echo "$STATUS" >"$OUT/rc1"; cp "$FAKE/holds" "$OUT/during"; in_subshell undone_held; echo "$STATUS" >"$OUT/d"; in_subshell undo_held; echo "$STATUS" >"$OUT/rc2"; '
          'in_subshell undone_held; echo "$STATUS" >"$OUT/u"', files={"out/rc1": "0\n", "out/during": "libc6\n", "out/d": "1\n", "out/rc2": "0\n", "out/u": "0\n"}, calls=[])
    gcase("held_docker: apt-mark hold docker-ce, and unhold it", 'in_subshell dirty_held_docker; echo "$STATUS" >"$OUT/rc1"; cp "$FAKE/holds" "$OUT/during"; in_subshell undo_held_docker; in_subshell undone_held_docker; echo "$STATUS" >"$OUT/u"',
          files={"out/rc1": "0\n", "out/during": "docker-ce\n", "out/u": "0\n"})
    for sid, want_line in (("sources_cdrom", "deb cdrom:[Readiness proof disc 1]/ bullseye main"), ("sources_file", "deb [trusted=yes] file:/srv/osr-local-repo ./"), ("sources_alias", "deb http://deb.debian.org/debian stable main"),
                           ("sources_proposed", "deb http://deb.debian.org/debian bullseye-proposed-updates main"), ("sources_third_party", "deb https://repo.example.invalid/osr-third-party stable main")):
        fname = {"sources_cdrom": "osr-cdrom.list", "sources_file": "osr-file.list", "sources_alias": "osr-alias.list", "sources_proposed": "osr-proposed.list", "sources_third_party": "osr-third-party.list"}[sid]
        gcase("%s: the source line is written, in a file of its own that the undo removes" % sid,
              'in_subshell dirty_%s; echo "$STATUS" >"$OUT/rc1"; cp "$APT_DIR/sources.list.d/%s" "$OUT/line"; in_subshell undo_%s; ls "$APT_DIR/sources.list.d" >"$OUT/left"; in_subshell undone_%s; echo "$STATUS" >"$OUT/rc2"' % (sid, fname, sid, sid),
              files={"out/rc1": "0\n", "out/line": want_line + "\n", "out/left": "docker.list\n", "out/rc2": "0\n"})
    gcase("sources_proposed on Ubuntu: jammy-proposed, with universe", 'FAMILY=ubuntu; FROM_CODENAME=jammy; in_subshell dirty_sources_proposed; cp "$APT_DIR/sources.list.d/osr-proposed.list" "$OUT/line"',
          files={"out/line": "deb http://archive.ubuntu.com/ubuntu jammy-proposed main universe\n"})
    gcase("sources_alias on Ubuntu: `devel`, an alias of Ubuntu's archive (dists/devel is there), and the file goes with the undo", 'FAMILY=ubuntu; in_subshell dirty_sources_alias; echo "$STATUS" >"$OUT/rc1"; cp "$APT_DIR/sources.list.d/osr-alias.list" "$OUT/line"; in_subshell undo_sources_alias; in_subshell undone_sources_alias; echo "$STATUS" >"$OUT/rc2"',
          files={"out/rc1": "0\n", "out/line": "deb http://archive.ubuntu.com/ubuntu devel main\n", "out/rc2": "0\n"})
    gcase("sources_alias on Debian: `stable`", 'FAMILY=debian; in_subshell dirty_sources_alias; cp "$APT_DIR/sources.list.d/osr-alias.list" "$OUT/line"', files={"out/line": "deb http://deb.debian.org/debian stable main\n"})
    gcase("sources_pin: a Default-Release naming the box's own release, and a pin of the preferences naming the target, and both undone", 'in_subshell dirty_sources_pin; in_subshell dirty_sources_pin_prefs; cp "$APT_DIR/apt.conf.d/99osr-default-release" "$OUT/dr"; '
          'cp "$APT_DIR/preferences.d/99osr-pin" "$OUT/pin"; in_subshell undo_sources_pin; in_subshell undo_sources_pin_prefs; ls "$APT_DIR/apt.conf.d" "$APT_DIR/preferences.d" >"$OUT/left"',
          files={"out/dr": 'APT::Default-Release "bullseye";\n', "out/pin": "Package: *\nPin: release n=bookworm\nPin-Priority: 100\n", "out/left": "*"})
    gcase("sources_pin on Ubuntu names jammy", 'FROM_CODENAME=jammy; in_subshell dirty_sources_pin; cp "$APT_DIR/apt.conf.d/99osr-default-release" "$OUT/dr"', files={"out/dr": 'APT::Default-Release "jammy";\n'})
    gcase("sources_pin: a Default-Release that the sources of the box offer is one apt does not fail on (the stand-in apt fails as apt does on one that no source offers)",
          'in_subshell dirty_sources_pin; apt-config dump >"$OUT/dump" 2>"$OUT/err"; echo "$?" >"$OUT/rc"', files={"out/rc": "0\n", "out/dump": 'APT::Default-Release "bullseye";\n', "out/err": ""})
    gcase("the stand-in apt fails, as apt does, on the Default-Release of the target (which no source of the box carries before the hop): status 100, in every command that opens its cache",
          'printf \'APT::Default-Release "bookworm";\\n\' >"$APT_DIR/apt.conf.d/99osr-default-release"; apt-config dump >"$OUT/dump" 2>"$OUT/err"; echo "$?" >"$OUT/rc"',
          files={"out/rc": "100\n", "out/dump": "", "out/err": "E: The value 'bookworm' is invalid for APT::Default-Release*"})
    gcase("broken_dpkg: the file is made empty, with a name of digits only (apt counts it as an unfinished journal), and the undo takes it away",
          'in_subshell dirty_broken_dpkg; echo "$STATUS" >"$OUT/rc1"; basename "$DPKG_JOURNAL" >"$OUT/name"; [ -e "$DPKG_JOURNAL" ] && echo there >"$OUT/there"; in_subshell undo_broken_dpkg; [ ! -e "$DPKG_JOURNAL" ] && echo gone >"$OUT/gone"',
          files={"out/rc1": "0\n", "out/name": "0001\n", "out/there": "there\n", "out/gone": "gone\n"})
    check("the file the proof makes for an unfinished dpkg journal is a file of digits only in /var/lib/dpkg/updates (apt's own rule)", re.search(r'^DPKG_JOURNAL="\$\{DPKG_JOURNAL:-/var/lib/dpkg/updates/[0-9]+\}"', guest_text, re.M) is not None, "")
    gcase("reboot_first: /var/run/reboot-required is made and removed", 'in_subshell dirty_reboot_first; [ -e "$REBOOT_REQUIRED" ] && echo there >"$OUT/there"; in_subshell undo_reboot_first; [ ! -e "$REBOOT_REQUIRED" ] && echo gone >"$OUT/gone"',
          files={"out/there": "there\n", "out/gone": "gone\n"})
    gcase("fstab_unresolved: a line that is not nofail for a device that is not there, and the undo gives back the file byte for byte", 'cp "$FSTAB" "$OUT/orig"; in_subshell dirty_fstab_unresolved; echo "$STATUS" >"$OUT/rc1"; cp "$FSTAB" "$OUT/dirty"; '
          'in_subshell undo_fstab_unresolved; cp "$FSTAB" "$OUT/back"; in_subshell undone_fstab_unresolved; echo "$STATUS" >"$OUT/rc2"',
          files={"out/rc1": "0\n", "out/orig": "UUID=root-uuid / ext4 defaults 0 1\n", "out/dirty": "UUID=root-uuid / ext4 defaults 0 1\nUUID=0a1b2c3d-0000-4000-8000-000000000001 /mnt/osr-missing ext4 defaults 0 2\n",
                 "out/back": "UUID=root-uuid / ext4 defaults 0 1\n", "out/rc2": "0\n"})
    gcase("fstab_unresolved: a copy of the original that is already there is not overwritten (a dirt that was not undone)", 'echo old >"$AUTH_DIR/fstab.orig"; in_subshell dirty_fstab_unresolved; echo "$STATUS" >"$OUT/rc1"', files={"out/rc1": "1\n"})
    gcase("containers_policy, containers_ports_host, containers_ports_publish: the containers that dirty the box (the last on port 53 of a loopback alias), and their removal", 'in_subshell dirty_containers_policy; in_subshell dirty_containers_ports_host; in_subshell dirty_containers_ports_publish; echo "$STATUS" >"$OUT/rcp"; '
          'cat "$FAKE/containers" >"$OUT/c"; in_subshell undo_containers_policy; in_subshell undo_containers_ports_host; in_subshell undo_containers_ports_publish; cat "$FAKE/containers" >"$OUT/c2"; in_subshell undone_containers_policy; echo "$STATUS" >"$OUT/rc"',
          files={"out/c": "p-always|always|bridge|\np-unless-stopped|unless-stopped|bridge|\np-db|unless-stopped|bridge|\np-web|unless-stopped|bridge|18081\np-no|no|bridge|\np-hostnet|unless-stopped|host|\np-pub53|unless-stopped|bridge|53\n",
                 "out/c2": "p-always|always|bridge|\np-unless-stopped|unless-stopped|bridge|\np-db|unless-stopped|bridge|\np-web|unless-stopped|bridge|18081\n", "out/rc": "0\n", "out/rcp": "0\n"})
    gcase("the stand-in Docker cannot publish a port the box listens on, as Docker cannot: not 443 or 80 (the gateway), not 53 on every address (systemd-resolved is on 127.0.0.53), and can on 127.0.0.2:53",
          'docker run -d --name a -p 443:80 nginx:alpine 2>/dev/null; echo "$?" >"$OUT/a"; docker run -d --name b -p 80:80 nginx:alpine 2>/dev/null; echo "$?" >"$OUT/b"; docker run -d --name c -p 0.0.0.0:53:80 nginx:alpine 2>/dev/null; echo "$?" >"$OUT/c"; '
          'docker run -d --name d -p 53:80 nginx:alpine 2>/dev/null; echo "$?" >"$OUT/d"; docker run -d --name e -p 127.0.0.2:53:80 nginx:alpine >/dev/null; echo "$?" >"$OUT/e"; docker run -d --name f -p 127.0.0.1:41873:80 nginx:alpine 2>/dev/null; echo "$?" >"$OUT/f"; '
          'docker run -d --name g -p 127.0.0.2:18081:80 nginx:alpine 2>/dev/null; echo "$?" >"$OUT/g"; docker run -d --name h -p 8080:80 nginx:alpine >/dev/null; echo "$?" >"$OUT/h"; cat "$FAKE/containers" >"$OUT/c1"',
          files={"out/a": "125\n", "out/b": "125\n", "out/c": "125\n", "out/d": "125\n", "out/e": "0\n", "out/f": "125\n", "out/g": "125\n", "out/h": "0\n",
                 "out/c1": "p-always|always|bridge|\np-unless-stopped|unless-stopped|bridge|\np-db|unless-stopped|bridge|\np-web|unless-stopped|bridge|18081\ne|no|bridge|53\nh|no|bridge|8080\n"})
    gcase("containers_ports_publish: the dirt is made (status 0) where the gateway holds 80 and 443, and nothing is bound twice", 'in_subshell dirty_containers_ports_publish; echo "$STATUS" >"$OUT/rc"; docker ps -aq --filter name=^p-pub53$ >"$OUT/id"',
          files={"out/rc": "0\n", "out/id": "id-p-pub53\n"})
    gcase("busy_unit: a unit that holds the package update's name, stopped by the undo", 'in_subshell dirty_busy_unit; echo "$STATUS" >"$OUT/rc1"; in_subshell undone_busy_unit; echo "$STATUS" >"$OUT/d"; in_subshell undo_busy_unit; in_subshell undone_busy_unit; echo "$STATUS" >"$OUT/u"',
          files={"out/rc1": "0\n", "out/d": "1\n", "out/u": "0\n"})
    gcase("busy_lock: the lock is taken by another process and given back by the undo", 'in_subshell dirty_busy_lock; echo "$STATUS" >"$OUT/rc1"; lock_state >"$OUT/during"; in_subshell undone_busy_lock; echo "$STATUS" >"$OUT/d"; in_subshell undo_busy_lock; in_subshell undone_busy_lock; echo "$STATUS" >"$OUT/u"',
          files={"out/rc1": "0\n", "out/during": "held\n", "out/d": "1\n", "out/u": "0\n"})
    gcase("disk_boot: a file that leaves 200 MiB free, made with fallocate and not written, and removed by the undo", 'in_subshell dirty_disk_boot; echo "$STATUS" >"$OUT/rc1"; cat "$BOOT_DIR/osr-fill" >"$OUT/size"; in_subshell undo_disk_boot; [ ! -e "$BOOT_DIR/osr-fill" ] && echo gone >"$OUT/gone"',
          files={"out/rc1": "0\n", "out/size": "27800\n", "out/gone": "gone\n", "out/step-disk_boot.note": "a file of 27800 MiB on a filesystem that had 28000 MiB free\n"})
    gcase("disk_boot: a filesystem with no room to fill is a dirt that failed, and nothing is made", 'df() { echo Filesystem; echo "/dev/vda1 30000 29700 300 99% /"; }; in_subshell dirty_disk_boot; echo "$STATUS" >"$OUT/rc1"',
          files={"out/rc1": "1\n", "root/boot/osr-fill": None})
    gcase("apps: AppManagement is stopped, and the undo starts it and waits until it answers", 'in_subshell dirty_apps; in_subshell undone_apps; echo "$STATUS" >"$OUT/d"; in_subshell undo_apps; echo "$STATUS" >"$OUT/u"; in_subshell undone_apps; echo "$STATUS" >"$OUT/v"',
          files={"out/d": "1\n", "out/u": "0\n", "out/v": "0\n"})
    gcase("docker_down: Docker and its socket are stopped, and the undo brings them back, waits for Docker, and for the containers that come back by themselves", 'in_subshell dirty_docker_down; in_subshell undone_docker_down; echo "$STATUS" >"$OUT/d"; in_subshell undo_docker_down; echo "$STATUS" >"$OUT/u"; in_subshell undone_docker_down; echo "$STATUS" >"$OUT/v"',
          files={"out/d": "1\n", "out/u": "0\n", "out/v": "0\n", "out/step-docker_down.settle": "*"})
    gcase("docker_down: an undo whose containers do not come back is a failed undo", 'systemctl() { case "$1" in start) : >"$FAKE/active/docker.service" ;; esac; }; : >"$FAKE/containers"; in_subshell undo_docker_down; echo "$STATUS" >"$OUT/u"', files={"out/u": "1\n", "out/step-docker_down.settle": "never\n"})
    gcase("foreign_arch_empty: an architecture with no package, and the undo takes it away", 'in_subshell dirty_foreign_arch_empty; cat "$FAKE/archs" >"$OUT/a"; in_subshell undone_foreign_arch_empty; echo "$STATUS" >"$OUT/d"; in_subshell undo_foreign_arch_empty; in_subshell undone_foreign_arch_empty; echo "$STATUS" >"$OUT/u"',
          files={"out/a": "i386\n", "out/d": "1\n", "out/u": "0\n"})
    gcase("foreign_arch: the architecture and a package of it built from nothing (no network), and the undo purges the package then removes the architecture, in that order",
          'in_subshell dirty_foreign_arch; echo "$STATUS" >"$OUT/rc1"; cat "$FAKE/pkgs" >"$OUT/pkgs"; in_subshell undone_foreign_arch; echo "$STATUS" >"$OUT/d"; in_subshell undo_foreign_arch; echo "$STATUS" >"$OUT/rc2"; in_subshell undone_foreign_arch; echo "$STATUS" >"$OUT/u"',
          files={"out/rc1": "0\n", "out/pkgs": "osr-foreign:i386\n", "out/d": "1\n", "out/rc2": "0\n", "out/u": "0\n"})
    gcase("foreign_arch: an undo that cannot remove the architecture (a package of it is left) fails", 'dpkg() { case "$1" in --purge) return 0 ;; --remove-architecture) return 2 ;; *) command dpkg "$@" ;; esac; }; in_subshell undo_foreign_arch; echo "$STATUS" >"$OUT/rc"', files={"out/rc": "2\n"})

    # ---- the wait for AppManagement's own check of the images ----
    gcase("wait_image_check: the file is there, no wait", 'wait_image_check 5; echo "$?" >"$OUT/rc"', setup=lambda tmp, j: open(j("root/var/lib/casaos/image_updates.json"), "w").write("{}"), files={"out/rc": "0\n"})
    gcase("wait_image_check: the file comes while it waits", '( command sleep 0.4; echo "{}" >"$IMAGE_UPDATES" ) & wait_image_check 20; echo "$?" >"$OUT/rc"; wait', files={"out/rc": "0\n"})
    gcase("wait_image_check: the file never comes, and it says so when its time is over", 'wait_image_check 1; echo "$?" >"$OUT/rc"', files={"out/rc": "1\n"})

    # ---- the facts of the box ----
    gcase("leak_needles: the name of the run, every address and MAC but the loopback's, and the user", 'HOST_NAME=osr-abc; SUDO_USER=casa; leak_needles; cat "$OUT/leak-needles.tsv" >"$OUT/n"',
          files={"out/n": "hostname\tosr-abc\nipv4\t10.0.2.15\nipv4\t172.17.0.1\nipv6\tfe80::5054:ff:fe12:3456\nmac\t52:54:00:12:34:56\nuser\tcasa\n"})
    gcase("units_dump: one row a unit, with its state, its main pid and its invocation id, tab separated", 'units_dump t', files={"out/units-t.tsv": "casaos-app-management.service\tactive\trunning\t*"})
    gcase("units_dump: the units that are not running are listed too (--all): a unit that a call started and that ended in between is seen", 'units_dump t; grep -c "^logrotate.service" "$OUT/units-t.tsv" >"$OUT/n"',
          files={"out/n": "1\n"})
    gcase("listen_dump: the sockets as `<protocol> <address:port>`, sorted", 'listen_dump t', files={"out/listen-t.txt": LISTEN_SORTED})
    gcase("box_facts: the kernel, the boot mode, where /boot is, what the tools are; and a copy of os-release", 'box_facts', files={"out/box-facts": "kernel 5.10.0-46-cloud-amd64\ncpus 4\nmemory_mb 5943\nboot *", "out/os-release": "PRETTY_NAME=*"})

    # ---- api(): what a call leaves ----
    gcase("api: the body, the status, the time before and after, and how curl ended (written last)", 'api probe /v1/sys/os 30; cat "$OUT/probe.code" "$OUT/probe.curl" >"$OUT/both"; [ -s "$OUT/probe.t0" ] && [ -s "$OUT/probe.t1" ] && echo times >"$OUT/t"',
          files={"out/both": "200\n0\n", "out/t": "times\n", "out/probe.json": "{*"}, calls=["curl /v1/sys/os"])
    gcase("api: a curl that timed out is recorded as 28, with the status 000", 'curl() { printf "000"; return 28; }; api probe /v1/sys/os 1; cat "$OUT/probe.code" "$OUT/probe.curl" >"$OUT/both"', files={"out/both": "000\n28\n"})
    gcase("api: the call goes to the gateway with the loopback secret, and the secret is read again for each call", 'curl() { echo "$*" >>"$FAKE/calls"; printf 200; }; api a /x; echo other >"$INTERNAL_SECRET_FILE"; api b /y',
          calls=["-H Authorization: Internal sekrit http://127.0.0.1:80/x", "-H Authorization: Internal other http://127.0.0.1:80/y"], files={"out/a.curl": "0\n"})
    gcase("ready_call: the route, with a limit longer than the core's budget", 'curl() { echo "$*" >>"$FAKE/calls"; printf 200; }; ready_call r', calls=["--max-time 45", "http://127.0.0.1:80/v1/sys/os/readiness"])
    gcase("wait_services: until the gateway, the core and AppManagement answer 200, and says so when they do not", 'wait_services 5; echo "$?" >"$OUT/ok"; rm "$FAKE/active/casaos-app-management.service"; wait_services 1 >/dev/null; echo "$?" >"$OUT/bad"',
          files={"out/ok": "0\n", "out/bad": "1\n"}, returned=False)

    # ---- what the mutation checks of the guest script found to be left alone by the cases above ----
    gcase("the trap of the script adds nothing to what the run said before it failed", STUBS + 'guard_run; echo "first" >"$OUT/aborted"; false', files={"out/aborted": "first\n"}, returned=False, rc=1)
    gcase("api: the time after the call is later than the time before", 'curl() { command sleep 0.3; printf 200; }; api p /x; awk -v a="$(cat "$OUT/p.t0")" -v b="$(cat "$OUT/p.t1")" \'BEGIN { print (b > a) ? "later" : "same" }\' >"$OUT/cmp"',
          files={"out/cmp": "later\n"})
    gcase("api: a curl that says nothing and fails leaves the status 000", 'curl() { return 7; }; api p /x; cat "$OUT/p.code" "$OUT/p.curl" >"$OUT/both"', files={"out/both": "000\n7\n"})
    gcase("api: the call does not send the secret of an earlier one", 'curl() { echo "$*" >>"$FAKE/calls"; printf 200; }; echo one >"$INTERNAL_SECRET_FILE"; api a /x', calls=["Authorization: Internal one"], no_calls=["sekrit"])
    gcase("apps: the undo starts AppManagement and waits until it answers (it answers late)", 'code_of() { n=$(cat "$FAKE/n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" >"$FAKE/n"; if [ "$n" -lt 4 ]; then echo 000; else echo 200; fi; }; '
          'in_subshell undo_apps; echo "$STATUS" >"$OUT/u"; in_subshell undone_apps; echo "$STATUS" >"$OUT/v"', files={"out/u": "0\n", "out/v": "0\n"})
    gcase("apps: the undo starts the unit, then waits for the services", 'systemctl() { echo "systemctl $*" >>"$FAKE/calls"; }; wait_services() { echo "wait_services $1" >>"$FAKE/calls"; }; in_subshell undo_apps',
          ordered=["systemctl start casaos-app-management.service", "wait_services 120"])
    gcase("docker_down: the undo starts the socket and the service, waits for Docker, for the containers and for the services, in that order",
          'systemctl() { echo "systemctl $*" >>"$FAKE/calls"; }; wait_docker() { echo "wait_docker $1" >>"$FAKE/calls"; }; settle() { echo "settle $1" >>"$FAKE/calls"; }; wait_services() { echo "wait_services $1" >>"$FAKE/calls"; }; in_subshell undo_docker_down',
          ordered=["systemctl start docker.socket docker.service", "wait_docker 120", "settle step-docker_down.settle", "wait_services 120"])
    gcase("docker_down: the socket is stopped with the service, so that the first request does not start Docker again", 'in_subshell dirty_docker_down; ls "$FAKE/active" >"$OUT/active"; in_subshell undo_docker_down; ls "$FAKE/active" >"$OUT/active2"',
          files={"out/active": "casaos-app-management.service\ncasaos-gateway.service\ncasaos-message-bus.service\ncasaos.service\ncontainerd.service\nssh.service\n"})
    gcase("units_dump: the invocation id is the fifth column", 'units_dump t; awk -F"\\t" \'$1 == "casaos.service" {print NF, $2, $3, $5}\' "$OUT/units-t.tsv" >"$OUT/row"', files={"out/row": "5 active running inv-casaos.service\n"})
    gcase("listen_dump: TCP and UDP both", 'listen_dump t', files={"out/listen-t.txt": LISTEN_SORTED})
    # the real lock_hold_start, lock_state and lock_hold_stop over a stand-in for python3 that holds the lock as a process does (alive: held; killed: free), where fcntl is not
    FAKE_PY = r'''#!/bin/sh
case "$1" in
  */lockhold.py) trap 'kill $! 2>/dev/null; rm -f "$FAKE/holder.alive"; exit 0' TERM; echo locked; : >"$FAKE/holder.alive"; sleep 300 & wait ;;
  */lockprobe.py) if [ -e "$FAKE/holder.alive" ]; then echo held; else echo free; fi ;;
esac
'''

    def fake_python(tmp, j):
        with open(j("fake", "python3"), "w", newline="\n") as f:
            f.write(FAKE_PY)
        os.chmod(j("fake", "python3"), 0o755)
        for n in ("lockhold.py", "lockprobe.py"):
            open(j("lib", n), "w").close()
    gcase("lock_hold_start, lock_state and lock_hold_stop: a process holds the lock, another sees it held, and the holder is killed and the lock is free", 'lock_hold_start; echo "$?" >"$OUT/a"; lock_state >"$OUT/during"; lock_hold_stop; echo "$?" >"$OUT/b"; lock_state >"$OUT/after"',
          lock_shim=False, setup=fake_python, files={"out/a": "0\n", "out/during": "held\n", "out/b": "0\n", "out/after": "free\n"})
    gcase("lock_hold_stop says so when the lock is not given back", 'lock_hold_start; kill() { :; }; lock_hold_stop; echo "$?" >"$OUT/b"', lock_shim=False, setup=fake_python, files={"out/b": "1\n"})
    gcase("lock_hold_start says so when the holder does not take the lock", 'lock_hold_start 2>/dev/null; echo "$?" >"$OUT/a"', lock_shim=False, files={"out/a": "1\n"},
          setup=lambda tmp, j: [fake_python(tmp, j), open(j("fake", "python3"), "w", newline="\n").write("#!/bin/sh\ncase \"$1\" in */lockhold.py) echo busy; sleep 5 ;; esac\n")])
    if HAVE_FCNTL:
        gcase("lock_hold_start and lock_hold_stop: another process holds the lock, and it is free once the holder is let go of (real POSIX locks)",
              'write_lock_tools "$LIBDIR"; lock_hold_start; lock_state >"$OUT/during"; lock_hold_stop; lock_state >"$OUT/after"', lock_shim=False, files={"out/during": "held\n", "out/after": "free\n"})

    # ---- the manifest tool, on a tree of files ----
    def manifest_run(tree, extra_env=None):
        env = dict(ENV, **(extra_env or {}))
        p = subprocess.run([sys.executable, os.path.join(tmp_lib, "manifest.py"), tree], capture_output=True, text=True, env=env, encoding="utf-8")
        return p

    tmp_lib = tempfile.mkdtemp()
    p_ = run_bash(sandbox(tempfile.mkdtemp())[0], 'write_manifest_tool "' + tmp_lib.replace("\\", "/") + '/manifest.py"')
    check("write_manifest_tool writes the tool", os.path.exists(os.path.join(tmp_lib, "manifest.py")) and p_.returncode == 0, p_.stderr[-300:])

    def rows_of(text):
        out = {}
        for line in text.splitlines():
            f = line.split("\t")
            out[json.loads(f[0]).replace("\\", "/")] = f[1:]
        return out

    with tempfile.TemporaryDirectory() as tree:
        tree = tree.replace("\\", "/")
        os.makedirs(tree + "/sub/deeper")
        for name, text in (("a.txt", "alpha"), ("sub/b.txt", "beta"), ("sub/deeper/c.txt", "gamma")):
            with open(tree + "/" + name, "w") as f:
                f.write(text)
        manifest_run(tree)   # a first look: a file system may bring a directory's time up to date the first time it is read (NTFS does), and the rows are the second look's
        p1 = manifest_run(tree)
        first = rows_of(p1.stdout)
        check("manifest: a row for the tree, every directory and every file, once, with the sha256 of the content", p1.returncode == 0 and sorted(first) == sorted([tree, tree + "/a.txt", tree + "/sub", tree + "/sub/b.txt", tree + "/sub/deeper", tree + "/sub/deeper/c.txt"])
              and first[tree + "/a.txt"][0] == "f" and first[tree + "/a.txt"][-1] == "8ed3f6ad685b959ead7022518e1af76cd816f8e8ec7ccdda1ed4018e8f2223f8" and first[tree + "/sub"][-1] == "-", (p1.stdout, p1.stderr))
        check("manifest: the tool writes nothing in the tree (run twice, the same rows)", manifest_run(tree).stdout == p1.stdout)

        def changes(label, fn, columns):
            before = rows_of(manifest_run(tree).stdout)
            fn()
            after = rows_of(manifest_run(tree).stdout)
            cols = ("type", "size", "mtime", "ctime", "mode", "owner", "digest")
            seen = sorted({(p, c) for p in before.keys() & after.keys() for i, c in enumerate(cols) if before[p][i] != after[p][i]})
            gone, new = sorted(before.keys() - after.keys()), sorted(after.keys() - before.keys())
            check("manifest: " + label, all(all(any(p.endswith(path) and c == col for p, c in seen) for col in cs) for path, cs in columns.get("changed", {}).items())
                  and all(any(p.endswith(x) for p in new) for x in columns.get("created", [])) and all(any(p.endswith(x) for p in gone) for x in columns.get("removed", [])), (seen, new, gone))

        def write(path, text, keep_mtime=False):
            st = os.stat(path) if os.path.exists(path) else None
            with open(path, "w") as f:
                f.write(text)
            if keep_mtime and st is not None:
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        changes("a content that changes with the same size and the same mtime is seen by its hash", lambda: write(tree + "/a.txt", "ALPHA", keep_mtime=True), {"changed": {"a.txt": ["digest"]}})
        changes("a file created is seen", lambda: write(tree + "/sub/new.txt", "n"), {"created": ["sub/new.txt"]})
        changes("a file removed is seen", lambda: os.remove(tree + "/sub/b.txt"), {"removed": ["sub/b.txt"]})
        changes("a size that changes is seen", lambda: write(tree + "/a.txt", "alpha and more"), {"changed": {"a.txt": ["size", "digest"]}})
        os.utime(tree + "/sub/deeper/c.txt", (1_700_000_000, 1_700_000_000))
        changes("an mtime that changes alone is seen", lambda: os.utime(tree + "/sub/deeper/c.txt", (1_700_000_000, 1_700_000_100)), {"changed": {"c.txt": ["mtime"]}})

        def rewrite_same(path):
            """the same content, the same size, the same mtime, a new file in its place (a rename over it): only the ctime says so"""
            st = os.stat(path)
            with open(path + ".new", "w") as f:
                f.write(open(path).read())
            os.utime(path + ".new", ns=(st.st_atime_ns, st.st_mtime_ns))
            os.replace(path + ".new", path)
        changes("a file that is replaced by one of the same content and the same mtime is seen by its ctime", lambda: rewrite_same(tree + "/sub/deeper/c.txt"), {"changed": {"c.txt": ["ctime"]}})
        changes("a mode that changes is seen", lambda: os.chmod(tree + "/a.txt", 0o444), {"changed": {"a.txt": ["mode"]}})
        os.chmod(tree + "/a.txt", 0o644)
        try:
            weird = tree + "/we\tird\nname.txt"
            with open(weird, "w") as f:
                f.write("w")
            rows = rows_of(manifest_run(tree).stdout)
            check("manifest: a name with a tab and a line break in it stays one row, as a JSON string", weird in rows and manifest_run(tree).stdout.count("\n") == len(rows), list(rows))
        except OSError:
            print("skip: this file system has no names with a tab or a line break in them")
        try:
            os.symlink("a.txt", tree + "/link")
            rows = rows_of(manifest_run(tree).stdout)
            check("manifest: a link is a row of its own, with where it points, and is not followed", rows.get(tree + "/link", [""])[0] == "l" and rows[tree + "/link"][-1] == "->a.txt", rows.get(tree + "/link"))
            changes("a link that points elsewhere is seen", lambda: (os.remove(tree + "/link"), os.symlink("sub", tree + "/link")), {"changed": {"link": ["digest"]}})
        except (OSError, NotImplementedError):
            print("skip: no symlinks here: a link is not tested")
        p_ = manifest_run(tree, {"INV_ROOT": tree})
        check("manifest: with INV_ROOT the paths are shown without it (the tests of the guest script look at a tree of their own)", "/a.txt" in rows_of(p_.stdout) and "/" in rows_of(p_.stdout) and not any(k.startswith(tree) for k in rows_of(p_.stdout)), p_.stdout[:300])
        p_ = subprocess.run([sys.executable, os.path.join(tmp_lib, "manifest.py"), tree + "/does-not-exist"], capture_output=True, text=True, env=ENV)
        check("manifest: a tree that is not there is an error, not an empty manifest", p_.returncode != 0 and not p_.stdout, (p_.returncode, p_.stdout, p_.stderr[-200:]))

    # ---- the lock tools: real fcntl on Linux ----
    if HAVE_FCNTL:
        count[0] += 1
        lib = tempfile.mkdtemp().replace("\\", "/")
        run_bash(sandbox(tempfile.mkdtemp())[0], 'write_lock_tools "%s"' % lib)
        lockfile = lib + "/dpkg-lock"
        open(lockfile, "w").close()
        probe = lambda: subprocess.run([sys.executable, lib + "/lockprobe.py", lockfile], capture_output=True, text=True).stdout.strip()
        holder = subprocess.Popen([sys.executable, lib + "/lockhold.py", lockfile, "30"], stdout=subprocess.PIPE, text=True)
        said = holder.stdout.readline().strip()
        during = probe()
        holder.kill()
        holder.wait()
        after = probe()
        missing = subprocess.run([sys.executable, lib + "/lockprobe.py", lib + "/nope"], capture_output=True, text=True).stdout.strip()
        check("the lock tools: a process holds the lock, another sees it held, then free once the holder is gone, and a lock file that is not there is `missing` (nothing is created)",
              (said, during, after, missing) == ("locked", "held", "free", "missing") and not os.path.exists(lib + "/nope"), (said, during, after, missing))
    else:
        print("skip: no fcntl here (Windows): the lock tools are tested on Linux, where the CI runs this file, and by the VM legs")

    # ---- invariance(): the order of what it does ----
    INV = ('inv_snapshot() { echo "snapshot $1" >>"$FAKE/calls"; }; api() { echo "api $1 $2" >>"$FAKE/calls"; }; ready_call() { echo "ready_call $1" >>"$FAKE/calls"; }; marker() { :; }; '
           'lock_hold_start() { echo lock_hold_start >>"$FAKE/calls"; }; lock_hold_stop() { echo lock_hold_stop >>"$FAKE/calls"; }; lock_state() { n=$(cat "$FAKE/n" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" >"$FAKE/n"; echo "lock_state $n" >>"$FAKE/calls"; echo held; }; ')
    gcase("invariance: the control, then the window (snapshot, GET /v1/sys/os, three calls, one under the lock, snapshot), then the clean answer, in that order", INV + 'invariance',
          ordered=["snapshot ctl-a", "api ctl-core-1 /v1/sys/version/current", "api ctl-core-2 /v1/sys/version/current", "snapshot ctl-b", "snapshot before", "api sysos-inv /v1/sys/os", "ready_call ready-inv-1", "ready_call ready-inv-2",
                   "ready_call ready-inv-3", "lock_hold_start", "lock_state 1", "ready_call ready-inv-lock", "lock_state 2", "lock_hold_stop", "snapshot after", "ready_call ready-base"],
          files={"out/lock-probe-during": "held\n", "out/lock-probe-after": "held\n", "out/timer-units.txt": "logrotate.service\n"})
    gcase("invariance: the lock is held for the third call only, and let go of before the second snapshot", INV + 'invariance', ordered=["ready_call ready-inv-3", "lock_hold_start", "ready_call ready-inv-lock", "lock_hold_stop", "snapshot after"])
    gcase("invariance: a lock that is not given back stops the run", INV + 'lock_hold_stop() { return 1; }; invariance', files={"out/aborted": "dpkg's lock was not given back\n"}, no_calls=["snapshot after"], returned=False)
    gcase("inv_snapshot: the files, the units and the sockets, under the tag", 'INV_TREES=("$INV_ROOT/etc"); write_manifest_tool "$LIBDIR/manifest.py"; inv_snapshot t', files={"out/inv-t.tsv": '"/etc"\td*', "out/units-t.tsv": "*", "out/listen-t.txt": "*"})

    # ---- the end to end: the real guest functions, the stand-in core, the real report ----
    E2E = ('write_manifest_tool "$LIBDIR/manifest.py"; write_lock_tools "$LIBDIR"; INV_TREES=("$INV_ROOT/etc" "$INV_ROOT/var/lib/dpkg" "$INV_ROOT/var/lib/apt" "$INV_ROOT/var/cache/apt" "$INV_ROOT/var/lib/casaos" "$INV_ROOT/var/log/casaos"); '
           'HOST_NAME=' + HOST + '; SUDO_USER=casa; guard_run; : >"$OUT/timeline.tsv"; echo "$LEG" >"$OUT/leg"; echo "$PRETTY_NAME" >"$OUT/os"; box_facts; leak_needles; marker prepare_start; invariance; for id in "${STEP_IDS[@]}"; do run_step "$id"; done; date -u +%FT%TZ >"$OUT/finished"')

    def e2e(label, want_code, must_have=(), must_not_have=(), env_extra=None, snippet=E2E, leg="d11", after=None):
        """the real guest functions, the stand-in tools and core, the real report: the report's verdict on what the guest left. after(tmp, j) says what else is wrong with the box"""
        if not wanted(label):
            return
        count[0] += 1
        with tempfile.TemporaryDirectory() as tmp:
            env, j = sandbox(tmp)
            env["LEG"] = leg
            env.update({k: v.replace("{root}", j("root")) for k, v in (env_extra or {}).items()})
            if leg == "u22":
                with open(j("root/etc/os-release"), "w", newline="\n") as f:
                    f.write('PRETTY_NAME="%s"\nID=ubuntu\nVERSION_ID="22.04"\nVERSION_CODENAME=jammy\n' % LEG["u22"]["pretty"])
            sn = snippet.replace("FAMILY=debian", "FAMILY=debian")
            fam = "FAMILY=%s; FROM_CODENAME=%s; TO_CODENAME=%s; " % (LEG[leg]["family"], LEG[leg]["codename"], LEG[leg]["to_codename"])
            p = subprocess.run([BASH, "-c", PRELUDE.replace("FAMILY=debian; FROM_CODENAME=bullseye; TO_CODENAME=bookworm; ", fam) + ("" if HAVE_FCNTL else LOCK_SHIM) + ". \"$OS_RELEASE\"; " + sn, "_", SCRIPT.replace("\\", "/")],
                               capture_output=True, text=True, env=env, timeout=1200, encoding="utf-8", errors="replace")
            out = j("out")
            r = subprocess.run([sys.executable, REPORT, out, leg], capture_output=True, text=True, env=ENV, encoding="utf-8", errors="replace")
            text = r.stdout + r.stderr
            problems = after(tmp, j) if after else []
            if r.returncode != want_code:
                problems.append("the report exits %d, wanted %d" % (r.returncode, want_code))
            problems += ["missing %r" % s for s in must_have if s not in text]
            problems += ["should not say %r" % s for s in must_not_have if s in text]
            if "Traceback" in text:
                problems.append("the report crashed")
            if problems:
                failures.append("end to end, %s: %s\n--- guest stderr\n%s\n--- report\n%s" % (label, "; ".join(problems), p.stderr[-1500:], text[-2500:]))
            else:
                print("ok: end to end, " + label)

    e2e("the real guest functions, a stand-in core, the real report: a healthy Debian 11 leg passes with every verdict", 0, ["# OS readiness proof, d11 leg: PASS"] + ["**PASS** " + v for v in healthy_verdicts("d11")], ["FAIL", "INVALID"])
    e2e("the same on Ubuntu 22.04: every step runs, the alias step with `devel`, and the leg passes with every verdict", 0, ["# OS readiness proof, u22 leg: PASS"] + ["**PASS** " + v for v in healthy_verdicts("u22")], ["FAIL", "INVALID"], leg="u22")
    e2e("a core that writes a file of /etc during the call is caught by the invariance, naming the file", 1, ["**FAIL** " + V_INV_FILES, "/etc/osr-written"], ["Traceback"],
        env_extra={"FAKE_CORE_MODE": "write", "FAKE_CORE_WRITE": "{root}/etc/osr-written"}, snippet=E2E)
    e2e("a core that restarts a service during the call is caught by the invariance", 1, ["**FAIL** " + V_INV_UNITS, "casaos-gateway.service"], ["Traceback"], env_extra={"FAKE_CORE_MODE": "restart", "FAKE_CORE_UNIT": "casaos-gateway.service"})
    e2e("a core that opens a port during the call is caught by the invariance", 1, ["**FAIL** " + V_INV_PORTS, "started listening tcp 0.0.0.0:9999"], ["Traceback"], env_extra={"FAKE_CORE_MODE": "listen"})
    e2e("a core that blocks while another process holds dpkg's lock is caught", 1, ["**FAIL** " + V_LOCK_FAST], ["Traceback"], env_extra={"FAKE_CORE_MODE": "hang"})
    e2e("a core that never says busy is caught under the lock and by both busy steps", 1, ["**FAIL** " + V_LOCK_BUSY, "**FAIL** " + f_fires(STEP_BY_ID["busy_lock"]), "**FAIL** " + f_fires(STEP_BY_ID["busy_unit"])], ["Traceback"],
        env_extra={"FAKE_CORE_MODE": "nobusy"})
    e2e("a core that leaks the box's name in the text is caught", 1, ["**FAIL** " + V_TEXT, HOST], ["Traceback"], env_extra={"FAKE_CORE_MODE": "leak"})
    e2e("a core that has no such route is an invalid run, by name", 1, ["INVALID RUN", "the release under test has no such route"], ["Traceback"], env_extra={"FAKE_CORE_MODE": "route404"})
    e2e("a core that fires a check nobody expected with every dirt is caught step by step", 1, ["**FAIL** " + f_alone(STEP_BY_ID["held"]), "also fires: network_uplink"], ["Traceback"],
        env_extra={"FAKE_CORE_MODE": "extra", "FAKE_CORE_CODE": "network_uplink"})
    e2e("a core that answers 500 is caught as a route that fails", 1, ["**FAIL** " + V_ROUTE], ["Traceback"], env_extra={"FAKE_CORE_MODE": "hang500"})
    e2e("a guest whose Default-Release names a release that no source offers makes apt fail, and the core with it: broken_dpkg, docker_origin and docker_update_pending could not be asked, and the proof says so", 1,
        ["**FAIL** " + f_alone(STEP_BY_ID["sources_pin"]), "also fires: broken_dpkg, docker_origin"], ["Traceback"],
        snippet=E2E.replace("guard_run;", "guard_run; dirty_sources_pin() { mkdir -p \"$APT_DIR/apt.conf.d\" && printf 'APT::Default-Release \"%s\";\\n' \"$TO_CODENAME\" >\"$APT_DIR/apt.conf.d/99osr-default-release\"; };"))
    e2e("a guest whose Docker cannot publish the port it asked for (443 is the gateway's) could not dirty the box: an invalid run, and not a FAIL of the feature", 1, ["INVALID RUN", "step containers_ports_publish: the box could not be dirtied (exit 125)"], ["Traceback"],
        snippet=E2E.replace("guard_run;", "guard_run; dirty_containers_ports_publish() { docker run -d --init --name p-pub53 --restart unless-stopped -p 443:80 nginx:alpine >/dev/null; };"))
    e2e("a guest whose undo fails is an invalid run and not a FAIL of the feature", 1, ["INVALID RUN", "step reboot_first: the undo failed"], ["**FAIL** " + f_undone(STEP_BY_ID["reboot_first"])],
        snippet=E2E.replace("guard_run;", "guard_run; undo_reboot_first() { return 1; }; undone_reboot_first() { return 1; };"))
    e2e("a guest whose dirt does nothing is an invalid run for that step (the dirt could not be made)", 1, ["INVALID RUN", "step held: the box could not be dirtied"], [],
        snippet=E2E.replace("guard_run;", "guard_run; dirty_held() { return 1; };"))
    e2e("a guest that dies in the middle of a step still undoes it, and the report says the run stopped", 1, ["INVALID RUN", "the guest stopped early", "the guest script ended on a failing command (exit 7)"], ["Traceback"],
        snippet=E2E.replace("guard_run;", 'guard_run; ready_call() { if [ "$1" = step-reboot_first-dirty ]; then exit 7; fi; api "$1" /v1/sys/os/readiness 45; };'),
        after=lambda tmp, j: [] if not os.path.exists(j("root/run/reboot-required")) else ["the dirt of the step that was in the middle (the reboot flag) was left on the box"])
else:
    print("skip: no bash that works here, the guest script and the end-to-end cases are not tested")


# ---- the workflow ---------------------------------------------------------------------------------------------------------------------------------------------

def workflow_text():
    with open(WORKFLOW, newline="") as f:
        return f.read().replace("\r\n", "\n")


def workflow_runs(text):
    """{step name: the script of its `run: |` block, without the indentation of the YAML}; the steps of the `plan` job are named plan:<name>"""
    lines = text.split("\n")
    runs, name, i, job = {}, None, 0, None
    while i < len(lines):
        m = re.match(r"^  (\w+):\s*$", lines[i])
        if m:
            job = m.group(1)
        m = re.match(r"^\s*- name: (.*)$", lines[i])
        if m:
            name = m.group(1).strip()
        m = re.match(r"^(\s*)run: \|\s*$", lines[i])
        if m:
            base, block = len(m.group(1)), []
            i += 1
            while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > base):
                block.append(lines[i])
                i += 1
            indent = min(len(b) - len(b.lstrip()) for b in block if b.strip())
            runs[("%s:%s" % (job, name)) if job == "plan" else name] = "\n".join(b[indent:] if b.strip() else "" for b in block).rstrip("\n") + "\n"
            continue
        i += 1
    return runs


if not os.environ.get("REPORT_ONLY"):
    wf = workflow_text()
    wf_runs = workflow_runs(wf)
    check("the workflow has its run steps, each under a name of its own", len(wf_runs) >= 7 and all(wf_runs), sorted(wf_runs))
    check("the workflow is dispatch-only", re.search(r"^on:\n  workflow_dispatch:\n", wf, re.M) is not None and not re.search(r"^  (push|pull_request|schedule):", wf, re.M))
    check("the workflow asks for no permission but to read the repository", re.search(r"^permissions:\n  contents: read\n", wf, re.M) is not None and ": write" not in wf)
    check("no run block has a ${{ }} in it: what the matrix and the inputs say reaches a script through env", all("${{" not in script for script in wf_runs.values()), [n for n, s in wf_runs.items() if "${{" in s])
    check("the inputs reach the job through env and nowhere else", all(re.match(r"^\s+\w+: \$\{\{ (inputs\.\w+|matrix\.\w+|github\.token|needs\.plan\.outputs\.matrix|steps\.plan\.outputs\.matrix) \}\}$", ln) or "${{" not in ln or ln.lstrip().startswith(("name:", "matrix:", "- ", "#"))
                                                                for ln in wf.split("\n")), [ln for ln in wf.split("\n") if "${{" in ln])
    check("no run block of the workflow has a pipeline whose consumer quits before its producer is done", all(not early_quitters(s) for s in wf_runs.values()), {n: early_quitters(s) for n, s in wf_runs.items() if early_quitters(s)})
    check("the workflow runs this harness and syntax-checks the guest script before it boots anything", "python3 scripts/test-os-readiness-proof.py" in wf_runs.get("The proof can say no", "") and "bash -n scripts/os-readiness-proof.sh" in wf_runs.get("The proof can say no", ""))
    if have_bash():
        for step, script in sorted(wf_runs.items()):
            count[0] += 1
            p_ = subprocess.run([BASH, "-n"], input=script, capture_output=True, text=True)
            if p_.returncode != 0:
                failures.append("the run block of %r does not parse as bash: %s" % (step, p_.stderr[-300:]))
            else:
                print("ok: the run block of %r parses as bash" % step)
    prove = wf_runs.get("Prove it", "")
    report_step = wf_runs.get("Report", "")
    check("the guest is run as root, marked disposable, with the installer's options, Docker's version and the leg, and gets the installer and the script", "sudo env READINESS_DISPOSABLE=1" in prove and "INSTALLER_ARGS='${INSTALLER_ARGS}'" in prove
          and "DOCKER_FROM='${DOCKER_FROM}'" in prove and "bash os-readiness-proof.sh ${LEG}" in prove and "scp -F ssh_config published/install.sh scripts/os-readiness-proof.sh vm:" in prove, prove)
    check("the report judges the evidence of the leg and its result is the job's, whatever the earlier steps did", 'python3 scripts/os-readiness-proof-report.py out "${LEG}"' in report_step and re.search(r"- name: Report\n\s+if: always\(\)", wf) is not None and
          "tee -a \"${GITHUB_STEP_SUMMARY}\"" in report_step)
    check("the evidence the guest left is the directory the report is run on", "/home/casa/osr-out" in prove and "/home/casa/osr-out" in report_step)
    check("the tag is the input, and nothing else", "TAG: ${{ inputs.tag }}" in wf and "EVENT" not in wf)
    check("the legs are chosen by the plan job from the file of the legs, which stops a run that matches none", "scripts/os-readiness-proof-legs.json" in wf_runs.get("plan:The legs this run proves", "") and 'raise SystemExit("no leg is' in wf_runs.get("plan:The legs this run proves", ""))
    artifact = re.search(r"^\s+name: (os-readiness-proof-.+)$", wf, re.M)
    check("each leg uploads its evidence under a name of its own", bool(artifact) and "${{ matrix.leg }}" in artifact.group(1))
    check("the job stops a leg that hangs, and the proof step has its own limit", re.search(r"^    timeout-minutes: 75$", wf, re.M) is not None and re.search(r"name: Prove it\n\s+timeout-minutes: \d+", wf) is not None)

    # the legs: what the workflow boots, what the guest knows, what the report judges
    with open(LEGS_JSON, newline="") as f:
        cells = json.loads(f.read())
    with open(os.path.join(HERE, "os-upgrade-measure-legs.json"), newline="") as f:
        measured = json.loads(f.read())
    check("the legs are d11 and u22, the names the guest and the report know", [c["leg"] for c in cells] == ["d11", "u22"] and sorted(LEG) == ["d11", "u22"])
    for c in cells:
        twin = next((m for m in measured if (m["id"], m["version"]) == (c["id"], c["version"])), None)
        check("the image and the sums of %s %s are the ones the OS upgrade measurement boots" % (c["id"], c["version"]), twin is not None and all(c.get(k) == twin.get(k) for k in ("base", "image", "sums", "algo", "args", "docker_from")), (c, twin))
        check("the %s leg is the system the report expects" % c["leg"], (c["id"], c["version"]) == (LEG[c["leg"]]["family"], LEG[c["leg"]]["version"]) and "%s-%s-" % (c["id"], c["version"]) in c["image"])
    d11 = next(c for c in cells if c["leg"] == "d11")
    check("the Debian 11 leg is the owner's box: the archive's security line and Docker 28.0.4", d11.get("args") == "--use-debian-archive" and d11.get("docker_from") == "28.0.4" and not any(c.get("docker_from") or c.get("args") for c in cells if c["leg"] != "d11"))
    if have_bash():
        count[0] += 1
        facts_ok = True
        for c in cells:
            p_ = subprocess.run([BASH, "-c", 'source "$1"; leg_facts "$2"', "_", SCRIPT.replace("\\", "/"), c["leg"]], capture_output=True, text=True)
            facts_ok = facts_ok and p_.stdout.split()[:2] == [LEG[c["leg"]]["family"], LEG[c["leg"]]["version"]] and p_.stdout.split()[2:] == [LEG[c["leg"]]["codename"], LEG[c["leg"]]["to_version"], LEG[c["leg"]]["to_codename"]]
        if facts_ok:
            print("ok: the guest's leg table is the report's")
        else:
            failures.append("the guest's leg_facts and the report's LEGS differ")

    # the plan job, run against the file of the legs
    plan_step = wf_runs.get("plan:The legs this run proves", "")

    def plan_case(label, legs, want_code, want_legs=None):
        if not have_bash() or not wanted(label):
            return
        count[0] += 1
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "scripts"))
            shutil.copy(LEGS_JSON, os.path.join(tmp, "scripts", "os-readiness-proof-legs.json"))
            out = os.path.join(tmp, "output").replace("\\", "/")
            summary = os.path.join(tmp, "summary").replace("\\", "/")
            p_ = subprocess.run([BASH, "-c", plan_step], capture_output=True, text=True, cwd=tmp, env=dict(ENV, LEGS=legs, GITHUB_OUTPUT=out, GITHUB_STEP_SUMMARY=summary))
            got = None
            if os.path.exists(out):
                with open(out) as f:
                    m = re.search(r"^matrix=(.*)$", f.read(), re.M)
                    got = [c["leg"] for c in json.loads(m.group(1))["include"]] if m else None
            if (p_.returncode == 0) != (want_code == 0) or got != want_legs:
                failures.append("the plan for %r: exit %d, legs %r, wanted %r %r\n%s" % (legs, p_.returncode, got, want_code, want_legs, p_.stderr[-300:]))
            else:
                print("ok: " + label)

    plan_case("the plan for `all` is both legs", "all", 0, ["d11", "u22"])
    plan_case("the plan for d11 is the Debian 11 leg alone", "d11", 0, ["d11"])
    plan_case("the plan for u22 is the Ubuntu 22.04 leg alone", "u22", 0, ["u22"])
    plan_case("the plan for a leg that is none stops the run before any machine is booted", "d12", 1, None)

    # fetching the installer: the tag, the pre-release, the checksum
    FAKE_GH = '''gh() {
  echo "gh $*" >>"$FAKE/calls"
  case "$*" in
  *"--json tagName"*) echo "$FAKE_LATEST" ;;
  *"--json isPrerelease"*) if [ -n "$FAKE_MISSING" ]; then echo "release not found" >&2; return 1; fi; echo "$FAKE_PRE" ;;
  "release download "*)
    echo 'echo installed' >published/install.sh
    if [ -n "$FAKE_BAD_SUM" ]; then echo "0000000000000000000000000000000000000000000000000000000000000000  install.sh" >published/install.sh.sha256
    else (cd published && sha256sum install.sh >install.sh.sha256); fi ;;
  esac
}
'''
    fetch_step = wf_runs.get("Fetch the published installer", "")

    def fetch_case(label, tag, want_code=0, latest="v0.5.20", pre="false", bad_sum="", missing="", calls=(), no_calls=(), summary=(), said=()):
        if not have_bash() or not wanted("fetching the installer, " + label):
            return
        count[0] += 1
        with tempfile.TemporaryDirectory() as tmp:
            fake, work = os.path.join(tmp, "fake").replace("\\", "/"), os.path.join(tmp, "work")
            os.mkdir(fake)
            os.mkdir(work)
            env = dict(ENV, TAG=tag, GITHUB_REPOSITORY="ReCasaOS/CasaOS-Install", GITHUB_STEP_SUMMARY=os.path.join(tmp, "summary").replace("\\", "/"), FAKE=fake, FAKE_LATEST=latest, FAKE_PRE=pre, FAKE_BAD_SUM=bad_sum, FAKE_MISSING=missing)
            p_ = subprocess.run([BASH, "-c", FAKE_GH + fetch_step], capture_output=True, text=True, env=env, cwd=work)
            seen = open(os.path.join(fake, "calls")).read() if os.path.exists(os.path.join(fake, "calls")) else ""
            said_summary = open(os.path.join(tmp, "summary")).read() if os.path.exists(os.path.join(tmp, "summary")) else ""
            problems = []
            if (p_.returncode == 0) != (want_code == 0):
                problems.append("exit %d, wanted %s" % (p_.returncode, "0" if want_code == 0 else "a failure"))
            problems += ["missing call %r in %r" % (s, seen) for s in calls if s not in seen]
            problems += ["should not have called %r in %r" % (s, seen) for s in no_calls if s in seen]
            problems += ["the summary lacks %r: %r" % (s, said_summary) for s in summary if s not in said_summary]
            problems += ["did not say %r: %r" % (s, p_.stdout + p_.stderr) for s in said if s not in p_.stdout + p_.stderr]
            if problems:
                failures.append("fetching the installer, %s: %s" % (label, "; ".join(problems)))
            else:
                print("ok: fetching the installer, " + label)

    REPO = "-R ReCasaOS/CasaOS-Install"
    fetch_case("no tag takes the latest release", "", calls=["release view %s --json tagName --jq .tagName" % REPO, "release download v0.5.20 %s" % REPO], summary=["installer release: v0.5.20 (pre-release: false)"])
    fetch_case("a tag takes that release and does not ask which is the latest", "v0.5.21", calls=["release download v0.5.21 %s" % REPO], no_calls=["--json tagName"], summary=["installer release: v0.5.21 (pre-release: false)"])
    fetch_case("a pre-release is a candidate: it is installed, and the summary says so", "v0.5.21-rc.1", pre="true", calls=["release view v0.5.21-rc.1 %s --json isPrerelease" % REPO, "release download v0.5.21-rc.1 %s" % REPO],
               no_calls=["--json tagName"], summary=["installer release: v0.5.21-rc.1 (pre-release: true)"])
    for bad in ("main", "v1", "0.5.21", "v0.5.21; reboot", "v0.5.21 -R other/repo", "v0.5.21\n", "$(reboot)", "../v0.5.21"):
        fetch_case("a tag that is not a release tag (%r) is refused before anything is asked of GitHub" % bad, bad, want_code=1, said=["not a release tag"], no_calls=["gh release view v", "release download"])
    fetch_case("an installer whose checksum is not the published one is not used", "v0.5.21", want_code=1, bad_sum="1", calls=["release download v0.5.21"])
    fetch_case("a tag GitHub has no release for stops the run before anything is downloaded", "v9.9.9", want_code=1, missing="1", calls=["release view v9.9.9"], no_calls=["release download"])

    try:
        import yaml
    except ImportError:
        print("skip: PyYAML is not here, the workflow was read by the plain parser only")
    else:
        doc = yaml.safe_load(wf)
        steps = doc["jobs"]["vm"]["steps"]
        runs_yaml = {s["name"]: s["run"] for s in steps if "run" in s}
        runs_yaml["plan:" + [s for s in doc["jobs"]["plan"]["steps"] if "run" in s][0]["name"]] = [s for s in doc["jobs"]["plan"]["steps"] if "run" in s][0]["run"]
        check("PyYAML reads the workflow, with the run blocks the plain parser found, dispatch-only, the inputs and the matrix of the plan job",
              doc.get("permissions") == {"contents": "read"} and list(doc[True]) == ["workflow_dispatch"] and set(doc[True]["workflow_dispatch"]["inputs"]) == {"tag", "legs"}
              and doc[True]["workflow_dispatch"]["inputs"]["legs"]["options"] == ["all", "d11", "u22"] and runs_yaml == wf_runs and doc["jobs"]["vm"]["strategy"]["matrix"] == "${{ fromJSON(needs.plan.outputs.matrix) }}"
              and doc["jobs"]["vm"]["needs"] == "plan", "the two readings differ")

if failures:
    print("\n".join(failures), file=sys.stderr)
    print("%d of %d cases failed" % (len(failures), count[0]), file=sys.stderr)
    sys.exit(1)
print("ok: the proof can say no (%d cases)" % count[0])
