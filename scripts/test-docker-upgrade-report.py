#!/usr/bin/env python3
"""The report script has to be able to say no. Each case below builds the directory a run
would leave, in a state that must come out a given way, and checks what
docker-upgrade-report.py says and its exit status:

    python3 scripts/test-docker-upgrade-report.py

A run that measured nothing, or in which Docker was never upgraded, must be INVALID and
not "n of n pass"; a short blip must not hide a long outage; an acknowledged write that
is not in the file must fail; a rollback that did not happen must fail.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(HERE, "docker-upgrade-report.py")
NAMES = ["m-always", "m-db", "m-host", "m-no", "m-on-failure", "m-unless-stopped", "m-web", "smoke", "smoke2"]
POLICY = {"m-always": "always", "m-db": "unless-stopped", "m-host": "unless-stopped", "m-no": "no", "m-on-failure": "on-failure",
          "m-unless-stopped": "unless-stopped", "m-web": "unless-stopped", "smoke": "no", "smoke2": "unless-stopped"}
T0 = 1000.0


def snap(path, docker, ce, pid, am=77, stopped=()):
    lines = [
        "docker %s" % docker, "driver overlay2", "live_restore false", "images 4", "all_containers 9",
        "packages docker-ce=%s containerd.io=1.7.28-1 " % ce, "needrestart absent",
        "unit docker MainPID=%s ActiveEnterTimestamp=x NRestarts=0" % pid,
        "unit containerd MainPID=11 ActiveEnterTimestamp=x NRestarts=0",
        "unit casaos-app-management MainPID=%s ActiveEnterTimestamp=x NRestarts=0" % am,
        "unit casaos MainPID=5 ActiveEnterTimestamp=x NRestarts=0",
        "containers",
    ]
    for n in NAMES:
        lines.append("/%s policy=%s state=%s started=x restarts=0" % (n, POLICY[n], "exited" if n in stopped else "running"))
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def build(d, down=((10, 22),), blips=(), stay_down=(), return_names=None, ack_missing=0, same_version=False, rc="0",
          rollback=True, rollback_ce="5:29.8.1-1", rollback_rc="0", am_after=77, web_down=None, drop_markers=False, stopped_after=()):
    """a timeline of 200 samples a second, upgrade from 0 to 100, settled at 199; dockerd down in each of `down`"""
    prev, newest = "5:29.8.1-1", "5:29.8.2-1"
    rows = ["#\t%f\tupgrade_start" % T0]
    running = ",".join(NAMES)
    back_names = return_names if return_names is not None else NAMES
    for i in range(200):
        t = T0 + i
        isdown = any(a <= i < b for a, b in down) or i in blips
        if isdown:
            rows.append("%f\tinactive\tdown\t200\t500\t000\t-" % t)
        elif any(b <= i < b + 3 for a, b in down):
            rows.append("%f\tactive\tup\t200\t200\t000\t-" % t)
        elif any(i >= b for a, b in down) and i < max(b for a, b in down) + 6:
            rows.append("%f\tactive\tup\t200\t200\t200\t%s" % (t, ",".join(back_names)))
        else:
            rows.append("%f\tactive\tup\t200\t200\t200\t%s" % (t, running if not any(i >= b for a, b in down) else ",".join(back_names)))
        if i == 100:
            rows.append("#\t%f\tupgrade_end" % t)
        if i == 199:
            rows.append("#\t%f\tupgrade_settled" % t)
    if rollback:
        for i in range(200, 330):
            t = T0 + i
            if i == 200:
                rows.append("#\t%f\trollback_start" % t)
            if 210 <= i < 218:
                rows.append("%f\tinactive\tdown\t200\t500\t000\t-" % t)
            else:
                rows.append("%f\tactive\tup\t200\t200\t200\t%s" % (t, running))
            if i == 250:
                rows.append("#\t%f\trollback_end" % t)
        rows.append("#\t%f\trollback_settled" % (T0 + 330))
    if drop_markers:
        rows = [r for r in rows if not r.startswith("#")]
    with open(os.path.join(d, "timeline.tsv"), "w") as f:
        f.write("\n".join(rows) + "\n")
    snap(os.path.join(d, "snapshot-before.txt"), "29.8.1", prev, 500)
    snap(os.path.join(d, "snapshot-after.txt"), "29.8.1" if same_version else "29.8.2", prev if same_version else newest, 900, am_after, stopped_after)
    snap(os.path.join(d, "snapshot-rollback.txt"), "29.8.1", rollback_ce, 950)
    for name, value in (("prev", prev), ("newest", newest), ("upgrade.rc", rc), ("rollback.rc", rollback_rc)):
        with open(os.path.join(d, name), "w") as f:
            f.write(value + "\n")
    ids = ["1000-%d" % k for k in range(1, 201)]
    with open(os.path.join(d, "db-logs.txt"), "w") as f:
        for k, i in enumerate(ids):
            f.write("2026-10-08T10:00:%02d.%09dZ ack %s\n" % (k % 60, k, i))
    with open(os.path.join(d, "db-file.txt"), "w") as f:
        f.write("\n".join(ids[: len(ids) - ack_missing]) + "\n")
    with open(os.path.join(d, "simulation.txt"), "w") as f:
        f.write("Inst docker-ce [5:29.8.1-1] (5:29.8.2-1 Docker CE:jammy [amd64])\nInst libc6 [1] (2 Ubuntu [amd64])\n")
    with open(os.path.join(d, "alerts.txt"), "w") as f:
        f.write("")


def build_excluded(d, ce_after="5:28.0.4-1", pid_after=500, missing_at=None, candidate="5:29.8.2-1", names="libc6\nzlib1g\n", log=""):
    """a run of the explicit list on a Docker 28 with a 29 on offer: nothing of Docker's should move"""
    rows = ["#\t%f\tupgrade_start" % T0]
    running = ",".join(NAMES)
    for i in range(160):
        t = T0 + i
        here = ",".join(n for n in NAMES if n != missing_at) if missing_at and i == 50 else running
        rows.append("%f\tactive\tup\t200\t200\t200\t%s" % (t, here))
        if i == 20:
            rows.append("#\t%f\tupgrade_end" % t)
    rows.append("#\t%f\tupgrade_settled" % (T0 + 160))
    with open(os.path.join(d, "timeline.tsv"), "w") as f:
        f.write("\n".join(rows) + "\n")
    snap(os.path.join(d, "snapshot-before.txt"), "28.0.4", "5:28.0.4-1", 500)
    snap(os.path.join(d, "snapshot-after.txt"), "28.0.4", ce_after, pid_after)
    for name, value in (("prev", "5:28.0.4-1"), ("newest", "5:29.8.2-1"), ("upgrade.rc", "0")):
        with open(os.path.join(d, name), "w") as f:
            f.write(value + "\n")
    with open(os.path.join(d, "simulation.txt"), "w") as f:
        f.write("Inst docker-ce [5:28.0.4-1] (%s Docker CE:jammy [amd64])\nInst libc6 [1] (2 Ubuntu [amd64])\nInst zlib1g [1] (2 Ubuntu [amd64])\n" % candidate)
    with open(os.path.join(d, "upgrade-names.txt"), "w") as f:
        f.write(names)
    with open(os.path.join(d, "upgrade.log"), "w") as f:
        f.write(log)


def report(d, mode="generic", arch="amd64"):
    p = subprocess.run([sys.executable, REPORT, d, arch, mode], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


failures = []


def case(name, want_code, must_have=(), must_not_have=(), mode="generic", **kw):
    with tempfile.TemporaryDirectory() as d:
        if kw.pop("_empty", False):
            pass
        else:
            build(d, **kw)
        code, text = report(d, mode)
        problems = []
        if code != want_code:
            problems.append("exit %d, wanted %d" % (code, want_code))
        for s in must_have:
            if s not in text:
                problems.append("missing %r" % s)
        for s in must_not_have:
            if s in text:
                problems.append("should not say %r" % s)
        if problems:
            failures.append("%s: %s\n%s" % (name, "; ".join(problems), text[-1500:]))
        else:
            print("ok:", name)


case("a healthy run is measured and passes", 0,
     ["**PASS** upgrade: dockerd back within 60 s", "**PASS** upgrade: always/unless-stopped containers back", "**PASS** no acknowledged write lost", "**PASS** the rollback puts the previous docker-ce back"],
     ["INVALID", "FAIL"])
case("a 90 s outage fails the 60 s threshold", 0, ["**FAIL** upgrade: dockerd back within 60 s"], ["INVALID"], down=((10, 100),))
case("one failed sample before a real outage does not hide it", 0, ["**FAIL** upgrade: dockerd back within 60 s"], ["INVALID"], down=((20, 95),), blips=(2,))
case("a single blip alone is no outage", 0, ["dockerd never failed two samples in a row", "not fully healthy"], ["INVALID"], down=(), blips=(30,))
case("a daemon restart shorter than the poller sees is still a pass, and said", 0, ["**PASS** upgrade: dockerd back within 60 s: away for about", "not fully healthy"], ["INVALID", "never stopped"], down=(), blips=(30,))
case("containers without a restart policy that stayed stopped are a finding", 0, ["**FINDING** upgrade: containers without a restart policy are not started again", "m-no (policy `no`)", "smoke (policy `no`)"], ["INVALID"], stopped_after=("m-no", "smoke"))
case("an upgrade that changed nothing is invalid, not a pass", 1, ["INVALID RUN", "docker-ce was not upgraded"], ["pass"], same_version=True, down=())
case("an upgrade command that failed is invalid", 1, ["INVALID RUN", "exited with status '100'"], [], rc="100")
case("an empty run is invalid and does not crash", 1, ["INVALID RUN", "no samples"], ["Traceback"], _empty=True)
case("an acknowledged write missing from the file fails", 0, ["**FAIL** no acknowledged write lost"], ["INVALID"], ack_missing=3)
case("containers that never come back fail", 0, ["**FAIL** upgrade: always/unless-stopped containers back", "never"], ["INVALID"], return_names=["m-always", "m-no"])
case("a rollback that left the new version fails", 0, ["**FAIL** the rollback puts the previous docker-ce back"], ["INVALID"], rollback_ce="5:29.8.2-1")
case("a rollback that failed fails", 0, ["**FAIL** the rollback puts the previous docker-ce back"], ["INVALID"], rollback_rc="100")
case("AppManagement restarted fails", 0, ["**FAIL** AppManagement not restarted"], ["INVALID"], am_after=99)
case("a run with no markers is invalid", 1, ["INVALID RUN", "marker upgrade_start is missing"], [], drop_markers=True)

def case_excluded(name, want_code, must_have=(), must_not_have=(), **kw):
    with tempfile.TemporaryDirectory() as d:
        build_excluded(d, **kw)
        code, text = report(d, "excluded")
        problems = []
        if code != want_code:
            problems.append("exit %d, wanted %d" % (code, want_code))
        problems += ["missing %r" % s for s in must_have if s not in text]
        problems += ["should not say %r" % s for s in must_not_have if s in text]
        if problems:
            failures.append("%s: %s\n%s" % (name, "; ".join(problems), text[-1500:]))
        else:
            print("ok:", name)


case_excluded("an excluded run that left Docker alone passes everything", 0,
              ["**PASS** Docker's packages are unchanged", "**PASS** dockerd was not restarted", "**PASS** every container kept running throughout", "**PASS** AppManagement was not restarted", "4 of 4 verdicts pass".replace("4 of 4", "7 of 7")], ["FAIL", "INVALID"])
case_excluded("an excluded run that upgraded docker-ce fails", 0, ["**FAIL** Docker's packages are unchanged"], ["INVALID"], ce_after="5:29.8.2-1")
case_excluded("an excluded run that restarted dockerd fails", 0, ["**FAIL** dockerd was not restarted"], ["INVALID"], pid_after=901)
case_excluded("an excluded run where a container went away fails", 0, ["**FAIL** every container kept running throughout", "m-web"], ["INVALID"], missing_at="m-web")
case_excluded("an excluded run that installed a Docker package fails", 0, ["**FAIL** apt did not install or upgrade a Docker package"], ["INVALID"], log="Unpacking docker-ce (5:29.8.2-1) over (5:28.0.4-1) ...\n")
case_excluded("an excluded run with no major to refuse is invalid", 1, ["INVALID RUN", "nothing to exclude"], [], candidate="5:28.0.5-1")
case_excluded("an excluded run with an empty list is invalid", 1, ["INVALID RUN", "explicit list is empty"], [], names="")

with tempfile.TemporaryDirectory() as d:
    with open(os.path.join(d, "skipped"), "w") as f:
        f.write("no older docker-ce in the same major\n")
    code, text = report(d)
    if code != 0 or "NOT MEASURED: no older docker-ce" not in text:
        failures.append("a skipped run: exit %d, %r" % (code, text[:200]))
    else:
        print("ok: a skipped run says so, and is not a failure")

if failures:
    print("\n".join(failures), file=sys.stderr)
    sys.exit(1)
print("ok: the report can say no")
