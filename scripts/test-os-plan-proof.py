#!/usr/bin/env python3
"""The proof has to be able to say no. Each case below builds the directory a leg would leave, in a state that must come out a given way, and checks what
os-plan-proof-report.py says and its exit status:

    python3 scripts/test-os-plan-proof.py

A healthy leg passes. Every verdict has a case in which only the thing it judges is wrong, and that case must fail by that name; a run that did not get as
far as it should (a step that did not run, a call that got no answer of the route, a box that could not be dirtied or put back, evidence that is missing)
must be INVALID and never green. What this guest cannot do is NOT TESTABLE, with its reason, and neither a pass nor a fail.

The core is not here: the answers of the cases are what the real core said, dumped from it (fixtures/plan/*.json, see the README there): the plan of the
measured Debian 11 box, the failures the leg causes, the refusals, the runbook. The cases that need a shape the core never makes edit them. The sources of
the plan are judged against the report's own computation of them, which is checked here against what the core's rewrite did of boxes of six shapes
(fixtures/plan/sources-cases.json), and against the golden of the measured box (hash_current 35d17f59..., hash_generated b5112f83...). The same fixtures, behind
a stand-in for curl, answer the END-TO-END cases, which run the REAL guest functions of os-plan-proof.sh (the control, the window, the plans, the dirty matrix,
with the real manifest tool and the readiness proof's own functions it sources) against stand-ins for systemctl, docker, apt-mark, mount and the rest, and hand
what they wrote to the REAL report: the plumbing between the guest and the report is tested as one, and a core that writes a file during the plan, leaves its
sandbox behind, restarts a unit, opens a port, answers wrongly or does not refuse what it should must come out red by the name of the verdict that catches it.
No VM leg can run here: what the real tools, systemd, apt and Docker say is what the first dispatch of the workflow shows.

REPORT=<path>, PLAN_SCRIPT=<path>, READINESS_SCRIPT=<path>, WORKFLOW=<path> and FIXTURES=<dir> run the cases against other copies, to see that a broken one is
caught. CASES=<regex> runs the cases whose name matches. REPORT_ONLY=1 stops after the cases of the report, GUEST_ONLY=1 skips them. TEST_BASH=<path> names a
bash where plain `bash` is not the one (on Windows: the MSYS bash of Git, C:/Program Files/Git/usr/bin/bash.exe, with a PATH that has no WindowsApps stub of python3).
"""
import ast
import builtins
import concurrent.futures
import contextlib
import copy
import hashlib
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import textwrap
import traceback
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.environ.get("REPORT") or os.path.join(HERE, "os-plan-proof-report.py")
SCRIPT = os.environ.get("PLAN_SCRIPT") or os.path.join(HERE, "os-plan-proof.sh")
READINESS = os.environ.get("READINESS_SCRIPT") or os.path.join(HERE, "os-readiness-proof.sh")
WORKFLOW = os.environ.get("WORKFLOW") or os.path.join(HERE, "..", ".github", "workflows", "os-plan-proof.yml")
LEGS_JSON = os.path.join(HERE, "os-plan-proof-legs.json")
FIXTURES = os.environ.get("FIXTURES") or os.path.join(HERE, "fixtures", "plan")
ONLY = os.environ.get("CASES")
VERDICT = os.environ.get("VERDICT")     # only the cases that expect this verdict by name (a text that their must_have has): what a mutation of one verdict is checked against
BASH = os.environ.get("TEST_BASH") or "bash"
T0 = 1790000000.0
sys.dont_write_bytecode = True      # nothing of this harness leaves a __pycache__ in the worktree: the processes it starts, whatever env they are given, write none either
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
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

def fxj(name):
    """a fixture of the core's own, as a fresh object"""
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def report_steps():
    """the Step(...) rows of the report's STEPS, read from its source without running it: [dict]"""
    with open(REPORT, newline="") as f:
        tree = ast.parse(f.read().replace("\r\n", "\n"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "STEPS" for t in node.targets):
            rows = []
            for call in node.value.elts:
                row = dict(zip(("id", "what", "kind", "code"), [ast.literal_eval(a) for a in call.args]))
                row.update({k.arg: ast.literal_eval(k.value) for k in call.keywords})
                for k, v in (("phase", ""), ("needle", ""), ("kind_in", ()), ("data", {}), ("frag", ""), ("reason", ""), ("file", "")):
                    row.setdefault(k, v)
                rows.append(row)
            return rows
    raise SystemExit("the report has no STEPS")


def pin(id, kind, code, phase="", needle="", kind_in=(), data=None, frag="", reason="", file=""):
    return dict(id=id, kind=kind, code=code, phase=phase, needle=needle, kind_in=kind_in, data=data or {}, frag=frag, reason=reason, file=file)


# The steps of the dirty matrix as LITERALS, in the order the guest runs them. The cases below are made from this table and not from the report's own: a step that the report
# weakens (a kind of failure it no longer asks for, a needle that is gone, a datum it no longer holds) has to be seen, and not lose its cases with it. The report's table is held
# against this one by the first check below.
PINNED_STEPS = [
    pin("format", "job", "sources_format", phase="sources", needle="osp-deb822.sources"),
    pin("cdrom", "job", "sources_cdrom", phase="sources", needle="osp-cdrom.list"),
    pin("alias", "job", "sources_alias", phase="sources", needle="osp-alias.list"),
    pin("target", "job", "sources_target", phase="sources", needle="osp-target.list"),
    pin("mixed", "job", "sources_mixed_file", phase="sources", needle="osp-mixed.list"),
    pin("busy", "refused", "busy"),
    pin("noroom", "refused", "no_room", data={"path": "/var/tmp", "need": 671088640}),
    pin("deadmirror", "job", "sandbox_update_failed", phase="update", needle="ftp9.zz.debian.org", kind_in=("network", "not_found")),
    pin("canarysplice", "canary", "", frag="99osp-canary-splice", reason="not_simple", file="osp-canary-splice"),
    pin("canaryinclude", "canary", "", frag="99osp-canary-include", reason="has_directive", file="osp-canary-include"),
    pin("canarygpgv", "canary", "", frag="99osp-canary-gpgv", reason="not_allowed_setting", file="/var/lib/apt/lists/partial/osp-canary"),
]
STEPS = PINNED_STEPS
STEP_BY_ID = {s["id"]: s for s in STEPS}
FAILED_FIXTURE = {"format": "failed-sources_format", "cdrom": "failed-sources_cdrom", "alias": "failed-sources_alias", "target": "failed-sources_target", "mixed": "failed-sources_mixed_file",
                  "deadmirror": "failed-sandbox_update_failed"}
REFUSED_FIXTURE = {"busy": "refused-busy", "noroom": "refused-no_room"}
check("the steps of the report are exactly the pinned ones (id, kind, code, phase, needle, kinds of a failed update, data, fragment, reason, file), in the order the guest runs them",
      [{k: v for k, v in r.items() if k != "what"} for r in report_steps()] == PINNED_STEPS, [{k: v for k, v in r.items() if k != "what"} for r in report_steps()])
check("every job and refused step has a fixture of the core's own, for the failure it asks for or the refusal",
      all((FAILED_FIXTURE if s["kind"] == "job" else REFUSED_FIXTURE).get(s["id"]) for s in STEPS if s["kind"] != "canary") and
      all(fxj(FAILED_FIXTURE[s["id"]])["data"]["error"]["code"] == s["code"] for s in STEPS if s["kind"] == "job") and
      all(fxj(REFUSED_FIXTURE[s["id"]])["data"]["error_code"] == s["code"] for s in STEPS if s["kind"] == "refused"), "")

BOX_CASES = fxj("sources-cases")
MEASURED_BOX = BOX_CASES[0]
check("the first box of the core's rewrite cases is the measured one, and its hashes are the golden ones of the core's tests (hash_current 35d17f59..., hash_generated b5112f83...)",
      MEASURED_BOX["name"] == "the measured box" and MEASURED_BOX["hash_current"] == "35d17f59fd0705980471e5e52dcf6f54e2292d8b14e245051b88f45539ebcb8a"
      and MEASURED_BOX["hash_generated"] == "b5112f83e29b14e258b0c60275b06ec2c6212a7c0fab519289ac9ba3f2624d0a", MEASURED_BOX["name"])
# the apt.conf.d of the Debian 11 cloud image of the measurement, as the dump made the box: the same texts, so that what the plan left out is what the report computes
APTCONF = {
    "01autoremove": "APT\n{\n  NeverAutoRemove\n  {\n\t\"^firmware-linux.*\";\n  };\n};\n",
    "01autoremove-kernels": "// DO NOT EDIT! File autogenerated by /etc/kernel/postinst.d/apt-auto-removal\nAPT::LastInstalledKernel \"5.10.0-46-cloud-amd64\";\n",
    "20auto-upgrades": "APT::Periodic::Update-Package-Lists \"1\";\nAPT::Periodic::Unattended-Upgrade \"1\";\n",
    "20listchanges": "DPkg::Pre-Install-Pkgs { \"/usr/bin/apt-listchanges --apt || test $? -lt 10\"; };\nDPkg::Tools::Options::/usr/bin/apt-listchanges::Version \"2\";\n",
    "50unattended-upgrades": "// unattended-upgrades\nUnattended-Upgrade::Origins-Pattern {\n        \"origin=Debian,codename=${distro_codename},label=Debian\";\n};\n// This option allows you to control if on a unclean dpkg exit\n",
    "70debconf": "// Pre-configure all packages with debconf before they are installed.\nDPkg::Pre-Install-Pkgs {\"/usr/sbin/dpkg-preconfigure --apt || true\";};\n",
}
LEFT_OUT_OF_THE_IMAGE = [{"file": "01autoremove", "reason": "not_simple"}, {"file": "01autoremove-kernels", "reason": "not_allowed_setting"}, {"file": "20auto-upgrades", "reason": "not_allowed_setting"},
                         {"file": "20listchanges", "reason": "not_simple"}, {"file": "50unattended-upgrades", "reason": "not_simple"}, {"file": "70debconf", "reason": "not_simple"}]
check("the core's own plan of the measured box left out the six fragments that the harness's apt.conf.d has (a brace block, a setting that is not on the allow-list), each for its reason",
      all(fxj(n)["data"]["plan"]["apt_conf_left_out"] == LEFT_OUT_OF_THE_IMAGE for n in ("plan-needs_acceptance", "plan-ok")), "")
check("the core's own plan of a box that has a file of apt's configuration for each reason names all six reasons it leaves one out for, the proxy of one host (a name with dots in it) being kept, the option of gpgv "
      "left out in a fragment whose name has a colon (apt reads it), a fragment with a colon that is kept not named, and a directory of apt.conf.d not named (apt passes over it without a word)",
      {e["reason"] for e in fxj("plan-conf_reasons")["data"]["plan"]["apt_conf_left_out"]} == {"not_simple", "has_directive", "not_allowed_setting", "too_large", "not_regular", "proxy_auto_detect"}
      and "99proxy-host" not in {e["file"] for e in fxj("plan-conf_reasons")["data"]["plan"]["apt_conf_left_out"]}
      and {"file": "96osp:gpgv", "reason": "not_allowed_setting"} in fxj("plan-conf_reasons")["data"]["plan"]["apt_conf_left_out"]
      and not {"95osp:kept", "94subdir"} & {e["file"] for e in fxj("plan-conf_reasons")["data"]["plan"]["apt_conf_left_out"]}, "")
check("the core's own failures that the box's apt configuration can explain (a failed update, a solver that fails, an output that is not understood) carry the plan's list of the files it left out: the same files "
      "and reasons in the same order, and no flag of a cut",
      all(fxj(n)["data"]["error"]["data"].get("apt_conf_left_out") == LEFT_OUT_OF_THE_IMAGE and "apt_conf_left_out_truncated" not in fxj(n)["data"]["error"]["data"]
          and fxj(n)["data"]["error"]["code"] == n[len("failed-"):] for n in ("failed-sandbox_update_failed", "failed-sandbox_solver_failed", "failed-plan_unparsed")), "")
check("the core's own failures of the sources carry no list of the files left out, and no flag of a cut",
      not any(k in (fxj(n)["data"]["error"].get("data") or {}) for n in ("failed-sources_format", "failed-sources_cdrom", "failed-sources_alias", "failed-sources_target", "failed-sources_mixed_file")
              for k in ("apt_conf_left_out", "apt_conf_left_out_truncated")), "")
check("when the sandbox leaves out more files than the plan names (211 here), the plan and the failed update name the same first 200 and say that the list is cut; a plan that is not cut says nothing",
      (lambda p, e: len(p["apt_conf_left_out"]) == 200 and p["apt_conf_left_out_truncated"] is True and e["code"] == "sandbox_update_failed" and e["data"]["apt_conf_left_out"] == p["apt_conf_left_out"]
       and e["data"]["apt_conf_left_out_truncated"] is True)(fxj("plan-conf_truncated")["data"]["plan"], fxj("failed-conf_truncated")["data"]["error"])
      and "apt_conf_left_out_truncated" not in fxj("plan-needs_acceptance")["data"]["plan"], "")
HOLD = "base-files"
LEG = dict(family="debian", version="11", codename="bullseye", pretty="Debian GNU/Linux 11 (bullseye)", to_version="12", to_codename="bookworm")
WITNESSES = ["p-always", "p-unless-stopped", "p-db", "p-web"]
HOST, IPV4, IPV6, MAC, USER = "osp-1a2b3c4d", "10.0.2.15", "fe80::5054:ff:fe12:3456", "52:54:00:12:34:56", "casa"


# ---- the evidence directory a healthy leg leaves ----------------------------------------------------------------------------------------------------------

def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class VDir:
    """a directory of evidence that lives in memory: the cases of the report build hundreds of them, and the report is run on them in this process"""

    def __init__(self):
        self.files = {}


def as_text(x):
    return x.decode("utf-8", errors="replace") if isinstance(x, bytes) else x


def put(d, name, text):
    if isinstance(d, VDir):
        d.files[name] = text
        return
    with open(os.path.join(d, name), "w", newline="") as f:
        f.write(text)


def slurp(d, name):
    if isinstance(d, VDir):
        return as_text(d.files[name]).replace("\r\n", "\n")
    with open(os.path.join(d, name), newline="") as f:
        return f.read().replace("\r\n", "\n")


def exists(d, name):
    return name in d.files if isinstance(d, VDir) else os.path.exists(os.path.join(d, name))


def remove(d, *names):
    for n in names:
        if isinstance(d, VDir):
            d.files.pop(n, None)
            continue
        try:
            os.remove(os.path.join(d, n))
        except OSError:
            pass


HDR = "HTTP/1.1 %d X\r\nContent-Type: application/json; charset=UTF-8\r\nCache-Control: %s\r\nDate: Mon, 12 Oct 2026 09:30:00 GMT\r\nContent-Length: 100\r\n\r\n"


def api(d, name, code=200, body=None, t0=None, secs=0.2, curl=0, err=None, raw=None, cache="no-store", hdr=True):
    """the files of one call the way the guest writes them: body, headers, status, how curl ended, the time before and after (.curl last, as the guest does)"""
    t0 = T0 if t0 is None else t0
    put(d, name + ".t0", "%.6f\n" % t0)
    put(d, name + ".t1", "%.6f\n" % (t0 + secs))
    if raw is not None:
        put(d, name + ".json", raw)
    elif body is not None:
        put(d, name + ".json", json.dumps(body))
    if hdr:
        put(d, name + ".hdr", HDR % (code, cache))
    if err:
        put(d, name + ".err", err)
    put(d, name + ".code", "%03d\n" % code)
    put(d, name + ".curl", "%d\n" % curl)


PHASES = ["sources", "update", "upgrade", "dist-upgrade", "estimate", "parse"]
SECONDS = {"sources": 0.4, "update": 38.2, "upgrade": 6.1, "dist-upgrade": 9.3, "estimate": 5.0, "parse": 0.3}
PHASE_WALL = [("sources", 2), ("update", 40), ("upgrade", 8), ("dist-upgrade", 12), ("estimate", 8), ("parse", 4)]


def plan_get(tpost, wall=75.0, edit=None, fxname="plan-needs_acceptance"):
    """GET of a job that is done, as the core says it, with the times of a job that began at tpost and the numbers of a real box"""
    j = fxj(fxname)
    data = j["data"]
    plan = data["plan"]
    data["started_at"], data["finished_at"] = iso(tpost + 0.3), iso(tpost + wall)
    plan["made_at"] = iso(tpost + wall - 1)
    plan["expires_at"] = data["expires_at"] = iso(tpost + wall - 1 + 86400)
    plan["sandbox"] = {"bytes": 224395264, "seconds_by_phase": dict(SECONDS)}
    if edit:
        edit(plan)
    return j


def poll_rows(t0, wall_phases=PHASE_WALL, step=4.0, tail="done", phase_of_tail="-"):
    rows = []
    t = t0
    for phase, secs in wall_phases:
        for _ in range(max(1, int(secs // step))):
            rows.append("%.6f\t%.6f\t200\t0\trunning\t%s\n" % (t, t + 0.02, phase))
            t += step
    rows.append("%.6f\t%.6f\t200\t0\t%s\t%s\n" % (t, t + 0.02, tail, phase_of_tail))
    return "".join(rows), t + 0.02


SANDBOX_DIR = "/var/tmp/casaos-os-plan.481516"
VAR_TMP = ["/var/tmp/systemd-private-aaaa-systemd-logind.service-bbbb", "/var/tmp/systemd-private-aaaa-systemd-logind.service-bbbb/tmp"]

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
    "/var/lib/apt/lists/deb.debian.org_debian_dists_bullseye_InRelease": ("f", "116", "1790000000000000000", "1790000000000000000", "644", "0:0", "1" * 64),
    "/var/lib/apt/extended_states": ("f", "800", "1790000000000000000", "1790000000000000000", "644", "0:0", "7" * 64),
    "/var/cache/apt": ("d", "4096", "1790000000000000000", "1790000000000000000", "755", "0:0", "-"),
    "/var/cache/apt/pkgcache.bin": ("f", "50000000", "1790000000000000000", "1790000000000000000", "644", "0:0", "2" * 64),
    "/var/lib/casaos": ("d", "4096", "1790000000000000000", "1790000000000000000", "755", "0:0", "-"),
    "/var/lib/casaos/db/casaos.db": ("f", "65536", "1790000200000000000", "1790000200000000000", "644", "0:0", "3" * 64),
    "/var/log/apt": ("d", "4096", "1790000000000000000", "1790000000000000000", "755", "0:0", "-"),
    "/var/log/apt/history.log": ("f", "30000", "1780000000000000000", "1780000000000000000", "644", "0:0", "9" * 64),
    "/var/log/casaos": ("d", "4096", "1790000300000000000", "1790000300000000000", "755", "0:0", "-"),
    "/var/log/casaos/casaos.log": ("f", "20000", "1790000300000000000", "1790000300000000000", "644", "0:0", "4" * 64),
}
STAMP_ROW = ("f", "0", "1780000000000000000", "1780000000000000000", "644", "0:0", "8" * 64)
UNITS = {"casaos.service": ("active", "running", "811", "inv-core"), "casaos-gateway.service": ("active", "running", "812", "inv-gw"),
         "casaos-app-management.service": ("active", "running", "813", "inv-am"), "casaos-message-bus.service": ("active", "running", "814", "inv-mb"),
         "docker.service": ("active", "running", "900", "inv-docker"), "ssh.service": ("active", "running", "700", "inv-ssh"),
         "logrotate.service": ("inactive", "dead", "0", "inv-logrotate"), "systemd-tmpfiles-clean.service": ("inactive", "dead", "0", "inv-tmpfiles")}
PORTS = ["tcp 0.0.0.0:22", "tcp *:80", "tcp *:443", "tcp 127.0.0.1:41873", "tcp 0.0.0.0:18081", "udp 0.0.0.0:68", "tcp 127.0.0.53:53", "udp 127.0.0.53:53"]


def manifest_text(rows):
    return "".join("\t".join([json.dumps(p)] + list(r)) + "\n" for p, r in sorted(rows.items()))


def witnesses_text():
    return "".join("%s\t2026-10-12T08:00:0%dZ\t%d\n" % (n, i, 1000 + i) for i, n in enumerate(WITNESSES))


def build(d, stamp=False):
    """the evidence of a healthy leg: the answers are the core's own (the fixtures), the numbers those of a real box"""
    put(d, "leg", "d11\n")
    put(d, "os", LEG["pretty"] + "\n")
    put(d, "os-release", 'PRETTY_NAME="%s"\nNAME="Debian GNU/Linux"\nVERSION_ID="11"\nVERSION_CODENAME=bullseye\nID=debian\n' % LEG["pretty"])
    put(d, "box-facts", "kernel 5.10.0-46-cloud-amd64\ncpus 4\nmemory_mb 5943\nboot bios\nroot ext4 /dev/vda1\nvirt kvm\ndocker 28.0.4\ndocker_offer installed 5:28.0.4-1~debian.11~bullseye, candidate 5:29.8.0-1~debian.11~bullseye\n"
                        "apt apt 2.2.4 (amd64)\nsmartctl /usr/sbin/smartctl\n")
    put(d, "timeline.tsv", "#\t%.6f\tprepare_start\n#\t%.6f\tcontrol_start\n#\t%.6f\tinvariance_start\n#\t%.6f\tinvariance_done\n" % (T0, T0 + 80, T0 + 100, T0 + 400))
    put(d, "finished", iso(T0 + 3000) + "\n")
    put(d, "timer-units.txt", "logrotate.service\nsystemd-tmpfiles-clean.service\n")
    put(d, "leak-needles.tsv", "hostname\t%s\nipv4\t%s\nipv4\t172.17.0.1\nipv6\t%s\nmac\t%s\nuser\t%s\n" % (HOST, IPV4, IPV6, MAC, USER))
    base = dict(TREE_ROWS)
    if stamp:
        base["/var/lib/apt/periodic/update-success-stamp"] = STAMP_ROW
    after = dict(base)
    after["/var/log/casaos"] = ("d", "4096", "1790000900000000000", "1790000900000000000", "755", "0:0", "-")
    after["/var/log/casaos/casaos.log"] = ("f", "21000", "1790000900000000000", "1790000900000000000", "644", "0:0", "5" * 64)
    after["/var/log/casaos/plan.log"] = ("f", "100", "1790000900000000000", "1790000900000000000", "644", "0:0", "6" * 64)
    for tag, rows in (("ctl-a", base), ("ctl-b", base), ("before", base), ("after", after)):
        put(d, "inv-%s.tsv" % tag, manifest_text(rows))
        put(d, "units-%s.tsv" % tag, "".join("%s\t%s\n" % (u, "\t".join(v)) for u, v in sorted(UNITS.items())))
        put(d, "listen-%s.txt" % tag, "\n".join(sorted(PORTS)) + "\n")
        put(d, "tmp-%s.txt" % tag, "".join(p + "\n" for p in VAR_TMP))
    put(d, "witnesses-before.txt", witnesses_text())
    put(d, "witnesses-after.txt", witnesses_text())
    # what the box's own files are, as the guest copied them
    put(d, "sources.index", "sources.list\nsources.list.d/docker.list\n")
    put(d, "src-main", MEASURED_BOX["files"]["sources.list"])
    put(d, "src-d-docker.list", MEASURED_BOX["files"]["sources.list.d/docker.list"])
    put(d, "aptconf.large", "")
    put(d, "aptconf.nonregular", "")
    put(d, "aptconf.dirs", "")
    put(d, "aptconf.index", "".join(n + "\n" for n in sorted(APTCONF)))
    for n, text in APTCONF.items():
        put(d, "aptconf-d-" + n, text)
    t = [T0 + 110]

    def at(secs=0.2):
        t0 = t[0]
        t[0] += secs + 1.0
        return t0

    # the control's own calls: the core says its bare version as text, outside the envelope
    api(d, "ctl-core-1", raw="0.5.26\n", t0=T0 + 82, hdr=False)
    api(d, "ctl-core-2", raw="0.5.26\n", t0=T0 + 84, hdr=False)
    # the window: before any plan, the POST, the second POST, the watching, the GET that says it is done
    api(d, "plan-idle", body=fxj("idle"), t0=at())
    api(d, "runbook-idle", code=409, body=fxj("no_plan-idle"), t0=at())
    run(d, "p1", t, window=True)
    put(d, "plan-p1.sandbox-seen", SANDBOX_DIR + "\n")
    api(d, "runbook-p1", body=fxj("runbook"), t0=at(0.4))
    put(d, "runbook-p1.shn.tsv", "".join("%d\t%s\t0\n" % (i, b["id"]) for i, b in enumerate(fxj("runbook")["data"]["blocks"])))
    run(d, "p2", t)
    put(d, "plan-p2.sandbox-seen", SANDBOX_DIR + "\n")
    # the hold: a plan that apt cannot carry out (it keeps base-files back), and no runbook for it
    put(d, "step-hold.dirty-rc", "0\n")
    put(d, "step-hold.undo-rc", "0\n")
    put(d, "step-hold.undone", "yes\n")
    run(d, "p3", t, edit=hold_plan)
    put(d, "plan-p3.sandbox-seen", SANDBOX_DIR + "\n")
    api(d, "runbook-p3", code=409, body=fxj("no_plan-blocked"), t0=at())
    put(d, "steps.list", "".join(s["id"] + "\n" for s in STEPS))
    for s in STEPS:
        sid = s["id"]
        put(d, "step-%s.dirty-rc" % sid, "0\n")
        put(d, "step-%s.undo-rc" % sid, "0\n")
        put(d, "step-%s.undone" % sid, "yes\n")
        put(d, "step-%s.leftover" % sid, "")
        if s["kind"] == "job":
            api(d, "step-%s-post" % sid, code=202, body=fxj("accepted"), t0=at())
            api(d, "step-%s-get" % sid, body=fxj(FAILED_FIXTURE[sid]), t0=at())
            api(d, "step-%s-runbook" % sid, code=409, body=fxj("no_plan-failed"), t0=at())
        elif s["kind"] == "canary":
            api(d, "step-%s-post" % sid, code=202, body=fxj("accepted"), t0=at())
            api(d, "step-%s-get" % sid, body=canary_plan(s), t0=at())
            put(d, "step-%s.canary" % sid, "absent\n")
        else:
            api(d, "step-%s-post" % sid, code=409, body=fxj(REFUSED_FIXTURE[sid]), t0=at())
    run(d, "p4", t)
    put(d, "plan-p4.sandbox-seen", SANDBOX_DIR + "\n")


def canary_plan(s, listed=True):
    """GET of the job of a canary step: a plan that is done (the measured box's), with the fragment of the step left out for its reason"""
    j = fxj("plan-needs_acceptance")
    plan = j["data"]["plan"]
    if listed:
        plan["apt_conf_left_out"] = sorted(plan["apt_conf_left_out"] + [{"file": s["frag"], "reason": s["reason"]}], key=lambda e: e["file"])
    return j


def hold_plan(plan):
    plan["plan_id"] = "p1-0123456789abcdef0123456789abcdef"
    plan["holds"] = [HOLD]
    plan["kept_back"] = [HOLD]
    plan["verdict"] = "blocked"
    plan["problems"] = [{"code": "dist_kept_back", "severity": "blocked", "data": {"names": [HOLD]}}] + plan["problems"]
    plan["counts"]["dist_upgrade"]["kept_back"] = 1


def run(d, name, t, window=False, edit=None):
    """a plan: the POST, (the second POST of the window), the watching, the GET that says it is done"""
    tpost = t[0]
    api(d, "plan-%s-post" % name, code=202, body=fxj("accepted"), t0=tpost)
    t[0] += 1.0
    if window:
        api(d, "plan-p1-post-2", body=fxj("already_running"), t0=t[0])
        t[0] += 1.0
    rows, tend = poll_rows(t[0])
    put(d, "plan-%s.polls.tsv" % name, rows)
    api(d, "plan-%s-get" % name, body=plan_get(tpost, wall=tend - tpost, edit=edit), t0=tend)
    t[0] = tend + 2.0


def jedit(d, name, fn):
    j = json.loads(slurp(d, name))
    fn(j)
    put(d, name, json.dumps(j))


def jdata(d, call, fn):
    jedit(d, call + ".json", lambda j: fn(j["data"]))


def jplan(d, run_name, fn):
    jdata(d, "plan-%s-get" % run_name, lambda x: fn(x["plan"]))


def jstatus(d, run_name, fn):
    jdata(d, "plan-%s-get" % run_name, fn)


def ledit(d, name, fn):
    put(d, name, fn(slurp(d, name)))


def replace(d, name, old, new, n=-1):
    def fn(t):
        assert old in t, "%r is not in %s" % (old, name)
        return t.replace(old, new, n)
    ledit(d, name, fn)


# ---- the cases of the report ---------------------------------------------------------------------------------------------------------------------------

VROOT = "/virtual-out"


def run_report(d, leg="d11"):
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
            return io.StringIO() if "b" not in mode else io.BytesIO()
        if n in d.files:
            return io.BytesIO(d.files[n] if isinstance(d.files[n], bytes) else d.files[n].encode("utf-8")) if "b" in mode else io.StringIO(as_text(d.files[n]))
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


def case(name, want_code, must_have=(), must_not_have=(), mutate=None, empty=False, stamp=False, leg="d11"):
    if not wanted(name):
        return
    if VERDICT and not any(VERDICT in s for s in must_have):
        return
    count[0] += 1
    d = VDir()
    if not empty:
        build(d, stamp=stamp)
    if mutate:
        mutate(d)
    code, text = run_report(d, leg)
    problems = []
    if code != want_code:
        problems.append("exit %d, wanted %d" % (code, want_code))
    problems += ["missing %r" % s for s in must_have if s not in text]
    # no case means to have the report crash on its evidence, or leave a verdict that it owes unsaid, unless it says so in what it wants
    must_not_have = list(must_not_have) + [s for s in ("the report could not be made of this evidence", "verdicts that the run owes were not made") if not any(s in h for h in must_have)]
    problems += ["should not say %r" % s for s in must_not_have if s in text]
    if "Traceback" in text:
        problems.append("crashed")
    if os.environ.get("SHOW"):
        print(text)
    if problems:
        failures.append("%s: %s\n%s" % (name, "; ".join(problems), text[-1800:]))
    else:
        print("ok:", name)


def fails(name, verdict, mutate, extra=(), stamp=False):
    """a case in which one thing is wrong and the verdict of that name has to say FAIL (and the harness has nothing to say against the run)"""
    case(name, 1, ["**FAIL** " + verdict] + list(extra), ["INVALID"], mutate=mutate, stamp=stamp)


def invalid(name, reason, mutate, empty=False, silent=(), has=(), lacks=(), stamp=False):
    """a run that proves nothing; the verdicts named in silent must not be made up from the evidence that is not there"""
    case(name, 1, ["INVALID RUN", reason] + list(has), ["**FAIL** " + s for s in silent] + list(lacks), mutate=mutate, empty=empty, stamp=stamp)


V_IDLE = "before any plan: GET answers 200 with plannable true and state idle (the box is one a plan can be made for)"
V_RB_IDLE = "with no plan, the runbook answers 409 no_plan with the reason idle"
V_POST = "POST starts the job: 202, state running, phase sources"
V_POST_TWICE = "a second POST while the job runs answers 200 with already_running true and the running job's state"
V_POLL = "the status answered every poll while the job ran, at once (200, 5 s at most)"
V_PHASES = "the job went through its phases in order (sources, update, upgrade, dist-upgrade, estimate, parse) and ended done within 15 minutes"
V_SANDBOX_SEEN = "while the job ran there was one sandbox directory, casaos-os-plan.<id> in /var/tmp (a second POST made no second one)"
V_SHAPE = "the plan has the contract's shape, no tree of the sources, and counts that agree with its lists"
V_TIMES = "made_at is the time of the job, the plan expires 24 hours later, and the times of the job are in order"
V_VERDICT = "the plan's verdict is ok or needs_acceptance (not blocked)"
V_VERDICT_RULE = "the verdict follows the problems, and every problem is one of the contract's, with its severity and its data"
V_REMOVALS = "no removal is protected or essential, and a protected one is named with its pattern"
V_COUNTS_DIST = "the counts of the full upgrade are within 15 % (rounded up to a whole package) of the measured 384 upgraded, 75 new, 2 removed, 0 kept back"
V_COUNTS_UP = "the counts of the minimal upgrade are within 15 % (rounded up to a whole package) of the measured 274 upgraded, 0 new, 0 removed, 111 kept back"
V_ESTIMATES = "apt's sizes are known and plausible: a download of 100 MB to 2 GB, and some room taken"
V_DISK = "the disk has its three rows (apt's archives, /usr and /), each with what it needs and what it has, and all of them are met"
V_SANDBOX = "the sandbox took less than the 640 MiB that is asked to be free, and each of the six phases has its time"
V_SOURCES = "the sources of the plan are what the leg computes itself from the box's real sources: the lines changed, the files put aside, the dropped lines and both hashes"
V_DOCKER = "the plan sees Docker: present, its source paused, nothing of it on hold"
V_HOOKS = "the plan names the files of apt's configuration it left out, each with its reason, and they are the ones the sandbox's grammar leaves out (a `//` comment, or one statement a line of the allow-list of the proxy, the time to wait, the tries, the languages and the two Install settings, is all it keeps)"
V_ID_STABLE = "two plans made back to back have the same plan_id"
V_ID_HOLD = "a hold on base-files changes the plan_id and is in holds"
V_ID_BACK = "after the dirty matrix and its undos a plan has the same plan_id as the first: the box is as it was"
V_NOSTORE = "every answer of the plan and the runbook routes says Cache-Control: no-store"
V_RB_SHAPE = "the runbook is the plan's: its plan_id and hop, a text of 256 KiB at most, and the nine blocks in order with one point of no return and one restart"
V_RB_SHN = "every block of the runbook is read by sh -n on the guest"
V_RB_TREE = "the runbook writes the plan's tree byte for byte, puts aside the plan's paused files, and checks the plan's hashes of the sources and of what it wrote"
V_RB_BLOCKED = "a plan that is blocked (a hold on base-files) says dist_kept_back and has no runbook: 409 no_plan, reason blocked"
V_STEP_LEFTOVER = "no sandbox directory is left in /var/tmp after the plan of any step, and no plan says it left one behind"
V_STEP_NORUNBOOK = "a job that failed has no runbook: 409 no_plan with the reason failed"
V_INV_FILES = "invariance: the plan created, removed or changed no file of /etc, /var/lib/dpkg, /var/lib/apt, /var/cache/apt or /var/lib/casaos (only /var/log/casaos may differ)"
V_INV_UNITS = "invariance: the plan started, stopped or restarted no service"
V_INV_PORTS = "invariance: the listening ports are the same after the plan"
V_INV_TMP = "invariance: /var/tmp lists the same after the plan: no sandbox directory is left"
V_INV_WITNESS = "invariance: the witness containers ran through the plan untouched (the same start times)"
V_STAMP = "the hooks of the box did not run in the sandbox's update: apt's update-success-stamp is untouched"
V_LEAK = "no answer of the plan and runbook routes says the box's own hostname, address, MAC or user name"
NOT_TESTABLE_NAMES = ["not_root", "hop_not_planned, identity_unsupported", "removal_protected, removal_essential", "plan_unparsed, sandbox_solver_failed, timeout, canceled, internal"]


def f_step(s):
    if s["kind"] == "refused":
        return "step %s: the POST is refused with a 409 and `%s`%s" % (s["id"], s["code"], " with %s" % ", ".join("%s %s" % kv for kv in sorted(s["data"].items())) if s["data"] else "")
    if s["kind"] == "canary":
        return "step %s: the plan leaves %s out of apt's configuration (%s), and what is in it does not run: %s is not made" % (s["id"], s["frag"], s["reason"], s["file"])
    return "step %s: the plan fails in the %s phase with `%s`%s%s" % (s["id"], s["phase"], s["code"], " naming %s" % s["needle"] if s["needle"] else "",
                                                                     " (kind %s)" % " or ".join(s["kind_in"]) if s["kind_in"] else "")


GLOBAL_VERDICTS = [V_IDLE, V_RB_IDLE, V_POST, V_POST_TWICE, V_POLL, V_PHASES, V_SANDBOX_SEEN, V_SHAPE, V_TIMES, V_VERDICT, V_VERDICT_RULE, V_REMOVALS, V_COUNTS_DIST, V_COUNTS_UP, V_ESTIMATES,
                   V_DISK, V_SANDBOX, V_SOURCES, V_DOCKER, V_HOOKS, V_ID_STABLE, V_ID_HOLD, V_ID_BACK, V_NOSTORE, V_RB_SHAPE, V_RB_SHN, V_RB_TREE, V_RB_BLOCKED, V_STEP_LEFTOVER, V_STEP_NORUNBOOK,
                   V_LEAK, V_INV_FILES, V_INV_UNITS, V_INV_PORTS, V_INV_TMP, V_INV_WITNESS]


def healthy_verdicts():
    return list(GLOBAL_VERDICTS) + [f_step(s) for s in STEPS]


if os.environ.get("LIST_VERDICTS"):
    print(json.dumps(sorted(healthy_verdicts() + [V_STAMP])))
    sys.exit(0)


if not os.environ.get("GUEST_ONLY"):
    # ---- a healthy leg ----
    case("a healthy leg passes, and every verdict says so by name", 0,
         ["# OS plan proof, d11 leg: PASS"] + ["**PASS** " + v for v in healthy_verdicts()] + ["**NOT TESTABLE** " + n + ": " for n in NOT_TESTABLE_NAMES + [V_STAMP]],
         ["FAIL", "INVALID", "Traceback"])
    def timestamp_of(d, r):
        return float(slurp(d, "plan-%s-post.t0" % r))

    def ok_plan(d):
        """the plans of the leg are the core's `ok` one (nothing to accept), with the times of each run, and the runbook is for it"""
        ok_id = fxj("plan-ok")["data"]["plan"]["plan_id"]
        old_id = fxj("plan-needs_acceptance")["data"]["plan"]["plan_id"]
        for r in ("p1", "p2", "p3", "p4"):
            t_post = timestamp_of(d, r)
            wall = float(slurp(d, "plan-%s-get.t0" % r)) - t_post
            put(d, "plan-%s-get.json" % r, json.dumps(plan_get(t_post, wall=wall, edit=hold_plan if r == "p3" else None, fxname="plan-ok")))
        put(d, "runbook-p1.json", slurp(d, "runbook-p1.json").replace(old_id, ok_id))
    case("a plan that has nothing to accept (verdict ok) passes the same way", 0, ["**PASS** " + V_VERDICT + ": ok", "**PASS** " + V_VERDICT_RULE, "**PASS** " + V_RB_SHAPE], ["FAIL", "INVALID"], mutate=ok_plan)
    case("the report lists the plans run by run with their numbers, the removals, the seconds by phase and what this leg computes of the sources, independently", 0,
         ["## The plans, run by run", "| p1 | needs_acceptance | p1-5787acb2f81006785a3076312615ae6c | 384 / 75 / 2 / 0 | 274 / 0 / 0 / 111 | 140 MB | 300 MB | 214 MiB |", "Seconds by phase (first plan): sources 0.4, update 38.2",
          "Removals: fuse 2.9.9-5 (manual), libsemanage1 3.1-1+b2.", "Left out of the sandbox's apt configuration: 01autoremove (not_simple), 01autoremove-kernels (not_allowed_setting), 20auto-upgrades (not_allowed_setting), 20listchanges (not_simple), 50unattended-upgrades (not_simple), 70debconf (not_simple).", "hash_current 35d17f59fd0705980471e5e52dcf6f54e2292d8b14e245051b88f45539ebcb8a"],
         ["FAIL"])
    p_ = subprocess.run([sys.executable, REPORT], capture_output=True, text=True, env=ENV)
    check("no argument at all is a usage error and not a crash", p_.returncode == 2 and "usage" in p_.stderr and "Traceback" not in p_.stderr, p_.stderr[-200:])
    case("a leg that is not d11 is an invalid run", 1, ["INVALID RUN", "the leg is d11"], [], empty=True, leg="sideways")

    # ---- before any plan ----
    for what_, mut_ in (("plannable is false (the box is not one a plan can be made for)", lambda x: x.update(plannable=False, reason="identity_unsupported")),
                        ("the state is running before any POST", lambda x: x.update(state="running", phase="sources")),
                        ("the state is failed", lambda x: x.update(state="failed")),
                        ("there is a plan already", lambda x: x.update(plan={})),
                        ("there is an error already", lambda x: x.update(error={"code": "busy"}))):
        fails("GET before any plan: " + what_, V_IDLE, lambda d, m=mut_: jdata(d, "plan-idle", m))
    fails("GET before any plan answers 500", V_IDLE, lambda d: api(d, "plan-idle", code=500, body={"success": 500, "message": "boom"}))
    fails("GET before any plan: success is not the status", V_IDLE, lambda d: jedit(d, "plan-idle.json", lambda j: j.update(success=0)))
    fails("GET before any plan: no message", V_IDLE, lambda d: jedit(d, "plan-idle.json", lambda j: j.pop("message")))
    fails("the runbook with no plan answers 200", V_RB_IDLE, lambda d: api(d, "runbook-idle", body=fxj("runbook")))
    fails("the runbook with no plan: the reason is running", V_RB_IDLE, lambda d: api(d, "runbook-idle", code=409, body=fxj("no_plan-running")))
    fails("the runbook with no plan: another code", V_RB_IDLE, lambda d: jdata(d, "runbook-idle", lambda x: x.update(error_code="no_runbook")))
    fails("the runbook with no plan: the error is not the message", V_RB_IDLE, lambda d: jdata(d, "runbook-idle", lambda x: x.update(error="other words")))
    fails("the runbook with no plan: 500", V_RB_IDLE, lambda d: api(d, "runbook-idle", code=500, body={"success": 500, "message": "boom"}))

    # ---- the routes: a release that has none, a call that left nothing ----
    for call_ in ("plan-idle", "runbook-idle", "plan-p1-post", "plan-p2-get", "step-busy-post"):
        invalid("a 404 on %s: the release has no such route, an invalid run, by name" % call_, "%s got no answer of the route" % call_, lambda d, c=call_: api(d, c, code=404, body={"message": "no"}),
                has=["the release under test has no such route"], silent=[V_IDLE, V_POST])
    for code_ in (0, 401, 403, 502, 503, 504):
        invalid("HTTP %03d on the first call is no answer of the route: INVALID" % code_, "got no answer of the route", lambda d, c=code_: api(d, "plan-idle", code=c, curl=7 if c == 0 else 0, raw=""))
    invalid("a call that left no status", "no answer was recorded for plan-p3-post", lambda d: remove(d, "plan-p3-post.code"))
    invalid("a call that left no record of how curl ended", "plan-idle.curl, .code, .t0 or .t1 is missing", lambda d: remove(d, "plan-idle.curl"))
    invalid("a call that left no time", "plan-idle.curl, .code, .t0 or .t1 is missing", lambda d: remove(d, "plan-idle.t1"))
    for what_, v_ in (("infinite", "inf"), ("not a number (NaN)", "nan"), ("1e999, which reads as infinity", "1e999")):
        invalid("a call whose time is %s is no time" % what_, "plan-idle.curl, .code, .t0 or .t1 is missing or unreadable", lambda d, v=v_: put(d, "plan-idle.t1", v + "\n"))
    invalid("a call that left no headers", "plan-p2-get.hdr is missing", lambda d: remove(d, "plan-p2-get.hdr"), silent=[V_NOSTORE])
    fails("a call that blocked: curl gave up on the second POST", V_POST_TWICE, lambda d: api(d, "plan-p1-post-2", code=0, curl=28, raw="", secs=20.0, err="timed out"), extra=["no usable answer (timeout)"])
    fails("a call that was cut off after the status", V_POST_TWICE, lambda d: api(d, "plan-p1-post-2", curl=56, body=fxj("already_running")), extra=["no usable answer (cut)"])
    fails("an answer that says 200 and is not JSON", V_POST_TWICE, lambda d: api(d, "plan-p1-post-2", raw="<html>gateway</html>"), extra=["no usable answer (unreadable)"])
    invalid("the control's calls of the core that answered 500: the control did not run", "the control of the window did not run", lambda d: api(d, "ctl-core-1", code=500, body={}, hdr=False))
    case("the control answers the bare version as text, as the real core does: it is no INVALID RUN", 0, ["# OS plan proof, d11 leg: PASS"], ["INVALID"], mutate=lambda d: api(d, "ctl-core-1", raw="0.5.24", hdr=False))
    invalid("the control answered 200 and nothing", "ctl-core-2 answered HTTP 200 with no text", lambda d: api(d, "ctl-core-2", raw="", hdr=False))

    # ---- the POST ----
    for what_, mut_ in (("200 and not 202", lambda d: api(d, "plan-p1-post", code=200, body=fxj("accepted"))),
                        ("202 and a state that is done", lambda d: jdata(d, "plan-p1-post", lambda x: x.update(state="done"))),
                        ("202 and a phase that is update", lambda d: jdata(d, "plan-p1-post", lambda x: x.update(phase="update"))),
                        ("202 and no started_at", lambda d: jdata(d, "plan-p1-post", lambda x: x.pop("started_at"))),
                        ("202 and plannable false", lambda d: jdata(d, "plan-p1-post", lambda x: x.update(plannable=False))),
                        ("202 and an envelope that is not one", lambda d: jedit(d, "plan-p1-post.json", lambda j: j.update(data="x"))),
                        ("500", lambda d: api(d, "plan-p1-post", code=500, body={"success": 500, "message": "boom"})),
                        ("a 409 that is not busy", lambda d: api(d, "plan-p1-post", code=409, body=fxj("refused-no_room")))):
        fails("POST: " + what_, V_POST, mut_)
    invalid("POST refused as busy on the clean box: a package manager was at work, an invalid run and no FAIL of the feature", "the first plan was refused as `busy` on the clean box",
            lambda d: api(d, "plan-p1-post", code=409, body=fxj("refused-busy")), silent=[V_POST], has=["says nothing against the feature"], lacks=["**PASS** " + V_SHAPE])
    for what_, mut_ in (("202 and not 200", lambda d: api(d, "plan-p1-post-2", code=202, body=fxj("already_running"))),
                        ("200 and already_running false", lambda d: jdata(d, "plan-p1-post-2", lambda x: x.update(already_running=False))),
                        ("200 and no already_running", lambda d: jdata(d, "plan-p1-post-2", lambda x: x.pop("already_running"))),
                        ("200 and a state that is idle", lambda d: jdata(d, "plan-p1-post-2", lambda x: x.update(state="idle"))),
                        ("200 and a phase that is none of the six", lambda d: jdata(d, "plan-p1-post-2", lambda x: x.update(phase="later"))),
                        ("409", lambda d: api(d, "plan-p1-post-2", code=409, body=fxj("refused-busy")))):
        fails("the second POST: " + what_, V_POST_TWICE, mut_)

    # ---- the watching of the job ----
    def polls(fn):
        return lambda d: ledit(d, "plan-p1.polls.tsv", fn)

    fails("a poll that answered 500", V_POLL, polls(lambda t: t.replace("\t200\t0\trunning\tupdate", "\t500\t0\trunning\tupdate", 1)), extra=["1 of "])
    fails("a poll that curl gave up on", V_POLL, polls(lambda t: t.replace("\t200\t0\trunning\tupdate", "\t000\t28\trunning\tupdate", 1)))
    fails("a poll that took 6 s (the status waits for nothing)", V_POLL, polls(lambda t: re.sub(r"^(\S+)\t\S+(\t200\t0\trunning\tupdate)", lambda m: "%s\t%.6f%s" % (m.group(1), float(m.group(1)) + 6.0, m.group(2)), t, count=1, flags=re.M)))
    case("a poll that took 4.9 s is inside the allowance", 0, ["**PASS** " + V_POLL], ["FAIL"],
         mutate=polls(lambda t: re.sub(r"^(\S+)\t\S+(\t200\t0\trunning\tupdate)", lambda m: "%s\t%.6f%s" % (m.group(1), float(m.group(1)) + 4.9, m.group(2)), t, count=1, flags=re.M)))
    fails("phases out of order (update, then sources)", V_PHASES, polls(lambda t: t.replace("running\tsources", "running\tTMP").replace("running\tupdate", "running\tsources").replace("running\tTMP", "running\tupdate")))
    fails("a phase that is none of the six", V_PHASES, polls(lambda t: t.replace("running\tupgrade", "running\tclean-up", 1)), extra=["a phase that is not one of the six"])
    fails("no poll saw the job running", V_PHASES, polls(lambda t: t.splitlines()[-1] + "\n"), extra=["no poll saw the job running"])
    fails("a poll said done and the watching went on", V_PHASES, polls(lambda t: t + t.splitlines()[-1] + "\n"))
    fails("the job did not end within 15 minutes", V_PHASES, lambda d: put(d, "plan-p1.timeout", "900\n"), extra=["the job did not end within 15 minutes"])

    def failed_job(code, phase, detail, data=None):
        def mut(d):
            ledit(d, "plan-p1.polls.tsv", lambda t: t.replace("\tdone\t-", "\tfailed\t" + phase))
            jstatus(d, "p1", lambda x: (x.update(state="failed", phase=phase, error={"code": code, "phase": phase, "detail": detail, "data": data or {}}), x.pop("plan")))
        return mut
    fails("the job ended failed (sandbox_update_failed, kind not_found): a FAIL of the feature, with what it said", V_PHASES,
          failed_job("sandbox_update_failed", "update", "404 Not Found", {"kind": "not_found"}), extra=["the job ended failed with sandbox_update_failed in update: 404 Not Found"])
    invalid("the job ended failed with a network kind: the mirrors of the target were not reached, an invalid run", "failed in its update for a reason of the network",
            failed_job("sandbox_update_failed", "update", "Could not resolve", {"kind": "network"}), has=["says nothing against the feature"])
    invalid("the job ended failed as busy in its update phase: the box was not quiet", "failed as `busy` in its update phase", failed_job("busy", "update", "another package manager is at work"))
    invalid("no row of the watching at all", "plan-p1.polls.tsv is missing or empty", lambda d: remove(d, "plan-p1.polls.tsv"), silent=[V_POLL, V_PHASES])
    invalid("a row of the watching that is not one", "plan-p1.polls.tsv has a row that is not one of the watching", polls(lambda t: t + "garbage\n"))
    fails("no sandbox directory was seen while the job ran", V_SANDBOX_SEEN, lambda d: put(d, "plan-p1.sandbox-seen", ""))
    fails("two sandbox directories were seen: the second POST made a second job", V_SANDBOX_SEEN, lambda d: put(d, "plan-p1.sandbox-seen", SANDBOX_DIR + "\n/var/tmp/casaos-os-plan.999\n"))
    fails("the directory seen is not named as a sandbox", V_SANDBOX_SEEN, lambda d: put(d, "plan-p1.sandbox-seen", "/var/tmp/osm-sandbox.NHGO69\n"))
    fails("the directory seen is elsewhere than /var/tmp", V_SANDBOX_SEEN, lambda d: put(d, "plan-p1.sandbox-seen", "/tmp/casaos-os-plan.481516\n"))
    invalid("the guest did not look for the sandbox", "plan-p1.sandbox-seen is missing", lambda d: remove(d, "plan-p1.sandbox-seen"))

    # ---- the shape of the plan ----
    def plan_edit(fn):
        return lambda d: jplan(d, "p1", fn)

    shape = [("no plan_id", lambda p: p.pop("plan_id")), ("a plan_id that is not p1- and 32 hex", lambda p: p.update(plan_id="p1-xyz")), ("a plan_id of another version", lambda p: p.update(plan_id="p2-" + "0" * 32)),
             ("another hop", lambda p: p.update(hop="debian-12-13")), ("a made_at that is not a time", lambda p: p.update(made_at="yesterday")), ("a verdict that is none of the three", lambda p: p.update(verdict="fine")),
             ("problems that are not a list", lambda p: p.update(problems={})), ("a problem with no data", lambda p: p.update(problems=[{"code": "removal_manual", "severity": "needs_acceptance"}])),
             ("counts with a boolean in them", lambda p: p["counts"]["upgrade"].update(new=True)), ("counts with a fifth key", lambda p: p["counts"]["upgrade"].update(extra=1)),
             ("counts without the full upgrade", lambda p: p["counts"].pop("dist_upgrade")), ("removals that are not objects", lambda p: p.update(removals=["fuse"])),
             ("a removal with no manual flag", lambda p: p["removals"][0].pop("manual")), ("new_packages that are not strings", lambda p: p.update(new_packages=[1, 2])),
             ("more than 600 new packages", lambda p: p.update(new_packages=["p%d" % i for i in range(601)])), ("a name with a control character", lambda p: p.update(kept_back=["a\nb"])),
             ("sources that are not an object", lambda p: p.update(sources=[])), ("a hash that is not SHA-256", lambda p: p["sources"].update(hash_current="abc")),
             ("a tree of the sources in the plan", lambda p: p["sources"].update(tree=[{"path": "sources.list", "content": "x"}])), ("a file with an action that is none of the three", lambda p: p["sources"]["files"][0].update(action="moved")),
             ("a paused file with no docker flag", lambda p: p["paused"][0].pop("docker")), ("hosts that are not strings", lambda p: p["paused"][0].update(hosts=[1])),
             ("docker that is not an object", lambda p: p.update(docker=[])), ("estimates with no known", lambda p: p["estimates"].pop("known")),
             ("estimates known and no download", lambda p: p["estimates"].pop("download_bytes")), ("a disk row with a text for need", lambda p: p["disk"][0].update(need="much")),
             ("a sandbox with no bytes", lambda p: p["sandbox"].pop("bytes")), ("seconds by phase that are not numbers", lambda p: p["sandbox"].update(seconds_by_phase={"update": "slow"})),
             ("apt_conf_left_out that is not a list", lambda p: p.update(apt_conf_left_out="x")), ("holds that are not strings", lambda p: p.update(holds=[3])),
             ("apt_conf_left_out as a list of names, with no reason", lambda p: p.update(apt_conf_left_out=["20listchanges"])),
             ("an entry of apt_conf_left_out with no reason", lambda p: p["apt_conf_left_out"][0].pop("reason")),
             ("an entry of apt_conf_left_out with a reason that is none of the six", lambda p: p["apt_conf_left_out"][0].update(reason="hooked")),
             ("an entry of apt_conf_left_out with a key more", lambda p: p["apt_conf_left_out"][0].update(why="x")),
             ("an entry of apt_conf_left_out whose file is not a string", lambda p: p["apt_conf_left_out"][0].update(file=7)),
             ("the files left out under apt_hooks_left_out and not under apt_conf_left_out", lambda p: (p.pop("apt_conf_left_out"), p.update(apt_hooks_left_out=["20listchanges"]))),
             ("201 files in apt_conf_left_out (the core names 200 at most)", lambda p: p.update(apt_conf_left_out=[{"file": "f%03d" % i, "reason": "not_simple"} for i in range(201)], apt_conf_left_out_truncated=True)),
             ("apt_conf_left_out that is not in the core's order", lambda p: p["apt_conf_left_out"].reverse()),
             ("apt_conf_left_out_truncated on a list that is not 200 long", lambda p: p.update(apt_conf_left_out_truncated=True)),
             ("apt_conf_left_out_truncated that is false (the core says it only when it is true)", lambda p: p.update(apt_conf_left_out_truncated=False)),
             ("apt_conf_left_out_truncated that is not a boolean", lambda p: p.update(apt_conf_left_out_truncated="yes"))]
    for what_, fn_ in shape:
        fails("shape: " + what_, V_SHAPE, plan_edit(fn_))
    fails("shape: the status is not plannable", V_SHAPE, lambda d: jstatus(d, "p1", lambda x: x.update(plannable=False)))
    fails("shape: the status has no finished_at", V_SHAPE, lambda d: jstatus(d, "p1", lambda x: x.pop("finished_at")))
    fails("shape: the envelope has no message", V_SHAPE, lambda d: jedit(d, "plan-p1-get.json", lambda j: j.pop("message")))
    case("shape: a key the contract does not list is not a failure, and the report says it", 0, ["**PASS** " + V_SHAPE, "Keys of the plan that the contract does not list: surprise."], ["FAIL"], mutate=plan_edit(lambda p: p.update(surprise=1)))
    case("shape: a plan with a list of exactly 600 names, a truncated flag and a pattern on a removal is fine (and a count of 700 new packages is far from the measured 75)", 1, ["**PASS** " + V_SHAPE], ["INVALID", "**FAIL** " + V_SHAPE],
         mutate=plan_edit(lambda p: (p.update(new_packages=["p%03d" % i for i in range(600)], new_truncated=True), p["counts"]["dist_upgrade"].update(new=700), p["removals"][0].update(pattern="x*"))))
    case("a plan whose shape is wrong never crashes the report, and is not judged on what it does not have", 1, ["**FAIL** " + V_SHAPE], ["Traceback", "INVALID"],
         mutate=plan_edit(lambda p: p.update(removals="fuse", counts=[], sources=5, estimates=None, disk={}, sandbox=3, problems=7, docker="x", paused=None, holds=5)))

    # ---- the times ----
    fails("times: expires_at is not 24 hours after made_at", V_TIMES, lambda d: jplan(d, "p1", lambda p: p.update(expires_at=iso(T0 + 100000))))
    fails("times: the status says another expires_at", V_TIMES, lambda d: jstatus(d, "p1", lambda x: x.update(expires_at=iso(T0 + 100000))), extra=["the status says another expires_at than the plan"])
    def made_at(d, shift):
        t = timestamp_of(d, "p1") + shift
        jplan(d, "p1", lambda p: p.update(made_at=iso(t), expires_at=iso(t + 86400)))
        jstatus(d, "p1", lambda x: x.update(expires_at=iso(t + 86400)))
    fails("times: made_at is an hour before the job", V_TIMES, lambda d: made_at(d, -3600), extra=["made_at is not between the start and the end of the job"])
    fails("times: started_at is a minute from the POST", V_TIMES, lambda d: jstatus(d, "p1", lambda x: x.update(started_at=iso(timestamp_of(d, "p1") + 60))), extra=["started_at is"])
    fails("times: finished_at before started_at", V_TIMES, lambda d: jstatus(d, "p1", lambda x: x.update(finished_at=iso(timestamp_of(d, "p1") - 5))))
    fails("times: made_at long after the end of the job", V_TIMES, lambda d: made_at(d, 4000), extra=["made_at is not between the start and the end of the job"])
    fails("times: the job says it finished long after the GET that says it is done", V_TIMES,
          lambda d: jstatus(d, "p1", lambda x: x.update(finished_at=iso(timestamp_of(d, "p1") + 4000))), extra=["made_at is not between the start and the end of the job"])

    # ---- the verdict and the problems ----
    fails("a plan that is blocked (a disk that is short)", V_VERDICT, plan_edit(lambda p: p.update(verdict="blocked", problems=[{"code": "disk_short", "severity": "blocked", "data": {"path": "/usr", "need": 5, "free": 1}}])),
          extra=["blocked; the problems: disk_short"])
    case("a plan that has nothing to accept passes the verdict and the rule", 0, ["**PASS** " + V_VERDICT + ": ok", "**PASS** " + V_VERDICT_RULE], ["FAIL"],
         mutate=plan_edit(lambda p: (p.update(verdict="ok", problems=[]), p["removals"][0].update(manual=False))))
    fails("the verdict is ok with a problem that needs acceptance", V_VERDICT_RULE, plan_edit(lambda p: p.update(verdict="ok")))
    fails("the verdict is needs_acceptance with a problem that blocks", V_VERDICT_RULE, plan_edit(lambda p: p["problems"].append({"code": "removal_protected", "severity": "blocked", "data": {"name": "x"}})))
    fails("the verdict is needs_acceptance with nothing to accept", V_VERDICT_RULE, plan_edit(lambda p: (p.update(problems=[]), p["removals"][0].update(manual=False))))
    fails("a problem that is not one of the contract's", V_VERDICT_RULE, plan_edit(lambda p: p["problems"].append({"code": "mystery", "severity": "info", "data": {}})), extra=["a problem of a code that is not the contract's: mystery"])
    fails("a problem with the wrong severity", V_VERDICT_RULE, plan_edit(lambda p: p["problems"][0].update(severity="info")), extra=["removal_manual has the severity info"])
    fails("a removal installed by hand and no removal_manual for it", V_VERDICT_RULE, plan_edit(lambda p: (p["removals"][1].update(manual=True))), extra=["libsemanage1 was installed by hand and there is no removal_manual for it"])
    fails("a protected removal and no removal_protected (the verdict is not blocked)", V_VERDICT_RULE, plan_edit(lambda p: p["removals"][1].update(protected=True, pattern="lib*")))
    fails("an essential removal and no removal_essential", V_VERDICT_RULE, plan_edit(lambda p: p["removals"][1].update(essential=True)))
    case("a docker_held problem is information: the verdict stays", 0, ["**PASS** " + V_VERDICT_RULE], ["FAIL"],
         mutate=plan_edit(lambda p: p["problems"].append({"code": "docker_held", "severity": "info", "data": {"names": ["docker-ce"]}})))

    # ---- the removals ----
    protected = lambda p: (p["removals"][1].update(protected=True, pattern="lib*"), p.update(verdict="blocked", problems=p["problems"] + [{"code": "removal_protected", "severity": "blocked", "data": {"name": "libsemanage1", "pattern": "lib*"}}]))
    fails("a removal that is protected", V_REMOVALS, plan_edit(protected), extra=["protected or essential: libsemanage1 (lib*)"])
    fails("a removal that is essential", V_REMOVALS, plan_edit(lambda p: p["removals"][1].update(essential=True)), extra=["protected or essential: libsemanage1"])
    fails("a protected removal that is named with no pattern", V_REMOVALS, plan_edit(lambda p: p["removals"][1].update(protected=True)), extra=["libsemanage1 is protected and has no pattern"])
    case("a removal that is not of the measured set is a finding and no failure", 0, ["**PASS** " + V_REMOVALS, "not of the measured set (cron-extra): a finding, not a failure"], ["FAIL"],
         mutate=plan_edit(lambda p: (p["removals"].append({"name": "cron-extra", "version": "1", "manual": False, "protected": False, "essential": False}), p["counts"]["dist_upgrade"].update(removed=3))))
    case("a plan that removes nothing (and so is far from the 2 that the measured box removes)", 1, ["**PASS** " + V_REMOVALS + ": removes nothing", "**FAIL** " + V_COUNTS_DIST], ["INVALID"],
         mutate=plan_edit(lambda p: (p.update(removals=[], verdict="ok", problems=[]), p["counts"]["dist_upgrade"].update(removed=0))))

    # ---- the counts: within 15 % of the measured ones, rounded up ----
    def set_count(p, k, f, v):
        """a count of the plan, and (for the full upgrade) the list that goes with it, which a plan says twice: the names of the new packages, the removals, the packages kept back"""
        p["counts"][k][f] = v
        if k != "dist_upgrade":
            return
        if f == "new":
            p["new_packages"] = ["pkg%03d" % i for i in range(v)]
        elif f == "removed":
            p["removals"] = (p["removals"] + [{"name": "extra%d" % i, "version": "1", "manual": False, "protected": False, "essential": False} for i in range(v)])[:v]
        elif f == "kept_back" and v:
            p["kept_back"] = ["kept%d" % i for i in range(v)]
            p["problems"].append({"code": "dist_kept_back", "severity": "blocked", "data": {"names": p["kept_back"]}})
            p["verdict"] = "blocked"

    for key, field_, bad_, good_ in (("dist_upgrade", "upgraded", 384 - 59, 384 - 58), ("dist_upgrade", "upgraded", 384 + 59, 384 + 58), ("dist_upgrade", "new", 75 + 13, 75 + 12), ("dist_upgrade", "new", 75 - 13, 75 - 12),
                                     ("dist_upgrade", "removed", 4, 3), ("dist_upgrade", "removed", 0, 1), ("dist_upgrade", "kept_back", 1, 0), ("upgrade", "upgraded", 274 + 43, 274 + 42), ("upgrade", "upgraded", 274 - 43, 274 - 42),
                                     ("upgrade", "new", 1, 0), ("upgrade", "removed", 1, 0), ("upgrade", "kept_back", 111 + 18, 111 + 17), ("upgrade", "kept_back", 111 - 18, 111 - 17)):
        name_ = V_COUNTS_DIST if key == "dist_upgrade" else V_COUNTS_UP
        fails("the %s of the %s is %d: more than 15 %% off" % (field_, key, bad_), name_, plan_edit(lambda p, k=key, f=field_, v=bad_: set_count(p, k, f, v)), extra=[field_.replace("_", " ") + " %d (measured" % bad_])
        case("the %s of the %s is %d: inside the 15 %%" % (field_, key, good_), 0, ["**PASS** " + name_], ["FAIL"], mutate=plan_edit(lambda p, k=key, f=field_, v=good_: set_count(p, k, f, v)))

    # ---- the estimates, the disk, the sandbox ----
    fails("apt's sizes are not known", V_ESTIMATES, plan_edit(lambda p: p.update(estimates={"known": False}, disk_note="estimates_unknown")), extra=["estimates.known is false"])
    fails("apt's sizes are not known and the plan does not say it in the word of its own", V_SHAPE, plan_edit(lambda p: p.update(estimates={"known": False})), extra=["estimates.known is false and disk_note is None, not estimates_unknown"])
    fails("apt's sizes are not known and the note says another word", V_SHAPE, plan_edit(lambda p: p.update(estimates={"known": False}, disk_note="later")), extra=["disk_note is later, not estimates_unknown"])
    fails("apt's sizes are known and the plan says they are not", V_SHAPE, plan_edit(lambda p: p.update(disk_note="estimates_unknown")), extra=["estimates.known is true and disk_note says estimates_unknown"])
    fails("a note of the disk that is not a text", V_SHAPE, plan_edit(lambda p: p.update(disk_note=5)), extra=["disk_note is not a text"])
    fails("a download of 99 MB", V_ESTIMATES, plan_edit(lambda p: p["estimates"].update(download_bytes=99 * 10 ** 6)), extra=["a download of 99 MB"])
    case("a download of 100 MB and one of 2 GB are plausible", 0, ["**PASS** " + V_ESTIMATES], ["FAIL"], mutate=plan_edit(lambda p: p["estimates"].update(download_bytes=100 * 10 ** 6)))
    case("a download of 2 GB is plausible", 0, ["**PASS** " + V_ESTIMATES], ["FAIL"], mutate=plan_edit(lambda p: p["estimates"].update(download_bytes=2 * 10 ** 9)))
    fails("a download of 2 GB and a byte", V_ESTIMATES, plan_edit(lambda p: p["estimates"].update(download_bytes=2 * 10 ** 9 + 1)))
    fails("no room taken at all", V_ESTIMATES, plan_edit(lambda p: p["estimates"].update(extra_bytes=0)), extra=["0 MB of room taken"])
    fails("room freed (negative extra)", V_ESTIMATES, plan_edit(lambda p: p["estimates"].update(extra_bytes=-5)))
    fails("8 GB and a byte of room taken", V_ESTIMATES, plan_edit(lambda p: p["estimates"].update(extra_bytes=8 * 10 ** 9 + 1)))
    fails("the disk has two rows", V_DISK, plan_edit(lambda p: p.update(disk=p["disk"][:2])))
    fails("the disk rows are in another order", V_DISK, plan_edit(lambda p: p.update(disk=[p["disk"][1], p["disk"][0], p["disk"][2]])))
    fails("a disk row that is short (ok false)", V_DISK, plan_edit(lambda p: p["disk"][1].update(ok=False, free=1)), extra=["/usr needs"])
    fails("a disk row that says ok and is short", V_DISK, plan_edit(lambda p: p["disk"][1].update(free=1)))
    fails("a disk row that needs nothing", V_DISK, plan_edit(lambda p: p["disk"][2].update(need=0)))
    fails("the sandbox is 640 MiB", V_SANDBOX, plan_edit(lambda p: p["sandbox"].update(bytes=640 << 20)), extra=["the sandbox is 640 MiB"])
    case("the sandbox is 640 MiB less a byte", 0, ["**PASS** " + V_SANDBOX], ["FAIL"], mutate=plan_edit(lambda p: p["sandbox"].update(bytes=(640 << 20) - 1)))
    fails("the sandbox has no size", V_SANDBOX, plan_edit(lambda p: p["sandbox"].update(bytes=0)))
    fails("a phase has no time", V_SANDBOX, plan_edit(lambda p: p["sandbox"]["seconds_by_phase"].pop("estimate")), extra=["the phases with a time are"])
    fails("a seventh phase has a time", V_SANDBOX, plan_edit(lambda p: p["sandbox"]["seconds_by_phase"].update(clean=1.0)))

    # ---- the sources: what this leg computes of the box's real files ----
    def src_text(sources_list=None, docker=None, index=None):
        def mut(d):
            if sources_list is not None:
                put(d, "src-main", sources_list)
            if docker is not None:
                put(d, "src-d-docker.list", docker)
            if index is not None:
                put(d, "sources.index", index)
        return mut

    fails("a rewritten line that is not the one the rewrite gives (the suite is not changed)", V_SOURCES, plan_edit(lambda p: p["sources"]["files"][0]["lines"][0].update(after="deb http://deb.debian.org/debian bullseye main")),
          extra=["the lines of sources.list differ at the 1st one"])
    fails("a line that kept the archive's host", V_SOURCES, plan_edit(lambda p: p["sources"]["files"][0]["lines"][2].update(after="deb http://archive.debian.org/debian-security bookworm-security main")), extra=["at the 3rd one"])
    fails("a line shown with another reason", V_SOURCES, plan_edit(lambda p: p["sources"]["files"][0]["lines"][2].update(why="suite")))
    fails("a line that is missing from what is changed", V_SOURCES, plan_edit(lambda p: p["sources"]["files"][0]["lines"].pop()))
    fails("a line that the plan changed and the box does not have", V_SOURCES, plan_edit(lambda p: p["sources"]["files"][0]["lines"].append({"n": 7, "before": "x", "after": "y", "why": "suite"})))
    fails("a before that is not the box's line", V_SOURCES, plan_edit(lambda p: p["sources"]["files"][0]["lines"][0].update(before="deb http://other.example/debian bullseye main")))
    fails("a file that is not put aside (docker.list is unchanged)", V_SOURCES, plan_edit(lambda p: (p["sources"]["files"][1].update(action="unchanged", lines=[]), p.update(paused=[]))))
    fails("docker.list put aside and not flagged as Docker's", V_SOURCES, plan_edit(lambda p: p["paused"][0].update(docker=False)))
    fails("docker.list put aside with a host that is not scheme and host (a path)", V_SOURCES, plan_edit(lambda p: p["paused"][0].update(hosts=["https://download.docker.com/linux/debian"])))
    fails("another file put aside as well", V_SOURCES, plan_edit(lambda p: p["paused"].append({"file": "other.list", "hosts": [], "docker": False})))
    fails("a file that the box does not have", V_SOURCES, plan_edit(lambda p: p["sources"]["files"].append({"file": "ghost.list", "action": "unchanged", "lines": []})))
    fails("a file of the box that the plan does not say", V_SOURCES, plan_edit(lambda p: p["sources"].update(files=p["sources"]["files"][:1])))
    fails("the backports dropped are counted though there are none", V_SOURCES, plan_edit(lambda p: p["sources"].update(dropped_backports=1)))
    fails("a component added though there is no non-free", V_SOURCES, plan_edit(lambda p: p["sources"].update(firmware_added=1)))
    fails("hash_current that is not the box's", V_SOURCES, plan_edit(lambda p: p["sources"].update(hash_current="0" * 64)), extra=["hash_current is 0000000"])
    fails("hash_generated that is not the tree's", V_SOURCES, plan_edit(lambda p: p["sources"].update(hash_generated="0" * 64)), extra=["hash_generated is 0000000"])
    fails("the box's sources.list has a line more than the plan saw", V_SOURCES, src_text(sources_list=MEASURED_BOX["files"]["sources.list"] + "deb http://deb.debian.org/debian bullseye-backports main\n"))
    fails("the box's docker.list is another file than the plan saw", V_SOURCES, src_text(docker=MEASURED_BOX["files"]["sources.list.d/docker.list"].replace("stable", "test")))
    invalid("no copy of the box's sources: they cannot be checked", "sources.index is missing or empty", lambda d: remove(d, "sources.index"), silent=[V_SOURCES])
    invalid("the copy of docker.list is missing", "the copy of sources.list.d/docker.list is missing", lambda d: remove(d, "src-d-docker.list"), silent=[V_SOURCES])
    invalid("an index line that is not a file of apt's sources", "sources.index has a line that is not a file of apt's sources", lambda d: put(d, "sources.index", "sources.list\n../../etc/passwd\n"), silent=[V_SOURCES])
    invalid("a box whose sources are a host this leg does not compute", "are not of a shape this leg computes", src_text(sources_list="deb http://deb.debian.org/debian bullseye main\ndeb http://mirror.example.com/debian bullseye main\n"), silent=[V_SOURCES], has=["is of the host mirror.example.com"])
    invalid("a box with a line of snapshot.debian.org: it is a frozen copy, not the archive, and the core decides it by its Release file", "are not of a shape this leg computes",
            src_text(sources_list="deb http://snapshot.debian.org/archive/debian/20230101T000000Z bullseye main\n"), silent=[V_SOURCES], has=["is of the host snapshot.debian.org"])
    invalid("a box with a line of people.debian.org", "are not of a shape this leg computes", src_text(sources_list="deb http://deb.debian.org/debian bullseye main\ndeb http://people.debian.org/~x/repo bullseye main\n"), silent=[V_SOURCES],
            has=["is of the host people.debian.org"])
    invalid("a box with a host that has a port", "are not of a shape this leg computes", src_text(sources_list="deb http://deb.debian.org:80/debian bullseye main\n"), silent=[V_SOURCES])
    invalid("a box whose Docker source has a port", "are not of a shape this leg computes", src_text(docker=MEASURED_BOX["files"]["sources.list.d/docker.list"].replace("download.docker.com", "download.docker.com:443")), silent=[V_SOURCES])
    invalid("a box with a deb822 file", "is a deb822 file", lambda d: (src_text(index="sources.list\nsources.list.d/debian.sources\nsources.list.d/docker.list\n")(d), put(d, "src-d-debian.sources", "Types: deb\n")), silent=[V_SOURCES])
    invalid("a box whose line is of a shape this leg does not compute", "is not of the shape `deb|deb-src [options] URI SUITE components`", src_text(sources_list="deb http://deb.debian.org/debian\n"), silent=[V_SOURCES])
    invalid("a box with a line of another suite", "is of the suite buster", src_text(sources_list="deb http://deb.debian.org/debian buster main\n"), silent=[V_SOURCES])
    invalid("a box with a mixed file", "has lines of the distribution and of others", src_text(docker="deb http://deb.debian.org/debian bullseye main\n" + MEASURED_BOX["files"]["sources.list.d/docker.list"]), silent=[V_SOURCES])
    invalid("a box with no line of the distribution", "no line of the distribution is left", src_text(sources_list="# nothing\n"), silent=[V_SOURCES])
    invalid("a box whose file has a carriage return", "has a carriage return", src_text(sources_list="deb http://deb.debian.org/debian bullseye main\r\n"), silent=[V_SOURCES])
    # what this leg does not compute, line by line: a character that is not printable ASCII on a line of code, the forms of address that are not a repository (cdrom, file, copy), a bracket in the components
    for what_, line_ in (("a letter that is not ASCII in the components", "deb http://deb.debian.org/debian bullseye main caf\u00e9\n"), ("a control character after the components", "deb http://deb.debian.org/debian bullseye main\x0b\n"),
                         ("a letter that is not ASCII in the comment after the line", "deb http://deb.debian.org/debian bullseye main # caf\u00e9\n")):
        invalid("a box whose sources.list has %s" % what_, "sources.list line 1 has a character that is not printable ASCII", src_text(sources_list=line_), silent=[V_SOURCES])
    for what_, uri_ in (("cdrom", "cdrom://disc/"), ("file", "file://srv/repo"), ("copy", "copy://srv/repo"), ("FILE, in capital letters", "FILE://srv/repo"), ("Cdrom, with a capital", "Cdrom://disc/")):
        invalid("a box whose line is a %s address" % what_, "is not of the shape `deb|deb-src [options] URI SUITE components`", src_text(sources_list="deb %s bullseye main\n" % uri_), silent=[V_SOURCES])
    for what_, comps_ in (("an opening bracket", "main [x"), ("a closing bracket", "main x]"), ("a pair of brackets", "main [x]"), ("an opening bracket in the middle of a word", "ma[in")):
        invalid("a box whose components have %s" % what_, "is not of the shape `deb|deb-src [options] URI SUITE components`", src_text(sources_list="deb http://deb.debian.org/debian bullseye %s\n" % comps_), silent=[V_SOURCES])

    # ---- Docker, and what is left out of apt's configuration ----
    fails("Docker is not seen", V_DOCKER, plan_edit(lambda p: p["docker"].update(present=False)))
    fails("Docker's source is not paused", V_DOCKER, plan_edit(lambda p: p["docker"].update(source_paused=False)))
    fails("a Docker package is on hold on the clean box", V_DOCKER, plan_edit(lambda p: p["docker"].update(packages_held=["docker-ce"])))

    def left_out(*entries, drop=()):
        """the plan of the first run, and the failed update of the dead mirror (which carries the plan's own list), with these entries more in apt_conf_left_out and the files of `drop` out of it"""
        def listed(lst):
            return sorted([e for e in lst if e["file"] not in drop] + [{"file": f, "reason": r} for f, r in entries], key=lambda e: e["file"])

        def mut(d):
            plan_edit(lambda p: p.update(apt_conf_left_out=listed(p["apt_conf_left_out"])))(d)
            jdata(d, "step-deadmirror-get", lambda x: x["error"]["data"].update(apt_conf_left_out=listed(x["error"]["data"]["apt_conf_left_out"])))
        return mut

    def conf(name, text, *entries, drop=(), index=True):
        """a file of apt.conf.d that the box has (text), and what the plan says is left out of it (entries, and the files of `drop`)"""
        def mut(d):
            if index:
                put(d, "aptconf.index", slurp(d, "aptconf.index") + name + "\n")
            put(d, "aptconf-d-" + name, text)
            left_out(*entries, drop=drop)(d)
        return mut

    fails("a file of apt's configuration that the grammar leaves out is not named", V_HOOKS, left_out(drop=("20listchanges",)), extra=["20listchanges is not named as left out, and the grammar leaves it out (not_simple)"])
    fails("a file that the grammar keeps is named as left out", V_HOOKS, conf("99proxy", 'Acquire::http::Proxy "http://cache.example.lan:3142";\n', ("99proxy", "not_simple")), extra=["99proxy is left out (not_simple), and the grammar keeps it"])
    fails("a file that is left out for a reason that its lines do not give", V_HOOKS, left_out(("01autoremove", "has_directive"), drop=("01autoremove",)), extra=["01autoremove is left out for has_directive, and the grammar says not_simple"])
    fails("a file that is left out for a reason of the contract's six that is not the file's (a comment is not a hook)", V_HOOKS, left_out(("20auto-upgrades", "proxy_auto_detect"), drop=("20auto-upgrades",)))
    fails("nothing is left out", V_HOOKS, plan_edit(lambda p: p.update(apt_conf_left_out=[])), extra=["is not named as left out, and the grammar leaves it out"])
    fails("a file named twice", V_HOOKS, plan_edit(lambda p: p["apt_conf_left_out"].insert(1, dict(p["apt_conf_left_out"][0]))), extra=["01autoremove is named twice"])
    case("the file that every reason is the first of: the cloud image's six, each for its reason, pass", 0, ["**PASS** " + V_HOOKS + ": left out: 01autoremove (not_simple), 01autoremove-kernels (not_allowed_setting)"], ["FAIL"])

    # what is KEPT: blank lines, a // comment, and one statement a line of the kinds that are kept (the core's grammar: aptconf.go)
    KEPT = [("a proxy", 'Acquire::http::Proxy "http://cache.example.lan:3142";\n'), ("a proxy and a comment", '// the proxy of the lab\nAcquire::https::Proxy "DIRECT";\n\n'),
            ("retries and timeouts of a protocol", 'Acquire::Retries "3";\nAcquire::http::Timeout "30";\nAcquire::ForceIPv4 "true";\n'), ("the Install settings", 'APT::Install-Recommends "false";\nAPT::Install-Suggests "0";\n'),
            ("a statement with spaces and tabs around it", '  Acquire::http::Proxy\t"http://x";  \n'), ("only comments and blank lines", '// nothing\n\n  // here either\n'),
            ("a value of exactly 512 characters", 'Acquire::http::Proxy "%s";\n' % ("x" * 512)), ("a name of many parts", 'Acquire::http::Proxy::mirror "DIRECT";\n'),
            ("the proxy of one host (a name with dots in it)", 'Acquire::http::Proxy::deb.debian.org "http://cache.example.lan:3142";\nAcquire::https::Proxy::download.docker.com   "DIRECT" ;\n'),
            ("the proxy of one host, in capital letters, with a dash, an underscore, a digit and a dot at the end", 'ACQUIRE::HTTP::PROXY::My_Host-1.Example.LAN. "http://p:3128";\n'),
            ("every setting of http and https that is kept", "".join('Acquire::%s::%s "1";\n' % (m, s) for m in ("http", "https") for s in ("Proxy", "Timeout", "Pipeline-Depth", "Dl-Limit", "Max-Age", "No-Cache", "AllowRedirect"))),
            ("every setting of ftp that is kept (its program reads the proxy and the time to wait)", 'Acquire::ftp::Proxy "DIRECT";\nAcquire::ftp::Timeout "30";\nacquire::FTP::PROXY "http://x";\n'),
            ("every setting of Acquire:: itself that is kept", "".join('Acquire::%s "x";\n' % s for s in ("Retries", "Languages", "PDiffs", "By-Hash", "Check-Valid-Until", "ForceIPv4", "ForceIPv6", "Queue-Mode"))),
            ("the proxy of one host, for ftp, with the dots of its name", 'Acquire::ftp::Proxy::ftp.de.debian.org "DIRECT";\n'),
            ("an empty value", 'Acquire::http::Proxy "";\n'), ("a file with no end of line", 'Acquire::Languages "none";'),
            ("names in another case (apt reads them in any case)", 'acquire::HTTP::proxy "DIRECT";\napt::INSTALL-recommends "false";\n'), ("lines that end with a carriage return", 'Acquire::Retries "3";\r\n// a comment\r\n'),
            ("a letter that is not ASCII in a comment", '// caf\u00e9\nAcquire::Retries "3";\n'), ("a comment that holds what is not allowed in a statement", '// Acquire { Proxy "x"; }; #include "y"\n'),
            ("a comment with a space before the slashes", '    // Acquire::Retries "3";\n')]
    for what_, text_ in KEPT:
        case("kept by the grammar, and not named: " + what_, 0, ["**PASS** " + V_HOOKS], ["FAIL"], mutate=conf("99kept", text_))
    # what is LEFT OUT whole, and for which reason: the reason of the first line that is not kept
    LEFT = [("an Update hook of an Ubuntu (15update-stamp)", 'APT::Update::Post-Invoke-Success {"touch /var/lib/apt/periodic/update-success-stamp";};\n', "not_simple"),
            ("a hook with the key spliced by a comment", 'APT::Update::Post-In/**/voke-Success {"touch /x";};\n', "not_simple"),
            ("a file that #includes another", '#include "/etc/apt/other.conf";\n', "has_directive"), ("a #clear", '#clear APT::Update;\n', "has_directive"),
            ("a directive with spaces before it", '   \t#include "/x";\n', "has_directive"), ("a directive after good lines", 'Acquire::Retries "3";\n#include "/x";\n', "has_directive"),
            ("a /* */ comment", '/* a comment */\nAcquire::Retries "3";\n', "not_simple"), ("a brace block of Acquire", 'Acquire { Retries "3"; };\n', "not_simple"),
            ("two statements on a line", 'Acquire::Retries "3"; Acquire::http::Timeout "30";\n', "not_simple"), ("two semicolons", 'Acquire::Retries "3";;\n', "not_simple"),
            ("a value with a backslash", 'Acquire::http::Proxy "a\\b";\n', "not_simple"), ("a value of 513 characters", 'Acquire::http::Proxy "%s";\n' % ("x" * 513), "not_simple"),
            ("a value with a quote in it", 'Acquire::http::Proxy "a"b";\n', "not_simple"), ("a value with a tab in it", 'Acquire::http::Proxy "a\tb";\n', "not_simple"),
            ("a control character in a value", 'Acquire::http::Proxy "a\x01b";\n', "not_simple"), ("a letter that is not ASCII in a value", 'Acquire::http::Proxy "caf\u00e9";\n', "not_simple"),
            ("a statement with no semicolon", 'Acquire::Retries "3"\n', "not_simple"), ("a name with no value", 'Acquire::Retries;\n', "not_simple"), ("a name with a brace in it", 'Acquire{x} "3";\n', "not_simple"), ("a value that follows the name with no space", 'Acquire::Retries"3";\n', "not_simple"),
            ("a name with a semicolon in it", 'Acquire::a;b "x";\n', "not_simple"), ("a name with a quote in it", 'Acquire::a"b "x";\n', "not_simple"),
            ("a control character in a comment", '// a\x01b\n', "not_simple"), ("a NUL in a comment", '// a\x00b\n', "not_simple"), ("a DEL in a comment", '// a\x7fb\n', "not_simple"),
            ("bytes that are not UTF-8", b'// a\xffb\nAcquire::Retries "3";\n', "not_simple"),
            ("another setting than Acquire and the two Install ones", 'APT::Periodic::Update-Package-Lists "1";\n', "not_allowed_setting"), ("an Install setting that is not one of the two", 'APT::Install-Pkgs "1";\n', "not_allowed_setting"),
            ("a Dir:: setting that moves the directories of apt", 'Dir::Etc::main "/etc/x";\n', "not_allowed_setting"), ("a setting of dpkg", 'DPkg::Options "--force-confold";\n', "not_allowed_setting"),
            ("a host whose name has two dots in a row", 'Acquire::http::Proxy::deb..debian.org "DIRECT";\n', "not_allowed_setting"), ("a segment that is two dots", 'Acquire::http::Proxy::.. "DIRECT";\n', "not_allowed_setting"),
            ("a segment that is one dot", 'Acquire::http::Proxy::. "x";\n', "not_allowed_setting"),
            ("a host with a lone colon in it", 'Acquire::http::Proxy::a:b "x";\n', "not_allowed_setting"), ("a name that ends with the separator", 'Acquire::http::Proxy:: "x";\n', "not_allowed_setting"),
            ("three colons", 'Acquire::http:::Proxy "x";\n', "not_allowed_setting"),
            ("a host with a letter that folds to an ASCII one (the Kelvin sign)", 'Acquire::http::Proxy::\u212a.example "x";\n', "not_allowed_setting"),
            ("an Install setting with a dot after it", 'APT::Install-Recommends.x "1";\n', "not_allowed_setting"), ("an Install setting with the long s for a letter", 'APT::Install-Recommend\u017f "1";\n', "not_allowed_setting"),
            ("Acquire in the middle of a name", 'Foo::Acquire::http::Proxy "x";\n', "not_allowed_setting"), ("an Acquire name with an empty part", 'Acquire::::x "y";\n', "not_allowed_setting"),
            ("Acquire alone", 'Acquire "y";\n', "not_allowed_setting"), ("a name that is not ASCII", 'Acquire::h\u00e9::x "y";\n', "not_allowed_setting"),
            ("a proxy that runs a script", 'Acquire::http::Proxy-Auto-Detect "/usr/bin/auto-apt-proxy";\n', "proxy_auto_detect"),
            ("the other spelling of the proxy that runs a script", 'Acquire::https::ProxyAutoDetect "/usr/bin/x";\n', "proxy_auto_detect"),
            ("the proxy that runs a script, in capital letters", 'ACQUIRE::HTTP::PROXY-AUTO-DETECT "/usr/bin/x";\n', "proxy_auto_detect"),
            ("the proxy that runs a script, with a part after it", 'Acquire::http::Proxy-Auto-Detect::deb "/usr/bin/x";\n', "proxy_auto_detect"),
            ("the proxy that runs a script, for a host", 'Acquire::http::Proxy-Auto-Detect::deb.debian.org "/usr/bin/x";\n', "proxy_auto_detect"),
            ("the proxy that runs a script, in the name of a host", 'Acquire::http::Proxy::deb.Proxy-Auto-Detect.org "/usr/bin/x";\n', "proxy_auto_detect"),
            ("the other spelling of the proxy that runs a script, for a host", 'Acquire::https::ProxyAutoDetect::a.b "/usr/bin/x";\n', "proxy_auto_detect"),
            ("the proxy that runs a script, in the middle of the name", 'Acquire::http::proxyautodetect::x "/usr/bin/x";\n', "proxy_auto_detect"),
            ("the proxy that runs a script, with the letter that folds to an i (a capital I with a dot) in Acquire", 'Acqu\u0130re::http::Proxy-Auto-Detect "/usr/bin/x";\n', "proxy_auto_detect"),
            ("a dot in the first segment and in the last: not a method, and four parts", 'Acquire::v1.5::Foo.Bar "1";\n', "not_allowed_setting"),
            ("four parts: a method, a setting, a host and one more", 'Acquire::http::Proxy::a.b::c.d "1";\n', "not_allowed_setting"),
            # an open Acquire:: would let through what apt's helpers read and run, as the user _apt (Acquire::gpgv::Options::0 "--log-file=..." made gpgv write that file wherever it was told)
            ("the option of gpgv, that makes it write a file", 'Acquire::gpgv::Options::0 "--log-file=/var/lib/apt/lists/partial/osp-canary";\n', "not_allowed_setting"),
            ("the option of gpgv, in capital letters", 'ACQUIRE::GPGV::OPTIONS::0 "--log-file=/var/tmp/x";\n', "not_allowed_setting"),
            ("the option of gpgv with no number", 'Acquire::gpgv::Options "--log-file=/var/tmp/x";\n', "not_allowed_setting"),
            ("the options of a method that is on the list", 'Acquire::http::Options "-x";\n', "not_allowed_setting"),
            ("the method of a method, the program that runs", 'Acquire::http::Method "/bin/sh";\n', "not_allowed_setting"),
            ("a command of a method", 'Acquire::https::Command "/bin/sh";\n', "not_allowed_setting"),
            ("the sandbox user that apt drops to", 'APT::Sandbox::User "root";\n', "not_allowed_setting"),
            ("the sandbox of the methods", 'Acquire::Sandbox::User "root";\n', "not_allowed_setting"),
            ("the program that gets the changelogs", 'Acquire::Changelogs::URI::Origin::Debian "http://x";\n', "not_allowed_setting"),
            ("the types of compression, and a program for one", 'Acquire::CompressionTypes::foo "foo";\nAcquire::CompressionTypes::foo::Comp-Type "/bin/sh";\n', "not_allowed_setting"),
            ("a method that is not on the list", 'Acquire::file::Proxy "x";\n', "not_allowed_setting"), ("a method of the list with a setting that it has not (Retries)", 'Acquire::http::Retries "3";\n', "not_allowed_setting"),
            ("a setting of Acquire:: itself that is a method's (Timeout)", 'Acquire::Timeout "3";\n', "not_allowed_setting"), ("a setting of Acquire:: with a part after it", 'Acquire::Retries::0 "3";\n', "not_allowed_setting"),
            ("a setting of a method with a host that has no host (Timeout)", 'Acquire::http::Timeout::deb.debian.org "3";\n', "not_allowed_setting"),
            ("a setting of one of the two Install ones with a part after it", 'APT::Install-Recommends::0 "1";\n', "not_allowed_setting"),
            ("the log of the planner that apt writes in /var/log/apt", 'Dir::Log::Planner "/var/log/apt/x";\n', "not_allowed_setting"),
            ("a setting that is cased as one on the list but has a letter that is not ASCII (the Kelvin sign)", 'Acquire::http::Pro\u212axy "x";\n', "not_allowed_setting")]
    # the program of the ftp method reads the proxy and the time to wait and none of the five others that the http program reads: they do nothing for it, and are not on its list
    LEFT += [("a setting of the http program that the ftp program does not read (%s)" % s_, 'Acquire::ftp::%s "1";\n' % s_, "not_allowed_setting") for s_ in ("Pipeline-Depth", "Dl-Limit", "Max-Age", "No-Cache", "AllowRedirect")]
    LEFT += [("the same, in capital letters", 'ACQUIRE::FTP::PIPELINE-DEPTH "1";\n', "not_allowed_setting"), ("a setting of ftp that has no host (Timeout)", 'Acquire::ftp::Timeout::ftp.de.debian.org "3";\n', "not_allowed_setting"),
             ("the http setting of one host that ftp does not read", 'Acquire::ftp::Pipeline-Depth::ftp.de.debian.org "3";\n', "not_allowed_setting")]
    for what_, text_, reason_ in LEFT:
        case("left out whole, for %s: %s" % (reason_, what_), 0, ["**PASS** " + V_HOOKS + ": left out: 01autoremove (not_simple)", "99left (%s)" % reason_], ["FAIL"], mutate=conf("99left", text_, ("99left", reason_)))
        fails("not named when it has %s" % what_, V_HOOKS, conf("99left", text_), extra=["99left is not named as left out, and the grammar leaves it out (%s)" % reason_])
    # the first line that is not kept says why
    TWO = ['Acquire { Retries "3"; };\nAPT::Periodic::Enable "1";\n', 'APT::Periodic::Enable "1";\nAcquire { Retries "3"; };\n', 'Acquire { Retries "3"; };\n#include "/x";\n', '#include "/x";\nAcquire { Retries "3"; };\n']
    for text_, first_, other_ in ((TWO[0], "not_simple", "not_allowed_setting"), (TWO[1], "not_allowed_setting", "not_simple"), (TWO[2], "not_simple", "has_directive"), (TWO[3], "has_directive", "not_simple")):
        case("a file with two kinds of line is left out for the reason of the first: %s" % first_, 0, ["**PASS** " + V_HOOKS], ["FAIL"], mutate=conf("99two", text_, ("99two", first_)))
        fails("a file with two kinds of line, the reason of the second (%s) and not of the first (%s)" % (other_, first_), V_HOOKS, conf("99two", text_, ("99two", other_)), extra=["99two is left out for %s, and the grammar says %s" % (other_, first_)])
    case("a file too large is left out for that before anything else is read of it (a directive in it too)", 0, ["**PASS** " + V_HOOKS], ["FAIL"],
         mutate=conf("99big", "#include \"/x\"\n" + "// " + "x" * (64 * 1024), ("99big", "too_large")))
    case("a name that apt does not read (a backup file, a hidden file, a .dpkg-old) is neither read nor left out", 0, ["**PASS** " + V_HOOKS], ["FAIL"],
         mutate=lambda d: (put(d, "aptconf.index", slurp(d, "aptconf.index") + "99proxy~\n.hidden\n20listchanges.dpkg-old\n20listchanges.bak\n"), ))
    case("a name with the extension conf is read, and one with another extension is not", 0, ["**PASS** " + V_HOOKS], ["FAIL"], mutate=conf("99a.conf", "// invoke\nAPT::X::Y 1;\n", ("99a.conf", "not_simple")))
    case("a fragment that is too large (64 KiB and a byte) is left out and named", 0, ["**PASS** " + V_HOOKS], ["FAIL"],
         mutate=lambda d: (put(d, "aptconf-d-01autoremove", "// " + "x" * (64 * 1024 - 2)), left_out(("01autoremove", "too_large"), drop=("01autoremove",))(d)))
    case("a fragment of exactly 64 KiB of comment is kept (the other six are left out)", 0, ["**PASS** " + V_HOOKS], ["FAIL"],
         mutate=lambda d: (put(d, "aptconf-d-01autoremove", "// " + "x" * (64 * 1024 - 4) + "\n"), left_out(drop=("01autoremove",))(d)))
    case("a fragment that the guest says is large is left out and named", 0, ["**PASS** " + V_HOOKS], ["FAIL"],
         mutate=lambda d: (put(d, "aptconf.large", "99big\n"), put(d, "aptconf.index", slurp(d, "aptconf.index") + "99big\n"), left_out(("99big", "too_large"))(d)))
    case("a fragment that is a link (or a fifo, or a device) is left out, for not_regular, and not read", 0, ["**PASS** " + V_HOOKS, "99link (not_regular)"], ["FAIL"],
         mutate=lambda d: (put(d, "aptconf.nonregular", "99link\n"), put(d, "aptconf.index", slurp(d, "aptconf.index") + "99link\n"), left_out(("99link", "not_regular"))(d)))
    fails("a link that is left out for another reason than not_regular", V_HOOKS, lambda d: (put(d, "aptconf.nonregular", "99link\n"), put(d, "aptconf.index", slurp(d, "aptconf.index") + "99link\n"), left_out(("99link", "not_simple"))(d)))
    case("an apt.conf with a hook is left out and named `apt.conf`", 0, ["**PASS** " + V_HOOKS, "apt.conf (not_simple)"], ["FAIL"],
         mutate=lambda d: (put(d, "aptconf-main", 'APT::Update::Pre-Invoke {"true";};\n'), left_out(("apt.conf", "not_simple"))(d)))
    case("an apt.conf that is a link is left out for not_regular", 0, ["**PASS** " + V_HOOKS, "apt.conf (not_regular)"], ["FAIL"], mutate=lambda d: (put(d, "aptconf.nonregular", "apt.conf\n"), left_out(("apt.conf", "not_regular"))(d)))
    case("an apt.conf that is too large is left out for too_large", 0, ["**PASS** " + V_HOOKS, "apt.conf (too_large)"], ["FAIL"],
         mutate=lambda d: (put(d, "aptconf-main", "// " + "x" * (64 * 1024)), left_out(("apt.conf", "too_large"))(d)))
    case("an apt.conf with a proxy is kept", 0, ["**PASS** " + V_HOOKS], ["FAIL"], mutate=lambda d: put(d, "aptconf-main", 'Acquire::http::Proxy "http://cache.example.lan:3142";\n'))
    fails("an apt.conf with a hook that the plan does not say it left out", V_HOOKS, lambda d: put(d, "aptconf-main", 'APT::Update::Pre-Invoke {"true";};\n'), extra=["apt.conf is not named as left out"])
    # a directory of apt.conf.d: apt passes over it without a word, and so does the plan (it is one of the 200 that are read, see below); an apt.conf that is a directory is not a file: not_regular
    case("a directory in apt.conf.d is neither read nor named", 0, ["**PASS** " + V_HOOKS], ["FAIL"], mutate=lambda d: (put(d, "aptconf.dirs", "99dir\n"), put(d, "aptconf.index", slurp(d, "aptconf.index") + "99dir\n")))
    fails("a directory in apt.conf.d that the plan names as not regular", V_HOOKS, lambda d: (put(d, "aptconf.dirs", "99dir\n"), put(d, "aptconf.index", slurp(d, "aptconf.index") + "99dir\n"), left_out(("99dir", "not_regular"))(d)),
          extra=["99dir is left out (not_regular), and the grammar keeps it"])
    case("a directory that has the name of a fragment of the image is no fragment: nothing is read of it, and nothing is named", 0, ["**PASS** " + V_HOOKS], ["FAIL"],
         mutate=lambda d: (put(d, "aptconf.dirs", "01autoremove\n"), remove(d, "aptconf-d-01autoremove"), left_out(drop=("01autoremove",))(d)))
    # a name with a colon is read by apt 2.2.4 (and so by the core, which asks apt itself: aptFragmentName), and one that starts with a colon too
    case("a fragment whose name has a colon is read: the plan names it for its first line", 0, ["**PASS** " + V_HOOKS, "99a:b (not_allowed_setting)"], ["FAIL"], mutate=conf("99a:b", 'Dir::Cache "/tmp";\n', ("99a:b", "not_allowed_setting")))
    fails("a fragment whose name has a colon, left out, and not named", V_HOOKS, conf("99a:b", 'Dir::Cache "/tmp";\n'), extra=["99a:b is not named as left out, and the grammar leaves it out (not_allowed_setting)"])
    case("a fragment whose name has a colon and the extension conf is read", 0, ["**PASS** " + V_HOOKS, "99a:b.conf (not_simple)"], ["FAIL"], mutate=conf("99a:b.conf", "Acquire {};\n", ("99a:b.conf", "not_simple")))
    case("a fragment whose name starts with a colon is read", 0, ["**PASS** " + V_HOOKS, ":lead (has_directive)"], ["FAIL"], mutate=conf(":lead", "#clear APT;\n", (":lead", "has_directive")))
    case("a fragment whose name has a colon and a kept setting is kept, and not named", 0, ["**PASS** " + V_HOOKS], ["FAIL"], mutate=conf("95a:kept", 'Acquire::Languages "none";\n'))
    case("a name with a colon and another extension is not read", 0, ["**PASS** " + V_HOOKS], ["FAIL"], mutate=conf("99a:b.bak", 'Dir::Cache "/tmp";\n'))
    case("a name that starts with a dot is not read, whatever its extension (.hidden.conf, .conf)", 0, ["**PASS** " + V_HOOKS], ["FAIL"],
         mutate=lambda d: (conf(".hidden.conf", 'Dir::Cache "/tmp";\n')(d), conf(".conf", 'Dir::Cache "/tmp";\n')(d)))
    invalid("no copy of apt's configuration", "aptconf.index is missing", lambda d: remove(d, "aptconf.index"), silent=[V_HOOKS])
    invalid("a fragment that the index names and the guest did not copy", "the copy of apt.conf.d/70debconf is missing", lambda d: remove(d, "aptconf-d-70debconf"), silent=[V_HOOKS])

    # a plan, and a failure, name the first 200 of the files that the sandbox left out (by name), and say that the list is cut: apt_conf_left_out_truncated, true, there only then (the core's MaxLeftOut)
    SIX_ = [(e["file"], e["reason"]) for e in LEFT_OUT_OF_THE_IMAGE]
    EXTRA_ = ["b%03d" % i for i in range(1, 195)]
    LEFT_200 = sorted(SIX_ + [(n, "not_simple") for n in EXTRA_])        # 200 fragments, all left out
    LEFT_201 = sorted(LEFT_200 + [("apt.conf", "not_simple")])           # and the apt.conf too: one more than the plan names

    def box_left_out(with_main):
        """the guest's box has 194 fragments more that the grammar leaves out (200 in all), and an apt.conf that it leaves out as well when with_main"""
        def mut(d):
            put(d, "aptconf.index", slurp(d, "aptconf.index") + "".join(n + "\n" for n in EXTRA_))
            for n in EXTRA_:
                put(d, "aptconf-d-" + n, "Acquire { };\n")
            if with_main:
                put(d, "aptconf-main", 'APT::Update::Pre-Invoke {"true";};\n')
        return mut

    def names_left_out(entries, cut=None):
        """the plan of the first run names these files as left out, and says the list is cut (True), is not (False), or says nothing (None)"""
        def fn(p):
            p.update(apt_conf_left_out=[{"file": f, "reason": r} for f, r in entries])
            p.pop("apt_conf_left_out_truncated", None)
            if cut is not None:
                p["apt_conf_left_out_truncated"] = cut
        return plan_edit(fn)

    DEAD_ = STEP_BY_ID["deadmirror"]
    DEAD_NAME_, DEAD_GET_ = f_step(DEAD_), "step-deadmirror-get"

    def dead_says(entries, cut=None):
        """the failed update of the dead mirror carries these files as left out, and says the list is cut (True) or says nothing (None)"""
        def fn(x):
            x["error"]["data"].update(apt_conf_left_out=[{"file": f, "reason": r} for f, r in entries])
            x["error"]["data"].pop("apt_conf_left_out_truncated", None)
            if cut is not None:
                x["error"]["data"]["apt_conf_left_out_truncated"] = cut
        return lambda d: jdata(d, DEAD_GET_, fn)
    case("a box that leaves out exactly 200 files: the plan names them all, and does not say it is cut", 0, ["**PASS** " + V_HOOKS, "**PASS** " + V_SHAPE], ["FAIL"],
         mutate=lambda d: (box_left_out(False)(d), names_left_out(LEFT_200)(d), dead_says(LEFT_200)(d)))
    fails("a box that leaves out exactly 200 files, and a plan that says its list is cut", V_HOOKS, lambda d: (box_left_out(False)(d), names_left_out(LEFT_200, True)(d)),
          extra=["apt_conf_left_out_truncated is there, and the grammar leaves out 200 files"])
    case("a box that leaves out 201 files: the plan names the first 200 by name and says that the list is cut (and the flag is a key of the contract's, not one the report calls new)", 0,
         ["**PASS** " + V_HOOKS, "**PASS** " + V_SHAPE, "(the list is cut at 200, of 201)", "Keys of the plan that the contract does not list: none."], ["FAIL"],
         mutate=lambda d: (box_left_out(True)(d), names_left_out(LEFT_201[:200], True)(d), dead_says(LEFT_201[:200], True)(d)))
    fails("a box that leaves out 201 files, and a plan that cuts the list and does not say so", V_HOOKS, lambda d: (box_left_out(True)(d), names_left_out(LEFT_201[:200])(d)),
          extra=["apt_conf_left_out_truncated is not there, and the grammar leaves out 201 files"])
    fails("a box that leaves out 201 files, and a plan that names them all", V_SHAPE, lambda d: (box_left_out(True)(d), names_left_out(LEFT_201, True)(d)), extra=["apt_conf_left_out is not a list of at most 200"])
    fails("a box that leaves out 201 files, and a plan that names 200 and cuts the first by name instead of the last", V_HOOKS, lambda d: (box_left_out(True)(d), names_left_out(LEFT_201[1:], True)(d)),
          extra=["01autoremove is not named as left out", "b194 is named, and it is past the 200 that the plan names"])
    fails("a box that leaves out 201 files, and a plan that cuts the list and says that it is cut with a false (the flag is there only when true)", V_SHAPE, lambda d: (box_left_out(True)(d), names_left_out(LEFT_201[:200], False)(d)),
          extra=["apt_conf_left_out_truncated is there, and is not true on a list of 200"])
    fails("a plan that names 199 files and says its list is cut", V_SHAPE, lambda d: (box_left_out(True)(d), names_left_out(LEFT_201[:199], True)(d)), extra=["apt_conf_left_out_truncated is there, and is not true on a list of 200"])
    fails("a box that leaves out 201 files, and a failed update that names them all", DEAD_NAME_, lambda d: (box_left_out(True)(d), names_left_out(LEFT_201[:200], True)(d), dead_says(LEFT_201, True)(d)),
          extra=["data.apt_conf_left_out is "])
    fails("a box that leaves out 201 files, and a failed update that cuts the list and does not say so", DEAD_NAME_, lambda d: (box_left_out(True)(d), names_left_out(LEFT_201[:200], True)(d), dead_says(LEFT_201[:200])(d)),
          extra=["data.apt_conf_left_out_truncated is not there, and the list is cut"])
    fails("a box that leaves out 201 files, and a failed update that cuts the list elsewhere than at the end", DEAD_NAME_, lambda d: (box_left_out(True)(d), names_left_out(LEFT_201[:200], True)(d), dead_says(LEFT_201[1:], True)(d)),
          extra=["data.apt_conf_left_out is "])
    # a box that leaves nothing out: the failed update carries no list (the core does not say an empty one), and a list that it does carry is a FAIL
    NOTHING_ = lambda d: ([put(d, "aptconf-d-" + n, "// kept\n") for n in sorted(APTCONF)], names_left_out([])(d))
    case("a box that leaves nothing out: the failed update carries no list of the files left out, and that is what it should", 0, ["**PASS** " + DEAD_NAME_], ["FAIL"],
         mutate=lambda d: (NOTHING_(d), jdata(d, DEAD_GET_, lambda x: x["error"]["data"].pop("apt_conf_left_out"))))
    fails("a box that leaves nothing out, and a failed update that carries a list of files left out", DEAD_NAME_, lambda d: NOTHING_(d), extra=["data.apt_conf_left_out is there", "nothing was left out of apt's configuration"])

    # ---- the ids ----
    fails("two plans made back to back have two ids", V_ID_STABLE, lambda d: jplan(d, "p2", lambda p: p.update(plan_id="p1-" + "a" * 32)))
    fails("the plan after the matrix has another id than the first", V_ID_BACK, lambda d: jplan(d, "p4", lambda p: p.update(plan_id="p1-" + "b" * 32)))
    fails("a hold that does not change the id", V_ID_HOLD, lambda d: jplan(d, "p3", lambda p: p.update(plan_id=json.loads(slurp(d, "plan-p1-get.json"))["data"]["plan"]["plan_id"])))
    fails("a hold that is not in holds", V_ID_HOLD, lambda d: jplan(d, "p3", lambda p: p.update(holds=[])))
    fails("a hold that is in holds with another package", V_ID_HOLD, lambda d: jplan(d, "p3", lambda p: p.update(holds=["libc6"])))
    case("a plan that fails in its run is an invalid run, not a failure of the ids", 1, ["INVALID RUN", "the plan p2 failed with sandbox_update_failed in its update phase"], ["**FAIL** " + V_ID_STABLE],
         mutate=lambda d: jstatus(d, "p2", lambda x: (x.update(state="failed", phase="update", error={"code": "sandbox_update_failed", "phase": "update", "detail": "x", "data": {"kind": "other"}}), x.pop("plan"))))
    case("the plan with the hold that fails in its run is an invalid run", 1, ["INVALID RUN", "the plan with the hold failed with sandbox_update_failed"], [],
         mutate=lambda d: jstatus(d, "p3", lambda x: (x.update(state="failed", phase="update", error={"code": "sandbox_update_failed", "phase": "update", "detail": "x", "data": {}}), x.pop("plan"))))
    invalid("the plan after the matrix left no record", "no answer was recorded for plan-p4-get", lambda d: remove(d, "plan-p4-get.code"), silent=[V_ID_BACK])
    invalid("the second plan left no watching", "plan-p2.polls.tsv is missing or empty", lambda d: remove(d, "plan-p2.polls.tsv"))

    # ---- Cache-Control ----
    for call_ in ("plan-idle", "runbook-idle", "plan-p1-post", "plan-p1-get", "runbook-p1", "step-busy-post", "step-format-runbook"):
        fails("%s does not say no-store" % call_, V_NOSTORE, lambda d, c=call_: put(d, c + ".hdr", HDR % (200, "no-cache")), extra=[call_ + " says no-cache"])
    fails("an answer with no Cache-Control at all", V_NOSTORE, lambda d: put(d, "plan-p1-get.hdr", "HTTP/1.1 200 X\r\nContent-Type: application/json\r\n\r\n"), extra=["plan-p1-get says nothing"])
    case("no-store among other directives, in any case, is no-store", 0, ["**PASS** " + V_NOSTORE], ["FAIL"], mutate=lambda d: put(d, "plan-p1-get.hdr", HDR % (200, "Private, No-Store, max-age=0")))
    fails("no-store as a part of another word is not no-store", V_NOSTORE, lambda d: put(d, "plan-p1-get.hdr", HDR % (200, "x-no-store-y")))
    case("the last block of the headers is the answer's (a 100 Continue before it)", 0, ["**PASS** " + V_NOSTORE], ["FAIL"],
         mutate=lambda d: put(d, "plan-p1-post.hdr", "HTTP/1.1 100 Continue\r\n\r\n" + HDR % (202, "no-store")))

    # ---- the runbook ----
    def runbook(fn):
        return lambda d: jdata(d, "runbook-p1", fn)

    def rb_text(fn):
        return runbook(lambda x: x.update(text=fn(x["text"])))

    fails("the runbook is for another plan_id", V_RB_SHAPE, runbook(lambda x: x.update(plan_id="p1-" + "c" * 32)))
    fails("the runbook is for another hop", V_RB_SHAPE, runbook(lambda x: x.update(hop="debian-12-13")))
    fails("the runbook answers 202", V_RB_SHAPE, lambda d: api(d, "runbook-p1", code=202, body=fxj("runbook")))
    fails("the runbook's text is more than 256 KiB", V_RB_SHAPE, rb_text(lambda t: t + "# " + "x" * 262144 + "\n"), extra=["bytes, 262144 at most"])
    fails("the runbook's text is not for this plan", V_RB_SHAPE, rb_text(lambda t: t.replace("# Plan: p1-5787acb2f81006785a3076312615ae6c ", "# Plan: p1-other ")))
    fails("the runbook lists eight blocks", V_RB_SHAPE, runbook(lambda x: x["blocks"].pop()))
    fails("the runbook lists the blocks in another order", V_RB_SHAPE, runbook(lambda x: x["blocks"].reverse()))
    fails("the runbook has two points of no return", V_RB_SHAPE, runbook(lambda x: x["blocks"][1].update(point_of_no_return=True)), extra=["the point of no return is"])
    fails("the runbook has no point of no return", V_RB_SHAPE, runbook(lambda x: x["blocks"][5].update(point_of_no_return=False)))
    fails("the runbook has no restart", V_RB_SHAPE, runbook(lambda x: x["blocks"][7].update(reboot=False)), extra=["the restart is after nowhere"])
    fails("the restart is after the wrong block", V_RB_SHAPE, runbook(lambda x: (x["blocks"][7].update(reboot=False), x["blocks"][8].update(reboot=True))))
    fails("a block that is not an object", V_RB_SHAPE, runbook(lambda x: x["blocks"].__setitem__(0, "verify")))
    fails("a block whose reboot is a number", V_RB_SHAPE, runbook(lambda x: x["blocks"][0].update(reboot=0)))
    fails("the text has a block that is not listed", V_RB_SHAPE, rb_text(lambda t: t.replace("# >>> BLOCK 8 after", "# >>> BLOCK 8 later")))
    fails("the text is not a string", V_RB_SHAPE, runbook(lambda x: x.update(text=5)))
    invalid("the runbook of the first plan left no record", "no answer was recorded for runbook-p1", lambda d: remove(d, "runbook-p1.code"), silent=[V_RB_SHAPE])
    # sh -n on the guest
    shn = lambda fn: (lambda d: ledit(d, "runbook-p1.shn.tsv", fn))
    fails("a block that sh -n does not read", V_RB_SHN, shn(lambda t: t.replace("3\tsources\t0", "3\tsources\t2")), extra=["sh -n does not read: sources"])
    fails("the guest read fewer blocks than the text has", V_RB_SHN, shn(lambda t: "".join(t.splitlines(True)[:-1])))
    fails("the guest read the blocks under other names", V_RB_SHN, shn(lambda t: t.replace("verify", "check")))
    invalid("the guest left no result of sh -n", "runbook-p1.shn.tsv is missing or empty", lambda d: remove(d, "runbook-p1.shn.tsv"), silent=[V_RB_SHN])
    invalid("a row of sh -n that is not index, id and status", "runbook-p1.shn.tsv has a row that is not", shn(lambda t: t + "9\n"), silent=[V_RB_SHN])
    # the tree and the files put aside
    fails("the runbook writes a line of sources.list that the plan did not make", V_RB_TREE, rb_text(lambda t: t.replace("deb http://deb.debian.org/debian bookworm main\n", "deb http://deb.debian.org/debian bullseye main\n", 1)), extra=["sources.list: the runbook writes"])
    fails("the runbook writes a file more", V_RB_TREE, rb_text(lambda t: t.replace("mv -f \"$APT_ETC/sources.list.casaos-new\"", "cat > \"$APT_ETC/sources.list.d/extra.list.casaos-new\" <<'CASAOS_EOF'\nx\nCASAOS_EOF\nmv -f \"$APT_ETC/sources.list.casaos-new\"")))
    fails("the runbook does not put docker.list aside", V_RB_TREE, rb_text(lambda t: t.replace('mv -f "$APT_ETC/sources.list.d/docker.list" "$APT_ETC/sources.list.d/docker.list.casaos-paused"\n', "")), extra=["the files put aside are none"])
    case("the runbook puts docker.list aside with a plain mv, which is read as well (the block said so before it said -f)", 0, ["**PASS** " + V_RB_TREE], ["FAIL"],
         mutate=rb_text(lambda t: t.replace('mv -f "$APT_ETC/sources.list.d/docker.list" "$APT_ETC/sources.list.d/docker.list.casaos-paused"', 'mv "$APT_ETC/sources.list.d/docker.list" "$APT_ETC/sources.list.d/docker.list.casaos-paused"')))
    fails("the runbook puts another file aside", V_RB_TREE, rb_text(lambda t: t.replace("docker.list.casaos-paused", "docker.list.casaos-paused\"\nmv \"$APT_ETC/sources.list.d/other.list\" \"$APT_ETC/sources.list.d/other.list.casaos-paused", 1)))
    fails("the runbook checks another hash of the sources", V_RB_TREE, rb_text(lambda t: t.replace("35d17f59fd0705980471e5e52dcf6f54e2292d8b14e245051b88f45539ebcb8a", "0" * 64)), extra=["the hash of the sources that block 0 checks"])
    fails("the runbook checks another hash of the files it wrote", V_RB_TREE, rb_text(lambda t: t.replace("b5112f83e29b14e258b0c60275b06ec2c6212a7c0fab519289ac9ba3f2624d0a", "0" * 64)), extra=["the hash of the files written for apt that block 3 checks"])
    fails("the runbook does not check the hash of the files it wrote", V_RB_TREE, rb_text(lambda t: t.replace('[ "$written" = ', '[ "$written" != ')), extra=["the hash of the files written for apt that block 3 checks"])
    fails("the runbook's tree is not the one of the plan's hash_generated", V_RB_TREE, lambda d: jplan(d, "p1", lambda p: p["sources"].update(hash_generated="0" * 64)), extra=["not the one of the plan's hash_generated"])
    fails("the first line of the tree is commented out in the runbook", V_RB_TREE, rb_text(lambda t: t.replace("<<'CASAOS_EOF'\ndeb http", "<<'CASAOS_EOF'\n# deb http", 1)))

    # a blocked plan has no runbook: the first plan is blocked, so the runbook is not judged
    case("a first plan that is blocked has no runbook: the checks of the runbook are not made, and the verdict is a FAIL", 1, ["**FAIL** " + V_VERDICT, "**NOT TESTABLE** the runbook of the first plan (shape, sh -n, tree)"], ["INVALID", "**PASS** " + V_RB_SHAPE, "Traceback"],
         mutate=plan_edit(lambda p: p.update(verdict="blocked", problems=[{"code": "dist_kept_back", "severity": "blocked", "data": {"names": ["x"]}}])))
    # the plan with a hold
    fails("a plan with a hold that is not blocked though base-files is kept back", V_RB_BLOCKED, lambda d: jplan(d, "p3", lambda p: p.update(verdict="needs_acceptance")), extra=["the verdict is needs_acceptance, not blocked"])
    fails("a blocked plan that does not say dist_kept_back", V_RB_BLOCKED, lambda d: jplan(d, "p3", lambda p: p.update(problems=[q for q in p["problems"] if q["code"] != "dist_kept_back"])), extra=["no dist_kept_back names base-files"])
    fails("a dist_kept_back that does not name base-files", V_RB_BLOCKED, lambda d: jplan(d, "p3", lambda p: p["problems"][0]["data"].update(names=["libc6"])))
    fails("the runbook of a blocked plan is written (200)", V_RB_BLOCKED, lambda d: api(d, "runbook-p3", body=fxj("runbook")))
    fails("the runbook of a blocked plan says another reason", V_RB_BLOCKED, lambda d: api(d, "runbook-p3", code=409, body=fxj("no_plan-failed")), extra=["the runbook's reason is failed, not blocked"])
    # a plan with a hold that does not keep the held package back lost it: apt keeps back a held package that has a newer candidate (measured, apt 2.2.4, in -s upgrade and -s dist-upgrade), and no package of
    # bookworm needs a newer base-files than the held one. It is a FAIL of the blocked plan and the run is red: never a NOT TESTABLE, which leaves the job green
    lost_ = lambda p: (p.update(kept_back=[], verdict="needs_acceptance", problems=[q for q in p["problems"] if q["code"] != "dist_kept_back"]), p["counts"]["dist_upgrade"].update(kept_back=0))
    case("a hold that the plan does not keep back is a FAIL of the blocked plan with the reason, and never a NOT TESTABLE that leaves the run green", 1, ["**FAIL** " + V_RB_BLOCKED, "it lost the held package"],
         ["INVALID", "**NOT TESTABLE** " + V_RB_BLOCKED, "**PASS** " + V_RB_BLOCKED, "Traceback"], mutate=lambda d: jplan(d, "p3", lost_))
    case("a hold that is not in holds either, and not kept back, is a FAIL of the ids and of the blocked plan", 1, ["**FAIL** " + V_ID_HOLD, "**FAIL** " + V_RB_BLOCKED, "lists the holds nothing"],
         ["INVALID", "**NOT TESTABLE** " + V_RB_BLOCKED, "Traceback"], mutate=lambda d: jplan(d, "p3", lambda p: (lost_(p), p.update(holds=[]))))
    # the plan with the hold is a plan: its id has the form of one (p1- and 32 hex), and its shape and counts are the first plan's (an id that is any text, or a plan that is not one, passed)
    for what_, edit_, said_ in (("no plan_id", lambda p: p.pop("plan_id"), "plan_id is not p1- and 32 hex"), ("a plan_id that is a word", lambda p: p.update(plan_id="garbage"), "plan_id is not p1- and 32 hex"),
                                ("a plan_id of 31 hex digits", lambda p: p.update(plan_id="p1-" + "a" * 31), "plan_id is not p1- and 32 hex"), ("a plan_id of 33 hex digits", lambda p: p.update(plan_id="p1-" + "a" * 33), "plan_id is not p1- and 32 hex"),
                                ("a plan_id with a capital letter", lambda p: p.update(plan_id="p1-" + "A" * 32), "plan_id is not p1- and 32 hex"), ("a plan_id that ends with a newline", lambda p: p.update(plan_id="p1-" + "a" * 32 + "\n"), "plan_id is not p1- and 32 hex"),
                                ("a plan_id of another version (p2-)", lambda p: p.update(plan_id="p2-" + "a" * 32), "plan_id is not p1- and 32 hex"), ("a plan_id that is a number", lambda p: p.update(plan_id=7), "plan_id is not p1- and 32 hex"),
                                ("counts that are not four whole numbers", lambda p: p["counts"].update(dist_upgrade={"upgraded": 1}), "counts.dist_upgrade is not four whole numbers"),
                                ("a list of new packages that its count does not say", lambda p: p.update(new_packages=[]), "counts.dist_upgrade.new is 75 and new_packages has 0"),
                                ("a verdict that is not one", lambda p: p.update(verdict="fine"), "verdict is not one of ok, needs_acceptance, blocked"),
                                ("the tree of the sources", lambda p: p["sources"].update(tree=[]), "the plan has the tree of the sources")):
        fails("the plan with the hold has %s" % what_, V_ID_HOLD, lambda d, e=edit_: jplan(d, "p3", e), extra=[said_])
    case("the plan with the hold that is whole passes, with its id and its hold in the detail", 0, ["**PASS** " + V_ID_HOLD, "p1-0123456789abcdef0123456789abcdef with base-files on hold"], ["FAIL", "INVALID"])
    for r_, name_ in (("p2", V_ID_STABLE), ("p4", V_ID_BACK)):
        fails("the first plan and the plan %s have the same id, and it is a word and not the id of a plan" % r_, name_, lambda d, r=r_: [jplan(d, x, lambda p: p.update(plan_id="garbage")) for x in ("p1", r)], extra=["garbage"])
    invalid("the runbook of the plan with a hold left no record", "no answer was recorded for runbook-p3", lambda d: remove(d, "runbook-p3.code"))
    invalid("the hold could not be made", "the hold step: base-files could not be put on hold (exit 100)", lambda d: put(d, "step-hold.dirty-rc", "100\n"), silent=[V_ID_HOLD, V_RB_BLOCKED], lacks=["**PASS** " + V_ID_HOLD, "**PASS** " + V_RB_BLOCKED])
    invalid("the hold was not undone", "the hold step: the undo failed", lambda d: put(d, "step-hold.undone", "no\n"), silent=[V_ID_BACK])
    invalid("the hold step did not run to its end", "the hold step did not run to its end", lambda d: remove(d, "step-hold.undone"))

    # ---- the dirty matrix, one step at a time ----
    for s in STEPS:
        sid, code_ = s["id"], s["code"]
        name_ = f_step(s)
        P_, G_, RB_ = "step-%s-post" % sid, "step-%s-get" % sid, "step-%s-runbook" % sid
        if s["kind"] == "job":
            fails("step %s: the plan succeeds instead (a job that is done)" % sid, name_, lambda d, g=G_: api(d, g, body=plan_get(T0)), extra=["the job is done, not failed"])
            fails("step %s: the POST is refused instead" % sid, name_, lambda d, p=P_: api(d, p, code=409, body=fxj("refused-busy")), extra=["the POST answered HTTP 409, not 202"])
            fails("step %s: the job fails with another code" % sid, name_, lambda d, g=G_: jdata(d, g, lambda x: x["error"].update(code="plan_unparsed")), extra=["the code is plan_unparsed, not " + code_])
            fails("step %s: the job fails in another phase" % sid, name_, lambda d, g=G_, ph=s["phase"]: jdata(d, g, lambda x: x["error"].update(phase="parse" if ph != "parse" else "update")), extra=["the phase is "])
            fails("step %s: the status says another phase than the error" % sid, name_, lambda d, g=G_, ph=s["phase"]: jdata(d, g, lambda x: x.update(phase="parse")))
            fails("step %s: the GET at the end of the job is not usable" % sid, name_, lambda d, g=G_: api(d, g, raw="<html>", code=200), extra=["no usable GET at the end of the job"])
            if s["needle"]:
                fails("step %s: the failure does not say %s" % (sid, s["needle"]), name_, lambda d, g=G_, n=s["needle"]: jedit(d, g + ".json", lambda j: j.update(data=json.loads(json.dumps(j["data"]).replace(n, "x")))), extra=["it does not say " + s["needle"]])
            if s["kind_in"]:
                fails("step %s: the kind of the failed update is other" % sid, name_, lambda d, g=G_: jdata(d, g, lambda x: x["error"]["data"].update(kind="other")), extra=["data.kind is other"])
                for k_ in s["kind_in"]:
                    case("step %s: the kind %s is one the step accepts" % (sid, k_), 0, ["**PASS** " + name_], ["FAIL"], mutate=lambda d, g=G_, k=k_: jdata(d, g, lambda x: x["error"]["data"].update(kind=k)))
            # what the failure says of the files that the sandbox left out of apt's configuration: a failed update, a solver that fails and an output that is not understood carry the plan's own list
            # (the same files and reasons in the same order), and every other failure carries none, and no flag of a cut
            if code_ in ("sandbox_update_failed", "sandbox_solver_failed", "plan_unparsed"):
                fails("step %s: the failure does not carry the files that the sandbox left out" % sid, name_, lambda d, g=G_: jdata(d, g, lambda x: x["error"]["data"].pop("apt_conf_left_out")), extra=["data.apt_conf_left_out is not there"])
                fails("step %s: the failure carries a list with one file less than the plan's" % sid, name_, lambda d, g=G_: jdata(d, g, lambda x: x["error"]["data"]["apt_conf_left_out"].pop()), extra=["data.apt_conf_left_out is "])
                fails("step %s: the failure carries the list in another order" % sid, name_, lambda d, g=G_: jdata(d, g, lambda x: x["error"]["data"]["apt_conf_left_out"].reverse()), extra=["data.apt_conf_left_out is "])
                fails("step %s: the failure carries a reason that is not the plan's" % sid, name_, lambda d, g=G_: jdata(d, g, lambda x: x["error"]["data"]["apt_conf_left_out"][0].update(reason="too_large")), extra=["data.apt_conf_left_out is "])
                fails("step %s: the failure carries a list that is not a list" % sid, name_, lambda d, g=G_: jdata(d, g, lambda x: x["error"]["data"].update(apt_conf_left_out="x")), extra=["data.apt_conf_left_out is "])
                fails("step %s: the failure says the list is cut, and it is not" % sid, name_, lambda d, g=G_: jdata(d, g, lambda x: x["error"]["data"].update(apt_conf_left_out_truncated=True)),
                      extra=["data.apt_conf_left_out_truncated is true, and the list is not cut"])
                fails("step %s: the failure has no data at all" % sid, name_, lambda d, g=G_: jdata(d, g, lambda x: x["error"].pop("data")), extra=["data.apt_conf_left_out is not there"])
            else:
                fails("step %s: the failure carries a list of the files left out (a failure of this code does not)" % sid, name_,
                      lambda d, g=G_: jdata(d, g, lambda x: x["error"].setdefault("data", {}).update(apt_conf_left_out=LEFT_OUT_OF_THE_IMAGE)), extra=["a failure of this code does not carry it"])
                fails("step %s: the failure says that a list is cut (a failure of this code has none)" % sid, name_,
                      lambda d, g=G_: jdata(d, g, lambda x: x["error"].setdefault("data", {}).update(apt_conf_left_out_truncated=True)), extra=["data.apt_conf_left_out_truncated is true, and the list is not cut"])
            fails("step %s: the failed job has a runbook" % sid, V_STEP_NORUNBOOK, lambda d, r=RB_: api(d, r, body=fxj("runbook")), extra=["%s: HTTP 200, not 409" % sid])
            fails("step %s: the runbook of the failed job says another reason" % sid, V_STEP_NORUNBOOK, lambda d, r=RB_: api(d, r, code=409, body=fxj("no_plan-blocked")), extra=["%s: reason is blocked, not failed" % sid])
            # the answer that says the job failed is judged as a whole: its status, its envelope, and the detail of the failure (at most 6 lines of 200 printable characters, 400 for a source that the core refuses: its own sentence, not apt's words; a URL no more than its scheme and host)
            cap_ = 400 if code_.startswith("sources_") else 200
            fails("step %s: the GET at the end of the job answers 202" % sid, name_, lambda d, g=G_, f=FAILED_FIXTURE[sid]: api(d, g, code=202, body=fxj(f)), extra=["HTTP 202, not 200"])
            fails("step %s: the answer at the end of the job has success 0" % sid, name_, lambda d, g=G_: jedit(d, g + ".json", lambda j: j.update(success=0)), extra=["success is 0, not 200"])
            fails("step %s: the answer at the end of the job has no message" % sid, name_, lambda d, g=G_: jedit(d, g + ".json", lambda j: j.pop("message")), extra=["no message"])
            for what_, detail_, said_ in (("seven lines", "\n".join("line %d" % i for i in range(7)), "the detail has 7 lines, 6 at most"),
                                          ("a line of %d characters" % (cap_ + 1), "x" * (cap_ + 1), "a line of the detail is longer than %d characters" % cap_),
                                          ("a character that is not printable ASCII", "Could not resolve h\u00e9", "the detail is not printable ASCII"),
                                          ("a control character", "a\tb", "the detail is not printable ASCII"),
                                          ("a URL with its path", "Err:1 http://deb.debian.org/debian bookworm InRelease", "the detail has a URL that is more than its scheme and host: http://deb.debian.org/debian"),
                                          ("a URL with a user name and a password", "Err:1 http://user:secret@deb.debian.org bookworm", "the detail has a URL that is more than its scheme and host"),
                                          ("a URL with a query", "Get http://deb.debian.org?token=abc", "the detail has a URL that is more than its scheme and host"),
                                          ("a detail that is not a string", ["Could not resolve"], "the detail is not a string"),
                                          ("six lines", "\n".join(["%s" % (s["needle"] or "line 0")] + ["line %d" % i for i in range(1, 6)]), None),
                                          ("a line of %d characters" % cap_, ((s["needle"] or "x") + " " + "x" * cap_)[:cap_], None),
                                          ("URLs that are scheme and host, one with a port, and a dot after it", "W: Failed to fetch 'http://a.debian.org', then (https://b.debian.org:8443); %s\nErr http://c.debian.org:80." % s["needle"], None)):
                if said_ is None:
                    case("step %s: a detail with %s is fine" % (sid, what_), 0, ["**PASS** " + name_], ["FAIL"], mutate=lambda d, g=G_, v=detail_: jdata(d, g, lambda x: x["error"].update(detail=v)))
                else:
                    fails("step %s: a detail with %s" % (sid, what_), name_, lambda d, g=G_, v=detail_: jdata(d, g, lambda x: x["error"].update(detail=v)), extra=[said_])
        elif s["kind"] == "canary":
            fails("step %s: the job failed instead of making a plan" % sid, name_, lambda d, g=G_: api(d, g, body=fxj("failed-sources_format")), extra=["the job is failed, not done (sources_format in sources)"])
            fails("step %s: the POST is refused instead" % sid, name_, lambda d, p=P_: api(d, p, code=409, body=fxj("refused-busy")), extra=["the POST answered HTTP 409, not 202"])
            fails("step %s: the GET at the end of the job is not usable" % sid, name_, lambda d, g=G_: api(d, g, raw="<html>", code=200), extra=["no usable GET at the end of the job"])
            fails("step %s: the GET at the end of the job answers 202" % sid, name_, lambda d, g=G_, s_=s: api(d, g, code=202, body=canary_plan(s_)), extra=["HTTP 202, not 200"])
            fails("step %s: the plan does not name the fragment as left out" % sid, name_, lambda d, g=G_, s_=s: api(d, g, body=canary_plan(s_, listed=False)), extra=["the plan does not name %s as left out" % s["frag"]])
            fails("step %s: the plan names another fragment and not this one" % sid, name_,
                  lambda d, g=G_, s_=s: api(d, g, body=canary_plan(dict(s_, frag="99osp-canary-other"))), extra=["the plan does not name %s as left out" % s["frag"]])
            fails("step %s: the fragment is left out for another reason" % sid, name_,
                  lambda d, g=G_, f=s["frag"]: jdata(d, g, lambda x: [e.update(reason="too_large") for e in x["plan"]["apt_conf_left_out"] if e["file"] == f]), extra=["%s is left out for too_large, not for %s" % (s["frag"], s["reason"])])
            fails("step %s: the answer says the job is running, with a plan in it" % sid, name_, lambda d, g=G_: jdata(d, g, lambda x: x.update(state="running")), extra=["the job is running, not done"])
            fails("step %s: the answer at the end of the job has success 0" % sid, name_, lambda d, g=G_: jedit(d, g + ".json", lambda j: j.update(success=0)), extra=["success is 0, not 200"])
            fails("step %s: the plan has no list of what it left out" % sid, name_, lambda d, g=G_: jdata(d, g, lambda x: x["plan"].pop("apt_conf_left_out")), extra=["the plan does not name %s as left out (it names nothing)" % s["frag"]])
            fails("step %s: the file that the fragment makes is there after the plan: what it says ran" % sid, name_, lambda d, i=sid: put(d, "step-%s.canary" % i, "present\n"), extra=["%s is there after the plan: what the fragment says ran" % s["file"]])
            fails("step %s: the guest's look at the file says something else" % sid, name_, lambda d, i=sid: put(d, "step-%s.canary" % i, "maybe\n"), extra=["is there after the plan"])
            fails("step %s: what the fragment says ran and the fragment is not named either" % sid, name_,
                  lambda d, g=G_, i=sid, s_=s: (api(d, g, body=canary_plan(s_, listed=False)), put(d, "step-%s.canary" % i, "present\n")), extra=["the plan does not name %s as left out" % s["frag"], "what the fragment says ran"])
            invalid("step %s: the guest did not look for the file that the fragment makes" % sid, "step %s: the guest did not look for the file %s that the fragment makes" % (sid, s["file"]), lambda d, i=sid: remove(d, "step-%s.canary" % i), silent=[name_])
            # a plan that failed in its update for the network says nothing against the feature, as the first plan's does: the run is invalid, and the step has no verdict (a hook's file that is there is a FAIL all the same)
            invalid("step %s: the plan failed in its update because a mirror was not reached: no verdict of the fragment, and the run is invalid" % sid, "step %s: the plan failed in its update for a reason of the network" % sid,
                    lambda d, g=G_: api(d, g, body=fxj("failed-sandbox_update_failed")), silent=[name_], lacks=["**PASS** " + name_])
            case("step %s: the plan failed in its update for the network and the hook's file is there: a FAIL, and the run is invalid as well" % sid, 1, ["**FAIL** " + name_, "is there after the plan", "for a reason of the network"], ["Traceback"],
                 mutate=lambda d, g=G_, i=sid: (api(d, g, body=fxj("failed-sandbox_update_failed")), put(d, "step-%s.canary" % i, "present\n")))
            for k_ in ("not_found", "other", "no_pubkey"):
                fails("step %s: the plan failed in its update for the kind %s: a FAIL of the feature, as before" % (sid, k_), name_, lambda d, g=G_, k=k_: (api(d, g, body=fxj("failed-sandbox_update_failed")), jdata(d, g, lambda x: x["error"]["data"].update(kind=k))),
                      extra=["the job is failed, not done (sandbox_update_failed in update)"])
            fails("step %s: the plan failed with another code than the update's, and kind network: a FAIL" % sid, name_, lambda d, g=G_: (api(d, g, body=fxj("failed-sandbox_update_failed")), jdata(d, g, lambda x: x["error"].update(code="sandbox_solver_failed"))),
                  extra=["the job is failed, not done (sandbox_solver_failed in update)"])
            fails("step %s: the job is running and has the error of a failed update of the network: it is not a plan that failed for a mirror" % sid, name_,
                  lambda d, g=G_: (api(d, g, body=fxj("failed-sandbox_update_failed")), jdata(d, g, lambda x: x.update(state="running"))), extra=["the job is running, not done (sandbox_update_failed in update)"])
        elif s["kind"] == "refused":
            fails("step %s: the POST is accepted instead" % sid, name_, lambda d, p=P_: api(d, p, code=202, body=fxj("accepted")), extra=["HTTP 202, not 409"])
            fails("step %s: the POST is refused with another code" % sid, name_, lambda d, p=P_, c=code_: jdata(d, p, lambda x: x.update(error_code="busy" if c != "busy" else "no_room")), extra=["error_code is "])
            fails("step %s: the refusal has no error text" % sid, name_, lambda d, p=P_: jdata(d, p, lambda x: x.pop("error")))
            fails("step %s: the refusal's message is not the error" % sid, name_, lambda d, p=P_: jedit(d, p + ".json", lambda j: j.update(message="other")), extra=["error is not the message"])
            fails("step %s: the POST answers 500" % sid, name_, lambda d, p=P_: api(d, p, code=500, body={"success": 500, "message": "boom"}))
            for k_, v_ in s["data"].items():
                fails("step %s: the refusal says another %s" % (sid, k_), name_, lambda d, p=P_, k=k_: jdata(d, p, lambda x: x["data"].update({k: "/other"})), extra=["data.%s is /other" % k_])
                fails("step %s: the refusal has no %s" % (sid, k_), name_, lambda d, p=P_, k=k_: jdata(d, p, lambda x: x.pop("data")), extra=["data.%s is None" % k_])
            if code_ == "no_room":
                fails("step %s: the room that is free is the room that is needed" % sid, name_, lambda d, p=P_: jdata(d, p, lambda x: x["data"].update(free=x["data"]["need"])), extra=["not a room that is below the"])
                fails("step %s: the room that is free is more than the room that is needed" % sid, name_, lambda d, p=P_: jdata(d, p, lambda x: x["data"].update(free=x["data"]["need"] + 1)), extra=["not a room that is below the"])
                fails("step %s: no room that is free is said" % sid, name_, lambda d, p=P_: jdata(d, p, lambda x: x["data"].pop("free")), extra=["free is None"])
                fails("step %s: the room that is free is less than nothing" % sid, name_, lambda d, p=P_: jdata(d, p, lambda x: x["data"].update(free=-1)), extra=["not a room that is below the"])
                fails("step %s: the room that is free is a text" % sid, name_, lambda d, p=P_: jdata(d, p, lambda x: x["data"].update(free="little")), extra=["free is little"])
                case("step %s: no room at all (free is 0) is a room that is below the need" % sid, 0, ["**PASS** " + name_], ["FAIL"], mutate=lambda d, p=P_: jdata(d, p, lambda x: x["data"].update(free=0)))
        fails("step %s: a sandbox directory is left in /var/tmp" % sid, V_STEP_LEFTOVER, lambda d, i=sid: put(d, "step-%s.leftover" % i, SANDBOX_DIR + "\n"), extra=["%s: %s" % (sid, SANDBOX_DIR)])
        invalid("step %s: /var/tmp was not looked at after the plan" % sid, "step-%s.leftover is missing" % sid, lambda d, i=sid: remove(d, "step-%s.leftover" % i))
        invalid("step %s: the box could not be dirtied" % sid, "step %s: the box could not be dirtied (exit 1)" % sid, lambda d, i=sid: put(d, "step-%s.dirty-rc" % i, "1\n"))
        # a box that was not dirtied answers as a clean box does (a 202 and no refusal, a plan that is done and names no fragment): the step is not judged, so that answer is no FAIL of the feature beside the INVALID
        clean_ = {"job": lambda d, g=G_: api(d, g, body=plan_get(T0)), "refused": lambda d, p=P_: api(d, p, code=202, body=fxj("accepted")), "canary": lambda d, g=G_, s_=s: api(d, g, body=canary_plan(s_, listed=False))}[s["kind"]]
        invalid("step %s: a box that could not be dirtied and answered as a clean box does is no FAIL of the feature" % sid, "step %s: the box could not be dirtied (exit 1)" % sid, lambda d, i=sid, c=clean_: (put(d, "step-%s.dirty-rc" % i, "1\n"), c(d)),
                silent=[name_], has=["| not run | |"], lacks=["**PASS** " + name_])
        invalid("step %s: the undo failed" % sid, "step %s: the undo failed (exit 1, box back: yes)" % sid, lambda d, i=sid: put(d, "step-%s.undo-rc" % i, "1\n"))
        invalid("step %s: the undo did not put the box back" % sid, "step %s: the undo failed (exit 0, box back: no)" % sid, lambda d, i=sid: put(d, "step-%s.undone" % i, "no\n"))
        invalid("step %s: the step did not run to its end" % sid, "step %s: it did not run to its end" % sid, lambda d, i=sid: remove(d, "step-%s.undone" % i))
        invalid("step %s: the POST left no answer" % sid, "no answer was recorded for step-%s-post" % sid, lambda d, i=sid: remove(d, "step-%s-post.code" % i), silent=[name_])
        invalid("step %s: skipped on a system where it exists" % sid, "step %s: the guest skipped it" % sid, lambda d, i=sid: put(d, "step-%s.skipped" % i, "no reason\n"))
    case("a step whose undo failed taints the ones after it: they are not judged, never a FAIL of the feature", 1,
         ["INVALID RUN", "step format: the undo failed", "not judged: the undo of step format failed"], ["**FAIL** step", "**PASS** step cdrom"],
         mutate=lambda d: (put(d, "step-format.undo-rc", "1\n"), api(d, "step-cdrom-get", body=fxj("failed-sources_format"))))
    case("a plan after the matrix that has another id because an undo failed is not a FAIL of the ids", 1, ["INVALID RUN", "step busy: the undo failed"], ["**FAIL** " + V_ID_BACK],
         mutate=lambda d: (put(d, "step-busy.undone", "no\n"), jplan(d, "p4", lambda p: p.update(plan_id="p1-" + "b" * 32))))
    case("a plan after the matrix that has another id because the hold was not undone is not a FAIL of the ids", 1, ["INVALID RUN", "the hold step: the undo failed"], ["**FAIL** " + V_ID_BACK],
         mutate=lambda d: (put(d, "step-hold.undo-rc", "1\n"), jplan(d, "p4", lambda p: p.update(plan_id="p1-" + "b" * 32))))

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
                            ("/var/lib/apt/lists/deb.debian.org_debian_dists_bullseye_InRelease", "its ctime changed (a rename over it)", dict(ctime="1790000999000000000")),
                            ("/var/lib/apt/extended_states", "its content changed", dict(digest="9" * 64)),
                            ("/var/cache/apt/pkgcache.bin", "its mode changed", dict(mode="600")),
                            ("/var/lib/casaos/db/casaos.db", "its owner changed", dict(owner="1000:1000")),
                            ("/var/lib/apt", "its directory time changed (something was created or removed in it)", dict(mtime="1790000999000000000")),
                            ("/etc/casaos/gateway.ini", "it became a link", dict(type="l", digest="->/tmp/x"))):
        fails("invariance: %s: %s" % (path_, what), V_INV_FILES, inv("after", row_edit(path_, **kw)), extra=[path_])
    fails("invariance: a file was created in /var/lib/apt/lists (a list of the target that the plan kept)", V_INV_FILES,
          inv("after", lambda t: t + manifest_text({"/var/lib/apt/lists/deb.debian.org_debian_dists_bookworm_InRelease": ("f", "5", "1790000999000000000", "1790000999000000000", "644", "0:0", "7" * 64)})),
          extra=["created /var/lib/apt/lists/deb.debian.org_debian_dists_bookworm_InRelease"])
    fails("invariance: a file was created in /var/cache/apt/archives", V_INV_FILES,
          inv("after", lambda t: t + manifest_text({"/var/cache/apt/archives/fuse3_3.14_amd64.deb": ("f", "5", "1790000999000000000", "1790000999000000000", "644", "0:0", "7" * 64)})))
    fails("invariance: a file was created in /var/lib/dpkg/updates", V_INV_FILES,
          inv("after", lambda t: t + manifest_text({"/var/lib/dpkg/updates/0001": ("f", "0", "1790000999000000000", "1790000999000000000", "644", "0:0", "8" * 64)})), extra=["created /var/lib/dpkg/updates/0001"])
    fails("invariance: a file was removed from /etc", V_INV_FILES, inv("after", lambda t: "".join(ln + "\n" for ln in t.splitlines() if "/etc/fstab" not in ln)), extra=["removed /etc/fstab"])
    fails("invariance: a file was created in /var/lib/casaos", V_INV_FILES,
          inv("after", lambda t: t + manifest_text({"/var/lib/casaos/os-upgrade/plan.json": ("f", "5", "1790000999000000000", "1790000999000000000", "600", "0:0", "7" * 64)})), extra=["created /var/lib/casaos/os-upgrade/plan.json"])
    case("invariance: /var/log/casaos may change (and the healthy leg's changes there are the proof)", 0, ["**PASS** " + V_INV_FILES], ["FAIL"],
         mutate=inv("after", lambda t: t + manifest_text({"/var/log/casaos/more.log": ("f", "5", "1790000999000000000", "1790000999000000000", "600", "0:0", "7" * 64)})))
    case("invariance: a path under /var/log/casaosX is not /var/log/casaos", 1, ["**FAIL** " + V_INV_FILES, "created /var/log/casaosX/y"], ["INVALID"],
         mutate=inv("after", lambda t: t + manifest_text({"/var/log/casaosX/y": ("f", "5", "1790000999000000000", "1790000999000000000", "600", "0:0", "7" * 64)})))

    def noisy(d):
        ledit(d, "inv-ctl-b.tsv", row_edit("/var/lib/casaos/db/casaos.db", mtime="1790000555000000000", digest="a1" * 32))
        ledit(d, "inv-after.tsv", row_edit("/var/lib/casaos/db/casaos.db", mtime="1790000666000000000", digest="a2" * 32))
    case("invariance: a file that changed by itself in the control is left out of the verdict, and named", 0, ["**PASS** " + V_INV_FILES, "- files: /var/lib/casaos/db/casaos.db"], ["FAIL"], mutate=noisy)
    case("invariance: a control that is quiet leaves the same change a FAIL", 1, ["**FAIL** " + V_INV_FILES], [], mutate=inv("after", row_edit("/var/lib/casaos/db/casaos.db", digest="a2" * 32)))
    invalid("invariance: a snapshot that is missing", "inv-before.tsv is missing or empty", lambda d: remove(d, "inv-before.tsv"), silent=[V_INV_FILES])
    invalid("invariance: the control's snapshot is empty", "inv-ctl-a.tsv is missing or empty", lambda d: put(d, "inv-ctl-a.tsv", ""))
    invalid("invariance: a row that is not a manifest row", "inv-before.tsv has a row that is not a manifest row", lambda d: ledit(d, "inv-before.tsv", lambda t: t + "garbage\n"), silent=[V_INV_FILES])
    fails("invariance: the log of apt's planner is written in /var/log/apt (eipp.log.xz)", V_INV_FILES,
          inv("after", lambda t: t + manifest_text({"/var/log/apt/eipp.log.xz": ("f", "900", "1790000999000000000", "1790000999000000000", "644", "0:0", "7" * 64)})), extra=["created /var/log/apt/eipp.log.xz"])
    fails("invariance: a file of /var/log/apt changed", V_INV_FILES, inv("after", row_edit("/var/log/apt/history.log", digest="a" * 64)), extra=["changed /var/log/apt/history.log (digest)"])
    for tree_ in ("/etc", "/var/lib/dpkg", "/var/lib/apt", "/var/cache/apt", "/var/lib/casaos", "/var/log/apt", "/var/log/casaos"):
        invalid("invariance: a snapshot with no row for %s: the tree was not looked at" % tree_, "has no row for %s" % tree_,
                inv("before", lambda t, p=tree_: "".join(ln + "\n" for ln in t.splitlines() if json.loads(ln.split("\t")[0]) != p)), silent=[V_INV_FILES])
    # the stamp that the hooks of the box touch
    case("the stamp is untouched where the box has one", 0, ["**PASS** " + V_STAMP], ["FAIL", "NOT TESTABLE** " + V_STAMP], stamp=True)
    fails("the stamp is touched by the plan: the hooks of the box ran in the sandbox's update", V_STAMP,
          inv("after", row_edit("/var/lib/apt/periodic/update-success-stamp", mtime="1790000999000000000", ctime="1790000999000000000")), extra=["the sandbox's update ran a hook of the box"], stamp=True)
    case("the stamp touched is caught by the files too, by its name", 1, ["**FAIL** " + V_INV_FILES, "changed /var/lib/apt/periodic/update-success-stamp (mtime/ctime)"], ["INVALID"],
         mutate=inv("after", row_edit("/var/lib/apt/periodic/update-success-stamp", mtime="1790000999000000000", ctime="1790000999000000000")), stamp=True)
    case("the stamp that is not there on a box that has none is NOT TESTABLE, with the reason", 0, ["**NOT TESTABLE** " + V_STAMP, "Debian 11 has no 15update-stamp"], ["FAIL"])

    # ---- invariance: the units, the ports, /var/tmp, the witnesses ----
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
    fails("invariance: a sandbox directory is left in /var/tmp", V_INV_TMP, lambda d: put(d, "tmp-after.txt", slurp(d, "tmp-after.txt") + SANDBOX_DIR + "\n" + SANDBOX_DIR + "/lists\n"), extra=["a sandbox directory is left: " + SANDBOX_DIR])
    fails("invariance: something else was left in /var/tmp", V_INV_TMP, lambda d: put(d, "tmp-after.txt", slurp(d, "tmp-after.txt") + "/var/tmp/apt-key-gpghome.abc\n"), extra=["created /var/tmp/apt-key-gpghome.abc"])
    fails("invariance: something of /var/tmp is gone", V_INV_TMP, lambda d: put(d, "tmp-after.txt", ""), extra=["removed /var/tmp/systemd-private-aaaa-systemd-logind.service-bbbb"])
    case("invariance: a path of /var/tmp that changed by itself in the control is left out", 0, ["**PASS** " + V_INV_TMP, "- paths of /var/tmp: /var/tmp/systemd-private-zzzz"], ["FAIL"],
         mutate=lambda d: (put(d, "tmp-ctl-b.txt", slurp(d, "tmp-ctl-b.txt") + "/var/tmp/systemd-private-zzzz\n"), put(d, "tmp-after.txt", slurp(d, "tmp-after.txt") + "/var/tmp/systemd-private-zzzz\n")))
    case("invariance: an empty /var/tmp before and after is a listing, and passes", 0, ["**PASS** " + V_INV_TMP], ["FAIL", "INVALID"], mutate=lambda d: [put(d, "tmp-%s.txt" % t, "") for t in ("ctl-a", "ctl-b", "before", "after")])
    invalid("invariance: the listing of /var/tmp before was not made", "tmp-before.txt is missing", lambda d: remove(d, "tmp-before.txt"), silent=[V_INV_TMP])
    invalid("invariance: the listing of /var/tmp after was not made", "tmp-after.txt is missing", lambda d: remove(d, "tmp-after.txt"), silent=[V_INV_TMP])
    fails("invariance: a witness container was restarted (another start time)", V_INV_WITNESS, lambda d: ledit(d, "witnesses-after.txt", lambda t: t.replace("08:00:02Z", "08:59:02Z")))
    fails("invariance: a witness container was recreated (another pid)", V_INV_WITNESS, lambda d: ledit(d, "witnesses-after.txt", lambda t: t.replace("1003", "1999")))
    invalid("invariance: a witness container is missing from the start of the window", "witnesses-before.txt does not have the four witness containers",
            lambda d: ledit(d, "witnesses-before.txt", lambda t: "".join(ln for ln in t.splitlines(True) if not ln.startswith("p-db"))), silent=[V_INV_WITNESS])
    invalid("invariance: the witness containers were not looked at", "witnesses-after.txt is missing", lambda d: remove(d, "witnesses-after.txt"), silent=[V_INV_WITNESS])

    # ---- invariance: a unit that started or stopped by itself during the control (PID 1's own journal of the control) is noise, wherever else it changes ----
    TD = "systemd-timedated.service"
    clk = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S")

    def pid1_line(unit_, t, message="Starting Time & Date Service...", **extra):
        e = {"__CURSOR": "s=0;i=1", "__REALTIME_TIMESTAMP": "%d" % round(t * 1e6), "_PID": "1", "_COMM": "systemd", "UNIT": unit_, "MESSAGE": message, "JOB_TYPE": "start", "JOB_ID": "4321"}
        e.update(extra)
        return json.dumps(e) + "\n"

    def appeared(unit_=TD):
        return lambda d: ledit(d, "units-after.tsv", lambda t: t + "%s\tactive\trunning\t14332\tinv-td\n" % unit_)

    def control_of(unit_, *times, **kw):
        return lambda d: put(d, "pid1-control.jsonl", "".join(pid1_line(unit_, T0 + t, **kw) for t in times))

    def raw(name, text):
        return lambda d: put(d, name, text)

    def both(*fns):
        return lambda d: [f(d) for f in fns]

    SELF = "- units that started or stopped by themselves during the control, by PID 1's own journal: "
    started_at = lambda t: "started by itself at " + clk(T0 + t) + " during the control"
    case("the control's journal: a unit that appeared, with an entry of it in the control: left out, and named with the time of its first entry", 0,
         ["**PASS** " + V_INV_UNITS, "and left out: " + TD + ": " + started_at(90.0), SELF + TD + ": " + started_at(90.0)], ["FAIL", "INVALID"], mutate=both(appeared(), control_of(TD, 90.0)))
    case("the control's journal: the first instant of the interval (2 s before the marker control_start)", 0, ["**PASS** " + V_INV_UNITS], ["FAIL"], mutate=both(appeared(), control_of(TD, 78.0)))
    case("the control's journal: the last instant of the interval (1 s after the marker invariance_start)", 0, ["**PASS** " + V_INV_UNITS], ["FAIL"], mutate=both(appeared(), control_of(TD, 101.0)))
    for t_ in (77.9, 101.1, 10.0, 150.0, 300.0):
        fails("the control's journal: an entry %s s after T0 is outside the control: the unit is held against the plan" % t_, V_INV_UNITS, both(appeared(), control_of(TD, t_)), extra=[TD + " appeared"])
    case("the control's journal: a unit that stopped by itself", 0, ["**PASS** " + V_INV_UNITS, "stopped by itself at " + clk(T0 + 90.0)], ["FAIL"],
         mutate=both(lambda d: ledit(d, "units-before.tsv", lambda t: t + "%s\tactive\trunning\t14332\tinv-td\n" % TD), control_of(TD, 90.0, message="Stopped Time & Date Service.")))
    fails("the control's journal: entries of another unit only", V_INV_UNITS, both(appeared(), control_of("ssh.service", 90.0)), extra=[TD + " appeared"])
    # a blank line is no entry (and no reason to disbelieve the file), and an entry that is about no unit is an entry that names none
    NOUNIT = json.dumps({"__CURSOR": "s=0;i=2", "__REALTIME_TIMESTAMP": "%d" % round((T0 + 91.0) * 1e6), "_PID": "1", "_COMM": "systemd", "MESSAGE": "Reached target Multi-User System."}) + "\n"
    for what_, text_ in (("blank lines and a line of spaces and tabs around an entry", "\n   \n" + pid1_line(TD, T0 + 90.0) + "\n \t \n"), ("a blank line between two entries", pid1_line(TD, T0 + 90.0) + "\n" + pid1_line(TD, T0 + 91.0)),
                         ("an entry that is about no unit before the entry of the unit", NOUNIT + pid1_line(TD, T0 + 90.0)), ("an entry that is about no unit after the entry of the unit", pid1_line(TD, T0 + 90.0) + NOUNIT)):
        case("the control's journal: %s: the entry of the unit is believed" % what_, 0, ["**PASS** " + V_INV_UNITS, SELF + TD + ": " + started_at(90.0)], ["FAIL", "INVALID", "was not used"], mutate=both(appeared(), raw("pid1-control.jsonl", text_)))
    fails("the control's journal: a file of blank lines has no entry: nothing is believed", V_INV_UNITS, both(appeared(), raw("pid1-control.jsonl", "\n   \n\t\n")), extra=[TD + " appeared", "PID 1's journal of the control was not used: pid1-control.jsonl has no entry"])
    case("the control's journal: a file with an entry that is about no unit is a journal that names none (it is believed, and the unit that appeared is held against the plan)", 1, ["**FAIL** " + V_INV_UNITS, TD + " appeared"], ["INVALID", "was not used"],
         mutate=both(appeared(), raw("pid1-control.jsonl", NOUNIT)))
    fails("the control's journal: a unit that is left out does not excuse another that changed", V_INV_UNITS, both(appeared(), unit("after", "ssh.service", inv="inv-new"), control_of(TD, 90.0)), extra=["ssh.service went from"])
    fails("no pid1-control.jsonl: held against the plan, and the detail says the journal was not used", V_INV_UNITS, both(appeared(), lambda d: remove(d, "pid1-control.jsonl")),
          extra=[TD + " appeared", "PID 1's journal of the control was not used: there is no pid1-control.jsonl"])
    for what_, text_, why_ in (("a line that is not JSON", pid1_line(TD, T0 + 90.0) + "-- No entries --\n", "a line of pid1-control.jsonl is not JSON"),
                               ("a line that is not an object", pid1_line(TD, T0 + 90.0) + "[1, 2]\n", "a line of pid1-control.jsonl is not a JSON object"),
                               ("a UNIT that is a list", pid1_line(TD, T0 + 90.0) + pid1_line(["a.service"], T0 + 91.0), "an entry of pid1-control.jsonl has a UNIT that is not a string"),
                               ("a time that is not a time", pid1_line(TD, T0 + 90.0, __REALTIME_TIMESTAMP="abc"), "an entry of pid1-control.jsonl has a time that is not a plausible one"),
                               ("an empty file", "", "pid1-control.jsonl has no entry")):
        fails("the control's journal: %s: nothing of it is believed" % what_, V_INV_UNITS, both(appeared(), raw("pid1-control.jsonl", text_)), extra=[TD + " appeared", "PID 1's journal of the control was not used: " + why_])
    fails("the control's journal: no marker control_start: the interval is not known", V_INV_UNITS,
          both(appeared(), control_of(TD, 90.0), lambda d: put(d, "timeline.tsv", "#\t%.6f\tprepare_start\n#\t%.6f\tinvariance_start\n" % (T0, T0 + 100))),
          extra=["the timeline has no marker control_start and invariance_start, in that order"])
    fails("the control's journal: the markers in the wrong order", V_INV_UNITS,
          both(appeared(), control_of(TD, 90.0), lambda d: put(d, "timeline.tsv", "#\t%.6f\tcontrol_start\n#\t%.6f\tinvariance_start\n" % (T0 + 100, T0 + 80))),
          extra=["in that order"])
    case("the control's journal: the interval follows the markers (a control that lasted 320 s has its entry at 300 s inside)", 0, ["**PASS** " + V_INV_UNITS, "(it lasted 320 s)"], ["FAIL"],
         mutate=both(appeared(), control_of(TD, 300.0), lambda d: put(d, "timeline.tsv", "#\t%.6f\tprepare_start\n#\t%.6f\tcontrol_start\n#\t%.6f\tinvariance_start\n" % (T0, T0 + 80, T0 + 400))))
    case("the control's journal: a file in the window and the units that are not changed: nothing is left out", 0, [SELF + "none"], ["FAIL", "and left out: "], mutate=control_of(TD, 10.0))
    case("the control's journal: a unit that is left out does not excuse a file created in the window", 1, ["**PASS** " + V_INV_UNITS, "**FAIL** " + V_INV_FILES, "created /etc/cron.d/new"], ["INVALID", "**FAIL** " + V_INV_UNITS],
         mutate=both(appeared(), control_of(TD, 90.0), inv("after", lambda t: t + manifest_text({"/etc/cron.d/new": ("f", "5", "1790000999000000000", "1790000999000000000", "644", "0:0", "7" * 64)}))))
    case("the control's journal: a unit that is left out does not excuse a socket opened in the window", 1, ["**PASS** " + V_INV_UNITS, "**FAIL** " + V_INV_PORTS], ["INVALID"],
         mutate=both(appeared(), control_of(TD, 90.0), lambda d: ledit(d, "listen-after.txt", lambda t: t + "tcp 0.0.0.0:8080\n")))
    case("the report says what the control is, how long it lasted, and what PID 1's journal adds to it", 0,
         ["## What changed by itself (the control)", "(it lasted 20 s)", "`journalctl _PID=1 -o json`", "Only units are left out so, never a file or a listening socket"], ["FAIL"])
    case("the report says which calls are the window's, and that every other call is outside it", 0,
         ["## What the invariance window looks at", "plan-idle, runbook-idle, plan-p1-post, plan-p1-post-2, the polls, plan-p1-get", "Every other call (the runbook of the plan, the later plans, the dirty matrix) is outside it"], ["FAIL"])

    # ---- what the run says of itself ----
    invalid("a guest that stopped early says why", "the guest stopped early: the installer failed", lambda d: put(d, "aborted", "the installer failed\n"))
    invalid("a guest that did not reach its end", "the guest did not reach the end of its run", lambda d: remove(d, "finished"))
    invalid("a run with no timeline", "timeline.tsv is missing or empty", lambda d: remove(d, "timeline.tsv"))
    invalid("a guest that is not the system the leg needs", "the guest is ubuntu 22.04, the d11 leg needs debian 11", lambda d: put(d, "os-release", 'ID=ubuntu\nVERSION_ID="22.04"\nVERSION_CODENAME=jammy\n'))
    invalid("a guest that says it ran another leg", "the guest says it ran leg 'u22'", lambda d: put(d, "leg", "u22\n"))
    invalid("no os-release", "os-release is missing or empty", lambda d: remove(d, "os-release"))
    case("an empty directory is an invalid run and a pile of reasons, never a pass or a crash", 1, ["INVALID RUN"], ["Traceback", "PASS"], empty=True)
    case("a feature that is wrong and a run that stopped says FAIL and INVALID", 1, ["FAIL and INVALID RUN", "**FAIL** " + V_VERDICT], [],
         mutate=lambda d: (plan_edit(lambda p: p.update(verdict="blocked", problems=[{"code": "dist_kept_back", "severity": "blocked", "data": {"names": ["x"]}}]))(d), put(d, "aborted", "something\n")))
    case("a guest that stopped says so first among the reasons of an invalid run", 1, ["- INVALID: the guest stopped early: the release under test has no /v1/sys/os/upgrade/plan"], [],
         mutate=lambda d: (put(d, "aborted", "the release under test has no /v1/sys/os/upgrade/plan\n"), remove(d, "os-release")))
    case("the report is the same whatever the line ends of the guest's files", 0, ["PASS"], ["FAIL", "INVALID"],
         mutate=lambda d: [put(d, n, slurp(d, n).replace("\n", "\r\n")) for n in ("os-release", "timer-units.txt", "listen-after.txt", "units-after.tsv", "witnesses-after.txt", "tmp-after.txt", "plan-p1.sandbox-seen", "plan-p1.polls.tsv") if exists(d, n)])

    # ---- guards of the report that one case each holds: a guard that is weakened or removed makes its case fail ----
    fails("a poll that curl cut off after the status", V_POLL, polls(lambda t: t.replace("\t200\t0\trunning\tupdate", "\t200\t56\trunning\tupdate", 1)))
    case("8 GB of room taken is plausible", 0, ["**PASS** " + V_ESTIMATES], ["FAIL"], mutate=plan_edit(lambda p: p["estimates"].update(extra_bytes=8 * 10 ** 9)))
    fails("the plan says the action of a file that is not what the box does (the lines are right)", V_SOURCES, plan_edit(lambda p: p["sources"]["files"][0].update(action="unchanged")), extra=["sources.list is unchanged, it should be rewritten"])
    fails("a docker.list that the plan calls rewritten (the lines are right)", V_SOURCES, plan_edit(lambda p: p["sources"]["files"][1].update(action="rewritten")))
    fails("the runbook of a plan that is blocked is refused with another code, and the reason is blocked", V_RB_BLOCKED,
          lambda d: jedit(d, "runbook-p3.json", lambda j: j["data"].update(error_code="no_runbook")), extra=["error_code is no_runbook, not no_plan"])
    fails("the runbook's blocks are listed under other names, and the text says the same names", V_RB_SHAPE,
          lambda d: (jdata(d, "runbook-p1", lambda x: [b.update(id="fetch") for b in x["blocks"] if b["id"] == "download"]),
                     jdata(d, "runbook-p1", lambda x: x.update(text=x["text"].replace("# >>> BLOCK 4 download", "# >>> BLOCK 4 fetch")))), extra=["the blocks are verify, timers, snapshot, sources, fetch"])

    def padded_to(n):
        def pad_(t):
            pad = n - len(t.encode("utf-8"))
            assert pad >= 2, pad
            return t + "#" * (pad - 1) + "\n"
        return pad_
    case("a runbook text of exactly 256 KiB is within the limit", 0, ["**PASS** " + V_RB_SHAPE], ["FAIL"], mutate=rb_text(padded_to(262144)))
    fails("a runbook text of 256 KiB and a byte is more than the limit", V_RB_SHAPE, rb_text(padded_to(262145)), extra=["bytes, 262144 at most"])
    # the cases just above pin the limit itself: the core's golden is held under it
    check("the runbook that the core wrote of the measured box (its golden) is within the 256 KiB that the report allows", len(fxj("runbook")["data"]["text"].encode("utf-8")) <= 262144,
          len(fxj("runbook")["data"]["text"].encode("utf-8")))
    case("the runbook of the measured box, as the core wrote it, is within the limit", 0, ["**PASS** " + V_RB_SHAPE], ["FAIL"])
    # 201 fragments of apt's configuration: the first 200 are read, the 201st is left out and named
    many = lambda d: (put(d, "aptconf.index", slurp(d, "aptconf.index") + "".join("a%03d\n" % i for i in range(1, 196))), [put(d, "aptconf-d-a%03d" % i, "// nothing\n") for i in range(1, 195)])
    case("a link in apt.conf.d is one of the 200 that are read: the plan names the 201st, which is the 200th of the regular ones", 0, ["**PASS** " + V_HOOKS, "a001 (not_regular)", "a195 (too_large)"], ["FAIL"],
         mutate=lambda d: (many(d), put(d, "aptconf.nonregular", "a001\n"), remove(d, "aptconf-d-a001"), left_out(("a001", "not_regular"), ("a195", "too_large"))(d)))
    case("201 fragments of apt's configuration: the 201st is left out, and the plan names it", 0, ["**PASS** " + V_HOOKS, "a195 (too_large)"], ["FAIL"],
         mutate=lambda d: (many(d), left_out(("a195", "too_large"))(d)))
    fails("200 fragments are read and the 201st is not left out by the plan", V_HOOKS, many)
    case("a directory is one of the 200 that are read: the 201st is named, whatever the directory was", 0, ["**PASS** " + V_HOOKS, "a195 (too_large)"], ["FAIL"],
         mutate=lambda d: (many(d), put(d, "aptconf.dirs", "a001\n"), remove(d, "aptconf-d-a001"), left_out(("a195", "too_large"))(d)))
    case("the 201st name is left out for too_large even when it is a directory (the core counts it before it looks at what it is)", 0, ["**PASS** " + V_HOOKS, "a195 (too_large)"], ["FAIL"],
         mutate=lambda d: (many(d), put(d, "aptconf.dirs", "a195\n"), left_out(("a195", "too_large"))(d)))
    invalid("a row of the files that has one column too few", "inv-before.tsv has a row that is not a manifest row",
            lambda d: ledit(d, "inv-before.tsv", lambda t: t + '"/etc/x"\td\t0\t0\t0\t0\t0:0\n'), silent=[V_INV_FILES])
    invalid("a row of the files that has one column too many", "inv-after.tsv has a row that is not a manifest row",
            lambda d: ledit(d, "inv-after.tsv", lambda t: t + '"/etc/x"\td\t0\t0\t0\t0\t0:0\t-\textra\n'), silent=[V_INV_FILES])
    for what_, value_ in (("a time of zero", "0"), ("a time before 2001 (1e8 s)", "100000000000000"), ("a time after 2096 (5e9 s)", "5000000000000000"), ("a time of 1e9 s less a microsecond", "999999999999999"),
                          ("a time of twenty-one digits", "999999999999999999999"), ("a time that is a number and not a string", 1790000090000000), ("a time with a fraction", "1790000090.5")):
        fails("the control's journal: an entry with %s: no unit is left out of the file" % what_, V_INV_UNITS, both(appeared(), raw("pid1-control.jsonl", pid1_line(TD, T0 + 90.0, __REALTIME_TIMESTAMP=value_))),
              extra=["has a time that is not a plausible one"])
    case("the control's journal: a time of exactly 1e9 s is a time", 0, ["**PASS** " + V_INV_UNITS], ["FAIL"], mutate=both(appeared(), raw("pid1-control.jsonl", pid1_line(TD, 1e9)), lambda d: put(d, "timeline.tsv", "#\t%.6f\tprepare_start\n#\t%.6f\tcontrol_start\n#\t%.6f\tinvariance_start\n" % (1e9, 1e9, 1e9 + 100))))

    # ---- a POST that was refused began no job, and the plan the core answers after it is an earlier one ----
    for r_ in ("p2", "p3", "p4"):
        lacking = {"p2": V_ID_STABLE, "p3": V_ID_HOLD, "p4": V_ID_BACK}[r_]
        for what_, mut_, said_ in (("a 409 busy", lambda d, r=r_: api(d, "plan-%s-post" % r, code=409, body=fxj("refused-busy")), "HTTP 409 (busy)"),
                                   ("a 409 that the plan has no room", lambda d, r=r_: api(d, "plan-%s-post" % r, code=409, body=fxj("refused-no_room")), "HTTP 409 (no_room)"),
                                   ("a 200 (an earlier job is still running)", lambda d, r=r_: api(d, "plan-%s-post" % r, body=fxj("already_running")), "HTTP 200 (no reason)"),
                                   ("a 500", lambda d, r=r_: api(d, "plan-%s-post" % r, code=500, body={"success": 500, "message": "boom"}), "HTTP 500 (no reason)"),
                                   ("a curl that gave up", lambda d, r=r_: api(d, "plan-%s-post" % r, code=0, curl=28, raw="", err="timed out"), "timeout (no reason)")):
            invalid("the POST of the plan %s is refused with %s: the plan the core answers after it is not this run's, and the run is invalid" % (r_, what_), "the POST of the plan %s did not begin a job: %s" % (r_, said_), mut_,
                    has=["is not judged as this one's"], silent=[lacking], lacks=["**PASS** " + lacking, "**FAIL** " + lacking, "plan-%s-get" % r_ + " was recorded"])
    for r_ in ("p2", "p3", "p4"):
        # the answers of the run that the guest did not watch are not there: no row of the watching, no final answer, and no noise of an invalid run about them
        invalid("the POST of the plan %s is refused and the guest left no watching and no final answer: the reason is the POST's alone" % r_, "the POST of the plan %s did not begin a job: HTTP 409 (busy)" % r_,
                lambda d, r=r_: (api(d, "plan-%s-post" % r, code=409, body=fxj("refused-busy")), remove(d, "plan-%s.polls.tsv" % r, "plan-%s-get.code" % r, "plan-%s-get.json" % r, "plan-%s.sandbox-seen" % r)),
                lacks=["no answer was recorded for plan-%s-get" % r_, "plan-%s.polls.tsv is missing" % r_])
    # a plan that the core answers at the end of a run that began, which was made before its POST, is the one of an earlier run
    for r_ in ("p2", "p3", "p4"):
        invalid("the plan %s that is answered at the end was made 10 minutes before its POST" % r_, "the plan %s that the core answers at the end is not the job its POST began: made_at is 60" % r_,
                lambda d, r=r_: jplan(d, r, lambda p: p.update(made_at=iso(timestamp_of(d, r) - 600), expires_at=iso(timestamp_of(d, r) - 600 + 86400))), lacks=["**PASS** " + {"p2": V_ID_STABLE, "p3": V_ID_HOLD, "p4": V_ID_BACK}[r_]])
        invalid("the status of the plan %s at the end says the job started a minute before its POST" % r_, "the plan %s that the core answers at the end is not the job its POST began: started_at is -6" % r_,
                lambda d, r=r_: jstatus(d, r, lambda x: x.update(started_at=iso(timestamp_of(d, r) - 60))))
        invalid("the plan %s has no made_at, so it cannot be told from an earlier plan" % r_, "the plan %s that the core answers at the end is not the job its POST began: started_at or made_at is not a time" % r_,
                lambda d, r=r_: jplan(d, r, lambda p: p.pop("made_at")))

    # ---- the times of a call: of a guest of this decade, the end after the beginning, and both inside the run, from the first marker to `finished` ----
    # a t0 that is damaged switched off the check of a plan made before its POST: the same plan, with the real t0, is invalid, and with a t0 of 5 it passed
    def times_of(d, name, t0, t1):
        put(d, name + ".t0", "%.6f\n" % t0)
        put(d, name + ".t1", "%.6f\n" % t1)
    CALL_NOT_OF_THE_RUN = "are not the times of a call of this run"
    invalid("a t0 of 5 on the POST of a plan made two hours before it: it is not a call of this run, and the plan is not judged as the run's", "plan-p2-post.t0 and .t1 " + CALL_NOT_OF_THE_RUN,
            lambda d: (lambda tp: (jplan(d, "p2", lambda p: p.update(made_at=iso(tp - 7200), expires_at=iso(tp - 7200 + 86400))), times_of(d, "plan-p2-post", 5.0, tp + 0.2)))(timestamp_of(d, "p2")), lacks=["**PASS** " + V_ID_STABLE])
    for what_, t0_, t1_ in (("a t0 that is 5 s after the epoch", 5.0, T0 + 90.2), ("a t1 that is 5 s after the epoch", T0 + 90.0, 5.0), ("a t0 of 1e9 s less a microsecond", 999999999.999999, T0 + 90.2),
                            ("a t1 of 4e9 s and a microsecond", T0 + 90.0, 4000000000.000001), ("a t1 before its t0", T0 + 90.0, T0 + 89.0), ("a call that ended before the first marker (and more than 3 s)", T0 - 100.0, T0 - 99.8),
                            ("a t0 3.5 s before the first marker", T0 - 3.5, T0 - 3.3), ("a call that began after the guest finished (and more than 3 s)", T0 + 4000.0, T0 + 4000.2), ("a t1 3.5 s after the end", T0 + 3000.0, T0 + 3003.5)):
        invalid("a call with %s" % what_, "ctl-core-1.t0 and .t1 " + CALL_NOT_OF_THE_RUN, lambda d, a=t0_, b=t1_: times_of(d, "ctl-core-1", a, b))
    for what_, t0_, t1_ in (("a t0 3 s before the first marker (the slack is 3 s)", T0 - 3.0, T0 - 2.8), ("a t0 of the first marker", T0, T0 + 0.2), ("a t1 of the end of the run", T0 + 2999.0, T0 + 3000.0),
                            ("a t1 3 s after the end (the slack is 3 s)", T0 + 3000.0, T0 + 3003.0), ("a call of no time at all (t0 is t1)", T0 + 90.0, T0 + 90.0)):
        case("a call with %s is a call of the run" % what_, 0, ["PASS"], ["FAIL", "INVALID"], mutate=lambda d, a=t0_, b=t1_: times_of(d, "ctl-core-1", a, b))
    invalid("a call with a t0 of 5 and a run that did not finish (nothing to hold its times against): the time is not a time of this decade", "ctl-core-1.t0 and .t1 " + CALL_NOT_OF_THE_RUN,
            lambda d: (remove(d, "finished"), times_of(d, "ctl-core-1", 5.0, T0 + 90.2)), has=["the guest did not reach the end of its run"])
    invalid("a call with a t1 of 5e9 and a run that did not finish: the end is not a time of this decade either", "ctl-core-1.t0 and .t1 " + CALL_NOT_OF_THE_RUN,
            lambda d: (remove(d, "finished"), times_of(d, "ctl-core-1", T0 + 90.0, 5e9)), has=["the guest did not reach the end of its run"])
    invalid("a `finished` that is not a time: the calls cannot be placed in the run", "`finished` is not a time", lambda d: put(d, "finished", "yesterday\n"))
    invalid("a timeline whose markers have no plausible time: the calls cannot be placed in the run", "timeline.tsv has no marker with a plausible time", lambda d: put(d, "timeline.tsv", "#\t5\tprepare_start\n#\tsoon\tcontrol_start\n"))

    # ---- an end of a run that the guest watched and the answer does not have: no verdict vanishes, the run is invalid ----
    for r_ in ("p1", "p2", "p3", "p4"):
        g_ = "plan-%s-get" % r_
        for what_, mut_, why_ in (("not JSON", lambda d, g=g_: api(d, g, raw="<html>gateway</html>"), "not usable (unreadable)"),
                                  ("a whole number of 5000 digits", lambda d, g=g_: api(d, g, raw="7" * 5000), "not usable (unreadable)"),
                                  ("an array nested a hundred thousand deep", lambda d, g=g_: api(d, g, raw="[" * 100000 + "]" * 100000), "not usable (unreadable)"),
                                  ("an object nested forty deep", lambda d, g=g_: api(d, g, raw='{"a":' * 40 + "1" + "}" * 40), "not usable (unreadable)"),
                                  ("nothing in the body", lambda d, g=g_: api(d, g, raw=""), "not usable (unreadable)"),
                                  ("a status that is 500, with a body", lambda d, g=g_: api(d, g, code=500, body={"success": 500, "message": "boom", "data": {}}), "HTTP 500, not 200"),
                                  ("a status that is 202, with the body of a plan", lambda d, g=g_: api(d, g, code=202, body=json.loads(slurp(d, g + ".json"))), "HTTP 202, not 200"),
                                  ("curl that gave up on it", lambda d, g=g_: api(d, g, code=0, curl=28, raw="", err="timed out"), "not usable (timeout)"),
                                  ("curl that was cut after the status", lambda d, g=g_: api(d, g, curl=56, body=json.loads(slurp(d, g + ".json"))), "not usable (cut)"),
                                  ("a state that is running while the polls say done", lambda d, g=g_: jdata(d, g, lambda x: x.update(state="running")), "of the state running"),
                                  ("a state that is failed while the polls say done", lambda d, g=g_: jdata(d, g, lambda x: x.update(state="failed")), "of the state failed")):
            invalid("the end of the plan %s is %s, and the polls say it was done: the end cannot be judged" % (r_, what_), "the last poll of the plan %s said done, and the answer kept as its end is %s" % (r_, why_), mut_,
                    silent=[V_SHAPE, V_TIMES, V_ID_STABLE])
    for r_ in ("p1", "p2", "p3", "p4"):
        case("the plan %s that is done and has no plan in its answer is a plan with nothing in it, which the ids and the shape fail on, and not a silence" % r_, 1,
             ["**FAIL** " + (V_SHAPE if r_ == "p1" else {"p2": V_ID_STABLE, "p3": V_ID_HOLD, "p4": V_ID_BACK}[r_])], ["INVALID", "Traceback"],
             mutate=lambda d, r=r_: jdata(d, "plan-%s-get" % r, lambda x: x.pop("plan")))
    for r_ in ("p2", "p3", "p4"):
        invalid("the plan %s that was still running when the guest stopped watching it did not end within 15 minutes" % r_, "the plan %s was still running when the guest stopped watching it" % r_,
                lambda d, r=r_: (ledit(d, "plan-%s.polls.tsv" % r, lambda t: t.replace("\tdone\t-", "\trunning\tparse")), put(d, "plan-%s.timeout" % r, "900\n"), jdata(d, "plan-%s-get" % r, lambda x: (x.update(state="running", phase="parse"), x.pop("plan")))))
    case("the first plan that was still running when the guest stopped is a FAIL of the phases (it did not end), and the other verdicts of the plan are not made", 1, ["**FAIL** " + V_PHASES, "the job did not end within 15 minutes"], ["Traceback", "INVALID"],
         mutate=lambda d: (ledit(d, "plan-p1.polls.tsv", lambda t: t.replace("\tdone\t-", "\trunning\tparse")), put(d, "plan-p1.timeout", "900\n"), jdata(d, "plan-p1-get", lambda x: (x.update(state="running", phase="parse"), x.pop("plan")))))

    # a verdict that goes quiet is no pass: the report is run on a copy in which one verdict is not made, and says so
    def changed_report(old, new, many=False):
        """the path of a copy of the report in which the text `old` (found once, or the pattern `old` wherever it is, with many) is `new`"""
        src = open(REPORT, newline="").read().replace("\r\n", "\n")
        if many:
            src, n = re.subn(old, new, src)
            assert n >= 1, old
        else:
            assert src.count(old) == 1, old
            src = src.replace(old, new)
        path_ = os.path.join(tempfile.mkdtemp(), "os-plan-proof-report.py")
        with open(path_, "w", newline="") as f:
            f.write(src)
        return path_

    real_report_ = REPORT
    QUIET = [("V_IDLE", V_IDLE), ("V_RB_IDLE", V_RB_IDLE), ("V_POST", V_POST), ("V_POST_TWICE", V_POST_TWICE), ("V_POLL", V_POLL), ("V_PHASES", V_PHASES), ("V_SANDBOX_SEEN", V_SANDBOX_SEEN),
             ("V_NOSTORE", V_NOSTORE), ("V_LEAK", V_LEAK), ("V_INV_FILES", V_INV_FILES), ("V_INV_UNITS", V_INV_UNITS), ("V_INV_PORTS", V_INV_PORTS), ("V_INV_TMP", V_INV_TMP), ("V_INV_WITNESS", V_INV_WITNESS),
             ("V_STAMP", V_STAMP), ("V_STEP_LEFTOVER", V_STEP_LEFTOVER), ("V_SHAPE", V_SHAPE), ("V_TIMES", V_TIMES), ("V_ID_STABLE", V_ID_STABLE), ("V_ID_BACK", V_ID_BACK), ("V_ID_HOLD", V_ID_HOLD),
             ("V_RB_BLOCKED", V_RB_BLOCKED), ("V_VERDICT", V_VERDICT), ("V_VERDICT_RULE", V_VERDICT_RULE), ("V_REMOVALS", V_REMOVALS), ("V_ESTIMATES", V_ESTIMATES), ("V_DISK", V_DISK), ("V_SANDBOX", V_SANDBOX),
             ("V_DOCKER", V_DOCKER), ("V_HOOKS", V_HOOKS), ("V_SOURCES", V_SOURCES), ("V_RB_SHAPE", V_RB_SHAPE), ("V_RB_SHN", V_RB_SHN), ("V_RB_TREE", V_RB_TREE)]
    for const_, name_ in QUIET:
        REPORT = changed_report(r"\b(?:verdict|not_testable)\(%s," % const_, "(lambda *a: None)(%s," % const_, many=True)
        case("a verdict that is not made is not a pass: %s" % const_, 1, ["INVALID RUN", "verdicts that the run owes were not made", name_[:50]], ["**PASS** " + name_, "Traceback"])
        REPORT = real_report_
    REPORT = changed_report("verdict(name, not off,", "(lambda *a: None)(name, not off,")
    case("a verdict that is not made is not a pass: the counts of the two upgrades", 1, ["INVALID RUN", "2 verdicts that the run owes were not made", V_COUNTS_DIST[:50]], ["**PASS** " + V_COUNTS_DIST, "Traceback"])
    REPORT = real_report_
    for pat_ in (r"verdict\(step_name\(s\),",):
        REPORT = changed_report(pat_, "(lambda *a: None)(step_name(s),", many=True)
        case("a verdict that is not made is not a pass: the steps of the dirty matrix", 1, ["INVALID RUN", "%d verdicts that the run owes were not made" % len(STEPS), "step format"], ["**PASS** step", "Traceback"])
        REPORT = real_report_
    # an exception of the report itself ends as an invalid run that names it, with the verdicts made so far
    REPORT = changed_report("verdict(V_POLL,", "(1 // 0, verdict)[1](V_POLL,")
    case("an exception of the report itself is an invalid run that names it, and the verdicts made so far are in the report", 1,
         ["INVALID RUN", "the report could not be made of this evidence: ZeroDivisionError", "**PASS** " + V_IDLE], ["Traceback"])
    REPORT = real_report_

    # ---- hostile numbers, hostile nesting: the report says what it could not read and does not crash ----
    for what_, mut_ in (("a whole number of 401 digits in sandbox.bytes", plan_edit(lambda p: p["sandbox"].update(bytes=10 ** 400))),
                        ("a whole number of 401 digits in estimates.download_bytes", plan_edit(lambda p: p["estimates"].update(download_bytes=10 ** 400))),
                        ("a whole number of 401 digits in estimates.extra_bytes", plan_edit(lambda p: p["estimates"].update(extra_bytes=10 ** 400))),
                        ("a whole number of 401 digits in the seconds of a phase", plan_edit(lambda p: p["sandbox"]["seconds_by_phase"].update(update=10 ** 400))),
                        ("a whole number of 401 digits in a count", plan_edit(lambda p: p["counts"]["upgrade"].update(upgraded=10 ** 400))),
                        ("a whole number of 401 digits in a disk row", plan_edit(lambda p: p["disk"][0].update(need=10 ** 400))),
                        ("a whole number of 19 digits (2 ** 63) in sandbox.bytes", plan_edit(lambda p: p["sandbox"].update(bytes=1 << 63))),
                        ("an Infinity in the seconds of a phase", plan_edit(lambda p: p["sandbox"]["seconds_by_phase"].update(update=float("inf")))),
                        ("a NaN in the seconds of a phase", plan_edit(lambda p: p["sandbox"]["seconds_by_phase"].update(update=float("nan")))),
                        ("an Infinity in sandbox.bytes", plan_edit(lambda p: p["sandbox"].update(bytes=float("inf"))))):
        case("hostile number: " + what_, 1, ["**FAIL** " + V_SHAPE], ["Traceback", "INVALID"], mutate=mut_)
    case("hostile number: the largest whole number that is a count (2 ** 63 less one) is a number, and far from the measured one", 1, ["**FAIL** " + V_COUNTS_UP], ["Traceback", "INVALID"],
         mutate=plan_edit(lambda p: (p["counts"]["upgrade"].update(upgraded=(1 << 63) - 1))))
    invalid("hostile record: a curl status of 5000 digits", "plan-p1-post.curl, .code, .t0 or .t1 is missing or unreadable", lambda d: put(d, "plan-p1-post.curl", "9" * 5000 + "\n"))
    invalid("hostile record: an HTTP status of 5000 digits", "plan-p1-post.curl, .code, .t0 or .t1 is missing or unreadable", lambda d: put(d, "plan-p1-post.code", "2" * 5000 + "\n"))
    invalid("hostile record: an HTTP status of seven digits", "plan-p1-post.curl, .code, .t0 or .t1 is missing or unreadable", lambda d: put(d, "plan-p1-post.code", "2000000\n"))
    invalid("hostile record: a time of 5000 digits", "plan-p1-post.curl, .code, .t0 or .t1 is missing or unreadable", lambda d: put(d, "plan-p1-post.t0", "1" * 5000 + "\n"))
    invalid("hostile record: a row of the watching with a status of 5000 digits", "plan-p1.polls.tsv has a row that is not one of the watching", lambda d: ledit(d, "plan-p1.polls.tsv", lambda t: t + "1\t2\t" + "9" * 5000 + "\t0\trunning\tupdate\n"))
    case("hostile nesting in the body of a first answer: the answer is not read, and the verdict that wanted it fails by name", 1, ["**FAIL** " + V_IDLE, "no usable answer (unreadable)"], ["Traceback"],
         mutate=lambda d: put(d, "plan-idle.json", '{"success": 200, "message": "ok", "data": ' + "[" * 100000 + "]" * 100000 + "}"))
    case("hostile nesting in the body of the second POST", 1, ["**FAIL** " + V_POST_TWICE, "no usable answer (unreadable)"], ["Traceback"],
         mutate=lambda d: put(d, "plan-p1-post-2.json", '{"success": 200, "message": "ok", "data": ' + "{\"a\": " * 5000 + "1" + "}" * 5000 + "}"))
    case("hostile nesting in a line of the journal of the control (RecursionError of json.loads): nothing of the journal is believed", 1, ["PID 1's journal of the control was not used: a line of pid1-control.jsonl is not JSON"], ["Traceback"],
         mutate=lambda d: (put(d, "pid1-control.jsonl", "[" * 100000 + "]" * 100000 + "\n"), ledit(d, "units-after.tsv", lambda t: t + "systemd-timedated.service\tactive\trunning\t1\tinv-td\n")))

    # ---- more guards of the report that one case each holds ----
    for r_ in ("p2", "p3", "p4"):
        lacking_ = {"p2": V_ID_STABLE, "p3": V_ID_HOLD, "p4": V_ID_BACK}[r_]
        invalid("the POST of the plan %s answered 202 and curl was cut after the status: it did not begin a job that can be said to have begun" % r_, "the POST of the plan %s did not begin a job: cut (no reason)" % r_,
                lambda d, r=r_: api(d, "plan-%s-post" % r, code=202, curl=56, body=fxj("accepted")), lacks=["**PASS** " + lacking_])
        invalid("the status of the plan %s says the job started ten minutes after its POST" % r_, "the plan %s that the core answers at the end is not the job its POST began: started_at is +" % r_,
                lambda d, r=r_: jstatus(d, r, lambda x: x.update(started_at=iso(timestamp_of(d, r) + 600))))
        case("the plan %s that was made a second after its POST (the job was quick) is the job's" % r_, 0, ["**PASS** " + lacking_], ["FAIL", "INVALID"],
             mutate=lambda d, r=r_: jplan(d, r, lambda p: p.update(made_at=iso(timestamp_of(d, r) + 1), expires_at=iso(timestamp_of(d, r) + 1 + 86400))))
    for r_ in ("p1", "p2", "p3", "p4"):
        g_ = "plan-%s-get" % r_
        invalid("the end of the plan %s is not JSON, and the polls say it failed: the end cannot be judged" % r_, "the last poll of the plan %s said failed, and the answer kept as its end is not usable (unreadable)" % r_,
                lambda d, g=g_, r=r_: (ledit(d, "plan-%s.polls.tsv" % r, lambda t: t.replace("\tdone\t-", "\tfailed\tparse")), api(d, g, raw="<html>gateway</html>")))
        invalid("the end of the plan %s says it is done and the polls say it failed" % r_, "the last poll of the plan %s said failed, and the answer kept as its end is of the state done" % r_,
                lambda d, r=r_: ledit(d, "plan-%s.polls.tsv" % r, lambda t: t.replace("\tdone\t-", "\tfailed\tparse")))
    # a control with a lot of change in /var/log/casaos is the box's own: that tree is the one the plan may write, and the control does not count its rows
    case("twelve rows of /var/log/casaos that changed by themselves in the control are not noise that is counted", 0, ["**PASS** " + V_INV_FILES], ["FAIL", "INVALID"],
         mutate=lambda d: (ledit(d, "inv-ctl-a.tsv", lambda t: t + manifest_text({"/var/log/casaos/x%d" % i: ("f", "5", "1790000100000000000", "1790000100000000000", "644", "0:0", "7" * 64) for i in range(12)})),
                           ledit(d, "inv-ctl-b.tsv", lambda t: t + manifest_text({"/var/log/casaos/x%d" % i: ("f", "5", "1790000200000000000", "1790000200000000000", "644", "0:0", "8" * 64) for i in range(12)}))))
    case("five paths of /var/tmp that only one of the control's listings has are too many to believe", 1, ["INVALID RUN", "the two snapshots of the control (the paths of /var/tmp) differ in 5 rows that only one of them has, out of 7"], ["Traceback"],
         mutate=lambda d: put(d, "tmp-ctl-b.txt", slurp(d, "tmp-ctl-b.txt") + "".join("/var/tmp/systemd-private-n%d\n" % i for i in range(5))))
    # the nesting that is allowed: 32 levels, the envelope and its data among them
    nest_ = lambda k: "[" * k + "]" * k
    case("an answer nested exactly 32 deep is read", 0, ["**PASS** " + V_IDLE], ["FAIL", "INVALID"],
         mutate=lambda d: put(d, "plan-idle.json", '{"success": 200, "message": "ok", "data": {"plannable": true, "state": "idle", "x": ' + nest_(30) + "}}"))
    case("an answer nested 33 deep is not", 1, ["**FAIL** " + V_IDLE, "no usable answer (unreadable)"], ["Traceback"],
         mutate=lambda d: put(d, "plan-idle.json", '{"success": 200, "message": "ok", "data": {"plannable": true, "state": "idle", "x": ' + nest_(31) + "}}"))
    invalid("a row of the watching with a status of seven digits", "plan-p1.polls.tsv has a row that is not one of the watching", lambda d: ledit(d, "plan-p1.polls.tsv", lambda t: t + "1\t2\t1000000\t0\trunning\tupdate\n"))
    invalid("a row of the watching with a time that is infinite", "plan-p1.polls.tsv has a row that is not one of the watching", lambda d: ledit(d, "plan-p1.polls.tsv", lambda t: t + "inf\t2\t200\t0\trunning\tupdate\n"))
    invalid("a row of the watching with a time that is not a number", "plan-p1.polls.tsv has a row that is not one of the watching", lambda d: ledit(d, "plan-p1.polls.tsv", lambda t: t + "nan\t2\t200\t0\trunning\tupdate\n"))

    # ---- the box's own name is in no answer ----
    case("the hostname of the box is in the runbook: a leak, by name", 1, ["**FAIL** " + V_LEAK, "runbook-p1 says the box's hostname"], ["Traceback", "INVALID"], mutate=rb_text(lambda t: t + "\n# this is " + HOST + "\n"))
    for what_, kind_, needle_ in (("an IPv4 address", "ipv4", IPV4), ("the second IPv4 address (the Docker bridge)", "ipv4", "172.17.0.1"), ("the IPv6 address", "ipv6", IPV6), ("the MAC address", "mac", MAC), ("the user", "user", USER)):
        case("%s of the box is in the plan: a leak, by name" % what_, 1, ["**FAIL** " + V_LEAK, "plan-p1-get says the box's %s" % kind_], ["Traceback", "INVALID"], mutate=plan_edit(lambda p, n=needle_: p.update(surprise="reached from " + n)))
    case("the hostname is in the answer of the POST", 1, ["**FAIL** " + V_LEAK, "plan-p1-post says the box's hostname"], ["Traceback"], mutate=lambda d: jedit(d, "plan-p1-post.json", lambda j: j.update(message=HOST)))
    case("the hostname is in the failure of a step", 1, ["**FAIL** " + V_LEAK, "step-format-get says the box's hostname"], ["Traceback"], mutate=lambda d: jdata(d, "step-format-get", lambda x: x["error"].update(detail="the host " + HOST + " cannot")))
    case("the hostname in capital letters is the hostname", 1, ["**FAIL** " + V_LEAK], ["Traceback"], mutate=rb_text(lambda t: t + "\n# " + HOST.upper() + "\n"))
    case("the user as a part of another word is no leak (casaos, casa-x, mycasa, casa_y)", 0, ["**PASS** " + V_LEAK], ["FAIL"], mutate=plan_edit(lambda p: p.update(surprise="casaos casa-x mycasa casa_y")))
    case("the address inside a longer number is no leak (110.0.2.150)", 0, ["**PASS** " + V_LEAK], ["FAIL"], mutate=plan_edit(lambda p: p.update(surprise="110.0.2.150")))
    invalid("no needles to look for in the answers: the leak check proves nothing", "leak-needles.tsv has no hostname", lambda d: put(d, "leak-needles.tsv", "mac\t" + MAC + "\n"), silent=[V_LEAK], lacks=["**PASS** " + V_LEAK])
    invalid("no needles file at all", "leak-needles.tsv has no hostname", lambda d: remove(d, "leak-needles.tsv"), lacks=["**PASS** " + V_LEAK])
    invalid("no user among the needles", "leak-needles.tsv has no hostname, no IPv4 address or no user name", lambda d: put(d, "leak-needles.tsv", "hostname\t%s\nipv4\t%s\n" % (HOST, IPV4)))
    # the rows of leak-needles.tsv: a needle that is missing alone is a run that proves nothing, and a row with no kind or no value is no needle (an empty one would be found in every answer)
    for gone_ in ("hostname", "ipv4", "user"):
        invalid("no %s among the needles, and the rest of them there" % gone_, "leak-needles.tsv has no hostname, no IPv4 address or no user name",
                lambda d, g=gone_: ledit(d, "leak-needles.tsv", lambda t: "".join(ln for ln in t.splitlines(True) if not ln.startswith(g + "\t"))), silent=[V_LEAK], lacks=["**PASS** " + V_LEAK])
    for what_, row_ in (("an empty value", "mac\t\n"), ("a value of spaces", "mac\t   \n"), ("a value of a tab", "mac\t\t\n"), ("no kind (a value of the box's own files: a source line)", "\tdeb.debian.org\n"), ("a line with no tab", "deb.debian.org\n")):
        case("a row of leak-needles.tsv with %s is no needle" % what_, 0, ["**PASS** " + V_LEAK], ["FAIL", "INVALID"], mutate=lambda d, r=row_: put(d, "leak-needles.tsv", slurp(d, "leak-needles.tsv") + r))
    case("a needle that has spaces around it is the needle without them: the leak of the user is found", 1, ["**FAIL** " + V_LEAK, "plan-p1-get says the box's user"], ["INVALID", "Traceback"],
         mutate=lambda d: (put(d, "leak-needles.tsv", "hostname\t  %s \nipv4\t %s\nuser\t %s  \n" % (HOST, IPV4, USER)), plan_edit(lambda p: p.update(surprise="reached from " + USER))(d)))
    case("a runbook whose tree is not the plan's does not print what a URL of the box's file says of its user name, its password and its query", 1,
         ["**FAIL** " + V_RB_TREE, "***@deb.debian.org"], ["SECRETPW", "INVALID", "Traceback", "token=abc"],
         mutate=rb_text(lambda t: t.replace("deb http://deb.debian.org/debian bookworm main\n", "deb http://user:SECRETPW@deb.debian.org/debian?token=abc bookworm main\n", 1)))
    case("a failure of a job whose detail has a URL with a password is not printed with it", 1, ["**FAIL** " + f_step(STEP_BY_ID["format"]), "***@deb.debian.org"], ["SECRETPW", "Traceback"],
         mutate=lambda d: jdata(d, "step-format-get", lambda x: x["error"].update(detail="Err http://user:SECRETPW@deb.debian.org bookworm")))

    # ---- the cross-checks of a plan: counts and lists, kept back and its problem, the room that could not be read ----
    fails("removals that are empty with a count of 2 removed", V_SHAPE, plan_edit(lambda p: p.update(removals=[], verdict="ok", problems=[])), extra=["counts.dist_upgrade.removed is 2 and removals has 0"])
    fails("a list of what is kept back that has a name and a count that says none", V_SHAPE, plan_edit(lambda p: p.update(kept_back=[HOLD])), extra=["counts.dist_upgrade.kept_back is 0 and kept_back has 1"])
    fails("new packages that are empty with a count of 75 new", V_SHAPE, plan_edit(lambda p: p.update(new_packages=[])), extra=["counts.dist_upgrade.new is 75 and new_packages has 0"])
    fails("one new package more than the count says", V_SHAPE, plan_edit(lambda p: p["new_packages"].append("one-more")), extra=["counts.dist_upgrade.new is 75 and new_packages has 76"])
    fails("a list cut at 600 names that is longer than its count", V_SHAPE, plan_edit(lambda p: p.update(new_packages=["p%03d" % i for i in range(600)], new_truncated=True)), extra=["counts.dist_upgrade.new is 75 and new_packages has 600"])
    fails("a flag of truncation that is not true or false", V_SHAPE, plan_edit(lambda p: p.update(new_truncated="yes")), extra=["new_truncated is not true or false"])
    fails("a name kept back with a count that says one and no problem for it", V_VERDICT_RULE,
          plan_edit(lambda p: (p.update(kept_back=[HOLD]), p["counts"]["dist_upgrade"].update(kept_back=1))), extra=["base-files is kept back and there is no dist_kept_back problem"])
    fails("the same, with a count of one that is inside neither side of the measured zero", V_COUNTS_DIST,
          plan_edit(lambda p: (p.update(kept_back=[HOLD]), p["counts"]["dist_upgrade"].update(kept_back=1))))
    case("a list that is cut and shorter than its count is a list that was cut", 1, ["**PASS** " + V_SHAPE], ["INVALID", "**FAIL** " + V_SHAPE],
         mutate=plan_edit(lambda p: (p.update(new_packages=["p%03d" % i for i in range(100)], new_truncated=True), p["counts"]["dist_upgrade"].update(new=700))))
    case("a list that was cut and has as many names as its count is a list that was cut", 0, ["**PASS** " + V_SHAPE], ["FAIL"], mutate=plan_edit(lambda p: p.update(new_truncated=True)))
    BLOCKED_ = lambda p, problem: p.update(verdict="blocked", problems=p["problems"] + [problem])
    for code_, data_ in (("disk_short", {"path": "/usr", "need": 5, "free": 1}), ("disk_short", {"path": "/usr", "need": 5, "unknown": True}), ("dist_kept_back", {"names": [HOLD]}),
                         ("removal_protected", {"name": "libsemanage1", "pattern": "lib*"}), ("removal_essential", {"name": "libsemanage1"})):
        case("a %s problem with the severity of the contract (blocked)%s passes the rule, and blocks the plan" % (code_, " and the room unknown" if "unknown" in data_ else ""), 1, ["**PASS** " + V_VERDICT_RULE, "**FAIL** " + V_VERDICT], ["INVALID", "Traceback"],
             mutate=plan_edit(lambda p, c=code_, x=data_: (BLOCKED_(p, {"code": c, "severity": "blocked", "data": x}),
                                                           c == "dist_kept_back" and (p.update(kept_back=list(x["names"])), p["counts"]["dist_upgrade"].update(kept_back=len(x["names"]))))))
    case("a downgrade problem with the severity of the contract (blocked) passes the rule, and blocks the plan", 1, ["**PASS** " + V_VERDICT_RULE, "**FAIL** " + V_VERDICT], ["INVALID", "Traceback"],
         mutate=plan_edit(lambda p: BLOCKED_(p, {"code": "downgrade", "severity": "blocked", "data": {"names": ["libfoo"]}})))
    fails("a downgrade problem that only needs acceptance is not the contract's severity", V_VERDICT_RULE, plan_edit(lambda p: p["problems"].append({"code": "downgrade", "severity": "needs_acceptance", "data": {"names": ["libfoo"]}})))
    for what_, data_ in (("free and unknown true together", {"path": "/usr", "need": 5, "free": 1, "unknown": True}), ("neither free nor unknown", {"path": "/usr", "need": 5}),
                         ("no path", {"need": 5, "free": 1}), ("a need that is a text", {"path": "/usr", "need": "5", "free": 1}), ("unknown that is a text", {"path": "/usr", "need": 5, "unknown": "yes"})):
        fails("a disk_short problem with %s" % what_, V_VERDICT_RULE, plan_edit(lambda p, x=data_: BLOCKED_(p, {"code": "disk_short", "severity": "blocked", "data": x})), extra=["disk_short has not path, need and free"])
    case("a disk row of a room that could not be read (unknown true, no free, not ok) is a shape, and fails the disk by saying so", 1, ["**PASS** " + V_SHAPE, "**FAIL** " + V_DISK, "its room could not be read"], ["INVALID", "Traceback"],
         mutate=plan_edit(lambda p: p["disk"][1].update(ok=False, unknown=True) or p["disk"][1].pop("free")))
    fails("a disk row of a room that could not be read and is said to be ok", V_DISK, plan_edit(lambda p: p["disk"][1].update(unknown=True)), extra=["unknown"])
    fails("a disk row whose unknown is a text", V_SHAPE, plan_edit(lambda p: p["disk"][1].update(unknown="yes")))
    fails("a disk row that has no free and does not say that the room is unknown", V_SHAPE, plan_edit(lambda p: p["disk"][1].pop("free")))
    case("a disk row with free and unknown false is a row", 0, ["**PASS** " + V_DISK], ["FAIL"], mutate=plan_edit(lambda p: p["disk"][1].update(unknown=False)))

    # ---- what the core says that a clean box does not make it say: its own answers, dumped from it (fixtures/plan/plan-*.json, made of the real output of apt in testdata/real) ----
    def real_plan(name):
        """the plan of a fixture of the core's own in the place of the first plan's, with the times and the sandbox of the leg (what plan_get does of the plan it has)"""
        def edit(p):
            keep = {k: p[k] for k in ("made_at", "expires_at", "sandbox")}
            p.clear()
            p.update(fxj(name)["data"]["plan"])
            p.update(keep)
        return plan_edit(edit)

    case("the core's own plan for a downgrade (a package on hold at a version above the pin's): its shape and its rule pass, and the plan is blocked by it", 1,
         ["**PASS** " + V_SHAPE, "**PASS** " + V_VERDICT_RULE, "**FAIL** " + V_VERDICT + ': blocked; the problems: downgrade {"names": ["docker-ce"]}'], ["INVALID", "Traceback"], mutate=real_plan("plan-downgrade"))
    case("the core's own plan for a package that apt counts as not upgraded and does not name: one kept back with no name, said in data.unlisted, agrees with the count of its list", 1,
         ["**PASS** " + V_SHAPE, "**PASS** " + V_VERDICT_RULE, "**FAIL** " + V_VERDICT + ': blocked; the problems: dist_kept_back {"names": [], "unlisted": 1}'], ["INVALID", "Traceback"], mutate=real_plan("plan-kept_back_unlisted"))
    case("the core's own plan whose sizes apt did not say: no disk rows, and the note that says why (estimates_unknown) is the shape; the sizes and the disk are what fail", 1,
         ["**PASS** " + V_SHAPE, "**FAIL** " + V_ESTIMATES + ": estimates.known is false: apt did not say its sizes", "**FAIL** " + V_DISK + ": the rows are none"], ["INVALID", "Traceback"], mutate=real_plan("plan-estimates_unknown"))
    case("the core's own plan for a room that could not be read (free 0 and unknown true in the row, no free in the problem) next to one that was read and is short", 1,
         ["**PASS** " + V_SHAPE, "**PASS** " + V_VERDICT_RULE, "**FAIL** " + V_DISK, "/ needs 512 MB and its room could not be read", "/usr needs 1453 MB and has 1024 MB"], ["INVALID", "Traceback"], mutate=real_plan("plan-disk_unknown"))
    case("the core's own plan of a box with a file of apt's configuration for each of the six reasons: every reason of the contract is read", 1, ["**PASS** " + V_SHAPE, "**PASS** " + V_VERDICT_RULE], ["INVALID", "Traceback"], mutate=real_plan("plan-conf_reasons"))
    # the same answers, one thing wrong in each
    UNL = lambda **kw: plan_edit(lambda p: (p.update(kept_back=[], verdict="blocked", problems=[{"code": "dist_kept_back", "severity": "blocked", "data": dict({"names": []}, **kw)}] + p["problems"]),
                                            p["counts"]["dist_upgrade"].update(kept_back=1)))
    case("one package kept back and not named, with data.unlisted of 1: the count of the list and the count of the plan agree", 1, ["**PASS** " + V_SHAPE, "**PASS** " + V_VERDICT_RULE], ["INVALID", "Traceback"], mutate=UNL(unlisted=1))
    fails("one package kept back and not named, and the problem does not say that apt did not name it", V_SHAPE, UNL(), extra=["counts.dist_upgrade.kept_back is 1 and kept_back has 0"])
    fails("data.unlisted says two where the count says one", V_SHAPE, UNL(unlisted=2), extra=["counts.dist_upgrade.kept_back is 1 and kept_back has 0 (and 2 that apt does not name)"])
    for what_, value_ in (("a text", "1"), ("zero", 0), ("a negative number", -1), ("true", True), ("a number with a fraction", 1.5), ("a list", [1])):
        fails("data.unlisted is %s (on a plan whose count and list agree without it)" % what_, V_VERDICT_RULE,
              plan_edit(lambda p, v=value_: (p.update(kept_back=[HOLD], verdict="blocked", problems=[{"code": "dist_kept_back", "severity": "blocked", "data": {"names": [HOLD], "unlisted": v}}] + p["problems"]),
                                             p["counts"]["dist_upgrade"].update(kept_back=1))), extra=["dist_kept_back says unlisted"])
    # a package that the minimal upgrade keeps back and the full upgrade names in no list and counts nowhere is left behind: the problem names it, and no list and no count of the plan has it (the core's leftBehind)
    KB = lambda names, **kw: plan_edit(lambda p: (p.update(verdict="blocked", problems=[{"code": "dist_kept_back", "severity": "blocked", "data": {"names": list(names)}}] + p["problems"], **kw)))
    case("a name that the problem has and no list of the plan has, with every count at zero, is a package that is left behind: the shape and the rule pass, and the plan is blocked", 1,
         ["**PASS** " + V_SHAPE, "**PASS** " + V_VERDICT_RULE, "**FAIL** " + V_VERDICT + ': blocked; the problems: dist_kept_back {"names": ["' + HOLD + '"]}'], ["INVALID", "Traceback"], mutate=KB([HOLD]))
    fails("the plan's own list of what is kept back is not inside the names of the problem", V_VERDICT_RULE,
          plan_edit(lambda p: (p.update(kept_back=[HOLD], verdict="blocked", problems=[{"code": "dist_kept_back", "severity": "blocked", "data": {"names": ["libfoo"]}}] + p["problems"]), p["counts"]["dist_upgrade"].update(kept_back=1))),
          extra=["dist_kept_back names libfoo and the plan says that base-files is kept back"])
    case("the plan's own list of what is kept back and a package that is left behind, sorted, are the names of the problem", 1, ["**PASS** " + V_SHAPE, "**PASS** " + V_VERDICT_RULE], ["INVALID", "Traceback"],
         mutate=plan_edit(lambda p: (p.update(kept_back=[HOLD], verdict="blocked", problems=[{"code": "dist_kept_back", "severity": "blocked", "data": {"names": ["apt-utils", HOLD, "zzz-left"]}}] + p["problems"]), p["counts"]["dist_upgrade"].update(kept_back=1))))
    fails("the names of the problem are not sorted", V_VERDICT_RULE, KB(["b-left", "a-left"]), extra=["dist_kept_back names b-left, a-left, which is not sorted and without repeats"])
    fails("a name of the problem twice", V_VERDICT_RULE, KB(["a-left", "a-left"]), extra=["which is not sorted and without repeats"])
    fails("a package that is said left behind and that the plan removes", V_VERDICT_RULE, KB(["libsemanage1"]), extra=["dist_kept_back names libsemanage1, which the plan says it removes, installs or downgrades"])
    fails("a package that is said left behind and that the plan installs", V_VERDICT_RULE, lambda d: (KB(["a-new-one"])(d), plan_edit(lambda p: p["new_packages"].__setitem__(0, "a-new-one"))(d)), extra=["dist_kept_back names a-new-one, which the plan says it removes, installs or downgrades"])
    fails("a package that is said left behind and that the plan downgrades", V_VERDICT_RULE,
          plan_edit(lambda p: p.update(verdict="blocked", problems=[{"code": "dist_kept_back", "severity": "blocked", "data": {"names": ["libfoo"]}}, {"code": "downgrade", "severity": "blocked", "data": {"names": ["libfoo"]}}] + p["problems"])),
          extra=["dist_kept_back names libfoo, which the plan says it removes, installs or downgrades"])
    case("names that are cut at 600 are not asked to hold the plan's list: the cut may have taken some of it", 1, ["**PASS** " + V_SHAPE, "**PASS** " + V_VERDICT_RULE], ["INVALID", "Traceback"],
         mutate=plan_edit(lambda p: (p.update(kept_back=["zzz-kept"], verdict="blocked", problems=[{"code": "dist_kept_back", "severity": "blocked", "data": {"names": ["a%03d-left" % i for i in range(600)]}}] + p["problems"]), p["counts"]["dist_upgrade"].update(kept_back=1))))
    fails("599 names that do not have the plan's list", V_VERDICT_RULE,
          plan_edit(lambda p: (p.update(kept_back=["zzz-kept"], verdict="blocked", problems=[{"code": "dist_kept_back", "severity": "blocked", "data": {"names": ["a%03d-left" % i for i in range(599)]}}] + p["problems"]), p["counts"]["dist_upgrade"].update(kept_back=1))),
          extra=["says that zzz-kept is kept back"])
    case("the core's own plan for a package that is left behind (testdata/real/chroot-d11-silent-*): no kept back package, the problem names it, the plan is blocked", 1,
         ["**PASS** " + V_SHAPE, "**PASS** " + V_VERDICT_RULE, "**FAIL** " + V_VERDICT + ': blocked; the problems: dist_kept_back {"names": ["osp-k"]}'], ["INVALID", "Traceback"], mutate=real_plan("plan-kept_back_silent"))
    case("the core's own plan for an estimate that apt ends for lack of room in the sandbox's cache: the sizes are known, the rows of the disk are there, and the box that is short is blocked by them", 1,
         ["**PASS** " + V_SHAPE, "**PASS** " + V_VERDICT_RULE, "**PASS** " + V_ESTIMATES, "**FAIL** " + V_DISK, "/usr needs 1453 MB and has 1024 MB"], ["INVALID", "Traceback", "estimates_unknown", "could not be read"], mutate=real_plan("plan-lowroom"))
    case("the core's own plan of a box with nothing to do (apt ends the estimate with status 0 and says no sizes): the sizes are known and zero, with no note and the rows of the disk at the floors, which is the shape; "
         "what fails on this leg's box is the sizes of nothing, and the counts of nothing", 1,
         ["**PASS** " + V_SHAPE, "**PASS** " + V_VERDICT_RULE, "**FAIL** " + V_ESTIMATES + ": a download of 0 MB, not between 100 MB and 2 GB, 0 MB of room taken, not between 0 and 8 GB", "**PASS** " + V_DISK],
         ["INVALID", "Traceback", "estimates_unknown", "apt did not say its sizes"], mutate=real_plan("plan-nothing_to_do"))
    fails("the sizes of a box with nothing to do, and the plan says that they are not known (the word of the sizes apt did not say)", V_SHAPE, lambda d: (real_plan("plan-nothing_to_do")(d), plan_edit(lambda p: p.update(disk_note="estimates_unknown"))(d)),
          extra=["estimates.known is true and disk_note says estimates_unknown"])
    fails("a count of what is kept back that is more than the list and no problem for it", V_SHAPE, plan_edit(lambda p: p["counts"]["dist_upgrade"].update(kept_back=1)), extra=["counts.dist_upgrade.kept_back is 1 and kept_back has 0"])
    fails("a dist_kept_back problem and nothing kept back", V_VERDICT_RULE, plan_edit(lambda p: p.update(verdict="blocked", problems=[{"code": "dist_kept_back", "severity": "blocked", "data": {"names": []}}] + p["problems"])),
          extra=["there is a dist_kept_back problem and nothing is kept back"])
    for what_, data_ in (("no names", {}), ("names that are a text", {"names": "libfoo"}), ("an empty list of names", {"names": []}), ("a name that is a number", {"names": [1]}), ("a name with a control character", {"names": ["a\nb"]})):
        fails("a downgrade problem with %s" % what_, V_VERDICT_RULE, plan_edit(lambda p, x=data_: BLOCKED_(p, {"code": "downgrade", "severity": "blocked", "data": x})), extra=["downgrade has not names as a list of short texts"])
    for what_, code_, sev_, data_ in (("no name", "removal_essential", "blocked", {}), ("no pattern", "removal_protected", "blocked", {"name": "libsemanage1"}), ("no version", "removal_manual", "needs_acceptance", {"name": "libsemanage1"}),
                                      ("a name that is a number", "removal_manual", "needs_acceptance", {"name": 7, "version": "1"})):
        fails("a %s problem with %s" % (code_, what_), V_VERDICT_RULE, plan_edit(lambda p, c=code_, s=sev_, x=data_: p.update(problems=p["problems"] + [{"code": c, "severity": s, "data": x}], verdict="blocked" if s == "blocked" else p["verdict"])),
              extra=["%s has not" % code_])
    fails("a docker_held problem with no names", V_VERDICT_RULE, plan_edit(lambda p: p["problems"].append({"code": "docker_held", "severity": "info", "data": {}})), extra=["docker_held has not names as a list of short texts"])

    # ---- the status: unknown (the core could not tell whether a plan can be made) and sandbox_left_behind (the directory it could not remove) ----
    UNKNOWN_STATUS = fxj("unknown")["data"]
    LEFT_DIR = fxj("plan-left_behind")["data"]["sandbox_left_behind"]
    check("the core's own answers that say unknown and sandbox_left_behind are what the cases below take them from: not plannable with no reason, and the directory of a sandbox of /var/tmp",
          UNKNOWN_STATUS == {"plannable": False, "unknown": True, "state": "idle"} and re.fullmatch(r"/var/tmp/casaos-os-plan\.[0-9a-f]+", LEFT_DIR) is not None
          and fxj("failed-left_behind")["data"]["sandbox_left_behind"] == LEFT_DIR and fxj("failed-left_behind")["data"]["error"] == fxj("failed-sandbox_update_failed")["data"]["error"], "")
    case("the first GET says unknown (the core could not tell): the run is invalid, and the box is not said to be one that cannot be planned", 1,
         ["INVALID RUN", "plan-idle says unknown true"], ["**FAIL** " + V_IDLE, "**PASS** " + V_IDLE], mutate=lambda d: jdata(d, "plan-idle", lambda x: (x.clear(), x.update(UNKNOWN_STATUS))))
    case("the end of the first plan says unknown (the question was asked again and went wrong): the run is invalid, and the plan's shape is not failed for it", 1,
         ["INVALID RUN", "plan-p1-get says unknown true", "**PASS** " + V_SHAPE], ["**FAIL**"], mutate=lambda d: jstatus(d, "p1", lambda x: x.update(plannable=False, unknown=True)))
    case("a status that says unknown and is plannable too", 1, ["**FAIL** " + V_SHAPE, "unknown is true with plannable true or with a reason"], [], mutate=lambda d: jstatus(d, "p1", lambda x: x.update(unknown=True)))
    case("a status that says unknown and gives a reason", 1, ["**FAIL** " + V_SHAPE, "unknown is true with plannable true or with a reason"], [],
         mutate=lambda d: jstatus(d, "p1", lambda x: x.update(plannable=False, unknown=True, reason="not_root")))
    fails("a status that says unknown with a text", V_SHAPE, lambda d: jstatus(d, "p1", lambda x: x.update(unknown="yes")), extra=["unknown is not true or false"])
    fails("a status that is not plannable and says no reason and no unknown", V_SHAPE, lambda d: jstatus(d, "p1", lambda x: x.update(plannable=False)), extra=["plannable is not true"])
    fails("a status that is not plannable for a reason", V_SHAPE, lambda d: jstatus(d, "p1", lambda x: x.update(plannable=False, reason="not_root")), extra=["plannable is not true, reason not_root"])
    case("the end of the first plan says that the core could not remove the sandbox's directory: the directory is a text, and /var/tmp is not said to be the same after it", 1,
         ["**PASS** " + V_SHAPE, "**FAIL** " + V_INV_TMP, "the core says it could not remove %s after the plan" % LEFT_DIR], ["INVALID", "Traceback"], mutate=lambda d: jstatus(d, "p1", lambda x: x.update(sandbox_left_behind=LEFT_DIR)))
    for what_, value_ in (("a number", 5), ("nothing", ""), ("a text with a line feed", "/var/tmp/a\nb"), ("a text of 500 characters", "/" + "a" * 500), ("a list", ["/var/tmp/x"])):
        fails("sandbox_left_behind is %s" % what_, V_SHAPE, lambda d, v=value_: jstatus(d, "p1", lambda x: x.update(sandbox_left_behind=v)), extra=["sandbox_left_behind is not a short text"])
    for run_ in ("p2", "p3", "p4"):
        case("the end of the plan %s says that the core could not remove the sandbox's directory" % run_, 1, ["**FAIL** " + V_STEP_LEFTOVER, "%s: the core says it could not remove %s" % (run_, LEFT_DIR)], ["INVALID", "Traceback"],
             mutate=lambda d, r=run_: jstatus(d, r, lambda x: x.update(sandbox_left_behind=LEFT_DIR)))
    case("the end of the job of a step says that the core could not remove the sandbox's directory (the core's own answer for it): the leftover of the step fails by the core's word, though /var/tmp is clean", 1,
         ["**FAIL** " + V_STEP_LEFTOVER, "deadmirror: the core says it could not remove %s" % LEFT_DIR, "**PASS** " + f_step(STEP_BY_ID["deadmirror"])], ["INVALID", "Traceback"],
         mutate=lambda d: jdata(d, "step-deadmirror-get", lambda x: (x.clear(), x.update(fxj("failed-left_behind")["data"]))))

    # ---- the tolerance of the counts is rounded up, and the report says so and prints the range it allowed ----
    case("the range of the counts that the report allowed is in its detail, and the name says it is rounded up", 0,
         ["**PASS** " + V_COUNTS_DIST + ": 384 upgraded, 75 new, 2 removed, 0 kept back (the range allowed: 326 to 442, 63 to 87, 1 to 3, 0 to 0)",
          "**PASS** " + V_COUNTS_UP + ": 274 upgraded, 0 new, 0 removed, 111 kept back (the range allowed: 232 to 316, 0 to 0, 0 to 0, 94 to 128)"], ["FAIL"])
    fails("a count that is off by more than the range says so in the range's own words", V_COUNTS_DIST, plan_edit(lambda p: set_count(p, "dist_upgrade", "removed", 4)), extra=["off by more than 15 % (rounded up to a whole package): removed 4 (measured 2, 1 to 3)"])

    # ---- the second POST answers the SAME job ----
    fails("the second POST answers the running job with another started_at", V_POST_TWICE, lambda d: jdata(d, "plan-p1-post-2", lambda x: x.update(started_at="2026-10-12T09:29:07Z")), extra=["it is not the same job"])
    fails("the second POST answers with no started_at", V_POST_TWICE, lambda d: jdata(d, "plan-p1-post-2", lambda x: x.pop("started_at")), extra=["it is not the same job"])
    fails("the second POST answers with a started_at that is null", V_POST_TWICE, lambda d: jdata(d, "plan-p1-post-2", lambda x: x.update(started_at=None)), extra=["it is not the same job"])
    fails("the second POST answers with a started_at that is not a time", V_POST_TWICE, lambda d: (jdata(d, "plan-p1-post", lambda x: x.update(started_at="soon")), jdata(d, "plan-p1-post-2", lambda x: x.update(started_at="soon"))), extra=["it is not the same job"])

    # ---- the control cannot be a snapshot that is cut short ----
    TREES_ONLY = lambda t: "".join(ln + "\n" for ln in t.splitlines() if json.loads(ln.split("\t")[0]) in ("/etc", "/var/lib/dpkg", "/var/lib/apt", "/var/cache/apt", "/var/lib/casaos", "/var/log/apt", "/var/log/casaos"))
    case("a control whose first snapshot holds the trees and nothing else is not believed, and the digest of a file of /etc that changed in the window is a FAIL all the same", 1,
         ["FAIL and INVALID RUN", "the two snapshots of the control (the files) differ in 11 rows that only one of them has, out of 19", "**FAIL** " + V_INV_FILES, "changed /etc/fstab (digest)"], ["Traceback"],
         mutate=lambda d: (ledit(d, "inv-ctl-a.tsv", TREES_ONLY), ledit(d, "inv-after.tsv", row_edit("/etc/fstab", digest="9" * 64))))
    case("a control whose units list was cut to two rows is not believed, and the services the plan restarted are not excused by it", 1,
         ["FAIL and INVALID RUN", "the two snapshots of the control (the units) differ in 6 rows that only one of them has, out of 8", "**FAIL** " + V_INV_UNITS, "ssh.service", "docker.service"], ["Traceback"],
         mutate=lambda d: (ledit(d, "units-ctl-a.tsv", lambda t: "".join(t.splitlines(True)[:2])), unit("after", "ssh.service", inv="inv-new")(d), unit("after", "docker.service", inv="inv-new")(d)))
    case("a control whose listening sockets were cut to one is not believed", 1, ["INVALID RUN", "the two snapshots of the control (the listening sockets) differ in 7 rows"], ["Traceback"],
         mutate=lambda d: put(d, "listen-ctl-a.txt", "tcp 0.0.0.0:22\n"))
    case("a control whose snapshot of /var/tmp is cut (two rows) is within what a box does by itself", 0, ["**PASS** " + V_INV_TMP], ["FAIL", "INVALID"], mutate=lambda d: put(d, "tmp-ctl-a.txt", ""))
    case("a few rows that only one of the control's snapshots has (three) are the box's own, left out, and named", 0, ["**PASS** " + V_INV_FILES, "- files: /var/lib/casaos/db/a1, /var/lib/casaos/db/a2, /var/lib/casaos/db/a3"], ["FAIL", "INVALID"],
         mutate=lambda d: ledit(d, "inv-ctl-b.tsv", lambda t: t + manifest_text({"/var/lib/casaos/db/a%d" % i: ("f", "5", "1790000999000000000", "1790000999000000000", "644", "0:0", "7" * 64) for i in (1, 2, 3)})))
    case("four rows that only one of the control's snapshots has, out of 20, are too many to believe", 1, ["INVALID RUN", "differ in 4 rows that only one of them has, out of 23"], ["Traceback"],
         mutate=lambda d: ledit(d, "inv-ctl-b.tsv", lambda t: t + manifest_text({"/var/lib/casaos/db/a%d" % i: ("f", "5", "1790000999000000000", "1790000999000000000", "644", "0:0", "7" * 64) for i in (1, 2, 3, 4)})))
    case("a file that appeared in the control (only the second snapshot has it) and changes in the window is left out: created rows are noise", 0, ["**PASS** " + V_INV_FILES, "- files: /var/lib/casaos/db/new.db"], ["FAIL", "INVALID"],
         mutate=lambda d: (ledit(d, "inv-ctl-b.tsv", lambda t: t + manifest_text({"/var/lib/casaos/db/new.db": ("f", "5", "1790000500000000000", "1790000500000000000", "644", "0:0", "7" * 64)})),
                           ledit(d, "inv-before.tsv", lambda t: t + manifest_text({"/var/lib/casaos/db/new.db": ("f", "5", "1790000500000000000", "1790000500000000000", "644", "0:0", "7" * 64)})),
                           ledit(d, "inv-after.tsv", lambda t: t + manifest_text({"/var/lib/casaos/db/new.db": ("f", "9", "1790000700000000000", "1790000700000000000", "644", "0:0", "8" * 64)}))))
    case("a file that went away in the control (only the first snapshot has it) is noise too, and a plan that removes it is not blamed", 0, ["**PASS** " + V_INV_FILES, "- files: /var/lib/casaos/db/old.db"], ["FAIL", "INVALID"],
         mutate=lambda d: (ledit(d, "inv-ctl-a.tsv", lambda t: t + manifest_text({"/var/lib/casaos/db/old.db": ("f", "5", "1790000500000000000", "1790000500000000000", "644", "0:0", "7" * 64)})),
                           ledit(d, "inv-before.tsv", lambda t: t + manifest_text({"/var/lib/casaos/db/old.db": ("f", "5", "1790000500000000000", "1790000500000000000", "644", "0:0", "7" * 64)}))))
    case("a control in which most of the rows changed (the box was not quiet) is not believed", 1, ["INVALID RUN", "the files changed by themselves in 14 of 19 rows during the control"], ["Traceback"],
         mutate=lambda d: ledit(d, "inv-ctl-b.tsv", lambda t: "".join("\t".join(ln.split("\t")[:7] + ["f" * 64]) + "\n" if not json.loads(ln.split("\t")[0]).startswith("/var/log") else ln + "\n" for ln in t.splitlines())))
    case("a sub-state that changed and nothing else is a change of a unit", 1, ["**FAIL** " + V_INV_UNITS, "ssh.service went from active/running"], ["Traceback", "INVALID"], mutate=unit("after", "ssh.service", sub="exited"))

    # ---- the stamp of the box that changed by itself during the control is not the plan's to answer for ----
    case("the stamp changed by itself in the control and not in the window: not testable, with the reason, and no FAIL", 0, ["**NOT TESTABLE** " + V_STAMP + ": the stamp changed by itself during the control"], ["FAIL", "INVALID", "the stamp is untouched"],
         mutate=inv("ctl-b", row_edit("/var/lib/apt/periodic/update-success-stamp", mtime="1790000555000000000")), stamp=True)
    case("the stamp changed by itself in the control and again in the window: not testable too (the plan's touch cannot be told from the box's)", 0, ["**NOT TESTABLE** " + V_STAMP + ": the stamp changed by itself during the control"], ["FAIL", "INVALID"],
         mutate=lambda d: (inv("ctl-b", row_edit("/var/lib/apt/periodic/update-success-stamp", mtime="1790000555000000000"))(d), inv("after", row_edit("/var/lib/apt/periodic/update-success-stamp", mtime="1790000666000000000"))(d)), stamp=True)

    # ---- the times, the shape and the identity of a plan: one case for each guard ----
    fails("the plan expires before it was made", V_TIMES, lambda d: (jplan(d, "p1", lambda p: p.update(expires_at=iso(timestamp_of(d, "p1") + 70))), jstatus(d, "p1", lambda x: x.update(expires_at=iso(timestamp_of(d, "p1") + 70)))), extra=["expires_at is"])
    fails("a problem whose severity is a number", V_SHAPE, plan_edit(lambda p: p["problems"][0].update(severity=5)))
    fails("a problem whose code is a number", V_SHAPE, plan_edit(lambda p: p["problems"][0].update(code=5)))
    fails("a problem whose code is a list (it cannot be looked up, and the report does not crash on it)", V_SHAPE, plan_edit(lambda p: p["problems"][0].update(code=["removal_manual"])))
    fails("a hold of two names (the one that was held and one that was not) is not the hold", V_ID_HOLD, lambda d: jplan(d, "p3", lambda p: p.update(holds=[HOLD, "libc6"])))
    fails("a file of the sources that the plan says twice", V_SOURCES, plan_edit(lambda p: p["sources"]["files"].append(dict(p["sources"]["files"][0]))))
    fails("the answer at the end of the first plan says success 202 though its status is 200", V_SHAPE, lambda d: jedit(d, "plan-p1-get.json", lambda j: j.update(success=202)), extra=["success is 202, not 200"])

    # ---- the report's own computation of the sources, against what the core's rewrite did of boxes of six shapes ----
    with open(REPORT, newline="") as f:
        report_text = f.read().replace("\r\n", "\n")
    start_ = report_text.find("class NotComputable")
    stop_ = report_text.find("def box_sources():")
    masks_, masks_end_ = report_text.find("URL_IN_TEXT = re.compile"), report_text.find("def cell(x, n=100):")
    if start_ < 0 or stop_ < start_ or not 0 <= masks_ < masks_end_:
        check("the report's computation of the sources can be found in its text, to be tested against the core's rewrite", False, "")
    else:
        ns = {}
        # the body of the report is in a try: its lines have their indent, which the slice loses
        start_, stop_ = report_text.rfind("\n", 0, start_) + 1, report_text.rfind("\n", 0, stop_) + 1
        exec("import hashlib, json, math, os, re, sys\nMAX_SHOWN = 400\nL = %r\n" % dict(codename="bullseye", to_codename="bookworm") + report_text[masks_:masks_end_] + textwrap.dedent(report_text[start_:stop_]), ns)
        check("the report masks the user name, the password and the query of a URL in the text it prints, and keeps the scheme, the host, the port and the path",
              ns["shown"]("deb http://user:pw@deb.debian.org/debian?token=abc bookworm main # https://u@h/p") == "deb http://***@deb.debian.org/debian?*** bookworm main # https://***@h/p"
              and ns["mask_url"]("http://h/p") == "http://h/p" and ns["mask_url"]("http://a:b@h:80/p?q=1&r=2") == "http://***@h:80/p?***" and ns["mask_url"]("http://h?x") == "http://h?***"
              and ns["mask_url"]("ftp://a@b@c/d") == "ftp://***@c/d" and len(ns["shown"]("x" * 500)) == 400 and ns["shown"]("x" * 400) == "x" * 400, "")
        check("the report takes for the distribution's own the archive hosts of Debian's list by their name, as the core does, and no other host under debian.org",
              all(ns["official"](h) for h in ("deb.debian.org", "security.debian.org", "archive.debian.org", "httpredir.debian.org", "ftp.debian.org", "ftp.de.debian.org", "ftp2.fr.debian.org", "ftp9.zz.debian.org", "cdn-fastly.deb.debian.org"))
              and not any(ns["official"](h) for h in ("snapshot.debian.org", "people.debian.org", "debian.org", "deb.debian.org.example.com", "xdeb.debian.org", "ftp.germany.debian.org", "ftp10.de.debian.org", "cdn-fastly2.deb.debian.org", "cdn-.deb.debian.org", "cdn-fastly.deb.debian.org.example.com", "DEB.debian.org", "")), "")
        # the grammar of apt's configuration that the report computes with, against what the core's own does of the same files (fixtures/plan/conf-cases.json, dumped from the core: the core's
        # own cases of its grammar and the ones of this harness)
        cs_, ce_ = report_text.find("CONF_STATEMENT = re.compile"), report_text.find("def expected_left_out():")
        if not 0 <= cs_ < ce_:
            check("the report's grammar of apt's configuration can be found in its text, to be held against the core's", False, "")
        else:
            ns_conf = {}
            exec("import re\nCONF_MAX_BYTES = 64 << 10\n" + textwrap.dedent(report_text[report_text.rfind("\n", 0, cs_) + 1:report_text.rfind("\n", 0, ce_) + 1]), ns_conf)
            with open(os.path.join(FIXTURES, "conf-cases.json"), encoding="utf-8") as f_:
                conf_cases = json.load(f_)
            conf_diffs = [(c_["name"], c_["reason"], ns_conf["conf_reason"](bytes.fromhex(c_["hex"]))) for c_ in conf_cases if ns_conf["conf_reason"](bytes.fromhex(c_["hex"])) != c_["reason"]]
            check("the report's grammar of apt's configuration says of each of the %d files of conf-cases.json what the core's says: the reason it is left out, or that it is kept" % len(conf_cases),
                  not conf_diffs and len(conf_cases) >= 1000, conf_diffs[:3])
            with open(os.path.join(FIXTURES, "conf-inputs.json"), encoding="utf-8") as f_:
                conf_inputs = json.load(f_)
            check("conf-cases.json is the core's answer to each file of conf-inputs.json, the generated ones included, in the same order (a corpus that changed and a dump that was not made again is seen)",
                  [(c_["name"], c_["hex"]) for c_ in conf_cases] == [(c_["name"], c_["hex"]) for c_ in conf_inputs] and sum(c_["name"].startswith("generated: ") for c_ in conf_inputs) >= 1000, "")
            with open(os.path.join(FIXTURES, "conf-settings.json"), encoding="utf-8") as f_:
                core_settings = json.load(f_)
            check("the allow-list of the report (SANDBOX_APT_SETTINGS) is the core's sandboxAptSettings, entry by entry and in its order: for each download method what its program reads, what is kept of one host, of Acquire:: itself, and the whole names",
                  json.loads(json.dumps(ns_conf["SANDBOX_APT_SETTINGS"])) == core_settings, (ns_conf["SANDBOX_APT_SETTINGS"], core_settings))
            HTTP_ = ["Proxy", "Timeout", "Pipeline-Depth", "Dl-Limit", "Max-Age", "No-Cache", "AllowRedirect"]
            check("the core's table is what the programs of apt read: http and https the seven settings, ftp the proxy and the time to wait and no more, the proxy of one host, and nothing else per method",
                  core_settings["per_method"] == {"http": HTTP_, "https": HTTP_, "ftp": ["Proxy", "Timeout"]} and core_settings["per_host"] == ["Proxy"], core_settings["per_method"])
            check("the files of conf-cases.json that the allow-list keeps are of every setting that is on it: each method with each of ITS settings, each setting of Acquire:: itself, the whole names, and a host",
                  all(any(c_["reason"] == "" and name_.encode().lower() in bytes.fromhex(c_["hex"]).lower() for c_ in conf_cases)
                      for name_ in ["Acquire::%s::%s" % (m, s) for m, ss_ in core_settings["per_method"].items() for s in ss_] + ["Acquire::" + s for s in core_settings["top"]] + core_settings["other"]), "")
            check("the files of conf-cases.json that the allow-list leaves out are of every setting that a method has not: each setting of the others that ftp does not read, for ftp",
                  all(any(c_["reason"] == "not_allowed_setting" and ("acquire::ftp::%s " % s.lower()).encode() in bytes.fromhex(c_["hex"]).lower() for c_ in conf_cases)
                      for s in core_settings["per_method"]["http"] if s not in core_settings["per_method"]["ftp"]), "")
            check("the files of conf-cases.json that the allow-list leaves out are of every kind that is next to it: each of the settings that an open Acquire:: would let through (gpgv, a method's Options, Method, Cmd, Command, Sandbox, Changelogs, Auto-Detect, CompressionTypes, Comp-Type)",
                  all(any(c_["reason"] in ("not_allowed_setting", "proxy_auto_detect") and w_.encode().lower() in bytes.fromhex(c_["hex"]).lower() for c_ in conf_cases)
                      for w_ in ("::gpgv::options", "::http::options", "::method", "::cmd", "::command", "::sandbox", "::changelogs", "::auto-detect", "::compressiontypes", "::comp-type")), "")
            check("the files of conf-cases.json are of every kind: kept, and left out for each reason of their content",
                  {c_["reason"] for c_ in conf_cases} == {"", "not_simple", "has_directive", "not_allowed_setting", "too_large", "proxy_auto_detect"}, sorted({c_["reason"] for c_ in conf_cases}))
        cl_, cl_end_ = report_text.find("def capped_left_out(left):"), report_text.find("BOX = box_sources()")
        if not 0 <= cl_ < cl_end_:
            check("the report's list of what the core names as left out can be found in its text, to be tested against the core's capLeftOut", False, "")
        else:
            ns_cap = {}
            exec("MAX_LEFT_OUT = 200\n" + textwrap.dedent(report_text[report_text.rfind("\n", 0, cl_) + 1:report_text.rfind("\n", 0, cl_end_) + 1]), ns_cap)
            cap_ = ns_cap["capped_left_out"]
            n200_, n201_ = {"f%03d" % i: "not_simple" for i in range(200)}, {"f%03d" % i: "not_simple" for i in range(201)}
            check("the report's list of the files left out is the core's capLeftOut: by file, then reason, at most 200, and the cut is said only when there were more (200 files are not cut, 201 are)",
                  cap_({}) == ([], False) and cap_({"b": "too_large", "a": "not_simple"}) == ([{"file": "a", "reason": "not_simple"}, {"file": "b", "reason": "too_large"}], False)
                  and cap_(n200_)[1] is False and len(cap_(n200_)[0]) == 200 and cap_(n201_)[1] is True and len(cap_(n201_)[0]) == 200 and cap_(n201_)[0][-1]["file"] == "f199"
                  and cap_({"f%03d" % i: "not_simple" for i in range(250, 0, -1)})[0][0]["file"] == "f001", "")
        # what the report says of the list of the files left out in the data of a failure: for each code, with the files the sandbox left out and without, cut and not cut
        fl_, fl_end_ = report_text.find("LEFT_OUT_CODES = ("), report_text.find("def run_step(s):")
        if not 0 <= fl_ < fl_end_ or not 0 <= cl_ < cl_end_:
            check("the report's reading of the data of a failure can be found in its text, to be tested", False, "")
        else:
            def fl_run(left, code, data):
                ns_fl = {"LEFT_OUT": left, "capped_left_out": cap_, "json": json, "cell": lambda x, n=100: str(x)[:n]}
                exec(textwrap.dedent(report_text[report_text.rfind("\n", 0, fl_) + 1:report_text.rfind("\n", 0, fl_end_) + 1]), ns_fl)
                return ns_fl["failure_left_out_problems"]({"code": code, "data": data}), ns_fl["LEFT_OUT_CODES"]
            six_ = {e["file"]: e["reason"] for e in LEFT_OUT_OF_THE_IMAGE}
            plan_list_ = LEFT_OUT_OF_THE_IMAGE
            codes_ = fl_run(six_, "x", {})[1]
            ok_ = sorted(codes_) == ["plan_unparsed", "sandbox_solver_failed", "sandbox_update_failed"]
            for code_ in codes_:
                ok_ = ok_ and fl_run(six_, code_, {"apt_conf_left_out": plan_list_})[0] == [] and fl_run(six_, code_, {})[0] and fl_run(six_, code_, {"apt_conf_left_out": plan_list_[:-1]})[0]
                ok_ = ok_ and fl_run({}, code_, {"exit": 100})[0] == [] and fl_run({}, code_, {"apt_conf_left_out": plan_list_})[0] and fl_run({}, code_, {"apt_conf_left_out": []})[0]
            for code_ in ("sources_format", "timeout", "canceled", "internal", "busy", "no_room"):
                ok_ = ok_ and fl_run(six_, code_, {})[0] == [] and fl_run(six_, code_, {"apt_conf_left_out": plan_list_})[0] and fl_run(six_, code_, {"apt_conf_left_out_truncated": True})[0]
            ok_ = ok_ and fl_run(None, "sandbox_update_failed", {})[0] == [] and fl_run(six_, "plan_unparsed", {"x": 1})[0] and fl_run(six_, "plan_unparsed", None)[0]
            n201_ = {"f%03d" % i: "not_simple" for i in range(201)}
            cut_list_ = [{"file": f, "reason": r} for f, r in sorted(n201_.items())][:200]
            ok_ = ok_ and fl_run(n201_, "plan_unparsed", {"apt_conf_left_out": cut_list_, "apt_conf_left_out_truncated": True})[0] == [] and fl_run(n201_, "plan_unparsed", {"apt_conf_left_out": cut_list_})[0] \
                and fl_run(n201_, "plan_unparsed", {"apt_conf_left_out": cut_list_, "apt_conf_left_out_truncated": False})[0] and fl_run(n201_, "plan_unparsed", {"apt_conf_left_out": cut_list_[:199], "apt_conf_left_out_truncated": True})[0]
            check("the report asks for the list of the files left out in the data of a failed update, of a solver that fails and of an output that is not understood, and of no other failure: the same list as the plan's, cut "
                  "at 200 with its flag, and none at all when nothing was left out", ok_, "")
        sb_, sb_end_ = report_text.find("def split_blocks(text):"), report_text.find('RB_TEXT = ""')
        if not 0 <= sb_ < sb_end_:
            check("the report's reading of the text of a runbook can be found in its text, to be tested against the core's runbook of a box with several files", False, "")
        else:
            ns_rb = {}
            exec("import re\n" + textwrap.dedent(report_text[report_text.rfind("\n", 0, sb_) + 1:report_text.rfind("\n", 0, sb_end_) + 1]), ns_rb)
            rich_ = fxj("runbook-rich")
            tree_, aside_ = ns_rb["tree_of"](rich_["text"])
            blocks_ = [b_[1] for b_ in ns_rb["split_blocks"](rich_["text"])]
            check("the report reads the core's runbook of a box whose sources are in several files: its nine blocks by their markers, the files it writes byte for byte (sources.list and each file of sources.list.d), "
                  "the files it puts aside, and the hashes it checks are the plan's",
                  blocks_ == ["verify", "timers", "snapshot", "sources", "download", "minimal", "full", "prereboot", "after"] and len(tree_) == 3 and tree_ == {t_["path"]: t_["content"] for t_ in rich_["tree"]}
                  and sorted(aside_) == sorted(rich_["put_aside"]) == ["docker.list"] and ("[ \"$now\" = '%s' ]" % rich_["hash_current"]) in rich_["text"]
                  and ("[ \"$written\" = '%s' ]" % rich_["hash_generated"]) in rich_["text"], (blocks_, sorted(tree_), aside_))
        for c_ in BOX_CASES:
            files_ = {("sources.list" if n == "sources.list" else n): c_["files"][n].encode() for n in sorted(c_["files"], key=lambda n: (n != "sources.list", n))}
            try:
                got = ns["expected_sources"](files_)
            except Exception as e_:   # a rule that the report lost (or never had) is a failed case with the name of the box, and not a traceback that ends the harness
                check("the report's computation of the sources agrees with the core's rewrite of: " + c_["name"], False, "the report could not compute it (%s: %s)" % (type(e_).__name__, e_))
                continue
            want = (c_["files_out"], c_["paused"], c_["tree"], c_["dropped_backports"], c_["firmware_added"], c_["hash_current"], c_["hash_generated"])
            names_ = ("the files", "what is put aside", "the tree", "the dropped lines", "the components added", "hash_current", "hash_generated")
            diff_ = [n for n, a, b in zip(names_, got, want) if json.loads(json.dumps(a)) != json.loads(json.dumps(b))]
            check("the report's computation of the sources agrees with the core's rewrite of: " + c_["name"], not diff_, "%s differ: got %s, wanted %s" % (diff_, [json.dumps(a)[:300] for n, a in zip(names_, got) if n in diff_], [json.dumps(b)[:300] for n, b in zip(names_, want) if n in diff_]))
        refused_ = []
        for what_, files_ in (("a deb822 file", {"sources.list": b"deb http://deb.debian.org/debian bullseye main\n", "sources.list.d/x.sources": b"Types: deb\n"}),
                              ("a line with a bracket in the components", {"sources.list": b"deb http://deb.debian.org/debian bullseye main [x]\n"}),
                              ("a host that is not debian.org or Docker's", {"sources.list": b"deb http://example.com/debian bullseye main\n"}),
                              ("a host that only ends like debian.org", {"sources.list": b"deb http://notdebian.org/debian bullseye main\n"}),
                              ("a NUL", {"sources.list": b"deb http://deb.debian.org/debian bullseye main\n\x00\n"}),
                              ("a file that is not UTF-8", {"sources.list": b"# \xff\ndeb http://deb.debian.org/debian bullseye main\n"})):
            try:
                ns["expected_sources"](files_)
                refused_.append(what_)
            except ns["NotComputable"]:
                pass
        check("the report's computation refuses what it does not compute (a deb822 file, a bracket, another host, a NUL, bytes that are not UTF-8) and never guesses", not refused_, refused_)

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
    exits outside its END block. Comment lines are left out."""
    code = "\n".join(ln for ln in script.replace("\r\n", "\n").split("\n") if not ln.lstrip().startswith("#"))
    found = re.findall(r"\|\s*head\b[^|\n]*|\|\s*grep\s+-[A-Za-z]*[qm][^|\n]*|\|\s*sed\s[^|\n]*?(?<![A-Za-z])q\b[^|\n]*", code)
    found += ["awk '%s'" % prog.strip()[:60] for prog in re.findall(r"\bawk\b[^'\n]*'([^']*)'", code) if re.search(r"\bexit\b", re.sub(r"END\s*\{[^}]*\}", "", prog))]
    return found


with open(SCRIPT, newline="") as f:
    guest_text = f.read().replace("\r\n", "\n")
with open(READINESS, newline="") as f:
    readiness_text = f.read().replace("\r\n", "\n")
OLD_FORMS = ("cprev=\"$(apt-cache madison containerd.io | awk '$3 ~ /^1\\.7\\./ {print $3; exit}')\"", "dpkg --version 2>&1 | head -n1", "pgrep -af x | head -n1", "ps | grep -q x", "ps | grep -m1 x",
             "seq 1 9 | sed 1q", "awk '$1 == 1 {print; exit}' f")
READS_ALL = ("dpkg --version 2>&1 | sed -n 1p", "# x | head -n1\ntrue", "awk 'END { exit 1 }' f", "grep -q x f", "grep -q x <<<\"$y\"", "docker ps -q | xargs -r docker inspect | sed 's#^/##' | sort")
check("the lint of pipelines whose consumer quits early sees the forms that ended the Debian 11 leg, and not the ones that read everything",
      all(early_quitters(s) for s in OLD_FORMS) and not any(early_quitters(s) for s in READS_ALL), [s for s in OLD_FORMS if not early_quitters(s)] + [s for s in READS_ALL if early_quitters(s)])
check("no pipeline of the guest script has a consumer that quits before its producer is done (head, grep -q, sed q, an awk that exits outside END)", not early_quitters(guest_text), early_quitters(guest_text))
check("the guest script has no ${{ }} in it (the expressions of a workflow reach a script through env)", "${{" not in guest_text, "")
with open(REPORT, newline="") as f:
    report_src_for_checks = f.read().replace("\r\n", "\n")
guest_steps = re.search(r"^PLAN_STEPS=\(([^)]*)\)", guest_text, re.M)
guest_steps = guest_steps.group(1).split() if guest_steps else []
check("the guest runs the steps the report judges, in the same order", guest_steps == [s["id"] for s in STEPS], (guest_steps, [s["id"] for s in STEPS]))
check("every step has its dirt, its undo and its check that the undo held, and so has the hold", all(("%s_%s() " % (p, i)) in guest_text for i in guest_steps + ["hold"] for p in ("dirty", "undo", "undone")),
      [i for i in guest_steps + ["hold"] if not all(("%s_%s() " % (p, i)) in guest_text for p in ("dirty", "undo", "undone"))])
check("the guest has no step of the readiness proof's matrix under the same name as one of its own (the sourced functions are not replaced by accident)",
      not set(re.findall(r"^(dirty|undo|undone)_(\w+)\(\)", guest_text, re.M)) & set(re.findall(r"^(dirty|undo|undone)_(\w+)\(\)", readiness_text, re.M)), "")
readiness_functions = set(re.findall(r"^(\w+)\(\) \{", readiness_text, re.M))
mine = set(re.findall(r"^(\w+)\(\) \{", guest_text, re.M))
check("the guest sources the readiness proof's functions and copies none of them: what both define is main alone", mine & readiness_functions == {"main"}, sorted(mine & readiness_functions))
check("the guest sources the readiness proof next to it, and does not run it", re.search(r'^\. "\$\{READINESS_LIB:-\$\{HERE_PLAN\}/os-readiness-proof\.sh\}"$', guest_text, re.M) is not None, "")
check("the guest never runs the runbook: its blocks are read by sh -n and by nothing else", len(re.findall(r'\bsh -n "', guest_text)) == 1 and not re.search(r'\b(?:sh|bash|source|\.) "?\$\{OUT\}/(?:rb-block|runbook)', guest_text) and "eval" not in guest_text, "")
guest_main = re.search(r"^main\(\) \{\n.*?^\}\n", guest_text, re.M | re.S)
guest_main = guest_main.group(0) if guest_main else ""
guest_window = re.search(r"^window\(\) \{\n(.*?)^\}\n", guest_text, re.M | re.S)
guest_window = guest_window.group(1) if guest_window else ""
check("the run is: the preparation, the copy of the box's own files, the window, the runbook of the first plan, the second plan, the hold, the steps, the last plan, in this order",
      re.search(r'    wait_image_check 240 \|\| not_proven [^\n]*\n    copy_sources\n    copy_aptconf\n    marker prepare_done\n[^\n]*\n    sleep 20\n\n    window\n    call_core runbook-p1 GET "\$\{RUNBOOK_PATH\}" 30\n'
                r'    runbook_shn runbook-p1\n    plan_run p2\n    plan_hold\n    local id\n    for id in "\$\{PLAN_STEPS\[@\]\}"; do plan_step "\$\{id\}"; done\n    marker plan4_start\n    plan_run p4\n    date -u \+%FT%TZ >"\$\{OUT\}/finished"\n',
                guest_main) is not None, guest_main[-900:])
guest_trees = re.search(r'if \[ -z "\$\{INV_TREES\+x\}" \]; then INV_TREES=\((/etc[^)]*)\); fi', guest_text)
report_trees = re.search(r"^TREES = \(([^)]*)\)", report_src_for_checks, re.M)
check("the guest's trees of the invariance window are the report's, in the same order: the readiness proof's six and /var/log/apt before /var/log/casaos, set before the readiness proof is sourced",
      guest_trees is not None and report_trees is not None and guest_trees.group(1).split() == [t.strip().strip('"') for t in report_trees.group(1).split(",")]
      and guest_text.index("INV_TREES=(") < guest_text.index('. "${READINESS_LIB'), (guest_trees and guest_trees.group(1), report_trees and report_trees.group(1)))
check("the guest asks for the names that no answer may say right after the facts of the box, and before the wait for the images",
      "    box_facts\n    leak_needles\n    # AppManagement's check of the images" in guest_main, "")
check("the guest's canary fragments are written as the report says: the key spliced by a comment, an #include of /etc/apt/osp-canary-include.conf that holds the hook, and the option of gpgv that names a file in the box's lists of apt",
      "APT::Update::Post-In/**/voke-Success {\"touch /var/lib/apt/periodic/osp-canary-splice\";};" in guest_text and "#include \"/etc/apt/osp-canary-include.conf\";" in guest_text
      and "APT::Update::Post-Invoke-Success {\"touch /var/lib/apt/periodic/osp-canary-include\";};" in guest_text
      and "'Acquire::gpgv::Options::0 \"--log-file=/var/lib/apt/lists/partial/osp-canary\";'" in guest_text, "")
check("the file that gpgv is told to write is the box's own lists of apt by default (/var/lib/apt/lists/partial/osp-canary), and the guest's directory of it can be moved for a test, as the periodic one can",
      'LISTS_PARTIAL="${LISTS_PARTIAL:-/var/lib/apt/lists/partial}"' in guest_text and 'CANARY_GPGV_FILE="${CANARY_GPGV_FILE:-${LISTS_PARTIAL}/osp-canary}"' in guest_text, "")
check("the guest's canary steps are the report's: the three fragments, in the order of the matrix, after the dirt that fails",
      [s["id"] for s in STEPS][-3:] == ["canarysplice", "canaryinclude", "canarygpgv"] and guest_steps[-3:] == ["canarysplice", "canaryinclude", "canarygpgv"]
      and all(s["frag"] in guest_text and s["file"] in guest_text for s in STEPS if s["kind"] == "canary"), "")
check("the guest copies the box's sources and its apt configuration BEFORE the window, and the first plan is not asked for before the window's first snapshot",
      guest_main.index("copy_sources") < guest_main.index("    window\n") and "plan_run p1" not in guest_main and "probe_plan_route" not in guest_main and "call_core plan-" not in guest_main, "")
check("the window is: the control (two snapshots, two calls, the wait, PID 1's journal), the first snapshot, the witnesses, the first call of the plan route (a 404 ends the run), the runbook before any plan, the POST, "
      "the second POST, the watching, the last snapshot, the witnesses, PID 1's journal of the window",
      re.search(r'    marker control_start\n    window_snapshot ctl-a\n    api ctl-core-1 /v1/sys/version/current 20\n    api ctl-core-2 /v1/sys/version/current 20\n[^\n]*\n[^\n]*\n    sleep "\$\{CONTROL_SECONDS\}"\n    window_snapshot ctl-b\n'
                r'    pid1_window control_start pid1-control\.jsonl\n[^\n]*\n    marker invariance_start\n    window_snapshot before\n    witnesses before\n    probe_plan_route\n    call_core runbook-idle GET "\$\{RUNBOOK_PATH\}" 30\n'
                r'    call_core plan-p1-post POST "\$\{PLAN_PATH\}" 20\n[^\n]*\n    call_core plan-p1-post-2 POST "\$\{PLAN_PATH\}" 20\n    plan_wait plan-p1\n    window_snapshot after\n    witnesses after\n'
                r'    pid1_window invariance_start pid1-window\.jsonl\n    marker invariance_done\n', guest_window) is not None, guest_window)
check("the control waits 320 s by default, as the readiness proof's does, and the first call of the window is the probe of the route", 'CONTROL_SECONDS="${CONTROL_SECONDS:-320}"' in readiness_text and "    probe_plan_route\n" in guest_window, "")
def guest_window_calls():
    """the calls the guest makes between the first and the last snapshot of the window, in order, named as the report names them"""
    out = []
    for ln in guest_window.splitlines():
        ln = ln.strip()
        m = re.match(r"^call_core (\S+) (?:GET|POST)", ln)
        if ln == "probe_plan_route":
            out.append("plan-idle")
        elif m:
            out.append(m.group(1))
        elif ln == "plan_wait plan-p1":
            out += ["the polls", "plan-p1-get"]
    return out


def report_window_calls():
    d_ = VDir()
    build(d_)
    m = re.search(r"^The window is: GET of the plan, GET of the runbook, POST, a second POST, and GET every second until the job is done \(([^)]*)\)", run_report(d_)[1], re.M)
    return [x.strip() for x in m.group(1).split(",")] if m else None


check("the calls the report says are inside the window are exactly those the guest makes between the first and the last snapshot, in the same order",
      guest_window_calls() == report_window_calls() and guest_window_calls()[0] == "plan-idle", (guest_window_calls(), report_window_calls()))
check("the files the guest names for the plans and the steps are the ones the report reads: plan-<run>.polls.tsv, plan-<run>.sandbox-seen, plan-<run>.timeout, <run>-get, step-<id>.leftover, runbook-p1.shn.tsv",
      all(s in guest_text for s in ("${prefix}.polls.tsv", "${prefix}.sandbox-seen", "${prefix}.timeout", "${prefix}-get.${ext}", "step-${id}.leftover", "$1.shn.tsv", "step-${id}-post", "step-${id}-runbook", "step-hold.dirty-rc", "runbook-p3")), "")
check("the mutated paths of the dirt are the ones the report names: osp-deb822.sources, osp-cdrom.list, osp-alias.list, osp-target.list, osp-mixed.list, osp-deadmirror.list",
      all(s in guest_text for s in ("osp-deb822.sources", "osp-cdrom.list", "osp-alias.list", "osp-target.list", "osp-mixed.list", "osp-deadmirror.list")) and
      all(s["needle"] in guest_text for s in STEPS if s["needle"] and s["id"] != "deadmirror") and "ftp9.zz.debian.org" in guest_text, "")


def hosts_of_dirt(name):
    """the hosts of the URLs that the guest writes in the file of the step `name` (its dirty_<name> function)"""
    m = re.search(r"^dirty_%s\(\) \{(.*)$" % name, guest_text, re.M)
    return re.findall(r"https?://([A-Za-z0-9.-]+)[/ ]", m.group(1)) if m else None


check("the hosts of the dirty steps are ones the core decides on by name, with no look at the lists of the box: the mixed file has a line of an archive host and one of Docker's, and the dead mirror is a country mirror "
      "of Debian's own list (ftp<digit>.<cc>.debian.org) whose code, zz, no country has; the core's own failures for them are in the phase the steps pin: a host that was not decided would stop the mixed file at sources "
      "with sources_format, and the dead mirror before the update",
      hosts_of_dirt("mixed") == ["deb.debian.org", "download.docker.com"] and (hosts_of_dirt("deadmirror") or [""])[0] == "ftp9.zz.debian.org"
      and len(hosts_of_dirt("deadmirror") or []) == 1
      and fxj("failed-sources_mixed_file")["data"]["phase"] == STEP_BY_ID["mixed"]["phase"] == "sources" and fxj("failed-sources_mixed_file")["data"]["error"]["code"] == "sources_mixed_file"
      and fxj("failed-sandbox_update_failed")["data"]["phase"] == STEP_BY_ID["deadmirror"]["phase"] == "update" and fxj("failed-sandbox_update_failed")["data"]["error"]["code"] == "sandbox_update_failed",
      (hosts_of_dirt("mixed"), hosts_of_dirt("deadmirror")))

if have_bash() and not os.environ.get("REPORT_ONLY"):
    # fakes of the tools the guest script calls, over a box that lives in files ($FAKE); the model below is the core behind the fake curl
    FAKECORE = r'''
import hashlib, json, os, re, subprocess, sys, time
from datetime import datetime, timezone
F = os.environ["FAKE"]
FX = os.environ["FIXTURES"]
method, path, out, hdr = sys.argv[1], "/" + sys.argv[2], sys.argv[3], sys.argv[4]
mode = os.environ.get("FAKE_CORE_MODE", "")
APT_DIR, TMP = os.environ["APT_DIR"], os.environ["TMP_DIR"]
PHASES = ["sources", "update", "upgrade", "dist-upgrade", "estimate", "parse"]
SANDBOX = os.path.join(TMP, "casaos-os-plan.481516")
SANDBOX2 = os.path.join(TMP, "casaos-os-plan.999")


def fx(name):
    return json.load(open(os.path.join(FX, name + ".json"), encoding="utf-8"))


def lines(p):
    try:
        return [x for x in open(p).read().split("\n") if x.strip()]
    except OSError:
        return []


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reply(code, body=None, rc=0, cache=True):
    if body is not None and out != "/dev/null":
        open(out, "w").write(body if isinstance(body, str) else json.dumps(body))
    if hdr != "/dev/null":
        open(hdr, "w", newline="").write("HTTP/1.1 %d X\r\nContent-Type: application/json; charset=UTF-8\r\n%sDate: Mon, 12 Oct 2026 09:30:00 GMT\r\n\r\n" % (code, "Cache-Control: no-store\r\n" if cache and mode != "nostore" else ""))
    print(code, rc)
    sys.exit(0)


def read_state():
    try:
        return json.load(open(os.path.join(F, "state.json")))
    except OSError:
        return None


def write_state(s):
    json.dump(s, open(os.path.join(F, "state.json"), "w"))


if path == "/":
    reply(200, "<html></html>")
if path == "/v1/sys/version/current":
    reply(200, "0.5.99\n", cache=False)
if path == "/v2/app_management/categories":
    reply(200, {"success": 200, "data": []})
if not path.startswith("/v1/sys/os/upgrade/") or mode == "route404":
    reply(404, {"success": 404, "message": "no"})

holds = lines(os.path.join(F, "holds"))
now = time.time()


def dirt():
    """which file of sources.list.d the box was dirtied with: the job fails as the real core says"""
    sd = os.path.join(APT_DIR, "sources.list.d")
    for name, fixture in (("osp-deb822.sources", "failed-sources_format"), ("osp-cdrom.list", "failed-sources_cdrom"), ("osp-alias.list", "failed-sources_alias"), ("osp-target.list", "failed-sources_target"),
                          ("osp-mixed.list", "failed-sources_mixed_file"), ("osp-deadmirror.list", "failed-sandbox_update_failed")):
        if os.path.exists(os.path.join(sd, name)):
            return fixture
    return ""


def plan_id():
    sources = "".join(open(os.path.join(APT_DIR, n), errors="replace").read() for n in ("sources.list",))
    return "p1-" + hashlib.sha256(json.dumps([sorted(holds), sources]).encode()).hexdigest()[:32]


def plan_body(t_start):
    j = fx("plan-needs_acceptance")
    d = j["data"]
    p = d["plan"]
    started, finished = t_start, now
    d["started_at"], d["finished_at"] = iso(started), iso(finished)
    p["made_at"] = iso(finished)
    p["expires_at"] = d["expires_at"] = iso(finished + 86400)
    p["sandbox"] = {"bytes": 224395264, "seconds_by_phase": {"sources": 0.4, "update": 38.2, "upgrade": 6.1, "dist-upgrade": 9.3, "estimate": 5.0, "parse": 0.3}}
    p["plan_id"] = "p1-" + hashlib.sha256(json.dumps([sorted(holds), mode == "badid" and now]).encode()).hexdigest()[:32]
    p["holds"] = sorted(holds)
    if mode == "nohooks":
        p["apt_conf_left_out"] = []
    for name, reason in (("99osp-canary-splice", "not_simple"), ("99osp-canary-include", "has_directive"), ("99osp-canary-gpgv", "not_allowed_setting")):
        if os.path.exists(os.path.join(APT_DIR, "apt.conf.d", name)) and mode != "canaryunnamed":
            p["apt_conf_left_out"].append({"file": name, "reason": reason})
    p["apt_conf_left_out"].sort(key=lambda e: e["file"])
    if mode == "badsources":
        p["sources"]["files"][0]["lines"][0]["after"] = p["sources"]["files"][0]["lines"][0]["before"]
    if holds:
        p["kept_back"] = sorted(holds)
        p["verdict"] = "blocked"
        p["problems"] = [{"code": "dist_kept_back", "severity": "blocked", "data": {"names": sorted(holds)}}] + p["problems"]
        p["counts"]["dist_upgrade"]["kept_back"] = len(holds)
    return j


def finish_job(st):
    """the job ends: its sandbox directory is removed (unless this core leaves it behind)"""
    if os.path.isdir(SANDBOX) and mode != "leftover":
        os.rmdir(SANDBOX)
    if os.path.isdir(SANDBOX2) and mode != "leftover":
        os.rmdir(SANDBOX2)


st = read_state()
if method == "POST" and path.endswith("/plan"):
    if st and st["state"] == "running":
        if mode == "dupjob":
            os.makedirs(SANDBOX2, exist_ok=True)
        d = fx("already_running")
        d["data"]["phase"] = PHASES[min(st["polls"], 5)]
        reply(200, d)
    held = False
    if os.environ.get("HAVE_FCNTL") == "1":
        held = subprocess.run([sys.executable, os.path.join(os.environ["LIBDIR"], "lockprobe.py"), os.environ["DPKG_LOCK"]], capture_output=True, text=True).stdout.strip() == "held"
    else:
        held = os.path.exists(os.path.join(F, "lock.held"))
    if mode == "busyalways" or (held and mode != "nobusy"):
        reply(409, fx("refused-busy"))
    if os.path.exists(os.path.join(F, "tmpfs")) and mode != "noroomignored":
        reply(409, fx("refused-no_room"))
    # the n-th POST that would begin a job is refused when FAKE_CORE_REFUSE says n: p1 is the first, p2 the second, the hold the third
    posts = int(open(os.path.join(F, "posts")).read()) + 1 if os.path.exists(os.path.join(F, "posts")) else 1
    open(os.path.join(F, "posts"), "w").write(str(posts))
    if os.environ.get("FAKE_CORE_REFUSE") == str(posts):
        reply(409, fx("refused-busy"))
    w = os.environ.get("FAKE_CORE_WRITE")
    if w and mode == "write":
        open(w, "a").write("x")
    if mode == "restart":
        open(os.path.join(F, "inv", os.environ["FAKE_CORE_UNIT"]), "w").write("restarted-%d" % now)
    if mode == "listen":
        open(os.path.join(F, "ports"), "a").write("tcp 0.0.0.0:9000\n")
    if mode == "stampwrite":
        open(os.environ["STAMP"], "a").write("x")
    if mode == "canaryfires":
        # a sandbox that did not move apt's configuration: the hooks of the box run in its update
        for name, where, made in (("99osp-canary-splice", "PERIODIC_DIR", "osp-canary-splice"), ("99osp-canary-include", "PERIODIC_DIR", "osp-canary-include"), ("99osp-canary-gpgv", "LISTS_PARTIAL", "osp-canary")):
            if os.path.exists(os.path.join(APT_DIR, "apt.conf.d", name)):
                open(os.path.join(os.environ[where], made), "a").write("x")
    failing = dirt()
    write_state({"state": "running", "polls": 0, "fail": failing, "started": now})
    if not failing or failing == "failed-sandbox_update_failed":
        os.makedirs(SANDBOX, exist_ok=True)
    reply(202, fx("accepted"))

if method == "GET" and path.endswith("/plan"):
    if st is None:
        reply(200, fx("idle"))
    if st["state"] == "running":
        st["polls"] += 1
        fail = st["fail"]
        if fail:
            e = fx(fail)
            limit = 2 if fail == "failed-sandbox_update_failed" else 1
            if st["polls"] >= limit:
                finish_job(st)
                st = {"state": "failed", "body": e}
                write_state(st)
                reply(200, e)
        elif st["polls"] >= 3:
            finish_job(st)
            body = plan_body(st["started"])
            write_state({"state": "done", "body": body, "blocked": body["data"]["plan"]["verdict"] == "blocked", "id": body["data"]["plan"]["plan_id"]})
            reply(200, body)
        write_state(st)
        r = fx("running-" + PHASES[min(st["polls"] - 1, 5)])
        reply(200, r)
    reply(200, st["body"])

if method == "GET" and path.endswith("/runbook"):
    reason = "idle" if st is None else "running" if st["state"] == "running" else "failed" if st["state"] == "failed" else "blocked" if st.get("blocked") else ""
    if reason:
        if mode == "runbookfailed" and reason == "failed":
            reply(200, fx("runbook"))
        reply(409, fx("no_plan-" + reason))
    rb = fx("runbook")
    old = fx("plan-needs_acceptance")["data"]["plan"]["plan_id"]
    reply(200, json.loads(json.dumps(rb).replace(old, st["id"])))
reply(404, {"success": 404})
'''
    FAKES = {
        "curl": r'''#!/bin/sh
out=""; fmt=""; url=""; method=GET; hdr=/dev/null
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w) fmt="$2"; shift 2 ;;
    -D) hdr="$2"; shift 2 ;;
    -X) method="$2"; shift 2 ;;
    --data-binary) method=POST; shift 2 ;;
    -H|--max-time) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
path="/${url#http://127.0.0.1:*/}"
printf '%s\n' "curl $method $path $*" >>"$FAKE/calls"
res="$(python3 "$FAKE/fakecore.py" "$method" "${path#/}" "${out:-/dev/null}" "$hdr")"
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
    value=""; prop=""
    for a in "$@"; do case "$a" in --value) value=1 ;; --property=*) prop="${a#--property=}" ;; esac; done
    while [ $# -gt 0 ]; do
      u="$1"; shift
      case "$u" in -p) shift; continue ;; -*) continue ;; esac
      st=inactive; sub=dead; pid=0
      if [ -e "$FAKE/active/$u" ]; then st=active; sub=running; pid=$(( $(printf '%s' "$u" | cksum | cut -d' ' -f1) % 30000 + 100 )); fi
      inv="inv-$u"; [ ! -e "$FAKE/inv/$u" ] || inv="$(cat "$FAKE/inv/$u")"
      if [ -n "$value" ]; then
        case "$prop" in ActiveState) echo "$st" ;; *) echo ;; esac
      else
        printf 'Id=%s\nActiveState=%s\nSubState=%s\nMainPID=%s\nInvocationID=%s\n\n' "$u" "$st" "$sub" "$pid" "$inv"
      fi
    done ;;
  *) printf '%s\n' "systemctl $cmd $*" >>"$FAKE/calls" ;;
esac
''',
        "docker": r'''#!/bin/sh
cmd="$1"; shift
case "$cmd" in
  ps) while IFS='|' read -r name rest; do [ -z "$name" ] || echo "id-$name"; done <"$FAKE/containers" ;;
  inspect)
    fmt=""; ids=""
    while [ $# -gt 0 ]; do case "$1" in --format) fmt="$2"; shift 2 ;; id-*) ids="$ids $1"; shift ;; *) shift ;; esac; done
    for id in $ids; do
      n="${id#id-}"; i=$(( $(printf '%s' "$n" | cksum | cut -d' ' -f1) % 9000 + 1000 ))
      printf '%s\n' "$fmt" | sed -e "s#{{.Name}}#/$n#" -e "s#{{.State.StartedAt}}#2026-10-12T08:00:00Z$(cat "$FAKE/restarted.$n" 2>/dev/null)#" -e "s#{{.State.Pid}}#$i#"
    done ;;
  *) ;;
esac
''',
        "apt-mark": r'''#!/bin/sh
case "$1" in
  hold) echo "$2" >>"$FAKE/holds" ;;
  unhold) grep -vx "$2" "$FAKE/holds" >"$FAKE/holds.new" || true; mv "$FAKE/holds.new" "$FAKE/holds" ;;
  showhold) cat "$FAKE/holds" ;;
esac
''',
        "mount": "#!/bin/sh\nprintf '%s\\n' \"mount $*\" >>\"$FAKE/calls\"\n: >\"$FAKE/tmpfs\"\n",
        "umount": "#!/bin/sh\nprintf '%s\\n' \"umount $*\" >>\"$FAKE/calls\"\n[ -e \"$FAKE/umount.fail\" ] && exit 32\nrm -f \"$FAKE/tmpfs\"\n",
        "findmnt": "#!/bin/sh\n[ -e \"$FAKE/tmpfs\" ]\n",
        "ss": r'''#!/bin/sh
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
        "journalctl": r'''#!/bin/sh
printf '%s\n' "journalctl $*" >>"$FAKE/calls"
exit 0
''',
        # jq -r <filter> <file>: the filters of the guest, `.a.b // "-"`, `.a // .b // "-"`, `.a // empty`
        "jq": r'''#!/bin/sh
exec python3 "$FAKE/jq.py" "$@"
''',
    }
    FAKEJQ = r'''
import json, sys
args = [a for a in sys.argv[1:] if a != "-r"]
flt, path = args[0], args[1]
try:
    data = json.load(open(path))
except (OSError, ValueError):
    sys.exit(2)
for alt in [a.strip() for a in flt.split("//")]:
    if alt == "empty":
        sys.exit(0)
    if alt.startswith('"'):
        print(json.loads(alt))
        sys.exit(0)
    v = data
    for k in [k for k in alt.split(".") if k]:
        v = v.get(k) if isinstance(v, dict) else None
    if v is not None and v is not False:
        print(v if isinstance(v, str) else json.dumps(v))
        sys.exit(0)
'''
    HAVE_FCNTL = subprocess.run([sys.executable, "-c", "import fcntl"], capture_output=True).returncode == 0
    UNITS_G = {"casaos.service": ("active", "running", "811", "inv-core"), "casaos-gateway.service": ("active", "running", "812", "inv-gw"), "casaos-app-management.service": ("active", "running", "813", "inv-am"),
               "docker.service": ("active", "running", "900", "inv-docker"), "ssh.service": ("active", "running", "700", "inv-ssh"), "logrotate.service": ("inactive", "dead", "0", "inv-logrotate")}
    PORTS_G = ["tcp 0.0.0.0:22", "tcp *:80", "tcp 0.0.0.0:18081", "udp 0.0.0.0:68"]

    def sandbox(tmp):
        """the box of the guest in a directory of its own: the fake tools in front of PATH, the files the guest script's variables point at, the state the tools keep"""
        j = lambda *p: os.path.join(tmp, *p).replace("\\", "/")
        for sub in ("fake", "fake/active", "fake/inv", "root/etc/apt/sources.list.d", "root/etc/apt/apt.conf.d", "root/etc/casaos", "root/var/lib/dpkg", "root/var/lib/apt/lists", "root/var/lib/apt/periodic",
                    "root/var/cache/apt", "root/var/lib/casaos", "root/var/log/apt", "root/var/log/casaos", "root/var/tmp", "root/run/casaos", "lib", "auth", "out"):
            os.makedirs(j(*sub.split("/")), exist_ok=True)
        for name, text in FAKES.items():
            with open(j("fake", name), "w", newline="\n") as f:
                f.write(text)
            os.chmod(j("fake", name), 0o755)
        for name, text in (("fakecore.py", FAKECORE), ("jq.py", FAKEJQ)):
            with open(j("fake", name), "w", newline="\n") as f:
                f.write(text)
        for unit in UNITS_G:
            if UNITS_G[unit][0] == "active":
                open(j("fake", "active", unit), "w").close()
        with open(j("fake", "units.list"), "w", newline="\n") as f:
            f.write("".join("%s loaded %s %s x\n" % (u, v[0], v[1]) for u, v in sorted(UNITS_G.items())))
        for name, text in (("holds", ""), ("ports", "\n".join(PORTS_G) + "\n"), ("containers", "".join("%s|always|bridge|\n" % n for n in WITNESSES))):
            with open(j("fake", name), "w", newline="\n") as f:
                f.write(text)
        files = {"root/etc/os-release": 'PRETTY_NAME="%s"\nID=debian\nVERSION_ID="11"\nVERSION_CODENAME=bullseye\n' % LEG["pretty"], "root/etc/apt/sources.list": MEASURED_BOX["files"]["sources.list"],
                 "root/etc/apt/sources.list.d/docker.list": MEASURED_BOX["files"]["sources.list.d/docker.list"], "root/var/lib/dpkg/lock-frontend": "", "root/var/lib/dpkg/status": "Package: x\n",
                 "root/run/casaos/internal.secret": "sekrit\n", "root/var/lib/apt/lists/deb.debian.org_debian_dists_bullseye_InRelease": "release\n", "root/var/lib/apt/periodic/update-success-stamp": "",
                 "root/var/cache/apt/pkgcache.bin": "pkg cache\n", "root/var/log/apt/history.log": "history\n", "root/var/log/casaos/casaos.log": "log\n", "root/var/lib/casaos/state": "s\n", "root/etc/casaos/gateway.ini": "HttpPort=80\n",
                 "root/var/tmp/systemd-private-aaaa": ""}
        for n, text in APTCONF.items():
            files["root/etc/apt/apt.conf.d/" + n] = text
        for name, text in files.items():
            with open(j(*name.split("/")), "w", newline="\n") as f:
                f.write(text)
        os.makedirs(j("root", "var", "lib", "apt", "lists", "partial"), exist_ok=True)
        env = dict(ENV, PATH=j("fake") + os.pathsep + os.environ["PATH"], FAKE=j("fake"), HAVE_FCNTL="1" if HAVE_FCNTL else "0", APT_DIR=j("root/etc/apt"), TMP_DIR=j("root/var/tmp"), OS_RELEASE=j("root/etc/os-release"),
                   DPKG_LOCK=j("root/var/lib/dpkg/lock-frontend"), INTERNAL_SECRET_FILE=j("root/run/casaos/internal.secret"), LIBDIR=j("lib"), AUTH_DIR=j("auth"), OUT=j("out"),
                   IMAGE_UPDATES=j("root/var/lib/casaos/image_updates.json"), FIXTURES=FIXTURES.replace("\\", "/"), INV_ROOT=j("root"), INV_TREES=j("root/etc"), STAMP=j("root/var/lib/apt/periodic/update-success-stamp"), PERIODIC_DIR=j("root/var/lib/apt/periodic"),
                   LISTS_PARTIAL=j("root/var/lib/apt/lists/partial"), READINESS_LIB=READINESS.replace("\\", "/"), CONTROL_SECONDS="0")
        return env, j

    PRELUDE = ('source "$1"; shift; export LC_ALL=C; FAMILY=debian; FROM_CODENAME=bullseye; TO_CODENAME=bookworm; CASA_PORT=80; PENDING_UNDO=""; '
               'APT=(env DEBIAN_FRONTEND=noninteractive apt-get); sleep() { command sleep 0.02; }; ')
    LOCK_SHIM = ('lock_hold_start() { : >"$FAKE/lock.held"; }; lock_hold_stop() { rm -f "$FAKE/lock.held"; }; lock_state() { if [ -e "$FAKE/lock.held" ]; then echo held; else echo free; fi; }; ')

    def run_guarded(argv, env, timeout):
        """a run of bash that does not end in its time is a failure that says so (and is asked once more by the end of the section: a machine that is busy with a few hundred processes at once
        loses one now and then), never an exception that ends the harness"""
        try:
            return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(argv, 124, "", "the run did not end in %d s" % timeout)

    def run_bash(env, snippet, lock_shim=True, timeout=float(os.environ.get("BASH_TIMEOUT", 300))):
        text = PRELUDE + ("" if HAVE_FCNTL or not lock_shim else LOCK_SHIM) + snippet
        return run_guarded([BASH, "-c", text, "_", SCRIPT.replace("\\", "/")], env, timeout)

    JOBS = []   # what runs on the guest side, kept for the end of this section, where all of it runs side by side (each in a directory of its own): on Windows a process costs, and a run is a few hundred

    def gcase(label, snippet, files=None, calls=(), no_calls=(), ordered=(), lock_shim=True, setup=None, returned=True, rc=None, in_stdout=(), prelude_env=None):
        """a snippet run in a bash that has sourced the guest script, with the stand-ins in front. files maps a file of the sandbox ($OUT, relative to the sandbox) to its exact content
        (a string ending in * is a prefix), None to its absence; calls and no_calls are what the stand-ins were called with (the file `calls` of the fake directory)"""
        if not wanted(label):
            return
        count[0] += 1
        JOBS.append(lambda: gcase_run(label, snippet, files, calls, no_calls, ordered, lock_shim, setup, returned, rc, in_stdout, prelude_env))

    def gcase_run(label, snippet, files, calls, no_calls, ordered, lock_shim, setup, returned, rc, in_stdout, prelude_env):
        with tempfile.TemporaryDirectory() as tmp:
            env, j = sandbox(tmp)
            env.update(prelude_env or {})
            if setup:
                setup(tmp, j)
            p = run_bash(env, snippet + ("; echo returned" if returned else ""), lock_shim)

            def get(path):
                try:
                    with open(os.path.join(tmp, path), newline="") as f:
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
                return label, "%s: %s\n%s" % (label, "; ".join(problems), p.stderr[-400:])
            return label, None

    # ---- the text-only functions, and what the guest refuses ----
    p_ = run_bash(sandbox(tempfile.mkdtemp())[0], "echo sourced")
    check("sourcing the guest script runs nothing (and sources the readiness proof, which runs nothing either)", p_.stdout.strip() == "sourced" and p_.returncode == 0, p_.stdout + p_.stderr[-300:])
    env_ = {k: v for k, v in os.environ.items() if k != "PLAN_DISPOSABLE"}
    for label, args, env_extra, want in (("a machine that is not marked disposable", ["d11"], {}, "disposable machine"), ("a leg that is not d11", ["u22"], {"PLAN_DISPOSABLE": "1"}, "usage"),
                                         ("no leg at all", [], {"PLAN_DISPOSABLE": "1"}, "usage")):
        count[0] += 1
        p_ = subprocess.run([BASH, SCRIPT.replace("\\", "/")] + args, capture_output=True, text=True, env=dict(env_, **env_extra))
        check("the guest script refuses " + label, p_.returncode == 2 and want in p_.stderr, "%d %r" % (p_.returncode, p_.stderr[-300:]))

    # ---- call_core: what a call leaves ----
    gcase("call_core: a GET leaves the body, the headers, the status, the time before and after, and how curl ended (written last)", 'call_core c GET /v1/sys/os/upgrade/plan 30; cat "$OUT/c.code" "$OUT/c.curl" >"$OUT/both"; [ -s "$OUT/c.t0" ] && [ -s "$OUT/c.t1" ] && [ -s "$OUT/c.hdr" ] && echo files >"$OUT/f"',
          files={"out/both": "200\n0\n", "out/f": "files\n", "out/c.json": "{*"}, calls=["curl GET /v1/sys/os/upgrade/plan"])
    gcase("call_core: a POST is a POST with no body, and still has the headers", 'call_core c POST /v1/sys/os/upgrade/plan 20', calls=["curl POST /v1/sys/os/upgrade/plan"], files={"out/c.code": "202\n", "out/c.hdr": "HTTP/1.1 202*"})
    gcase("call_core: the call goes to the gateway with the loopback secret, which is read again for each call", 'curl() { printf "%s\\n" "$*" >>"$FAKE/calls"; printf 200; }; call_core a GET /x; echo other >"$INTERNAL_SECRET_FILE"; call_core b GET /y',
          calls=["-H Authorization: Internal sekrit http://127.0.0.1:80/x", "-H Authorization: Internal other http://127.0.0.1:80/y"], files={"out/a.curl": "0\n"})
    gcase("call_core: a POST sends -X POST and an empty body, a GET sends neither", 'curl() { printf "%s\\n" "$*" >>"$FAKE/calls"; printf 200; }; call_core a POST /x; call_core b GET /y', calls=["-X POST --data-binary  -D"], no_calls=["-X POST --data-binary  -D /nonexistent"])
    gcase("call_core: a curl that timed out is recorded as 28, with the status 000", 'curl() { printf "000"; return 28; }; call_core c GET /x 1; cat "$OUT/c.code" "$OUT/c.curl" >"$OUT/both"', files={"out/both": "000\n28\n"})
    gcase("call_core: a curl that says nothing and fails leaves the status 000", 'curl() { return 7; }; call_core c GET /x; cat "$OUT/c.code" "$OUT/c.curl" >"$OUT/both"', files={"out/both": "000\n7\n"})
    gcase("call_core: the time after the call is later than the time before", 'curl() { command sleep 0.3; printf 200; }; call_core c GET /x; awk -v a="$(cat "$OUT/c.t0")" -v b="$(cat "$OUT/c.t1")" \'BEGIN { print (b > a) ? "later" : "same" }\' >"$OUT/cmp"',
          files={"out/cmp": "later\n"})
    gcase("call_core: a call leaves its files, and a curl that wrote no body leaves no body", 'curl() { printf 200; }; call_core c GET /x; l=$(ls "$OUT" | sort | tr "\\n" " "); printf "%s" "$l" >"$OUT/ls"', files={"out/ls": "c.code c.curl c.err c.t0 c.t1 "})
    # (the listing is taken into a variable BEFORE the file that holds it is made: `ls "$OUT" | tr >"$OUT/ls"` races ls against its own redirect, and on Linux it lists its own output file)
    gcase("json_field: what a filter says of a body, and `-` of a body that is not JSON", 'echo \'{"data":{"state":"done"}}\' >"$OUT/a"; echo nope >"$OUT/b"; json_field "$OUT/a" \'.data.state // "-"\' >"$OUT/x"; json_field "$OUT/b" \'.data.state // "-"\' >"$OUT/y"; json_field "$OUT/a" \'.data.phase // "-"\' >"$OUT/z"',
          files={"out/x": "done\n", "out/y": "-\n", "out/z": "-\n"})

    # ---- plan_wait: the watching ----
    gcase("plan_wait: a row a poll (the times, the status, how curl ended, the state, the phase), until the job is done, and the last answer is kept as <prefix>-get", 'call_core x POST /v1/sys/os/upgrade/plan 20; plan_wait plan-q; cut -f3- "$OUT/plan-q.polls.tsv" | tr "\\n" "," >"$OUT/rows"',
          files={"out/rows": "200\t0\trunning\tsources,200\t0\trunning\tupdate,200\t0\tdone\t-,", "out/plan-q-get.code": "200\n", "out/plan-q-get.hdr": "HTTP/1.1 200*",
                 "out/plan-q-get.json": '{"success": 200*', "out/plan-q.timeout": None, "out/plan-q-get.t0": "*", "out/plan-q-get.t1": "*", "out/plan-q-get.curl": "0\n"})
    gcase("plan_wait: the sandbox directories seen while the job runs are written down, one a look", 'call_core x POST /v1/sys/os/upgrade/plan 20; plan_wait plan-q; sort -u "$OUT/plan-q.sandbox-seen" >"$OUT/u"',
          files={"out/u": "/var/tmp/casaos-os-plan.481516\n"}, calls=[])
    gcase("plan_wait: a job that fails ends the watching, with the state failed", 'call_core x POST /v1/sys/os/upgrade/plan 20; plan_wait plan-q; tail -n 1 "$OUT/plan-q.polls.tsv" | cut -f3- >"$OUT/last"; wc -l <"$OUT/plan-q.polls.tsv" | tr -d " " >"$OUT/n"',
          setup=lambda tmp, j: open(j("root/etc/apt/sources.list.d/osp-cdrom.list"), "w").write("x\n"), files={"out/last": "200\t0\tfailed\tsources\n", "out/n": "1\n", "out/plan-q-get.json": '{"success": 200*'})
    ALWAYS = 'call_core() { local n="$1"; echo \'{"data":{"state":"running","phase":"update"}}\' >"$OUT/$n.json"; echo 200 >"$OUT/$n.code"; echo 0 >"$OUT/$n.curl"; echo 1 >"$OUT/$n.t0"; echo 2 >"$OUT/$n.t1"; : >"$OUT/$n.hdr"; }; '
    gcase("plan_wait: a job that does not end says so when PLAN_WAIT is over, and the watching stops", ALWAYS + 'PLAN_WAIT=1; plan_wait plan-q', files={"out/plan-q.timeout": "1\n"})
    gcase("plan_wait: the timeout of an earlier watching is not left behind", 'echo 900 >"$OUT/plan-q.timeout"; call_core x POST /v1/sys/os/upgrade/plan 20; plan_wait plan-q', files={"out/plan-q.timeout": None})
    gcase("plan_wait: a body that is not JSON is a row with `-` for the state and the phase, and the watching goes on until its time is over",
          ALWAYS.replace('\'{"data":{"state":"running","phase":"update"}}\'', 'nope') + 'PLAN_WAIT=0; plan_wait plan-q; cut -f3- "$OUT/plan-q.polls.tsv" >"$OUT/rows"',
          files={"out/rows": "200\t0\t-\t-\n", "out/plan-q.timeout": "0\n"})
    gcase("sandbox_dirs: the directories of the plan in /var/tmp, and only those", 'mkdir "$TMP_DIR/casaos-os-plan.1" "$TMP_DIR/other" "$TMP_DIR/casaos-os-plan.2"; : >"$TMP_DIR/casaos-os-plan.file"; sandbox_dirs | sed "s#^.*/var/tmp#T#" >"$OUT/d"',
          files={"out/d": "T/casaos-os-plan.1\nT/casaos-os-plan.2\n"})

    # ---- the first call of the route, the runbook ----
    gcase("probe_plan_route: the first call is kept as plan-idle, and a box that answers goes on", 'probe_plan_route; echo "$?" >"$OUT/rc"', files={"out/rc": "0\n", "out/plan-idle.code": "200\n", "out/aborted": None}, calls=["curl GET /v1/sys/os/upgrade/plan"])
    gcase("probe_plan_route: a 404 ends the run, and says that the release has no such route", 'probe_plan_route', prelude_env={"FAKE_CORE_MODE": "route404"}, returned=False,
          files={"out/aborted": "the release under test has no /v1/sys/os/upgrade/plan\n", "out/plan-idle.code": "404\n"})
    gcase("probe_plan_route: an answer that is not a 404 (the core is down: nothing answers) is not the answer of a release with no route", 'curl() { printf 000; return 7; }; probe_plan_route; echo "$?" >"$OUT/rc"',
          files={"out/rc": "0\n", "out/plan-idle.code": "000\n", "out/aborted": None})

    def runbook_setup(text_edit=None):
        def setup(tmp, j):
            rb = fxj("runbook")
            if text_edit:
                rb["data"]["text"] = text_edit(rb["data"]["text"])
            with open(j("out", "runbook-p1.json"), "w", newline="\n") as f:
                json.dump(rb, f)
        return setup
    gcase("runbook_shn: the text of the runbook is split into its nine blocks by their own markers, each read by sh -n, and a row says its index, its id and what sh -n said",
          "runbook_shn runbook-p1; cat \"$OUT/runbook-p1.shn.tsv\" | tr '\\t\\n' ' ;' >\"$OUT/rows\"; ls \"$OUT\"/rb-block-*.sh | wc -l | tr -d ' ' >\"$OUT/n\"", setup=runbook_setup(),
          files={"out/rows": "0 verify 0;1 timers 0;2 snapshot 0;3 sources 0;4 download 0;5 minimal 0;6 full 0;7 prereboot 0;8 after 0;", "out/n": "9\n"})
    gcase("runbook_shn: a block that sh cannot read has a status that is not 0, and the others are read all the same", "runbook_shn runbook-p1; awk -F'\\t' '$3 != 0 {print $1, $2}' \"$OUT/runbook-p1.shn.tsv\" >\"$OUT/bad\"",
          setup=runbook_setup(lambda t: t.replace("# <<< END BLOCK 4\n", "if then fi (\n# <<< END BLOCK 4\n")), files={"out/bad": "4 download\n"})
    # what the call leaves in the evidence, and nothing else: the nine blocks and what sh -n said of them, the text, the two tables (a block that was RUN would leave its own files, or make its
    # directory under /var/backups, and a block made executable is a block somebody means to run)
    RB_LS = " ".join(sorted(["rb-block-%d.%s" % (i, e) for i in range(9) for e in ("err", "sh")] + ["rb-index.tsv", "runbook-p1.json", "runbook-p1.shn.tsv", "runbook-p1.text"])) + " "
    gcase("runbook_shn: nothing of the runbook is run (no file of the blocks is made executable, nothing is left in the evidence but the blocks and the tables, and /var/backups, where the blocks make their directory, is as it was)",
          'chmod() { printf "%s\\n" "chmod $*" >>"$FAKE/calls"; }; b0=$(ls /var/backups 2>/dev/null | wc -l | tr -d " "); runbook_shn runbook-p1; x=$(find "$OUT" -name "rb-block-*" -perm -u+x | wc -l | tr -d " "); b1=$(ls /var/backups 2>/dev/null | wc -l | tr -d " "); '
          'l=$(ls "$OUT" | sort | tr "\\n" " "); printf "%s" "$l" >"$OUT/ls"; printf "%s\\n" "$x" >"$OUT/exec"; if [ "$b0" = "$b1" ]; then echo same; else echo changed; fi >"$OUT/backups"',
          setup=runbook_setup(), files={"out/exec": "0\n", "out/backups": "same\n", "out/ls": RB_LS}, no_calls=["chmod"])
    gcase("runbook_shn: a runbook that is not there (a 409 answer) has no row, and no block", "echo '{\"success\":409}' >\"$OUT/runbook-p1.json\"; runbook_shn runbook-p1; wc -c <\"$OUT/runbook-p1.shn.tsv\" | tr -d ' ' >\"$OUT/n\"", files={"out/n": "0\n"})
    gcase("runbook_shn: the blocks of an earlier text are not left in the evidence", "echo old >\"$OUT/rb-block-7.sh\"; echo '{\"success\":409}' >\"$OUT/runbook-p1.json\"; runbook_shn runbook-p1", files={"out/rb-block-7.sh": None})

    # ---- what is written down of the box ----
    gcase("copy_sources: sources.list and the files of sources.list.d that apt reads, copied whole, with their names in the index", 'copy_sources; cat "$OUT/sources.index" >"$OUT/i"',
          files={"out/i": "sources.list\nsources.list.d/docker.list\n", "out/src-main": MEASURED_BOX["files"]["sources.list"], "out/src-d-docker.list": MEASURED_BOX["files"]["sources.list.d/docker.list"]})
    gcase("copy_sources: a file whose name apt does not read (a backup, a hidden file, a .dpkg-old) is not copied, and a name that is apt's is", 'cd "$APT_DIR/sources.list.d"; : >backup~; : >.hidden; : >docker.list.dpkg-old; : >z.sources; : >"a b.list"; cd - >/dev/null; copy_sources; cat "$OUT/sources.index" >"$OUT/i"',
          files={"out/i": "sources.list\nsources.list.d/docker.list\nsources.list.d/z.sources\n", "out/src-d-backup~": None})
    gcase("copy_sources: a box with no sources.list says so by leaving it out of the index", 'rm "$APT_DIR/sources.list"; copy_sources; cat "$OUT/sources.index" >"$OUT/i"', files={"out/i": "sources.list.d/docker.list\n", "out/src-main": None})
    gcase("copy_aptconf: the fragments of apt.conf.d that apt reads, copied, in the index, and the one over 64 KiB named and not copied", 'python3 -c "open(\'$APT_DIR/apt.conf.d/99big\', \'w\').write(\'x\' * 65537)"; : >"$APT_DIR/apt.conf.d/99a~"; : >"$APT_DIR/apt.conf.d/.hidden"; copy_aptconf; cat "$OUT/aptconf.index" >"$OUT/i"',
          files={"out/i": "01autoremove\n01autoremove-kernels\n20auto-upgrades\n20listchanges\n50unattended-upgrades\n70debconf\n99big\n", "out/aptconf.large": "99big\n", "out/aptconf.nonregular": "", "out/aptconf.dirs": "", "out/aptconf-d-99big": None,
                 "out/aptconf-d-70debconf": APTCONF["70debconf"], "out/aptconf-main": None})
    gcase("copy_aptconf: a directory in apt.conf.d is in the index and in aptconf.dirs (apt passes over it without a word, so the report does not name it) and is not copied; an apt.conf that is a directory is named as not regular",
          'mkdir "$APT_DIR/apt.conf.d/99dir" "$APT_DIR/apt.conf"; copy_aptconf; cat "$OUT/aptconf.index" >"$OUT/i"',
          files={"out/aptconf.dirs": "99dir\n", "out/aptconf.nonregular": "apt.conf\n", "out/aptconf-d-99dir": None, "out/aptconf-main": None, "out/aptconf.large": "",
                 "out/i": "01autoremove\n01autoremove-kernels\n20auto-upgrades\n20listchanges\n50unattended-upgrades\n70debconf\n99dir\n"})
    gcase("copy_aptconf: a link to a directory is a link (named as not regular), and not a directory of the 200 that are passed over without a word",
          'f="$APT_DIR/apt.conf.d/99dlink"; ln -s "$APT_DIR/apt.conf.d" "$f" 2>/dev/null; copy_aptconf; if [ -L "$f" ]; then grep -qx 99dlink "$OUT/aptconf.nonregular" && ! grep -qx 99dlink "$OUT/aptconf.dirs" && echo ok || echo no; else echo ok; fi >"$OUT/r"',
          files={"out/r": "ok\n"})
    gcase("copy_aptconf: a fifo in apt.conf.d is named as not regular and not copied (a copy of it would wait for ever), where the box can make one",
          'f="$APT_DIR/apt.conf.d/99fifo"; if mkfifo "$f" 2>/dev/null && [ -p "$f" ]; then copy_aptconf; grep -qx 99fifo "$OUT/aptconf.nonregular" && ! grep -qx 99fifo "$OUT/aptconf.dirs" && [ ! -e "$OUT/aptconf-d-99fifo" ] && echo ok || echo no; else echo ok; fi >"$OUT/r"',
          files={"out/r": "ok\n"})
    gcase("copy_aptconf: a name with a colon is a name of apt (apt 2.2.4 reads it, and so does the core): it is in the index and copied, and a name that apt does not read is not",
          'if : >"$APT_DIR/apt.conf.d/99a:b" 2>/dev/null && [ -e "$APT_DIR/apt.conf.d/99a:b" ]; then echo "// x" >"$APT_DIR/apt.conf.d/:lead"; : >"$APT_DIR/apt.conf.d/99a:b~"; copy_aptconf; '
          'n=$(grep -c -x -e 99a:b -e :lead "$OUT/aptconf.index"); t=$(grep -c "~" "$OUT/aptconf.index" || true); [ "$n" = 2 ] && [ "$t" = 0 ] && [ "$(cat "$OUT/aptconf-d-:lead")" = "// x" ] && echo ok || echo no; else echo ok; fi >"$OUT/r"',
          files={"out/r": "ok\n"})
    gcase("copy_aptconf: a link in apt.conf.d is named as not regular and not copied, where the box can make one (a Windows checkout copies the file, and the directory case above is the proof there)",
          'f="$APT_DIR/apt.conf.d/99link"; ln -s 70debconf "$f" 2>/dev/null; copy_aptconf; if [ -L "$f" ]; then grep -qx 99link "$OUT/aptconf.nonregular" && [ ! -e "$OUT/aptconf-d-99link" ] && echo ok || echo no; else echo ok; fi >"$OUT/r"',
          files={"out/r": "ok\n"})
    gcase("copy_aptconf: a regular file is not named as not regular", 'copy_aptconf', files={"out/aptconf.nonregular": ""})
    gcase("copy_aptconf: apt.conf is copied when the box has one", 'echo \'Acquire::http::Proxy "http://x";\' >"$APT_DIR/apt.conf"; copy_aptconf', files={"out/aptconf-main": 'Acquire::http::Proxy "http://x";\n'})
    gcase("tmp_snapshot: the listing of /var/tmp, sorted, whole or not at all", 'mkdir -p "$TMP_DIR/b/c"; : >"$TMP_DIR/a"; tmp_snapshot t; sed "s#^.*/var/tmp#T#" "$OUT/tmp-t.txt" >"$OUT/l"', files={"out/l": "T/a\nT/b\nT/b/c\nT/systemd-private-aaaa\n"})
    gcase("witnesses: the containers that run, with the time they started at and their pid, sorted, a tab between", 'witnesses before; cut -f1 "$OUT/witnesses-before.txt" | tr "\\n" " " >"$OUT/n"; awk -F"\\t" \'{print NF, ($2 ~ /^2026-10-12T08:00:00Z/), ($3 ~ /^[0-9]+$/)}\' "$OUT/witnesses-before.txt" | sort -u >"$OUT/nf"',
          files={"out/n": "p-always p-db p-unless-stopped p-web ", "out/nf": "3 1 1\n"})
    gcase("witnesses: a container that was restarted has another start time in the next listing", 'witnesses before; echo X >"$FAKE/restarted.p-db"; witnesses after; diff "$OUT/witnesses-before.txt" "$OUT/witnesses-after.txt" | grep -c "^[<>]" >"$OUT/d" || true', files={"out/d": "2\n"})
    gcase("window_snapshot: the files, the units, the sockets and the listing of /var/tmp, under the tag", 'INV_TREES=("$INV_ROOT/etc"); write_manifest_tool "$LIBDIR/manifest.py"; window_snapshot t',
          files={"out/inv-t.tsv": '"/etc"\td*', "out/units-t.tsv": "*", "out/listen-t.txt": "*", "out/tmp-t.txt": "*"})

    # ---- the dirt and the undo of every step, with stand-ins that keep the box in files ----
    SD = "$APT_DIR/sources.list.d"
    for sid, fname, content in (("format", "osp-deb822.sources", "Types: deb\nURIs: http://deb.debian.org/debian\nSuites: bullseye\nComponents: main\n"), ("cdrom", "osp-cdrom.list", "deb cdrom:[Plan proof disc 1]/ bullseye main\n"),
                                ("alias", "osp-alias.list", "deb http://deb.debian.org/debian stable main\n"), ("target", "osp-target.list", "deb http://deb.debian.org/debian bookworm main\n"),
                                ("mixed", "osp-mixed.list", "deb http://deb.debian.org/debian bullseye main\ndeb https://download.docker.com/linux/debian bullseye stable\n"),
                                ("deadmirror", "osp-deadmirror.list", "deb http://ftp9.zz.debian.org/debian bullseye main\n")):
        gcase("%s: the file %s is written, in the directory of apt's sources, and the undo takes it away, and nothing else of the box" % (sid, fname),
              'in_subshell dirty_%s; echo "$STATUS" >"$OUT/rc1"; cp "%s/%s" "$OUT/content"; in_subshell undone_%s; echo "$STATUS" >"$OUT/d"; in_subshell undo_%s; ls "%s" >"$OUT/left"; in_subshell undone_%s; echo "$STATUS" >"$OUT/rc2"' % (sid, SD, fname, sid, sid, SD, sid),
              files={"out/rc1": "0\n", "out/content": content, "out/d": "1\n", "out/left": "docker.list\n", "out/rc2": "0\n"})
    gcase("busy: another process holds dpkg's lock, and the undo gives it back", 'write_lock_tools "$LIBDIR"; in_subshell dirty_busy; echo "$STATUS" >"$OUT/rc1"; lock_state >"$OUT/during"; in_subshell undone_busy; echo "$STATUS" >"$OUT/d"; in_subshell undo_busy; in_subshell undone_busy; echo "$STATUS" >"$OUT/u"',
          files={"out/rc1": "0\n", "out/during": "held\n", "out/d": "1\n", "out/u": "0\n"})
    gcase("noroom: a small tmpfs is mounted over /var/tmp, and the undo takes it away", 'in_subshell dirty_noroom; echo "$STATUS" >"$OUT/rc1"; in_subshell undone_noroom; echo "$STATUS" >"$OUT/d"; in_subshell undo_noroom; echo "$STATUS" >"$OUT/rc2"; in_subshell undone_noroom; echo "$STATUS" >"$OUT/u"',
          files={"out/rc1": "0\n", "out/d": "1\n", "out/rc2": "0\n", "out/u": "0\n"}, calls=["mount -t tmpfs -o size=64m,mode=1777 tmpfs", "umount "])
    gcase("noroom: the tmpfs is 64 MiB, far under the 640 MiB that a plan asks /var/tmp to have free", 'in_subshell dirty_noroom', calls=["size=64m"], no_calls=["size=640m", "size=1g"])
    gcase("noroom: a tmpfs that cannot be mounted is a dirt that failed (an invalid step, not a FAIL of the feature)", 'mount() { return 32; }; in_subshell dirty_noroom; echo "$STATUS" >"$OUT/rc"', files={"out/rc": "32\n"})
    gcase("noroom: an undo that cannot take the tmpfs away is tried three times, and fails", 'touch "$FAKE/umount.fail"; in_subshell dirty_noroom; in_subshell undo_noroom; echo "$STATUS" >"$OUT/rc"; grep -c "^umount" "$FAKE/calls" >"$OUT/n"', files={"out/rc": "1\n", "out/n": "3\n"})
    gcase("hold: base-files is put on hold, and the undo takes the hold away", 'in_subshell dirty_hold; echo "$STATUS" >"$OUT/rc1"; cp "$FAKE/holds" "$OUT/during"; in_subshell undone_hold; echo "$STATUS" >"$OUT/d"; in_subshell undo_hold; in_subshell undone_hold; echo "$STATUS" >"$OUT/u"',
          files={"out/rc1": "0\n", "out/during": "base-files\n", "out/d": "1\n", "out/u": "0\n"})

    # ---- the canaries: a real hook of apt-get update that a filter of text does not see ----
    SPLICE = 'APT::Update::Post-In/**/voke-Success {"touch /var/lib/apt/periodic/osp-canary-splice";};\n'
    INCLUDE_F = '#include "/etc/apt/osp-canary-include.conf";\n'
    INCLUDE_H = 'APT::Update::Post-Invoke-Success {"touch /var/lib/apt/periodic/osp-canary-include";};\n'
    gcase("canarysplice: the fragment 99osp-canary-splice is written in apt.conf.d, with the key spliced by a comment (Post-In/**/voke-Success) and a hook that touches osp-canary-splice, and the undo takes it away",
          'in_subshell dirty_canarysplice; echo "$STATUS" >"$OUT/rc1"; cp "$APT_DIR/apt.conf.d/99osp-canary-splice" "$OUT/content"; in_subshell undone_canarysplice; echo "$STATUS" >"$OUT/d"; in_subshell undo_canarysplice; ls "$APT_DIR/apt.conf.d" | grep -c canary >"$OUT/n" || true; in_subshell undone_canarysplice; echo "$STATUS" >"$OUT/rc2"',
          files={"out/rc1": "0\n", "out/content": SPLICE, "out/d": "1\n", "out/n": "0\n", "out/rc2": "0\n"})
    gcase("canaryinclude: the fragment 99osp-canary-include #includes /etc/apt/osp-canary-include.conf, which holds a real Post-Invoke-Success hook that touches osp-canary-include; the undo takes both away",
          'in_subshell dirty_canaryinclude; echo "$STATUS" >"$OUT/rc1"; cp "$APT_DIR/apt.conf.d/99osp-canary-include" "$OUT/content"; cp "$APT_DIR/osp-canary-include.conf" "$OUT/hook"; in_subshell undone_canaryinclude; echo "$STATUS" >"$OUT/d"; '
          'in_subshell undo_canaryinclude; ls "$APT_DIR" "$APT_DIR/apt.conf.d" | grep -c canary >"$OUT/n" || true; in_subshell undone_canaryinclude; echo "$STATUS" >"$OUT/rc2"',
          files={"out/rc1": "0\n", "out/content": INCLUDE_F, "out/hook": INCLUDE_H, "out/d": "1\n", "out/n": "0\n", "out/rc2": "0\n"})
    for cid_, cfile_ in (("canarysplice", "osp-canary-splice"), ("canaryinclude", "osp-canary-include")):
        gcase("%s: the file that the hook touches is not there before the plan (the dirt takes away one an earlier run left), and the check after the plan says absent" % cid_,
              'touch "$PERIODIC_DIR/%s"; in_subshell dirty_%s; [ ! -e "$PERIODIC_DIR/%s" ] && echo gone >"$OUT/g"; in_subshell after_%s; cp "$OUT/step-%s.canary" "$OUT/seen"' % (cfile_, cid_, cfile_, cid_, cid_),
              files={"out/g": "gone\n", "out/seen": "absent\n"})
        gcase("%s: the check after the plan says present when the hook made its file, and that is all it says" % cid_, 'in_subshell dirty_%s; touch "$PERIODIC_DIR/%s"; in_subshell after_%s' % (cid_, cfile_, cid_),
              files={"out/step-%s.canary" % cid_: "present\n"})
        gcase("%s: an undo that finds the file of the hook there takes it away too, and the check that it held says the box is back only then" % cid_,
              'in_subshell dirty_%s; touch "$PERIODIC_DIR/%s"; in_subshell undone_%s; echo "$STATUS" >"$OUT/d"; in_subshell undo_%s; in_subshell undone_%s; echo "$STATUS" >"$OUT/u"; [ ! -e "$PERIODIC_DIR/%s" ] && echo gone >"$OUT/g"' % (cid_, cfile_, cid_, cid_, cid_, cfile_),
              files={"out/d": "1\n", "out/u": "0\n", "out/g": "gone\n"})
        gcase("%s: a directory of the periodic stamps that the box does not have is made (the hook has somewhere to touch), and the step does not fail for it" % cid_,
              'rm -rf "$PERIODIC_DIR"; in_subshell dirty_%s; echo "$STATUS" >"$OUT/rc"; [ -d "$PERIODIC_DIR" ] && echo made >"$OUT/m"' % cid_,
              files={"out/rc": "0\n", "out/m": "made\n"})
    for cid_, parts_ in (("canarysplice", ("$APT_DIR/apt.conf.d/99osp-canary-splice", "$PERIODIC_DIR/osp-canary-splice")),
                         ("canaryinclude", ("$APT_DIR/apt.conf.d/99osp-canary-include", "$APT_DIR/osp-canary-include.conf", "$PERIODIC_DIR/osp-canary-include"))):
        for i_, part_ in enumerate(parts_):
            gcase("%s: the box is not back while only %s is there" % (cid_, part_.rsplit("/", 1)[1]), 'mkdir -p "$APT_DIR/apt.conf.d" "$PERIODIC_DIR"; : >"%s"; in_subshell undone_%s; echo "$STATUS" >"$OUT/s"' % (part_, cid_),
                  files={"out/s": "1\n"})
        gcase("%s: the box is back when none of them is there" % cid_, 'mkdir -p "$APT_DIR/apt.conf.d" "$PERIODIC_DIR"; in_subshell undone_%s; echo "$STATUS" >"$OUT/s"' % cid_, files={"out/s": "0\n"})
    gcase("the trap of the script undoes a canary that was in the middle of its dirt when the script died: neither the fragment nor the included file is left", 'guard_run; PENDING_UNDO=canaryinclude; dirty_canaryinclude; false',
          files={"out/step-canaryinclude.undo-rc-exit": "0\n"}, returned=False, rc=1)
    # the third canary: the option of the gpgv that apt runs as _apt, which names a file for it to write in the box's own lists of apt
    GPGV_F = 'Acquire::gpgv::Options::0 "--log-file=/var/lib/apt/lists/partial/osp-canary";\n'
    gcase("canarygpgv: the fragment 99osp-canary-gpgv is written in apt.conf.d, with the option of gpgv that names /var/lib/apt/lists/partial/osp-canary, and the undo takes it away",
          'in_subshell dirty_canarygpgv; echo "$STATUS" >"$OUT/rc1"; cp "$APT_DIR/apt.conf.d/99osp-canary-gpgv" "$OUT/content"; in_subshell undone_canarygpgv; echo "$STATUS" >"$OUT/d"; in_subshell undo_canarygpgv; '
          'ls "$APT_DIR/apt.conf.d" | grep -c canary >"$OUT/n" || true; in_subshell undone_canarygpgv; echo "$STATUS" >"$OUT/rc2"',
          files={"out/rc1": "0\n", "out/content": GPGV_F, "out/d": "1\n", "out/n": "0\n", "out/rc2": "0\n"})
    gcase("canarygpgv: the file that gpgv writes is not there before the plan (the dirt takes away one an earlier run left), and the check after the plan says absent",
          'touch "$LISTS_PARTIAL/osp-canary"; in_subshell dirty_canarygpgv; [ ! -e "$LISTS_PARTIAL/osp-canary" ] && echo gone >"$OUT/g"; in_subshell after_canarygpgv; cp "$OUT/step-canarygpgv.canary" "$OUT/seen"',
          files={"out/g": "gone\n", "out/seen": "absent\n"})
    gcase("canarygpgv: the check after the plan says present when gpgv made its file, and that is all it says", 'in_subshell dirty_canarygpgv; touch "$LISTS_PARTIAL/osp-canary"; in_subshell after_canarygpgv', files={"out/step-canarygpgv.canary": "present\n"})
    gcase("canarygpgv: an undo that finds the file there takes it away too, and the check that it held says the box is back only then",
          'in_subshell dirty_canarygpgv; touch "$LISTS_PARTIAL/osp-canary"; in_subshell undone_canarygpgv; echo "$STATUS" >"$OUT/d"; in_subshell undo_canarygpgv; in_subshell undone_canarygpgv; echo "$STATUS" >"$OUT/u"; [ ! -e "$LISTS_PARTIAL/osp-canary" ] && echo gone >"$OUT/g"',
          files={"out/d": "1\n", "out/u": "0\n", "out/g": "gone\n"})
    gcase("canarygpgv: a box with no apt.conf.d gets one, with the fragment in it, and the step does not fail for it", 'rm -rf "$APT_DIR/apt.conf.d"; in_subshell dirty_canarygpgv; echo "$STATUS" >"$OUT/rc"; [ -f "$APT_DIR/apt.conf.d/99osp-canary-gpgv" ] && echo made >"$OUT/m"',
          files={"out/rc": "0\n", "out/m": "made\n"})
    for part_ in ("$APT_DIR/apt.conf.d/99osp-canary-gpgv", "$LISTS_PARTIAL/osp-canary"):
        gcase("canarygpgv: the box is not back while only %s is there" % part_.rsplit("/", 1)[1], 'mkdir -p "$APT_DIR/apt.conf.d" "$LISTS_PARTIAL"; : >"%s"; in_subshell undone_canarygpgv; echo "$STATUS" >"$OUT/s"' % part_, files={"out/s": "1\n"})
    gcase("canarygpgv: the box is back when neither is there", 'mkdir -p "$APT_DIR/apt.conf.d" "$LISTS_PARTIAL"; in_subshell undone_canarygpgv; echo "$STATUS" >"$OUT/s"', files={"out/s": "0\n"})
    gcase("the trap of the script undoes the third canary that was in the middle of its dirt when the script died: neither the fragment nor the file is left", 'guard_run; PENDING_UNDO=canarygpgv; dirty_canarygpgv; touch "$LISTS_PARTIAL/osp-canary"; false',
          files={"out/step-canarygpgv.undo-rc-exit": "0\n"}, returned=False, rc=1)

    # ---- the edges of the guest's helpers: plan_wait, sandbox_dirs, copy_aptconf, noroom ----
    gcase("plan_wait: a directory that is there only once the job is done is not one of the directories seen while it ran",
          'n=0; call_core() { n=$((n + 1)); local st=running; if [ "$n" -ge 3 ]; then st=done; mkdir -p "$TMP_DIR/casaos-os-plan.late"; fi; printf \'{"data":{"state":"%s","phase":"update"}}\\n\' "$st" >"$OUT/$1.json"; echo 200 >"$OUT/$1.code"; echo 0 >"$OUT/$1.curl"; echo 1 >"$OUT/$1.t0"; echo 2 >"$OUT/$1.t1"; : >"$OUT/$1.hdr"; }; '
          'mkdir -p "$TMP_DIR/casaos-os-plan.early"; plan_wait plan-q; sed "s#^.*/var/tmp#T#" "$OUT/plan-q.sandbox-seen" | sort -u >"$OUT/u"', files={"out/u": "T/casaos-os-plan.early\n"})
    gcase("plan_wait: the rows of an earlier watching are not left in the file", 'echo old >"$OUT/plan-q.polls.tsv"; echo old >"$OUT/plan-q.sandbox-seen"; call_core x POST /v1/sys/os/upgrade/plan 20; plan_wait plan-q; cat "$OUT/plan-q.polls.tsv" "$OUT/plan-q.sandbox-seen" | grep -c old >"$OUT/n" || true',
          files={"out/n": "0\n"})
    gcase("sandbox_dirs: a directory of the plan inside another one, and one that is named like it and is not, are not the plan's", 'mkdir -p "$TMP_DIR/other/casaos-os-plan.9" "$TMP_DIR/casaos-os-other.1" "$TMP_DIR/casaos-os-plan.3"; sandbox_dirs | sed "s#^.*/var/tmp#T#" >"$OUT/d"',
          files={"out/d": "T/casaos-os-plan.3\n"})
    gcase("copy_aptconf: a fragment of exactly 64 KiB is copied, and one of a byte more is named and not copied", 'python3 -c "open(\'$APT_DIR/apt.conf.d/98exact\', \'w\').write(\'x\' * 65536); open(\'$APT_DIR/apt.conf.d/99over\', \'w\').write(\'x\' * 65537)"; copy_aptconf; cat "$OUT/aptconf.large" >"$OUT/l"; wc -c <"$OUT/aptconf-d-98exact" | tr -d " " >"$OUT/n"',
          files={"out/l": "99over\n", "out/n": "65536\n", "out/aptconf-d-99over": None})
    gcase("noroom: a mount that did nothing is a dirt that failed (the tmpfs is not there)", 'mount() { return 0; }; in_subshell dirty_noroom; echo "$STATUS" >"$OUT/rc"', files={"out/rc": "1\n"})

    # ---- plan_step and plan_hold: the dirt, a call, the watching, the runbook, the undo, the check that it held ----
    STUBS = ('call_core() { printf "%s\\n" "call_core $*" >>"$FAKE/calls"; printf "%s\\n" "${FAKE_POST_CODE:-202}" >"$OUT/$1.code"; }; plan_wait() { printf "%s\\n" "plan_wait $*" >>"$FAKE/calls"; }; log() { :; }; marker() { :; }; '
             'dirty_x() { echo dirty >>"$FAKE/calls"; ${DIRTY_CMD:-true}; }; undo_x() { echo undo >>"$FAKE/calls"; ${UNDO_CMD:-true}; }; undone_x() { echo undone >>"$FAKE/calls"; ${UNDONE_CMD:-true}; }; ')
    gcase("plan_step: the dirt, the POST, the watching, the runbook, the undo, the check that it held, in that order, and what each said", STUBS + 'plan_step x',
          files={"out/step-x.dirty-rc": "0\n", "out/step-x.undo-rc": "0\n", "out/step-x.undone": "yes\n", "out/steps.list": "x\n", "out/step-x.leftover": ""},
          ordered=["dirty", "call_core step-x-post POST /v1/sys/os/upgrade/plan", "plan_wait step-x", "call_core step-x-runbook GET /v1/sys/os/upgrade/runbook", "undo", "undone"])
    gcase("plan_step: a POST that is refused (409) is not watched, and has no runbook asked for", STUBS + 'plan_step x', prelude_env={"FAKE_POST_CODE": "409"}, no_calls=["plan_wait", "step-x-runbook"], ordered=["dirty", "call_core step-x-post", "undo", "undone"])
    gcase("plan_step: a dirt that failed is recorded, the plan is asked for and the box is undone all the same", STUBS + 'plan_step x', prelude_env={"DIRTY_CMD": "false"},
          files={"out/step-x.dirty-rc": "1\n", "out/step-x.undo-rc": "0\n", "out/step-x.undone": "yes\n"}, ordered=["dirty", "call_core step-x-post", "undo", "undone"])
    gcase("plan_step: an undo that failed is recorded, stays pending for the trap, and the check says the box is not back", STUBS + 'plan_step x; echo "pending=${PENDING_UNDO}" >"$OUT/p"',
          prelude_env={"UNDO_CMD": "false", "UNDONE_CMD": "false"}, files={"out/step-x.undo-rc": "1\n", "out/step-x.undone": "no\n", "out/p": "pending=x\n"})
    gcase("plan_step: an undo that held leaves nothing pending", STUBS + 'plan_step x; echo "pending=${PENDING_UNDO}" >"$OUT/p"', files={"out/p": "pending=\n"})
    gcase("plan_step: the undo is pending while the dirt and the call are made", STUBS + 'dirty_x() { echo "pending=${PENDING_UNDO}" >>"$FAKE/calls"; }; plan_step x', calls=["pending=x"])
    gcase("plan_step: a sandbox directory that is left in /var/tmp is written down once the box is undone", STUBS + 'undo_x() { mkdir "$TMP_DIR/casaos-os-plan.77"; }; plan_step x; sed "s#^.*/var/tmp#T#" "$OUT/step-x.leftover" >"$OUT/l"',
          files={"out/l": "T/casaos-os-plan.77\n"})
    gcase("plan_step: nothing left in /var/tmp is an empty file, which is a listing", STUBS + 'plan_step x', files={"out/step-x.leftover": ""})
    gcase("plan_hold: the hold, the plan p3, the runbook that a blocked plan does not have, the undo, in that order", STUBS + 'plan_hold',
          files={"out/step-hold.dirty-rc": "0\n", "out/step-hold.undo-rc": "0\n", "out/step-hold.undone": "yes\n", "out/step-hold.leftover": ""},
          ordered=["call_core plan-p3-post POST /v1/sys/os/upgrade/plan", "plan_wait plan-p3", "call_core runbook-p3 GET /v1/sys/os/upgrade/runbook"])
    gcase("plan_hold: the hold is on while the plan is made, and is taken away after", STUBS + 'dirty_hold() { echo hold >>"$FAKE/calls"; }; undo_hold() { echo unhold >>"$FAKE/calls"; }; undone_hold() { echo undone >>"$FAKE/calls"; }; plan_hold',
          ordered=["hold", "call_core plan-p3-post", "plan_wait plan-p3", "call_core runbook-p3", "unhold", "undone"])
    gcase("plan_hold: the undo of the hold is pending while the hold is made and the plan is asked for", STUBS + 'dirty_hold() { echo "pending=${PENDING_UNDO}" >>"$FAKE/calls"; }; plan_hold', calls=["pending=hold"])
    gcase("plan_run: the POST, then the watching, under the name of the run", STUBS + 'plan_run p2', ordered=["call_core plan-p2-post POST /v1/sys/os/upgrade/plan", "plan_wait plan-p2"])
    gcase("plan_run: a POST that is refused (409) began no job: it is not watched, and the report is left to say what the core refused", STUBS + 'plan_run p2', prelude_env={"FAKE_POST_CODE": "409"},
          calls=["call_core plan-p2-post POST /v1/sys/os/upgrade/plan"], no_calls=["plan_wait"], files={"out/plan-p2-post.code": "409\n", "out/plan-p2.polls.tsv": None})
    gcase("plan_run: a POST that is answered 200 (an earlier job still running) is not a job begun either", STUBS + 'plan_run p2', prelude_env={"FAKE_POST_CODE": "200"}, no_calls=["plan_wait"])
    gcase("plan_hold: a POST of the plan with the hold that is refused is not watched, the hold is undone all the same", STUBS + 'dirty_hold() { echo hold >>"$FAKE/calls"; }; undo_hold() { echo unhold >>"$FAKE/calls"; }; undone_hold() { echo undone >>"$FAKE/calls"; }; plan_hold',
          prelude_env={"FAKE_POST_CODE": "409"}, no_calls=["plan_wait"], ordered=["hold", "call_core plan-p3-post", "unhold", "undone"], files={"out/step-hold.undo-rc": "0\n"})
    gcase("plan_step: what the step looks at once its plan is done (after_<id>) is looked at after the watching and the runbook, and before the undo",
          STUBS + 'after_x() { echo after >>"$FAKE/calls"; }; plan_step x', ordered=["dirty", "call_core step-x-post", "plan_wait step-x", "call_core step-x-runbook", "after", "undo", "undone"], files={"out/step-x.after-rc": "0\n"})
    gcase("plan_step: a POST that is refused is not looked at after (there is no plan to look at)", STUBS + 'after_x() { echo after >>"$FAKE/calls"; }; plan_step x', prelude_env={"FAKE_POST_CODE": "409"}, no_calls=["after"], files={"out/step-x.after-rc": None})
    gcase("plan_step: a step with no after_<id> is no worse for it", STUBS + 'plan_step x', no_calls=["after"], files={"out/step-x.after-rc": None, "out/step-x.undone": "yes\n"})
    gcase("plan_step: an after_<id> that fails is recorded and the undo is done all the same", STUBS + 'after_x() { echo after >>"$FAKE/calls"; false; }; plan_step x', ordered=["after", "undo", "undone"], files={"out/step-x.after-rc": "1\n", "out/step-x.undone": "yes\n"})
    gcase("the trap of the script undoes the step that was in the middle of its dirt when the script died: the tmpfs is taken away", 'guard_run; PENDING_UNDO=noroom; dirty_noroom; false',
          files={"out/aborted": "the guest script ended on a failing command (exit 1)*", "out/step-noroom.undo-rc-exit": "0\n"}, calls=["umount "], returned=False, rc=1)
    gcase("the trap of the script undoes the hold when the script died while the plan with the hold was made", 'guard_run; PENDING_UNDO=hold; dirty_hold; false', files={"out/step-hold.undo-rc-exit": "0\n"}, calls=[], returned=False, rc=1)

    # ---- the window: the order of what it does ----
    WSTUB = ('inv_snapshot() { echo "snapshot $1" >>"$FAKE/calls"; }; tmp_snapshot() { echo "tmp $1" >>"$FAKE/calls"; }; witnesses() { echo "witnesses $1" >>"$FAKE/calls"; }; api() { echo "api $1 $2" >>"$FAKE/calls"; }; '
             'call_core() { echo "call_core $1 $2" >>"$FAKE/calls"; printf "%s\\n" "${FAKE_CODES:-200}" >"$OUT/$1.code"; case "$1" in plan-p1-post) echo 202 >"$OUT/$1.code" ;; esac; echo \'{}\' >"$OUT/$1.json"; }; '
             'plan_wait() { echo "plan_wait $1" >>"$FAKE/calls"; }; marker() { :; }; log() { :; }; ')
    gcase("window: the control, the first snapshot, the first call of the plan route, the runbook before any plan, the POST, the second POST, the watching, the last snapshot, in that order", WSTUB + 'sleep() { echo "sleep $*" >>"$FAKE/calls"; }; window',
          ordered=["snapshot ctl-a", "tmp ctl-a", "api ctl-core-1 /v1/sys/version/current", "api ctl-core-2 /v1/sys/version/current", "sleep 0", "snapshot ctl-b", "snapshot before", "witnesses before", "call_core plan-idle GET", "call_core runbook-idle GET",
                   "call_core plan-p1-post POST", "call_core plan-p1-post-2 POST", "plan_wait plan-p1", "snapshot after", "witnesses after"], files={"out/timer-units.txt": "logrotate.service\n"})
    gcase("window: the control waits CONTROL_SECONDS (320 when nothing says otherwise), and nothing else waits in the window", WSTUB + 'sleep() { echo "sleep $*" >>"$FAKE/calls"; }; window', prelude_env={"CONTROL_SECONDS": "17"}, calls=["sleep 17"], no_calls=["sleep 320", "sleep 1\n"])
    gcase("window: a 404 on the first call of the plan route ends the run there, inside the window: nothing else is called, and the second snapshot is never taken", WSTUB + 'call_core() { echo "call_core $1" >>"$FAKE/calls"; echo 404 >"$OUT/$1.code"; }; window',
          files={"out/aborted": "the release under test has no /v1/sys/os/upgrade/plan\n"}, calls=["snapshot before", "call_core plan-idle"], no_calls=["call_core runbook-idle", "plan-p1-post", "plan_wait", "snapshot after", "witnesses after"], returned=False)
    gcase("window: a first POST that is not accepted ends the run with what the core said, and the second snapshot is never taken", WSTUB + 'call_core() { echo "call_core $1" >>"$FAKE/calls"; echo 409 >"$OUT/$1.code"; echo \'{"data":{"error_code":"busy"}}\' >"$OUT/$1.json"; }; window',
          files={"out/aborted": "the first plan was not accepted: HTTP 409, busy\n"}, no_calls=["plan-p1-post-2", "plan_wait", "snapshot after"], returned=False)
    check("the guest waits 320 s by default in its control, as the readiness proof does", 'CONTROL_SECONDS="${CONTROL_SECONDS:-320}"' in readiness_text, "")

    # ---- the end to end: the real guest functions, the stand-in core, the real report ----
    E2E = ('write_manifest_tool "$LIBDIR/manifest.py"; write_lock_tools "$LIBDIR"; INV_TREES=("$INV_ROOT/etc" "$INV_ROOT/var/lib/dpkg" "$INV_ROOT/var/lib/apt" "$INV_ROOT/var/cache/apt" "$INV_ROOT/var/lib/casaos" "$INV_ROOT/var/log/apt" "$INV_ROOT/var/log/casaos"); '
           'guard_run; : >"$OUT/timeline.tsv"; echo d11 >"$OUT/leg"; echo "Debian GNU/Linux 11 (bullseye)" >"$OUT/os"; cp "$OS_RELEASE" "$OUT/os-release"; printf "kernel 5.10\\ndocker 28.0.4\\n" >"$OUT/box-facts"; HOST_NAME=osp-1a2b3c4d; SUDO_USER=casa; leak_needles; '
           'marker prepare_start; copy_sources; copy_aptconf; marker prepare_done; window; call_core runbook-p1 GET "${RUNBOOK_PATH}" 30; runbook_shn runbook-p1; plan_run p2; plan_hold; for id in "${PLAN_STEPS[@]}"; do plan_step "$id"; done; '
           'plan_run p4; date -u +%FT%TZ >"$OUT/finished"')

    def e2e(label, want_code, must_have=(), must_not_have=(), env_extra=None, snippet=E2E, after=None, setup=None):
        """the real guest functions, the stand-in tools and core, the real report: the report's verdict on what the guest left. after(tmp, j) says what else is wrong with the box.
        The run is kept for the end of this section, where all of them run side by side (each in a directory of its own): on Windows a process costs, and a run is a few hundred of them"""
        if not wanted(label):
            return
        count[0] += 1
        JOBS.append(lambda: e2e_run(label, want_code, must_have, must_not_have, env_extra, snippet, after, setup))

    def e2e_run(label, want_code, must_have, must_not_have, env_extra, snippet, after, setup):
        with tempfile.TemporaryDirectory() as tmp:
            env, j = sandbox(tmp)
            env.update({k: v.replace("{root}", j("root")) for k, v in (env_extra or {}).items()})
            if setup:
                setup(tmp, j)
            p = run_guarded([BASH, "-c", PRELUDE + ("" if HAVE_FCNTL else LOCK_SHIM) + ". \"$OS_RELEASE\"; " + snippet, "_", SCRIPT.replace("\\", "/")], env, float(os.environ.get("E2E_TIMEOUT", 900)))
            if p.returncode == 124 and "did not end" in p.stderr:
                return "end to end, " + label, "end to end, %s: %s" % (label, p.stderr)
            out = j("out")
            r = subprocess.run([sys.executable, REPORT, out, "d11"], capture_output=True, text=True, env=ENV, encoding="utf-8", errors="replace")
            text = r.stdout + r.stderr
            problems = after(tmp, j) if after else []
            if r.returncode != want_code:
                problems.append("the report exits %d, wanted %d" % (r.returncode, want_code))
            problems += ["missing %r" % s for s in must_have if s not in text]
            problems += ["should not say %r" % s for s in must_not_have if s in text]
            if "Traceback" in text:
                problems.append("the report crashed")
            if problems:
                return "end to end, " + label, "end to end, %s: %s\n--- guest stderr\n%s\n--- report\n%s" % (label, "; ".join(problems), p.stderr[-1500:], text[-2500:])
            return "end to end, " + label, None

    e2e("the real guest functions, a stand-in core, the real report: a healthy leg passes with every verdict", 0, ["# OS plan proof, d11 leg: PASS"] + ["**PASS** " + v for v in healthy_verdicts() + [V_STAMP]] +
        ["**NOT TESTABLE** " + n + ": " for n in NOT_TESTABLE_NAMES], ["FAIL", "INVALID"])
    e2e("a core that writes a file of /etc during the plan is caught by the invariance, naming the file", 1, ["**FAIL** " + V_INV_FILES, "/etc/osp-written"], ["Traceback"],
        env_extra={"FAKE_CORE_MODE": "write", "FAKE_CORE_WRITE": "{root}/etc/osp-written"})
    e2e("a core whose update runs the hooks of the box (apt's update-success-stamp is touched) is caught by name, by the stamp and by the files", 1, ["**FAIL** " + V_STAMP, "**FAIL** " + V_INV_FILES, "update-success-stamp"], ["Traceback"],
        env_extra={"FAKE_CORE_MODE": "stampwrite"})
    e2e("a core that restarts a service during the plan is caught by the invariance", 1, ["**FAIL** " + V_INV_UNITS, "casaos-gateway.service"], ["Traceback"], env_extra={"FAKE_CORE_MODE": "restart", "FAKE_CORE_UNIT": "casaos-gateway.service"})
    e2e("a core that opens a port during the plan is caught by the invariance", 1, ["**FAIL** " + V_INV_PORTS, "started listening tcp 0.0.0.0:9000"], ["Traceback"], env_extra={"FAKE_CORE_MODE": "listen"})
    e2e("a core that leaves its sandbox directory behind is caught by the listing of /var/tmp, and by every step that plans", 1,
        ["**FAIL** " + V_INV_TMP, "a sandbox directory is left", "**FAIL** " + V_STEP_LEFTOVER], ["Traceback"], env_extra={"FAKE_CORE_MODE": "leftover"})
    e2e("a core whose second POST starts a second job is caught by the sandbox directories seen", 1, ["**FAIL** " + V_SANDBOX_SEEN, "2 sandbox directories were seen"], ["Traceback"], env_extra={"FAKE_CORE_MODE": "dupjob"})
    e2e("a core whose plan_id is not stable is caught by the plans made back to back, and by the plan after the matrix", 1, ["**FAIL** " + V_ID_STABLE, "**FAIL** " + V_ID_BACK], ["Traceback"], env_extra={"FAKE_CORE_MODE": "badid"})
    e2e("a core that leaves out nothing of the hooks is caught by name", 1, ["**FAIL** " + V_HOOKS], ["Traceback"], env_extra={"FAKE_CORE_MODE": "nohooks"})
    e2e("a core that shows a line of the sources as it was is caught by the sources", 1, ["**FAIL** " + V_SOURCES], ["Traceback"], env_extra={"FAKE_CORE_MODE": "badsources"})
    e2e("a core that does not say no-store is caught by every answer", 1, ["**FAIL** " + V_NOSTORE], ["Traceback"], env_extra={"FAKE_CORE_MODE": "nostore"})
    e2e("a core that does not refuse when dpkg's lock is held is caught by the step busy, by name", 1, ["**FAIL** " + f_step(STEP_BY_ID["busy"])], ["Traceback", "INVALID"], env_extra={"FAKE_CORE_MODE": "nobusy"})
    e2e("a core that does not see that there is no room is caught by the step noroom, by name", 1, ["**FAIL** " + f_step(STEP_BY_ID["noroom"])], ["Traceback", "INVALID"], env_extra={"FAKE_CORE_MODE": "noroomignored"})
    e2e("a core that writes the runbook of a job that failed is caught", 1, ["**FAIL** " + V_STEP_NORUNBOOK], ["Traceback", "INVALID"], env_extra={"FAKE_CORE_MODE": "runbookfailed"})
    e2e("a core whose sandbox runs the hooks of the box and keeps the option of gpgv (the three canaries make their file) is caught by the three canary steps, by name", 1,
        ["**FAIL** " + f_step(STEP_BY_ID["canarysplice"]), "**FAIL** " + f_step(STEP_BY_ID["canaryinclude"]), "**FAIL** " + f_step(STEP_BY_ID["canarygpgv"]), "osp-canary-splice is there after the plan", "osp-canary-include is there after the plan",
         "/var/lib/apt/lists/partial/osp-canary is there after the plan"], ["Traceback", "INVALID"],
        env_extra={"FAKE_CORE_MODE": "canaryfires"})
    e2e("a core that does not name the canary fragments as left out is caught by the three canary steps, by name", 1,
        ["**FAIL** " + f_step(STEP_BY_ID["canarysplice"]), "**FAIL** " + f_step(STEP_BY_ID["canaryinclude"]), "**FAIL** " + f_step(STEP_BY_ID["canarygpgv"]), "does not name 99osp-canary-splice as left out", "does not name 99osp-canary-gpgv as left out"],
        ["Traceback", "INVALID"], env_extra={"FAKE_CORE_MODE": "canaryunnamed"})
    for run_, nth_ in (("p2", 2), ("p3", 3), ("p4", 13)):
        e2e("a core that refuses the POST of the plan %s (the %dth that would begin a job) is an invalid run that says so, and the earlier plan it answers is not judged as this run's" % (run_, nth_), 1,
            ["INVALID RUN", "the POST of the plan %s did not begin a job: HTTP 409 (busy)" % run_], ["Traceback", "**PASS** " + {"p2": V_ID_STABLE, "p3": V_ID_HOLD, "p4": V_ID_BACK}[run_]],
            env_extra={"FAKE_CORE_REFUSE": str(nth_)})
    e2e("a release that has no such route is found out by the first call, and the run stops there: an invalid run, by name", 1,
        ["INVALID RUN", "the guest stopped early: the release under test has no /v1/sys/os/upgrade/plan", "plan-idle got no answer of the route", "the release under test has no such route"], ["Traceback"], env_extra={"FAKE_CORE_MODE": "route404"},
        after=lambda tmp, j: [m for m, bad in (("the run did not take the first snapshot of the window before the first call", not os.path.exists(j("out/inv-before.tsv"))),
                                               ("the run went on to the second snapshot of the window after the first call said 404", os.path.exists(j("out/inv-after.tsv"))),
                                               ("the run went on to the plans after the first call said 404", os.path.exists(j("out/plan-p1-post.code"))),
                                               ("the run went on to the dirty matrix after the first call said 404", os.path.exists(j("out/steps.list")))) if bad])
    e2e("a core that is busy on the clean box is an invalid run, and no FAIL of the feature", 1, ["INVALID RUN", "the guest stopped early: the first plan was not accepted: HTTP 409, busy"], ["Traceback", "**FAIL** " + V_POST], env_extra={"FAKE_CORE_MODE": "busyalways"})
    e2e("a guest whose undo fails is an invalid run and not a FAIL of the feature", 1, ["INVALID RUN", "step cdrom: the undo failed"], ["**FAIL** " + V_ID_BACK],
        snippet=E2E.replace("guard_run;", "guard_run; undo_cdrom() { return 1; }; undone_cdrom() { return 1; };"))
    e2e("a guest whose dirt does nothing is an invalid run for that step (the dirt could not be made)", 1, ["INVALID RUN", "step alias: the box could not be dirtied"], [], snippet=E2E.replace("guard_run;", "guard_run; dirty_alias() { return 1; };"))
    e2e("a guest whose hold cannot be made is an invalid run, and the plan with the hold is not judged on it", 1, ["INVALID RUN", "the hold step: base-files could not be put on hold"], ["**FAIL** " + V_ID_HOLD], snippet=E2E.replace("guard_run;", "guard_run; dirty_hold() { return 1; };"))
    e2e("a guest that dies in the middle of a step still undoes it (the tmpfs is taken away), and the report says the run stopped", 1,
        ["INVALID RUN", "the guest stopped early", "the guest script ended on a failing command (exit 7)"], ["Traceback"],
        snippet=E2E.replace("guard_run;", 'guard_run; eval "$(declare -f call_core | sed \'1s/call_core/orig_call_core/\')"; call_core() { if [ "$1" = step-noroom-post ]; then exit 7; fi; orig_call_core "$@"; };'),
        after=lambda tmp, j: [] if not os.path.exists(j("fake/tmpfs")) else ["the dirt of the step that was in the middle (the tmpfs) was left on the box"])
    with concurrent.futures.ThreadPoolExecutor(int(os.environ.get("E2E_WORKERS") or min(8, os.cpu_count() or 2))) as pool:
        def again(job):
            label, bad = job()
            if bad and "did not end in" in bad:
                label, bad = job()
            return label, bad
        for label, bad in pool.map(again, JOBS):
            if bad:
                failures.append(bad)
            else:
                print("ok: " + label)
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
    wf_flat = re.sub(r"\n#\s*", " ", wf)   # the comment lines of the header, joined into sentences
    report_text_ = open(REPORT, newline="").read().replace("\r\n", "\n")
    check("the workflow has its run steps, each under a name of its own", len(wf_runs) >= 7 and all(wf_runs), sorted(wf_runs))
    check("the workflow is dispatch-only", re.search(r"^on:\n  workflow_dispatch:\n", wf, re.M) is not None and not re.search(r"^  (push|pull_request|schedule):", wf, re.M) and "TODO-REMOVE" not in wf)
    check("the workflow asks for no permission but to read the repository", re.search(r"^permissions:\n  contents: read\n", wf, re.M) is not None and ": write" not in wf)
    check("no run block has a ${{ }} in it: what the matrix and the inputs say reaches a script through env", all("${{" not in script for script in wf_runs.values()), [n for n, s in wf_runs.items() if "${{" in s])
    check("the inputs reach the job through env and nowhere else", all(re.match(r"^\s+\w+: \$\{\{ (inputs\.\w+|matrix\.\w+|github\.token|needs\.plan\.outputs\.matrix|steps\.plan\.outputs\.matrix) \}\}$", ln) or "${{" not in ln or ln.lstrip().startswith(("name:", "matrix:", "- ", "#"))
                                                                for ln in wf.split("\n")), [ln for ln in wf.split("\n") if "${{" in ln])
    check("no run block of the workflow has a pipeline whose consumer quits before its producer is done", all(not early_quitters(s) for s in wf_runs.values()), {n: early_quitters(s) for n, s in wf_runs.items() if early_quitters(s)})
    check("the workflow runs this harness and syntax-checks both guest scripts before it boots anything",
          "python3 scripts/test-os-plan-proof.py" in wf_runs.get("The proof can say no", "") and "bash -n scripts/os-plan-proof.sh" in wf_runs.get("The proof can say no", "") and "bash -n scripts/os-readiness-proof.sh" in wf_runs.get("The proof can say no", ""))
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
    check("the guest is run as root, marked disposable, with the installer's options, Docker's version and the leg, and gets the installer and BOTH scripts (the plan's sources the readiness proof's)",
          "sudo env PLAN_DISPOSABLE=1" in prove and "INSTALLER_ARGS='${INSTALLER_ARGS}'" in prove and "DOCKER_FROM='${DOCKER_FROM}'" in prove and "bash os-plan-proof.sh ${LEG}" in prove
          and "scp -F ssh_config published/install.sh scripts/os-readiness-proof.sh scripts/os-plan-proof.sh vm:" in prove, prove)
    check("the report judges the evidence of the leg and its result is the job's, whatever the earlier steps did", 'python3 scripts/os-plan-proof-report.py out "${LEG}" | tee -a "${GITHUB_STEP_SUMMARY}"' in report_step and re.search(r"- name: Report\n\s+if: always\(\)", wf) is not None)
    check("the evidence the guest left is the directory the report is run on", "/home/casa/osp-out" in prove and "/home/casa/osp-out" in report_step)
    check("the tag is the input, and nothing else", "TAG: ${{ inputs.tag }}" in wf and "EVENT" not in wf)
    check("the workflow says that the release under test has to carry the routes, what the guest does when it does not, and that an empty tag is the latest release",
          "It needs a release that carries the routes" in wf_flat and "the release under test has no /v1/sys/os/upgrade/plan" in wf_flat and "Left empty, the tag is the latest published release" in wf_flat and "until then, give the tag of the candidate" in wf_flat, "")
    check("the workflow says what the control is and what the verdict on the units means, as the readiness proof says it",
          "a service that has an entry in the journal of the control is noise wherever else it changes" in wf_flat and "a journal that cannot be believed leaves nothing out (the false red may come back, never a false pass)" in wf_flat
          and "a CONTROL of five minutes (CONTROL_SECONDS, 320 by default)" in wf_flat, "")
    check("the workflow says what the window sees, that only /var/log/casaos may differ, that /var/tmp has to list the same, and what the named expected catch is",
          "Only /var/log/casaos may differ, and /var/tmp has to list the same" in wf_flat and "The stamp that the box's own hooks touch when an `apt-get update` ends is the named expected catch" in wf_flat
          and "neutralises the hooks of /etc/apt/apt.conf.d in its sandbox, and names what it left out of apt's configuration, each file with its reason (apt_conf_left_out" in wf_flat
          and "(/var/log/apt is looked at because the planner of apt writes its log of a simulated transaction there" in wf_flat, "")
    check("the workflow says that three steps of the dirty matrix are canaries: a real hook of apt-get update that a filter of text does not see, and the option of gpgv, left out by the plan and never run",
          all(s in wf_flat for s in ("Three of its steps are CANARIES", "a comment spliced into the key (`Post-In/**/voke-Success`), and an #include of a file that holds the hook",
                                     "the option of the gpgv that apt runs as _apt (Acquire::gpgv::Options::0", "leave the fragment out and name it with its reason (not_simple, has_directive, not_allowed_setting)",
                                     "the file that the hook touches (or that gpgv writes) must not be there when the plan is done")), "")
    check("the workflow says what the plan is judged on, the dirty matrix and its codes, and what the guest cannot do", all(s in wf_flat for s in (
        "a blocked plan is a failure, with its problems printed", "(fuse, libsemanage1", "384 upgraded, 75 new, 2 removed, 0 kept back and 274, 0, 0, 111", "sources_format, sources_cdrom, sources_alias, sources_target",
        "sources_mixed_file, sandbox_update_failed of the kind network, busy, no_room", "a failed job has no runbook", "NOT TESTABLE with the reason", "not_root: the core runs as root")), "")
    check("the workflow says that the numbers it prints are the first real-guest data of the rung, which re-tare the floors", "first real-guest data of the rung, which re-tare the floors of the disk and of /var/tmp" in wf_flat, "")
    check("the guest script, the report and the workflow name the file of the control's journal", all("pid1-control.jsonl" in x for x in (guest_text, report_text_)) and "journal of the control" in wf_flat, "")
    _step = re.search(r"name: Prove it\n\s+timeout-minutes: (\d+)", wf)
    _job = re.search(r"^    timeout-minutes: (\d+)$", wf.split("  vm:\n", 1)[1], re.M)
    _wait = re.search(r'^CONTROL_SECONDS="\$\{CONTROL_SECONDS:-(\d+)\}"$', readiness_text, re.M)
    _plans = 4 + len([s for s in STEPS if s["id"] == "deadmirror" or s["kind"] == "canary"])   # the four plans, the one that fetches the lists and fails, and the three canaries that make a whole plan each
    check("the proof step has time for the control (320 s), the preparation (15 minutes with the installer and Docker), and five full plans (each fetches the lists of the target: 3 minutes is a slow one) with half as much again, and the job has more than the step",
          _step is not None and _job is not None and _wait is not None and int(_wait.group(1)) >= 301 and int(_step.group(1)) * 60 >= 1.5 * (int(_wait.group(1)) + 15 * 60 + (_plans + 1) * 3 * 60) and int(_job.group(1)) > int(_step.group(1)),
          (_step and _step.group(1), _job and _job.group(1), _wait and _wait.group(1)))
    check("the guest script ends the run with the very words the workflow says", "the release under test has no ${PLAN_PATH}" in guest_text and "PLAN_PATH=/v1/sys/os/upgrade/plan" in guest_text)
    check("the legs are chosen by the plan job from the file of the legs, which stops a run that matches none", "scripts/os-plan-proof-legs.json" in wf_runs.get("plan:The legs this run proves", "") and 'raise SystemExit("no leg is' in wf_runs.get("plan:The legs this run proves", ""))
    artifact = re.search(r"^\s+name: (os-plan-proof-.+)$", wf, re.M)
    check("each leg uploads its evidence under a name of its own", bool(artifact) and "${{ matrix.leg }}" in artifact.group(1))
    check("the job stops a leg that hangs, and the proof step has its own limit", re.search(r"^    timeout-minutes: 90$", wf, re.M) is not None and re.search(r"name: Prove it\n\s+timeout-minutes: \d+", wf) is not None)
    check("the machine is the one of the readiness proof (six GiB, four cpus, a 30 GiB disk: the plan's sandbox is a few hundred MiB and the guest installs Docker)", "-m 6144 -smp 4" in wf and "qemu-img resize disk.img 30G" in wf)
    check("the workflow shows what is in /var/tmp when the machine is not well", "ls -la /var/tmp" in wf_runs.get("What the machine says when it is not well", ""))

    # the legs: what the workflow boots, what the guest knows, what the report judges
    with open(LEGS_JSON, newline="") as f:
        cells = json.loads(f.read())
    with open(os.path.join(HERE, "os-upgrade-measure-legs.json"), newline="") as f:
        measured = json.loads(f.read())
    check("the one leg is d11, the name the guest and the report know", [c["leg"] for c in cells] == ["d11"] and "d11" in report_text_ and 'd11' in guest_text)
    for c in cells:
        twin = next((m for m in measured if (m["id"], m["version"]) == (c["id"], c["version"])), None)
        check("the image and the sums of %s %s are the ones the OS upgrade measurement boots" % (c["id"], c["version"]), twin is not None and all(c.get(k) == twin.get(k) for k in ("base", "image", "sums", "algo", "args", "docker_from")), (c, twin))
        check("the %s leg is the system the report expects" % c["leg"], (c["id"], c["version"]) == (LEG["family"], LEG["version"]) and "%s-%s-" % (c["id"], c["version"]) in c["image"])
    d11 = cells[0]
    check("the Debian 11 leg is the owner's box: the archive's security line and Docker 28.0.4", d11.get("args") == "--use-debian-archive" and d11.get("docker_from") == "28.0.4")
    if have_bash():
        count[0] += 1
        p_ = subprocess.run([BASH, "-c", 'source "$1"; leg_facts "$2"', "_", SCRIPT.replace("\\", "/"), "d11"], capture_output=True, text=True, env=dict(ENV, READINESS_LIB=READINESS.replace("\\", "/")))
        if p_.stdout.split() == [LEG["family"], LEG["version"], LEG["codename"], LEG["to_version"], LEG["to_codename"]]:
            print("ok: the guest's leg table is the report's")
        else:
            failures.append("the guest's leg_facts and the report's LEGS differ: %r" % p_.stdout)

    # the plan job, run against the file of the legs
    plan_step_text = wf_runs.get("plan:The legs this run proves", "")

    def plan_case(label, legs, want_code, want_legs=None):
        if not have_bash() or not wanted(label):
            return
        count[0] += 1
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "scripts"))
            shutil.copy(LEGS_JSON, os.path.join(tmp, "scripts", "os-plan-proof-legs.json"))
            out = os.path.join(tmp, "output").replace("\\", "/")
            summary = os.path.join(tmp, "summary").replace("\\", "/")
            p_ = subprocess.run([BASH, "-c", plan_step_text], capture_output=True, text=True, cwd=tmp, env=dict(ENV, LEGS=legs, GITHUB_OUTPUT=out, GITHUB_STEP_SUMMARY=summary))
            got = None
            if os.path.exists(out):
                with open(out) as f:
                    m = re.search(r"^matrix=(.*)$", f.read(), re.M)
                    got = [c["leg"] for c in json.loads(m.group(1))["include"]] if m else None
            if (p_.returncode == 0) != (want_code == 0) or got != want_legs:
                failures.append("the plan for %r: exit %d, legs %r, wanted %r %r\n%s" % (legs, p_.returncode, got, want_code, want_legs, p_.stderr[-300:]))
            else:
                print("ok: " + label)

    plan_case("the plan for `all` is the Debian 11 leg", "all", 0, ["d11"])
    plan_case("the plan for d11 is the Debian 11 leg alone", "d11", 0, ["d11"])
    plan_case("the plan for a leg that is none (u22: the readiness proof's, not this one) stops the run before any machine is booted", "u22", 1, None)

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
              and doc[True]["workflow_dispatch"]["inputs"]["legs"]["options"] == ["all", "d11"] and runs_yaml == wf_runs and doc["jobs"]["vm"]["strategy"]["matrix"] == "${{ fromJSON(needs.plan.outputs.matrix) }}"
              and doc["jobs"]["vm"]["needs"] == "plan" and doc["jobs"]["vm"]["timeout-minutes"] == 90, "the two readings differ")

if failures:
    print("\n".join(failures), file=sys.stderr)
    print("%d of %d cases failed" % (len(failures), count[0]), file=sys.stderr)
    sys.exit(1)
print("ok: the proof can say no (%d cases)" % count[0])
