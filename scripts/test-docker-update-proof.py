#!/usr/bin/env python3
"""The proof has to be able to say no. Each case below builds the directory a leg would leave, in a state
that must come out a given way, and checks what docker-update-proof-report.py says and its exit status:

    python3 scripts/test-docker-update-proof.py

A healthy major leg and a healthy minor leg pass. Every verdict has a case in which only the thing it
judges is wrong, and that case must fail by that name; a run that did not get as far as it should, or
started from a box that was not what the leg needs, must be INVALID and never green. The healthy major
leg is the owner's box: nftables was taken out, the plan brings it back with its libraries, and dpkg
agrees; the same leg on a box that kept nftables passes too, and says that no new dependency was
exercised. The text-only functions of docker-update-proof.sh (which version a leg starts from, whether
nftables may be taken out) are tested too, when bash is here, and so is the step that takes it out, with
stand-ins for apt, dpkg, systemctl and docker.

REPORT=<path> and PROOF_SCRIPT=<path> run the cases against other copies of the report and of the guest
script, to see that a broken one is caught. TEST_BASH=<path> names a bash where plain `bash` is not the one.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.environ.get("REPORT") or os.path.join(HERE, "docker-update-proof-report.py")
SCRIPT = os.environ.get("PROOF_SCRIPT") or os.path.join(HERE, "docker-update-proof.sh")
T0 = 1790000000.0
NONCE = "0123456789abcdef0123456789abcdef"
PREFIX = "CASAOS_DOCKER_UPDATE_"
CONTAINERS = {"p-always": "always", "p-unless-stopped": "unless-stopped", "p-no": "no", "p-db": "unless-stopped",
              "p-web": "unless-stopped", "p-host": "unless-stopped"}

# what nftables brings on Debian 11: the package and the libraries apt takes out with it, and puts back when Docker 29 asks for it
NFT = [("nftables", "0.9.8-3.1+deb11u1"), ("libnftables1", "0.9.8-3.1+deb11u1"), ("libjansson4", "2.13.1-1.1"), ("libedit2", "3.1-20191231-2+b1")]
NFT_REMOVED = "action removed\npackages " + " ".join(n for n, _ in NFT) + "\n"
# what the box has that the update has nothing to do with
BASE = [("ca-certificates", "20210119"), ("curl", "7.74.0-1.3+deb11u14"), ("iptables", "1.8.7-1"), ("jq", "1.6-2.1"), ("libc6", "2.31-13+deb11u11"),
        ("python3", "3.9.2-3"), ("systemd", "247.3-7+deb11u6")]

LEGS = {
    "major": dict(os="Debian GNU/Linux 11 (bullseye)", tag="debian.11~bullseye", start="28.0.4", to="29.8.0", major_jump=True,
                  plan=[("docker-ce", "5:28.0.4-1~debian.11~bullseye", "5:29.8.0-1~debian.11~bullseye"),
                        ("docker-ce-cli", "5:28.0.4-1~debian.11~bullseye", "5:29.8.0-1~debian.11~bullseye"),
                        ("containerd.io", "1.7.27-1", "2.1.4-1"),
                        ("docker-ce-rootless-extras", "5:28.0.4-1~debian.11~bullseye", "5:29.8.0-1~debian.11~bullseye")],
                  new=NFT, removed=NFT, dependency=NFT_REMOVED),
    "minor": dict(os="Ubuntu 24.04.3 LTS", tag="ubuntu.24.04~noble", start="29.8.1", to="29.8.2", major_jump=False,
                  plan=[("docker-ce", "5:29.8.1-1~ubuntu.24.04~noble", "5:29.8.2-1~ubuntu.24.04~noble"),
                        ("docker-ce-cli", "5:29.8.1-1~ubuntu.24.04~noble", "5:29.8.2-1~ubuntu.24.04~noble"),
                        ("docker-ce-rootless-extras", "5:29.8.1-1~ubuntu.24.04~noble", "5:29.8.2-1~ubuntu.24.04~noble")],
                  new=[], removed=[], dependency="action not-attempted\nreason only the major leg takes nftables out: Docker 29 already needs it\n"),
}
# the major leg on a box where nftables could not be taken out: it and its libraries are installed, the plan has nothing new to bring
HAD_NFT = dict(LEGS["major"], new=[], removed=[], present=NFT,
               dependency="action skipped\nreason taking nftables out would take more than libraries with it: docker-ce nftables\n")
# the harness took nftables out, and the Docker on offer does not need it after all
NO_NEED = dict(LEGS["major"], new=[])


def many_new(n):
    """the major leg whose update brings n packages the box did not have, none of them nftables"""
    return dict(LEGS["major"], new=[("libdep%d" % i, "1.%d-1" % i) for i in range(n)], removed=[],
                dependency="action skipped\nreason nftables is not installed\n")


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ok(data):
    return {"success": 200, "message": "ok", "data": data}


def refusal(code, text=None):
    text = text or "the reason for %s" % code
    return {"success": 409, "message": text, "data": {"error": text, "error_code": code}}


def plan_hash(plan):
    return hashlib.sha256("\n".join(sorted("%s %s>%s" % p for p in plan)).encode()).hexdigest()


def put(d, name, text):
    with open(os.path.join(d, name), "w") as f:
        f.write(text)


def api(d, name, code, body=None, secs=0.4):
    put(d, name + ".code", "%d\n" % code)
    put(d, name + ".secs", "%s\n" % secs)
    if body is not None:
        put(d, name + ".json", json.dumps(body))


def full_plan(s):
    """the upgrades, then the new packages (which have no current version), as (name, current, candidate)"""
    return list(s["plan"]) + [(n, "", k) for n, k in s["new"]]


def dpkg_text(s, after):
    """what `dpkg-query -W` lists before the update or after it: the base of the box, Docker's packages at the version they have then,
    the new ones only after; what the harness took out is `rc` (removed, configuration kept) until the update brings it back"""
    rows = [(n, v, "ii") for n, v in BASE + s.get("present", [])]
    rows += [(n, k if after else c, "ii") for n, c, k in s["plan"]]
    brought = {n for n, _ in s["new"]}
    if after:
        rows += [(n, k, "ii") for n, k in s["new"]]
    rows += [(n, v, "rc") for n, v in s["removed"] if not (after and n in brought)]
    rows.append(("docker.io", "", "un"))
    return "".join("%s\t%s\t%s \n" % row for row in sorted(rows))


def packages_body(s, available=True, refusal_code="", version=None):
    plan = [dict(name=n, current_version=c, candidate_version=k) for n, c, k in s["plan"]]
    plan += [dict(name=n, current_version="", candidate_version=k, new=True) for n, k in s["new"]]
    update = dict(available=available, refusal=refusal_code, refusal_detail=[], packages=plan, plan_id=plan_hash(full_plan(s)),
                  **{"from": s["start"], "to": s["to"], "major_jump": s["major_jump"]})
    return ok({"supported": True, "count": 0, "docker": dict(installed=True, version=version or s["start"], update=update)})


def status_data(state, outcome="", **kw):
    d = {"supported": True, "state": state, "outcome": outcome, "error": "", "error_code": "", "exit_code": None, "started_at": "", "completed_at": "",
         "from": "", "to": "", "not_returned": [], "rollback_command": ""}
    d.update(kw)
    return d


def marker(kind, rest=""):
    return "%s%s %s%s" % (PREFIX, kind, NONCE, (" " + rest) if rest else "")


IMAGES = "sha256:1a2b3c busybox:latest\nsha256:4d5e6f nginx:alpine\n"
JOURNAL = ("Oct 09 10:02:01 box systemd[1]: Started CasaOS Docker update.\n"
           "Oct 09 10:04:11 box systemd[1]: casaos-docker-update.service: Deactivated successfully.\n")
EMPTY_ENV = "Oct 09 10:02:01 box systemd[1]: casaos-docker-update.service: Invalid environment variable name evaluates to an empty string: Package\n"


def success_log(s, t_dl):
    prev = " ".join("%s=%s" % (n, c) for n, c, _ in s["plan"])
    lines = [marker("QUEUED", iso(T0 + 100)), marker("STARTED", iso(T0 + 101)), marker("PREVIOUS", prev)]
    lines += ["container: %s %s" % (n, p) for n, p in sorted(CONTAINERS.items())]
    lines += ["Reading package lists...", marker("DOWNLOADED", iso(t_dl)), "Unpacking docker-ce ...", marker("INSTALLED", iso(T0 + 140)),
              marker("DAEMON", s["to"]), marker("NOTRETURNED", "p-no no"), marker("SUCCESS", iso(T0 + 190))]
    return "\n".join(lines) + "\n"


def snapshot_text(s, after, docker_pid, enter, am=77, core=79, gw=80, gone=("p-no",), packages_new=False, docker_version=None):
    if docker_version is None:
        docker_version = s["to"] if after else s["start"]
    pk = " ".join("%s=%s" % (n, k if packages_new else c) for n, c, k in s["plan"])
    lines = ["# x", "docker %s" % docker_version, "driver overlay2", "packages %s" % pk,
             "unit docker MainPID=%d NRestarts=0 EnterEpoch=%d" % (docker_pid, enter),
             "unit containerd MainPID=11 NRestarts=0 EnterEpoch=%d" % enter,
             "unit casaos-app-management MainPID=%d NRestarts=0 EnterEpoch=1" % am,
             "unit casaos-message-bus MainPID=78 NRestarts=0 EnterEpoch=1",
             "unit casaos MainPID=%d NRestarts=0 EnterEpoch=1" % core,
             "unit casaos-gateway MainPID=%d NRestarts=0 EnterEpoch=1" % gw, "containers"]
    for n, p in sorted(CONTAINERS.items()):
        lines.append("/%s policy=%s state=%s started=x restarts=0" % (n, p, "exited" if (after and n in gone) else "running"))
    return "\n".join(lines) + "\n"


def build(d, leg, s=None):
    """the directory a healthy leg leaves; s describes the leg (LEGS[leg] when none is given)"""
    s = s or LEGS[leg]
    installed, candidate = s["plan"][0][1], s["plan"][0][2]
    put(d, "os", s["os"] + "\n")
    put(d, "leg", leg + "\n")
    put(d, "start-version", s["start"] + "\n")
    put(d, "apt-docker-ce.txt", "installed %s\ncandidate %s\n" % (installed, candidate))
    t_dl = T0 + 120
    all_names = ",".join(sorted(CONTAINERS))
    back_names = ",".join(sorted(n for n in CONTAINERS if n != "p-no"))

    # the timeline: a sample a second; dockerd away from +140 to +146, the port to +148
    rows = []
    for i in range(0, 430):
        t = T0 + i
        dv, web, amdocker, names = s["start"], "200", "200", all_names
        if 140 <= i < 146:
            dv, web, amdocker, names = "-", "000", "500", "-"
        elif 146 <= i < 150:
            dv, web, names = s["to"], "000" if i < 148 else "200", "-"
        elif i >= 150:
            dv, names = s["to"], back_names
        if leg == "minor" and 210 <= i < 214:    # the box put back on the start version
            dv, web, amdocker, names = "-", "000", "500", "-"
        elif leg == "minor" and 214 <= i < 330:
            dv, names = s["start"], back_names
        if leg == "minor" and 330 <= i < 410:    # dockerd cannot start
            dv, web, amdocker, names = "-", "000", "500", "-"
        rows.append("%f\t%s\t200\t%s\t200\t%s\t%s" % (t, dv, web, amdocker, names))
    marks = {"post_start": 100, "post_done": 101, "terminal": 190, "settled": 196}
    if leg == "major":
        marks.update(rollback_start=215, rollback_done=250, rollback_settled=262)
    if leg == "minor":
        marks.update(fail_post=300, fail_terminal=400, fail_settled=420)
    rows += ["#\t%f\t%s" % (T0 + at, name) for name, at in marks.items()]
    put(d, "timeline.tsv", "\n".join(rows) + "\n")

    # the status endpoint, asked every 2 s
    def samples(first, last, terminal_state, extra):
        out = []
        t = first
        while t <= last:
            st = terminal_state if t >= last else ("running" if t < last - 5 else "finalizing")
            out.append("%f\t200\t%s" % (T0 + t, json.dumps(status_data(st, **(extra if st == terminal_state else {})))))
            t += 2
        return "\n".join(out) + "\n"
    success_extra = dict(outcome="success", started_at=iso(T0 + 100), completed_at=iso(T0 + 190), **{"from": s["start"], "to": s["to"]},
                         not_returned=[{"name": "p-no", "restart_policy": "no"}])
    put(d, "status.jsonl", samples(102, 190, "succeeded", success_extra))
    api(d, "status-idle", 200, ok(status_data("idle")))
    put(d, "status-final.json", json.dumps(ok(dict(status_data("succeeded", **success_extra), log=success_log(s, t_dl)))))

    api(d, "packages-1", 200, packages_body(s), secs=21.5)
    api(d, "packages-2", 200, packages_body(s))
    api(d, "packages-held", 200, packages_body(s, available=False, refusal_code="held"))
    api(d, "containers-before", 200, ok({"running": True, "containers": [
        {"name": n, "image": "busybox", "restart_policy": p, "host_network": n == "p-host",
         "ports": [{"port": 80, "protocol": "tcp", "host_port": 18081}] if n == "p-web" else []} for n, p in sorted(CONTAINERS.items())]}))
    api(d, "held-post", 409, refusal("held"))
    api(d, "lock-post", 409, refusal("maintenance"))
    api(d, "unit-post", 409, refusal("running"))
    api(d, "wrong-post", 409, refusal("changed"))
    api(d, "bad-post", 400, {"success": 400, "message": "bad plan_id"})
    api(d, "query-post", 401, {"message": "missing or malformed jwt"})
    api(d, "refresh-post", 401, {"message": "invalid token"})
    api(d, "jwt-post", 409, refusal("changed"))
    api(d, "update-post", 200, ok(status_data("running")), secs=3.1)
    api(d, "running-post", 409, refusal("running"))
    put(d, "docker-update.log", success_log(s, t_dl))
    put(d, "snapshot-before.txt", snapshot_text(s, False, 500, int(T0 - 3000)))
    put(d, "snapshot-after.txt", snapshot_text(s, True, 900, int(T0 + 147), packages_new=True))
    ids = ["1790000000-%d" % k for k in range(1, 301)]
    t = datetime(2026, 10, 9, 10, 0, 0, tzinfo=timezone.utc).timestamp()
    acks = []
    for k, i in enumerate(ids):
        t += 6.7 if k == 150 else 0.1
        acks.append("%s ack %s" % (datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z", i))
    put(d, "db-logs.txt", "\n".join(acks) + "\n")
    put(d, "db-file.txt", "\n".join(ids) + "\n")
    if leg == "major":
        # the way back: the command made of the PREVIOUS pins, run on the Docker that has just been installed
        put(d, "rollback-command", "sudo apt-get install --allow-downgrades " + " ".join("%s=%s" % (n, c) for n, c, _ in s["plan"]) + "\n")
        put(d, "rollback-exit", "0\n")
        put(d, "snapshot-rollback.txt", snapshot_text(s, True, 1100, int(T0 + 240), docker_version=s["start"]))
        put(d, "images-before.txt", IMAGES)
        put(d, "images-rollback.txt", IMAGES)
        put(d, "rollback-settle", "9\n")
        put(d, "rollback-db-logs.txt", "\n".join(acks) + "\n")
        put(d, "rollback-db-file.txt", "\n".join(ids) + "\n")
        api(d, "rollback-web", 200)
    api(d, "am-compose", 200)
    api(d, "web-after", 200)
    api(d, "packages-after", 200, ok({"supported": True, "docker": {"installed": True, "version": s["to"]}}))
    api(d, "containers-after", 200, ok({"running": True, "containers": [{"name": n, "restart_policy": p} for n, p in sorted(CONTAINERS.items()) if n != "p-no"]}))

    put(d, "dpkg-before.tsv", dpkg_text(s, False))
    put(d, "dpkg-after.tsv", dpkg_text(s, True))
    put(d, "dependency-path", s["dependency"])
    put(d, "unit-journal.txt", JOURNAL)
    put(d, "unit-journal-end.txt", JOURNAL)

    if leg == "minor":
        api(d, "fail-packages", 200, packages_body(s))
        api(d, "fail-post", 200, ok(status_data("running")))
        fail_extra = dict(outcome="failed", error="The update failed: Docker did not start again.", error_code="daemon", started_at=iso(T0 + 300), completed_at=iso(T0 + 400),
                          **{"from": s["start"]}, rollback_command="sudo apt-get install --allow-downgrades " + " ".join("%s=%s" % (n, c) for n, c, _ in s["plan"]))
        fl = [marker("QUEUED", iso(T0 + 300)), marker("STARTED", iso(T0 + 301)), marker("PREVIOUS", " ".join("%s=%s" % (n, c) for n, c, _ in s["plan"])),
              marker("DOWNLOADED", iso(T0 + 310)), marker("INSTALLED", iso(T0 + 330)), marker("FAILED", iso(T0 + 400) + " daemon")]
        put(d, "docker-update-fail.log", "\n".join(fl) + "\n")
        put(d, "fail-status.jsonl", "\n".join("%f\t200\t%s" % (T0 + t_, json.dumps(status_data("running" if t_ < 400 else "failed", **(fail_extra if t_ >= 400 else {}))))
                                              for t_ in range(302, 401, 2)) + "\n")
        put(d, "fail-status-final.json", json.dumps(ok(dict(status_data("failed", **fail_extra), log="\n".join(fl) + "\n"))))
        put(d, "fail-repaired", "yes, after 12 s\n")
        put(d, "fail-settle", "7\n")


# ---- mutations: change one thing in a healthy directory ----------------------------------------------------------

def jedit(d, name, fn):
    path = os.path.join(d, name)
    with open(path) as f:
        j = json.load(f)
    fn(j)
    with open(path, "w") as f:
        json.dump(j, f)


def ledit(d, name, fn):
    """fn gets the lines of a text file and returns the new ones"""
    path = os.path.join(d, name)
    with open(path) as f:
        lines = f.read().splitlines()
    with open(path, "w") as f:
        f.write("\n".join(fn(lines)) + "\n")


def replace(d, name, old, new, count=-1):
    path = os.path.join(d, name)
    with open(path) as f:
        text = f.read()
    assert old in text, "%r is not in %s" % (old, name)
    with open(path, "w") as f:
        f.write(text.replace(old, new, count))


def remove(d, *names):
    for n in names:
        os.remove(os.path.join(d, n))


def update(j):
    return j["data"]["docker"]["update"]


def final(j):
    return j["data"]


def jsonl_set(d, name, fn):
    """fn(t, http, data) -> (http, data) for every line of a status.jsonl"""
    def each(lines):
        out = []
        for ln in lines:
            t, http, data = ln.split("\t", 2)
            http, data = fn(float(t), http, json.loads(data))
            out.append("%s\t%s\t%s" % (t, http, json.dumps(data)))
        return out
    ledit(d, name, each)


def swap_kinds(a, b):
    """a line editor that swaps the first marker line of kind a with the first of kind b"""
    def edit(lines):
        ia = next(i for i, ln in enumerate(lines) if ln.startswith(PREFIX + a + " "))
        ib = next(i for i, ln in enumerate(lines) if ln.startswith(PREFIX + b + " "))
        lines[ia], lines[ib] = lines[ib], lines[ia]
        return lines
    return edit


def timeline_set(d, fn):
    """fn(index, fields) -> fields for every sample of timeline.tsv; the markers are left alone"""
    def each(lines):
        out, i = [], 0
        for ln in lines:
            f = ln.split("\t")
            if f[0] != "#":
                f = fn(i, f)
                i += 1
            out.append("\t".join(f))
        return out
    ledit(d, "timeline.tsv", each)


# ---- the cases ----------------------------------------------------------------------------------------------------------

failures = []
count = [0]


def run_report(d, leg):
    p = subprocess.run([sys.executable, REPORT, d, leg], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


PIN_SHAPE = re.compile(r"^[a-z0-9][a-z0-9+.-]*=([0-9]+:)?[0-9][A-Za-z0-9.+~-]*$")


def rollback_follows_log(d):
    """what the guest makes of the log it collected: the command of the log's PREVIOUS pins (the words that are not pins dropped), or the word
    that there was nothing to run, and none of what a rollback would have left behind"""
    if not os.path.exists(os.path.join(d, "rollback-command")):
        return
    with open(os.path.join(d, "docker-update.log")) as f:
        pins = [w for ln in f.read().splitlines() if ln.split(" ")[0] == PREFIX + "PREVIOUS" for w in ln.split(" ")[2:] if PIN_SHAPE.match(w)]
    if pins:
        put(d, "rollback-command", "sudo apt-get install --allow-downgrades " + " ".join(pins) + "\n")
        return
    put(d, "rollback-exit", "not-run\n")
    put(d, "rollback-command", "none\n")
    remove(d, "snapshot-rollback.txt", "images-rollback.txt", "rollback-settle", "rollback-db-logs.txt", "rollback-db-file.txt", "rollback-web.code", "rollback-web.secs")


def case(name, want_code, must_have=(), must_not_have=(), leg="major", mutate=None, empty=False, spec=None, follow=False):
    """follow: the guest ran on the log as mutated, so the rollback it did is the one of that log's PREVIOUS marker"""
    count[0] += 1
    with tempfile.TemporaryDirectory() as d:
        if not empty:
            build(d, leg, spec)
        if mutate:
            mutate(d)
        if follow:
            rollback_follows_log(d)
        code, text = run_report(d, leg)
        problems = []
        if code != want_code:
            problems.append("exit %d, wanted %d" % (code, want_code))
        problems += ["missing %r" % s for s in must_have if s not in text]
        problems += ["should not say %r" % s for s in must_not_have if s in text]
        if "Traceback" in text:
            problems.append("crashed")
        if problems:
            failures.append("%s: %s\n%s" % (name, "; ".join(problems), text[-1800:]))
        else:
            print("ok:", name)


def fails(name, verdict, mutate, leg="major", extra=(), spec=None, follow=False):
    """a case in which one thing is wrong and the verdict of that name has to say FAIL (and nothing else about the harness)"""
    case(name, 1, ["**FAIL** " + verdict] + list(extra), ["INVALID"], leg=leg, mutate=mutate, spec=spec, follow=follow)


def invalid(name, reason, mutate, leg="major", empty=False, silent=()):
    """a run that proves nothing; the verdicts named in silent must not be made up from the evidence that is not there"""
    case(name, 1, ["INVALID RUN", reason], ["**FAIL** " + "the run succeeded"] + ["**FAIL** " + s for s in silent], leg=leg, mutate=mutate, empty=empty)


# healthy
V_UP = "every upgrade in the plan is an engine package with validated versions"
V_NEW = "every new package in the plan has a valid name and version, is not a distro Docker package, and there are at most 10"
case("a healthy major leg passes, rolls back, and says that the dependency path was exercised", 0,
     ["Docker update proof, major leg: PASS", "28.0.4", "29.8.0", "dockerd did not answer for 6.0 s", "The dependency path was exercised",
      "nftables 0.9.8-3.1+deb11u1", "the harness took out nftables, libnftables1, libjansson4, libedit2", "**PASS** rollback after the major jump"],
     ["**FAIL**", "INVALID", "No new dependency was exercised"])
case("a healthy minor leg passes, failure injection included, and says that no new dependency was exercised", 0,
     ["Docker update proof, minor leg: PASS", "failure: the run failed with error_code `daemon`", "failure: the rollback command", "No new dependency was exercised",
      "only the major leg takes nftables out"],
     ["**FAIL**", "INVALID", "The dependency path was exercised", "rollback after the major jump"], leg="minor")
case("a major leg on a box that kept nftables passes, and says that no new dependency was exercised", 0,
     ["Docker update proof, major leg: PASS", "No new dependency was exercised", "taking nftables out would take more than libraries with it: docker-ce nftables",
      "nftables was already installed"],
     ["**FAIL**", "INVALID", "The dependency path was exercised"], spec=HAD_NFT)
case("nftables taken out for a Docker that does not need it: no new dependency was exercised", 0,
     ["Docker update proof, major leg: PASS", "No new dependency was exercised", "the harness took out nftables"],
     ["**FAIL**", "INVALID", "The dependency path was exercised"], spec=NO_NEED)
case("ten new packages are within the bound", 0, ["Docker update proof, major leg: PASS", "The dependency path was exercised"], ["**FAIL**", "INVALID"], spec=many_new(10))
fails("eleven new packages are not", V_NEW, None, spec=many_new(11))
case("a package update in the way may be refused as `maintenance` or as `running`", 0, ["PASS"], ["**FAIL**"],
     mutate=lambda d: api(d, "unit-post", 409, refusal("maintenance")))

# the check
fails("the check does not offer the update", "GET /v1/sys/packages offers the Docker update", lambda d: jedit(d, "packages-1.json", lambda j: update(j).update(available=False, refusal="origin")))
fails("a docker.update that is not there at all is a FAIL, not a skip", "GET /v1/sys/packages offers the Docker update", lambda d: jedit(d, "packages-1.json", lambda j: j["data"]["docker"].pop("update")))
fails("an update that is available and refused", "GET /v1/sys/packages offers the Docker update", lambda d: jedit(d, "packages-1.json", lambda j: update(j).update(refusal="disk")))
fails("to is not what apt offers", "from and to are the engine versions apt shows", lambda d: jedit(d, "packages-1.json", lambda j: update(j).update(to="29.8.1")))
fails("from is not what is installed", "from and to are the engine versions apt shows", lambda d: jedit(d, "packages-1.json", lambda j: update(j).update(**{"from": "28.0.3"})))
fails("a major jump that says it is not one", "major_jump says what the jump is", lambda d: jedit(d, "packages-1.json", lambda j: update(j).update(major_jump=False)))
fails("a same-major update that says it is a jump", "major_jump says what the jump is", lambda d: jedit(d, "packages-1.json", lambda j: update(j).update(major_jump=True)), leg="minor")
def pkgs(j):
    return update(j)["packages"]


def add_new(name):
    """a package the plan would install, appended to the first check"""
    return lambda d: jedit(d, "packages-1.json", lambda j: pkgs(j).append({"name": name, "current_version": "", "candidate_version": "1.0-1", "new": True}))


fails("an upgrade of a package outside the allowlist", V_UP, lambda d: jedit(d, "packages-1.json", lambda j: pkgs(j).append(
    {"name": "libc6", "current_version": "2.31-13", "candidate_version": "2.31-14"})))
fails("an upgrade of the distribution's docker.io", V_UP, lambda d: jedit(d, "packages-1.json", lambda j: pkgs(j).append(
    {"name": "docker.io", "current_version": "1", "candidate_version": "2"})))
fails("a version string that is not a dpkg version", V_UP, lambda d: jedit(d, "packages-1.json", lambda j: pkgs(j)[0].update(candidate_version="1; rm -rf /")))
fails("an upgrade with no current version that does not say it is new", V_UP, lambda d: jedit(d, "packages-1.json", lambda j: pkgs(j)[0].update(current_version="")))
fails("a plan with nothing to upgrade", V_UP, lambda d: jedit(d, "packages-1.json", lambda j: update(j).update(packages=[p for p in pkgs(j) if p.get("new")])))
fails("a new package whose name is not a package name", V_NEW, add_new("Bad Name; reboot"))
for distro in ("docker.io", "containerd", "docker-compose-v2", "docker-buildx"):
    fails("a new package that is the distribution's %s" % distro, V_NEW, add_new(distro))
fails("a new package with a version that is not a dpkg version", V_NEW, lambda d: jedit(d, "packages-1.json", lambda j: [p for p in pkgs(j) if p["name"] == "nftables"][0].update(candidate_version="1.0; reboot")))
fails("a new package that says it has a current version", V_NEW, lambda d: jedit(d, "packages-1.json", lambda j: [p for p in pkgs(j) if p["name"] == "nftables"][0].update(current_version="0.9.7-1")))
fails("a plan entry that is not an object", V_UP, lambda d: jedit(d, "packages-1.json", lambda j: pkgs(j).append("nftables")))
fails("the plan's docker-ce is not what apt offers", "the plan's docker-ce is the one dpkg has", lambda d: jedit(d, "packages-1.json", lambda j: update(j)["packages"][0].update(candidate_version="5:29.9.9-1")))
fails("the plan's docker-ce is not what is installed", "the plan's docker-ce is the one dpkg has", lambda d: jedit(d, "packages-1.json", lambda j: update(j)["packages"][0].update(current_version="5:28.0.3-1~debian.11~bullseye")))
fails("a plan_id that is not the hash of the plan", "plan_id is the sha256", lambda d: jedit(d, "packages-1.json", lambda j: update(j).update(plan_id="a" * 64)))
fails("a container listed with the wrong restart policy", "GET /v1/sys/docker/containers lists the running containers", lambda d: jedit(d, "containers-before.json",
      lambda j: j["data"]["containers"][0].update(restart_policy="no")))
fails("the daemon reported as not running", "GET /v1/sys/docker/containers lists the running containers", lambda d: jedit(d, "containers-before.json", lambda j: j["data"].update(running=False)))
fails("the published port is not reported", "the published port and the host network are reported", lambda d: jedit(d, "containers-before.json",
      lambda j: [c.update(ports=[]) for c in j["data"]["containers"]]))
fails("the published port is reported with another host port", "the published port and the host network are reported", lambda d: jedit(d, "containers-before.json",
      lambda j: [c.update(ports=[{"port": 80, "protocol": "tcp", "host_port": 8080}]) for c in j["data"]["containers"] if c["name"] == "p-web"]))
fails("host networking is not reported", "the published port and the host network are reported", lambda d: jedit(d, "containers-before.json",
      lambda j: [c.update(host_network=False) for c in j["data"]["containers"]]))
fails("a box that is not idle before the first run", "the status before any run is idle", lambda d: jedit(d, "status-idle.json", lambda j: j["data"].update(state="failed")))

# the refusals
fails("a held docker-ce that is not refused by the check", "a held docker-ce is refused by the check", lambda d: jedit(d, "packages-held.json", lambda j: update(j).update(refusal="", available=True)))
fails("a held docker-ce refused with another code", "a held docker-ce is refused by the check", lambda d: jedit(d, "packages-held.json", lambda j: update(j).update(refusal="plan")))
fails("a held docker-ce that still has a button", "a held docker-ce is refused by the check", lambda d: jedit(d, "packages-held.json", lambda j: update(j).update(available=True)))
fails("a POST while held that goes through", "POST while docker-ce is held", lambda d: api(d, "held-post", 200, ok(status_data("running"))))
fails("a POST while held refused with `changed`", "POST while docker-ce is held", lambda d: api(d, "held-post", 409, refusal("changed")))
fails("a POST while dpkg is locked that goes through", "POST while dpkg is locked", lambda d: api(d, "lock-post", 200, ok(status_data("running"))))
fails("a POST while dpkg is locked refused with `running`", "POST while dpkg is locked", lambda d: api(d, "lock-post", 409, refusal("running")))
fails("a POST while a package update runs that goes through", "POST while the generic package update runs", lambda d: api(d, "unit-post", 200, ok(status_data("running"))))
fails("a POST while a package update runs refused with `changed`", "POST while the generic package update runs", lambda d: api(d, "unit-post", 409, refusal("changed")))
fails("a wrong plan_id that goes through", "POST with a plan_id that is not the plan", lambda d: api(d, "wrong-post", 200, ok(status_data("running"))))
fails("a wrong plan_id refused with another status", "POST with a plan_id that is not the plan", lambda d: api(d, "wrong-post", 400, refusal("changed")))
fails("a refusal whose message is not the error", "POST with a plan_id that is not the plan", lambda d: jedit(d, "wrong-post.json", lambda j: j.update(message="Conflict")))
fails("a refusal without a code", "POST with a plan_id that is not the plan", lambda d: jedit(d, "wrong-post.json", lambda j: j["data"].pop("error_code")))
fails("a refusal with no reason in it", "POST with a plan_id that is not the plan", lambda d: jedit(d, "wrong-post.json", lambda j: (j.update(message=""), j["data"].update(error=""))))
fails("a body that is not a plan_id accepted", "POST with a body that is not a plan_id", lambda d: api(d, "bad-post", 409, refusal("changed")))
fails("a token in the query that is accepted", "POST with the token in the query only", lambda d: api(d, "query-post", 409, refusal("changed")))
fails("a refresh token that is accepted", "POST with a refresh token", lambda d: api(d, "refresh-post", 409, refusal("changed")))
fails("the dashboard's own token refused", "POST with the dashboard's own token", lambda d: api(d, "jwt-post", 401, {"message": "nope"}))
fails("a plan that is not offered again after the hold", "after the hold is lifted", lambda d: jedit(d, "packages-2.json", lambda j: update(j).update(plan_id="b" * 64)))

# the run
fails("a POST that is refused", "POST /v1/sys/docker/update with the right plan_id starts the run", lambda d: api(d, "update-post", 409, refusal("changed")))
fails("a POST that blocks through apt", "the POST returns fast", lambda d: api(d, "update-post", 200, ok(status_data("running")), secs=45.0))
fails("a second POST that is not refused for running", "a second POST while the unit runs", lambda d: api(d, "running-post", 409, refusal("changed")))
fails("a run that never ends", "the run reaches a terminal state", lambda d: (jsonl_set(d, "status.jsonl", lambda t, h, x: (h, dict(x, state="running", outcome=""))),
                                                                                  put(d, "status.timeout", "1500 s\n")))
fails("a poll that stopped without a terminal state and without saying why", "the run reaches a terminal state", lambda d: (
    jsonl_set(d, "status.jsonl", lambda t, h, x: (h, dict(x, state="running", outcome=""))), None)[0])
fails("a status that goes back to idle", "the status never goes back to idle", lambda d: jsonl_set(d, "status.jsonl", lambda t, h, x: (h, dict(x, state="idle")) if abs(t - (T0 + 150)) < 1 else (h, x)))
fails("a status that says succeeded right after the POST", "the status right after the POST says running or finalizing",
      lambda d: jsonl_set(d, "status.jsonl", lambda t, h, x: (h, dict(x, state="succeeded")) if t < T0 + 106 else (h, x)))
fails("a status endpoint that goes away for three samples", "the status endpoint keeps answering",
      lambda d: jsonl_set(d, "status.jsonl", lambda t, h, x: ("000", None) if T0 + 140 <= t <= T0 + 144 else (h, x)))
case("a status endpoint that fails once is not a failure", 0, ["PASS"], ["**FAIL**"],
     mutate=lambda d: jsonl_set(d, "status.jsonl", lambda t, h, x: ("000", None) if abs(t - (T0 + 150)) < 1 else (h, x)))
fails("a run that failed", "the run succeeded", lambda d: jedit(d, "status-final.json", lambda j: final(j).update(state="failed", outcome="failed", error_code="install")))
fails("a run that ends restart_pending", "the run succeeded", lambda d: jedit(d, "status-final.json", lambda j: final(j).update(outcome="restart_pending")))
fails("a status that gets from wrong", "the status says from and to", lambda d: jedit(d, "status-final.json", lambda j: final(j).update(**{"from": "28.0.3"})))
fails("a status that gets to wrong", "the status says from and to", lambda d: jedit(d, "status-final.json", lambda j: final(j).update(to="")))
fails("not_returned that is empty", "not_returned lists the container with restart policy `no`", lambda d: jedit(d, "status-final.json", lambda j: final(j).update(not_returned=[])))
fails("not_returned that lists a container that came back", "not_returned lists the container with restart policy `no`", lambda d: jedit(d, "status-final.json",
      lambda j: final(j).update(not_returned=[{"name": "p-no", "restart_policy": "no"}, {"name": "p-db", "restart_policy": "unless-stopped"}])))
fails("a rollback command after a success", "there is no rollback command after a success", lambda d: jedit(d, "status-final.json", lambda j: final(j).update(rollback_command="sudo apt-get install x=1")))
fails("a completed_at before started_at", "started_at and completed_at are timestamps, in order", lambda d: jedit(d, "status-final.json", lambda j: final(j).update(completed_at=iso(T0))))
fails("a status without the log", "the status shows the log", lambda d: jedit(d, "status-final.json", lambda j: final(j).update(log="")))

# the log: the nonce rule, the order of the work, one terminal marker, last
fails("a line printed after the terminal marker", "success: the log has the markers", lambda d: replace(d, "docker-update.log", marker("SUCCESS", iso(T0 + 190)), marker("SUCCESS", iso(T0 + 190)) + "\ndpkg: trailing output"))
fails("a marker with another nonce", "success: the log has the markers", lambda d: replace(d, "docker-update.log", "Reading package lists...",
      PREFIX + "SUCCESS " + "f" * 32 + " " + iso(T0)))
fails("two terminal markers", "success: the log has the markers", lambda d: replace(d, "docker-update.log", "Reading package lists...", marker("FAILED", iso(T0) + " install")))
fails("a progress marker with another nonce", "success: the log has the markers", lambda d: replace(d, "docker-update.log", "Reading package lists...", PREFIX + "STARTED " + "f" * 32 + " " + iso(T0)))
fails("something before the core's line", "success: the log has the markers", lambda d: ledit(d, "docker-update.log", lambda ls: ["Reading package lists..."] + ls))
fails("installed before downloaded", "success: the log has the markers", lambda d: ledit(d, "docker-update.log", swap_kinds("DOWNLOADED", "INSTALLED")))
fails("no download marker", "success: the log has the markers", lambda d: ledit(d, "docker-update.log", lambda ls: [ln for ln in ls if "DOWNLOADED" not in ln]))
fails("a log that does not start with the core's line", "success: the log has the markers", lambda d: ledit(d, "docker-update.log", lambda ls: ls[1:]))
fails("a PREVIOUS marker without docker-ce", "the PREVIOUS marker records the docker-ce", lambda d: replace(d, "docker-update.log", "docker-ce=5:28.0.4-1~debian.11~bullseye ", ""), follow=True)
fails("a DAEMON marker with another version", "the DAEMON marker records the running version", lambda d: replace(d, "docker-update.log", marker("DAEMON", "29.8.0"), marker("DAEMON", "28.0.4")))
fails("a NOTRETURNED marker the status does not list", "the NOTRETURNED markers", lambda d: replace(d, "docker-update.log", marker("NOTRETURNED", "p-no no"), marker("NOTRETURNED", "p-db unless-stopped")))

V_PINS = "the PREVIOUS marker carries a pin for every package the update upgraded, at the version it had, and only pins"
V_JOURNAL = "the unit's journal has no line saying that systemd evaluated an environment variable name to an empty string"
PREV = "CASAOS_DOCKER_UPDATE_PREVIOUS " + NONCE
fails("a PREVIOUS marker that is empty (systemd ate the script's variables)", V_PINS,
      lambda d: ledit(d, "docker-update.log", lambda ls: [PREV if ln.startswith(PREV) else ln for ln in ls]),
      extra=["**FAIL** the PREVIOUS marker records the docker-ce"], follow=True)
fails("a PREVIOUS marker without one of the upgraded packages", V_PINS, lambda d: replace(d, "docker-update.log", " containerd.io=1.7.27-1", ""),
      extra=["missing: containerd.io=1.7.27-1"], follow=True)
fails("a PREVIOUS marker with a pin at another version than the one the package had", V_PINS, lambda d: replace(d, "docker-update.log", "containerd.io=1.7.27-1", "containerd.io=1.7.26-1"),
      extra=["missing: containerd.io=1.7.27-1"], follow=True)
fails("a PREVIOUS marker with a word that is not a pin", V_PINS, lambda d: replace(d, "docker-update.log", "containerd.io=1.7.27-1", "containerd.io=1.7.27-1 nftables;reboot"),
      extra=["not pins: nftables;reboot"], follow=True)
fails("a PREVIOUS marker that is empty in the minor leg too", V_PINS, lambda d: ledit(d, "docker-update.log", lambda ls: [PREV if ln.startswith(PREV) else ln for ln in ls]), leg="minor")
fails("a journal in which systemd emptied a variable of the script", V_JOURNAL, lambda d: put(d, "unit-journal.txt", JOURNAL + EMPTY_ENV),
      extra=["unit-journal.txt"])
fails("a journal of the later runs in which systemd emptied a variable", V_JOURNAL, lambda d: put(d, "unit-journal-end.txt", EMPTY_ENV + JOURNAL),
      extra=["unit-journal-end.txt"])
fails("that line in the minor leg, in other letters", V_JOURNAL, lambda d: put(d, "unit-journal.txt", JOURNAL + EMPTY_ENV.replace("Invalid environment", "invalid Environment")), leg="minor")
case("other lines about the unit are not that line", 0, ["PASS"], ["**FAIL**", "INVALID"],
     mutate=lambda d: put(d, "unit-journal.txt", JOURNAL + "Oct 09 10:02:02 box systemd[1]: casaos-docker-update.service: Failed to set up environment: x\n"))

# the box afterwards
fails("a daemon that is still the old one", "the running Docker is the new version", lambda d: replace(d, "snapshot-after.txt", "docker 29.8.0", "docker 28.0.4"))
fails("a daemon that is not the new one in the poller either", "the running Docker is the new version",
      lambda d: timeline_set(d, lambda i, f: f[:1] + ["28.0.4"] + f[2:] if 150 <= i <= 200 else f))
fails("a dpkg that has the old docker-ce", "dpkg has the version apt offered", lambda d: replace(d, "snapshot-after.txt", "docker-ce=5:29.8.0-1~debian.11~bullseye", "docker-ce=5:28.0.4-1~debian.11~bullseye"))
fails("a dockerd that was not restarted", "the new dockerd is a new process", lambda d: replace(d, "snapshot-after.txt", "unit docker MainPID=900", "unit docker MainPID=500"))
fails("a dockerd that came up before everything was downloaded", "everything was downloaded before Docker restarted", lambda d: replace(d, "snapshot-after.txt", "EnterEpoch=%d" % int(T0 + 147), "EnterEpoch=%d" % int(T0 + 110), 1))
fails("a container with restart policy always that did not come back", "every container with restart policy always or unless-stopped is running again",
      lambda d: replace(d, "snapshot-after.txt", "/p-web policy=unless-stopped state=running", "/p-web policy=unless-stopped state=exited"),
      extra=["not running: p-web"])
fails("a container that is down and not listed", "the containers that did not come back are exactly the ones the status listed",
      lambda d: replace(d, "snapshot-after.txt", "/p-web policy=unless-stopped state=running", "/p-web policy=unless-stopped state=exited"))
fails("a container with policy no that came back but is listed", "the containers that did not come back are exactly the ones the status listed",
      lambda d: replace(d, "snapshot-after.txt", "/p-no policy=no state=exited", "/p-no policy=no state=running"))
fails("AppManagement restarted", "AppManagement was not restarted", lambda d: replace(d, "snapshot-after.txt", "unit casaos-app-management MainPID=77", "unit casaos-app-management MainPID=99"))
fails("the core restarted", "the core was not restarted", lambda d: replace(d, "snapshot-after.txt", "unit casaos MainPID=79", "unit casaos MainPID=99"))
fails("the gateway restarted", "the gateway was not restarted", lambda d: replace(d, "snapshot-after.txt", "unit casaos-gateway MainPID=80", "unit casaos-gateway MainPID=99"))
fails("AppManagement not answering for three samples", "AppManagement keeps answering on a route that needs no Docker",
      lambda d: timeline_set(d, lambda i, f: f[:4] + ["500"] + f[5:] if 141 <= i <= 143 else f))
case("AppManagement failing once is not a failure", 0, ["PASS"], ["**FAIL**"], mutate=lambda d: timeline_set(d, lambda i, f: f[:4] + ["000"] + f[5:] if i == 141 else f))
fails("AppManagement not listing its projects after", "AppManagement lists its projects again", lambda d: api(d, "am-compose", 500))
fails("the published port silent after", "the published port answers again", lambda d: api(d, "web-after", 0))
fails("an acknowledged write that is not in the file", "no acknowledged write of the database is lost", lambda d: ledit(d, "db-file.txt", lambda ls: ls[:-3]))
fails("a database log that is empty", "no acknowledged write of the database is lost", lambda d: put(d, "db-logs.txt", ""))
fails("a check that still offers the update", "the next check shows the new version and nothing to update", lambda d: api(d, "packages-after", 200, packages_body(LEGS["major"], version="29.8.0")))
fails("a check that shows the old version", "the next check shows the new version", lambda d: jedit(d, "packages-after.json", lambda j: j["data"]["docker"].update(version="28.0.4")))
fails("a container list that lacks a container that is running", "GET /v1/sys/docker/containers lists the containers that are running again",
      lambda d: jedit(d, "containers-after.json", lambda j: j["data"].update(containers=[c for c in j["data"]["containers"] if c["name"] != "p-web"])))
fails("a container list that still has the container that did not come back", "GET /v1/sys/docker/containers lists the containers that are running again",
      lambda d: jedit(d, "containers-after.json", lambda j: j["data"]["containers"].append({"name": "p-no", "restart_policy": "no"})))

# the way back after the major jump: the command made of the PREVIOUS pins, run for real
V_ROLLBACK = "rollback after the major jump"
fails("a rollback whose apt-get fails", V_ROLLBACK, lambda d: put(d, "rollback-exit", "100\n"), extra=["apt-get exited 100"])
fails("a rollback that leaves the new Docker running", V_ROLLBACK, lambda d: replace(d, "snapshot-rollback.txt", "docker 28.0.4", "docker 29.8.0"), extra=["the daemon says '29.8.0'"])
fails("a rollback that leaves the new docker-ce installed", V_ROLLBACK,
      lambda d: replace(d, "snapshot-rollback.txt", "docker-ce=5:28.0.4-1~debian.11~bullseye", "docker-ce=5:29.8.0-1~debian.11~bullseye"), extra=["dpkg has docker-ce"])
fails("a container with a restart policy that is not running after the rollback", V_ROLLBACK,
      lambda d: replace(d, "snapshot-rollback.txt", "/p-web policy=unless-stopped state=running", "/p-web policy=unless-stopped state=exited"), extra=["not running: p-web"])
fails("containers that never settle after the rollback", V_ROLLBACK, lambda d: put(d, "rollback-settle", "never\n"), extra=["not back after 150 s"])
fails("an image that is gone after the rollback", V_ROLLBACK, lambda d: put(d, "images-rollback.txt", "sha256:1a2b3c busybox:latest\n"), extra=["images gone: sha256:4d5e6f nginx:alpine"])
fails("an image that lost its tag in the rollback", V_ROLLBACK, lambda d: put(d, "images-rollback.txt", "sha256:1a2b3c busybox:latest\nsha256:4d5e6f nginx:<none>\n"), extra=["images gone: sha256:4d5e6f nginx:alpine"])
fails("an acknowledged write the volume lost in the rollback", V_ROLLBACK, lambda d: ledit(d, "rollback-db-file.txt", lambda ls: ls[:-3]), extra=["acknowledged writes missing"])
fails("a database log that cannot be read after the rollback", V_ROLLBACK, lambda d: put(d, "rollback-db-logs.txt", ""), extra=["no acknowledged write could be read back"])
fails("a database file that cannot be read after the rollback", V_ROLLBACK, lambda d: put(d, "rollback-db-file.txt", ""), extra=["no acknowledged write could be read back"])
fails("a published port silent after the rollback", V_ROLLBACK, lambda d: api(d, "rollback-web", 0), extra=["the published port answers HTTP 0"])
case("no rollback to run because PREVIOUS carries no pin: the rollback fails, as the pins do", 1,
     ["**FAIL** " + V_ROLLBACK, "carries no pin", "**FAIL** " + V_PINS], ["INVALID"],
     mutate=lambda d: ledit(d, "docker-update.log", lambda ls: [PREV if ln.startswith(PREV) else ln for ln in ls]), follow=True)
case("a failed rollback says what it means for the button", 1, ["**FAIL** " + V_ROLLBACK, "must not offer the rollback command"], ["INVALID"],
     mutate=lambda d: put(d, "rollback-exit", "100\n"))
case("a rollback that holds says so, and not what a failed one means for the button", 0, ["**PASS** " + V_ROLLBACK, "Docker 28.0.4 started again"], ["must not offer the rollback command"])

# the failure injection
fails("a failure that is not `daemon`", "failure: the run failed with error_code `daemon`", lambda d: jedit(d, "fail-status-final.json", lambda j: final(j).update(error_code="install")), leg="minor")
fails("a failure run that succeeded", "failure: the run failed with error_code `daemon`", lambda d: jedit(d, "fail-status-final.json", lambda j: final(j).update(state="succeeded", outcome="success")), leg="minor")
fails("a failure without an explanation", "failure: the run failed with error_code `daemon`", lambda d: jedit(d, "fail-status-final.json", lambda j: final(j).update(error="")), leg="minor")
fails("a failure without a rollback command", "failure: the rollback command", lambda d: jedit(d, "fail-status-final.json", lambda j: final(j).update(rollback_command="")), leg="minor")
fails("a rollback command of another shape", "failure: the rollback command", lambda d: jedit(d, "fail-status-final.json",
      lambda j: final(j).update(rollback_command="sudo apt-get install --allow-downgrades docker-ce=5:29.8.1-1~ubuntu.24.04~noble; rm -rf /")), leg="minor")
fails("a rollback command that is not an install", "failure: the rollback command", lambda d: jedit(d, "fail-status-final.json",
      lambda j: final(j).update(rollback_command="sudo apt-get remove --allow-downgrades docker-ce=5:29.8.1-1~ubuntu.24.04~noble")), leg="minor")
fails("a rollback command that does not name docker-ce", "failure: the rollback command", lambda d: jedit(d, "fail-status-final.json",
      lambda j: final(j).update(rollback_command="sudo apt-get install --allow-downgrades docker-ce-cli=5:29.8.1-1~ubuntu.24.04~noble")), leg="minor")
fails("a rollback command to a package out of the allowlist", "failure: the rollback command", lambda d: jedit(d, "fail-status-final.json",
      lambda j: final(j).update(rollback_command="sudo apt-get install --allow-downgrades docker-ce=5:29.8.1-1~ubuntu.24.04~noble bash=1")), leg="minor")
fails("a rollback command to another version than the start", "failure: the rollback command", lambda d: jedit(d, "fail-status-final.json",
      lambda j: final(j).update(rollback_command="sudo apt-get install --allow-downgrades docker-ce=5:29.7.0-1~ubuntu.24.04~noble")), leg="minor")
fails("a failure poll that stopped without a terminal state and without saying why", "failure: the run reaches a terminal state",
      lambda d: jsonl_set(d, "fail-status.jsonl", lambda t, h, x: (h, dict(x, state="running"))), leg="minor")
fails("a failure that never ends", "failure: the run reaches a terminal state", lambda d: (jsonl_set(d, "fail-status.jsonl", lambda t, h, x: (h, dict(x, state="running"))),
                                                                                           put(d, "fail-status.timeout", "1500 s\n")), leg="minor")
fails("a status endpoint that goes away while dockerd cannot start", "failure: the status endpoint keeps answering", lambda d: jsonl_set(d, "fail-status.jsonl",
      lambda t, h, x: ("000", None) if T0 + 340 <= t <= T0 + 346 else (h, x)), leg="minor")
fails("a failure log that ends in a success", "failure: the log has the markers", lambda d: ledit(d, "docker-update-fail.log",
      lambda ls: ls[:-1] + [marker("SUCCESS", iso(T0 + 400))]), leg="minor")
fails("a FAILED marker with another reason", "failure: the FAILED marker names the reason `daemon`", lambda d: replace(d, "docker-update-fail.log", "daemon", "install"), leg="minor")
fails("a Docker that does not start again", "failure: with the file mended", lambda d: put(d, "fail-repaired", "no\n"), leg="minor")
fails("containers that do not come back after the repair", "failure: with the file mended", lambda d: put(d, "fail-settle", "never\n"), leg="minor")
fails("a failure POST that is refused", "failure: the POST starts the run", lambda d: api(d, "fail-post", 409, refusal("daemon")), leg="minor")

# what dpkg did during the run: the plan against the packages that really came and went
V_REMOVED = "the run removed no package"
V_STRAY = "nothing outside the plan was installed or upgraded"
V_ADDED = "dpkg added exactly the plan's new packages, at the planned versions"
V_UPGRADED = "dpkg upgraded exactly the plan's upgrades, from and to the planned versions"


def after_lines(fn):
    return lambda d: ledit(d, "dpkg-after.tsv", fn)


fails("a package the run removed", V_REMOVED, after_lines(lambda ls: [ln for ln in ls if not ln.startswith("jq\t")]))
fails("a package left unpacked counts as gone", V_REMOVED, lambda d: replace(d, "dpkg-after.tsv", "libc6\t2.31-13+deb11u11\tii ", "libc6\t2.31-13+deb11u11\tiU "))
fails("a package nobody planned that the run installed", V_STRAY, after_lines(lambda ls: ls + ["ufw\t0.36-7.1\tii "]), extra=["**FAIL** " + V_ADDED])
fails("a package nobody planned that the run upgraded", V_STRAY, lambda d: replace(d, "dpkg-after.tsv", "libc6\t2.31-13+deb11u11", "libc6\t2.31-13+deb11u12"),
      extra=["**FAIL** " + V_UPGRADED])
fails("a new package of the plan that dpkg did not install", V_ADDED, after_lines(lambda ls: [ln for ln in ls if not ln.startswith("nftables\t")]))
fails("a new package installed at another version than planned", V_ADDED, lambda d: replace(d, "dpkg-after.tsv", "nftables\t0.9.8-3.1+deb11u1", "nftables\t0.9.8-3.1+deb11u2"))
fails("a package the plan calls new that the box already had", V_ADDED, lambda d: (
    replace(d, "dpkg-before.tsv", "nftables\t0.9.8-3.1+deb11u1\trc ", "nftables\t0.9.8-3.1+deb11u1\tii "),
    put(d, "dependency-path", "action skipped\nreason taking nftables out would take more than libraries with it: x\n"))[0])
fails("an upgrade that dpkg did not apply", V_UPGRADED, lambda d: replace(d, "dpkg-after.tsv", "docker-ce\t5:29.8.0-1~debian.11~bullseye", "docker-ce\t5:28.0.4-1~debian.11~bullseye"))
fails("an upgrade from another version than the plan says", V_UPGRADED, lambda d: replace(d, "dpkg-before.tsv", "containerd.io\t1.7.27-1", "containerd.io\t1.7.26-1"))
fails("an upgrade to another version than the plan says", V_UPGRADED, lambda d: replace(d, "dpkg-after.tsv", "containerd.io\t2.1.4-1", "containerd.io\t2.1.3-1"))
case("new packages named in the plan that dpkg did not install are not claimed as a dependency that was exercised", 1,
     ["**FAIL** " + V_ADDED, "No new dependency was exercised"], ["The dependency path was exercised", "INVALID"],
     mutate=after_lines(lambda ls: [ln for ln in ls if not ln.startswith(("nftables\t", "libnftables1\t"))]))
case("a plan whose id counts the new packages: dropping one from the list is a different plan", 1, ["**FAIL** plan_id is the sha256"], ["INVALID"],
     mutate=lambda d: jedit(d, "packages-1.json", lambda j: update(j).update(packages=[p for p in pkgs(j) if p["name"] != "libedit2"])))

# a run that proves nothing is never green
invalid("an empty directory", "start-version is missing or empty", None, empty=True)
invalid("no journal of the unit", "unit-journal.txt is missing or holds no journal line", lambda d: remove(d, "unit-journal.txt"), silent=(V_JOURNAL,))
invalid("no journal of the unit for the later runs", "unit-journal-end.txt is missing or holds no journal line", lambda d: remove(d, "unit-journal-end.txt"), silent=(V_JOURNAL,))
invalid("a journal that only says there is nothing in it", "unit-journal.txt is missing or holds no journal line",
        lambda d: put(d, "unit-journal.txt", "-- No entries --\n"), silent=(V_JOURNAL,))
invalid("a journal that journalctl could not read", "unit-journal-end.txt is missing or holds no journal line",
        lambda d: put(d, "unit-journal-end.txt", "\n-- Journal begins at Fri 2026-10-09 --\n"), silent=(V_JOURNAL,))
SILENT_POLL = ("the run reaches a terminal state", "the status endpoint keeps answering")
SILENT_FAIL_POLL = ("failure: the run reaches a terminal state", "failure: the status endpoint keeps answering")
invalid("a status poll that is not there", "status.jsonl is missing or holds no status sample", lambda d: remove(d, "status.jsonl"), silent=SILENT_POLL)
invalid("a status poll that holds only lines that are not samples", "status.jsonl is missing or holds no status sample", lambda d: put(d, "status.jsonl", "not a sample\n"), silent=SILENT_POLL)
invalid("a status poll that is empty", "status.jsonl is missing or holds no status sample", lambda d: put(d, "status.jsonl", ""), silent=SILENT_POLL)
invalid("a failure poll that is not there", "fail-status.jsonl is missing or holds no status sample", lambda d: remove(d, "fail-status.jsonl"), leg="minor", silent=SILENT_FAIL_POLL)
invalid("a failure poll that is empty", "fail-status.jsonl is missing or holds no status sample", lambda d: put(d, "fail-status.jsonl", ""), leg="minor", silent=SILENT_FAIL_POLL)
invalid("a failure poll that holds only lines that are not samples", "fail-status.jsonl is missing or holds no status sample",
        lambda d: put(d, "fail-status.jsonl", "not a sample\n"), leg="minor", silent=SILENT_FAIL_POLL)
SILENT_ROLLBACK = (V_ROLLBACK,)
invalid("a rollback step that did not run", "rollback-exit or rollback-command is missing", lambda d: remove(d, "rollback-exit"), silent=SILENT_ROLLBACK)
invalid("a rollback command that was not recorded", "rollback-exit or rollback-command is missing", lambda d: remove(d, "rollback-command"), silent=SILENT_ROLLBACK)
invalid("a rollback command that is not an install of pins", "is not the one made of the PREVIOUS pins",
        lambda d: put(d, "rollback-command", "sudo apt-get remove docker-ce\n"), silent=SILENT_ROLLBACK)
invalid("a rollback command with a pin the log does not have", "is not the one made of the PREVIOUS pins",
        lambda d: replace(d, "rollback-command", "containerd.io=1.7.27-1", "containerd.io=1.7.26-1"), silent=SILENT_ROLLBACK)
invalid("a rollback command that leaves a pin out", "is not the one made of the PREVIOUS pins",
        lambda d: replace(d, "rollback-command", " containerd.io=1.7.27-1", ""), silent=SILENT_ROLLBACK)
invalid("a rollback command with a package outside the allowlist", "is not the one made of the PREVIOUS pins",
        lambda d: (replace(d, "rollback-command", "containerd.io=1.7.27-1", "containerd.io=1.7.27-1 bash=5.1-2"), replace(d, "docker-update.log", "containerd.io=1.7.27-1", "containerd.io=1.7.27-1 bash=5.1-2"))[0],
        silent=SILENT_ROLLBACK)
invalid("a rollback command that removes", "is not the one made of the PREVIOUS pins",
        lambda d: replace(d, "rollback-command", "sudo apt-get install --allow-downgrades ", "sudo apt-get remove --allow-downgrades "), silent=SILENT_ROLLBACK)
invalid("a rollback command that is another command altogether", "is not the one made of the PREVIOUS pins",
        lambda d: replace(d, "rollback-command", "sudo apt-get install --allow-downgrades ", "sudo sh -c 'rm -rf /' ; sudo apt-get install --allow-downgrades "), silent=SILENT_ROLLBACK)
invalid("a timeline that lost the start of the rollback", "marker rollback_start is missing from the timeline",
        lambda d: ledit(d, "timeline.tsv", lambda ls: [ln for ln in ls if "\trollback_start" not in ln]), silent=SILENT_ROLLBACK)
invalid("a rollback exit that is not a number", "rollback-exit says 'maybe'", lambda d: put(d, "rollback-exit", "maybe\n"), silent=SILENT_ROLLBACK)
for gone in ("snapshot-rollback.txt", "images-before.txt", "images-rollback.txt", "rollback-settle"):
    invalid("a rollback that left no %s" % gone, "%s is missing or empty: the rollback did not get that far" % gone, lambda d, gone=gone: remove(d, gone), silent=SILENT_ROLLBACK)
invalid("a rollback with no answer from the published port", "no answer was recorded for rollback-web", lambda d: remove(d, "rollback-web.code"), silent=SILENT_ROLLBACK)
invalid("a timeline that lost the end of the rollback", "marker rollback_settled is missing from the timeline",
        lambda d: ledit(d, "timeline.tsv", lambda ls: [ln for ln in ls if "\trollback_settled" not in ln]), silent=SILENT_ROLLBACK)
invalid("a run that stopped", "the run stopped before it was done: NOT PROVEN: no older release", lambda d: put(d, "aborted", "NOT PROVEN: no older release\n"))
invalid("a step that did not run", "no answer was recorded for held-post: that step did not run", lambda d: remove(d, "held-post.code", "held-post.json"))
invalid("a status that was never recorded", "status-final.json holds no status", lambda d: remove(d, "status-final.json"))
invalid("no log was collected", "docker-update.log was not collected", lambda d: remove(d, "docker-update.log"))
invalid("a container that was not running before", "these containers were not running before the update: p-db",
        lambda d: replace(d, "snapshot-before.txt", "/p-db policy=unless-stopped state=running", "/p-db policy=unless-stopped state=exited"))
invalid("a box that did not start on the Docker the leg names", "the box did not start on Docker 28.0.4 (the daemon says 28.0.3)", lambda d: replace(d, "snapshot-before.txt", "docker 28.0.4", "docker 28.0.3"))
invalid("a dpkg that has another docker-ce than the leg starts on", "dpkg has docker-ce", lambda d: put(d, "start-version", "28.0.3\n"))
invalid("a major leg with no major to jump to", "there is no major jump to prove",
        lambda d: put(d, "apt-docker-ce.txt", "installed 5:28.0.4-1~debian.11~bullseye\ncandidate 5:28.5.2-1~debian.11~bullseye\n"))
invalid("a minor leg whose candidate is another major", "not a same-major update",
        lambda d: put(d, "apt-docker-ce.txt", "installed 5:29.8.1-1~ubuntu.24.04~noble\ncandidate 5:30.0.0-1~ubuntu.24.04~noble\n"), leg="minor")
invalid("a minor leg with nothing newer on offer", "not a same-major update",
        lambda d: put(d, "apt-docker-ce.txt", "installed 5:29.8.1-1~ubuntu.24.04~noble\ncandidate 5:29.8.1-2~ubuntu.24.04~noble\n"), leg="minor")
invalid("a poller that left a hole", "the poller left a hole", lambda d: timeline_set(d, lambda i, f: f if not 110 <= i < 190 else ["#skip"]))
invalid("a timeline that lost its markers", "marker terminal is missing from the timeline", lambda d: ledit(d, "timeline.tsv", lambda ls: [ln for ln in ls if "\tterminal" not in ln]))
invalid("a daemon that did not answer when the update was asked for", "docker did not answer with 28.0.4 when the update was asked for",
        lambda d: timeline_set(d, lambda i, f: f[:1] + ["-"] + f[2:] if 95 <= i <= 100 else f))
invalid("a box that was not put back for the failure injection", "the failure injection cannot run",
        lambda d: jedit(d, "fail-packages.json", lambda j: update(j).update(available=False, refusal="plan")), leg="minor")
invalid("a repair that was not recorded", "the repair after the failure injection was not recorded", lambda d: remove(d, "fail-repaired"), leg="minor")
DPKG_VERDICTS = ("the run removed no package", "nothing outside the plan was installed or upgraded", "dpkg added exactly", "dpkg upgraded exactly")
invalid("no listing of the packages before the update", "dpkg-before.tsv is missing or empty", lambda d: remove(d, "dpkg-before.tsv"), silent=DPKG_VERDICTS)
invalid("no listing of the packages after the update", "dpkg-after.tsv is missing or empty", lambda d: remove(d, "dpkg-after.tsv"), silent=DPKG_VERDICTS)
invalid("a listing with no installed package in it", "dpkg-before.tsv lists no installed package", lambda d: put(d, "dpkg-before.tsv", "not what dpkg-query writes\n"), silent=DPKG_VERDICTS)
invalid("no record of what the harness did about the dependency", "dependency-path is missing or empty", lambda d: remove(d, "dependency-path"))
invalid("a record of the dependency step that says something else", "dependency-path says 'reformat'", lambda d: put(d, "dependency-path", "action reformat\n"))
invalid("a major leg that did not try the dependency step", "the major leg did not try the dependency step", lambda d: put(d, "dependency-path", "action not-attempted\nreason x\n"))
invalid("nftables taken out and still installed", "the harness says it took nftables out, and dpkg-before.tsv still has it installed",
        lambda d: replace(d, "dpkg-before.tsv", "nftables\t0.9.8-3.1+deb11u1\trc ", "nftables\t0.9.8-3.1+deb11u1\tii "))
invalid("a removal that does not name nftables", "does not name nftables", lambda d: put(d, "dependency-path", "action removed\npackages libedit2\n"))
case("a leg that is neither", 1, ["INVALID RUN", "major or minor"], [], leg="sideways", empty=True)
# a run in which the harness aborted AND the feature is wrong says both
case("a feature that is missing and a run that stopped says FAIL and INVALID", 1, ["FAIL and INVALID RUN", "**FAIL** GET /v1/sys/packages offers the Docker update"], [],
     mutate=lambda d: (jedit(d, "packages-1.json", lambda j: j["data"]["docker"].pop("update")), put(d, "aborted", "the first check has no docker.update.plan_id\n")))


# ---- the text-only functions of the guest script ------------------------------------------------------------------------

BASH = os.environ.get("TEST_BASH") or "bash"   # on Windows, plain `bash` may be WSL's launcher: TEST_BASH names Git's


def bash_run(snippet, *args, input=None):
    """the guest script sourced into a bash that then runs the snippet"""
    return subprocess.run([BASH, "-c", 'source "$1"; shift; ' + snippet, "_", SCRIPT.replace("\\", "/")] + list(args), capture_output=True, text=True, input=input)


def have_bash():
    try:
        return subprocess.run([BASH, "-c", "true"], capture_output=True).returncode == 0
    except OSError:
        return False


U = "~ubuntu.24.04~noble"
if not have_bash():
    # only where there is none (the WSL launcher on Windows is not one): a bash that is here and cannot source the script is a failure below
    print("skip: no bash that works here, the guest script's text functions are not tested")
else:
    picks = [
        ("the previous patch of the current minor", "5:29.8.2-1" + U, ["5:29.8.1-1" + U, "5:29.8.0-1" + U, "5:29.7.0-1" + U, "5:28.5.2-1" + U], "5:29.8.1-1" + U),
        ("the first one down when the minor has no other patch, in the same major", "5:29.8.0-1" + U, ["5:29.7.3-1" + U, "5:29.7.2-1" + U, "5:28.5.2-1" + U], "5:29.7.3-1" + U),
        ("the same release packaged again is not an older one", "5:29.8.2-3" + U, ["5:29.8.2-2" + U, "5:29.8.1-1" + U], "5:29.8.1-1" + U),
        ("a minor of two digits is a minor", "5:29.10.0-1" + U, ["5:29.9.3-1" + U, "5:29.9.2-1" + U], "5:29.9.3-1" + U),
        ("an older minor is taken before an older major", "5:29.8.0-1" + U, ["5:28.5.2-1" + U, "5:29.7.1-1" + U], "5:29.7.1-1" + U),
        ("nothing older in the same major: not provable", "5:29.0.0-1" + U, ["5:28.5.2-1" + U, "5:28.5.1-1" + U], None),
        ("nothing older at all: not provable", "5:29.8.2-1" + U, [], None),
    ]
    for label, newest, older, want in picks:
        count[0] += 1
        p = bash_run('newest="$1"; shift; printf "%s\\n" "$@" | pick_previous "$newest"', newest, *older)
        got = p.stdout.strip() or None
        if got != want or (p.returncode == 0) != (want is not None):
            failures.append("pick_previous, %s: got %r (status %d), wanted %r\n%s" % (label, got, p.returncode, want, p.stderr[-400:]))
        else:
            print("ok: pick_previous, " + label)
    for fn, arg, want in (("upstream_of", "5:29.8.2-1" + U, "29.8.2"), ("major_of", "5:29.8.2-1" + U, "29"), ("minor_of", "5:29.8.2-1" + U, "29.8"),
                          ("major_of", "1.7.27-1", "1"), ("upstream_of", "2.1.4-1", "2.1.4")):
        count[0] += 1
        got = bash_run('%s "$1"' % fn, arg).stdout.strip()
        if got != want:
            failures.append("%s %s: got %r, wanted %r" % (fn, arg, got, want))
        else:
            print("ok: %s %s" % (fn, arg))
    # sourcing the script runs nothing
    count[0] += 1
    p = bash_run("echo sourced")
    if p.stdout.strip() != "sourced" or p.returncode != 0:
        failures.append("sourcing the guest script did something: %r %r" % (p.stdout, p.stderr))
    else:
        print("ok: sourcing the guest script runs nothing")
    # run as itself, it refuses to start on a machine that is not marked disposable
    count[0] += 1
    env = {k: v for k, v in os.environ.items() if k != "PROOF_DISPOSABLE"}
    p = subprocess.run([BASH, SCRIPT.replace("\\", "/"), "minor"], capture_output=True, text=True, env=env)
    if p.returncode != 2 or "disposable machine" not in p.stderr:
        failures.append("the guest script started on a machine not marked disposable: %d %r" % (p.returncode, p.stderr[-300:]))
    else:
        print("ok: the guest script refuses a machine that is not marked disposable")
    count[0] += 1
    p = subprocess.run([BASH, SCRIPT.replace("\\", "/"), "sideways"], capture_output=True, text=True, env=env)
    if p.returncode != 2 or "usage" not in p.stderr:
        failures.append("the guest script took a leg that is neither: %d %r" % (p.returncode, p.stderr[-300:]))
    else:
        print("ok: the guest script refuses a leg that is neither")
    count[0] += 1
    env["DOCKER_FROM"] = "28.0.4; reboot"
    p = subprocess.run([BASH, SCRIPT.replace("\\", "/"), "major"], capture_output=True, text=True, env=env)
    if p.returncode != 2 or "DOCKER_FROM" not in p.stderr:
        failures.append("the guest script took a DOCKER_FROM that is not a version: %d %r" % (p.returncode, p.stderr[-300:]))
    else:
        print("ok: the guest script refuses a DOCKER_FROM that is not a version")

    # removal_ok: the answer of `apt-get -s remove nftables` to "may the harness take it out"
    removals = [
        ("nftables alone", "Remv nftables [0.9.8-3.1+deb11u1]\n", "nftables", True),
        ("nftables and the libraries that go with it", "Remv nftables [1]\nRemv libnftables1 [1]\nRemv libjansson4 [2]\nRemv libedit2 [3]\n",
         "nftables libnftables1 libjansson4 libedit2", True),
        ("names with an architecture", "Remv nftables:amd64 [1]\nRemv libedit2:amd64 [3]\n", "nftables:amd64 libedit2:amd64", True),
        ("what apt says besides the packages is not a package", "Reading package lists...\nConf nftables (1 Debian)\nRemv nftables [1]\n", "nftables", True),
        ("a package that needs nftables would go with it", "Remv firewalld [1]\nRemv nftables [1]\n", "firewalld nftables", False),
        ("Docker would go with it", "Remv docker-ce [1]\nRemv nftables [1]\n", "docker-ce nftables", False),
        ("an old kernel apt would also clear away", "Remv linux-image-5.10.0-30-amd64 [1]\nRemv nftables [1]\n", "linux-image-5.10.0-30-amd64 nftables", False),
        ("a purge of something else", "Purg nftables [1]\nPurg firewalld [1]\n", "nftables firewalld", False),
        ("something would be installed", "Inst foo (1 Debian)\nRemv nftables [1]\n", "nftables", False),
        ("nftables is not in it", "Remv libedit2 [3]\n", "libedit2", False),
        ("nothing is removed", "Reading package lists...\n", "", False),
    ]
    for label, sim, want_names, want_ok in removals:
        count[0] += 1
        p = bash_run("removal_ok", input=sim)
        if p.stdout.strip() != want_names or (p.returncode == 0) != want_ok:
            failures.append("removal_ok, %s: got %r (status %d), wanted %r (%s)\n%s" % (label, p.stdout.strip(), p.returncode, want_names, want_ok, p.stderr[-400:]))
        else:
            print("ok: removal_ok, " + label)

    # prepare_dependency_path, with stand-ins for the tools it calls
    fakes = {
        "dpkg-query": "#!/bin/sh\nif [ -e \"$FAKE/removed\" ]; then echo 'rc '; exit 0; fi\nif [ \"$FAKE_NFT\" = installed ]; then echo 'ii '; exit 0; fi\nexit 1\n",
        "apt-get": ("#!/bin/sh\necho \"apt-get $*\" >>\"$FAKE/calls\"\nsim=\"\"; auto=\"\"\n"
                    "for a in \"$@\"; do case \"$a\" in -s) sim=1 ;; --autoremove) auto=1 ;; esac; done\n"
                    "if [ -n \"$sim\" ]; then\n  if [ -n \"$auto\" ]; then printf '%s\\n' \"$FAKE_SIM_AUTO\"; else printf '%s\\n' \"$FAKE_SIM_PLAIN\"; fi\n  exit 0\nfi\n"
                    "if [ \"${FAKE_APT_RC:-0}\" != 0 ]; then exit \"$FAKE_APT_RC\"; fi\nif [ -z \"$FAKE_APT_KEEP\" ]; then : >\"$FAKE/removed\"; fi\n"),
        "systemctl": "#!/bin/sh\necho \"systemctl $*\" >>\"$FAKE/calls\"\n",
        "docker": "#!/bin/sh\nexit 0\n",
    }
    prep = ('source "$1"; OUT="$2"; LEG="$3"; APT=(env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3); '
            "set -euo pipefail; prepare_dependency_path; echo returned")
    auto_ok = "Remv nftables [1]\nRemv libnftables1 [1]\nRemv libjansson4 [2]\nRemv libedit2 [3]"
    kernel = "Remv linux-image-5.10.0-30-amd64 [1]\nRemv nftables [1]"
    needed = "Remv docker-ce [1]\nRemv nftables [1]"

    def prep_case(label, leg="major", nft="installed", auto="", plain="", apt_rc="0", keep="", record=(), no_record=(), calls=(), no_calls=(), aborted=None):
        count[0] += 1
        with tempfile.TemporaryDirectory() as tmp:
            fake, out = os.path.join(tmp, "fake").replace("\\", "/"), os.path.join(tmp, "out").replace("\\", "/")
            os.mkdir(fake)
            os.mkdir(out)
            for name, text in fakes.items():
                with open(os.path.join(fake, name), "w", newline="\n") as f:
                    f.write(text)
                os.chmod(os.path.join(fake, name), 0o755)
            env = dict(os.environ, PATH=fake + os.pathsep + os.environ["PATH"], FAKE=fake, FAKE_NFT=nft, FAKE_SIM_AUTO=auto,
                       FAKE_SIM_PLAIN=plain, FAKE_APT_RC=apt_rc, FAKE_APT_KEEP=keep)
            p = subprocess.run([BASH, "-c", prep, "_", SCRIPT.replace("\\", "/"), out, leg], capture_output=True, text=True, env=env)

            def slurp(path):
                try:
                    with open(path) as f:
                        return f.read()
                except OSError:
                    return ""
            rec, log_calls, abort_text = slurp(os.path.join(out, "dependency-path")), slurp(os.path.join(fake, "calls")), slurp(os.path.join(out, "aborted"))
            problems = ["missing %r in the record %r" % (s, rec) for s in record if s not in rec]
            problems += ["should not say %r in the record %r" % (s, rec) for s in no_record if s in rec]
            problems += ["missing call %r in %r" % (s, log_calls) for s in calls if s not in log_calls]
            problems += ["should not have called %r in %r" % (s, log_calls) for s in no_calls if s in log_calls]
            if aborted is None:
                if "returned" not in p.stdout:
                    problems.append("did not return: %r %r" % (p.stdout[-300:], p.stderr[-300:]))
                if abort_text:
                    problems.append("aborted: %r" % abort_text)
            elif aborted not in abort_text or "returned" in p.stdout:
                problems.append("wanted an abort saying %r, got %r (stdout %r)" % (aborted, abort_text, p.stdout[-200:]))
            if problems:
                failures.append("prepare_dependency_path, %s: %s\n%s" % (label, "; ".join(problems), p.stderr[-400:]))
            else:
                print("ok: prepare_dependency_path, " + label)

    prep_case("the minor leg takes nothing out", leg="minor", auto=auto_ok, record=["action not-attempted", "only the major leg takes nftables out"], no_calls=["apt-get", "systemctl"])
    prep_case("nothing to take out when nftables is not installed", nft="absent", auto=auto_ok, record=["action skipped", "reason nftables is not installed"], no_calls=["apt-get", "systemctl"])
    prep_case("nftables and its libraries go, when that is all apt would remove, and Docker starts again after", auto=auto_ok,
              record=["action removed", "packages nftables libnftables1 libjansson4 libedit2"],
              calls=["-s --autoremove remove nftables", "-y -q --autoremove remove nftables", "systemctl restart docker"])
    prep_case("the libraries nothing else needs go with it when apt offers both", auto=auto_ok, plain="Remv nftables [1]",
              record=["action removed", "packages nftables libnftables1 libjansson4 libedit2"], calls=["-y -q --autoremove remove nftables"], no_calls=["-y -q remove nftables"])
    prep_case("only nftables goes when apt would clear more away with it, if that is all a plain removal takes", auto=kernel, plain="Remv nftables [1]",
              record=["action removed", "packages nftables\n"], calls=["-s remove nftables", "-y -q remove nftables", "systemctl restart docker"],
              no_calls=["-y -q --autoremove remove nftables"])
    prep_case("nothing goes when something else needs nftables", auto=needed, plain=needed, no_record=["action removed"],
              record=["action skipped", "would take more than libraries with it: docker-ce nftables"], no_calls=["-y -q", "systemctl"])
    prep_case("a removal that fails is a run that cannot go on", auto=auto_ok, apt_rc="100", aborted="could not take nftables out", no_record=["action removed"], no_calls=["systemctl"])
    prep_case("a removal that leaves nftables installed is a run that cannot go on", auto=auto_ok, keep="1", aborted="nftables is still installed", no_record=["action removed"], no_calls=["systemctl"])

    # previous_pins: the pins of the PREVIOUS marker, read the way the core reads the log
    FORGED = "f" * 32
    QUEUED = PREFIX + "QUEUED " + NONCE + " 2026-10-09T10:00:00Z"
    PREV_LINE = PREFIX + "PREVIOUS " + NONCE
    DOCKER_PIN = "docker-ce=5:28.0.4-1~debian.11~bullseye"
    pins_cases = [
        ("the pins of the marker, in order", [QUEUED, PREFIX + "STARTED " + NONCE + " t", PREV_LINE + " " + DOCKER_PIN + " containerd.io=1.7.27-1"], [DOCKER_PIN, "containerd.io=1.7.27-1"]),
        ("a marker with no pin", [QUEUED, PREV_LINE], []),
        ("a marker with another nonce is somebody else's text", [QUEUED, PREFIX + "PREVIOUS " + FORGED + " " + DOCKER_PIN], []),
        ("words that are not pins are dropped", [QUEUED, PREV_LINE + " docker-ce=1 $(reboot) a;b=1 =2 x=y docker-ce-cli=5:2-1"], ["docker-ce=1", "docker-ce-cli=5:2-1"]),
        ("the nonce is the first line's: a QUEUED line further down is ignored", ["Reading package lists...", QUEUED, PREV_LINE + " " + DOCKER_PIN], []),
        ("a line that merely holds the marker is not the marker", [QUEUED, "container: " + PREV_LINE + " " + DOCKER_PIN], []),
        ("a queued line whose nonce is not 32 hex digits", [PREFIX + "QUEUED abc ts", PREFIX + "PREVIOUS abc " + DOCKER_PIN], []),
        ("a queued line whose nonce is 32 characters and not hex", [PREFIX + "QUEUED " + "g" * 32 + " ts", PREFIX + "PREVIOUS " + "g" * 32 + " " + DOCKER_PIN], []),
        ("no log", [], []),
    ]
    for label, log_lines, want in pins_cases:
        count[0] += 1
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "docker-update.log").replace("\\", "/")
            with open(path, "w", newline="\n") as f:
                f.write("".join(ln + "\n" for ln in log_lines))
            p = bash_run('previous_pins "$1"', path)
        got = p.stdout.split()
        if got != want or (p.returncode == 0) != bool(want):
            failures.append("previous_pins, %s: got %r (status %d), wanted %r\n%s" % (label, got, p.returncode, want, p.stderr[-400:]))
        else:
            print("ok: previous_pins, " + label)

    # rollback_run, with stand-ins for apt-get, docker, dpkg, systemctl and curl
    rfakes = {
        "apt-get": "#!/bin/sh\necho \"apt-get $*\" >>\"$FAKE/calls\"\nexit \"${FAKE_APT_RC:-0}\"\n",
        "docker": ("#!/bin/sh\necho \"docker $*\" >>\"$FAKE/calls\"\ncase \"$1\" in\ninfo) exit 0 ;;\nversion) echo 28.0.4 ;;\n"
                   "ps) case \"$*\" in *-aq*) echo aaaa ;; *) printf 'p-always\\np-unless-stopped\\np-db\\np-web\\np-host\\n' ;; esac ;;\n"
                   "inspect) echo '/p-always policy=always state=running started=x restarts=0' ;;\n"
                   "image) echo 'sha256:1a2b3c busybox:latest' ;;\nlogs) echo '2026-10-09T10:00:00.000000000Z ack 1790000000-1' ;;\nrun) echo 1790000000-1 ;;\nesac\n"),
        "dpkg": "#!/bin/sh\nexit 0\n",
        "systemctl": "#!/bin/sh\necho \"systemctl $*\" >>\"$FAKE/calls\"\n",
    }
    # curl is a function here: where a curl is installed beside bash (Git's), a stand-in in PATH is not the one that is found
    rb = ('source "$1"; OUT="$2"; APT=(env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3); curl() { printf 200; }; '
          "set -euo pipefail; rollback_run; echo returned")

    def rollback_case(label, log_lines, apt_rc="0", command=None, exit_is=None, ran=True, calls=(), no_calls=()):
        count[0] += 1
        with tempfile.TemporaryDirectory() as tmp:
            fake, out = os.path.join(tmp, "fake").replace("\\", "/"), os.path.join(tmp, "out").replace("\\", "/")
            os.mkdir(fake)
            os.mkdir(out)
            for name, text in rfakes.items():
                with open(os.path.join(fake, name), "w", newline="\n") as f:
                    f.write(text)
                os.chmod(os.path.join(fake, name), 0o755)
            with open(os.path.join(out, "docker-update.log"), "w", newline="\n") as f:
                f.write("".join(ln + "\n" for ln in log_lines))
            env = dict(os.environ, PATH=fake + os.pathsep + os.environ["PATH"], FAKE=fake, FAKE_APT_RC=apt_rc)
            p = subprocess.run([BASH, "-c", rb, "_", SCRIPT.replace("\\", "/"), out], capture_output=True, text=True, env=env)

            def slurp(name, where=out):
                try:
                    with open(os.path.join(where, name)) as f:
                        return f.read()
                except OSError:
                    return ""
            seen, timeline = slurp("calls", fake), slurp("timeline.tsv")
            problems = []
            if "returned" not in p.stdout:
                problems.append("did not return: %r %r" % (p.stdout[-300:], p.stderr[-300:]))
            if slurp("rollback-command").strip() != command:
                problems.append("rollback-command is %r, wanted %r" % (slurp("rollback-command").strip(), command))
            if slurp("rollback-exit").strip() != exit_is:
                problems.append("rollback-exit is %r, wanted %r" % (slurp("rollback-exit").strip(), exit_is))
            problems += ["missing call %r in %r" % (s, seen) for s in calls if s not in seen]
            problems += ["should not have called %r in %r" % (s, seen) for s in no_calls if s in seen]
            if "\trollback_start\n" not in timeline.replace("\r", ""):
                problems.append("no rollback_start marker in %r" % timeline)
            for name in ("snapshot-rollback.txt", "images-rollback.txt", "rollback-settle", "rollback-db-logs.txt", "rollback-db-file.txt", "rollback-web.code"):
                if bool(slurp(name).strip()) != ran:
                    problems.append("%s %s" % (name, "is empty" if ran else "should not be there"))
            for marker_name in ("rollback_done", "rollback_settled"):
                if ("\t%s\n" % marker_name in timeline.replace("\r", "")) != ran:
                    problems.append("marker %s %s" % (marker_name, "is missing" if ran else "should not be there"))
            if ran and slurp("rollback-web.code").strip() != "200":
                problems.append("rollback-web.code is %r" % slurp("rollback-web.code"))
            if problems:
                failures.append("rollback_run, %s: %s\n%s" % (label, "; ".join(problems), p.stderr[-400:]))
            else:
                print("ok: rollback_run, " + label)

    two_pins = DOCKER_PIN + " containerd.io=1.7.27-1"
    rollback_case("the PREVIOUS pins are what is installed, with the downgrade allowed, and the box is recorded afterwards", [QUEUED, PREV_LINE + " " + two_pins],
                  command="sudo apt-get install --allow-downgrades " + two_pins, exit_is="0",
                  calls=["install -y -q --allow-downgrades -o Dpkg::Options::=--force-confold " + two_pins, "docker image ls", "docker logs --timestamps p-db"],
                  no_calls=["--allow-change-held-packages", "--allow-remove-essential", "docker restart", "systemctl restart"])
    rollback_case("an apt-get that fails is recorded, and the box is looked at all the same", [QUEUED, PREV_LINE + " " + two_pins], apt_rc="100",
                  command="sudo apt-get install --allow-downgrades " + two_pins, exit_is="100", calls=["install -y -q --allow-downgrades"])
    rollback_case("a PREVIOUS without a pin leaves nothing to run", [QUEUED, PREV_LINE], command="none", exit_is="not-run", ran=False, no_calls=["apt-get"])
    rollback_case("a PREVIOUS of another nonce leaves nothing to run", [QUEUED, PREFIX + "PREVIOUS " + FORGED + " " + two_pins], command="none", exit_is="not-run", ran=False, no_calls=["apt-get"])

if failures:
    print("\n".join(failures), file=sys.stderr)
    print("%d of %d cases failed" % (len(failures), count[0]), file=sys.stderr)
    sys.exit(1)
print("ok: the proof can say no (%d cases)" % count[0])
