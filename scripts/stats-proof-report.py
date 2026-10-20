#!/usr/bin/env python3
"""Turn what stats-proof.sh recorded into verdicts, one line a case.

    stats-proof-report.py <out dir> <d12|u24>
    stats-proof-report.py helper <predicate> <file> <http code> [words]   (what the runner asks while it polls)

Each line is `PASS|FAIL|INVALID RUN|UNVERIFIED <id> <reason>`; the lines are also written to <out dir>/report.txt. Exit status 0 only when no line is a FAIL or an INVALID RUN. A FAIL is
a service that did not do what the contract says; an INVALID RUN is a harness that could not set up or observe (the run stopped early, a container never started, a file of evidence is
missing), never a silent skip; UNVERIFIED is what this guest cannot make observable, said with the reason, and it fails nothing.

The cases (stats-proof.sh says how each is made):
  H1 the core's routes: the list, the series of one hour and live, bad_range, unknown_item, 401 without a token
  H2 the core's history files: a fixed size, modes 0640 and 0750
  H3 the core stopped and started: the earlier minutes stay, the minutes it was away are null, never 0
  H4 the host's alert rules (defaults, bounds, a PUT), cpu_high fires under a hog and resolves after it; H4-hub the hub's category resources, on by default
  H5 A6 V6 the CPU of casaos, casaos-app-management and casaos-virt-management over 3 minutes (FAIL only above 2 % of one core)
  A1 a hand-launched busy container; A2 a compose project (cpus: 0.5, no limit); A3 stop, SIGTERM, kill: stopped, the alert resolved; A3-rm a removed container's history by id
  A4 Docker stopped and started: 503 or the last items, a null gap, no spike, the list back within 60 s; A5 AppManagement's files and a restart
  V1 the VM of libvirt's test driver; V2 the guest restarts; V3 a driver with no domain; V4 libvirtd restarts; V5 the VM's alert rules; V5-crash vm_crashed (UNVERIFIED by design)
  C1 what reached the hub's channel; C2 the restarts and panics of the units over the run
"""
import json
import math
import os
import re
import sys
from datetime import datetime

COST_FAIL = 2.0          # percent of one core: above it a service's sampling is a FAIL (generous: the whole process is measured, not the sampler alone)
COST_BUDGET = 0.5        # the design budget of each sampler, which this proof does not isolate
DEFAULT_RULES = {"cpu_high": (True, 90, 10), "mem_high": (True, 90, 5)}
HOST_SERIES = ("cpu/total", "cpu/vcpus", "memory/rss", "memory/cache", "memory/swap", "memory/limit")
HOST_FORBIDDEN_CHARTS = ("restarts", "health", "pids")
HOST_FORBIDDEN_KEYS = ("cpu/user", "cpu/system", "cpu/iowait", "cpu/throttled")
CASAOS_UNITS = ("casaos.service", "casaos-gateway.service", "casaos-user-service.service", "casaos-message-bus.service", "casaos-local-storage.service",
                "casaos-app-management.service", "casaos-virt-management.service")
EMPTY_DRIVER = "test:///usr/local/lib/sp/empty.xml"


class Missing(Exception):
    """the evidence a verdict needs is not there: INVALID RUN"""


# ---- reading what was recorded -------------------------------------------------------------------------------------------------------------


class Evidence:
    def __init__(self, out):
        self.out = out

    def path(self, name):
        return os.path.join(self.out, name)

    def has(self, name):
        return os.path.exists(self.path(name))

    def text(self, name):
        try:
            with open(self.path(name), encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            raise Missing("no %s" % name)

    def maybe(self, name):
        try:
            return self.text(name)
        except Missing:
            return None

    def num(self, name):
        try:
            return float(self.text(name).strip())
        except ValueError:
            raise Missing("%s is not a number" % name)

    def call(self, name):
        """(HTTP status, body or None) of a call through the gateway"""
        code = self.text(name + ".code").strip()
        try:
            code = int(code)
        except ValueError:
            raise Missing("%s.code is not a status" % name)
        raw = self.maybe(name + ".json")
        body = None
        if raw is not None and raw.strip():
            try:
                body = json.loads(raw)
            except ValueError:
                body = None
        return code, body

    def gx(self, name):
        """(exit status, standard output) of a verb of the guest"""
        rc = self.text(name + ".rc").strip()
        return (int(rc) if rc.lstrip("-").isdigit() else 255), self.maybe(name + ".out") or ""

    def tsv(self, name):
        rows = []
        for line in self.text(name + ".tsv").splitlines():
            cells = line.split("\t")
            if len(cells) < 4:
                continue
            try:
                rows.append({"t0": float(cells[0]), "t1": float(cells[1]), "code": cells[2], "yes": cells[3] == "yes", "seen": "\t".join(cells[4:])})
            except ValueError:
                continue
        return rows

    def t(self, name, which="t1"):
        return self.num("%s.%s" % (name, which))


def stamps(out):
    """the `stamp <name> <epoch>` lines a verb of the guest printed (the guest's own clock)"""
    found = {}
    for line in out.splitlines():
        cells = line.split("\t")
        if len(cells) == 3 and cells[0] == "stamp":
            try:
                found[cells[1]] = float(cells[2])
            except ValueError:
                pass
    return found


def kv(out):
    found = {}
    for line in out.splitlines():
        cells = line.split("\t")
        if len(cells) >= 2:
            found.setdefault(cells[0], cells[1])
    return found


def core_data(body):
    """the core's envelope {success, message, data}: data, or None when the body is not one"""
    if isinstance(body, dict) and "data" in body and "message" in body:
        return body["data"]
    return None


def bare(body):
    """a bare body of AppManagement or the virt module: the body itself, never wrapped in data"""
    if isinstance(body, dict) and not ("data" in body and "success" in body):
        return body
    return None


def message(body):
    return body.get("message") if isinstance(body, dict) else None


def series_of(result):
    out = {}
    for s in (result or {}).get("series") or []:
        if isinstance(s, dict) and isinstance(s.get("chart"), str) and isinstance(s.get("name"), str):
            out[s["chart"] + "/" + s["name"]] = s.get("points") or []
    return out


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def bad_points(result):
    """the series whose points are not [t, avg, min, max] with numbers or nulls"""
    bad = []
    for key, points in series_of(result).items():
        for p in points:
            if not (isinstance(p, list) and len(p) == 4 and isinstance(p[0], int) and all(v is None or is_num(v) for v in p[1:])):
                bad.append(key)
                break
    return bad


def avgs(points):
    return {p[0]: p[1] for p in points if isinstance(p, list) and len(p) == 4}


def cpu_sum(result, field=1):
    """per minute, cpu/user + cpu/system of a container (field 1 avg, 3 max), where cpu/user is measured"""
    s = series_of(result)
    user = {p[0]: p[field] for p in s.get("cpu/user", []) if isinstance(p, list) and len(p) == 4}
    system = {p[0]: p[field] for p in s.get("cpu/system", []) if isinstance(p, list) and len(p) == 4}
    return {t: v + (system.get(t) or 0) for t, v in user.items() if is_num(v)}


def items_of(lst):
    return [i for i in (lst or {}).get("items") or [] if isinstance(i, dict)]


def item(lst, item_id):
    for i in items_of(lst):
        if i.get("id") == item_id:
            return i
    return None


def rules_of(alerts):
    return {r.get("key"): (r.get("enabled"), r.get("threshold"), r.get("minutes")) for r in (alerts or {}).get("rules") or [] if isinstance(r, dict)}


def active_of(alerts):
    return [a for a in (alerts or {}).get("active") or [] if isinstance(a, dict)]


def rfc3339(s):
    if not isinstance(s, str):
        return False
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def first_yes(rows):
    for r in rows:
        if r["yes"]:
            return r
    return None


def ring_ls(out):
    """(dir mode or None, {name: (size, mode, type)}); None for no directory at all"""
    if out.strip() == "nodir":
        return None, {}
    mode, files = None, {}
    for line in out.splitlines():
        c = line.split("\t")
        if c[0] == "dir" and len(c) >= 2:
            mode = c[1]
        elif c[0] == "file" and len(c) >= 6:
            files[c[1]] = (int(c[2]), c[3], c[5])
    return mode, files


def full_minutes(start, end):
    """the minutes [m, m + 60) that lie whole inside [start, end]"""
    m = int(math.ceil(start / 60.0)) * 60
    out = []
    while m + 60 <= end:
        out.append(m)
        m += 60
    return out


def kept(pre, post, key):
    """the minutes of key that held a value before and lack one after (only those still inside the window of the later answer)"""
    before = avgs(series_of(pre).get(key, []))
    after = avgs(series_of(post).get(key, []))
    start = (post or {}).get("from", 0)
    return [t for t, v in before.items() if v is not None and t >= start and after.get(t) is None]


# ---- the verdicts --------------------------------------------------------------------------------------------------------------------------


class Case:
    def __init__(self):
        self.fails, self.notes = [], []

    def need(self, ok, msg):
        if not ok:
            self.fails.append(msg)
        return ok


def expect_error(c, ev, name, status, code):
    st, body = ev.call(name)
    c.need(st == status and message(body) == code, "%s: %s %r, expected %d %s" % (name, st, message(body), status, code))


def check_rules(c, name, data, want):
    got = rules_of(data)
    for key, (enabled, threshold, minutes) in want.items():
        r = got.get(key)
        c.need(r is not None and r[0] is enabled and r[1] == threshold and r[2] == minutes, "%s: rule %s is %r, expected %r" % (name, key, r, (enabled, threshold, minutes)))


def j_h1(ev):
    c = Case()
    st, body = ev.call("h1-list")
    d = core_data(body)
    c.need(st == 200 and d is not None, "h1-list: %s, no envelope {success, message, data}" % st)
    host = item(d, "host")
    c.need(host is not None and host.get("kind") == "host", "the list has no item host of kind host")
    c.need(isinstance((d or {}).get("notes"), list), "the list has no notes list")
    st, _ = ev.call("h1-noauth")
    c.need(st == 401, "without a token: %s, expected 401" % st)
    st, body = ev.call("h1-1h")
    d = core_data(body) or {}
    c.need(st == 200 and d.get("range") == "1h" and d.get("step_seconds") == 60, "range=1h: %s, range %r, step %r" % (st, d.get("range"), d.get("step_seconds")))
    s = series_of(d)
    for key in HOST_SERIES:
        c.need(key in s, "range=1h has no %s" % key)
    for prefix in ("network/rx:", "network/tx:", "disk/", "fs/free_percent:"):
        c.need(any(k.startswith(prefix) for k in s), "range=1h has no %s* series" % prefix)
    bad = [k for k in s if k.split("/")[0] in HOST_FORBIDDEN_CHARTS or k in HOST_FORBIDDEN_KEYS]
    c.need(not bad, "the host has series it must never have: %s" % ", ".join(sorted(bad)))
    c.need(not bad_points(d), "points that are not [t, avg, min, max]: %s" % ", ".join(bad_points(d)))
    c.need(any(v is not None for v in avgs(s.get("cpu/total", [])).values()), "cpu/total has no value in the last hour")
    c.need((d.get("scale") or {}).get("cpu") == "percent_of_one_core", "scale.cpu is not percent_of_one_core")
    st, body = ev.call("h1-live")
    d = core_data(body) or {}
    c.need(st == 200 and d.get("step_seconds") == 5 and d.get("range") == "live", "range=live: %s, step %r" % (st, d.get("step_seconds")))
    expect_error(c, ev, "h1-2d", 400, "bad_range")
    expect_error(c, ev, "h1-nosuch", 404, "unknown_item")
    c.notes.append("%d series in the last hour" % len(s))
    return c


def j_rings(c, ev, a, b, what, need_rings=True):
    if ev.t(b, "t0") - ev.t(a, "t1") < 120:
        raise Missing("the two listings of %s are less than 2 minutes apart" % what)
    ma, fa = ring_ls(ev.gx(a)[1])
    mb, fb = ring_ls(ev.gx(b)[1])
    rings = sorted(n for n in fa if n.endswith(".ring") and n in fb)
    if need_rings:
        c.need(bool(rings), "%s has no history file (*.ring) in both listings" % what)
    for n in rings:
        c.need(fa[n][0] == fb[n][0], "%s: %s changed size (%d then %d bytes)" % (what, n, fa[n][0], fb[n][0]))
    for mode in (ma, mb):
        c.need(mode == "750", "%s: directory mode %s, expected 0750" % (what, mode))
    for n, (_, mode, typ) in fb.items():
        if typ == "f" and not n.endswith(".tmp"):
            c.need(mode == "640", "%s: %s has mode %s, expected 0640" % (what, n, mode))
    c.notes.append("%d history files of fixed size" % len(rings))
    return rings


def j_h2(ev):
    c = Case()
    j_rings(c, ev, "h2-a", "h2-b", "/var/lib/casaos/stats/core")
    return c


def outage(ev, stop, start, what):
    down = stamps(ev.gx(stop)[1]).get("after")
    up = stamps(ev.gx(start)[1]).get("before")
    if down is None or up is None:
        raise Missing("%s: the guest did not say when it was stopped and started" % what)
    minutes = full_minutes(down, up)
    if not minutes:
        raise Missing("%s was away %.0f s: not one whole minute, no gap to look at" % (what, up - down))
    return down, up, minutes


def check_gap(c, pre, post, down, up, minutes, never_zero, what):
    s = series_of(post)
    for key, points in s.items():
        a = avgs(points)
        for m in minutes:
            c.need(a.get(m) is None, "%s: %s has %r at %d, a minute %s was away: expected null" % (what, key, a.get(m), m, what))
    for key in never_zero:
        a = avgs(s.get(key, []))
        zeros = [t for t, v in a.items() if v == 0 and down - 60 <= t <= up + 60]
        c.need(not zeros, "%s: %s is 0 around the outage (%s): a gap must be null, never 0" % (what, key, zeros[:3]))
    for key in never_zero:
        lost = kept(pre, post, key)
        c.need(not lost, "%s: the minutes %s of %s held a value before and none after" % (what, lost[:3], key))


def j_h3(ev):
    c = Case()
    down, up, minutes = outage(ev, "h3-stop", "h3-start", "the core")
    st0, pre = ev.call("h3-pre")
    st1, post = ev.call("h3-post")
    pre, post = core_data(pre), core_data(post)
    c.need(st0 == 200 and st1 == 200 and pre is not None and post is not None, "range=1h before and after: %s and %s" % (st0, st1))
    earlier = [t for t, v in avgs(series_of(post).get("cpu/total", [])).items() if v is not None and t + 60 <= down]
    c.need(bool(earlier), "no minute from before the stop is left in range=1h")
    later = [t for t, v in avgs(series_of(post).get("cpu/total", [])).items() if v is not None and t >= up]
    c.need(bool(later), "no minute after the start: the sampler did not resume")
    check_gap(c, pre, post, down, up, minutes, ("cpu/total", "memory/rss"), "the core")
    c.notes.append("away %.0f s, %d null minutes, %d earlier minutes kept" % (up - down, len(minutes), len(earlier)))
    return c


def j_h4(ev):
    c = Case()
    st, body = ev.call("h4-get")
    d = core_data(body)
    c.need(st == 200 and d is not None, "GET alerts: %s" % st)
    check_rules(c, "the defaults", d, DEFAULT_RULES)
    c.need(active_of(d) == [], "an alert is active on the idle machine: %r" % active_of(d))
    for name, field in (("h4-bad-t0", "threshold"), ("h4-bad-t101", "threshold"), ("h4-bad-m0", "minutes"), ("h4-bad-m121", "minutes")):
        expect_error(c, ev, name, 400, "bad_rules: rules[0].%s" % field)
    st, body = ev.call("h4-put")
    d = core_data(body)
    c.need(st == 200, "PUT cpu_high 20/1: %s" % st)
    check_rules(c, "after the PUT", d, {"cpu_high": (True, 20, 1), "mem_high": (True, 90, 5)})
    if not (first_yes(ev.tsv("h4-calm")) or {}).get("yes"):
        raise Missing("the machine was not calm before the hog")
    rc, _ = ev.gx("h4-hog")
    if rc != 0:
        raise Missing("the hog did not start")
    hog_t, hog_s = ev.t("h4-hog"), ev.num("h4-hog-seconds")
    fire = first_yes(ev.tsv("h4-fire"))
    if not c.need(fire is not None, "cpu_high never became active under a hog of %.0f s on every vCPU" % hog_s):
        return c
    seen = parse_seen(fire["seen"])
    c.need(is_num(seen.get("value")) and seen["value"] >= 20 and rfc3339(seen.get("since")), "the active alert has value %r since %r" % (seen.get("value"), seen.get("since")))
    c.notes.append("fired %.0f s after the hog started (value %s)" % (fire["t1"] - hog_t, seen.get("value")))
    end = hog_t + hog_s
    res = first_yes([r for r in ev.tsv("h4-resolve") if r["t0"] >= end - 10])
    c.need(res is not None and res["t1"] - end <= 180, "cpu_high did not resolve within 3 min of the hog's end")
    if res:
        c.notes.append("resolved %.0f s after the hog ended" % (res["t1"] - end))
    return c


def parse_seen(seen):
    try:
        v = json.loads(seen)
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def j_h4_hub(ev):
    c = Case()
    st, body = ev.call("h4-hub-get")
    d = core_data(body) or {}
    c.need(st == 200 and (d.get("categories") or {}).get("resources") is True, "GET /v1/sys/alerts: %s, categories.resources %r (expected on by default)" % (st, (d.get("categories") or {}).get("resources")))
    return c


def cost_of(ev, unit):
    a, b = kv_cpu(ev.gx("cost-a")[1]), kv_cpu(ev.gx("cost-b")[1])
    ua, ub = a["units"].get(unit), b["units"].get(unit)
    if not ua or not ub or ua[0] == "-" or ub[0] == "-":
        raise Missing("%s has no main process in one of the two readings" % unit)
    if ua[0] != ub[0]:
        raise Missing("%s was started again during the window (pid %s then %s)" % (unit, ua[0], ub[0]))
    dt = b["uptime"] - a["uptime"]
    if dt < 150:
        raise Missing("the window is %.0f s, under 150 s" % dt)
    tck = a["clk_tck"] or 100
    own = ((ub[1] + ub[2]) - (ua[1] + ua[2])) / tck / dt * 100
    kids = ((ub[3] + ub[4]) - (ua[3] + ua[4])) / tck / dt * 100
    return own, kids, dt, a, b


def kv_cpu(out):
    r = {"units": {}, "uptime": 0.0, "clk_tck": 100, "containers": 0}
    for line in out.splitlines():
        c = line.split("\t")
        try:
            if c[0] == "unit" and len(c) >= 7:
                r["units"][c[1]] = (c[2],) + tuple(int(x) if x != "-" else 0 for x in c[3:7])
            elif c[0] == "uptime":
                r["uptime"] = float(c[1])
            elif c[0] == "clk_tck":
                r["clk_tck"] = int(c[1])
            elif c[0] == "containers":
                r["containers"] = int(c[1])
        except (ValueError, IndexError):
            raise Missing("a CPU reading is not readable: %r" % line)
    return r


def j_cost(ev, unit, need_containers=False, ring=None):
    c = Case()
    own, kids, dt, a, b = cost_of(ev, unit)
    if need_containers and min(a["containers"], b["containers"]) < 10:
        raise Missing("fewer than 10 containers ran during the window (%d, %d)" % (a["containers"], b["containers"]))
    if ring:
        _, files = ring_ls(ev.gx(ring)[1])
        c.need(any(n.endswith(".ring") for n in files), "the virt module's history directory has no history file")
        c.notes.append("%d files in /var/lib/casaos/stats/virt_management" % len(files))
    c.need(own <= COST_FAIL, "%s used %.2f %% of one core over %.0f s, above %.1f %%" % (unit, own, dt, COST_FAIL))
    c.notes.append("%.2f %% of one core over %.0f s (children %.2f %%), whole process: the sampler's own %.1f %% budget is not isolated here, the whole process %s it"
                   % (own, dt, kids, COST_BUDGET, "is within" if own <= COST_BUDGET else "is above"))
    if need_containers:
        c.notes.append("%d containers running" % b["containers"])
    return c


def facts(ev):
    return kv(ev.gx("facts")[1])


def j_a1(ev):
    c = Case()
    rc, _ = ev.gx("a1-run")
    if rc != 0:
        raise Missing("the busy container never started")
    seen = first_yes(ev.tsv("a1-seen"))
    c.need(seen is not None and seen["t0"] - ev.t("a1-run") <= 20, "container:statproof was not listed running within 20 s of its start")
    st, body = ev.call("a1-list")
    lst = bare(body)
    c.need(st == 200 and lst is not None, "the list: %s, not a bare body" % st)
    it = item(lst, "container:statproof")
    c.need(it is not None and it.get("kind") == "container" and it.get("state") == "running", "the list has %r for container:statproof" % (it,))
    if ev.t("a1-15m", "t0") - ev.t("a1-run") < 90:
        raise Missing("the series was read less than 90 s after the start")
    st, body = ev.call("a1-15m")
    d = bare(body) or {}
    c.need(st == 200 and d.get("range") == "15m" and d.get("step_seconds") == 60, "range=15m: %s, step %r" % (st, d.get("step_seconds")))
    c.need(not bad_points(d), "points that are not [t, avg, min, max]: %s" % ", ".join(bad_points(d)))
    s = series_of(d)
    busy = [v for v in cpu_sum(d).values()]
    c.need(busy and max(busy) > 50, "cpu/user + cpu/system of a busy loop peaks at %r %% of one core, expected above 50" % (max(busy) if busy else None))
    rss = [v for v in avgs(s.get("memory/rss", [])).values() if v is not None]
    c.need(rss and max(rss) > 0, "memory/rss has no value above 0")
    for key in s:
        if key.startswith("network/"):
            c.need(all(v is None or is_num(v) for p in s[key] for v in p[1:]), "%s has a value that is not a finite number" % key)
    absent = ["cpu/iowait", "pids/fds"] + (["memory/swap"] if facts(ev).get("cgroup") == "cgroup2fs" else [])
    present = [k for k in absent if k in s]
    c.need(not present, "series Docker gives no source for are written: %s (they must be absent, never 0)" % ", ".join(present))
    expect_error(c, ev, "a1-2d", 400, "bad_range")
    expect_error(c, ev, "a1-unknown", 404, "unknown_item")
    st, _ = ev.call("a1-noauth")
    c.need(st == 401, "without a token: %s, expected 401" % st)
    if busy:
        c.notes.append("busy loop at %.0f %% of one core, cgroup %s" % (max(busy), facts(ev).get("cgroup")))
    return c


def j_a2(ev):
    c = Case()
    rc, _ = ev.gx("a2-up")
    if rc != 0:
        raise Missing("the compose project did not start")
    c.need(first_yes(ev.tsv("a2-seen")) is not None, "the compose services were not listed running within 60 s")
    _, body = ev.call("a1-list")
    lst = bare(body)
    for svc in ("lim", "free"):
        it = item(lst, "app:stproof:%s:1" % svc) or {}
        named = it.get("name") == "stproof / %s" % svc or (it.get("name") == svc and it.get("app") == "stproof")
        c.need(it.get("kind") == "app-container" and named, "app:stproof:%s:1 is %r (expected kind app-container, named stproof / %s)" % (svc, it or None, svc))
    st, body = ev.call("a2-lim")
    d = bare(body) or {}
    alloc = (d.get("scale") or {}).get("cpu_allocation")
    c.need(st == 200 and is_num(alloc) and abs(alloc - 0.5) < 1e-6, "cpus: 0.5 gives scale.cpu_allocation %r (HTTP %s)" % (alloc, st))
    c.need("no_cpu_limit" not in (d.get("notes") or []), "a limited container has the note no_cpu_limit")
    peak = max(cpu_sum(d).values(), default=None)
    c.need(peak is None or peak <= 65, "a busy loop under cpus: 0.5 peaks at %.0f %% of one core: the limit is not what the series says" % (peak or 0))
    st, body = ev.call("a2-free")
    d = bare(body) or {}
    alloc = (d.get("scale") or {}).get("cpu_allocation")
    c.need(st == 200 and "no_cpu_limit" in (d.get("notes") or []) and is_num(alloc) and alloc > 0, "a container without a CPU limit: notes %r, cpu_allocation %r" % (d.get("notes"), alloc))
    c.notes.append("lim %s %% of one core at most; free measured against %s cores (nproc %s)" % ("%.0f" % peak if peak is not None else "-", alloc, facts(ev).get("nproc")))
    return c


def j_a3(ev):
    c = Case()
    rc, _ = ev.gx("a3-run")
    if rc != 0:
        raise Missing("the containers to stop and kill did not start")
    st, body = ev.call("a3-put")
    c.need(st == 200, "PUT cpu_high 10/1 on container:statproof: %s" % st)
    check_rules(c, "after the PUT", bare(body), {"cpu_high": (True, 10, 1)})
    fire = first_yes(ev.tsv("a3-fire"))
    c.need(fire is not None, "cpu_high never became active for the busy container")
    rc, out = ev.gx("a3-stop")
    codes = {}
    for line in out.splitlines():
        cells = line.split("\t")
        if cells[0] == "exit" and len(cells) == 2 and len(cells[1].split()) == 3:
            name, _, code = cells[1].split()
            codes[name] = code
    if sorted(codes) != ["statkill", "statproof", "stattrm"] or not set(codes.values()) <= {"137", "143"}:
        raise Missing("the stops did not end as planned (exit codes %r): nothing to judge" % codes)
    if "143" not in codes.values():
        c.notes.append("no exit 143 was produced (%r)" % codes)
    stopped = first_yes(ev.tsv("a3-stopped"))
    c.need(stopped is not None, "the three containers were not all listed stopped within 45 s: last seen %r" % (ev.tsv("a3-stopped")[-1:] or [{}])[0].get("seen"))
    rows = ev.tsv("a3-stopped")
    c.need(not any("crashed" in r["seen"] for r in rows), "a container that was stopped or killed was listed crashed")
    if fire:
        c.need(first_yes(ev.tsv("a3-resolve")) is not None, "the container's cpu_high did not resolve within 60 s of its stop")
    st, body = ev.call("a3-15m")
    d = bare(body) or {}
    c.need(st == 200 and bool(cpu_sum(d)), "the stopped container's history: %s, %d minutes of CPU" % (st, len(cpu_sum(d))))
    c.notes.append("exit codes %s" % ", ".join("%s %s" % kv_ for kv_ in sorted(codes.items())))
    return c


def j_a3_rm(ev):
    c = Case()
    rc, _ = ev.gx("a3-rm")
    if rc != 0:
        raise Missing("docker rm -f stattrm failed")
    st, body = ev.call("a3-list-rm")
    it = item(bare(body), "container:stattrm")
    c.need(st == 200, "the list after the removal: %s" % st)
    c.need(it is None or (it.get("state") == "stopped" and it.get("cpu_percent") is None and it.get("memory_used") is None),
           "the removed container is listed as %r" % (it,))
    st, body = ev.call("a3-gone")
    d = bare(body) or {}
    c.need(st == 200 and "container_gone" in (d.get("notes") or []) and bool(cpu_sum(d)), "its history by id: %s, notes %r, %d minutes" % (st, d.get("notes"), len(cpu_sum(d))))
    c.notes.append("after the grace it is %s" % ("gone from the list" if it is None else "listed stopped with no numbers (its history is under 7 days)"))
    return c


def j_a4(ev):
    c = Case()
    down_t = ev.t("a4-down")
    rows = [r for r in ev.tsv("a4-out") if r["t0"] >= down_t + 30]
    if not rows:
        raise Missing("Docker was not away 30 s")
    odd = [r for r in rows if not (r["code"] == "200" or (r["code"] == "503" and "stats_unavailable" in r["seen"]))]
    c.need(not odd, "with Docker away the list answered %s" % sorted({(r["code"], r["seen"]) for r in odd}))
    modes = sorted({r["code"] for r in rows})
    c.notes.append("with Docker away the list answers %s" % ("503 stats_unavailable" if modes == ["503"] else "the last items" if modes == ["200"] else "/".join(modes)))
    st, _ = ev.call("a4-series-down")
    c.need(st == 200, "the series with Docker away: %s (it keeps answering from disk)" % st)
    back = first_yes(ev.tsv("a4-back"))
    c.need(back is not None and back["t0"] - ev.t("a4-up") <= 60, "the list did not list the compose container running within 60 s of Docker's start")
    down, up, minutes = outage(ev, "a4-down", "a4-up", "Docker")
    st0, pre = ev.call("a4-pre")
    st1, post = ev.call("a4-post")
    pre, post = bare(pre), bare(post)
    c.need(st0 == 200 and st1 == 200 and post is not None, "the series before and after: %s and %s" % (st0, st1))
    check_gap(c, pre, post, down, up, minutes, ("cpu/user", "memory/rss"), "Docker")
    before = [v for t, v in cpu_sum(post, 3).items() if t + 60 <= down] + list(cpu_sum(pre, 3).values())
    after = [v for t, v in cpu_sum(post, 3).items() if t >= up]
    bound = max(1.5 * max(before, default=0), 75)
    c.need(not after or max(after) <= bound, "a spike after Docker came back: %.0f %% of one core, above %.0f" % (max(after or [0]), bound))
    c.need(bool(after), "no minute after Docker came back")
    c.notes.append("away %.0f s, %d null minutes" % (up - down, len(minutes)))
    return c


def j_a5(ev):
    c = Case()
    j_rings(c, ev, "a5-ring-a", "a5-ring-b", "/var/lib/casaos/stats/app_management")
    rc, _ = ev.gx("a5-restart")
    if rc != 0:
        raise Missing("casaos-app-management could not be restarted")
    c.need(first_yes(ev.tsv("a5-up")) is not None, "the list did not come back after the restart")
    st0, pre = ev.call("a5-pre")
    st1, post = ev.call("a5-post")
    c.need(st0 == 200 and st1 == 200, "the series before and after the restart: %s and %s" % (st0, st1))
    lost = kept(bare(pre), bare(post), "cpu/user")
    c.need(not lost, "minutes lost by the restart: %s" % lost[:3])
    return c


def vm_expected(ev):
    out = ev.gx("v1-virsh")[1]
    return kv(out).get("test")


def j_v1(ev):
    c = Case()
    conf = kv(ev.gx("v1-conf")[1])
    if conf.get("uri") != "test:///default" or conf.get("active") != "active":
        raise Missing("the module is not on test:///default and active (%r)" % conf)
    vm = ev.text("vm-id").strip()
    if vm in ("", "-"):
        raise Missing("no VM id")
    want = vm_expected(ev)
    st, body = ev.call("v1-list")
    lst = bare(body)
    c.need(st == 200 and lst is not None, "the list: %s, not a bare body" % st)
    it = item(lst, "vm:%s" % (want or vm))
    c.need(it is not None and it.get("kind") == "vm" and it.get("group") == "Machines virtuelles", "the list has %r for vm:%s" % (it, want or vm))
    st, body = ev.call("v1-15m")
    d = bare(body) or {}
    c.need(st == 200 and d.get("range") == "15m" and d.get("step_seconds") == 60, "range=15m: %s, step %r" % (st, d.get("step_seconds")))
    c.need(not bad_points(d), "points that are not [t, avg, min, max]: %s" % ", ".join(bad_points(d)))
    s = series_of(d)
    c.need(any(v is not None for v in avgs(s.get("cpu/total", [])).values()), "cpu/total has no value")
    c.need("vm_memory_used_unavailable" in (d.get("notes") or []), "the note vm_memory_used_unavailable is missing (the test driver has no balloon)")
    c.need(not [k for k in s if k.startswith("alert/")], "alert/* series are served")
    expect_error(c, ev, "v1-2d", 400, "bad_range")
    expect_error(c, ev, "v1-unknown", 404, "unknown_item")
    st, _ = ev.call("v1-noauth")
    c.need(st == 401, "without a token: %s, expected 401" % st)
    c.notes.append("vm:%s, series %s; the test driver answers the batch with the state only: the CPU comes from DomainGetInfo" % (vm, ", ".join(sorted(s))))
    if not want:
        c.notes.append("virsh named no domain test: the id was taken from the list")
    return c


def j_v2(ev):
    c = Case()
    if ev.has("v2-reboot.failed"):
        raise Missing("the guest did not come back from its restart")
    btime = kv(ev.gx("v2-btime")[1]).get("btime")
    if not btime:
        raise Missing("no boot time after the restart")
    btime = float(btime)
    st0, pre = ev.call("v2-pre")
    st1, post = ev.call("v2-post")
    pre, post = bare(pre), bare(post)
    c.need(st0 == 200 and st1 == 200 and post is not None, "range=1h before and after: %s and %s" % (st0, st1))
    old = [t for t, v in avgs(series_of(post).get("cpu/total", [])).items() if v is not None and t < btime]
    c.need(bool(old), "no point older than the boot is left")
    lost = kept(pre, post, "cpu/total")
    c.need(not lost, "minutes lost by the restart: %s" % lost[:3])
    _, fa = ring_ls(ev.gx("v2-ring-a")[1])
    _, fb = ring_ls(ev.gx("v2-ring-b")[1])
    rings = [n for n in fa if n.endswith(".ring") and n in fb]
    c.need(bool(rings), "no history file before and after")
    for n in rings:
        c.need(fa[n][0] == fb[n][0], "%s changed size across the restart" % n)
    c.notes.append("%d minutes older than the boot kept" % len(old))
    return c


def j_v3(ev):
    c = Case()
    rc, out = ev.gx("v3-virsh")
    if rc != 0 or "done" not in out.split():
        raise Missing("the empty test driver could not be opened with virsh: %s" % out.strip()[:200])
    uuids = [l for l in out.splitlines() if l.startswith("uuid\t")]
    if uuids:
        raise Missing("the empty test driver holds domains: %r" % uuids)
    conf = kv(ev.gx("v3-conf")[1])
    if conf.get("uri") != EMPTY_DRIVER or conf.get("active") != "active":
        raise Missing("the module is not on the empty driver and active (%r)" % conf)
    if ev.t("v3-list", "t0") - ev.t("v3-conf") < 170:
        raise Missing("the list was read less than 3 minutes after the module started")
    st, body = ev.call("v3-list")
    lst = bare(body)
    c.need(st == 200 and lst is not None and lst.get("items") == [], "with no domain the list is %s %r, expected {\"items\": []}" % (st, (lst or {}).get("items")))
    _, fa = ring_ls(ev.gx("v3-ring-a")[1])
    _, fb = ring_ls(ev.gx("v3-ring-b")[1])
    rings = [n for n in fb if n.endswith(".ring")]
    c.need(not rings, "history files were written with no domain: %s" % rings)
    grown = [n for n in fb if n not in fa or fa[n][0] != fb[n][0]]
    c.need(not grown, "the directory grew with no domain: %s" % grown)
    return c


def j_v4(ev):
    c = Case()
    rc, out = ev.gx("v4-restart")
    s = stamps(out)
    if rc != 0 or "before" not in s or "after" not in s:
        raise Missing("libvirtd could not be restarted")
    back = first_yes(ev.tsv("v4-back"))
    c.need(back is not None and back["t0"] - ev.t("v4-restart") <= 30, "the route did not answer 200 within 30 s of libvirtd's restart")
    c.need(ev.gx("v4-unit")[1].strip() == "active", "casaos-virt-management is %r after libvirtd's restart" % ev.gx("v4-unit")[1].strip())
    st, body = ev.call("v4-live")
    d = bare(body) or {}
    c.need(st == 200 and d.get("step_seconds") == 5, "range=live: %s" % st)
    pts = {p[0]: p for p in series_of(d).get("cpu/total", []) if isinstance(p, list) and len(p) == 4}
    lo, hi = s["before"] - 5, s["after"] + 20
    # a gap is a null point in the window, or a step missing between two points around it
    ts = sorted(pts)
    gaps = [t for t in ts if lo <= t <= hi and pts[t][1] is None]
    gaps += [a for a, b in zip(ts, ts[1:]) if b - a > 7 and a <= hi and b >= lo]
    c.need(bool(gaps), "no null point between libvirtd's stop and 20 s after its start: the outage left no gap")
    vcpus = (d.get("scale") or {}).get("cpu_allocation") or 1
    pre = [p[1] for t, p in pts.items() if t < s["before"] and p[1] is not None]
    post = [p[1] for t, p in pts.items() if t > s["after"] and p[1] is not None]
    bound = 1.5 * max(max(pre, default=0), 100 * vcpus)
    c.need(not post or max(post) <= bound, "a spike after libvirtd came back: %.0f, above %.0f" % (max(post or [0]), bound))
    c.need(bool(post), "no point after libvirtd came back")
    c.notes.append("%d null points at the outage" % len(gaps))
    return c


def j_v5(ev):
    c = Case()
    st, body = ev.call("v5-get")
    c.need(st == 200 and bare(body) is not None, "GET alerts: %s" % st)
    check_rules(c, "the defaults", bare(body), DEFAULT_RULES)
    want = {"mem_high": (True, 50, 1), "cpu_high": (True, 20, 1)}
    st, body = ev.call("v5-put")
    c.need(st == 200, "PUT: %s" % st)
    check_rules(c, "the PUT's answer", bare(body), want)
    st, body = ev.call("v5-get2")
    check_rules(c, "the GET after the PUT", bare(body), want)
    expect_error(c, ev, "v5-bad", 400, "bad_rules: rules[0].threshold")
    c.notes.append("active: %r" % [a.get("key") for a in active_of(bare(body))])
    return c


def j_c1(ev):
    c = Case()
    st, _ = ev.call("c1-hub-put")
    if st != 200:
        return ("UNVERIFIED", "the hub refused the proof's channel (HTTP %s): no readable evidence of what it sent" % st)
    host = facts(ev).get("hostname") or ""
    entries = []
    for line in ev.gx("c1-hook-log")[1].splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("path") != "/probe":
            entries.append(str(e.get("path", "")) + " " + str(e.get("body", "")))
    found = {
        "host firing": any(host and (host + ": CPU at") in e for e in entries),
        "host resolved": any(host and ("Resolved: " + host + ": CPU is back to normal") in e for e in entries),
        "container firing": any("statproof: CPU at" in e for e in entries),
        "container resolved": any("Resolved: statproof: CPU is back to normal" in e for e in entries),
    }
    others = [e for e in entries if "CPU" not in e]
    if found["host firing"] and found["container firing"]:
        c.notes.append(", ".join("%s %s" % (k, "seen" if v else "not seen") for k, v in found.items()))
        return c
    if others:
        c.fails.append("the channel works (%d other messages arrived, the test one among them) but %s did not reach it"
                       % (len(others), " and ".join(k for k in ("host firing", "container firing") if not found[k])))
        return c
    _, body = ev.call("c1-hub-end")
    failure = (core_data(body) or {}).get("last_failure")
    if failure:
        return ("UNVERIFIED", "no message reached the proof's channel; the hub's last failure: %s" % json.dumps(failure)[:200])
    return ("UNVERIFIED", "no message reached the proof's channel, not even the test one, and the hub reports no failure: no readable evidence")


def units_of(out):
    r = {}
    for line in out.splitlines():
        c = line.split("\t")
        if c[0] == "unit" and len(c) >= 4:
            r[c[1]] = (c[2], c[3])
    return r


def journal_of(out):
    panics, errors = {}, []
    for line in out.splitlines():
        c = line.split("\t")
        if c[0] == "panic" and len(c) == 3:
            panics[c[1]] = int(c[2]) if c[2].isdigit() else 0
        elif c[0] == "error":
            errors.append(line)
    return panics, errors


def j_c2(ev):
    c = Case()
    segments = [("c2-units-start", "c2-units-prereboot")]
    if ev.has("c2-units-postreboot.out"):
        segments.append(("c2-units-postreboot", "c2-units-end"))
    else:
        c.notes.append("the second boot has no reading: judged up to the restart only")
    for a, b in segments:
        ua, ub = units_of(ev.gx(a)[1]), units_of(ev.gx(b)[1])
        for u in CASAOS_UNITS:
            if u not in ua or u not in ub:
                raise Missing("%s is not in %s and %s" % (u, a, b))
            c.need(ua[u][0] == ub[u][0], "%s was restarted by systemd between %s and %s (NRestarts %s then %s)" % (u, a, b, ua[u][0], ub[u][0]))
    end = units_of(ev.gx("c2-units-end")[1])
    for u in CASAOS_UNITS:
        c.need(end.get(u, ("", ""))[1] == "active", "%s is %r at the end" % (u, end.get(u, ("", ""))[1]))
    errors = 0
    for name in ("c2-journal-prereboot", "c2-journal-end"):
        if not ev.has(name + ".out"):
            if name == "c2-journal-prereboot":
                raise Missing("no journal reading before the restart")
            continue
        panics, errs = journal_of(ev.gx(name)[1])
        if not panics:
            raise Missing("%s has no reading of the units" % name)
        for u, n in panics.items():
            c.need(n == 0, "%s: %d journal lines say panic (%s)" % (u, n, name))
        errors += len(errs)
    c.notes.append("%d error lines of the stats components naming stats (see c2-journal-*.out)" % errors)
    return c


CASES = [
    ("H1", j_h1), ("H2", j_h2), ("H3", j_h3), ("H4", j_h4), ("H4-hub", j_h4_hub),
    ("H5", lambda ev: j_cost(ev, "casaos.service")),
    ("A1", j_a1), ("A2", j_a2), ("A3", j_a3), ("A3-rm", j_a3_rm), ("A4", j_a4), ("A5", j_a5),
    ("A6", lambda ev: j_cost(ev, "casaos-app-management.service", need_containers=True)),
    ("V1", j_v1), ("V2", j_v2), ("V3", j_v3), ("V4", j_v4), ("V5", j_v5),
    ("V6", lambda ev: j_cost(ev, "casaos-virt-management.service", ring="v6-ring")),
    ("C1", j_c1), ("C2", j_c2),
]
BY_DESIGN = [("V5-crash", "vm_crashed cannot be provoked on libvirt's test driver (it cannot make a domain crash); the crash path is proven by the module's unit tests only")]


def judge(out):
    ev = Evidence(out)
    lines = []
    aborted = ev.maybe("aborted") or ev.maybe(os.path.join("guest", "aborted"))
    if aborted:
        lines.append(("INVALID RUN", "run", "the run stopped early: %s" % " ".join(aborted.split())[:300]))
    invalid = {}
    for line in (ev.maybe("invalid.txt") or "").splitlines():
        cid, _, why = line.partition("\t")
        invalid.setdefault(cid.strip(), []).append(why.strip())
    for cid, fn in CASES:
        if cid in invalid:
            lines.append(("INVALID RUN", cid, "; ".join(invalid[cid])))
            continue
        try:
            r = fn(ev)
        except Missing as e:
            lines.append(("INVALID RUN", cid, str(e)))
            continue
        except Exception as e:  # evidence of a shape the judge did not expect proves nothing either way
            lines.append(("INVALID RUN", cid, "the judge could not read the evidence: %s: %s" % (type(e).__name__, e)))
            continue
        if isinstance(r, tuple):
            lines.append((r[0], cid, r[1]))
        elif r.fails:
            lines.append(("FAIL", cid, "; ".join(r.fails)))
        else:
            lines.append(("PASS", cid, "; ".join(r.notes) or "as the contract says"))
    for cid, why in BY_DESIGN:
        lines.append(("UNVERIFIED", cid, why))
    for cid in sorted(set(invalid) - {c for c, _ in CASES}):
        lines.append(("INVALID RUN", cid, "; ".join(invalid[cid])))
    return lines


# ---- what the runner asks while it polls ---------------------------------------------------------------------------------------------------


def unwrap(body):
    d = core_data(body)
    return d if d is not None else body


def helper(pred, path, code, words):
    """yes or no, a tab, and what was seen (one line). code is the HTTP status of the answer in path."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            body = unwrap(json.load(f))
    except (OSError, ValueError):
        body = None
    if pred == "never":
        return "no\t%s" % (message(body) or "-")
    if code != "200":
        return "no\t%s" % (message(body) or "-")
    if pred == "ok":
        return "yes\t200"
    if pred == "items":
        state, ids = words[0], words[1:]
        seen = []
        for i in ids:
            it = item(body, i)
            seen.append("%s=%s" % (i, it.get("state") if it else "absent"))
            if it is None or (state != "-" and it.get("state") != state):
                return "no\t%s" % " ".join(seen)
        return "yes\t%s" % " ".join(seen)
    if pred in ("active", "calm"):
        hit = [a for a in active_of(body) if a.get("key") == words[0]]
        if pred == "calm":
            return ("no\t%s" % json.dumps(hit[0])) if hit else "yes\t[]"
        return ("yes\t%s" % json.dumps(hit[0], separators=(",", ":"))) if hit else "no\t%s" % json.dumps([a.get("key") for a in active_of(body)])
    return "no\tunknown predicate %s" % pred


def vmid(virsh_out, list_path):
    """the uuid of the test driver's domain named test, else the first vm: item of the list, else -"""
    try:
        with open(virsh_out, encoding="utf-8", errors="replace") as f:
            t = kv(f.read()).get("test")
        if t:
            return t
    except OSError:
        pass
    try:
        with open(list_path, encoding="utf-8") as f:
            for it in items_of(json.load(f)):
                if str(it.get("id", "")).startswith("vm:"):
                    return it["id"][3:]
    except (OSError, ValueError):
        pass
    return "-"


def main(argv):
    if len(argv) >= 2 and argv[1] == "helper":
        if len(argv) >= 4 and argv[2] == "vmid":
            print(vmid(argv[3], argv[4] if len(argv) > 4 else ""))
            return 0
        if len(argv) < 5:
            print("usage: stats-proof-report.py helper <predicate> <file> <code> [words]", file=sys.stderr)
            return 2
        print(helper(argv[2], argv[3], argv[4], argv[5:]))
        return 0
    if len(argv) != 3:
        print("usage: stats-proof-report.py <out dir> <d12|u24>", file=sys.stderr)
        return 2
    lines = judge(argv[1])
    text = "".join("%s %s %s\n" % l for l in lines)
    sys.stdout.write("stats-proof %s, release %s\n" % (argv[2], (Evidence(argv[1]).maybe("tag") or "?").strip()) + text)
    try:
        with open(os.path.join(argv[1], "report.txt"), "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass
    return 1 if any(v in ("FAIL", "INVALID RUN") for v, _, _ in lines) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
