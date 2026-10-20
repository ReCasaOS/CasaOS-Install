#!/usr/bin/env python3
"""Tests of stats-proof-report.py: a recording of a good run gives PASS everywhere (and the one UNVERIFIED by design), each kind of failure gives a FAIL on its case, and evidence
that is missing or a harness that could not set up gives an INVALID RUN, never a PASS. Plain python, no guest: python3 scripts/test-stats-proof.py"""
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("stats_proof_report", os.path.join(HERE, "stats-proof-report.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

T = 1_800_000_000          # a whole minute
UUID = "6695eb01-f6a4-8304-79aa-97f2502e193f"
HOST = "sp-guest"


def pts(start, end, step, value):
    """[t, v, v, v] from start to end included; value(t) gives v or None"""
    return [[t, value(t), value(t), value(t)] for t in range(start, end + 1, step)]


def ser(**charts):
    out = []
    for key, points in charts.items():
        chart, _, name = key.replace("__", "/").replace("_c_", ":").partition("/")
        out.append({"chart": chart, "name": name, "unit": "percent", "points": points})
    return out


def result(rng, step, frm, to, series, alloc=None, limit=None, notes=None, iid="x"):
    return {"id": iid, "range": rng, "step_seconds": step, "from": frm, "to": to, "series": series,
            "scale": {"cpu": "percent_of_one_core", "cpu_allocation": alloc, "memory_limit": limit}, "notes": notes if notes is not None else []}


def env(data, status=200, msg="ok"):
    return {"success": status, "message": msg, "data": data}


def rules(cpu=(True, 90, 10), mem=(True, 90, 5)):
    return [{"key": "cpu_high", "enabled": cpu[0], "threshold": cpu[1], "minutes": cpu[2]},
            {"key": "mem_high", "enabled": mem[0], "threshold": mem[1], "minutes": mem[2]}]


def host_series(start, end, gap=()):
    v = lambda base: (lambda t: None if t in gap else base)
    s = {"cpu__total": pts(start, end, 60, v(12.5)), "cpu__vcpus": pts(start, end, 60, v(4)), "memory__rss": pts(start, end, 60, v(1e9)),
         "memory__cache": pts(start, end, 60, v(2e8)), "memory__swap": pts(start, end, 60, v(0)), "memory__limit": pts(start, end, 60, v(6e9)),
         "network__rx_c_ens3": pts(start, end, 60, v(100)), "network__tx_c_ens3": pts(start, end, 60, v(50)), "disk__read": pts(start, end, 60, v(0)),
         "fs__free_percent_c_/": pts(start, end, 60, v(80))}
    return ser(**s)


def ctr_series(start, end, user, gap=(), extra=None):
    v = lambda base: (lambda t: None if t in gap else base)
    s = {"cpu__user": pts(start, end, 60, v(user)), "cpu__system": pts(start, end, 60, v(2)), "cpu__throttled": pts(start, end, 60, v(0)),
         "memory__rss": pts(start, end, 60, v(1e6)), "memory__cache": pts(start, end, 60, v(0)), "network__rx_c_eth0": pts(start, end, 60, v(0)),
         "pids__count": pts(start, end, 60, v(1))}
    s.update(extra or {})
    return ser(**s)


def am_item(iid, kind, state, name, app=None):
    it = {"id": iid, "name": name, "kind": kind, "group": "Conteneurs" if app else "Autres conteneurs", "state": state, "started_at": None, "detail": "busybox",
          "cpu_percent": None if state != "running" else 50.0, "memory_used": None if state != "running" else 1e6, "memory_limit": None, "alerts": 0}
    if app:
        it["app"] = app
    return it


class Run:
    def __init__(self, d):
        self.d = d

    def w(self, name, text):
        with open(os.path.join(self.d, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def times(self, name, t0, t1):
        self.w(name + ".t0", "%.6f\n" % t0)
        self.w(name + ".t1", "%.6f\n" % t1)

    def call(self, name, code, body, t0, t1=None):
        self.w(name + ".code", "%s\n" % code)
        if body is not None:
            self.w(name + ".json", body if isinstance(body, str) else json.dumps(body))
        self.times(name, t0, t1 if t1 is not None else t0 + 0.2)

    def gx(self, name, out, rc=0, t0=T, t1=None):
        self.w(name + ".out", out)
        self.w(name + ".rc", "%d\n" % rc)
        self.times(name, t0, t1 if t1 is not None else t0 + 0.5)

    def tsv(self, name, rows):
        self.w(name + ".tsv", "".join("%.3f\t%.3f\t%s\t%s\n" % (t, t + 0.2, code, yn) for t, code, yn in rows))

    def rm(self, name):
        os.remove(os.path.join(self.d, name))


def ring(dirmode="750", files=(("a1b2.ring", 3000000, "640"), ("alerts.json", 200, "640"))):
    return "dir\t%s\troot:root\n" % dirmode + "".join("file\t%s\t%d\t%s\troot:root\tf\n" % f for f in files)


def stamp(**kw):
    return "".join("stamp\t%s\t%.6f\n" % (k, v) for k, v in kw.items())


def units(nrestarts=0, state="active"):
    names = list(R.CASAOS_UNITS) + ["docker.service", "libvirtd.service"]
    return "".join("unit\t%s\t%d\t%s\tinv-%d\n" % (u, nrestarts, state, i) for i, u in enumerate(names))


def journal(panics=0):
    return "".join("panic\t%s\t%d\n" % (u, panics) for u in R.CASAOS_UNITS) + "error\tcasaos.service\t{\"level\":\"error\",\"msg\":\"stats read failed\"}\n"


def cpu(uptime, ticks, containers=12, pids=("101", "202", "303")):
    out = "clk_tck\t100\nuptime\t%.2f\n" % uptime
    for (u, pid), tk in zip(zip(("casaos.service", "casaos-app-management.service", "casaos-virt-management.service"), pids), ticks):
        out += "unit\t%s\t%s\t%d\t%d\t0\t0\n" % (u, pid, tk, tk // 2)
    return out + "containers\t%d\n" % containers


def good_run(d):
    r = Run(d)
    r.w("tag", "v0.5.41\n")
    r.w("leg", "d12\n")
    r.gx("facts", "hostname\t%s\nos\tdebian 12\nnproc\t4\ncgroup\tcgroup2fs\nclk_tck\t100\n" % HOST)
    # H2
    r.gx("h2-a", ring(), t0=T + 9, t1=T + 10)
    # V3 begins
    r.gx("v3-virsh", "done\n", t0=T + 15)
    r.gx("v3-conf", stamp(before=T + 18, after=T + 20) + "uri\ttest:///usr/local/lib/sp/empty.xml\nactive\tactive\n", t0=T + 17, t1=T + 20)
    r.gx("v3-ring-a", "nodir\n", t0=T + 21)
    # H1 routes
    r.call("h1-list", 200, env({"items": [{"id": "host", "name": HOST, "kind": "host", "group": "Machine", "state": "running", "alerts": 0}], "notes": []}), T + 30)
    r.call("h1-noauth", 401, {"message": "token is missing"}, T + 31)
    r.call("h1-live", 200, env(result("live", 5, T - 865, T + 30, [], iid="host")), T + 32)
    r.call("h1-2d", 400, {"success": 400, "message": "bad_range", "data": None}, T + 33)
    r.call("h1-nosuch", 404, {"success": 404, "message": "unknown_item", "data": None}, T + 34)
    # H4
    r.call("h4-get", 200, env({"rules": rules(), "active": []}), T + 40)
    for name, field in (("h4-bad-t0", "threshold"), ("h4-bad-t101", "threshold"), ("h4-bad-m0", "minutes"), ("h4-bad-m121", "minutes")):
        r.call(name, 400, {"success": 400, "message": "bad_rules: rules[0].%s" % field, "data": None}, T + 41)
    r.call("h4-put", 200, env({"rules": rules(cpu=(True, 20, 1)), "active": []}), T + 42)
    r.call("h4-hub-get", 200, env({"channels": [], "categories": {"resources": True, "disks": True}, "disk_threshold": 90, "last_failure": None}), T + 43)
    r.gx("c1-hook", "listening\n", t0=T + 44)
    r.call("c1-hub-put", 200, env({"channels": [{"id": "c1", "name": "stats-proof"}], "categories": {"resources": True}}), T + 45)
    r.call("c1-test", 200, env([{"id": "c1", "ok": True}]), T + 46)
    r.tsv("h4-calm", [(T + 50, 200, "yes\t[]")])
    r.w("h4-hog-seconds", "150\n")
    r.gx("h4-hog", stamp(before=T + 69, after=T + 70), t0=T + 69, t1=T + 70)
    r.tsv("h4-fire", [(T + 80, 200, "no\t[]"), (T + 190, 200, 'yes\t{"key":"cpu_high","since":"2027-01-15T08:03:00Z","value":97}')])
    r.gx("h4-hog-stop", "was\tinactive\n", t0=T + 221)
    r.tsv("h4-resolve", [(T + 222, 200, "no\t{}"), (T + 320, 200, "yes\t[]")])
    # V3 ends, V1 begins
    r.call("v3-list", 200, {"items": [], "notes": []}, T + 400)
    r.gx("v3-ring-b", "nodir\n", t0=T + 401)
    r.gx("v1-conf", stamp(before=T + 408, after=T + 410) + "uri\ttest:///default\nactive\tactive\n", t0=T + 407, t1=T + 410)
    r.gx("v1-virsh", "uuid\t%s\ntest\t%s\ndone\n" % (UUID, UUID), t0=T + 411)
    # A1, A2
    r.gx("a1-run", "c0ffee\n", t0=T + 419, t1=T + 420)
    r.tsv("a1-seen", [(T + 422, 200, "no\tcontainer:statproof=absent"), (T + 428, 200, "yes\tcontainer:statproof=running")])
    r.gx("a2-up", "started\n", t0=T + 430)
    r.gx("a3-run", "x\ny\n", t0=T + 440)
    r.tsv("a2-seen", [(T + 445, 200, "yes\tapp:stproof:lim:1=running app:stproof:free:1=running")])
    # the series
    r.call("h1-1h", 200, env(result("1h", 60, T - 3000, T + 480, host_series(T - 3000, T + 480), alloc=4, iid="host")), T + 530)
    r.gx("h2-b", ring(), t0=T + 540)
    lst = {"items": [am_item("container:statproof", "container", "running", "statproof"), am_item("app:stproof:lim:1", "app-container", "running", "lim", "stproof"),
                     am_item("app:stproof:free:1", "app-container", "running", "free", "stproof")], "notes": []}
    r.call("a1-list", 200, lst, T + 530)
    r.call("a1-15m", 200, result("15m", 60, T - 360, T + 480, ctr_series(T + 420, T + 480, 97), alloc=4, notes=["no_cpu_limit", "no_memory_limit"]), T + 531)
    r.call("a1-noauth", 401, {"message": "token is missing"}, T + 532)
    r.call("a1-2d", 400, {"message": "bad_range"}, T + 533)
    r.call("a1-unknown", 404, {"message": "unknown_item"}, T + 534)
    r.call("a2-lim", 200, result("15m", 60, T - 360, T + 480, ctr_series(T + 480, T + 480, 48), alloc=0.5, notes=["no_memory_limit"]), T + 535)
    r.call("a2-free", 200, result("15m", 60, T - 360, T + 480, ctr_series(T + 480, T + 480, 0), alloc=4, notes=["no_cpu_limit", "no_memory_limit"]), T + 536)
    r.gx("a5-ring-a", ring(files=(("a.ring", 3000000, "640"), ("b.ring", 3000000, "640"), ("alerts.json", 300, "640"))), t0=T + 544, t1=T + 545)
    r.call("v1-list", 200, {"items": [{"id": "vm:" + UUID, "name": "test", "kind": "vm", "group": "Machines virtuelles", "state": "running", "alerts": 0}], "notes": []}, T + 600)
    r.w("vm-id", UUID + "\n")
    vm15 = ser(cpu__total=pts(T + 420, T + 540, 60, lambda t: 100.0), cpu__vcpus=pts(T + 420, T + 540, 60, lambda t: 2), memory__assigned=pts(T + 420, T + 540, 60, lambda t: 8e9))
    r.call("v1-15m", 200, result("15m", 60, T - 240, T + 540, vm15, alloc=2, notes=["vm_memory_used_unavailable"]), T + 601)
    r.call("v1-2d", 400, {"message": "bad_range"}, T + 602)
    r.call("v1-unknown", 404, {"message": "unknown_item"}, T + 603)
    r.call("v1-noauth", 401, {"message": "token is missing"}, T + 604)
    r.call("v5-get", 200, {"rules": rules(), "active": []}, T + 605)
    r.call("v5-put", 200, {"rules": rules(cpu=(True, 20, 1), mem=(True, 50, 1)), "active": []}, T + 606)
    r.call("v5-get2", 200, {"rules": rules(cpu=(True, 20, 1), mem=(True, 50, 1)), "active": []}, T + 607)
    r.call("v5-bad", 400, {"message": "bad_rules: rules[0].threshold"}, T + 608)
    # A3
    r.call("a3-put", 200, {"rules": rules(cpu=(True, 10, 1)), "active": []}, T + 610)
    r.tsv("a3-fire", [(T + 700, 200, 'yes\t{"key":"cpu_high","since":"2027-01-15T08:11:00Z","value":24}')])
    r.gx("a3-stop", stamp(before=T + 701, after=T + 715) + "exit\tstatproof exited 137\nexit\tstattrm exited 143\nexit\tstatkill exited 137\n", t0=T + 701, t1=T + 716)
    r.tsv("a3-stopped", [(T + 720, 200, "yes\tcontainer:statproof=stopped container:stattrm=stopped container:statkill=stopped")])
    r.tsv("a3-resolve", [(T + 725, 200, "yes\t[]")])
    r.call("a3-15m", 200, result("15m", 60, T - 120, T + 720, ctr_series(T + 420, T + 660, 97), alloc=4, notes=["container_gone"]), T + 730)
    r.gx("a3-rm", "stattrm\n", t0=T + 731)
    r.call("a3-list-rm", 200, {"items": lst["items"] + [am_item("container:stattrm", "container", "stopped", "stattrm")], "notes": []}, T + 762)
    r.call("a3-gone", 200, result("15m", 60, T - 120, T + 720, ctr_series(T + 480, T + 660, 0), notes=["container_gone"]), T + 763)
    # H3: the core away from T+820 to T+960
    r.call("h3-pre", 200, env(result("1h", 60, T - 2760, T + 780, host_series(T - 2760, T + 780), alloc=4, iid="host")), T + 810)
    r.gx("h3-stop", stamp(before=T + 818, after=T + 820) + "active\tinactive\n", t0=T + 817, t1=T + 821)
    r.gx("h3-start", stamp(before=T + 960, after=T + 962) + "active\tactive\n", t0=T + 959, t1=T + 963)
    r.call("h3-post", 200, env(result("1h", 60, T - 2460, T + 1080, host_series(T - 2460, T + 1080, gap=(T + 840, T + 900)), alloc=4, iid="host")), T + 1100)
    # A4: Docker away from T+1120 to T+1260
    lim = lambda frm, to, gap=(): ctr_series(frm, to, 48, gap=gap)
    r.call("a4-pre", 200, result("15m", 60, T + 240, T + 1080, lim(T + 480, T + 1080)), T + 1110)
    r.gx("a4-down", stamp(before=T + 1118, after=T + 1120) + "active\tinactive\n", t0=T + 1117, t1=T + 1120)
    r.tsv("a4-out", [(T + 1120 + i, 503, "no\tstats_unavailable") for i in range(5, 135, 5)])
    r.call("a4-series-down", 200, result("15m", 60, T + 300, T + 1200, lim(T + 480, T + 1080)), T + 1255)
    r.gx("a4-up", stamp(before=T + 1260, after=T + 1262) + "active\tactive\n", t0=T + 1259, t1=T + 1262)
    r.tsv("a4-back", [(T + 1265, 200, "no\tapp:stproof:lim:1=absent"), (T + 1280, 200, "yes\tapp:stproof:lim:1=running")])
    r.call("a4-post", 200, result("15m", 60, T + 600, T + 1440, lim(T + 600, T + 1440, gap=(T + 1140, T + 1200))), T + 1500)
    # A5
    r.gx("a5-ring-b", ring(files=(("a.ring", 3000000, "640"), ("b.ring", 3000000, "640"), ("alerts.json", 310, "640"))), t0=T + 1510)
    r.call("a5-pre", 200, result("15m", 60, T + 660, T + 1500, lim(T + 660, T + 1500, gap=(T + 1140, T + 1200))), T + 1511)
    r.gx("a5-restart", stamp(before=T + 1512, after=T + 1514) + "active\tactive\n", t0=T + 1512)
    r.tsv("a5-up", [(T + 1520, 200, "yes\tapp:stproof:lim:1=running")])
    r.call("a5-post", 200, result("15m", 60, T + 720, T + 1560, lim(T + 720, T + 1560, gap=(T + 1140, T + 1200))), T + 1580)
    # the cost: 0.3 % of a core for each over 180 s
    r.gx("cost-idle", "12\n", t0=T + 1590)
    r.gx("cost-a", cpu(1000.0, (1000, 1000, 1000)), t0=T + 1620)
    r.gx("cost-b", cpu(1180.0, (1036, 1036, 1036)), t0=T + 1800)
    r.w("cost-seconds", "180\n")
    r.gx("v6-ring", ring(files=(("v.ring", 3000000, "640"),)), t0=T + 1801)
    # V2: the guest restarts at T+1900
    r.call("v2-pre", 200, result("1h", 60, T - 1200, T + 1800, ser(cpu__total=pts(T + 420, T + 1800, 60, lambda t: 100.0))), T + 1850)
    r.gx("v2-ring-a", ring(files=(("v.ring", 3000000, "640"),)), t0=T + 1851)
    r.gx("c2-units-start", units(), t0=T + 5)
    r.gx("c2-units-prereboot", units(), t0=T + 1852)
    r.gx("c2-journal-prereboot", journal(), t0=T + 1853)
    r.gx("c2-units-postreboot", units(), t0=T + 1960)
    r.gx("v2-btime", "btime\t%d\n" % (T + 1900), t0=T + 1961)
    r.tsv("v2-up", [(T + 1970, 200, "yes\tvm:%s=running" % UUID)])
    r.call("v2-post", 200, result("1h", 60, T - 1080, T + 2040, ser(cpu__total=pts(T + 420, T + 2040, 60, lambda t: None if T + 1860 <= t < T + 1980 else 100.0))), T + 2050)
    r.gx("v2-ring-b", ring(files=(("v.ring", 3000000, "640"),)), t0=T + 2051)
    # V4: libvirtd restarts at T+2100
    r.gx("v4-restart", stamp(before=T + 2100, after=T + 2102) + "active\tactive\n", t0=T + 2099, t1=T + 2103)
    r.tsv("v4-back", [(T + 2105, 200, "yes\t200")])
    r.gx("v4-unit", "active\n", t0=T + 2145)
    live = pts(T + 1900, T + 2145, 5, lambda t: None if T + 2100 <= t <= T + 2110 else 100.0)
    r.call("v4-live", 200, result("live", 5, T + 1250, T + 2145, ser(cpu__total=live), alloc=2), T + 2146)
    # the end
    r.gx("c2-units-end", units(), t0=T + 2150)
    r.gx("c2-journal-end", journal(), t0=T + 2151)
    hook = [{"t": T + 46, "path": "/probe", "body": "probe"}, {"t": T + 47, "path": "/hook", "body": "This is a test message from ReCasaOS"},
            {"t": T + 191, "path": "/hook", "body": "%s: CPU at 97 %% for 1 min." % HOST}, {"t": T + 330, "path": "/hook", "body": "Resolved: %s: CPU is back to normal." % HOST},
            {"t": T + 701, "path": "/hook", "body": "statproof: CPU at 24 % for 1 min."}, {"t": T + 721, "path": "/hook", "body": "Resolved: statproof: CPU is back to normal."}]
    r.gx("c1-hook-log", "".join(json.dumps(h) + "\n" for h in hook), t0=T + 2152)
    r.call("c1-hub-end", 200, env({"channels": [], "categories": {"resources": True}, "last_failure": None}), T + 2153)
    return r


def verdicts(d):
    return {cid: (v, why) for v, cid, why in R.judge(d)}


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-test-")
        self.r = good_run(self.d)

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def v(self, cid):
        return verdicts(self.d)[cid]

    def assertVerdict(self, cid, want, needle=None):
        got, why = self.v(cid)
        self.assertEqual(got, want, "%s: %s %s" % (cid, got, why))
        if needle:
            self.assertIn(needle, why)


class GoodRun(Base):
    def test_every_case_passes(self):
        got = verdicts(self.d)
        for cid, _ in R.CASES:
            self.assertEqual(got[cid][0], "PASS", "%s: %s" % (cid, got[cid]))
        self.assertEqual(got["V5-crash"][0], "UNVERIFIED")

    def test_the_report_exits_0_and_writes_its_lines(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = R.main(["x", self.d, "d12"])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertTrue(os.path.exists(os.path.join(self.d, "report.txt")))
        for line in buf.getvalue().splitlines()[1:]:
            self.assertRegex(line, r"^(PASS|FAIL|INVALID RUN|UNVERIFIED) \S+ ")

    def test_costs_are_reported_with_the_budget_caveat(self):
        _, why = self.v("A6")
        self.assertIn("0.30 % of one core", why)
        self.assertIn("not isolated", why)


class Failures(Base):
    def test_h1_a_forbidden_series(self):
        _, body = R.Evidence(self.d).call("h1-1h")
        body["data"]["series"].append({"chart": "health", "name": "ok", "unit": "flag", "points": []})
        self.r.call("h1-1h", 200, body, T + 530)
        self.assertVerdict("H1", "FAIL", "health/ok")

    def test_h1_no_token_answered(self):
        self.r.call("h1-noauth", 200, env({"items": []}), T + 31)
        self.assertVerdict("H1", "FAIL", "401")

    def test_h1_a_point_that_is_not_four(self):
        _, body = R.Evidence(self.d).call("h1-1h")
        body["data"]["series"][0]["points"][0] = [T, 1, 2]
        self.r.call("h1-1h", 200, body, T + 530)
        self.assertVerdict("H1", "FAIL", "[t, avg, min, max]")

    def test_h2_a_file_that_grows(self):
        self.r.gx("h2-b", ring(files=(("a1b2.ring", 3000060, "640"), ("alerts.json", 200, "640"))), t0=T + 540)
        self.assertVerdict("H2", "FAIL", "changed size")

    def test_h2_modes(self):
        self.r.gx("h2-b", ring(dirmode="755", files=(("a1b2.ring", 3000000, "644"),)), t0=T + 540)
        got, why = self.v("H2")
        self.assertEqual(got, "FAIL")
        self.assertIn("0750", why)
        self.assertIn("0640", why)

    def test_h2_listings_too_close_is_invalid(self):
        self.r.gx("h2-b", ring(), t0=T + 60)
        self.assertVerdict("H2", "INVALID RUN", "less than 2 minutes")

    def test_h3_a_zero_where_the_core_was_away(self):
        _, body = R.Evidence(self.d).call("h3-post")
        for s in body["data"]["series"]:
            if s["chart"] == "cpu" and s["name"] == "total":
                for p in s["points"]:
                    if p[0] == T + 840:
                        p[1:] = [0, 0, 0]
        self.r.call("h3-post", 200, body, T + 1100)
        got, why = self.v("H3")
        self.assertEqual(got, "FAIL")
        self.assertIn("expected null", why)
        self.assertIn("never 0", why)

    def test_h3_earlier_minutes_lost(self):
        self.r.call("h3-post", 200, env(result("1h", 60, T - 2460, T + 1080, host_series(T + 960, T + 1080), alloc=4)), T + 1100)
        self.assertVerdict("H3", "FAIL", "no minute from before")

    def test_h3_an_outage_too_short_is_invalid(self):
        self.r.gx("h3-start", stamp(before=T + 850, after=T + 852), t0=T + 849)
        self.assertVerdict("H3", "INVALID RUN", "not one whole minute")

    def test_h4_never_fires(self):
        self.r.tsv("h4-fire", [(T + 80, 200, "no\t[]"), (T + 230, 200, "no\t[]")])
        self.assertVerdict("H4", "FAIL", "never became active")

    def test_h4_resolves_too_late(self):
        self.r.tsv("h4-resolve", [(T + 222, 200, "no\t{}"), (T + 420, 200, "yes\t[]")])
        self.assertVerdict("H4", "FAIL", "did not resolve within 3 min")

    def test_h4_bad_rules_form(self):
        self.r.call("h4-bad-m121", 400, {"success": 400, "message": "bad_rules", "data": None}, T + 41)
        self.assertVerdict("H4", "FAIL", "bad_rules: rules[0].minutes")

    def test_h4_defaults(self):
        self.r.call("h4-get", 200, env({"rules": rules(cpu=(True, 80, 10)), "active": []}), T + 40)
        self.assertVerdict("H4", "FAIL", "cpu_high")

    def test_h4_not_calm_is_invalid(self):
        self.r.tsv("h4-calm", [(T + 50, 200, 'no\t{"key":"cpu_high"}')])
        self.assertVerdict("H4", "INVALID RUN", "not calm")

    def test_h4_hub_category_off(self):
        self.r.call("h4-hub-get", 200, env({"categories": {"resources": False}}), T + 43)
        self.assertVerdict("H4-hub", "FAIL", "resources")

    def test_cost_above_two_percent(self):
        self.r.gx("cost-b", cpu(1180.0, (1000 + 540, 1036, 1036)), t0=T + 1800)
        self.assertVerdict("H5", "FAIL", "above 2.0 %")
        self.assertVerdict("A6", "PASS")

    def test_cost_a_restart_in_the_window_is_invalid(self):
        self.r.gx("cost-b", cpu(1180.0, (1036, 1036, 1036), pids=("101", "999", "303")), t0=T + 1800)
        self.assertVerdict("A6", "INVALID RUN", "started again")

    def test_cost_too_few_containers_is_invalid(self):
        self.r.gx("cost-b", cpu(1180.0, (1036, 1036, 1036), containers=3), t0=T + 1800)
        self.assertVerdict("A6", "INVALID RUN", "fewer than 10")

    def test_a1_not_busy_enough(self):
        self.r.call("a1-15m", 200, result("15m", 60, T - 360, T + 480, ctr_series(T + 420, T + 480, 30), alloc=4), T + 531)
        self.assertVerdict("A1", "FAIL", "above 50")

    def test_a1_a_series_without_source_written(self):
        extra = {"memory__swap": pts(T + 420, T + 480, 60, lambda t: 0)}
        self.r.call("a1-15m", 200, result("15m", 60, T - 360, T + 480, ctr_series(T + 420, T + 480, 97, extra=extra), alloc=4), T + 531)
        self.assertVerdict("A1", "FAIL", "memory/swap")

    def test_a1_listed_late(self):
        self.r.tsv("a1-seen", [(T + 445, 200, "yes\tcontainer:statproof=running")])
        self.assertVerdict("A1", "FAIL", "within 20 s")

    def test_a1_container_never_started_is_invalid(self):
        self.r.gx("a1-run", "", rc=125, t0=T + 419, t1=T + 420)
        self.assertVerdict("A1", "INVALID RUN", "never started")

    def test_a1_wrapped_body(self):
        _, body = R.Evidence(self.d).call("a1-list")
        self.r.call("a1-list", 200, env(body), T + 530)
        self.assertVerdict("A1", "FAIL", "bare")

    def test_a2_the_limit_is_not_the_allocation(self):
        self.r.call("a2-lim", 200, result("15m", 60, T - 360, T + 480, ctr_series(T + 480, T + 480, 48), alloc=4, notes=["no_cpu_limit"]), T + 535)
        self.assertVerdict("A2", "FAIL", "cpu_allocation")

    def test_a2_no_note_without_limit(self):
        self.r.call("a2-free", 200, result("15m", 60, T - 360, T + 480, ctr_series(T + 480, T + 480, 0), alloc=4, notes=[]), T + 536)
        self.assertVerdict("A2", "FAIL", "without a CPU limit")

    def test_a2_identity(self):
        _, body = R.Evidence(self.d).call("a1-list")
        body["items"][1]["id"] = "container:stproof-lim-1"
        self.r.call("a1-list", 200, body, T + 530)
        self.assertVerdict("A2", "FAIL", "app:stproof:lim:1")

    def test_a3_a_stop_is_a_crash(self):
        self.r.tsv("a3-stopped", [(T + 718, 200, "no\tcontainer:statproof=crashed container:stattrm=stopped"),
                                  (T + 720, 200, "yes\tcontainer:statproof=stopped container:stattrm=stopped container:statkill=stopped")])
        self.assertVerdict("A3", "FAIL", "crashed")

    def test_a3_not_resolved(self):
        self.r.tsv("a3-resolve", [(T + 725, 200, 'no\t{"key":"cpu_high"}')])
        self.assertVerdict("A3", "FAIL", "did not resolve")

    def test_a3_unexpected_exit_codes_are_invalid(self):
        self.r.gx("a3-stop", "exit\tstatproof exited 0\nexit\tstattrm exited 143\nexit\tstatkill exited 137\n", t0=T + 701)
        self.assertVerdict("A3", "INVALID RUN", "exit codes")

    def test_a3_rm_history_not_by_id(self):
        self.r.call("a3-gone", 404, {"message": "unknown_item"}, T + 763)
        self.assertVerdict("A3-rm", "FAIL", "history by id")

    def test_a3_rm_listed_with_numbers(self):
        _, body = R.Evidence(self.d).call("a3-list-rm")
        body["items"][-1]["cpu_percent"] = 0
        self.r.call("a3-list-rm", 200, body, T + 762)
        self.assertVerdict("A3-rm", "FAIL", "removed container")

    def test_a4_a_server_error_while_docker_is_away(self):
        self.r.tsv("a4-out", [(T + 1160, 500, "no\tinternal")])
        self.assertVerdict("A4", "FAIL", "500")

    def test_a4_the_last_items_are_accepted(self):
        self.r.tsv("a4-out", [(T + 1160, 200, "no\t-")])
        self.assertVerdict("A4", "PASS", "the last items")

    def test_a4_a_spike(self):
        post = ctr_series(T + 600, T + 1440, 48, gap=(T + 1140, T + 1200))
        for s in post:
            if s["name"] == "user":
                for p in s["points"]:
                    if p[0] == T + 1260:
                        p[1:] = [400, 400, 400]
        self.r.call("a4-post", 200, result("15m", 60, T + 600, T + 1440, post), T + 1500)
        self.assertVerdict("A4", "FAIL", "spike")

    def test_a4_zeros_instead_of_a_gap(self):
        self.r.call("a4-post", 200, result("15m", 60, T + 600, T + 1440, ctr_series(T + 600, T + 1440, 48, gap=(T + 1140,))), T + 1500)
        self.assertVerdict("A4", "FAIL", "expected null")

    def test_a4_not_back(self):
        self.r.tsv("a4-back", [(T + 1340, 200, "yes\tapp:stproof:lim:1=running")])
        self.assertVerdict("A4", "FAIL", "within 60 s")

    def test_a5_history_lost_by_the_restart(self):
        self.r.call("a5-post", 200, result("15m", 60, T + 720, T + 1560, ctr_series(T + 1560, T + 1560, 48)), T + 1580)
        self.assertVerdict("A5", "FAIL", "minutes lost")

    def test_v1_note_missing(self):
        vm15 = ser(cpu__total=pts(T + 420, T + 540, 60, lambda t: 100.0))
        self.r.call("v1-15m", 200, result("15m", 60, T - 240, T + 540, vm15, alloc=2, notes=[]), T + 601)
        self.assertVerdict("V1", "FAIL", "vm_memory_used_unavailable")

    def test_v1_no_cpu(self):
        self.r.call("v1-15m", 200, result("15m", 60, T - 240, T + 540, [], alloc=2, notes=["vm_memory_used_unavailable"]), T + 601)
        self.assertVerdict("V1", "FAIL", "cpu/total")

    def test_v1_wrong_vm(self):
        self.r.call("v1-list", 200, {"items": [{"id": "vm:other", "kind": "vm", "group": "Machines virtuelles"}], "notes": []}, T + 600)
        self.assertVerdict("V1", "FAIL", UUID)

    def test_v2_history_gone(self):
        self.r.call("v2-post", 200, result("1h", 60, T - 1080, T + 2040, ser(cpu__total=pts(T + 1980, T + 2040, 60, lambda t: 100.0))), T + 2050)
        self.assertVerdict("V2", "FAIL", "older than the boot")

    def test_v2_reboot_failed_is_invalid(self):
        self.r.w("v2-reboot.failed", "the guest did not come back\n")
        self.assertVerdict("V2", "INVALID RUN", "did not come back")

    def test_v3_items_with_no_domain(self):
        self.r.call("v3-list", 200, {"items": [{"id": "vm:" + UUID}], "notes": []}, T + 400)
        self.assertVerdict("V3", "FAIL", "items")

    def test_v3_a_file_written_with_no_domain(self):
        self.r.gx("v3-ring-b", ring(files=(("v.ring", 3000000, "640"),)), t0=T + 401)
        self.assertVerdict("V3", "FAIL", "history files")

    def test_v3_driver_not_opened_is_invalid(self):
        self.r.gx("v3-virsh", "error: failed to connect to the hypervisor\n", rc=1, t0=T + 15)
        self.assertVerdict("V3", "INVALID RUN", "could not be opened")

    def test_v3_read_too_early_is_invalid(self):
        self.r.call("v3-list", 200, {"items": [], "notes": []}, T + 100)
        self.assertVerdict("V3", "INVALID RUN", "3 minutes")

    def test_v4_no_gap(self):
        live = pts(T + 1900, T + 2145, 5, lambda t: 100.0)
        self.r.call("v4-live", 200, result("live", 5, T + 1250, T + 2145, ser(cpu__total=live), alloc=2), T + 2146)
        self.assertVerdict("V4", "FAIL", "no null point")

    def test_v4_a_missing_step_is_a_gap(self):
        live = [p for p in pts(T + 1900, T + 2145, 5, lambda t: 100.0) if not T + 2100 <= p[0] <= T + 2110]
        self.r.call("v4-live", 200, result("live", 5, T + 1250, T + 2145, ser(cpu__total=live), alloc=2), T + 2146)
        self.assertVerdict("V4", "PASS")

    def test_v4_a_spike(self):
        live = pts(T + 1900, T + 2145, 5, lambda t: None if T + 2100 <= t <= T + 2110 else (5000.0 if t == T + 2115 else 100.0))
        self.r.call("v4-live", 200, result("live", 5, T + 1250, T + 2145, ser(cpu__total=live), alloc=2), T + 2146)
        self.assertVerdict("V4", "FAIL", "spike")

    def test_v4_unit_down(self):
        self.r.gx("v4-unit", "failed\n", t0=T + 2145)
        self.assertVerdict("V4", "FAIL", "casaos-virt-management")

    def test_v5_bad_rules_form(self):
        self.r.call("v5-bad", 400, {"message": "bad_rules: threshold"}, T + 608)
        self.assertVerdict("V5", "FAIL", "bad_rules: rules[0].threshold")

    def test_v5_rules_not_kept(self):
        self.r.call("v5-get2", 200, {"rules": rules(), "active": []}, T + 607)
        self.assertVerdict("V5", "FAIL", "after the PUT")

    def test_c1_the_channel_works_but_no_alert(self):
        hook = [{"t": T + 47, "path": "/hook", "body": "This is a test message from ReCasaOS"}]
        self.r.gx("c1-hook-log", "".join(json.dumps(h) + "\n" for h in hook), t0=T + 2152)
        self.assertVerdict("C1", "FAIL", "host firing")

    def test_c1_nothing_arrived_is_unverified(self):
        self.r.gx("c1-hook-log", json.dumps({"t": T, "path": "/probe", "body": "probe"}) + "\n", t0=T + 2152)
        self.r.call("c1-hub-end", 200, env({"last_failure": {"at": "2027-01-15T08:00:00Z", "channel": "stats-proof", "error": "connection refused"}}), T + 2153)
        self.assertVerdict("C1", "UNVERIFIED", "connection refused")

    def test_c1_channel_refused_is_unverified(self):
        self.r.call("c1-hub-put", 400, env(None, 400, "invalid alerts settings"), T + 45)
        self.assertVerdict("C1", "UNVERIFIED", "refused")

    def test_c2_an_unexpected_restart(self):
        self.r.gx("c2-units-prereboot", units(nrestarts=1), t0=T + 1852)
        self.assertVerdict("C2", "FAIL", "restarted by systemd")

    def test_c2_a_panic(self):
        self.r.gx("c2-journal-end", journal(panics=2), t0=T + 2151)
        self.assertVerdict("C2", "FAIL", "panic")

    def test_c2_a_unit_down_at_the_end(self):
        self.r.gx("c2-units-end", units(state="failed"), t0=T + 2150)
        self.assertVerdict("C2", "FAIL", "at the end")


class Invalid(Base):
    def test_missing_evidence_is_invalid(self):
        self.r.rm("h1-1h.code")
        self.assertVerdict("H1", "INVALID RUN", "h1-1h.code")

    def test_a_missing_poll_is_invalid(self):
        self.r.rm("a4-out.tsv")
        self.assertVerdict("A4", "INVALID RUN", "a4-out.tsv")

    def test_an_aborted_run_fails_the_job(self):
        for name in os.listdir(self.d):
            os.remove(os.path.join(self.d, name))
        self.r.w("aborted", "the guest's preparation stopped\n")
        lines = R.judge(self.d)
        self.assertEqual(lines[0][:2], ("INVALID RUN", "run"))
        self.assertTrue(all(v != "PASS" for v, _, _ in lines))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(R.main(["x", self.d, "d12"]), 1)

    def test_a_harness_step_that_failed_is_invalid_not_fail(self):
        self.r.w("invalid.txt", "A1\tthe busy container did not start\n")
        self.r.call("a1-15m", 200, result("15m", 60, T - 360, T + 480, [], alloc=4), T + 531)
        self.assertVerdict("A1", "INVALID RUN", "did not start")

    def test_a_fail_fails_the_job(self):
        self.r.tsv("h4-fire", [(T + 80, 200, "no\t[]")])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(R.main(["x", self.d, "d12"]), 1)

    def test_a_body_that_is_not_json_is_no_pass(self):
        self.r.call("v1-15m", 200, "[1, 2", T + 601)
        self.assertVerdict("V1", "FAIL", "range=15m")


class Helper(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sp-helper-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def body(self, b):
        p = os.path.join(self.d, "b.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps(b))
        return p

    def test_items(self):
        p = self.body({"items": [am_item("container:a", "container", "running", "a"), am_item("container:b", "container", "stopped", "b")]})
        self.assertTrue(R.helper("items", p, "200", ["running", "container:a"]).startswith("yes"))
        self.assertTrue(R.helper("items", p, "200", ["running", "container:a", "container:b"]).startswith("no"))
        self.assertTrue(R.helper("items", p, "200", ["-", "container:a", "container:b"]).startswith("yes"))
        self.assertIn("container:c=absent", R.helper("items", p, "200", ["-", "container:c"]))
        self.assertTrue(R.helper("items", p, "503", ["-", "container:a"]).startswith("no"))

    def test_alerts_of_the_core_and_of_a_bare_body(self):
        firing = {"rules": [], "active": [{"key": "cpu_high", "since": "2027-01-15T08:00:00Z", "value": 95}]}
        for b in (firing, env(firing)):
            p = self.body(b)
            yes = R.helper("active", p, "200", ["cpu_high"])
            self.assertTrue(yes.startswith("yes\t"))
            self.assertEqual(json.loads(yes.split("\t", 1)[1])["value"], 95)
            self.assertTrue(R.helper("calm", p, "200", ["cpu_high"]).startswith("no"))
            self.assertTrue(R.helper("calm", p, "200", ["mem_high"]).startswith("yes"))

    def test_never_and_ok(self):
        p = self.body({"message": "stats_unavailable"})
        self.assertEqual(R.helper("never", p, "503", []), "no\tstats_unavailable")
        self.assertEqual(R.helper("ok", p, "200", []), "yes\t200")
        self.assertTrue(R.helper("ok", p, "502", []).startswith("no"))
        self.assertTrue(R.helper("ok", os.path.join(self.d, "none.json"), "000", []).startswith("no"))

    def test_vmid(self):
        v = os.path.join(self.d, "virsh.out")
        with open(v, "w") as f:
            f.write("uuid\t%s\ntest\t%s\ndone\n" % (UUID, UUID))
        self.assertEqual(R.vmid(v, ""), UUID)
        p = self.body({"items": [{"id": "vm:abc"}]})
        self.assertEqual(R.vmid(os.path.join(self.d, "nope"), p), "abc")
        self.assertEqual(R.vmid(os.path.join(self.d, "nope"), os.path.join(self.d, "nope")), "-")

    def test_full_minutes(self):
        self.assertEqual(R.full_minutes(T + 820, T + 960), [T + 840, T + 900])
        self.assertEqual(R.full_minutes(T + 10, T + 100), [])
        self.assertEqual(R.full_minutes(T, T + 60), [T])


if __name__ == "__main__":
    unittest.main()
