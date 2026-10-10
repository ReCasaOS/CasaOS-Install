#!/usr/bin/env python3
"""Turn what docker-update-proof.sh wrote into verdicts, or say the run is invalid.

    docker-update-proof-report.py <out dir> <major|minor> [kill]

Prints a markdown report (and writes it to <out dir>/report.md).

Exit status: 0 only when every verdict is PASS. 1 for a FAIL (the check, the button or the unit did
not do what the spec says) and 1 for an INVALID RUN (the harness could not do what it set out to:
the box was not what it should be, a step did not run, the poller died, the box could not be put
where the leg needs it). An invalid run proves nothing and is never green. A step that did not run
is INVALID, not a verdict: there is no skipped check that stays quiet. So is a tool of the harness that failed on the guest (a
date that cannot read a time, a curl that reaches nothing, an apt that cannot fetch, a docker CLI that cannot run): a FAIL needs a
value the feature produced, and what the harness's own tools leave behind when they fail says nothing about the button.

The legs:
    major   Debian 11, Docker pinned to 28.0.4 from Docker's repo, a 29 on offer: the owner's box. The
            harness takes nftables out first when apt can do that cleanly, because Docker 29 needs it
            and Docker 28 did not: the update then has a package to bring that the box does not have. When it has
            succeeded the harness runs the rollback command (apt-get install --allow-downgrades of the PREVIOUS pins) and the
            verdict "rollback after the major jump" says whether Docker 28.0.4 starts again with the containers, the database's
            volume and the images intact. A FAIL there is meant to turn the leg red: it means that the core must not offer the
            rollback command after a major jump.
    minor   Ubuntu 24.04, the previous patch of the current Docker minor, then a second update with
            dockerd made unable to start (failure injection, last). One minor leg is also given `kill` after the
            leg: it kills the unit with SIGKILL in the middle of the install (as soon as dpkg runs a maintainer
            script of Docker's), before that last failure. The report then wants the evidence of it: what the
            status says (failed, `no_result` or `install`), what dpkg --audit says, that the next check after
            the kill refuses with `dpkg` (and the one after the repair refuses nothing), and that the repair the
            dashboard will name (dpkg --configure -a, then apt-get -f install) completes the install.

The legs run on more than one system: the report shows the tools of the one it judges (apt, systemd, dpkg, the
utilities the unit's script calls) and checks that this system's apt prints its simulation in the form the core reads.

One verdict depends on the system: the unit's journal has no line saying that systemd emptied a variable of the command
line. systemd writes such a line from v254 on (Ubuntu 24.04's 255, Debian 13's 257) and never before (Debian 11's 247,
Debian 12's 252), so on an older one that verdict is NOT APPLICABLE: named in the report, neither a pass nor a fail, and not
counted among the passes. What catches an emptied variable on those systems is the verdict on the pins of the PREVIOUS marker.

The plan may hold packages the box does not have (the dependencies of the new Docker). It is judged on
what the box did with it: the packages dpkg had installed just before the POST and just after the run
(dpkg-before.tsv, dpkg-after.tsv) must differ by exactly the plan's new packages (added) and its upgrades,
and by nothing else, and nothing may be removed. A run in which no new package was brought says so
("No new dependency was exercised"): that is a pass, and it is not a proof of the dependency path.
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime

out_dir, leg = sys.argv[1], sys.argv[2]
options = sys.argv[3:]

ALLOWLIST = ["docker-ce", "docker-ce-cli", "containerd.io", "docker-ce-rootless-extras", "docker-buildx-plugin",
             "docker-compose-plugin", "docker-model-plugin"]
# what the harness starts, with the restart policy it gives each
CONTAINERS = {"p-always": "always", "p-unless-stopped": "unless-stopped", "p-no": "no", "p-db": "unless-stopped",
              "p-web": "unless-stopped", "p-host": "unless-stopped"}
COMES_BACK = ("always", "unless-stopped")
TERMINAL = ("succeeded", "failed")
VERSION_RE = re.compile(r"^([0-9]+:)?[0-9][A-Za-z0-9.+~-]*$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.\-]*(:[a-z0-9]+)?$")   # dockerpkg.ValidName
DISTRO_DOCKER = ("docker.io", "containerd", "docker-compose-v2", "docker-buildx")   # the distribution's, in conflict with Docker's own packages
MAX_NEW = 10           # dockerpkg.MaxNewPackages
MARKER_RE = re.compile(r"^CASAOS_DOCKER_UPDATE_([A-Z_]+) ([0-9a-f]{32})(?: (.*))?$")
PIN_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*=([0-9]+:)?[0-9][A-Za-z0-9.+~-]*$")   # name=version, the shape of a pin in PREVIOUS and in a rollback command
# systemd rewrites ${NAME} in the command line of a transient unit: a name it has no value for becomes the empty string. From v254 on it says so, in
# one of two words (src/core/exec-invoke.c): "Referenced but unset environment variable evaluates to an empty string: Package" for a name that is valid
# and has no value, "Invalid environment variable name evaluates to an empty string: pin%%=*" for a name that is not a name. Before v254 it says nothing.
EMPTY_ENV_RE = re.compile(r"(Referenced but unset|Invalid) environment variable( name)? evaluates to an empty string", re.I)
JOURNAL_FROM = 254     # the first systemd that writes either line
V_PINS = "the PREVIOUS marker carries a pin for every package the update upgraded, at the version it had, and only pins"
V_ROLLBACK = "rollback after the major jump"
V_SIM = "apt's simulation exits 0 and names docker-ce from the installed version to the one on offer, in the form the core reads"
K_POST = "kill -9: the POST starts the run"
K_TERMINAL = "kill -9: the run reaches a terminal state"
K_ANSWER = "kill -9: the status endpoint keeps answering while dpkg is half-finished"
K_FAILED = "kill -9: the run is reported as failed, with error_code `no_result` or `install`"
K_LOG = "kill -9: the log agrees with the status"
K_ROLLBACK = "kill -9: the rollback command is a fixed-shape apt command with validated pins that goes back to the start version"
K_AUDIT = "kill -9: dpkg --audit lists the half-finished install that `dpkg --configure -a` and `apt-get -f install` are for"
K_REPAIR = "kill -9: after `dpkg --configure -a` and `apt-get -f install` dpkg --audit is empty and Docker answers"
K_DIRTY = "kill -9: the next check after the kill refuses with `dpkg`, before the repair"
K_CLEAN = "kill -9: after the repair the check refuses nothing"
V_JOURNAL = "the unit's journal has no line saying that systemd evaluated an environment variable of the command line to an empty string"
# a curl that got no HTTP status is the feature's silence when it reached the core and waited for nothing (it timed out, was cut off), and the
# harness's failure in any other case (it could not resolve, could not connect, could not even start)
CURL_REACHED = (28, 52, 55, 56)
# what apt says when the network, dpkg's lock or its package lists are the trouble, and not the packages it was asked for
APT_TROUBLE_RE = re.compile(r"Failed to fetch|Temporary failure resolving|Could not resolve|Unable to locate package|Could not get lock|Unable to acquire the dpkg frontend lock|"
                            r"Hash Sum mismatch|Some index files failed to download|Connection (?:timed out|failed|reset)", re.I)
POST_SECONDS = 30      # "a few seconds" in the spec; this only catches a POST that blocks through apt
HOLE = 60.0            # seconds without a poller sample, inside the run: the poller is dead
FAIL_RUN = 3           # consecutive status samples that failed: the status endpoint went away

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


def not_applicable(name, why):
    """a check that this system cannot answer: named in the report, neither a pass nor a fail"""
    verdicts.append(("NOT APPLICABLE", name, why))


def on(have, cond):
    return None if not have else bool(cond)


def invalid(reason):
    if reason not in problems:
        problems.append(reason)


def finish():
    fails = [v for v in verdicts if v[0] == "FAIL"]
    head = "FAIL and INVALID RUN" if problems and fails else "INVALID RUN" if problems else "FAIL" if fails else "PASS"
    text = ["# Docker update proof, %s leg: %s" % (leg, head), ""]
    if problems:
        text += ["Nothing below that depends on the missing or untrustworthy evidence can be believed:", ""]
        text += ["- INVALID: " + p for p in problems] + [""]
    text += lines
    text += ["", "## Verdicts", ""]
    text += ["- **%s** %s%s" % (s, n, (": " + d) if d else "") for s, n, d in verdicts]
    skipped = [v[1] for v in verdicts if v[0] == "NOT APPLICABLE"]
    passes = sum(1 for v in verdicts if v[0] == "PASS")
    text += ["", "%d of %d verdicts pass%s%s." % (passes, passes + len(fails), "; failing: " + "; ".join(v[1] for v in fails) if fails else "",
                                                "; %d not applicable: %s" % (len(skipped), "; ".join(skipped)) if skipped else "")]
    out = "\n".join(text) + "\n"
    try:
        with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
            f.write(out)
    except OSError:
        pass
    print(out)
    sys.exit(0 if not problems and not fails else 1)


# ---- reading what was written ----------------------------------------------------------

def resp(name):
    """(http code, body) of an API call the harness made; (None, {}) when that call was never made, or its curl never reached the core"""
    code = read(name + ".code").strip()
    if not code:
        invalid("no answer was recorded for %s: that step did not run" % name)
        return None, {}
    curl = read(name + ".curl").strip()
    if curl.isdigit() and int(curl) != 0 and int(curl) not in CURL_REACHED and not (code.isdigit() and int(code) > 0):
        invalid("curl could not reach the core for %s (curl exit %s, see %s.err): the harness asked nothing of the feature" % (name, curl, name))
        return None, {}
    try:
        body = json.loads(read(name + ".json") or "null")
    except ValueError:
        body = None
    return (int(code) if code.isdigit() else 0), (body if isinstance(body, dict) else {})


def dget(body, *path):
    cur = body
    for key in ("data",) + path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def dobj(body, *path):
    v = dget(body, *path)
    return v if isinstance(v, dict) else {}


def dlist(body, *path):
    v = dget(body, *path)
    return v if isinstance(v, list) else []


def secs(name):
    try:
        return float(read(name + ".secs").strip())
    except ValueError:
        return None


def engine(v):
    """dockerpkg.EngineVersion: '5:29.8.0-1~debian.11~bullseye' -> '29.8.0'"""
    return re.sub(r"-.*", "", re.sub(r"^\d+:", "", v)) if v else None


def major(v):
    m = re.match(r"(\d+)", v or "")
    return int(m.group(1)) if m else None


def timestamp(s):
    """seconds since the epoch of an ISO 8601 / RFC 3339 time (no zone: UTC), or None"""
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$", s or "")
    if not m:
        return None
    base, frac, zone = m.groups()
    try:
        return datetime.fromisoformat(base + ("." + (frac + "000000")[:6] if frac else "") + ("+00:00" if zone in (None, "Z") else zone)).timestamp()
    except ValueError:
        return None


def valid_version(v):
    return isinstance(v, str) and len(v) <= 100 and bool(VERSION_RE.match(v))


def valid_name(n):
    return isinstance(n, str) and len(n) <= 128 and bool(NAME_RE.match(n))


def bare(n):
    """a package name without the architecture apt adds to a foreign one"""
    return re.sub(r":[a-z0-9]+$", "", str(n))


def load_dpkg(name):
    """{package: version} of what dpkg had installed (its second state letter is `i`: `ii`, `hi`, not `rc` or `iU`) in a dump of
    `dpkg-query -W` (name, version, state; tab separated). One row per name: a box with two architectures would need name:arch."""
    out = {}
    for line in read(name).splitlines():
        f = line.split("\t")
        if len(f) == 3 and f[1] and f[2].strip()[1:2] == "i":
            out[f[0]] = f[1]
    return out


def snapshot(name):
    s = read("snapshot-%s.txt" % name)
    info = {"containers": {}, "present": bool(s.strip())}
    for line in s.splitlines():
        m = re.match(r"^/(\S+) policy=(\S*) state=(\S+) started=(\S+) restarts=(\d+)", line)
        if m:
            info["containers"][m.group(1)] = dict(policy=m.group(2), state=m.group(3))
        elif line.startswith(("docker ", "driver ", "packages ")):
            k, _, v = line.partition(" ")
            info[k] = v.strip()
        elif line.startswith("unit "):
            parts = line.split(" ", 2)
            info["unit " + parts[1]] = parts[2] if len(parts) > 2 else ""
    return info


def unit_number(info, unit, field):
    m = re.search(r"%s=(\d+)" % field, info.get("unit " + unit, ""))
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


def package_version(info, name):
    m = re.search(r"(?:^| )%s=(\S+)" % re.escape(name), info.get("packages", ""))
    return m.group(1) if m else None


def running(info):
    return {n for n, c in info["containers"].items() if c["state"] == "running"}


def rollback_command_ok(rb):
    """the command a failure offers: a fixed shape, validated pins of the engine's packages, and docker-ce at the version the leg started on"""
    pins = rb.split()[4:] if rb.startswith("sudo apt-get install --allow-downgrades ") else []
    return bool(pins) and all(PIN_RE.match(p) and p.split("=")[0] in ALLOWLIST for p in pins) and \
        any(p.startswith("docker-ce=") and engine(p.split("=", 1)[1]) == start_version for p in pins)


def tool_trouble(status, text):
    """why a command that ended with this status, and said this, did not get to try what it was run for; None when it did. The harness's own trouble:
    the shell could not run it (126, 127), it was killed (128 and above), or apt says that the network, dpkg's lock or its package lists were the
    trouble. A version apt does not have, or packages that do not agree, are not that: they are what the command was run to find out."""
    if status in (126, 127):
        return "the shell could not run it (exit %d)" % status
    if status >= 128:
        return "it was killed (exit %d)" % status
    m = APT_TROUBLE_RE.search(text or "") if status else None
    return "apt says that the network, the lock or its package lists were the trouble (%r)" % m.group(0) if m else None


def audit_of(name):
    """(exit status, text) of a `dpkg --audit` the harness recorded: its first line is `exit N`, the rest is what dpkg said (nothing when it found nothing wrong)"""
    first, _, rest = read(name).partition("\n")
    m = re.match(r"^exit (\d+)$", first.strip())
    if not m:
        invalid("%s is missing or not a recorded dpkg --audit (its first line is `exit N`)" % name)
        return None
    return int(m.group(1)), rest.strip()


def lost_writes(logs_name, file_name):
    """(the ids the database acknowledged, the ids its file on the volume holds, the acknowledged ones the file does not hold)"""
    acked = re.findall(r"^\S+ ack (\S+)$", read(logs_name), re.M)
    filed = set(read(file_name).split())
    return acked, filed, [a for a in acked if a not in filed]


def db_evidence(prefix):
    """True when both commands that read the database's evidence (its log, its file on the volume) ran to their end. When one did not, say so: what it left
    proves nothing about the writes (a lost write is a file that was read and lacks them)"""
    ok_ = True
    for ev, what in (("db-logs", "docker logs"), ("db-file", "docker run ... cat")):
        name = "%s%s.exit" % (prefix, ev)
        status = read(name).strip()
        if not status.isdigit():
            invalid("%s is missing or not an exit status: the database's evidence was not read to its end" % name)
            ok_ = False
        elif status != "0":
            invalid("%s: %s exited %s, so %s%s.txt says nothing about the writes" % (name, what, status, prefix, ev))
            ok_ = False
    return ok_


def load_samples(name):
    """the status endpoint as the harness polled it: [(t, http code, data or None)]"""
    out = []
    for line in read(name).splitlines():
        f = line.split("\t", 2)
        if len(f) != 3:
            continue
        try:
            t = float(f[0])
            data = json.loads(f[2])
        except ValueError:
            continue
        out.append((t, f[1], data if isinstance(data, dict) else None))
    return out


def episodes(win, is_bad, need_bad, need_good):
    """[(start, end or None)] of the runs of at least need_bad consecutive bad samples (win: dicts with a "t"),
    each ended by the first of need_good consecutive good ones; a run that reaches the end of the window counts"""
    eps, i, n = [], 0, len(win)
    while i < n:
        if not is_bad(win[i]):
            i += 1
            continue
        j = i
        while j < n and is_bad(win[j]):
            j += 1
        if j - i < need_bad and j < n:
            i = j
            continue
        end = next((win[k]["t"] for k in range(j, n) if len(win[k:k + need_good]) == need_good and not any(is_bad(w) for w in win[k:k + need_good])), None)
        eps.append((win[i]["t"], end))
        if end is None:
            break
        i = next((x for x in range(j, n) if win[x]["t"] >= end), n)
    return eps


def status_runs(samples):
    return episodes([{"t": t, "bad": h != "200"} for t, h, _ in samples], lambda r: r["bad"], FAIL_RUN, 1)


def parse_log(text):
    """[(kind, nonce, fields)] of the lines that are markers"""
    return [(m.group(1), m.group(2), m.group(3) or "") for m in (MARKER_RE.match(ln) for ln in text.splitlines()) if m]


def check_log(text, label, want_terminal, order):
    marks = parse_log(text)
    nonempty = [ln for ln in text.splitlines() if ln.strip()]
    kinds = [m[0] for m in marks]
    terms = [k for k in kinds if k in ("SUCCESS", "RESTART_PENDING", "FAILED")]
    # a kind that is missing counts -1 and breaks the order, except at the front, which the first-line rule below catches
    idx = [kinds.index(k) if k in kinds else -1 for k in order]
    ordered = idx == sorted(idx)
    last = nonempty[-1].split(" ")[0] if nonempty else ""
    first = nonempty[0].split(" ")[0] if nonempty else ""
    verdict("%s: the log has the markers in the order of the work, one nonce, one terminal marker, last" % label,
            ordered and len({m[1] for m in marks}) == 1 and terms == [want_terminal] and last == "CASAOS_DOCKER_UPDATE_" + want_terminal
            and first == "CASAOS_DOCKER_UPDATE_QUEUED",
            "kinds %s; nonces %d; terminal markers %s; last line %r" % (",".join(kinds), len({m[1] for m in marks}), terms, nonempty[-1][:60] if nonempty else ""))
    return marks


if leg not in ("major", "minor"):
    invalid("the leg is %r: major or minor" % leg)
    finish()
# `kill`: this leg also kills the unit with SIGKILL during the install, so the evidence of that is required (and is not looked for on a leg that did not)
for option in options:
    if option != "kill":
        invalid("the report does not know the option %r (the only one is `kill`)" % option)
kill_expected = "kill" in options
if kill_expected and leg != "minor":
    invalid("the kill -9 injection belongs to a minor leg")

aborted = read("aborted").strip()
if aborted:
    invalid("the run stopped before it was done: %s" % aborted)

rows, markers = [], {}
for line in read("timeline.tsv").splitlines():
    f = line.split("\t")
    try:
        if f and f[0] == "#" and len(f) >= 3:
            markers[f[2]] = float(f[1])
        elif len(f) == 7:
            rows.append(dict(t=float(f[0]), dv=f[1], status=f[2], web=f[3], amfree=f[4], amdocker=f[5],
                             names=set(f[6].split(",")) if f[6] != "-" else set()))
    except ValueError:
        pass
rows.sort(key=lambda r: r["t"])
samples = load_samples("status.jsonl")

# ---- is this run to be trusted -------------------------------------------------------------

before, after = snapshot("before"), snapshot("after")
start_version = read("start-version").strip()
apt = {}
for line in read("apt-docker-ce.txt").splitlines():
    k, _, v = line.partition(" ")
    apt[k] = v.strip()
installed_full, candidate_full = apt.get("installed"), apt.get("candidate")
expected_to = engine(candidate_full)

for need in ("start-version", "apt-docker-ce.txt", "timeline.tsv", "snapshot-before.txt", "snapshot-after.txt", "dpkg-before.tsv", "dpkg-after.tsv", "dependency-path",
             "box-facts", "apt-simulation.txt"):
    if not read(need).strip():
        invalid("%s is missing or empty: the run did not get that far" % need)
# the tools of this system: the apt whose `-s` the core reads, the systemd that rewrites the unit's command line, and the utilities the unit's script calls
facts = {}
for line in read("box-facts").splitlines():
    k_, _, v_ = line.partition(" ")
    facts[k_] = v_.strip()
if read("box-facts").strip():
    for k_ in ("apt", "dpkg", "systemd", "timeout", "date", "sort", "sleep", "grep", "sh"):
        if not facts.get(k_):
            invalid("box-facts has no %s line: the tools of this system were not recorded" % k_)
systemd_major = None
if facts.get("systemd"):
    m_ = re.match(r"^systemd (\d+)\b", facts["systemd"])
    systemd_major = int(m_.group(1)) if m_ else None
    if systemd_major is None:
        invalid("box-facts has a systemd line with no version number (%r): whether this systemd can write the line the journal verdict looks for is not known" % facts["systemd"][:60])
sim = read("apt-simulation.txt")
sim_exit = re.search(r"^# exit (\d+)$", sim, re.M)
if sim.strip() and not sim_exit:
    invalid("apt-simulation.txt has no exit status line: the simulation was not recorded to its end")
# a poll that left no sample (a file that is missing, empty, or holds nothing but lines that are not samples) judges nothing: the verdicts
# that read it would be dropped without a word
if not samples:
    invalid("status.jsonl is missing or holds no status sample: the run was not polled")
# the journal of the unit is read for a line that is not there: a journal that could not be read proves nothing
journals = {}
for jname in ("unit-journal.txt", "unit-journal-end.txt"):
    journals[jname] = [ln for ln in read(jname).splitlines() if ln.strip() and not ln.startswith("-- ")]
    if not journals[jname]:
        invalid("%s is missing or holds no journal line of the unit's: journalctl could not read it, and its silence would prove nothing" % jname)
for m in ("post_start", "terminal", "settled"):
    if m not in markers:
        invalid("marker %s is missing from the timeline: the run did not get that far" % m)
if not rows:
    invalid("the timeline has no samples")
not_running = [n for n in CONTAINERS if before["containers"].get(n, {}).get("state") != "running"]
if before["present"] and not_running:
    invalid("these containers were not running before the update: %s" % ", ".join(not_running))
if before["present"] and start_version and before.get("docker") != start_version:
    invalid("the box did not start on Docker %s (the daemon says %s)" % (start_version, before.get("docker")))
if installed_full and start_version and engine(installed_full) != start_version:
    invalid("dpkg has docker-ce %s, not the %s the leg starts on" % (installed_full, start_version))
# the time dockerd began is a number, `none` (the unit is not active) or `unreadable` (this system's date could not read what systemd gave: the harness's, not dockerd's)
epoch_word = re.search(r"EnterEpoch=(\S+)", after.get("unit docker", ""))
epoch_unreadable = bool(epoch_word) and epoch_word.group(1) == "unreadable"
if epoch_unreadable:
    invalid("snapshot-after.txt: date could not read the time systemd gave for docker.service (EnterEpoch=unreadable), so when dockerd started cannot be set against the download")
if candidate_full and installed_full:
    if leg == "major" and not (major(expected_to) or 0) > (major(start_version) or 0):
        invalid("apt offers docker-ce %s on top of %s: there is no major jump to prove (the last release of a major, or a repository that stopped)" % (candidate_full, installed_full))
    if leg == "minor" and (major(expected_to) != major(start_version) or expected_to == start_version):
        invalid("apt offers docker-ce %s on top of %s: not a same-major update" % (candidate_full, installed_full))
else:
    invalid("apt-docker-ce.txt has no installed or no candidate version")
span = []
if "post_start" in markers and "settled" in markers and rows:
    span = [r for r in rows if markers["post_start"] <= r["t"] <= markers["settled"]]
    holes = [b["t"] - a["t"] for a, b in zip(span, span[1:]) if b["t"] - a["t"] > HOLE]
    if holes or not span:
        invalid("the poller left a hole of %s s in the run" % (", ".join("%.0f" % h for h in holes) or "its whole length"))
    pre = [r for r in rows if r["t"] <= markers["post_start"]]
    if not pre or pre[-1]["dv"] != start_version:
        invalid("docker did not answer with %s when the update was asked for" % start_version)

# ---- the plan the check offered, and what the box did about it -------------------------------------------

code, body = resp("packages-1")
plan_seen = code is not None
up = dobj(body, "docker", "update")
plan = up.get("packages") if isinstance(up.get("packages"), list) else []
entries = [p for p in plan if isinstance(p, dict)]
new_pk = [p for p in entries if p.get("new") is True]       # not installed yet: no current version
up_pk = [p for p in entries if p.get("new") is not True]    # an upgrade
plan_new = {bare(p.get("name")): p.get("candidate_version") for p in new_pk}
plan_up = {bare(p.get("name")): (p.get("current_version"), p.get("candidate_version")) for p in up_pk}

before_pk, after_pk = load_dpkg("dpkg-before.tsv"), load_dpkg("dpkg-after.tsv")
for name_, pk_ in (("dpkg-before.tsv", before_pk), ("dpkg-after.tsv", after_pk)):
    if read(name_).strip() and not pk_:
        invalid("%s lists no installed package: that is not what dpkg-query writes" % name_)
added = sorted(set(after_pk) - set(before_pk))
removed = sorted(set(before_pk) - set(after_pk))
changed = sorted(n for n in set(before_pk) & set(after_pk) if before_pk[n] != after_pk[n])
exercised = bool(plan_new) and set(added) == set(plan_new)

dep = {}   # what the harness did about the dependency path (docker-update-proof.sh, prepare_dependency_path)
for line in read("dependency-path").splitlines():
    k_, _, v_ = line.partition(" ")
    dep[k_] = v_.strip()
if read("dependency-path").strip():
    if dep.get("action") not in ("removed", "skipped", "not-attempted"):
        invalid("dependency-path says %r, which is not a thing the harness does" % dep.get("action"))
    elif leg == "major" and dep["action"] == "not-attempted":
        invalid("the major leg did not try the dependency step (dependency-path says not-attempted)")
    elif dep["action"] == "removed" and "nftables" not in [bare(n) for n in dep.get("packages", "").split()]:
        invalid("dependency-path says packages were removed but does not name nftables: %r" % dep.get("packages"))
    elif dep["action"] == "removed" and "nftables" in before_pk:
        invalid("the harness says it took nftables out, and dpkg-before.tsv still has it installed")

# ---- the box ------------------------------------------------------------------------------------

P("## The box")
P("")
P("| | |")
P("|---|---|")
P("| system | %s |" % (read("os").strip() or "?"))
P("| leg | %s: %s |" % (leg, "a major jump, the owner's case" if leg == "major" else "the previous patch to the current one, then a failure injected"))
P("| docker-ce at the start | %s (daemon %s) |" % (installed_full or "?", before.get("docker", "?")))
P("| docker-ce on offer | %s |" % (candidate_full or "?"))
for key_, label_ in (("apt", "apt"), ("systemd", "systemd"), ("dpkg", "dpkg"), ("sh", "/bin/sh")):
    P("| %s | %s |" % (label_, facts.get(key_, "?")))
P("| the utilities the unit's script calls | %s |" % "; ".join("%s: %s" % (k_, facts.get(k_, "?")) for k_ in ("timeout", "date", "sort", "sleep", "grep")))
P("| packages before | %s |" % before.get("packages", "?"))
P("| packages after | %s |" % after.get("packages", "?"))
P("| new packages the update installed | %s |" % (("; ".join("%s %s" % (n, after_pk[n]) for n in added) or "none (no new dependency was exercised)") if before_pk and after_pk else "?"))
P("")

# ---- the check offers the update -------------------------------------------------------------------

verdict("GET /v1/sys/packages offers the Docker update", on(code is not None, code == 200 and up.get("available") is True and not up.get("refusal")),
        "HTTP %s, available %r, refusal %r" % (code, up.get("available"), up.get("refusal")))
verdict("from and to are the engine versions apt shows", on(code is not None, up.get("from") == start_version and up.get("to") == expected_to),
        "from %r (wanted %r), to %r (wanted %r)" % (up.get("from"), start_version, up.get("to"), expected_to))
verdict("major_jump says what the jump is", on(code is not None, up.get("major_jump") is (leg == "major")), "major_jump %r on a %s leg" % (up.get("major_jump"), leg))
bad_up = [p for p in up_pk if p.get("name") not in ALLOWLIST or not (valid_version(p.get("current_version")) and valid_version(p.get("candidate_version")))]
bad_new = [p for p in new_pk if not valid_name(p.get("name")) or bare(p.get("name")) in DISTRO_DOCKER or p.get("current_version") != ""
           or not valid_version(p.get("candidate_version"))]
fmt = lambda p: "%s %s -> %s" % (p.get("name"), p.get("current_version") or "(new)", p.get("candidate_version"))
verdict("every upgrade in the plan is an engine package with validated versions", on(plan_seen, up_pk and not bad_up and len(entries) == len(plan)),
        "upgrades: %s%s%s" % (", ".join(fmt(p) for p in up_pk) or "none", "; not acceptable: " + ", ".join(fmt(p) for p in bad_up) if bad_up else "",
                              "; %d entries that are not objects" % (len(plan) - len(entries)) if len(entries) != len(plan) else ""))
verdict("every new package in the plan has a valid name and version, is not a distro Docker package, and there are at most %d" % MAX_NEW,
        on(plan_seen, not bad_new and len(new_pk) <= MAX_NEW),
        "%d new package%s: %s%s" % (len(new_pk), "" if len(new_pk) == 1 else "s", ", ".join(fmt(p) for p in new_pk) or "none",
                                    "; not acceptable: " + ", ".join(fmt(p) for p in bad_new) if bad_new else ""))
# apt's own words, in the form the core reads them (dockerpkg's planInstPattern): this system's apt may print them differently from the one the core was written against
sim_inst = {m.group(1): (m.group(2), m.group(3)) for m in re.finditer(r"^Inst (\S+)(?:\s+\[([^\]]*)\])?(?:\s+\((\S+))?", sim, re.M)}
verdict(V_SIM, on(sim.strip() and sim_exit and installed_full and candidate_full, bool(sim_exit) and sim_exit.group(1) == "0" and sim_inst.get("docker-ce") == (installed_full, candidate_full)),
        "exit %s; Inst docker-ce: %s (wanted %s)" % (sim_exit.group(1) if sim_exit else "?", sim_inst.get("docker-ce"), (installed_full, candidate_full)))
dce = next((p for p in plan if isinstance(p, dict) and p.get("name") == "docker-ce"), {})
verdict("the plan's docker-ce is the one dpkg has and the one apt offers",
        on(code is not None, dce.get("current_version") == installed_full and dce.get("candidate_version") == candidate_full),
        "plan %r -> %r, dpkg %r, apt %r" % (dce.get("current_version"), dce.get("candidate_version"), installed_full, candidate_full))
plan_id = up.get("plan_id")
want_id = hashlib.sha256("\n".join(sorted("%s %s>%s" % (p.get("name"), p.get("current_version"), p.get("candidate_version"))
                                          for p in plan if isinstance(p, dict))).encode()).hexdigest()
verdict("plan_id is the sha256 of the sorted `name current>candidate` lines", on(code is not None, plan_id == want_id), "got %r, computed %r" % (plan_id, want_id))
if secs("packages-1") is not None:
    P("The first check took %.1f s (apt-get update and the simulations included)." % secs("packages-1"))
    P("")

# ---- the containers --------------------------------------------------------------------------------

code, body = resp("containers-before")
by = {c.get("name"): c for c in dlist(body, "containers") if isinstance(c, dict)}
wrong = [n for n, pol in CONTAINERS.items() if n not in by or by[n].get("restart_policy") != pol]
verdict("GET /v1/sys/docker/containers lists the running containers with their restart policy",
        on(code is not None, code == 200 and dget(body, "running") is True and not wrong),
        "HTTP %s, running %r, wrong or missing: %s" % (code, dget(body, "running"), ", ".join(wrong) or "none"))
ports = by.get("p-web", {}).get("ports") if isinstance(by.get("p-web", {}).get("ports"), list) else []
verdict("the published port and the host network are reported",
        on(code is not None, any(isinstance(p, dict) and p.get("port") == 80 and p.get("protocol") == "tcp" and p.get("host_port") == 18081 for p in ports)
           and by.get("p-host", {}).get("host_network") is True and by.get("p-web", {}).get("host_network") is not True),
        "p-web ports %r, p-host host_network %r, p-web host_network %r" % (ports, by.get("p-host", {}).get("host_network"), by.get("p-web", {}).get("host_network")))
code, body = resp("status-idle")
verdict("the status before any run is idle and supported", on(code is not None, code == 200 and dget(body, "state") == "idle" and dget(body, "supported") is True),
        "HTTP %s, state %r, supported %r" % (code, dget(body, "state"), dget(body, "supported")))


# ---- refusals: nothing of the engine changes ------------------------------------------------------------

def refused(label, name, codes):
    code, body = resp(name)
    d = dobj(body)
    verdict(label, on(code is not None, code == 409 and d.get("error_code") in codes and bool(d.get("error")) and body.get("message") == d.get("error")),
            "HTTP %s, error_code %r (wanted %s), error %r, message %r" % (code, d.get("error_code"), " or ".join(codes), d.get("error"), body.get("message")))


code, body = resp("packages-held")
hu = dobj(body, "docker", "update")
verdict("a held docker-ce is refused by the check with `held`, with no button", on(code is not None, code == 200 and hu.get("refusal") == "held" and hu.get("available") is False),
        "HTTP %s, refusal %r, available %r" % (code, hu.get("refusal"), hu.get("available")))
refused("POST while docker-ce is held: 409 `held`", "held-post", ("held",))
refused("POST while dpkg is locked: 409 `maintenance`", "lock-post", ("maintenance",))
# the spec files "a generic package update is running" under `running` and under `maintenance`
refused("POST while the generic package update runs: 409 `running` or `maintenance`", "unit-post", ("running", "maintenance"))
refused("POST with a plan_id that is not the plan: 409 `changed`", "wrong-post", ("changed",))
code, _ = resp("bad-post")
verdict("POST with a body that is not a plan_id: 400", on(code is not None, code == 400), "HTTP %s" % code)
code, _ = resp("query-post")
verdict("POST with the token in the query only: 401", on(code is not None, code == 401), "HTTP %s" % code)
code, _ = resp("refresh-post")
verdict("POST with a refresh token: 401", on(code is not None, code == 401), "HTTP %s" % code)
refused("POST with the dashboard's own token in the header reaches the route (wrong plan: 409 `changed`)", "jwt-post", ("changed",))
code, body = resp("packages-2")
up2 = dobj(body, "docker", "update")
verdict("after the hold is lifted the update is offered again, with the same plan", on(code is not None and plan_seen, code == 200 and up2.get("available") is True and up2.get("plan_id") == plan_id),
        "HTTP %s, available %r, plan_id %r (first %r)" % (code, up2.get("available"), up2.get("plan_id"), plan_id))
P("The refusal checks ran on the live box and changed nothing of the engine: docker-ce held and released, dpkg's lock held by another process, "
  "a stand-in transient unit named casaos-package-update.service kept active, and POSTs with a wrong plan_id or a body that is not one.")
P("")

# ---- the run -------------------------------------------------------------------------------------------

code, body = resp("update-post")
verdict("POST /v1/sys/docker/update with the right plan_id starts the run", on(code is not None, code == 200 and dget(body, "state") in ("running", "finalizing")),
        "HTTP %s, state %r" % (code, dget(body, "state")))
t = secs("update-post")
verdict("the POST returns fast (a few seconds), not after apt", on(t is not None, t is not None and t < POST_SECONDS), "%s s" % ("%.1f" % t if t is not None else "?"))
refused("a second POST while the unit runs: 409 `running`", "running-post", ("running",))

verdict("the run reaches a terminal state", on(samples, any(d and d.get("state") in TERMINAL for _, _, d in samples)),
        "%d status samples%s" % (len(samples), "; gave up after " + read("status.timeout").strip() if read("status.timeout").strip() else ""))
seen = []
for _, _, d in samples:
    s = d.get("state") if d else None
    if s and (not seen or seen[-1] != s):
        seen.append(s)
P("States the status endpoint went through: %s." % (" -> ".join(seen) or "none"))
P("")
first_answer = next((d for _, h, d in samples if h == "200" and d), None)
verdict("the status right after the POST says running or finalizing", on(samples, bool(first_answer) and first_answer.get("state") in ("running", "finalizing")),
        "first answer: %r" % (first_answer or {}).get("state"))
verdict("the status never goes back to idle during the run", on(samples, "idle" not in seen), "states: %s" % ", ".join(seen))
verdict("the status endpoint keeps answering while Docker restarts (no %d failed samples in a row)" % FAIL_RUN, on(samples, not status_runs(samples)),
        "%d samples, %d failed" % (len(samples), sum(1 for _, h, _ in samples if h != "200")))

try:
    final = json.loads(read("status-final.json") or "null")
except ValueError:
    final = None
fd = final.get("data") if isinstance(final, dict) and isinstance(final.get("data"), dict) else {}
if not fd:
    invalid("status-final.json holds no status: the last answer was not recorded")
verdict("the run succeeded", on(fd, fd.get("state") == "succeeded" and fd.get("outcome") == "success" and not fd.get("error") and not fd.get("error_code")),
        "state %r, outcome %r, error_code %r, error %r" % (fd.get("state"), fd.get("outcome"), fd.get("error_code"), fd.get("error")))
verdict("the status says from and to", on(fd, fd.get("from") == start_version and fd.get("to") == expected_to),
        "from %r (wanted %r), to %r (wanted %r)" % (fd.get("from"), start_version, fd.get("to"), expected_to))
nr_pairs = sorted((c.get("name"), c.get("restart_policy")) for c in (fd.get("not_returned") if isinstance(fd.get("not_returned"), list) else []) if isinstance(c, dict))
verdict("not_returned lists the container with restart policy `no`, and only it", on(fd, nr_pairs == [("p-no", "no")]), "listed: %r" % nr_pairs)
verdict("there is no rollback command after a success", on(fd, not fd.get("rollback_command")), "rollback_command %r" % fd.get("rollback_command"))
st, en = timestamp(fd.get("started_at") or ""), timestamp(fd.get("completed_at") or "")
verdict("started_at and completed_at are timestamps, in order", on(fd, st is not None and en is not None and en >= st),
        "started_at %r, completed_at %r" % (fd.get("started_at"), fd.get("completed_at")))
log = fd.get("log") if isinstance(fd.get("log"), str) else ""
verdict("the status shows the log, up to its terminal marker", on(fd, re.search(r"^CASAOS_DOCKER_UPDATE_SUCCESS ", log, re.M)), "%d bytes of log" % len(log))

log_text = read("docker-update.log")
if not log_text.strip():
    invalid("docker-update.log was not collected")
marks = check_log(log_text, "success", "SUCCESS", ["QUEUED", "STARTED", "PREVIOUS", "DOWNLOADED", "INSTALLED", "DAEMON"]) if log_text.strip() else []
by_kind = {m[0]: m for m in marks}
field = lambda kind: by_kind.get(kind, (None, None, ""))[2]
prev_field = next((m[2] for m in marks if m[0] == "PREVIOUS"), "")   # the first one, as the core reads it
verdict("the PREVIOUS marker records the docker-ce that was installed", on(marks, ("docker-ce=%s" % installed_full) in prev_field.split()), "PREVIOUS: %s" % prev_field)
# what the rollback command is made of: a PREVIOUS that systemd emptied (the script's ${Package}=${Version} gone) gives a command that goes nowhere
prev_words = prev_field.split()
want_pins = sorted("%s=%s" % (n, cur) for n, (cur, _) in plan_up.items())
lost_pins = [p for p in want_pins if p not in prev_words]
odd_words = [w for w in prev_words if not PIN_RE.match(w)]
verdict(V_PINS, on(marks and plan_up, not lost_pins and not odd_words),   # an empty PREVIOUS misses every pin of the plan
        "PREVIOUS: %r; missing: %s; not pins: %s" % (prev_field, ", ".join(lost_pins) or "none", ", ".join(odd_words) or "none"))
bad_journal = ["%s: %s" % (jname, ln.strip()) for jname, jlines in sorted(journals.items()) for ln in jlines if EMPTY_ENV_RE.search(ln)]
if all(journals.values()):
    if bad_journal:      # a line that is there counts on every system
        verdict(V_JOURNAL, False, "; ".join(bad_journal))
    elif systemd_major is not None and systemd_major >= JOURNAL_FROM:
        verdict(V_JOURNAL, True, "systemd %d; %d lines read" % (systemd_major, sum(len(v) for v in journals.values())))
    elif systemd_major is not None:
        not_applicable(V_JOURNAL, "systemd %d is older than %d and writes no such line, whatever the command line holds: a journal without it proves nothing here, and the "
                                  "verdict on the pins of the PREVIOUS marker is what catches an emptied variable on this system" % (systemd_major, JOURNAL_FROM))
verdict("the DAEMON marker records the running version", on(marks, field("DAEMON").strip() == expected_to), "DAEMON: %r (wanted %r)" % (field("DAEMON"), expected_to))
logged_nr = sorted(m[2].split()[0] for m in marks if m[0] == "NOTRETURNED" and m[2].split())
verdict("the NOTRETURNED markers are the containers the status lists", on(marks and fd, logged_nr == [n for n, _ in nr_pairs]), "log %r, status %r" % (logged_nr, [n for n, _ in nr_pairs]))

# ---- what the box says afterwards -------------------------------------------------------------------------

at_settled = [r for r in rows if r["t"] <= markers.get("settled", 0)]
verdict("the running Docker is the new version", on(after["present"] and at_settled, after.get("docker") == expected_to and bool(at_settled) and at_settled[-1]["dv"] == expected_to),
        "daemon %r, the poller at the end %r, wanted %r" % (after.get("docker"), at_settled[-1]["dv"] if at_settled else None, expected_to))
verdict("dpkg has the version apt offered", on(after["present"], package_version(after, "docker-ce") == candidate_full),
        "docker-ce %r (wanted %r)" % (package_version(after, "docker-ce"), candidate_full))
have_dpkg = plan_seen and bool(before_pk) and bool(after_pk)
verdict("the run removed no package", on(before_pk and after_pk, not removed), "removed: %s" % (", ".join(removed) or "none"))
stray = sorted((set(added) | set(changed)) - set(plan_new) - set(plan_up))
verdict("nothing outside the plan was installed or upgraded", on(have_dpkg, not stray), "outside the plan: %s" % (", ".join(stray) or "none"))
wrong_new = sorted(n for n, k in plan_new.items() if after_pk.get(n) != k)
verdict("dpkg added exactly the plan's new packages, at the planned versions", on(have_dpkg, set(added) == set(plan_new) and not wrong_new),
        "plan: %s; dpkg added: %s" % (", ".join("%s %s" % kv for kv in sorted(plan_new.items())) or "none", ", ".join("%s %s" % (n, after_pk[n]) for n in added) or "none"))
wrong_up = sorted(n for n, (cur, cand) in plan_up.items() if before_pk.get(n) != cur or after_pk.get(n) != cand)
verdict("dpkg upgraded exactly the plan's upgrades, from and to the planned versions", on(have_dpkg, set(changed) == set(plan_up) and not wrong_up),
        "plan: %s; dpkg changed: %s" % (", ".join("%s %s>%s" % (n, c, k) for n, (c, k) in sorted(plan_up.items())) or "none",
                                         ", ".join("%s %s>%s" % (n, before_pk[n], after_pk[n]) for n in changed) or "none"))
pb, pa = unit_number(before, "docker", "MainPID"), unit_number(after, "docker", "MainPID")
dl_fields = field("DOWNLOADED").split()
dl = timestamp(dl_fields[0]) if dl_fields else None
enter = unit_number(after, "docker", "EnterEpoch")
verdict("the new dockerd is a new process", on(after["present"], pb is not None and pa is not None and pb != pa), "pid %s -> %s" % (pb, pa))
verdict("everything was downloaded before Docker restarted", on(marks and after["present"] and not epoch_unreadable, dl is not None and enter is not None and enter >= dl - 3),
        "DOWNLOADED at %r, dockerd running since %r" % (field("DOWNLOADED"), enter))
back_missing = sorted(n for n, pol in CONTAINERS.items() if pol in COMES_BACK and n not in running(after))
verdict("every container with restart policy always or unless-stopped is running again", on(after["present"], not back_missing), "not running: %s" % (", ".join(back_missing) or "none"))
gone = sorted(running(before) - running(after))
verdict("the containers that did not come back are exactly the ones the status listed", on(after["present"] and fd, gone == [n for n, _ in nr_pairs]),
        "stopped after: %r, status: %r" % (gone, [n for n, _ in nr_pairs]))
for unit, label in (("casaos-app-management", "AppManagement"), ("casaos", "the core"), ("casaos-gateway", "the gateway")):
    pb, pa = unit_number(before, unit, "MainPID"), unit_number(after, unit, "MainPID")
    verdict("%s was not restarted" % label, on(after["present"], pb is not None and pb == pa), "pid %s -> %s" % (pb, pa))
verdict("AppManagement keeps answering on a route that needs no Docker", on(span, not episodes(span, lambda r: r["amfree"] != "200", FAIL_RUN, 1)),
        "%d of %d samples not 200" % (sum(1 for r in span if r["amfree"] != "200"), len(span)))
code, _ = resp("am-compose")
verdict("AppManagement lists its projects again once Docker is back, without a restart", on(code is not None, code == 200), "HTTP %s" % code)
code, _ = resp("web-after")
verdict("the published port answers again", on(code is not None, code == 200), "HTTP %s" % code)
acked, filed, lost = lost_writes("db-logs.txt", "db-file.txt")
db_read = db_evidence("")
verdict("no acknowledged write of the database is lost", on(db_read, bool(acked) and bool(filed) and not lost),
        "%d acknowledged, %d in the file, %d missing (this finds a lost or rolled-back volume, not a power cut)" % (len(acked), len(filed), len(lost)))
code, body = resp("packages-after")
dk, uk = dobj(body, "docker"), dobj(body, "docker", "update")
verdict("the next check shows the new version and nothing to update", on(code is not None, code == 200 and dk.get("version") == expected_to and not uk.get("available")),
        "HTTP %s, docker.version %r, update %r" % (code, dk.get("version"), uk or None))
code, body = resp("containers-after")
after_names = {c.get("name") for c in dlist(body, "containers") if isinstance(c, dict)}
verdict("GET /v1/sys/docker/containers lists the containers that are running again",
        on(code is not None, code == 200 and dget(body, "running") is True and {n for n, p in CONTAINERS.items() if p in COMES_BACK} <= after_names and "p-no" not in after_names),
        "HTTP %s, listed: %s" % (code, ", ".join(sorted(after_names - {None})) or "none"))

# ---- the way back after the major jump: major leg only -----------------------------------------------------------------

rollback_seconds = None
if leg == "major":
    P("## Rolling back")
    P("")
    rb_exit, rb_cmd = read("rollback-exit").strip(), read("rollback-command").strip()
    rb_name = "%s: Docker %s starts again with the containers, the volume's content and the images intact" % (V_ROLLBACK, start_version or "?")
    if "rollback_start" not in markers:
        invalid("marker rollback_start is missing from the timeline: the rollback did not get that far")
    if not rb_exit or not rb_cmd:
        invalid("rollback-exit or rollback-command is missing: the rollback step did not run")
    elif rb_exit == "not-run":
        verdict(rb_name, False, "the PREVIOUS marker carries no pin, so the core could print no rollback command and there is nothing to run")
        P("The PREVIOUS marker held no pin: no rollback command could be made, and none was run.")
    elif not rb_exit.isdigit():
        invalid("rollback-exit says %r, which is not an exit status" % rb_exit)
    else:
        # the command is the one the core prints after a failure, made here of the pins of the PREVIOUS marker: set against the log, so that a
        # harness that built it from anything else is an invalid run, not a rollback that passed
        rb_pins = rb_cmd.split()[4:] if rb_cmd.startswith("sudo apt-get install --allow-downgrades ") else []
        prev_pins = [w for w in prev_field.split() if PIN_RE.match(w)]
        if marks and (not rb_pins or not all(bare(p.split("=")[0]) in ALLOWLIST for p in rb_pins) or sorted(rb_pins) != sorted(prev_pins)):   # prev_pins are pins
            invalid("the rollback command the harness ran (%r) is not the one made of the PREVIOUS pins (%s)" % (rb_cmd, " ".join(prev_pins)))
        if "rollback_settled" not in markers:
            invalid("marker rollback_settled is missing from the timeline: the rollback did not get that far")
        for need in ("snapshot-rollback.txt", "images-before.txt", "images-rollback.txt", "rollback-settle"):   # the database's two files may be empty: that is Docker 28 not reading what Docker 29 wrote
            if not read(need).strip():
                invalid("%s is missing or empty: the rollback did not get that far" % need)
        rb_code, _ = resp("rollback-web")
        rb_snap, rb_settle = snapshot("rollback"), read("rollback-settle").strip()
        images_before, images_after = set(read("images-before.txt").split("\n")) - {""}, set(read("images-rollback.txt").split("\n")) - {""}
        rb_acked, rb_filed, rb_lost = lost_writes("rollback-db-logs.txt", "rollback-db-file.txt")
        db_evidence("rollback-")
        rb_trouble = tool_trouble(int(rb_exit), read("rollback.log")) if rb_exit != "0" else None   # a failure that is apt's or the shell's, not the rollback's
        if rb_trouble:
            invalid("the rollback's apt-get did not get to try the rollback: %s (see rollback.log)" % rb_trouble)
        down = sorted(n for n, pol in CONTAINERS.items() if pol in COMES_BACK and n not in running(rb_snap))
        gone_images = sorted(images_before - images_after)
        why = []
        if rb_exit != "0":
            why.append("apt-get exited %s (see rollback.log)" % rb_exit)
        if rb_snap.get("docker") != start_version:
            why.append("the daemon says %r, not %s" % (rb_snap.get("docker"), start_version))
        if package_version(rb_snap, "docker-ce") != installed_full:
            why.append("dpkg has docker-ce %r, not %s" % (package_version(rb_snap, "docker-ce"), installed_full))
        if down:
            why.append("not running: %s" % ", ".join(down))
        if not rb_settle.isdigit():
            why.append("the containers that start by themselves were not back after 150 s")
        if gone_images:
            why.append("images gone: %s" % ", ".join(gone_images))
        if not rb_acked or not rb_filed:
            why.append("no acknowledged write could be read back (%d in the log, %d in the file)" % (len(rb_acked), len(rb_filed)))
        elif rb_lost:
            why.append("%d acknowledged writes missing from the volume" % len(rb_lost))
        if rb_code != 200:
            why.append("the published port answers HTTP %s" % rb_code)
        trusted = not any(p.startswith(("the rollback command the harness", "the rollback's apt-get", "marker rollback_", "snapshot-rollback", "images-", "rollback-",
                                        "no answer was recorded for rollback-web")) for p in problems)
        verdict(rb_name, on(trusted, not why), "; ".join(why) or "the command %r ran, exit %s; %d images, %d acknowledged writes, the containers back after %s s" %
                (rb_cmd, rb_exit, len(images_after), len(rb_acked), rb_settle))
        if why and trusted:
            P("**The rollback did not hold: %s.** The core must not offer the rollback command after a major jump (hide it when `major_jump` is true, and tell the owner to copy "
              "/var/lib/containerd and /var/lib/docker before the update)." % "; ".join(why))
        elif trusted:
            P("`%s` ran (exit %s; the harness adds -y and --force-confold so that it can run unattended). Docker %s started again, the containers that start by themselves "
              "were back after %s s, %d images were still there, and every one of the %d writes the database acknowledged was on the volume." %
              (rb_cmd, rb_exit, start_version, rb_settle, len(images_after), len(rb_acked)))
        if "rollback_start" in markers and "rollback_settled" in markers:
            rollback_seconds = markers["rollback_settled"] - markers["rollback_start"]
    P("")

# ---- the numbers, for whoever reads this ---------------------------------------------------------------------

P("## What was observed")
P("")
if before_pk and after_pk and plan_seen:
    if exercised:
        P("- The dependency path was exercised: the update installed %d package%s the box did not have, as its plan said (%s)." %
          (len(added), "" if len(added) == 1 else "s", "; ".join("%s %s" % (n, after_pk[n]) for n in added)))
    else:
        why = ("the plan named new packages (%s) and dpkg did not add exactly those, see the verdicts" % ", ".join(sorted(plan_new))) if plan_new else \
            "the plan held no new package" + ("; nftables was already installed before the update" if "nftables" in before_pk else "")
        P("- **No new dependency was exercised**: %s. This run does not prove the part of the update that installs packages the box did not have." % why)
    P("- " + {"removed": "Before the first check the harness took out %s (all that apt would remove was nftables and libraries), so that the update would have packages to bring." %
                        ", ".join(dep.get("packages", "").split()),
              "skipped": "The harness did not take nftables out: %s." % dep.get("reason", "no reason given"),
              "not-attempted": "The harness takes nothing out on this leg: %s." % dep.get("reason", "no reason given")}.get(dep.get("action"), "The harness left no usable record of the dependency step."))
if span:
    eps = episodes(span, lambda r: r["dv"] == "-", 2, 3)
    for s_, e_ in eps:
        P("- dockerd did not answer for %s, from %.0f s after the POST." % ("%.1f s" % (e_ - s_) if e_ else "the rest of the window", s_ - markers["post_start"]))
    if not eps:
        P("- dockerd never failed two samples in a row (a shorter restart can fall between two samples).")
    web_eps = episodes(span, lambda r: r["web"] != "200", 2, 3)
    P("- the published port (nginx, 18081) was silent for %s." % (", ".join("%.1f s" % ((e_ - s_) if e_ else 0) for s_, e_ in web_eps) or "no two samples in a row"))
    versions = []
    for r in span:
        if r["dv"] != "-" and (not versions or versions[-1] != r["dv"]):
            versions.append(r["dv"])
    P("- the daemon's version in the samples: %s." % " -> ".join(versions))
if "post_start" in markers and "terminal" in markers:
    P("- from the POST to the terminal state: %.0f s; the POST itself took %s s." % (markers["terminal"] - markers["post_start"], read("update-post.secs").strip() or "?"))
if "terminal" in markers and "settled" in markers:
    P("- from the terminal state to every container that should be back: %.0f s." % (markers["settled"] - markers["terminal"]))
stamps = re.findall(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z) ack ", read("db-logs.txt"), re.M)
if len(stamps) > 2:
    vals = [timestamp(s_) for s_ in stamps]
    gaps = [b - a for a, b in zip(vals, vals[1:]) if a is not None and b is not None]
    if gaps:
        P("- the database stopped writing for %.1f s at the longest (between two acknowledged writes)." % max(gaps))
P("- the status endpoint was asked %d times and failed %d." % (len(samples), sum(1 for _, h, _ in samples if h != "200")))
if rollback_seconds is not None:
    P("- the rollback, from its command to the containers that start by themselves being back: %.0f s." % rollback_seconds)
P("")

# ---- the failure injection: minor leg only, last ------------------------------------------------------------------

if leg == "minor":
    P("## Failure injection")
    P("")
    P("After the success the box was put back on docker-ce %s, /etc/docker/daemon.json was made invalid so that dockerd cannot start, and the button was pressed again." % start_version)
    P("")
    code, body = resp("fail-packages")
    fu = dobj(body, "docker", "update")
    if code is not None and not (code == 200 and fu.get("available") is True and fu.get("from") == start_version):
        invalid("after the box was put back on %s the check does not offer the update again (HTTP %s, available %r, from %r): the failure injection cannot run" % (start_version, code, fu.get("available"), fu.get("from")))
    code, body = resp("fail-post")
    verdict("failure: the POST starts the run although dockerd will be unable to start", on(code is not None, code == 200 and dget(body, "state") in ("running", "finalizing")),
            "HTTP %s, state %r" % (code, dget(body, "state")))
    fsamples = load_samples("fail-status.jsonl")
    if not fsamples:
        invalid("fail-status.jsonl is missing or holds no status sample: the failure run was not polled")
    verdict("failure: the run reaches a terminal state", on(fsamples, any(d and d.get("state") in TERMINAL for _, _, d in fsamples)),
            "%d status samples%s" % (len(fsamples), "; gave up after " + read("fail-status.timeout").strip() if read("fail-status.timeout").strip() else ""))
    verdict("failure: the status endpoint keeps answering while dockerd cannot start", on(fsamples, not status_runs(fsamples)),
            "%d samples, %d failed" % (len(fsamples), sum(1 for _, h, _ in fsamples if h != "200")))
    try:
        ff = json.loads(read("fail-status-final.json") or "null")
    except ValueError:
        ff = None
    fdd = ff.get("data") if isinstance(ff, dict) and isinstance(ff.get("data"), dict) else {}
    if not fdd:
        invalid("fail-status-final.json holds no status")
    verdict("failure: the run failed with error_code `daemon`", on(fdd, fdd.get("state") == "failed" and fdd.get("outcome") == "failed" and fdd.get("error_code") == "daemon" and fdd.get("error")),
            "state %r, outcome %r, error_code %r, error %r" % (fdd.get("state"), fdd.get("outcome"), fdd.get("error_code"), fdd.get("error")))
    rb = fdd.get("rollback_command") or ""
    verdict("failure: the rollback command is a fixed-shape apt command with validated pins that goes back to the start version", on(fdd, rollback_command_ok(rb)),
            "rollback_command %r" % rb)
    flog = read("docker-update-fail.log")
    if not flog.strip():
        invalid("docker-update-fail.log was not collected")
    fmarks = check_log(flog, "failure", "FAILED", ["QUEUED", "STARTED", "PREVIOUS"]) if flog.strip() else []
    verdict("failure: the FAILED marker names the reason `daemon`", on(fmarks, any(m[0] == "FAILED" and m[2].split()[-1:] == ["daemon"] for m in fmarks)),
            "FAILED markers: %r" % [m[2] for m in fmarks if m[0] == "FAILED"])
    repaired, settle = read("fail-repaired").strip(), read("fail-settle").strip()
    if not repaired or not settle:
        invalid("the repair after the failure injection was not recorded")
    verdict("failure: with the file mended Docker starts again and the containers come back", on(repaired and settle, repaired.startswith("yes") and settle.isdigit()),
            "repair: %r, containers back after: %r" % (repaired, settle))
    P("")

# ---- the kill -9 injection: the one minor leg that does it ---------------------------------------------------------------

HALF_FINISHED = re.compile(r"half configured|half installed|unpacked but not yet configured", re.I)   # what dpkg --audit says of an install that was cut short
if leg == "minor" and not kill_expected and read("kill-hit").strip():
    invalid("kill-hit exists: the guest ran the kill -9 injection and the report was not asked to judge it (give it `kill`)")
if leg == "minor" and kill_expected:
    P("## Failure injection: kill -9 of the unit during the install")
    P("")
    P("Before the failure above, the box was put back on docker-ce %s and the button was pressed again. As soon as dpkg ran a maintainer script of Docker's packages, "
      "the whole unit was killed with SIGKILL: the script, apt-get and dpkg, in the middle of a package." % start_version)
    P("")
    hit = read("kill-hit").strip()
    if not hit:
        invalid("kill-hit is missing: the kill -9 injection did not run")
    elif not hit.startswith("caught "):
        invalid("the kill -9 never caught dpkg in a maintainer script of Docker's packages (%s): the injection proved nothing" % hit)
    else:
        for m_ in ("kill_post", "kill_killed", "kill_terminal"):
            if m_ not in markers:
                invalid("marker %s is missing from the timeline: the kill -9 injection did not get that far" % m_)
        code, body = resp("kill-packages")
        ku = dobj(body, "docker", "update")
        if code is not None and not (code == 200 and ku.get("available") is True and ku.get("from") == start_version):
            invalid("after the box was put back on %s the check does not offer the update again (HTTP %s, available %r, from %r): the kill -9 injection cannot run" %
                    (start_version, code, ku.get("available"), ku.get("from")))
        code, body = resp("kill-post")
        verdict(K_POST, on(code is not None, code == 200 and dget(body, "state") in ("running", "finalizing")), "HTTP %s, state %r" % (code, dget(body, "state")))
        ksamples = load_samples("kill-status.jsonl")
        if not ksamples:
            invalid("kill-status.jsonl is missing or holds no status sample: the killed run was not polled")
        verdict(K_TERMINAL, on(ksamples, any(d and d.get("state") in TERMINAL for _, _, d in ksamples)),
                "%d status samples%s" % (len(ksamples), "; gave up after " + read("kill-status.timeout").strip() if read("kill-status.timeout").strip() else ""))
        verdict(K_ANSWER, on(ksamples, not status_runs(ksamples)), "%d samples, %d failed" % (len(ksamples), sum(1 for _, h, _ in ksamples if h != "200")))
        try:
            kf = json.loads(read("kill-status-final.json") or "null")
        except ValueError:
            kf = None
        kdd = kf.get("data") if isinstance(kf, dict) and isinstance(kf.get("data"), dict) else {}
        if not kdd:
            invalid("kill-status-final.json holds no status")
        ecode = kdd.get("error_code")
        verdict(K_FAILED, on(kdd, kdd.get("state") == "failed" and kdd.get("outcome") == "failed" and ecode in ("no_result", "install") and bool(kdd.get("error"))),
                "state %r, outcome %r, error_code %r, error %r" % (kdd.get("state"), kdd.get("outcome"), ecode, kdd.get("error")))
        klog = read("docker-update-kill.log")
        if not klog.strip():
            invalid("docker-update-kill.log was not collected")
        kmarks = parse_log(klog) if klog.strip() else []
        kkinds = [m[0] for m in kmarks]
        kterms = [(m[0], m[2].split()[-1:]) for m in kmarks if m[0] in ("SUCCESS", "RESTART_PENDING", "FAILED")]
        klines = [ln for ln in klog.splitlines() if ln.strip()]
        # the log is what the status was made of: cut off in the install (no INSTALLED, and no terminal marker unless the script itself wrote FAILED install)
        agree = (len({m[1] for m in kmarks}) == 1 and bool(klines) and klines[0].split(" ")[0] == "CASAOS_DOCKER_UPDATE_QUEUED"
                 and "PREVIOUS" in kkinds and "DOWNLOADED" in kkinds and "INSTALLED" not in kkinds
                 and ((ecode == "no_result" and not kterms) or (ecode == "install" and kterms == [("FAILED", ["install"])])))
        verdict(K_LOG, on(kmarks and kdd, agree), "error_code %r; kinds %s; terminal markers %s" % (ecode, ",".join(kkinds), kterms or "none"))
        krb = kdd.get("rollback_command") or ""
        verdict(K_ROLLBACK, on(kdd, rollback_command_ok(krb)), "rollback_command %r" % krb)
        audit = audit_of("kill-audit.txt")
        verdict(K_AUDIT, on(audit, bool(HALF_FINISHED.search(audit[1])) if audit else False), "dpkg --audit said: %s" % ((audit[1][:300] if audit else "") or "nothing"))
        # the core refuses what dpkg's own journal and `dpkg --audit` call unfinished, before the repair: a check that offers the update there would run apt into the
        # error that the unit reports as a failed download
        code, body = resp("kill-packages-dirty")
        du, code_dirty = dobj(body, "docker", "update"), code
        verdict(K_DIRTY, on(code is not None, code == 200 and du.get("refusal") == "dpkg" and du.get("available") is False),
                "HTTP %s, refusal %r, available %r%s" % (code, du.get("refusal"), du.get("available"), "" if du else " (the check returned no docker.update)"))
        audit1, audit2 = audit_of("kill-audit-1.txt"), audit_of("kill-audit-after.txt")
        r1, r2, back = read("kill-repair-1.exit").strip(), read("kill-repair-2.exit").strip(), read("kill-docker").strip()
        if not (r1.isdigit() and r2.isdigit() and back):
            invalid("the repair after the kill -9 injection was not recorded")
        repair_trouble = None
        if r1.isdigit() and r2.isdigit():
            for step_, status_, log_ in (("dpkg --configure -a", r1, "kill-repair-1.log"), ("apt-get -f install", r2, "kill-repair-2.log")):
                why_ = tool_trouble(int(status_), read(log_))
                if why_ and not repair_trouble:
                    repair_trouble = "%s: %s" % (step_, why_)
        if repair_trouble:
            invalid("the repair after the kill -9 injection could not be tried: %s" % repair_trouble)
        verdict(K_REPAIR, on(audit2 and r1.isdigit() and r2.isdigit() and back and not repair_trouble, bool(audit2) and audit2[1] == "" and back.startswith("yes")),
                "`dpkg --configure -a` exit %s, dpkg --audit then %s; `apt-get -f install` exit %s, dpkg --audit then %s; Docker back: %s" %
                (r1 or "?", (audit1[1][:80] or "nothing") if audit1 else "?", r2 or "?", ((audit2[1][:200] or "nothing") if audit2 else "?"), back or "?"))
        code, body = resp("kill-packages-clean")
        cu = dobj(body, "docker", "update")
        verdict(K_CLEAN, on(code is not None, code == 200 and not cu.get("refusal")),
                "HTTP %s, refusal %r, available %r" % (code, cu.get("refusal"), cu.get("available")))
        P("The kill caught dpkg running `%s` (pid %s)." % (hit.split(" ", 3)[3] if len(hit.split(" ", 3)) > 3 else "?", hit.split(" ", 3)[2] if len(hit.split(" ", 3)) > 2 else "?"))
        P("What the status said: state %s, error_code `%s`, %s." % (kdd.get("state"), ecode, "a rollback command" if krb else "no rollback command"))
        P("The killed unit left dpkg saying: %s" % ((audit[1][:300].replace("\n", " ") if audit else "") or "nothing"))
        if code is not None and code_dirty is not None:
            said = lambda u: "refusal `%s`" % u["refusal"] if u.get("refusal") else "no refusal (the update is %s)" % ("on offer" if u.get("available") else "not on offer")
            P("The next check after the kill said %s; after the repair it said %s." % (said(du), said(cu)))
        if audit1:
            P("The repair the dashboard will name: dpkg --configure -a (exit %s) left dpkg --audit %s, so apt-get -f install was %s." %
              (r1 or "?", "saying nothing" if not audit1[1] else "still unhappy", "not needed" if not audit1[1] else "needed (exit %s)" % (r2 or "?")))
        if "kill_killed" in markers and "kill_terminal" in markers:
            P("The status reached its terminal state %.0f s after the kill." % (markers["kill_terminal"] - markers["kill_killed"]))
        P("")

finish()
