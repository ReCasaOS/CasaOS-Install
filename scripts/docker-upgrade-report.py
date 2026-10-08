#!/usr/bin/env python3
"""Turn what docker-upgrade-measure.sh wrote into numbers, or say the run is invalid.

    docker-upgrade-report.py <out dir> <amd64|arm64> <generic|targeted|major>

Prints a markdown report (and writes it to <out dir>/report.md).

Exit status: 0 for a run that was measured, whatever the numbers say (a threshold
that fails is a finding, not a broken harness), or that says NOT MEASURED for a
reason it states; 1 for a run that cannot be trusted (the upgrade did not happen,
the poller left holes, the box did not start as it should): then there is no verdict
at all, only the reasons.

Thresholds, fixed before anything was measured: dockerd answers again within 60 s
(120 s on arm64); every container with restart policy always or unless-stopped is
running again within 120 s of that; no acknowledged write of the database is missing
from its file; AppManagement is not restarted and keeps answering on a route that
does not need Docker.
"""
import os
import re
import sys

out_dir, arch, mode = sys.argv[1], sys.argv[2], sys.argv[3]
DOCKER_BACK = 120 if arch == "arm64" else 60
CONTAINERS_BACK = 120
EXPECTED = ["m-always", "m-unless-stopped", "m-no", "m-on-failure", "m-db", "m-web", "m-host", "smoke", "smoke2"]
MAX_GAP = 10.0  # seconds between two samples, anywhere: more is a hole in the evidence


def read(name, default=""):
    try:
        with open(os.path.join(out_dir, name), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def finish(text, code):
    try:
        with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass
    print(text)
    sys.exit(code)


# ---- what was written ----------------------------------------------------------------

skipped = read("skipped").strip()
if skipped:
    finish("# Docker upgrade, %s, %s\n\nNOT MEASURED: %s\n" % (mode, arch, skipped), 0)

rows, markers = [], {}
for line in read("timeline.tsv").splitlines():
    f = line.split("\t")
    try:
        if f and f[0] == "#" and len(f) >= 3:
            markers[f[2]] = float(f[1])
        elif len(f) == 7:
            rows.append(dict(t=float(f[0]), unit=f[1], answers=f[2] == "up", amfree=f[3], amdocker=f[4], web=f[5],
                             names=set(f[6].split(",")) if f[6] != "-" else set()))
    except ValueError:
        pass
rows.sort(key=lambda r: r["t"])


def snapshot(name):
    s = read("snapshot-%s.txt" % name)
    info = {"containers": {}, "present": bool(s.strip())}
    for line in s.splitlines():
        m = re.match(r"^/(\S+) policy=(\S*) state=(\S+) started=(\S+) restarts=(\d+)", line)
        if m:
            info["containers"][m.group(1)] = dict(policy=m.group(2), state=m.group(3), started=m.group(4), restarts=int(m.group(5)))
        elif line.startswith(("docker ", "driver ", "live_restore ", "images ", "all_containers ", "packages ", "needrestart ")):
            k, _, v = line.partition(" ")
            info[k] = v.strip()
        elif line.startswith("unit "):
            parts = line.split(" ", 2)
            info["unit " + parts[1]] = parts[2] if len(parts) > 2 else ""
    return info


def unit_field(info, unit, field):
    m = re.search(field + r"=(\S[^=]*?)(?= \w+=|$)", info.get("unit " + unit, ""))
    return m.group(1).strip() if m else "?"


def package_version(info, name):
    m = re.search(r"(?:^| )%s=(\S+)" % re.escape(name), info.get("packages", ""))
    return m.group(1) if m else None


def upstream(v):
    return re.sub(r"-.*", "", re.sub(r"^\d+:", "", v)) if v else None


def major(v):
    m = re.match(r"(\d+)", v or "")
    return int(m.group(1)) if m else None


def pid(info, unit):
    v = unit_field(info, unit, "MainPID")
    return int(v) if v.isdigit() and int(v) > 0 else None


before, after, rolled = snapshot("before"), snapshot("after"), snapshot("rollback")
prev, newest = read("prev").strip(), read("newest").strip()
rc = read("upgrade.rc").strip()

# ---- is this run to be trusted -------------------------------------------------------

problems = []
need = ["upgrade_start", "upgrade_end", "upgrade_settled"] + ([] if mode == "major" else ["rollback_start", "rollback_end", "rollback_settled"])
for m in need:
    if m not in markers:
        problems.append("marker %s is missing: the run did not get that far" % m)
if not before["present"] or not after["present"]:
    problems.append("a snapshot is missing")
missing = [n for n in EXPECTED if before["containers"].get(n, {}).get("state") != "running"]
if before["present"] and missing:
    problems.append("these containers were not running before the upgrade: %s" % ", ".join(missing))
if rc != "0":
    problems.append("the upgrade command exited with status %r" % (rc or "none"))
if not rows:
    problems.append("the timeline has no samples")
elif "upgrade_start" in markers:
    gaps = [(b["t"] - a["t"], a["t"]) for a, b in zip(rows, rows[1:]) if b["t"] - a["t"] > MAX_GAP and a["t"] >= markers["upgrade_start"] - 5]
    if gaps:
        problems.append("the poller left holes: %d gap(s) over %.0f s, the longest %.0f s" % (len(gaps), MAX_GAP, max(g for g, _ in gaps)))
    first = [r for r in rows if r["t"] <= markers["upgrade_start"]]
    if not first or not first[-1]["answers"]:
        problems.append("docker was not answering when the upgrade started")
# the packages really changed
dc_before, dc_after = package_version(before, "docker-ce"), package_version(after, "docker-ce")
if before["present"] and after["present"]:
    if dc_before is None or dc_after is None:
        problems.append("docker-ce is not in the package list of a snapshot")
    elif dc_before == dc_after:
        problems.append("docker-ce was not upgraded (%s before and after)" % dc_after)
    if mode in ("generic", "targeted") and prev and newest and dc_before is not None and dc_after is not None:
        if dc_before != prev or dc_after != newest:
            problems.append("docker-ce went %s -> %s, not %s -> %s as set up" % (dc_before, dc_after, prev, newest))
    if mode == "major" and dc_before is not None and dc_after is not None and not (major(upstream(dc_after)) or 0) > (major(upstream(dc_before)) or 0):
        problems.append("the in-place upgrade did not go to a higher major (%s -> %s)" % (dc_before, dc_after))
if mode == "generic":
    sim = read("simulation.txt")
    if not re.search(r"^Inst (docker|containerd)", sim, re.M):
        problems.append("the dashboard's upgrade simulation lists no Docker package: the box was not behind")
if mode != "major" and "rollback_start" in markers:
    if read("rollback.rc").strip() == "":
        problems.append("the rollback did not report an exit status")

if problems:
    finish("# Docker upgrade, %s, %s: INVALID RUN\n\nNo verdict: nothing below can be trusted.\n\n%s\n" % (mode, arch, "\n".join("- " + p for p in problems)), 1)

# ---- episodes ----------------------------------------------------------------------


def episodes(win, is_down, need_down=2, need_up=3):
    """runs of at least need_down consecutive down samples, each ended by the first of need_up consecutive up samples;
    returns [(start, end or None)]"""
    eps, i, n = [], 0, len(win)
    while i < n:
        if is_down(win[i]):
            j = i
            while j < n and is_down(win[j]):
                j += 1
            if j - i >= need_down or j == n:
                # an episode; it ends at the first of need_up consecutive ups
                k = j
                end = None
                while k < n:
                    if not is_down(win[k]) and all(not is_down(w) for w in win[k:k + need_up]) and len(win[k:k + need_up]) == need_up:
                        end = win[k]["t"]
                        break
                    k += 1
                eps.append((win[i]["t"], end))
                # resume after the episode's end
                i = next((idx for idx in range(j, n) if win[idx]["t"] >= (end if end is not None else 1e18)), n)
                continue
            i = j
        else:
            i += 1
    return eps


lines = []
P = lines.append
verdicts = []


def verdict(name, status, detail):
    verdicts.append((name, status))
    P("- **%s** %s: %s" % (status, name, detail))


def docker_down(r):
    return (not r["answers"]) or r["unit"] != "active"


def web_down(r):
    return r["web"] != "200"


def window(a, b):
    return [r for r in rows if markers[a] <= r["t"] <= markers[b]]


P("# Docker upgrade on a ReCasaOS box: %s, %s" % (mode, arch))
P("")
P("| | before | after |")
P("|---|---|---|")
P("| docker (running engine) | %s | %s |" % (before.get("docker", "?"), after.get("docker", "?")))
P("| docker-ce package | %s | %s |" % (dc_before, dc_after))
P("| containerd.io package | %s | %s |" % (package_version(before, "containerd.io"), package_version(after, "containerd.io")))
P("| storage driver | %s | %s |" % (before.get("driver", "?"), after.get("driver", "?")))
P("| live-restore | %s | %s |" % (before.get("live_restore", "?"), after.get("live_restore", "?")))
P("| images / containers | %s / %s | %s / %s |" % (before.get("images", "?"), before.get("all_containers", "?"), after.get("images", "?"), after.get("all_containers", "?")))
P("| dockerd pid | %s | %s |" % (pid(before, "docker"), pid(after, "docker")))
P("| containerd pid | %s | %s |" % (pid(before, "containerd"), pid(after, "containerd")))
P("| app-management pid | %s | %s |" % (pid(before, "casaos-app-management"), pid(after, "casaos-app-management")))
P("| casaos (core) pid | %s | %s |" % (pid(before, "casaos"), pid(after, "casaos")))
P("| needrestart | %s | %s |" % (before.get("needrestart", "?"), after.get("needrestart", "?")))
P("")
P("The poller samples about once a second; every probe has a timeout of 1 to 2 s, so an edge is known to about +-3 s. "
  "The test containers run with `--init`: a daemon stop waits for none of them. A run is judged on the longest episode of at least two consecutive failed samples, ended by three good ones.")
P("")

if mode == "generic":
    sim = read("simulation.txt")
    inst = [l for l in sim.splitlines() if l.startswith("Inst ")]
    fam = [l for l in inst if re.match(r"Inst (docker|containerd)", l)]
    kept = re.search(r"The following packages have been kept back:\n((?:  .*\n)+)", sim)
    P("## What the dashboard's System packages update does")
    P("")
    P("It upgraded %d packages, **%d of them Docker's**: %s." % (len(inst), len(fam), "; ".join(re.sub(r" \(.*", "", l) for l in fam)))
    P("Kept back by --no-remove: %s." % (" ".join(kept.group(1).split()) if kept else "nothing"))
    nr = [l for l in read("needrestart.txt").splitlines() if l.strip()]
    P("needrestart said, in the journal during the upgrade: %s." % ("%d line(s), the first: `%s`" % (len(nr), nr[0][:160]) if nr else "nothing"))
    P("")


def judge(label, start, end, base, strict_label):
    win = window(start, end)
    eps = episodes(win, docker_down)
    P("### %s" % label)
    P("")
    if not eps:
        dockerd_moved = pid(base, "docker") is not None and pid(rolled if label == "rollback" else after, "docker") != pid(base, "docker")
        P("dockerd never failed two samples in a row (%d samples)%s." % (len(win), "; its pid did change, so it was away for less than the poller can see" if dockerd_moved else "; its pid did not change either: it was not restarted"))
        longest, d_end = 0.0, win[0]["t"] if win else markers[start]
        if not dockerd_moved and label == "upgrade":
            verdict("the upgrade restarts dockerd", "FINDING", "it did not: the new package is installed and the old daemon still runs")
    else:
        durs = [(e - s) if e is not None else None for s, e in eps]
        if any(d is None for d in durs):
            verdict("%s: dockerd comes back" % label, "FAIL", "it did not answer again before the window ended")
            P("")
            return
        longest = max(durs)
        d_end = eps[-1][1]
        P("%d episode(s) of dockerd not answering; the first began %.0f s after the command; total %.1f s, longest %.1f s." % (
            len(eps), eps[0][0] - markers[start], sum(durs), longest))
        verdict("%s: dockerd back within %d s" % (label, DOCKER_BACK), "PASS" if longest <= DOCKER_BACK else "FAIL", "longest outage %.1f s" % longest)
    web_eps = episodes(win, web_down)
    if web_eps:
        wdurs = [(e - s) if e is not None else None for s, e in web_eps]
        P("The published port (nginx, 18081) did not answer for %s in %d episode(s)." % ("the rest of the window" if None in wdurs else "%.1f s in total, longest %.1f s" % (sum(wdurs), max(wdurs)), len(web_eps)))
    else:
        P("The published port answered throughout.")
    P("")
    P("| container | restart policy | running again after dockerd |")
    P("|---|---|---|")
    ok_all = True
    for name in EXPECTED:
        pol = base["containers"].get(name, {}).get("policy", "?")
        if name not in base["containers"] or base["containers"][name]["state"] != "running":
            continue
        later = [r for r in win if r["t"] >= d_end]
        if not eps:
            seen = all(name in r["names"] for r in later) if later else False
            back = 0.0 if seen else None
        else:
            idx = next((i for i, r in enumerate(later) if name in r["names"] and all(name in x["names"] for x in later[i:i + 2])), None)
            back = None if idx is None else later[idx]["t"] - d_end
        P("| %s | %s | %s |" % (name, pol, "never" if back is None else "%.0f s" % back))
        if pol in ("always", "unless-stopped") and (back is None or back > CONTAINERS_BACK):
            ok_all = False
    verdict("%s: always/unless-stopped containers back within %d s" % (label, CONTAINERS_BACK), "PASS" if ok_all else "FAIL", "see the table")
    P("")
    bad = [r for r in win if r["amfree"] != "200"]
    badruns = episodes(win, lambda r: r["amfree"] != "200", need_down=3, need_up=2)
    P("AppManagement on a route that needs no Docker (categories) answered something other than 200 in %d of %d samples; on the route that lists projects (needs Docker): %d." % (
        len(bad), len(win), sum(1 for r in win if r["amdocker"] != "200")))
    verdict("%s: AppManagement keeps answering without Docker" % label, "PASS" if not badruns else "FAIL", "%d run(s) of 3+ failed samples" % len(badruns))
    P("")


P("## The daemon and what depends on it")
P("")
judge("upgrade", "upgrade_start", "upgrade_settled", before, "")
if mode != "major":
    judge("rollback", "rollback_start", "rollback_settled", after, "")
    rb_rc = read("rollback.rc").strip()
    rb_ce = package_version(rolled, "docker-ce")
    verdict("the rollback puts the previous docker-ce back", "PASS" if rb_rc == "0" and rb_ce == prev else "FAIL",
            "exit %s, docker-ce %s (wanted %s); containerd.io %s, which the rollback does not touch" % (rb_rc, rb_ce, prev, package_version(rolled, "containerd.io")))

pb, pa = pid(before, "casaos-app-management"), pid(after, "casaos-app-management")
verdict("AppManagement not restarted by the upgrade", "PASS" if pb is not None and pb == pa else "FAIL", "pid %s -> %s" % (pb, pa))

# the database: every acknowledged id is in the file
acked = re.findall(r"^\S+ ack (\S+)$", read("db-logs.txt"), re.M)
filed = set(read("db-file.txt").split())
if not acked or not filed:
    verdict("no acknowledged write lost", "FAIL", "the database's log or file could not be read (%d acknowledged, %d in the file)" % (len(acked), len(filed)))
else:
    lost = [a for a in acked if a not in filed]
    verdict("no acknowledged write lost", "PASS" if not lost else "FAIL", "%d acknowledged, %d in the file, %d missing (this finds a lost or rolled-back volume, not a power cut)" % (len(acked), len(filed), len(lost)))
    stamps = re.findall(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?)Z ack ", read("db-logs.txt"), re.M)
    if len(stamps) > 2:
        from datetime import datetime

        def ts(s):
            base, _, frac = s.partition(".")
            return datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").timestamp() + float("0." + frac[:6] if frac else 0)

        vals = [ts(s) for s in stamps]
        gaps = [b - a for a, b in zip(vals, vals[1:])]
        P("")
        P("The database stopped writing for %.1f s at the longest (between two acknowledged writes)." % max(gaps))

if mode == "major":
    ok = before.get("driver") == after.get("driver") and before.get("images") == after.get("images") and before.get("all_containers") == after.get("all_containers")
    verdict("an in-place upgrade keeps the storage driver, the images and the containers", "PASS" if ok else "FAIL",
            "driver %s -> %s, images %s -> %s, containers %s -> %s" % (before.get("driver"), after.get("driver"), before.get("images"), after.get("images"), before.get("all_containers"), after.get("all_containers")))
    bm = major(upstream(dc_before))
    if bm != 28:
        P("")
        P("Note: the image's Docker was %s, not 28." % dc_before)

P("")
P("## Restart policies on this box")
P("")
for name in EXPECTED:
    if name in before["containers"]:
        P("- %s: `%s`" % (name, before["containers"][name]["policy"]))
P("")
alerts = [l for l in read("alerts.txt").splitlines() if l.strip()]
P("## Alerts and container events in the journal since the upgrade began: %d lines" % len(alerts))
P("")
for l in alerts[-12:]:
    P("    " + l[:200])
P("")
P("## Verdict against the thresholds fixed beforehand")
P("")
passed = sum(1 for _, s in verdicts if s == "PASS")
failed = [n for n, s in verdicts if s == "FAIL"]
findings = [n for n, s in verdicts if s == "FINDING"]
P("%d of %d verdicts pass%s%s." % (passed, len(verdicts), "; failing: " + "; ".join(failed) if failed else "", "; findings: " + "; ".join(findings) if findings else ""))
finish("\n".join(lines) + "\n", 0)
