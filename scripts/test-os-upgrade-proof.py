#!/usr/bin/env python3
"""The proof has to be able to say no. A FAKE guest, with the core's own answers and a clock that only moves when the runner sleeps, plays the machine: the real runner script
(os-upgrade-proof.sh) is run against it, with stand-ins for ssh, for the QEMU monitor and for sleep, and what it records is handed to the real report
(os-upgrade-proof-report.py). Then the recordings are mutated, one fault at a time, and the report has to say what it should of each:

    python3 scripts/test-os-upgrade-proof.py

A healthy leg passes. Every verdict has a mutation in which only the thing it judges is wrong, and that mutation must fail by that name; a run that did not get as far as it should (the
runner stopped early, a snapshot of the guest is missing, an undo failed, the release has no engine, a mirror failed) must be INVALID and never green; what this guest cannot reach is NOT
TESTABLE, with its reason, and neither a pass nor a fail.

The fake plays the answers the real core gives (the envelope of every route, the status with its fields, the refusals with their codes, the plan of the measured box from
fixtures/plan/plan-needs_acceptance.json) and the states of the real reducer in the order the engine documents (docs/os-upgrade-beta.md of the core): refreshing, refreshed or
reboot_first; rehearsing, rehearsed, unwinding, rehearsal_done; running, awaiting_reboot, rebooting, resuming, verifying, succeeded; the abort, the kill, the stop at a waypoint, the
power cut. What the real tools, systemd, apt and the engine say on a real guest is what the first dispatch of the workflow shows: nothing here proves that.

REPORT=<path>, SCRIPT=<path> and WORKFLOW=<path> run the cases against other copies, to see that a broken one is caught. CASES=<regex> runs the checks whose name matches (and the scenarios;
the runs of the healthy legs and the mutations are made all the same: SCENARIOS_ONLY=1 leaves them out, and makes the healthy faults leg only when a scenario of the faults needs it). STATIC_ONLY=1 runs
the checks of the files only: the guest tools under dash with stand-ins, the workflow against GitHub's table of contexts, the legs file.
KEEP=<dir> keeps the recordings of the two healthy runs there; with REPORT_ONLY=1 they are used again instead of being made (only the mutations of the recordings are then run: minutes, not
the half hour that the runs of the script take), and BASE_ONLY=1 stops after the mutations, before the scenarios. SHELLCHECK=1 also runs shellcheck on the script (the workflow does not).
TEST_BASH=<path> names a bash where plain `bash` is not the one (on Windows: the MSYS bash of Git, C:/Program Files/Git/usr/bin/bash.exe, with a PATH that has no WindowsApps stub of
python3: this harness puts a `python3` of its own, and the directories of that bash's tools, first in the PATH of the script).
"""
import copy
import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.environ.get("REPORT") or os.path.join(HERE, "os-upgrade-proof-report.py")
SCRIPT = os.environ.get("SCRIPT") or os.path.join(HERE, "os-upgrade-proof.sh")
WORKFLOW = os.environ.get("WORKFLOW") or os.path.join(HERE, "..", ".github", "workflows", "os-upgrade-proof.yml")
LEGS_JSON = os.path.join(HERE, "os-upgrade-proof-legs.json")
FIXTURES = os.path.join(HERE, "fixtures", "plan")
ONLY = os.environ.get("CASES")
BASH = os.environ.get("TEST_BASH") or "bash"
KEEP = os.environ.get("KEEP")
sys.dont_write_bytecode = True
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
        print("FAIL: " + label + (": " + str(detail)[:400] if detail else ""))


# ---- the answers of the core: its own shapes -----------------------------------------------------------------------------------------------

EPOCH0 = 1790000000.0
KERNEL_11 = "5.10.0-35-cloud-amd64"
KERNEL_11_NEW = "5.10.0-36-cloud-amd64"
KERNEL_12 = "6.1.0-40-cloud-amd64"
PLAN_ID = "p1-5787acb2f81006785a3076312615ae6c"
UNITS = ("apt-daily.timer", "apt-daily-upgrade.timer", "unattended-upgrades.service")
HOP_BLOCKS = [("verify", "check that this is the machine of the plan"), ("timers", "stop the timers of apt"), ("snapshot", "write down the state of the machine"),
              ("sources", "write the sources of the next release"), ("download", "download the minimal upgrade"), ("minimal", "the minimal upgrade"), ("full", "the full upgrade"),
              ("prereboot", "check that the machine will start")]
REFRESH_BLOCKS = [("rcheck", "check that the old release can be brought up to date"), ("rsources", "keep a copy of the sources"), ("rupgrade", "bring the packages to their last state"),
                  ("rdone", "put the sources and the timers back")]
ACTIONS = {"refreshing": [], "rehearsing": [], "running": [], "refreshed": ["acknowledge"], "reboot_first": ["reboot", "acknowledge"], "rehearsed": ["discard"], "rehearsal_done": ["acknowledge"],
           "awaiting_reboot": ["reboot", "takeover"], "succeeded": ["acknowledge"], "succeeded_with_findings": ["acknowledge", "resume"], "idle": [], "unwinding": [], "recovering": [],
           "resuming": [], "verifying": [], "rebooting": []}


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def load_plan():
    with open(os.path.join(FIXTURES, "plan-needs_acceptance.json"), encoding="utf-8") as f:
        return json.load(f)["data"]


def iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


KEEP_KEYS = ("lock", "snapshots", "cfg", "log", "faultlog", "starts")      # what a restore of the guest does not take back: the test's own records
_NAMES_SKIP = []


def names_skip():
    """the filter that the guest tool of the script puts on the names of the file system (logind makes a scope, a slice and drop-ins for every login, under a new number): the fake plays the tool, so it applies the script's own;
    a script without one leaves them in"""
    if not _NAMES_SKIP:
        with open(SCRIPT, encoding="utf-8", newline="") as f:
            m = re.search(r"^NAMES_SKIP='([^']*)'", f.read().replace("\r\n", "\n"), re.M)
        _NAMES_SKIP.append(m.group(1) if m else "")
    return _NAMES_SKIP[0]


class Seg:
    """one stretch of a run: the state the status says while it lasts (dur seconds, None for the last), what is made true when it begins (enter), and the fields the status has then"""

    def __init__(self, state, dur=None, block=None, enter=None, **kw):
        self.state, self.dur, self.block, self.enter, self.kw, self.since = state, dur, block, enter, kw, 0.0


class World:
    """the guest and its core, as the runner sees them: the clock moves only when the runner sleeps (tick)"""

    def __init__(self, cfg=None):
        # what a real guest does that a tidy one does not: a login makes a scope of its own, the clock of a restored guest is that of the snapshot, an ssh is missed now and then (the first call of
        # each of these verbs, after every restore), the unit's death is seen by the core one look later and the interruption lasts one look, a start takes a minute and a half to answer
        self.cfg = dict(resume_fail_class="dpkg_broken", refresh_wants_reboot=True, refresh_touches_keyring=True, findings=False, block_seconds=dict(verify=3, timers=3, snapshot=6, sources=33, download=15, minimal=90, full=150,
                                                                                                                       prereboot=21), down_seconds=40, token_valid_s=10 ** 9,
                        restore_lag=1800, ssh_drops={"kill_unit": 1, "route_cut": 1, "set_clock": 1, "dpkg_wait": 1}, ssh_lost={}, start_takes_s=90, interrupted_s=1, notice_s=2)
        self.cfg.update(cfg or {})
        self.session_n, self.clock_lag, self.unit_gen, self.last_state = 0, 0.0, 0, ""
        self.ssh_calls, self.faultlog, self.starts, self.delay_taken = {}, [], [], 0.0
        self.run_info, self.run_ids = {}, []
        self.t = 0.0
        self.lock = threading.RLock()
        self.boot = str(uuid.uuid4())
        self.down_until = 0.0
        self.reboot_at = None
        self.hostname = "oup-1a2b3c4d"
        self.version_id, self.kernel = "11", KERNEL_11
        self.beta = False
        self.run, self.run_n, self.closed_runs = None, 0, []
        self.apt = {"/etc/apt/sources.list": sha("deb bullseye"), "/etc/apt/sources.list.d/docker.list": sha("deb docker"), "/etc/apt/apt.conf.d/99oup-quiet": sha("quiet"),
                    "/etc/apt/apt.conf.d/20auto-upgrades": sha("auto"), "/etc/apt/trusted.gpg.d/debian-archive-bullseye-stable.gpg": sha("key1"), "/etc/apt/preferences.d/none": sha("none")}
        self.sources_text = "bullseye"
        self.timers = {u: ("active", "enabled") for u in UNITS}
        self.flag, self.run_dir, self.bk_dirs, self.run_bk, self.timers_state = False, False, [], [], False
        self.dpkg = {"libc6": ("2.31-13+deb11u7", "ii "), "systemd": ("247.3-7+deb11u4", "ii "), "apt": ("2.2.4", "ii "), "dash": ("0.5.11+git20200708+dd9ef66-5", "ii "), "fuse": ("2.9.9-5", "ii "),
                     "openssh-server": ("1:8.4p1-5+deb11u2", "ii "), "docker-ce": ("5:28.0.4-1~debian.11~bullseye", "ii "), "linux-image-" + KERNEL_11: ("5.10.209-2", "ii "),
                     "curl": ("7.74.0-1.3+deb11u11", "ii "), "samba": ("2:4.13.13+dfsg-1~deb11u6", "ii ")}
        self.services = {"ssh.service": "active", "casaos.service": "active", "casaos-gateway.service": "active", "casaos-app-management.service": "active",
                         "casaos-user-service.service": "active", "docker.service": "active", "containerd.service": "active", "smbd.service": "active", "cron.service": "active"}
        self.witnesses = ["p-always", "p-db", "p-unless-stopped", "p-web"]
        self.failed_units, self.pid1, self.units = [], [], []
        self.base_names = ["/etc/apt/%s" % n for n in ("sources.list", "apt.conf.d")] + ["/etc/casaos", "/var/lib/casaos", "/var/lib/casaos/db", "/var/backups", "/var/lib/dpkg",
                                                                                           "/var/lib/dpkg/status", "/var/lib/apt/lists", "/var/cache/apt/archives", "/run/systemd/system"]
        self.noise, self.noise_tick = 0, 0
        self.lock_held, self.route_cut = False, False
        self.cache_bytes, self.rx, self.df_root, self.df_boot = 120 << 20, 5 << 30, 18 << 20, 300 << 10
        self.reboot_required = False
        self.last_refresh = None
        self.plan_job = None          # None, or dict(state, started, plan)
        self.plan_live = None
        self.events = []
        self.token, self.token_born = None, 0.0
        self.pw, self.user = None, None
        self.snapshots = {}
        self.log = []
        self.markers_text = ""

    # ---- the clock ----------------------------------------------------------------------------------------------------------------------

    def now(self):
        return EPOCH0 + self.t

    def up(self):
        return self.t >= self.down_until

    def tick(self, dt):
        """the clock moves, a second at a time: what is due happens in its order"""
        with self.lock:
            n = int(dt)
            for _ in range(n):
                self.step(1.0)
            if dt - n > 1e-9:
                self.step(dt - n)

    def step(self, d):
        self.t += d
        if self.run is not None:
            self.run["elapsed"] += d
        self.fire()

    def fire(self):
        if self.reboot_at is not None and self.t >= self.reboot_at:
            at, self.reboot_at = self.reboot_at, None
            self.do_reboot(at)
        due = [e for e in self.events if e[0] <= self.t]
        self.events = [e for e in self.events if e[0] > self.t]
        for _, name in sorted(due, key=lambda e: e[0]):
            getattr(self, name)()
        if self.plan_job and self.plan_job["state"] == "running" and self.t >= self.plan_job["done_at"]:
            self.plan_job["state"] = "done" if not self.cfg.get("plan_fails") else "failed"
            self.plan_live = self.plan_job
        r = self.run
        while r is not None:
            seg = r["segs"][r["i"]]
            if seg.dur is None or r["elapsed"] + 1e-9 < seg.dur:
                break
            r["elapsed"] -= seg.dur
            r["i"] += 1
            self.begin(r["segs"][r["i"]])
            r = self.run

    def at(self, delay, fn):
        self.events.append((self.t + delay, fn))

    def begin(self, seg):
        seg.since = self.now()
        if seg.state in ("unwinding", "recovering") and self.last_state != seg.state:
            self.unit_gen += 1          # the unwind and the repair run as a unit of their own, under the same name
        self.last_state = seg.state
        if seg.enter:
            getattr(self, seg.enter)()

    def go(self, segs):
        """the run goes on with these stretches from now on"""
        r = self.run
        r["segs"], r["i"], r["elapsed"] = segs, 0, 0.0
        self.begin(segs[0])

    def cur(self):
        return self.run["segs"][self.run["i"]] if self.run else None

    # ---- the machine ----------------------------------------------------------------------------------------------------------------

    def do_reboot(self, at):
        """the guest goes away and comes back with another boot id: what the new release brings is on it"""
        self.boot = str(uuid.uuid4())
        self.down_until = at + self.cfg["down_seconds"]
        self.rx = 40 << 20
        self.lock_held = False
        r = self.run
        if r and r["kind"] == "hop" and getattr(self, "reboot_for_hop", False):
            self.reboot_for_hop = False
            self.version_id, self.kernel = "12", KERNEL_12
            self.sources_text = "bookworm"
            for n, (v, st) in list(self.dpkg.items()):
                self.dpkg[n] = (v.replace("deb11", "deb12"), st)
            self.dpkg["linux-image-" + KERNEL_12] = ("6.1.158-1", "ii ")
            for k in ("libssl3", "python3.11", "libperl5.36"):
                self.dpkg[k] = ("3.0.17-1~deb12u3", "ii ")
            self.go([Seg("rebooting", self.cfg["down_seconds"], **self.f(state="rebooting", pnr=True)), Seg("resuming", 15, **self.f(state="resuming", pnr=True)),
                     Seg("verifying", 20, enter="verifying", **self.f(state="verifying", pnr=True))] + self.verdict_segs())
        elif r and r["kind"] == "refresh" and getattr(self, "reboot_for_refresh", False):
            self.reboot_for_refresh = False
            self.kernel = KERNEL_11_NEW
            self.reboot_required = False
            self.go([Seg("rebooting", self.cfg["down_seconds"], **self.f(state="rebooting")), Seg("refreshed", None, enter="release_flag_later", **self.f(state="refreshed"))])
        if self.cfg.get("no_boot_back") and self.run is not None and self.run["kind"] == "hop":
            self.down_until = 10 ** 12

    def verifying(self):
        self.timers = {u: ("active", "enabled") for u in UNITS}
        self.timers_state = False

    def release_flag_later(self):
        self.at(25, "drop_flag")

    def drop_flag(self):
        """the core's look at the run lets go of the flag once the state does not hold the machine"""
        if not self.open_run() or not self.cur().kw.get("busy", False):
            self.flag = False

    def f(self, **kw):
        """the fields of a stretch: what the status says by default for a state"""
        state = kw.pop("state")
        busy = state not in ("idle", "refreshed", "rehearsal_done", "succeeded", "succeeded_with_findings")
        base = dict(pnr=False, half=False, busy=busy, actions=list(ACTIONS.get(state, [])))
        base.update(kw)
        return base

    def verdict_segs(self):
        if self.cfg["findings"] == "mixed":
            # what the judge most likely says of a Debian 11 to 12 hop: information (the paused source of Docker, a file dpkg left as .dpkg-new) and a few warnings
            return [Seg("succeeded_with_findings", None, enter="release_flag_later",
                        **self.f(state="succeeded_with_findings", pnr=True, busy=False, not_judged=["ports"], findings=[
                            {"code": "docker_source_paused", "severity": "info"}, {"code": "dpkg_dist_new", "severity": "info", "arg": "/etc/ssh/sshd_config"},
                            {"code": "ports_missing", "severity": "warn", "arg": "445/tcp"}, {"code": "units_failed_new", "severity": "warn", "arg": "x.service"}, {"code": "facts_incomplete", "severity": "warn"}]))]
        if self.cfg["findings"]:
            return [Seg("succeeded_with_findings", None, enter="release_flag_later",
                        **self.f(state="succeeded_with_findings", pnr=True, findings=[{"code": "docker_source_unreachable", "severity": "warn"}], busy=False))]
        return [Seg("succeeded", None, enter="release_flag_later", **self.f(state="succeeded", pnr=True, busy=False))]

    # ---- the runs ------------------------------------------------------------------------------------------------------------------------

    def new_run(self, kind, plan_id, segs):
        self.run_n += 1
        self.run = dict(id="0b1c2d3e-%04d-4a5b-8c9d-0e1f2a3b4c5d" % self.run_n, kind=kind, plan_id=plan_id, segs=segs, i=0, elapsed=0.0, closed=False, started=self.now())
        self.run_ids.append(self.run["id"])
        self.run_info[self.run["id"]] = {"bk": None}
        self.unit_gen += 1
        self.run_dir, self.flag = True, True
        self.begin(segs[0])
        return self.run

    def hop_segs(self, rehearsal):
        bs = self.cfg["block_seconds"]
        blocks = HOP_BLOCKS[:5] if rehearsal else HOP_BLOCKS
        segs = []
        for i, (bid, title) in enumerate(blocks, 1):
            pnr = bid in ("minimal", "full", "prereboot")
            enter = {"timers": "stop_timers", "snapshot": "make_bk", "sources": "sources_begin", "minimal": "pnr_on"}.get(bid)
            segs.append(Seg("rehearsing" if rehearsal else "running", bs[bid], block=(i, bid, title), enter=enter,
                            **self.f(state="rehearsing" if rehearsal else "running", pnr=pnr, half=pnr and bid != "prereboot", actions=[] if pnr else ["abort"])))
        if rehearsal:
            segs.append(Seg("rehearsed", None, **self.f(state="rehearsed", expires=True, actions=["discard"])))
        else:
            segs.append(Seg("awaiting_reboot", None, enter="awaiting", **self.f(state="awaiting_reboot", pnr=True, barrier=True)))
        return segs

    def stop_timers(self):
        self.timers = {u: ("inactive", "enabled") for u in UNITS}
        self.timers_state = True

    def make_bk(self):
        self.bk_dirs = ["/var/backups/casaos-os-upgrade-" + (self.run["plan_id"] or "x")]
        self.run_info[self.run["id"]]["bk"] = self.bk_dirs[0]

    def sources_begin(self):
        """block 3 writes the sources of Debian 12 and runs the first apt-get update --error-on=any against them: a Release file dated later than the clock of the guest is not valid yet, and stops it"""
        self.write_sources()
        if self.clock_lag > 120:
            failure = {"stage": "sources", "class": "apt_failed", "rc": 100,
                       "detail_line": "E: Release file for http://deb.debian.org/debian/dists/bookworm/InRelease is not valid yet (invalid for another 31min 2s). Updates for this repository will not be applied."}
            self.go([Seg("unwinding", 6, **self.f(state="unwinding")), Seg("failed", None, enter="put_back", **self.f(state="failed", reason="apt_failed", failure=failure, busy=False, actions=["acknowledge", "discard"]))])

    def write_sources(self):
        self.sources_text = "bookworm"
        self.apt["/etc/apt/sources.list"] = sha("deb bookworm")
        self.apt.pop("/etc/apt/sources.list.d/docker.list", None)
        self.apt["/etc/apt/sources.list.d/docker.list.casaos-paused"] = sha("deb docker")

    def pnr_on(self):
        self.dpkg["libc6"] = ("2.36-9+deb12u10", "ii ")
        self.cache_bytes += 900 << 20
        self.rx += 1400 << 20

    def awaiting(self):
        self.cache_bytes += 100 << 20
        self.df_root -= 4 << 20
        self.barrier_ok = False
        self.at(25, "barrier_ready")

    def barrier_ready(self):
        self.barrier_ok = True

    def put_back(self):
        """what the unwind puts back: the sources, the timers, the folder of the backup"""
        self.sources_text = "bullseye"
        self.apt["/etc/apt/sources.list"] = sha("deb bullseye")
        self.apt.pop("/etc/apt/sources.list.d/docker.list.casaos-paused", None)
        self.apt["/etc/apt/sources.list.d/docker.list"] = sha("deb docker")
        if not self.cfg.get("timers_stay_stopped"):
            self.timers = {u: ("active", "enabled") for u in UNITS}
        self.timers_state = False
        self.run_bk = [d for d in self.run_bk] + ["/var/lib/casaos/os-upgrade/%s/bk-aborted" % self.run["id"]]
        self.bk_dirs = []
        self.at(25, "drop_flag")

    # ---- the status ------------------------------------------------------------------------------------------------------------------------

    def status(self):
        s = {"beta_enabled": self.beta, "state": "idle", "hop": "", "attempt": 0, "pnr": False, "reversible": True, "half_upgraded": False, "busy": False, "since": None, "last_activity_at": None,
             "manual": {"blocks_done": [], "pnr": False, "bk_path": ""}, "actions": [], "boot_unknown": False, "ignored_markers": 0, "log_tail": []}
        r = self.run
        if r is None or r["closed"]:
            return s
        seg = self.cur()
        k = seg.kw
        s.update(state=seg.state, run_id=r["id"], kind=r["kind"], hop="debian-11-12", plan_id=r["plan_id"], phase={"refreshing": "refresh", "rehearsing": "rehearse", "running": "run"}.get(seg.state, ""),
                 attempt=k.get("attempt", 1), pnr=k.get("pnr", False), reversible=not k.get("pnr", False), half_upgraded=k.get("half", False), busy=k.get("busy", False),
                 since=iso(seg.since), last_activity_at=iso(self.now()), actions=list(k.get("actions", [])))
        for key in ("reason", "cause"):
            if k.get(key):
                s[key] = k[key]
        if seg.block:
            s["block"] = {"index": seg.block[0], "id": seg.block[1], "title": seg.block[2]}
        if k.get("expires"):
            s["expires_at"] = iso(seg.since + 1800)
        if k.get("failure"):
            s["failure"] = k["failure"]
        if seg.state == "awaiting_reboot" and not getattr(self, "barrier_ok", True):
            s["actions"] = [a for a in s["actions"] if a != "reboot"]
        elif k.get("barrier"):
            s["barrier"] = {"ok": True, "checks": [{"code": "fstab", "ok": True, "severity": "refuse"}, {"code": "initrd_size", "ok": True, "severity": "warning"}]}
            s["after_reboot_hint"] = {"bk_path": self.bk_dirs[0] if self.bk_dirs else "", "log": "/var/lib/casaos/os-upgrade/%s/output.log" % r["id"], "commands": ["uname -r"], "block8": ""}
        if k.get("findings"):
            s["findings"] = k["findings"]
        if k.get("not_judged"):
            s["not_judged"] = k["not_judged"]
        if k.get("pnr") and seg.state in ("succeeded", "succeeded_with_findings"):
            s["half_upgraded"] = False
        if seg.state == "interrupted" and k.get("half"):
            s["manual"]["pnr"] = True
        return s

    # ---- the routes ------------------------------------------------------------------------------------------------------------------------

    def answer(self, status, data, message=None):
        return status, {"success": status, "message": message or ("accepted" if status == 202 else "ok"), "data": data}

    def refuse(self, code, detail, data=None, status=409):
        d = {"error_code": code, "error": detail}
        if data:
            d["data"] = data
        return status, {"success": status, "message": detail, "data": d}

    def open_run(self):
        return self.run is not None and not self.run["closed"]

    def route(self, method, path, auth, body):
        """(HTTP status, JSON) of a request to the gateway; None when the guest does not answer"""
        with self.lock:
            if not self.up():
                return None
            self.log.append((method, path))
            if path == "/v1/users/status":
                return self.answer(200, {"key": "reg-key-1", "initialized": self.user is not None})
            if path == "/v1/users/register":
                req = json.loads(body or "{}")
                if req.get("key") != "reg-key-1":
                    return self.refuse("bad_key", "bad key", status=400)
                self.user, self.pw = req.get("username"), req.get("password")
                return self.answer(200, {"id": 1})
            if path == "/v1/users/login":
                req = json.loads(body or "{}")
                if self.user is None or (req.get("username"), req.get("password")) != (self.user, self.pw):
                    return self.refuse("login_failed", "wrong user or password", status=400)
                self.token, self.token_born = "tok-%d" % (len(self.log)), self.t
                return self.answer(200, {"token": {"access_token": self.token, "refresh_token": "refresh-" + self.token}, "user": {"username": self.user}})
            if auth != self.token or self.t - self.token_born > self.cfg["token_valid_s"]:
                return 401, {"success": 401, "message": "token expired or not valid"}
            if path == "/v1/sys/os/upgrade/status" and method == "GET":
                return self.answer(200, self.status())
            if path == "/v1/sys/os/upgrade/plan":
                return self.plan_route(method)
            if path == "/v1/sys/state/restart" and method == "PUT":
                return self.put_restart()
            handler = None
            if path.startswith("/v1/sys/os/upgrade/") and method == "POST":
                handler = {"beta": self.post_beta, "refresh": self.post_refresh, "start": self.post_start, "abort": self.post_abort, "ack": self.post_ack, "reboot": self.post_reboot,
                           "resume": self.post_resume}.get(path.rsplit("/", 1)[-1])
            if handler is None:
                return 404, {"success": 404, "message": "not found"}
            try:
                req = json.loads(body) if body else {}
            except ValueError:
                return self.refuse("bad_request", "a field of the request is not valid: body", {"field": "body"}, status=400)
            return handler(req)

    def plan_route(self, method):
        if method == "POST":
            if self.lock_held:
                return self.refuse("busy", "another package manager is at work: dpkg is locked by another process")
            if self.plan_job and self.plan_job["state"] == "running":
                return self.answer(200, {"plannable": True, "state": "running", "phase": "update", "started_at": iso(self.plan_job["started"]), "already_running": True}, "ok")
            self.plan_job = dict(state="running", started=self.now(), done_at=self.t + 24, plan=None)
            self.plan_live = None
            return self.answer(202, {"plannable": True, "state": "running", "phase": "sources", "started_at": iso(self.now())})
        j = self.plan_live or self.plan_job
        if j is None:
            return self.answer(200, {"plannable": True, "state": "idle"})
        if j["state"] == "running":
            return self.answer(200, {"plannable": True, "state": "running", "phase": "update", "started_at": iso(j["started"])})
        if j["state"] == "failed":
            return self.answer(200, {"plannable": True, "state": "failed", "phase": "update", "started_at": iso(j["started"]), "finished_at": iso(self.now()),
                                     "error": {"code": "sandbox_update_failed", "phase": "update", "detail": "Err:5 http://deb.debian.org bookworm InRelease  Could not resolve deb.debian.org",
                                               "data": {"exit": 100, "kind": "network"}}})
        data = copy.deepcopy(load_plan())
        data["state"] = "done"
        if self.cfg.get("plan_blocked"):
            data["plan"]["verdict"] = "blocked"
            data["plan"]["problems"] = [{"code": "dist_kept_back", "severity": "blocked", "data": {"names": ["base-files"]}}]
        data["plan"]["plan_id"] = PLAN_ID
        return self.answer(200, data)

    def post_beta(self, req):
        if not isinstance(req.get("enabled"), bool):
            return self.refuse("bad_request", "a field of the request is not valid: enabled", {"field": "enabled"}, status=400)
        if not req["enabled"] and self.open_run():
            return self.refuse("run_open", "an upgrade is open", {"state": self.cur().state, "run_id": self.run["id"]})
        self.beta = req["enabled"]
        return self.answer(200, {"beta_enabled": self.beta}, "ok")

    def readiness_refusal(self):
        if self.lock_held and not self.cfg.get("lock_ignored"):
            return self.refuse("readiness_refuses", "the machine is not ready for the upgrade", {"checks": [{"code": "busy", "action": "wait"}], "state": "ready"})
        return None

    def post_refresh(self, req):
        if not self.beta and not self.cfg.get("start_ignores_beta"):
            return self.refuse("beta_off", "the upgrade of the operating system is not switched on")
        if self.open_run():
            return self.refuse("run_open", "an upgrade is open: it has to be finished or closed first", {"state": self.cur().state, "run_id": self.run["id"]})
        refusal = self.readiness_refusal()
        if refusal:
            return refusal
        self.take_time()
        bs = dict(rcheck=5, rsources=30, rupgrade=120)
        segs = []
        for i, (bid, title) in enumerate(REFRESH_BLOCKS[:3], 1):
            enter = {"rcheck": "stop_timers", "rsources": "make_refresh_bk", "rupgrade": "upgrade_old"}[bid]
            segs.append(Seg("refreshing", bs[bid], block=(i, bid, title), enter=enter, **self.f(state="refreshing", pnr=False, actions=["abort"] if bid != "rupgrade" else [])))
        segs.append(Seg("refreshing", 5, block=(4, "rdone", REFRESH_BLOCKS[3][1]), enter="refresh_done", **self.f(state="refreshing", actions=[])))
        if self.cfg["refresh_wants_reboot"]:
            segs.append(Seg("reboot_first", None, **self.f(state="reboot_first")))
            self.reboot_for_refresh = True
        else:
            segs.append(Seg("refreshed", None, enter="release_flag_later", **self.f(state="refreshed")))
        self.new_run("refresh", None, segs)
        return self.answer(202, self.status())

    def take_time(self):
        """the facts of the machine are taken before the run begins, and the call waits for them: the time goes by, and the handler learns how long the caller waited"""
        d = float(self.cfg.get("start_takes_s", 0))
        if d:
            self.tick(d)
        self.delay_taken = d

    def make_refresh_bk(self):
        self.bk_dirs = ["/var/backups/casaos-os-upgrade-refresh-" + self.run["id"][:8]]
        self.run_info[self.run["id"]]["bk"] = self.bk_dirs[0]

    def upgrade_old(self):
        for n, (v, st) in list(self.dpkg.items()):
            if n in ("libc6", "systemd", "apt", "curl", "openssh-server"):
                self.dpkg[n] = (v + "1", st)
        self.dpkg["linux-image-" + KERNEL_11_NEW] = ("5.10.216-1", "ii ")
        self.reboot_required = self.cfg["refresh_wants_reboot"]
        self.cache_bytes += 200 << 20
        if self.cfg["refresh_touches_keyring"]:
            self.apt["/etc/apt/trusted.gpg.d/debian-archive-bullseye-stable.gpg"] = sha("key2")

    def refresh_done(self):
        self.timers = {u: ("active", "enabled") for u in UNITS}
        self.timers_state = False
        self.bk_dirs = []
        self.run_bk.append("/var/lib/casaos/os-upgrade/%s/bk-done" % self.run["id"])
        self.last_refresh = self.now()

    def post_start(self, req):
        pid = req.get("plan_id")
        host = req.get("hostname")
        if not isinstance(pid, str) or not re.fullmatch(r"p1-[0-9a-f]{32}", pid):
            return self.refuse("bad_request", "a field of the request is not valid: plan_id", {"field": "plan_id"}, status=400)
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
            return self.refuse("bad_request", "a field of the request is not valid: hostname", {"field": "hostname"}, status=400)
        if not self.beta and not self.cfg.get("start_ignores_beta"):
            return self.refuse("beta_off", "the upgrade of the operating system is not switched on")
        if self.open_run():
            return self.refuse("run_open", "an upgrade is open: it has to be finished or closed first", {"state": self.cur().state, "run_id": self.run["id"]})
        j = self.plan_live
        if j is None or j["state"] != "done":
            return self.refuse("no_plan", "there is no plan to start from (idle)", {"reason": "idle"})
        if pid != PLAN_ID:
            return self.refuse("plan_changed", "the plan is not the one that was read", {"current": PLAN_ID})
        plan = load_plan()["plan"]
        if self.cfg.get("plan_blocked"):
            return self.refuse("plan_blocked", "the plan has problems that nothing can accept", {"problems": []})
        if self.last_refresh and self.last_refresh > self.plan_job["started"] and not self.cfg.get("ignore_refresh_age"):
            return self.refuse("plan_older_than_refresh", "the old release was refreshed after the plan was made")
        reh = bool(req.get("rehearsal"))
        if not (reh and req.get("safety_net", "") == "") and req.get("safety_net") not in ("snapshot", "console_and_backup", "reinstall_ok"):
            return self.refuse("bad_safety_net", "the safety net is not one of the three", {"allowed": ["snapshot", "console_and_backup", "reinstall_ok"]})
        if host != self.hostname:
            return self.refuse("hostname_mismatch", "the name typed is not the name of this machine")
        paused = sorted({x["file"] + (":%d" % x["line"] if x.get("line", 0) > 0 else "") for x in plan["paused"]})
        removals = sorted({p["data"]["name"] for p in plan["problems"] if p["code"] == "removal_manual"})
        need = ["no_interruption"] + ([] if reh else ["no_way_back"]) + (["sources_paused"] if paused else []) + (["removals_read"] if removals else [])
        missing = [a for a in need if a not in (req.get("acks") or [])]
        if missing:
            return self.refuse("acks_missing", "what the plan asks has not been acknowledged", {"missing": missing})
        if sorted(req.get("accept_removals") or []) != removals:
            return self.refuse("removals_mismatch", "the removals that were accepted are not the plan's", {"missing": [], "extra": []})
        if sorted(req.get("paused_sources") or []) != paused:
            return self.refuse("paused_mismatch", "the sources that were put aside are not the plan's", {"missing": [], "extra": []})
        refusal = self.readiness_refusal()
        if refusal:
            return refusal
        self.take_time()
        self.starts.append((reh, req.get("safety_net")))
        self.new_run("rehearsal" if reh else "hop", PLAN_ID, self.hop_segs(reh))
        return self.answer(202, self.status())

    def post_abort(self, req):
        if not self.open_run():
            return self.refuse("no_run", "there is no upgrade of the operating system to act on")
        seg = self.cur()
        if seg.state not in ("refreshing", "rehearsing", "running"):
            return self.refuse("not_abortable", "the run cannot be called off", {"reason": "not_running"})
        if seg.kw.get("pnr"):
            return self.refuse("not_abortable", "the run cannot be called off", {"reason": "point_of_no_return"})
        if "abort" not in seg.kw.get("actions", []) or self.cfg.get("abort_refused"):
            return self.refuse("not_abortable", "the run cannot be called off", {"reason": "past_block"})
        block = seg.block[1] if seg.block else ""
        failure = {"stage": block, "class": "abort", "rc": 0, "detail_line": "STOP: the call-off was asked for"}
        self.go(self.noticed(seg) + [Seg("interrupted", self.cfg["interrupted_s"], **self.f(state="interrupted", cause="killed", actions=[])), Seg("unwinding", 6, **self.f(state="unwinding")),
                 Seg("failed", None, enter="put_back", **self.f(state="failed", reason="abort", failure=failure, busy=False, actions=["acknowledge", "discard"]))])
        return self.answer(202, self.status())

    def noticed(self, seg):
        """the unit is gone, and the core has not looked yet: the state is still the one it was"""
        return [Seg(seg.state, self.cfg["notice_s"], block=seg.block, **dict(seg.kw))]

    def post_ack(self, req):
        if not self.open_run():
            return self.refuse("no_run", "there is no upgrade of the operating system to act on")
        action, seg = req.get("action"), self.cur()
        if action not in ("acknowledge", "discard", "manual"):
            return self.refuse("bad_request", "a field of the request is not valid: action", {"field": "action"}, status=400)
        if action == "discard" and seg.state == "rehearsed":
            self.go([Seg("unwinding", 6, **self.f(state="unwinding")), Seg("rehearsal_done", None, enter="put_back", **self.f(state="rehearsal_done", busy=False))])
            return self.answer(200, self.status(), "ok")
        if seg.state in ("refreshed", "rehearsal_done", "reboot_first", "succeeded", "succeeded_with_findings") or (seg.state == "failed" and not seg.kw.get("pnr")):
            if self.run["kind"] == "hop" and seg.state in ("succeeded", "succeeded_with_findings"):
                self.bk_dirs = []
                self.run_bk.append("/var/lib/casaos/os-upgrade/%s/bk-done" % self.run["id"])
            self.run["closed"] = True
            return self.answer(200, self.status(), "ok")
        return self.refuse("not_terminal", "the run is not in a state that the word of the person ends or leaves", {"state": seg.state})

    def post_reboot(self, req):
        if not self.open_run():
            return self.refuse("no_run", "there is no upgrade of the operating system to act on")
        seg = self.cur()
        if seg.state not in ("awaiting_reboot", "reboot_first"):
            return self.refuse("not_awaiting_reboot", "the machine does not wait for its restart", {"state": seg.state})
        if seg.state == "awaiting_reboot":
            self.reboot_for_hop = True
        self.reboot_at = self.t + 3
        self.go([Seg("rebooting", None, **self.f(state="rebooting", pnr=seg.kw.get("pnr", False)))])
        return self.answer(202, self.status())

    def post_resume(self, req):
        if not self.open_run():
            return self.refuse("no_run", "there is no upgrade of the operating system to act on")
        seg = self.cur()
        if seg.state not in ("interrupted", "stopped_at_waypoint") or "resume" not in seg.kw.get("actions", []):
            return self.refuse("not_resumable", "the run cannot be gone on with from where it is", {"state": seg.state, "class": (seg.kw.get("failure") or {}).get("class", "")})
        if self.cfg.get("resume_fails"):
            failure = {"stage": "forward", "class": self.cfg["resume_fail_class"], "rc": 1, "detail_line": "STOP: dpkg was interrupted"}
            self.go([Seg("recovering", 10, block=(1, "forward", "mend what dpkg left half done"), **self.f(state="recovering", pnr=True, half=True)),
                     Seg("stopped_at_waypoint", None, **self.f(state="stopped_at_waypoint", pnr=True, half=True, failure=failure, actions=["resume", "takeover", "acknowledge"]))])
            return self.answer(202, self.status())
        bs = self.cfg["block_seconds"]
        segs = [Seg("recovering", 8, block=(1, "forward", "mend what dpkg left half done"), **self.f(state="recovering", pnr=True, half=True))]
        for i, bid in enumerate(("minimal", "full", "prereboot"), 2):
            title = dict(HOP_BLOCKS)[bid]
            segs.append(Seg("recovering", bs[bid] // 2, block=(i, bid, title), **self.f(state="recovering", pnr=True, half=bid != "prereboot")))
        segs.append(Seg("awaiting_reboot", None, enter="awaiting", **self.f(state="awaiting_reboot", pnr=True, barrier=True)))
        self.go(segs)
        return self.answer(202, self.status())

    def put_restart(self):
        if not self.cfg.get("restart_unguarded") and self.open_run() and self.cur().state in ("refreshing", "rehearsing", "running", "unwinding", "recovering", "verifying", "resuming"):
            return self.refuse("os_upgrade_open", "an upgrade of the operating system is changing the machine: it cannot be restarted or switched off until the upgrade stops or asks for it",
                               {"state": self.cur().state})
        self.reboot_at = self.t + 2
        return self.answer(200, "The operation will be completed shortly.", "ok")

    # ---- faults --------------------------------------------------------------------------------------------------------------------------

    def unit_active(self):
        return self.open_run() and self.cur().state in ("refreshing", "rehearsing", "running", "recovering", "unwinding")

    def unit_inv(self):
        """the invocation of the unit as systemd names it: the unwind and the repair of a stop run under the same name, as another invocation"""
        return "inv-%d-%d" % (self.run_n, self.unit_gen) if self.unit_active() else ""

    # where dpkg works in a block: from the second it begins (apt has first solved and downloaded) to a few seconds before the block ends
    DPKG_WINDOW = {"minimal": (10, 3), "full": (40, 5), "forward": (3, 0)}

    def dpkg_running(self):
        if not self.unit_active() or self.cur().state == "unwinding" or not self.cur().block or self.cfg.get("dpkg_never"):
            return False
        seg, win = self.cur(), self.DPKG_WINDOW.get(self.cur().block[1])
        return bool(win) and self.run["elapsed"] >= win[0] and (seg.dur is None or self.run["elapsed"] < seg.dur - win[1])

    def kill_unit(self, want):
        """`kill_unit <invocation>`: the unit is killed only while it is the invocation the runner saw; what is said last is what became of it"""
        if not want:
            return 2, "no invocation given\n"
        out = ""
        if self.unit_inv() == want:
            dp = self.dpkg_running()
            self.faultlog.append(("kill", dp))
            out = "dpkg_at_kill %s\n" % ("yes" if dp else "no")
            seg = self.cur()
            self.unit_gen += 1
            if seg.state == "unwinding":
                failure = {"stage": "unwind", "class": "unit", "rc": 143}
                self.go([Seg("needs_attention", None, **self.f(state="needs_attention", reason="unwind_partial", failure=failure, actions=["manual", "acknowledge"]))])
            elif seg.kw.get("pnr"):
                if self.cfg.get("auto_repair"):
                    self.go(self.noticed(seg) + [Seg("interrupted", 20, **self.f(state="interrupted", cause="killed", pnr=True, half=True, actions=["resume", "takeover", "acknowledge"])),
                                                 Seg("recovering", None, block=(1, "forward", "mend what dpkg left half done"), **self.f(state="recovering", pnr=True, half=True))])
                else:
                    self.go(self.noticed(seg) + [Seg("interrupted", None, **self.f(state="interrupted", cause="killed", pnr=True, half=True, actions=["resume", "takeover", "acknowledge"]))])
            else:
                failure = {"stage": "unwind", "class": "interrupted_killed", "rc": 0}
                self.go(self.noticed(seg) + [Seg("interrupted", self.cfg["interrupted_s"], **self.f(state="interrupted", cause="killed", actions=[])), Seg("unwinding", 6, **self.f(state="unwinding")),
                                             Seg("failed", None, enter="put_back", **self.f(state="failed", reason="interrupted_killed", failure=failure, busy=False, actions=["acknowledge", "discard"]))])
            out += "rc 0\n"
        for _ in range(3):
            if self.unit_inv() != want:
                return 0, out + "gone\n"
            self.tick(1)
        return 1, out + "alive\n"

    def cut_route(self):
        """both families of default route and slirp's site-local one are taken away, and a probe from the guest must fail"""
        if self.cfg.get("v6_stays"):
            return 1, "still reachable\n"
        self.route_cut = True
        seg = self.cur() if self.open_run() else None
        if seg and seg.state == "running" and seg.kw.get("pnr") and seg.block and seg.block[1] in ("minimal", "full") and not self.cfg.get("net_cut_ignored"):
            self.at(8, "network_stop")
        return 0, "cut\n"

    def network_stop(self):
        seg = self.cur() if self.open_run() else None
        if self.route_cut and seg and seg.state == "running" and seg.block:
            failure = {"stage": seg.block[1], "class": "network", "rc": 100, "detail_line": "STOP: E: Failed to fetch http://deb.debian.org/debian/pool/main/l/linux/linux-image.deb  Network is unreachable"}
            self.go([Seg("stopped_at_waypoint", None, **self.f(state="stopped_at_waypoint", pnr=True, half=True, failure=failure, actions=["resume", "takeover", "acknowledge"]))])

    def power_cut(self):
        seg = self.cur() if self.open_run() else None
        self.faultlog.append(("reset", self.dpkg_running()))
        if seg and seg.state in ("running", "rehearsing", "refreshing", "recovering"):
            if seg.kw.get("pnr"):
                self.go([Seg("interrupted", None, **self.f(state="interrupted", cause="power", pnr=True, half=True, actions=["resume", "takeover", "acknowledge"]))])
            else:
                failure = {"stage": "unwind", "class": "interrupted_power", "rc": 0}
                self.go([Seg("interrupted", 12, **self.f(state="interrupted", cause="power", actions=[])), Seg("unwinding", 6, **self.f(state="unwinding")),
                         Seg("failed", None, enter="put_back", **self.f(state="failed", reason="interrupted_power", failure=failure, busy=False, actions=["acknowledge", "discard"]))])
        self.reboot_at = None
        self.do_reboot(self.t)

    # ---- what the guest says --------------------------------------------------------------------------------------------------------------

    def names(self):
        n = list(self.base_names) + ["/etc/apt/" + p.split("/etc/apt/")[1] for p in self.apt] + list(self.bk_dirs) + list(self.run_bk)
        if self.run_dir:
            n += ["/var/lib/casaos/os-upgrade"]
            if self.run:
                n += ["/var/lib/casaos/os-upgrade/" + self.run["id"], "/var/lib/casaos/os-upgrade/" + self.run["id"] + "/markers"]
        if self.flag:
            n += ["/var/lib/casaos/os-upgrade/busy"]
        if self.beta:
            n += ["/etc/casaos/os-upgrade.beta"]
        if self.open_run() and self.cur().state in ("refreshing", "rehearsing", "running", "recovering", "unwinding", "verifying"):
            n += ["/run/systemd/transient/casaos-os-upgrade.service"]
        self.snap_n = getattr(self, "snap_n", 0) + 1
        if self.snap_n % 2 == 1:
            n += ["/var/lib/casaos/flipper"]
        if self.cfg.get("stray_file_after") and self.snap_n > self.cfg["stray_file_after"]:
            n += ["/var/lib/casaos/stray-file"]
        n += ["/run/systemd/transient/session-%d.scope" % self.session_n, "/run/systemd/system/session-%d.scope.d" % self.session_n, "/run/systemd/transient/user-1000.slice",
              "/run/systemd/system/user@1000.service.d", "/run/systemd/system/user-runtime-dir@1000.service.d"]
        skip = names_skip()
        return sorted(x for x in set(n) if not (skip and re.search(skip, x)))

    def snap_text(self):
        o = ["@@ facts", "boot_id " + self.boot, "uptime_s %d" % int(self.t), "uname " + self.kernel, "hostname " + self.hostname, "version_id " + self.version_id,
             "version_codename " + ("bullseye" if self.version_id == "11" else "bookworm")]
        o += ["timer %s %s %s" % (u, a, e) for u, (a, e) in sorted(self.timers.items())]
        o += ["busy_flag " + ("yes" if self.flag else "no"), "run_dir " + ("yes" if self.run_dir else "no"), "beta_file " + ("yes" if self.beta else "no"),
              "timers_state_file " + ("yes" if self.timers_state else "no"), "bk_dirs " + " ".join(sorted(self.bk_dirs)), "run_bk " + " ".join(sorted(self.run_bk)),
              "sources_bookworm %d" % (3 if self.sources_text == "bookworm" else 0), "sources_bullseye %d" % (3 if self.sources_text == "bullseye" else 0),
              "df_root_kb %d" % self.df_root, "df_boot_kb %d" % self.df_boot, "rx_bytes %d" % self.rx, "cache_bytes %d" % self.cache_bytes, "witnesses " + "".join(w + "," for w in self.witnesses),
              "failed_units " + " ".join(self.failed_units), "system_state running", "dpkg_lock " + ("held" if self.lock_held else "free"), "dpkg_audit 0",
              "reboot_required " + ("yes" if self.reboot_required else "no"), "@@ services"]
        o += ["%s %s running" % (n, st) for n, st in sorted(self.services.items())]
        o += ["@@ dpkg"] + ["%s\t%s\t%s" % (n, v, st) for n, (v, st) in sorted(self.dpkg.items())]
        o += ["@@ aptfiles"] + ["%s\t%s" % (n, h) for n, h in sorted(self.apt.items())]
        o += ["@@ names"] + self.names()
        o += ["@@ units", "# asked"] + (["casaos-os-upgrade.service loaded active running an operating system upgrade"] if self.open_run() and self.cur().state in ("refreshing", "rehearsing", "running") else [])
        o += ["@@ pid1", "# lines %d" % (180 + int(self.t))] + self.pid1
        o += ["@@ runs"]
        for rid in self.run_ids[-3:]:
            bk = self.run_info[rid]["bk"]
            moved = next((m for m in ("bk-done", "bk-aborted") if "%s/%s" % (rid, m) in " ".join(self.run_bk)), "-")
            o.append("%s\t%s\t%s\t%s" % (rid, bk or "-", "yes" if bk and bk in self.bk_dirs else "no", moved))
        o += ["@@ end"]
        return "\n".join(o) + "\n"

    def ssh(self, cmd):
        """(exit status, stdout) of a command the runner sends to the guest"""
        with self.lock:
            if not self.up():
                return 255, ""
            m = re.match(r"^sudo sh /usr/local/lib/oup/guest\.sh (\w+)(?: (.*))?$", cmd.strip())
            if not m:
                return (0, "") if cmd.strip() == "true" else (127, "")
            verb, arg = m.group(1), (m.group(2) or "").strip()
            n = self.ssh_calls[verb] = self.ssh_calls.get(verb, 0) + 1
            if n <= self.cfg["ssh_drops"].get(verb, 0):
                return 255, ""          # the connection was missed: nothing was done
            self.session_n += 1
            rc, out = self.verb(verb, arg)
            if n <= self.cfg["ssh_lost"].get(verb, 0):
                return 255, ""          # it was done, and the reply was lost
            return rc, out

    def verb(self, verb, arg):
        if verb == "boot_id":
            return 0, self.boot + "\n"
        if verb == "hostname":
            return 0, self.hostname + "\n"
        if verb == "snap":
            return 0, self.snap_text()
        if verb == "flag":
            return 0, ("yes" if self.flag else "no") + "\n"
        if verb == "unit":
            return 0, ("active" if self.open_run() and self.cur().state in ("refreshing", "rehearsing", "running") else "inactive") + "\n"
        if verb == "unit_id":
            return 0, self.unit_inv() + "\n"
        if verb == "kill_unit":
            return self.kill_unit(arg)
        if verb == "dpkg_wait":
            mx, run = int(arg or 180), 0
            for i in range(mx):
                run = run + 1 if self.dpkg_running() else 0
                if run >= 3:
                    return 0, "seen %d\n" % i
                self.tick(1)
            return 1, "never %d\n" % mx
        if verb == "set_clock":
            if not re.fullmatch(r"\d+", arg):
                return 2, "no epoch\n"
            skew, self.clock_lag = int(round(self.clock_lag)), 0.0
            return 0, "skew %d\nset\n" % skew
        if verb == "lock_start":
            self.lock_held = True
            return 0, "held\n"
        if verb == "lock_stop":
            self.lock_held = bool(self.cfg.get("lock_stays"))
            return (0, "free\n") if not self.lock_held else (1, "the lock is still held\n")
        if verb == "lock_state":
            return 0, ("held" if self.lock_held else "free") + "\n"
        if verb == "route_cut":
            return self.cut_route()
        if verb == "route_restore":
            if self.cfg.get("route_stays_cut"):
                return 1, "no default route\n"
            had, self.route_cut = not self.route_cut, False
            return 0, ("present" if had else "restored") + "\n"
        if verb == "net_check":
            return (0, "200\n") if not self.route_cut else (22, "")
        if verb == "runfiles":
            return 0, "== /var/lib/casaos/os-upgrade\nCASAOS_OSU_UNIT_START NONCE 1 run\n"
        if verb == "diag":
            return 0, "0 loaded units listed.\n"
        return 2, "unknown verb: %s\n" % verb

    def qmon(self, cmd):
        with self.lock:
            word = cmd.split()
            if word and word[0] == "savevm":
                self.snapshots[word[1]] = copy.deepcopy({k: v for k, v in self.__dict__.items() if k not in KEEP_KEYS})
                return 0, "\n"
            if word and word[0] == "loadvm":
                if word[1] not in self.snapshots:
                    return 1, "Error: Snapshot %s does not exist\n" % word[1]
                keep = {k: self.__dict__[k] for k in KEEP_KEYS}
                state = copy.deepcopy(self.snapshots[word[1]])
                self.__dict__.clear()
                self.__dict__.update(state)
                self.__dict__.update(keep)
                self.clock_lag = float(self.cfg["restore_lag"])      # the clock is that of the instant of the snapshot
                return 0, "\n"
            if word and word[0] == "system_reset":
                self.power_cut()
                return 0, "\n"
            return 1, "unknown command: %s\n" % cmd


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    world = None

    def log_message(self, *a):
        pass

    def handle_any(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        path = self.path.split("?", 1)[0]
        w = Handler.world
        if path.startswith("/_"):
            if path == "/_tick":
                w.tick(float(self.path.split("s=", 1)[1]))
                rc, out = 0, ""
            elif path == "/_ssh":
                rc, out = w.ssh(body)
            else:
                rc, out = w.qmon(body)
            data = ("%d\n%s" % (rc, out)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        w.delay_taken = 0.0
        res = w.route(self.command, path, self.headers.get("Authorization", ""), body)
        if res is None:
            return      # the guest is away: nothing answers
        limit = float(self.headers.get("X-Max-Time") or 0)
        if limit and w.delay_taken > limit:
            return      # the caller gave up before the answer came: the work is done, and nobody knows
        status, doc = res
        data = json.dumps(doc).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = handle_any


def serve(world):
    Handler.world = world
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ---- the runner of the script against the fake -------------------------------------------------------------------------------------------------

def posix(p):
    """a path as the MSYS bash of Windows wants it (C:\\a\\b to /c/a/b); other systems keep theirs"""
    p = os.path.abspath(p)
    if os.name == "nt" and re.match(r"^[A-Za-z]:", p):
        return "/" + p[0].lower() + p[2:].replace("\\", "/")
    return p


SHIMS = {
    "ssh": '''#!/usr/bin/env bash
# a stand-in for ssh: the last word is the command for the guest, which the fake plays. sudo, on a guest that cloud-init gave another /etc/hosts, says on stderr that it cannot look its name up
cmd="${*: -1}"
[ -z "${FAKE_SUDO_NOISE:-}" ] || echo "sudo: unable to resolve host oup-1a2b3c4d: Name or service not known" >&2
body="$(curl -sS --max-time 300 --data-binary "$cmd" "http://127.0.0.1:${FAKE_PORT}/_ssh" 2>/dev/null; printf x)"
body="${body%x}"
rc="${body%%$'\\n'*}"
case "$rc" in ''|*[!0-9]*) exit 255 ;; esac
printf '%s' "${body#*$'\\n'}"
exit "$rc"
''',
    "qmon": '''#!/usr/bin/env bash
# a stand-in for the QEMU monitor
body="$(curl -sS --max-time 300 --data-binary "$*" "http://127.0.0.1:${FAKE_PORT}/_qmon" 2>/dev/null; printf x)"
body="${body%x}"
rc="${body%%$'\\n'*}"
case "$rc" in ''|*[!0-9]*) exit 1 ;; esac
printf '%s' "${body#*$'\\n'}"
exit "$rc"
''',
    "curl": '''#!/usr/bin/env bash
# the real curl, told the time it will wait (the fake guest takes as long as it takes, in the time of its own clock, and a caller that waited less gets no answer)
limit=0; prev=""
for a in "$@"; do [ "$prev" != --max-time ] || limit="$a"; prev="$a"; done
exec "@CURL@" -H "X-Max-Time: ${limit}" "$@"
''',
    "tick": '''#!/usr/bin/env bash
curl -sS --max-time 300 "http://127.0.0.1:${FAKE_PORT}/_tick?s=$1" >/dev/null 2>&1
''',
    "python3": '''#!/usr/bin/env bash
exec "%s" "$@"
''' % posix(sys.executable),
}


def tool_dirs():
    """the directories of the tools of a Git-for-Windows bash (cut, tr, timeout, curl), which a bash started by Python does not have in its PATH by itself"""
    if os.name != "nt" or not os.path.isabs(BASH):
        return []
    usr_bin = os.path.dirname(os.path.abspath(BASH))
    return [d for d in (usr_bin, os.path.join(os.path.dirname(os.path.dirname(usr_bin)), "mingw64", "bin")) if os.path.isdir(d)]


def make_bin(root):
    d = os.path.join(root, "bin")
    os.makedirs(d, exist_ok=True)
    real_curl = shutil.which("curl", path=os.pathsep.join(tool_dirs() + [ENV.get("PATH", "")])) or "curl"
    for name, text in SHIMS.items():
        with open(os.path.join(d, name), "w", newline="\n") as f:
            f.write(text.replace("@CURL@", posix(real_curl) if os.path.isabs(real_curl) else real_curl))
        os.chmod(os.path.join(d, name), 0o755)
    return d


def read_out(out, name, default=""):
    try:
        with open(os.path.join(out, name), encoding="utf-8", newline="") as f:
            return f.read()
    except OSError:
        return default


def write_out(out, name, text):
    with open(os.path.join(out, name), "w", encoding="utf-8", newline="") as f:
        f.write(text)


def run_script(leg, cases="", cfg=None, root=None, env_extra=None, world_from=None):
    """the real runner script, against a fake guest: (the directory of its recordings, the world, the finished process)"""
    root = root or tempfile.mkdtemp(prefix="oup-", dir=os.environ.get("TMPDIR") or None)
    out, work = os.path.join(root, "out"), os.path.join(root, "work")
    os.makedirs(out, exist_ok=True)
    os.makedirs(work, exist_ok=True)
    world = World(cfg)
    if world_from is not None:
        world.snapshots = copy.deepcopy(world_from.snapshots)
    srv, port = serve(world)
    bindir = make_bin(root)
    # what the preparation leaves for the runner: the evidence of the guest, and the box's user
    os.makedirs(os.path.join(out, "guest"), exist_ok=True)
    write_out(out, "guest/os-release", 'PRETTY_NAME="Debian GNU/Linux 11 (bullseye)"\nID=debian\nVERSION_ID="11"\nVERSION_CODENAME=bullseye\n')
    write_out(out, "guest/box-facts", "kernel %s\ncpus 4\nmemory_mb 5945\ndocker 28.0.4\napt apt 2.2.4 (amd64)\nsystemd systemd 247 (247.3-7+deb11u4)\n" % KERNEL_11)
    write_out(out, "guest/os", "Debian GNU/Linux 11 (bullseye)\n")
    write_out(out, "guest/prepared", "2026-10-18T10:00:00Z\n")
    write_out(out, "guest/timers-on", "".join("%s active enabled\n" % u for u in UNITS))
    with open(os.path.join(work, "pw"), "w") as f:
        f.write("Aa1-test")
    world.user, world.pw = "oupproof", "Aa1-test"
    env = dict(ENV, PATH=os.pathsep.join([bindir] + tool_dirs() + [ENV.get("PATH", "")]), OUT=posix(out), WORK=posix(work), SSH_CONFIG="ssh_config", SSH_HOST="vm", GW_PORT=str(port), FAKE_PORT=str(port),
               OUP_FAST="1", OUP_TICK=posix(os.path.join(bindir, "tick")), QMON=posix(os.path.join(bindir, "qmon")), TAG="v0.0.0-test", CASES=cases, POLL_EVERY="3", CONTROL_SECONDS="200",
               FAKE_SUDO_NOISE="1")
    env.update(env_extra or {})
    try:
        proc = subprocess.run([BASH, posix(SCRIPT), "run", leg], env=env, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=int(os.environ.get("RUN_TIMEOUT", "1500")))
    finally:
        srv.shutdown()
        srv.server_close()
    return out, world, proc


def report(out, leg):
    """(exit status, text) of the report on a directory of recordings"""
    p = subprocess.run([sys.executable, REPORT, out, leg], env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def verdict_line(text, name):
    """the line of the verdict that begins with this name, or None"""
    for ln in text.splitlines():
        m = re.match(r"^- \*\*(PASS|FAIL|NOT TESTABLE)\*\* (.*)$", ln)
        if m and m.group(2).startswith(name):
            return m.group(1), m.group(2)
    return None


def head(text):
    m = re.match(r"^# OS upgrade proof, \w+ leg: (.*)$", text.splitlines()[0])
    return m.group(1) if m else "?"




# ---- the mutations ---------------------------------------------------------------------------------------------------------------------------

def jdoc(d, name):
    return json.loads(read_out(d, name + ".json"))


def jedit(d, name, fn):
    """the body of a recorded call, changed by fn (which gets the whole document)"""
    doc = jdoc(d, name)
    fn(doc)
    write_out(d, name + ".json", json.dumps(doc))


def jdata(d, name, fn):
    jedit(d, name, lambda doc: fn(doc["data"]))


def code(d, name, n):
    write_out(d, name + ".code", "%d\n" % n)


def subst(d, name, old, new, count=1):
    text = read_out(d, name)
    if text.count(old) < 1:
        raise AssertionError("%s has no %r" % (name, old))
    write_out(d, name, text.replace(old, new, count))


def fact(d, tag, key, value):
    lines = read_out(d, "snap-%s.facts" % tag).splitlines()
    out, hit = [], False
    for ln in lines:
        if ln.startswith(key + " ") or ln == key:
            ln, hit = "%s %s" % (key, value), True
        out.append(ln)
    if not hit:
        raise AssertionError("snap-%s.facts has no %s" % (tag, key))
    write_out(d, "snap-%s.facts" % tag, "\n".join(out) + "\n")


def timer_line(d, tag, unit, active, enabled="enabled"):
    subst(d, "snap-%s.facts" % tag, [ln for ln in read_out(d, "snap-%s.facts" % tag).splitlines() if ln.startswith("timer %s " % unit)][0], "timer %s %s %s" % (unit, active, enabled))


def add_line(d, name, line):
    text = read_out(d, name)
    write_out(d, name, text + line + "\n")


def drop_line(d, name, prefix):
    lines = read_out(d, name).splitlines()
    kept = [ln for ln in lines if not ln.startswith(prefix)]
    if len(kept) == len(lines):
        raise AssertionError("%s has no line that starts with %r" % (name, prefix))
    write_out(d, name, "\n".join(kept) + "\n")


def rows_edit(d, tag, fn):
    rows = [ln.split("\t") for ln in read_out(d, "polls-%s.tsv" % tag).splitlines()]
    out = fn(rows)
    write_out(d, "polls-%s.tsv" % tag, "".join("\t".join(r) + "\n" for r in (out if out is not None else rows)))


COL = dict(t0=0, t1=1, http=2, curl=3, state=4, reason=5, cause=6, bidx=7, bid=8, pnr=9, half=10, busy=11, attempt=12, fclass=13, hint=14, flag=15, run=16, beta=17, actions=18)


def where(rows, state=None, bid=None):
    return [r for r in rows if (state is None or r[COL["state"]] == state) and (bid is None or r[COL["bid"]] == bid)]


def shift_times(d, names, by):
    """times of the files of calls and rows, moved by some seconds (the clock of the guest and of the runner are one: this is the evidence that was never taken)"""
    for n in names:
        write_out(d, n, "%.6f\n" % (float(read_out(d, n).strip()) + by))


def copytree(src):
    dst = tempfile.mkdtemp(prefix="oup-m-", dir=os.environ.get("TMPDIR") or None)
    shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return dst


def statuses(text):
    out = {}
    for ln in text.splitlines():
        m = re.match(r"^- \*\*(PASS|FAIL|NOT TESTABLE)\*\* (.*?)(?:: |$)", ln)
        if m:
            out[m.group(2)] = m.group(1)
    return out


def invalid_lines(text):
    return [ln[len("- INVALID: "):] for ln in text.splitlines() if ln.startswith("- INVALID: ")]


class Expect:
    def __init__(self, kind, name="", needle="", also=()):
        self.kind, self.name, self.needle, self.also = kind, name, needle, tuple(also)


def fails(name, needle="", also=()):
    return Expect("FAIL", name, needle, also)


def invalid_run(needle):
    return Expect("INVALID", "", needle)


def not_testable(name, needle="", also=()):
    return Expect("NOT TESTABLE", name, needle, also)


def passes(name, needle=""):
    return Expect("PASS", name, needle)


def judged(label, base_status, rc, text, exp, others_same=True):
    """what the report said of a mutated recording, held against what the mutation is meant to cause"""
    st = statuses(text)
    h = head(text)
    if exp.kind == "FAIL":
        line = [ln for ln in text.splitlines() if ln.startswith("- **FAIL** " + exp.name)]
        ok = bool(line) and rc == 1 and "FAIL" in h and exp.needle in " ".join(line)
        if ok and others_same:
            changed = sorted(n for n, s in st.items() if not n.startswith(exp.name) and not any(n.startswith(a) for a in exp.also) and base_status.get(n) != s)
            ok = not changed
            detail = "other verdicts changed: %s" % changed
        else:
            detail = "head %s, rc %s, wanted a FAIL of %r with %r; verdicts %s" % (h, rc, exp.name, exp.needle, {n: s for n, s in st.items() if s != "PASS"})
    elif exp.kind == "PASS":
        line = [ln for ln in text.splitlines() if ln.startswith("- **PASS** " + exp.name)]
        ok = bool(line) and exp.needle in text and not [n for n, s in st.items() if s == "FAIL"]
        detail = "head %s, wanted a PASS of %r with %r in the report; verdicts %s" % (h, exp.name, exp.needle, {n: s for n, s in st.items() if s != "PASS"})
    elif exp.kind == "INVALID":
        found = [ln for ln in invalid_lines(text) if exp.needle in ln]
        ok = rc == 1 and "INVALID RUN" in h and bool(found)
        detail = "head %s, rc %s, wanted an INVALID line with %r; got %s" % (h, rc, exp.needle, invalid_lines(text)[:3])
    else:
        line = [ln for ln in text.splitlines() if ln.startswith("- **NOT TESTABLE** " + exp.name)]
        ok = bool(line) and exp.needle in " ".join(line) and "FAIL" not in h and "INVALID" not in h
        detail = "head %s, wanted NOT TESTABLE for %r with %r; verdicts %s" % (h, exp.name, exp.needle, {n: s for n, s in st.items() if s != "PASS"})
    check(label, ok, detail if not ok else "")
    return ok


# ---- what is mutated ---------------------------------------------------------------------------------------------------------------------------

def drop_marker(d, name):
    lines = read_out(d, "timeline.tsv").splitlines()
    write_out(d, "timeline.tsv", "".join(ln + "\n" for ln in lines if not ln.endswith("\t" + name)))


def last_answered_row(rows):
    return [r for r in rows if r[COL["http"]] != "000"][-1]


def set_last(d, tag, **kw):
    def fn(rows):
        r = last_answered_row(rows)
        for k, v in kw.items():
            r[COL[k]] = v
    rows_edit(d, tag, fn)


def map_rows(d, tag, match, **kw):
    def fn(rows):
        for r in rows:
            if r[COL["state"]] == match:
                for k, v in kw.items():
                    r[COL[k]] = v
    rows_edit(d, tag, fn)


def runs_edit(d, tag, fn):
    """the rows of the runs section of a snapshot (id, folder, left, moved), changed by fn"""
    rows = [ln.split("\t") for ln in read_out(d, "snap-%s.runs" % tag).splitlines()]
    out = fn(rows)
    write_out(d, "snap-%s.runs" % tag, "".join("\t".join(r) + "\n" for r in (out if out is not None else rows)))


def after_seconds(rows, seconds):
    """the index of the first row of a poll that began at least this many seconds after the first one"""
    t0 = float(rows[0][COL["t0"]])
    return next(i for i, r in enumerate(rows) if float(r[COL["t0"]]) - t0 >= seconds)


def drop_rows(d, tag, state):
    rows_edit(d, tag, lambda rows: [r for r in rows if r[COL["state"]] != state])


def core_mutations():
    M = []

    def m(label, expect, fn):
        M.append((label, fn, expect))

    # the run as a whole
    m("no `finished` file and no reason", invalid_run("no `finished` file"), lambda d: os.remove(os.path.join(d, "finished")))
    m("a run that stopped early", invalid_run("the run stopped early: boom"), lambda d: write_out(d, "aborted", "boom\n"))
    m("a release that has no engine (404 on the status)", invalid_run("has no /v1/sys/os/upgrade/status"), lambda d: code(d, "probe-status", 404))
    m("a guest that is not Debian 11", invalid_run("the guest is debian 12"), lambda d: subst(d, "guest/os-release", 'VERSION_ID="11"', 'VERSION_ID="12"'))
    m("a step of the harness whose undo failed", invalid_run("could not be undone (C5): the lock stayed"), lambda d: add_line(d, "invalid.txt", "C5\tthe lock stayed"))
    m("a snapshot of the guest that is missing", invalid_run("the snapshot c3-after is missing"), lambda d: os.remove(os.path.join(d, "snap-c3-after.facts")))
    m("a snapshot of the guest that could not be had", invalid_run("snapshot c4-before"), lambda d: write_out(d, "snap-c4-before.failed", "three tries\n"))
    m("a row of the polls that is cut", invalid_run("fields, not 19"), lambda d: add_line(d, "polls-c7-run.tsv", "1\t2\t3"))
    m("a leg file that names another leg", invalid_run("says it ran leg"), lambda d: write_out(d, "leg", "faults\n"))
    m("a case that began and did not end", invalid_run("C5 (refusals) began and did not end"), lambda d: drop_marker(d, "case_C5_done"))
    m("a call that got no answer", invalid_run("c2-beta"), lambda d: write_out(d, "c2-beta.curl", "7\n"))
    m("the guest's preparation that stopped", invalid_run("the guest's preparation stopped: no space"), lambda d: write_out(d, "guest/aborted", "no space\n"))

    # C1
    m("C1: a start that is accepted with the flag off", fails("C1 flag off", "POST start answers beta_off"), lambda d: code(d, "c1-start", 202))
    m("C1: a refresh that is accepted with the flag off", fails("C1 flag off", "POST refresh answers beta_off"), lambda d: jdata(d, "c1-refresh", lambda x: x.update(error_code="busy")))
    m("C1: the folder of the engine exists after the calls", fails("C1 flag off", "has not been created"), lambda d: fact(d, "c1-after", "run_dir", "yes"))
    m("C1: a unit of the engine in PID 1's journal", fails("C1 flag off", "was ever started"), lambda d: add_line(d, "snap-c1-after.pid1", "Started casaos-os-upgrade.service."))
    m("C1: a name that nothing explains", fails("C1 flag off", "that the control did not show"), lambda d: add_line(d, "snap-c1-after.names", "/var/lib/casaos/stray-file"))
    m("C1: a file of the engine", fails("C1 flag off", "no file or folder of the engine was made"), lambda d: add_line(d, "snap-c1-after.names", "/var/lib/casaos/os-upgrade/busy"))
    m("C1: the status after the calls says the switch is on", fails("C1 flag off", "idle, with beta_enabled false"), lambda d: jdata(d, "c1-status-after", lambda x: x.update(beta_enabled=True)))
    m("C1: the flag file is there before the calls", fails("C1 flag off", "os-upgrade.beta is absent"), lambda d: fact(d, "c1-before", "beta_file", "yes"))

    m("C1: the journal of PID 1 was not read", invalid_run("the journal of PID 1 (it was not read)"), lambda d: write_out(d, "snap-c1-after.pid1", "# lines 0\n"))
    m("C1: systemd was not asked for the units", invalid_run("the units of the engine (systemd was not asked)"), lambda d: write_out(d, "snap-c1-before.units", ""))
    m("C1: the names of the file system are missing", invalid_run("the names of the file system"), lambda d: write_out(d, "snap-c1-ctl-a.names", ""))
    m("C1: the login of a snapshot is among the names that the control did not show", fails("C1 flag off", "that the control did not show"), lambda d: add_line(d, "snap-c1-after.names", "/run/systemd/transient/session-99.scope"))

    # C2
    m("C2: the file is not there after the switch", fails("C2 switch on", "os-upgrade.beta exists"), lambda d: fact(d, "c2-after", "beta_file", "no"))
    m("C2: the switch answers off", fails("C2 switch on", "beta_enabled true"), lambda d: jdata(d, "c2-beta", lambda x: x.update(beta_enabled=False)))

    # C3
    m("C3: the busy flag is never seen while the refresh works", fails("C3 refresh", "the busy flag is there while the run works"),
      lambda d: rows_edit(d, "c3-run", lambda rows: [r[:15] + ["no"] + r[16:] for r in rows]))
    m("C3: a kept run's backup folder is still in /var/backups", fails("C3 refresh", "each kept run's backup folder"), lambda d: runs_edit(d, "c3-after", lambda rows: [r[:2] + ["yes"] + r[3:] for r in rows]))
    m("C3: the sources are not what they were", fails("C3 refresh", "the sources of apt are as they were"), lambda d: subst(d, "snap-c3-after.aptfiles", "/etc/apt/sources.list\t", "/etc/apt/sources.list\tf"))
    m("C3: a timer of apt is still stopped", fails("C3 refresh", "the timers of apt are as they were"), lambda d: timer_line(d, "c3-after", "apt-daily.timer", "inactive"))
    m("C3: a package was removed", fails("C3 refresh", "no package was removed"), lambda d: drop_line(d, "snap-c3-after.dpkg", "fuse\t"))
    m("C3: the busy flag is still there after refreshed", fails("C3 refresh", "the busy flag is gone"), lambda d: write_out(d, "flag-c3.tsv", "1\tyes\n"))
    m("C3: the refresh fails with apt_failed", fails("C3 refresh", "the refresh ends refreshed or asking for a restart"), lambda d: set_last(d, "c3-run", state="failed", fclass="apt_failed", reason="apt_failed"))
    m("C3: the refresh fails with the class network: the harness's", invalid_run("class network"), lambda d: set_last(d, "c3-run", state="failed", fclass="network", reason="network"))
    m("C3: the refresh goes through a state it has not", fails("C3 refresh", "the states it went through are the engine's for this"), lambda d: map_rows(d, "c3-run", "refreshing", state="rehearsing"))
    m("C3: the restart is not accepted", fails("C3 refresh", "POST reboot is accepted"), lambda d: code(d, "c3-reboot-post", 409))
    m("C3: the guest does not come back", invalid_run("did not come back"), lambda d: write_out(d, "boot-c3-reboot.timeout", "1500\n"))
    m("C3: the close is refused", fails("C3 refresh", "acknowledge is accepted"), lambda d: code(d, "c3-ack", 409))

    # C4
    m("C4: the sources are not put back by the discard", fails("C4 plan and rehearsal", "the sources of apt are as they were"), lambda d: subst(d, "snap-c4-after.aptfiles", "/etc/apt/sources.list\t", "/etc/apt/sources.list\tf"))
    m("C4: /etc/apt differs after the discard", fails("C4 plan and rehearsal", "the hash of /etc/apt is as it was"), lambda d: subst(d, "snap-c4-after.aptfiles", "/etc/apt/apt.conf.d/99oup-quiet\t", "/etc/apt/apt.conf.d/99oup-quiet\tf"))
    m("C4: the timers still run while the rehearsal is held", fails("C4 plan and rehearsal", "the timers of apt are stopped"), lambda d: timer_line(d, "c4-rehearsed", "apt-daily-upgrade.timer", "active"))
    m("C4: the timers are not back after the discard", fails("C4 plan and rehearsal", "the timers of apt are as they were"), lambda d: timer_line(d, "c4-after", "unattended-upgrades.service", "inactive"))
    m("C4: the sources of Debian 12 are not in place", fails("C4 plan and rehearsal", "the sources of Debian 12 are in place"), lambda d: fact(d, "c4-rehearsed", "sources_bookworm", "0"))
    m("C4: the rehearsal is held for a day", fails("C4 plan and rehearsal", "held for at most 30 minutes"),
      lambda d: jdata(d, "c4-status-rehearsed", lambda x: x.update(expires_at=x["expires_at"].replace("T", "T", 1)[:8] + "%02d" % (int(x["expires_at"][8:10]) + 1) + x["expires_at"][10:])))
    m("C4: a package was installed by the rehearsal", fails("C4 plan and rehearsal", "no package was installed"), lambda d: add_line(d, "snap-c4-after.dpkg", "osm-new\t1.0\tii "))
    m("C4: the backup folder stays in /var/backups", fails("C4 plan and rehearsal", "no backup folder is left"), lambda d: fact(d, "c4-after", "bk_dirs", "/var/backups/casaos-os-upgrade-p1-x"))
    m("C4: the folder of an older run is still in /var/backups, though the folder list is empty", fails("C4 plan and rehearsal", "each kept run's backup folder"),
      lambda d: runs_edit(d, "c4-after", lambda rows: [r[:2] + ["yes"] + r[3:] if i == 0 else r for i, r in enumerate(rows)]))
    m("C4: the backup folder of the rehearsal was not moved into its run folder", fails("C4 plan and rehearsal", "backup folder of this run was moved"),
      lambda d: runs_edit(d, "c4-after", lambda rows: [r[:3] + ["-"] if i == len(rows) - 1 else r for i, r in enumerate(rows)]))
    m("C4: the polls carry no run id: the run whose folder was moved cannot be named", fails("C4 plan and rehearsal", "backup folder of this run was moved"),
      lambda d: rows_edit(d, "c4-run", lambda rows: [r[:16] + ["-"] + r[17:] for r in rows]))
    m("C4: the rehearsal is started with a safety net, which the page does not give", fails("C4 plan and rehearsal", "the way the page starts one"), lambda d: subst(d, "c4-start.req", '"safety_net":""', '"safety_net":"snapshot"'))
    m("C4: rehearsal_done keeps the machine busy", fails("C4 plan and rehearsal", "rehearsal_done lets go of the machine"), lambda d: map_rows(d, "c4-unwind", "rehearsal_done", busy="true"))
    m("C4: the plan is blocked: the cases that start from it cannot be run", fails("C4 plan and rehearsal", "can be started from", also=("C5", "C6", "C7")), lambda d: jdata(d, "c4-plan-get", lambda x: x["plan"].update(verdict="blocked")))
    m("C4: the flag is still there after rehearsal_done", fails("C4 plan and rehearsal", "the busy flag is gone"), lambda d: write_out(d, "flag-c4.tsv", "1\tyes\n"))
    m("C4: the rehearsal does not reach rehearsed", fails("C4 plan and rehearsal", "the rehearsal ends rehearsed"), lambda d: set_last(d, "c4-run", state="failed", fclass="apt_failed", reason="apt_failed"))
    m("C4: the discard is refused", fails("C4 plan and rehearsal", "ack discard is accepted"), lambda d: code(d, "c4-discard", 409))
    m("C4: a plan job that does not end", fails("C4 plan and rehearsal", "can be started from", also=("C5", "C6", "C7")), lambda d: jdata(d, "c4-plan-get", lambda x: x.update(state="running")))

    # C5
    m("C5: another hostname is accepted", fails("C5 hostname_mismatch", "another hostname is refused"), lambda d: code(d, "c5-hostname", 202))
    m("C5: the hostname is echoed", fails("C5 hostname_mismatch", "does not repeat"), lambda d: write_out(d, "c5-hostname.json", read_out(d, "c5-hostname.json").replace('"error"', '"echo": "%sx", "error"' % read_out(d, "hostname").strip(), 1)))
    m("C5: no acknowledgement is needed", fails("C5 acks_missing", "without its acknowledgements is refused"), lambda d: code(d, "c5-acks", 202))
    m("C5: the refusal does not name what is missing", fails("C5 acks_missing", "names the acknowledgements"), lambda d: jdata(d, "c5-acks", lambda x: x["data"].update(missing=[])))
    m("C5: a start under dpkg's lock goes through", fails("C5 dpkg lock", "is refused"), lambda d: code(d, "c5-lock", 202))
    m("C5: dpkg's lock was not held", fails("C5 dpkg lock", "the proof held dpkg's lock"), lambda d: write_out(d, "c5-lock.during", "free\n"))
    m("C5: dpkg's lock is not given back", invalid_run("dpkg's lock was not given back"), lambda d: write_out(d, "c5-lock.undone", "held\n"))
    m("C5: a second start is accepted", fails("C5 run_open", "a second start while a run is open is refused"), lambda d: code(d, "c5-second", 202))
    m("C5: the page's restart is accepted", fails("C5 os_upgrade_open", "is refused while the unit works"), lambda d: code(d, "c5-restart", 200))
    m("C5: the guest restarted", fails("C5 os_upgrade_open", "the guest did not restart"), lambda d: write_out(d, "c5-restart.boot-after", "00000000-0000-4000-8000-000000000000\n"))
    m("C5: the folder of an older run is still in /var/backups", fails("C5 nothing left behind", "each kept run's backup folder"),
      lambda d: runs_edit(d, "c5-after", lambda rows: [r[:2] + ["yes"] + r[3:] if i == 0 else r for i, r in enumerate(rows)]))
    m("C5: the rehearsal that holds the run open has no safety net", fails("C5 run_open", "safety net of a hop"), lambda d: subst(d, "c5-open.req", '"safety_net":"snapshot"', '"safety_net":""'))
    m("C5: sudo says on stderr that it cannot look the name up: the lock was held all the same", passes("C5 dpkg lock"),
      lambda d: write_out(d, "c5-lock.during", "sudo: unable to resolve host oup-1a2b3c4d: Name or service not known\nheld\n"))
    m("C5: the same with the lock given back", passes("C5 nothing left behind"), lambda d: write_out(d, "c5-lock.undone", "sudo: unable to resolve host oup-1a2b3c4d: Name or service not known\nfree\n"))
    m("C5: the rehearsal was not working: not testable", not_testable("C5 run_open", "was not working", also=("C5 os_upgrade_open",)), lambda d: jdata(d, "c5-status-before-second", lambda x: x.update(state="rehearsed")))
    m("C5: something is left behind", fails("C5 nothing left behind", "the timers of apt are as they were"), lambda d: timer_line(d, "c5-after", "apt-daily.timer", "inactive"))
    m("C5: the run that was opened is not closed", fails("C5 nothing left behind", "is closed"), lambda d: write_out(d, "c5-open.closed", "state rehearsing\n"))

    # C6
    m("C6: the call-off is refused", fails("C6 call-off", "POST abort is accepted"), lambda d: code(d, "c6-abort", 409))
    m("C6: the run ends with another class", fails("C6 call-off", "the class abort"), lambda d: jdata(d, "c6-status-failed", lambda x: x["failure"].update({"class": "unknown"})))
    m("C6: the timers are not back", fails("C6 call-off", "the timers of apt are as they were"), lambda d: timer_line(d, "c6-after", "apt-daily.timer", "inactive"))
    m("C6: the run never gets to failed", fails("C6 call-off", "the called-off run is failed"), lambda d: map_rows(d, "c6-after", "failed", state="unwinding"))
    m("C6: the backup folder is left", fails("C6 call-off", "no backup folder is left"), lambda d: fact(d, "c6-after", "bk_dirs", "/var/backups/casaos-os-upgrade-p1-x"))
    m("C6: the point of no return was passed", fails("C6 call-off", "the point of no return was not passed"), lambda d: jdata(d, "c6-status-failed", lambda x: x.update(pnr=True)))
    m("C6: the abort was not offered", fails("C6 call-off", "running offers `abort`"), lambda d: map_rows(d, "c6-run", "running", actions="-"))
    m("C6: the backup folder of the call-off was not moved into its run folder, whatever was moved of the runs before it", fails("C6 call-off", "backup folder of this run was moved"),
      lambda d: runs_edit(d, "c6-after", lambda rows: [r[:3] + ["-"] if i == len(rows) - 1 else r for i, r in enumerate(rows)]))
    m("C6: the hop is started with another safety net", fails("C6 call-off", "safety net snapshot"), lambda d: subst(d, "c6-start.req", '"safety_net":"snapshot"', '"safety_net":"reinstall_ok"'))
    m("C6: the interruption of the call-off is not caught by the polls", passes("C6 call-off"), lambda d: drop_rows(d, "c6-after", "interrupted"))
    m("C6: the sources are not put back", fails("C6 call-off", "the sources of apt are as they were"), lambda d: subst(d, "snap-c6-after.aptfiles", "/etc/apt/sources.list\t", "/etc/apt/sources.list\tf"))
    m("C6: a package changed", fails("C6 call-off", "no package was installed"), lambda d: subst(d, "snap-c6-after.dpkg", "libc6\t", "libc6\t9"))

    # C7
    m("C7: the barrier is red", fails("C7 the hop", "the barrier is green"), lambda d: jdata(d, "c7-status-awaiting", lambda x: x["barrier"].update(ok=False)))
    m("C7: a check before the restart is red", fails("C7 the hop", "no check before the restart is red"), lambda d: jdata(d, "c7-status-awaiting", lambda x: x["barrier"]["checks"][0].update(ok=False)))
    m("C7: the restart is not offered", fails("C7 the hop", "awaiting_reboot offers `reboot`"), lambda d: jdata(d, "c7-status-awaiting", lambda x: x.update(actions=["takeover"])))
    m("C7: the busy flag is down while the machine waits", fails("C7 the hop", "the busy flag stays up"), lambda d: fact(d, "c7-awaiting", "busy_flag", "no"))
    m("C7: the sources still say Debian 11 while it waits", fails("C7 the hop", "the sources are those of Debian 12"), lambda d: fact(d, "c7-awaiting", "sources_bullseye", "2"))
    m("C7: the machine is already on its new kernel while it waits", fails("C7 the hop", "the old kernel still runs until the restart"), lambda d: fact(d, "c7-awaiting", "uname", "6.1.0-40-cloud-amd64"))
    m("C7: Debian 11 after the restart", fails("C7 the hop", "VERSION_ID=12"), lambda d: fact(d, "c7-after", "version_id", "11"))
    m("C7: the same kernel after the restart", fails("C7 the hop", "a kernel newer than before"), lambda d: fact(d, "c7-after", "uname", read_out(d, "snap-c7-before.facts").split("uname ")[1].splitlines()[0]))
    m("C7: a witness container is missing", fails("C7 the hop", "the witness containers returned"), lambda d: fact(d, "c7-after", "witnesses", "p-always,p-db,"))
    m("C7: a ReCasaOS service is down", fails("C7 the hop", "every ReCasaOS service"), lambda d: subst(d, "snap-c7-after.services", "casaos-gateway.service active", "casaos-gateway.service failed"))
    m("C7: sshd is down", fails("C7 the hop", "sshd is active"), lambda d: subst(d, "snap-c7-after.services", "ssh.service active", "ssh.service inactive"))
    m("C7: a unit failed that was not failed before", fails("C7 the hop", "no unit failed that was not failed before"), lambda d: fact(d, "c7-after", "failed_units", "foo.service"))
    m("C7: dpkg --audit finds something", fails("C7 the hop", "dpkg --audit finds nothing"), lambda d: fact(d, "c7-after", "dpkg_audit", "3"))
    m("C7: the timers of apt are not back", fails("C7 the hop", "the timers of apt are back"), lambda d: timer_line(d, "c7-after", "unattended-upgrades.service", "inactive"))
    m("C7: half upgraded at the verdict", fails("C7 the hop", "not half upgraded"), lambda d: jdata(d, "c7-status-final", lambda x: x.update(half_upgraded=True)))
    m("C7: a container did not return", fails("C7 the hop", "the containers that ran before are back"), lambda d: jdata(d, "c7-status-final", lambda x: x.update(not_returned=[{"container": "p-web", "policy": "unless-stopped"}])))
    m("C7: an error finding", fails("C7 the hop", "the judge found no finding of severity error"), lambda d: jdata(d, "c7-status-final", lambda x: x.update(findings=[{"code": "service_down", "severity": "error", "arg": "x"}])))
    m("C7: the backup folder is not moved", fails("C7 the hop", "no backup folder is left"), lambda d: fact(d, "c7-final", "bk_dirs", "/var/backups/casaos-os-upgrade-p1-x"))
    m("C7: the backup folder of the hop is not moved into its run folder", fails("C7 the hop", "backup folder of this run was moved"),
      lambda d: runs_edit(d, "c7-final", lambda rows: [r[:3] + ["-"] if i == len(rows) - 1 else r for i, r in enumerate(rows)]))
    m("C7: the folder of an older run is still in /var/backups, though the folder list is empty", fails("C7 the hop", "each kept run's backup folder"),
      lambda d: runs_edit(d, "c7-final", lambda rows: [r[:2] + ["yes"] + r[3:] if i == 0 else r for i, r in enumerate(rows)]))
    m("C7: the hop is started with another safety net", fails("C7 the hop", "safety net snapshot"), lambda d: subst(d, "c7-start.req", '"safety_net":"snapshot"', '"safety_net":""'))
    m("C7: findings of every severity but error: they are printed, and the verdict stands", passes("C7 the hop", "warn ports_missing"),
      lambda d: jdata(d, "c7-status-final", lambda x: x.update(state="succeeded_with_findings", not_judged=["ports"], findings=[
          {"code": "docker_source_paused", "severity": "info"}, {"code": "dpkg_dist_new", "severity": "info", "arg": "/etc/ssh/sshd_config"},
          {"code": "ports_missing", "severity": "warn", "arg": "445/tcp"}, {"code": "units_failed_new", "severity": "warn", "arg": "x.service"}, {"code": "facts_incomplete", "severity": "warn"}])))
    m("C7: the same findings with an error among them", fails("C7 the hop", "the judge found no finding of severity error"),
      lambda d: jdata(d, "c7-status-final", lambda x: x.update(state="succeeded_with_findings", findings=[{"code": "docker_source_paused", "severity": "info"}, {"code": "ports_missing", "severity": "warn", "arg": "445/tcp"},
                                                                                                         {"code": "casaos_unit_down", "severity": "error", "arg": "casaos.service"}])))
    m("C7: the busy flag is still there at the verdict", fails("C7 the hop", "the busy flag is gone at the verdict"), lambda d: write_out(d, "flag-c7.tsv", "1\tyes\n"))
    m("C7: the blocks are seen out of order", fails("C7 the hop", "in their order"), lambda d: rows_edit(d, "c7-run", lambda rows: [r[:8] + ["verify"] + r[9:] if r[COL["bid"]] == "download" else r for r in rows]))
    m("C7: the point of no return is never seen", fails("C7 the hop", "point of no return is seen passed"), lambda d: rows_edit(d, "c7-run", lambda rows: [r[:9] + ["false"] + r[10:] for r in rows]))
    m("C7: the same boot id after the restart", fails("C7 the hop", "went away and came back"), lambda d: write_out(d, "boot-c7-reboot.tsv", "".join("%s\t%s\t%s\n" % (r.split("\t")[0], r.split("\t")[1], read_out(d, "c7-reboot.boot-old").strip()) for r in read_out(d, "boot-c7-reboot.tsv").splitlines())))
    m("C7: the guest does not come back", invalid_run("did not come back"), lambda d: write_out(d, "boot-c7-reboot.timeout", "1500\n"))
    m("C7: the check after the restart fails", fails("C7 the hop", "ends in a success"), lambda d: set_last(d, "c7-verify", state="failed", reason="verify"))
    m("C7: the restart is refused", fails("C7 the hop", "POST reboot is accepted"), lambda d: code(d, "c7-reboot-post", 409))
    m("C3: refreshed does not offer the close", fails("C3 refresh", "refreshed offers `acknowledge`"), lambda d: map_rows(d, "c3-after-reboot", "refreshed", actions="-"))
    m("C3: the file of the timers' state is left", fails("C3 refresh", "timers' state is gone"), lambda d: fact(d, "c3-after", "timers_state_file", "yes"))
    m("C3: the status after the close is not idle", fails("C3 refresh", "after the close the status is idle"), lambda d: jdata(d, "c3-status-closed", lambda x: x.update(state="refreshed")))
    m("C3: the refresh is not accepted", fails("C3 refresh", "POST refresh is accepted"), lambda d: code(d, "c3-refresh", 409))
    m("C3: the same boot id after the restart the refresh asked for", fails("C3 refresh", "came back with another boot id"),
      lambda d: write_out(d, "boot-c3-reboot.tsv", "".join("%s\t%s\t%s\n" % (r.split("\t")[0], r.split("\t")[1], read_out(d, "c3-reboot.boot-old").strip()) for r in read_out(d, "boot-c3-reboot.tsv").splitlines())))
    m("C1: a unit of the engine is loaded", fails("C1 flag off", "is loaded"), lambda d: add_line(d, "snap-c1-after.units", "casaos-os-upgrade.service loaded active running"))
    m("C4: the plan is not accepted", fails("C4 plan and rehearsal", "POST plan is accepted"), lambda d: code(d, "c4-plan-post", 409))
    m("C4: the rehearsal is not accepted", fails("C4 plan and rehearsal", "is accepted"), lambda d: code(d, "c4-start", 409))
    m("C4: rehearsed does not offer the discard", fails("C4 plan and rehearsal", "rehearsed offers `discard`"), lambda d: set_last(d, "c4-run", actions="-"))
    m("C4: /etc/apt is as it was while the rehearsal is held", fails("C4 plan and rehearsal", "/etc/apt is not what it was"), lambda d: write_out(d, "snap-c4-rehearsed.aptfiles", read_out(d, "snap-c4-before.aptfiles")))
    m("C4: the busy flag is down while the rehearsal is held", fails("C4 plan and rehearsal", "the busy flag is there while the rehearsal is held"), lambda d: fact(d, "c4-rehearsed", "busy_flag", "no"))
    m("C4: the file of the timers' state is left", fails("C4 plan and rehearsal", "timers' state is gone"), lambda d: fact(d, "c4-after", "timers_state_file", "yes"))
    m("C4: the status after the close is not idle", fails("C4 plan and rehearsal", "after the close the status is idle"), lambda d: jdata(d, "c4-status-closed", lambda x: x.update(state="rehearsal_done")))
    m("C5: the refusal does not say in which state the open run is", fails("C5 run_open", "in which state"), lambda d: jdata(d, "c5-second", lambda x: x["data"].update(state="weird")))
    m("C6: the hop is not accepted", fails("C6 call-off", "POST start (the hop) is accepted"), lambda d: code(d, "c6-start", 409))
    m("C6: the failed run keeps the machine busy", fails("C6 call-off", "lets go of the machine"), lambda d: map_rows(d, "c6-after", "failed", busy="true"))
    m("C6: the failed run does not offer the close", fails("C6 call-off", "failed offers `acknowledge`"), lambda d: map_rows(d, "c6-after", "failed", actions="-"))
    m("C6: the close is refused", fails("C6 call-off", "acknowledge closes the run"), lambda d: code(d, "c6-ack", 409))
    m("C6: the status after the close is not idle", fails("C6 call-off", "after the close the status is idle"), lambda d: jdata(d, "c6-status-closed", lambda x: x.update(state="failed")))
    m("C6: the busy flag is still there after the failed run", fails("C6 call-off", "the busy flag is gone"), lambda d: write_out(d, "flag-c6.tsv", "1\tyes\n"))
    m("C7: the hop is not accepted", fails("C7 the hop", "POST start (the hop) is accepted"), lambda d: code(d, "c7-start", 409))
    m("C7: the close is refused", fails("C7 the hop", "acknowledge closes the run"), lambda d: code(d, "c7-ack", 409))
    m("C7: the status after the close is not idle", fails("C7 the hop", "after the close the status is idle"), lambda d: jdata(d, "c7-status-closed", lambda x: x.update(state="succeeded")))
    m("C7: the restart is never offered", fails("C7 the hop", "`reboot` is offered"), lambda d: rows_edit(d, "c7-barrier", lambda rows: [r[:18] + ["takeover"] for r in rows]))
    m("C7: the status does not say awaiting_reboot", fails("C7 the hop", "the status says awaiting_reboot"), lambda d: jdata(d, "c7-status-awaiting", lambda x: x.update(state="running")))
    m("C7: the states after the restart are out of order", fails("C7 the hop", "come in their order"),
      lambda d: rows_edit(d, "c7-verify", lambda rows: rows + [rows[0][:4] + ["rebooting"] + rows[0][5:]]))
    m("the timers of apt were not on when the guest was prepared", invalid_run("the timers of apt were not on"), lambda d: write_out(d, "guest/timers-on", "apt-daily.timer inactive disabled\napt-daily-upgrade.timer active enabled\n"))
    m("C7: the blocks stop with a failure", fails("C7 the hop", "the blocks end with the machine waiting"), lambda d: set_last(d, "c7-run", state="stopped_at_waypoint", fclass="apt_failed"))
    m("C7: a failure of the class network: the harness's", invalid_run("class network"), lambda d: set_last(d, "c7-run", state="stopped_at_waypoint", fclass="network"))
    return M


def faults_mutations():
    M = []

    def m(label, expect, fn):
        M.append((label, fn, expect))

    m("faults: no snapshot of the whole machine", invalid_run("snapshot of the whole machine"), lambda d: os.remove(os.path.join(d, "savevm.ok")))
    m("faults: the start state was not reached", invalid_run("start state of the faults was not reached"), lambda d: jdata(d, "c4-status-closed", lambda x: x.update(state="rehearsal_done")))
    m("faults: a restored guest that is not the snapshotted one", invalid_run("not the snapshotted one"), lambda d: fact(d, "F2-base", "boot_id", "00000000-0000-4000-8000-000000000000"))
    m("F1: the poll missed `interrupted` (it lasts one look): the road through unwinding to failed(interrupted_killed) proves it", passes("F1 the unit killed before", "did not catch"), lambda d: drop_rows(d, "f1-after", "interrupted"))
    m("F1: interrupted missed and the reason of the failure is another", fails("F1 the unit killed before", "interrupted_killed"),
      lambda d: (drop_rows(d, "f1-after", "interrupted"), map_rows(d, "f1-after", "failed", reason="interrupted_power")))
    m("F1: interrupted and unwinding both missed by the polls: the reason of the failure shows the interruption", passes("F1 the unit killed before", "did not catch"),
      lambda d: (drop_rows(d, "f1-after", "interrupted"), drop_rows(d, "f1-after", "unwinding")))
    m("F1: unwinding comes after the failure", fails("F1 the unit killed before", "the run is seen interrupted before it is put back"),
      lambda d: (drop_rows(d, "f1-after", "interrupted"), rows_edit(d, "f1-after", lambda rows: [r[:4] + [{"unwinding": "failed", "failed": "unwinding"}.get(r[COL["state"]], r[COL["state"]])] + r[5:] for r in rows])))
    m("F1: the backup folder of the killed hop was not moved into its run folder, whatever was moved of the runs before it", fails("F1 the unit killed before", "backup folder of this run was moved"),
      lambda d: runs_edit(d, "f1-after", lambda rows: [r[:3] + ["-"] if i == len(rows) - 1 else r for i, r in enumerate(rows)]))
    m("F1: the kill took three tries", passes("F1 the unit killed before", "3 tries"), lambda d: write_out(d, "F1.kill", "# try 1\n# try 2\n# try 3\ndpkg_at_kill no\nrc 0\ngone\n"))
    m("F1: the clock of the restored guest was not set", invalid_run("F1: the clock of the restored guest was not set"), lambda d: os.remove(os.path.join(d, "F1.clock")))
    m("F1: the timers are not back", fails("F1 the unit killed before", "the timers of apt are as they were"), lambda d: timer_line(d, "f1-after", "apt-daily.timer", "inactive"))
    m("F1: another reason", fails("F1 the unit killed before", "interrupted_killed"), lambda d: map_rows(d, "f1-after", "failed", reason="interrupted_power"))
    m("F1: the kill did not take effect", fails("F1 the unit killed before", "systemctl kill"), lambda d: write_out(d, "F1.kill", "# try 1\nalive\n"))
    m("F1: the sources are not put back", fails("F1 the unit killed before", "the sources of apt are as they were"), lambda d: subst(d, "snap-f1-after.aptfiles", "/etc/apt/sources.list\t", "/etc/apt/sources.list\tf"))
    m("F2: the run is repaired by itself", fails("F2 the unit killed after", "no automatic repair"), lambda d: rows_edit(d, "f2-observe", lambda rows: [r[:4] + ["recovering"] + r[5:] if i == after_seconds(rows, 60) else r for i, r in enumerate(rows)]))
    m("F2: the run is repaired by itself after two minutes, which a window of 100 s would have missed", fails("F2 the unit killed after", "no automatic repair"),
      lambda d: rows_edit(d, "f2-observe", lambda rows: [r[:4] + ["recovering"] + r[5:] if i == after_seconds(rows, 120) else r for i, r in enumerate(rows)]))
    m("F2: the run is not looked at long enough", fails("F2 the unit killed after", "looked at long enough"), lambda d: rows_edit(d, "f2-observe", lambda rows: rows[:5]))
    m("F2: the run is looked at for 100 s only, which is not the backoff of a repair that is allowed", fails("F2 the unit killed after", "looked at long enough"), lambda d: rows_edit(d, "f2-observe", lambda rows: rows[:after_seconds(rows, 100)]))
    m("F2: dpkg was not seen at work", not_testable("F2 the unit killed after", "dpkg was not seen"), lambda d: write_out(d, "F2.dpkg", "never 240\n"))
    m("F2: there is no record of dpkg", invalid_run("no record of dpkg at work"), lambda d: os.remove(os.path.join(d, "F2.dpkg")))
    m("F2: dpkg was gone when the unit was killed", not_testable("F2 the unit killed after", "not running at the moment of the kill"), lambda d: write_out(d, "F2.kill", "# try 1\ndpkg_at_kill no\nrc 0\ngone\n"))
    m("F2: the kill did not take effect", fails("F2 the unit killed after", "systemctl kill"), lambda d: write_out(d, "F2.kill", "# try 1\ndpkg_at_kill yes\nrc 0\nalive\n"))
    m("F2: the clock of the restored guest was not set", invalid_run("F2: the clock of the restored guest was not set"), lambda d: write_out(d, "F2.clock", "# try 1\nthe clock could not be set\n"))
    m("F2: interrupted by a power cut, not a kill", fails("F2 the unit killed after", "interrupted by a kill after the point of no return"), lambda d: jdata(d, "f2-status-interrupted", lambda x: x.update(cause="power")))
    m("F2: resume is not offered", fails("F2 the unit killed after", "interrupted offers `resume`"), lambda d: jdata(d, "f2-status-interrupted", lambda x: x.update(actions=["takeover"])))
    m("F2: resume is refused", fails("F2 the unit killed after", "POST resume is accepted"), lambda d: code(d, "f2-resume", 409))
    m("F2: the repair never runs as a unit", fails("F2 the unit killed after", "recovering"), lambda d: drop_rows(d, "f2-recover", "recovering"))
    m("F2: Debian 11 after the restart", fails("F2 the unit killed after", "VERSION_ID=12"), lambda d: fact(d, "f2-after", "version_id", "11"))
    m("F3: the stop has another class", fails("F3 the network cut after", "the stop has the class network"), lambda d: jdata(d, "f3-status-stopped", lambda x: x["failure"].update({"class": "apt_failed"})))
    m("F3: the network is not given back", invalid_run("the network was not given back"), lambda d: write_out(d, "f3.restore", "failed\n"))
    m("F3: resume is refused after the network is back", fails("F3 the network cut after", "POST resume is accepted"), lambda d: code(d, "f3-resume", 409))
    m("F3: the routes were not taken away", fails("F3 the network cut after", "routes of the guest were taken away"), lambda d: write_out(d, "f3.cut", "# try 1\nno default route\n"))
    m("F3: the cut was not proved by a failing request", fails("F3 the network cut after", "routes of the guest were taken away"), lambda d: write_out(d, "f3.cut", "# try 1\nstill reachable\n"))
    m("F1: the hop is not accepted", fails("F1 the unit killed before", "POST start (the hop) is accepted"), lambda d: code(d, "f1-start", 409))
    m("F1: the unit is not killed in an early block", fails("F1 the unit killed before", "was killed while block 1 to 5 worked"), lambda d: map_rows(d, "f1-run", "running", bid="minimal"))
    m("F1: interrupted comes after the unwinding", fails("F1 the unit killed before", "interrupted, then"),
      lambda d: rows_edit(d, "f1-after", lambda rows: [r[:4] + [{"interrupted": "unwinding", "unwinding": "interrupted"}.get(r[COL["state"]], r[COL["state"]])] + r[5:] for r in rows]))
    m("F1: interrupted by a power cut", fails("F1 the unit killed before", "interrupted by a kill"), lambda d: map_rows(d, "f1-after", "interrupted", cause="power"))
    m("F1: the close is refused", fails("F1 the unit killed before", "acknowledge closes the run"), lambda d: code(d, "f1-ack", 409))
    m("F3: the stop does not offer resume", fails("F3 the network cut after", "offers `resume`"), lambda d: jdata(d, "f3-status-stopped", lambda x: x.update(actions=["takeover"])))
    m("F3: the hop is not accepted", fails("F3 the network cut after", "POST start (the hop) is accepted"), lambda d: code(d, "f3-start", 409))
    m("F4: the hop is not accepted", fails("F4 a power cut", "POST start (the hop) is accepted"), lambda d: code(d, "f4-start", 409))
    m("F4: resume is not offered", fails("F4 a power cut", "interrupted offers `resume`"), lambda d: jdata(d, "f4-status-interrupted", lambda x: x.update(actions=["takeover"])))
    m("F4: the guest does not come back after the reset", invalid_run("did not come back"), lambda d: write_out(d, "boot-f4-reset.timeout", "1500\n"))
    m("F4: interrupted by a kill, not a power cut", fails("F4 a power cut", "interrupted by a power cut"), lambda d: jdata(d, "f4-status-interrupted", lambda x: x.update(cause="killed")))
    m("F4: not half upgraded", fails("F4 a power cut", "half upgraded"), lambda d: jdata(d, "f4-status-interrupted", lambda x: x.update(half_upgraded=False)))
    m("F4: dpkg was not seen at work", not_testable("F4 a power cut", "dpkg was not seen"), lambda d: write_out(d, "F4.dpkg", "never 240\n"))
    m("F4: there is no record of dpkg", invalid_run("no record of dpkg at work"), lambda d: os.remove(os.path.join(d, "F4.dpkg")))
    m("F3: the clock of the restored guest was not set", invalid_run("F3: the clock of the restored guest was not set"), lambda d: os.remove(os.path.join(d, "F3.clock")))
    m("F4: the clock of the restored guest was not set", invalid_run("F4: the clock of the restored guest was not set"), lambda d: os.remove(os.path.join(d, "F4.clock")))
    m("F4: the clock was not set after the reset", invalid_run("the clock of the restored guest was not set"), lambda d: os.remove(os.path.join(d, "F4-reset.clock")))
    m("F4: the boot id did not change", fails("F4 a power cut", "another boot id after the reset"), lambda d: write_out(d, "boot-f4-reset.tsv", "".join("%s\t%s\t%s\n" % (r.split("\t")[0], r.split("\t")[1], read_out(d, "f4.boot-old").strip()) for r in read_out(d, "boot-f4-reset.tsv").splitlines())))
    return M


def run_mutations(label, base_out, leg, mutations):
    rc, text = report(base_out, leg)
    base = statuses(text)
    check("%s: the recordings of a healthy run pass" % label, rc == 0 and head(text) == "PASS", head(text) + "\n" + text[:1500])
    for name, fn, exp in mutations:
        if not wanted("%s: %s" % (label, name)):
            continue
        d = copytree(base_out)
        try:
            fn(d)
        except Exception as e:      # a mutation that does not apply is a mistake of this harness
            check("%s: %s" % (label, name), False, "the mutation did not apply: %r" % (e,))
            shutil.rmtree(d, ignore_errors=True)
            continue
        rc2, text2 = report(d, leg)
        judged("%s: %s" % (label, name), base, rc2, text2, exp, others_same=exp.kind == "FAIL")
        shutil.rmtree(d, ignore_errors=True)


# ---- the scenarios: the fake plays a core that does it wrong, and the script and the report are run on it -------------------------------------------------

def merge_prelude(out, base_out):
    """the recordings of a healthy run's start state (the prelude of the faults) under the recordings of one fault that was run from its snapshot: what a whole faults run would have left"""
    for name in os.listdir(base_out):
        if not os.path.exists(os.path.join(out, name)):
            src = os.path.join(base_out, name)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(out, name))
            else:
                shutil.copyfile(src, os.path.join(out, name))
    mine = read_out(out, "timeline.tsv").splitlines()
    theirs = [ln for ln in read_out(base_out, "timeline.tsv").splitlines() if "case_F" not in ln and not ln.endswith("run_start")]
    write_out(out, "timeline.tsv", "".join(ln + "\n" for ln in theirs + mine))


class FaultsBase:
    """the healthy faults leg, whose snapshot the scenarios of the faults start from: made when the first of them is asked for"""

    def __init__(self, world=None, out=None):
        self.world, self.out = world, out

    def get(self):
        if self.world is None:
            self.out, self.world, proc = run_script("faults", "")
            check("the script runs the faults leg to its end against the fake guest", proc.returncode == 0, proc.stdout.decode("utf-8", "replace")[-400:])
        return self


def scenario(label, leg, cases, cfg, exp, env_extra=None, base=None):
    """the real script against a fake guest that misbehaves one way: what the report says of the recordings it makes"""
    if not wanted(label):
        return None, None, ""
    world_from = base_out = None
    if base is not None:
        world_from, base_out = base.get().world, base.out
    out, w, proc = run_script(leg, cases, cfg, env_extra=env_extra, world_from=world_from)
    if base_out:
        merge_prelude(out, base_out)
    rc, text = report(out, leg)
    ok = judged(label, {}, rc, text, exp, others_same=False)
    if not ok:
        print(text[:3000])
        print(proc.stdout.decode("utf-8", "replace")[-1500:])
    return out, w, text


# ---- the tools of the guest, run for real -----------------------------------------------------------------------------------------------------
#
# The fake guest above plays what the verbs answer; it cannot show what they DO. These run the text of the guest tool, as the script writes it, under dash, with stand-ins for the commands it asks of the
# machine (ip, curl, systemctl, pgrep, sleep, date, journalctl) that keep their state in a file: the routes of both families, the invocation of the unit, the clock, where dpkg works.

STUBBOX = r'''
import json
import os
import sys

d = os.environ["STUB_STATE"]
path = os.path.join(d, "state.json")
st = json.load(open(path))
name, args = sys.argv[1], sys.argv[2:]
rc = 0


def say(x=""):
    sys.stdout.buffer.write((x + "\n").encode())          # a line ends with a line feed on any host: the tools of the guest compare what these print


def err(x):
    sys.stderr.buffer.write((x + "\n").encode())


st["calls"].append([name] + args)
if name == "ip":
    fam = "6" if "-6" in args else "4"
    key = "routes" + fam
    rest = [a for a in args if a not in ("-4", "-6")]
    if rest[:2] == ["route", "show"]:
        for r in st[key]:
            if len(rest) == 2 or (rest[2] == "default" and r.startswith("default")):
                say(r)
    elif rest[:2] == ["route", "del"]:
        hit = [r for r in st[key] if r.split()[0] == rest[2]]
        if hit and not (fam == "6" and st.get("keep6")):
            st[key].remove(hit[0])
        else:
            err("RTNETLINK answers: No such process")
            rc = 2
    elif rest[:2] == ["route", "add"]:
        spec = " ".join(rest[2:])
        if spec in st[key]:
            err("RTNETLINK answers: File exists")
            rc = 2
        else:
            st[key].append(spec)
    else:
        rc = 1
elif name == "curl":
    if any(r.startswith("default") for r in st["routes4"] + st["routes6"]):
        say("200")
    else:
        err("curl: (7) Failed to connect: Network is unreachable")
        rc = 7
elif name == "pgrep":
    rc = 0 if any(a <= st["vt"] < b for a, b in st["dpkg"]) else 1
elif name == "sleep":
    st["vt"] += float(args[0])
elif name == "date":
    if "-s" in args:
        st["clock"] = int(args[args.index("-s") + 1].lstrip("@"))
        st["order"].append("date-s")
    else:
        say(str(int(st["clock"])))
elif name == "journalctl":
    for i in range(st["journal"]):
        say("Started unit number %d." % i)
    for ln in st.get("journal_extra", []):
        say(ln)
    rc = st.get("journal_rc", 0)
elif name == "systemctl":
    if args[:1] == ["show"]:
        say(st["inv"] if st["unit_active"] else "")
    elif args[:1] == ["kill"]:
        st["kills"].append(args[1])
        st["order"].append("kill")
        if st["unit_active"] and not st.get("immortal"):
            st["inv"] = "B"          # the core starts the unwind under the same name, as another invocation
    elif args[:1] == ["is-active"]:
        up = st["svc"].get(args[-1], False)
        if "--quiet" not in args:
            say("active" if up else "inactive")
        rc = 0 if up else 3
    elif args[:1] in (["stop"], ["start"]):
        st["svc"][args[1]] = args[0] == "start"
        st["order"].append("%s %s" % (args[0], args[1]))
    elif args[:1] == ["list-units"]:
        if st.get("units_rc"):
            rc = st["units_rc"]
        else:
            for u in st["units"]:
                say(u)
json.dump(st, open(path, "w"))
sys.exit(rc)
'''

STUB_STATE0 = dict(calls=[], order=[], vt=0.0, clock=1000, dpkg=[], inv="A", unit_active=True, kills=[], svc={"systemd-timesyncd": True}, units=[], journal=40,
                   routes4=["default via 10.0.2.2 dev ens3 proto dhcp src 10.0.2.15 metric 100", "10.0.2.0/24 dev ens3 proto kernel scope link src 10.0.2.15"],
                   routes6=["fec0::/64 dev ens3 proto ra metric 256 expires 86300sec pref medium", "default via fe80::2 dev ens3 proto ra metric 1024 expires 1700sec hoplimit 64 pref medium"])


def dash_path():
    sib = os.path.join(os.path.dirname(BASH), "dash.exe") if os.path.isabs(BASH) else None
    return (sib if sib and os.path.exists(sib) else None) or shutil.which("dash") or BASH


def guest_tools_text():
    with open(SCRIPT, newline="") as f:
        m = re.search(r"cat >\"\$1/guest\.sh\" <<'OUP_GUEST'\n(.*?)\nOUP_GUEST\n", f.read().replace("\r\n", "\n"), re.S)
    return m.group(1) + "\n" if m else None


class Guest:
    """the guest tool of the script under dash, in folders of its own, with the stand-ins in front of the machine's commands"""

    def __init__(self, **state):
        self.root = tempfile.mkdtemp(prefix="oup-g-", dir=os.environ.get("TMPDIR") or None)
        self.state_dir = os.path.join(self.root, "stub")
        for sub_ in ("stub", "run", "lib", "osr"):
            os.makedirs(os.path.join(self.root, sub_))
        self.set(**dict(copy.deepcopy(STUB_STATE0), **state))
        write_out(self.state_dir, "stubbox.py", STUBBOX)
        py = posix(sys.executable)
        for cmd in ("ip", "curl", "systemctl", "pgrep", "sleep", "date", "journalctl"):
            with open(os.path.join(self.state_dir, cmd), "w", newline="\n") as f:
                f.write('#!/bin/sh\nexec "%s" "%s" %s "$@"\n' % (py, posix(os.path.join(self.state_dir, "stubbox.py")), cmd))
            os.chmod(os.path.join(self.state_dir, cmd), 0o755)
        text = guest_tools_text()
        self.text = None
        if text is not None:
            path_ = ":".join([posix(self.state_dir), "/usr/bin", "/bin"])
            for var, val in (("PATH", path_ + "; export PATH"), ("RUN", posix(os.path.join(self.root, "run"))), ("LIB", posix(os.path.join(self.root, "lib"))),
                             ("OSR", posix(os.path.join(self.root, "osr")))):
                text = re.sub(r"^%s=.*$" % var, lambda m_: "%s=%s" % (var, val), text, count=1, flags=re.M)
            self.text = text
        self.file = os.path.join(self.root, "guest.sh")
        if self.text:
            write_out(self.root, "guest.sh", self.text)

    def set(self, **kw):
        st = self.state() if os.path.exists(os.path.join(self.state_dir, "state.json")) else {}
        st.update(kw)
        write_out(self.state_dir, "state.json", json.dumps(st))

    def state(self):
        return json.loads(read_out(self.state_dir, "state.json"))

    def run(self, *argv, code=None):
        """(status, stdout) of a verb, or of a piece of shell run where the verbs are defined (code)"""
        file = self.file
        if code is not None:
            head_ = self.text.split('\nverb="${1:-}"')[0] + "\n" + code + "\n"
            file = os.path.join(self.root, "piece.sh")
            write_out(self.root, "piece.sh", head_)
        env = dict(ENV, STUB_STATE=posix(self.state_dir), KILL_WAIT="3")
        p = subprocess.run([dash_path(), posix(file)] + list(argv), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        return p.returncode, p.stdout.decode("utf-8", "replace").replace("\r\n", "\n")

    def done(self):
        shutil.rmtree(self.root, ignore_errors=True)


def guest_checks():
    if guest_tools_text() is None:
        check("the guest tools are in the script", False)
        return
    # the cut: both families of default route and slirp's site-local prefix, proved by a probe that fails; said twice it is the same; the restore brings them back
    g = Guest()
    rc, out = g.run("route_cut")
    st = g.state()
    check("route_cut takes away the default route of both families and the site-local prefix of slirp", rc == 0 and out.strip().splitlines()[-1] == "cut" and not [r for r in st["routes4"] + st["routes6"] if r.startswith("default") or r.startswith("fec0:")],
          "%s %s %s" % (rc, out, st["routes4"] + st["routes6"]))
    rc, out = g.run("route_cut")
    check("route_cut said twice is the same cut", rc == 0 and out.strip().splitlines()[-1] == "cut", "%s %s" % (rc, out))
    rc, out = g.run("route_restore")
    st = g.state()
    check("route_restore brings back the routes it took away", rc == 0 and out.strip().splitlines()[-1] == "restored" and any(r.startswith("default") for r in st["routes4"]) and any(r.startswith("default") for r in st["routes6"]),
          "%s %s %s" % (rc, out, st["routes4"] + st["routes6"]))
    rc, out = g.run("route_restore")
    check("route_restore said twice is the same", rc == 0 and out.strip().splitlines()[-1] == "present", "%s %s" % (rc, out))
    g.done()
    g = Guest(keep6=True)
    rc, out = g.run("route_cut")
    check("route_cut that does not cut (the guest still reaches the Internet) is not a cut", rc != 0 and out.strip().splitlines()[-1] != "cut", "%s %s" % (rc, out))
    g.done()
    g = Guest(routes4=[], routes6=[])
    rc, out = g.run("route_cut")
    check("route_cut with no default route says so", rc != 0 and "no default route" in out, "%s %s" % (rc, out))
    g.done()
    # the kill: only the invocation the runner saw; the unwind that follows has the same name and must not be killed by a second try
    g = Guest()
    rc, out = g.run("unit_id")
    check("unit_id says the invocation of the unit", rc == 0 and out.strip() == "A", "%s %s" % (rc, out))
    rc, out = g.run("kill_unit", "A")
    check("kill_unit kills the invocation it is given, and says it is gone", rc == 0 and "rc 0" in out and out.strip().endswith("gone") and g.state()["kills"] == ["casaos-os-upgrade.service"], "%s %s %s" % (rc, out, g.state()["kills"]))
    check("kill_unit says whether dpkg was running at the kill", "dpkg_at_kill no" in out, out)
    rc, out = g.run("kill_unit", "A")
    check("kill_unit said again does not kill the unit that took the name (the unwind)", rc == 0 and out.strip() == "gone" and len(g.state()["kills"]) == 1, "%s %s %s" % (rc, out, g.state()["kills"]))
    rc, out = g.run("kill_unit")
    check("kill_unit without the invocation does nothing", rc != 0 and not g.state()["kills"][1:], "%s %s" % (rc, out))
    g.done()
    g = Guest(dpkg=[[0, 100]])
    rc, out = g.run("kill_unit", "A")
    check("kill_unit says that dpkg was running", "dpkg_at_kill yes" in out, out)
    g.done()
    g = Guest(immortal=True)
    rc, out = g.run("kill_unit", "A")
    check("kill_unit of a unit that stays says so", rc != 0 and out.strip().endswith("alive"), "%s %s" % (rc, out))
    g.done()
    # dpkg: seen running three looks in a row; a dpkg that comes and goes within two looks is not one; never seen says so
    g = Guest(dpkg=[[5, 20]])
    rc, out = g.run("dpkg_wait", "30")
    check("dpkg_wait returns when dpkg has run for three looks", rc == 0 and re.fullmatch(r"seen [0-9]+", out.strip()) is not None and 5 < int(out.split()[1]) <= 9, "%s %s" % (rc, out))
    g.done()
    g = Guest(dpkg=[[5, 7], [9, 11], [14, 30]])
    rc, out = g.run("dpkg_wait", "30")
    check("dpkg_wait is not satisfied by a dpkg that runs two looks", rc == 0 and int(out.split()[1]) >= 14, "%s %s" % (rc, out))
    g.done()
    g = Guest()
    rc, out = g.run("dpkg_wait", "30")
    check("dpkg_wait says when dpkg was never seen", rc != 0 and out.strip() == "never 30", "%s %s" % (rc, out))
    g.done()
    # the clock: the services that fight it are stopped, the clock is set from the runner's, and they are started again; what it was behind is said
    g = Guest()
    rc, out = g.run("set_clock", "2800")
    st = g.state()
    lines = out.strip().splitlines()
    check("set_clock sets the clock, says how far behind it was, and says it is set", rc == 0 and "skew 1800" in lines and lines[-1] == "set" and st["clock"] == 2800, "%s %s %s" % (rc, out, st["clock"]))
    check("set_clock stops the time service before it sets the clock and starts it after", st["order"] == ["stop systemd-timesyncd", "date-s", "start systemd-timesyncd"], str(st["order"]))
    rc, out = g.run("set_clock", "soon")
    check("set_clock without a number does nothing", rc != 0 and g.state()["clock"] == 2800, "%s %s" % (rc, out))
    g.done()
    # the names: what a login makes goes out, what the engine makes stays
    g = Guest()
    skew = re.search(r"^NAMES_SKIP='([^']*)'", g.text or "", re.M)
    check("the guest tool filters the names of the file system with NAMES_SKIP", bool(skew), "")
    if skew:
        names_in = ["/run/systemd/transient/session-12.scope", "/run/systemd/system/session-12.scope.d", "/run/systemd/system/session-12.scope.d/50-TasksMax.conf", "/run/systemd/transient/user-1000.slice",
                    "/run/systemd/system/user@1000.service.d", "/run/systemd/system/user-runtime-dir@1000.service.d", "/run/systemd/transient/casaos-os-upgrade.service", "/run/systemd/transient",
                    "/run/systemd/system", "/run/systemd/system/casaos-os-upgrade.service.d", "/var/lib/casaos/os-upgrade", "/etc/apt/sources.list", "/var/lib/casaos/session-1"]
        write_out(g.root, "names.txt", "".join(n + "\n" for n in names_in))
        rc, out = g.run(code='grep -Ev "$NAMES_SKIP" "%s"' % posix(os.path.join(g.root, "names.txt")))
        kept = out.split()
        check("NAMES_SKIP takes out the scopes, slices and drop-ins of a login and nothing of the engine", names_in[6:] == kept and not [n for n in kept if "session-12" in n or "user-1000" in n or "user@" in n],
              "kept %s" % kept)
    g.done()
    # the sections that are empty when all is well say that they were asked
    g = Guest(units=[], journal=40)
    rc, out = g.run(code="units_section")
    check("the units section says that systemd was asked, when no unit of the engine is loaded", rc == 0 and out.splitlines() == ["@@ units", "# asked"], "%s %s" % (rc, out))
    g.set(units=["casaos-os-upgrade.service loaded active running an operating system upgrade"])
    rc, out = g.run(code="units_section")
    check("the units section lists the units of the engine", out.splitlines()[:2] == ["@@ units", "# asked"] and "casaos-os-upgrade.service" in out, out)
    g.set(units_rc=1)
    rc, out = g.run(code="units_section")
    check("the units section does not say it was asked when systemd did not answer", "# asked" not in out, out)
    rc, out = g.run(code="pid1_section")
    check("the PID 1 section says how many lines of the journal it read", out.splitlines() == ["@@ pid1", "# lines 40"], out)
    g.set(journal_extra=["Started casaos-os-upgrade.service."])
    rc, out = g.run(code="pid1_section")
    check("the PID 1 section lists the lines that name the engine", out.splitlines() == ["@@ pid1", "# lines 41", "Started casaos-os-upgrade.service."], out)
    g.set(journal=0, journal_extra=[], journal_rc=1)
    rc, out = g.run(code="pid1_section")
    check("the PID 1 section says that it read nothing when it could not", out.splitlines()[:2] == ["@@ pid1", "# lines 0"], out)
    g.done()
    # the kept runs: where each run's backup folder is
    g = Guest()
    run_dir = os.path.join(g.root, "run")
    left = os.path.join(g.root, "var-backups-left")
    os.makedirs(left)
    for rid, bk, moved in (("run-a", left, None), ("run-b", os.path.join(g.root, "gone"), "bk-done"), ("run-c", None, None)):
        os.makedirs(os.path.join(run_dir, rid, *([moved] if moved else [])))
        if bk:
            write_out(os.path.join(run_dir, rid), "bk-path", posix(bk) + "\n")
    rc, out = g.run(code="runs_section")
    rows = [ln.split("\t") for ln in out.splitlines()[1:]]
    check("the runs section tells, for each kept run, its backup folder, whether it is still there and where it was moved",
          out.splitlines()[0] == "@@ runs" and rows == [["run-a", posix(left), "yes", "-"], ["run-b", posix(os.path.join(g.root, "gone")), "no", "bk-done"], ["run-c", "-", "no", "-"]], out)
    g.done()
    p = subprocess.run([dash_path(), "-n", posix(write_guest_file())], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    check("the guest tools read under dash -n", p.returncode == 0, p.stdout.decode("utf-8", "replace"))


def write_guest_file():
    root = tempfile.mkdtemp(prefix="oup-g-", dir=os.environ.get("TMPDIR") or None)
    write_out(root, "guest.sh", guest_tools_text() or "")
    return os.path.join(root, "guest.sh")


# ---- the workflow: where each expression is -------------------------------------------------------------------------------------------------

# GitHub does not give every context to every key of a workflow ("Context availability" in its documentation): an expression that names one that is not there is refused when the workflow is read, which
# is when a dispatch fails before any job exists. The table is that one: the path of the key (a job's id and a step's place are `*`), the contexts that exist there, the special functions that exist there.
CONTEXT_NAMES = ("github", "needs", "strategy", "matrix", "job", "runner", "env", "vars", "secrets", "steps", "inputs")
SPECIAL_FUNCTIONS = ("always", "cancelled", "success", "failure", "hashFiles")
STEP_CONTEXTS = "github needs strategy matrix job runner env vars secrets steps inputs"
AVAILABLE = [
    (r"run-name", "github inputs vars", ""),
    (r"concurrency(\..*)?", "github inputs vars", ""),
    (r"env\..*", "github secrets inputs vars", ""),
    (r"jobs\.\*\.concurrency(\..*)?", "github needs strategy matrix inputs vars", ""),
    (r"jobs\.\*\.container\.credentials\..*", "github needs strategy matrix env vars secrets inputs", ""),
    (r"jobs\.\*\.container\.env\..*", "github needs strategy matrix job runner env vars secrets inputs", ""),
    (r"jobs\.\*\.container(\..*)?", "github needs strategy matrix vars inputs", ""),
    (r"jobs\.\*\.continue-on-error", "github needs strategy matrix vars inputs", ""),
    (r"jobs\.\*\.defaults\.run(\..*)?", "github needs strategy matrix env vars inputs", ""),
    (r"jobs\.\*\.env\..*", "github needs strategy matrix vars secrets inputs", ""),
    (r"jobs\.\*\.environment\.url", "github needs strategy matrix job runner env vars steps inputs", ""),
    (r"jobs\.\*\.environment(\..*)?", "github needs strategy matrix vars inputs", ""),
    (r"jobs\.\*\.if", "github needs vars inputs", "always cancelled success failure"),
    (r"jobs\.\*\.name", "github needs strategy matrix vars inputs", ""),
    (r"jobs\.\*\.outputs\..*", STEP_CONTEXTS, ""),
    (r"jobs\.\*\.runs-on(\..*)?", "github needs strategy matrix vars inputs", ""),
    (r"jobs\.\*\.secrets\..*", "github needs strategy matrix secrets inputs vars", ""),
    (r"jobs\.\*\.services\.[^.]+\.credentials\..*", "github needs strategy matrix env vars secrets inputs", ""),
    (r"jobs\.\*\.services\.[^.]+\.env\..*", "github needs strategy matrix job runner env vars secrets inputs", ""),
    (r"jobs\.\*\.services(\..*)?", "github needs strategy matrix vars inputs", ""),
    (r"jobs\.\*\.steps\.if", "github needs strategy matrix job runner env vars steps inputs", "always cancelled success failure hashFiles"),
    (r"jobs\.\*\.steps\.(continue-on-error|env|name|run|timeout-minutes|with|working-directory)(\..*)?", STEP_CONTEXTS, "hashFiles"),
    (r"jobs\.\*\.strategy(\..*)?", "github needs vars inputs", ""),
    (r"jobs\.\*\.timeout-minutes", "github needs strategy matrix vars inputs", ""),
    (r"jobs\.\*\.with\..*", "github needs strategy matrix inputs vars", ""),
    (r"on\.workflow_dispatch\.inputs\.[^.]+\.default", "github inputs vars", ""),
]


def workflow_expressions(wf):
    """(the key, the expression) of every ${{ }} in a workflow read by YAML, and the whole value of an `if`, which is an expression with or without them"""
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, path + ["on" if k is True else str(k)])         # YAML 1.1 reads the key `on` as true
        elif isinstance(node, list):
            for v in node:
                walk(v, path)                                          # the place of a step in its list is not part of the name of its key
        elif isinstance(node, str):
            key = re.sub(r"^jobs\.[^.]+", "jobs.*", ".".join(path))
            exprs = re.findall(r"\$\{\{(.*?)\}\}", node, re.S)
            if path and path[-1] == "if" and not exprs:
                exprs = [node]
            found.extend((key, e) for e in exprs)

    walk(wf, [])
    return found


def expression_problems(key, expr):
    """what the table says against an expression at a key: a context or a special function that is not there, or no entry for the key at all"""
    for pattern, contexts, functions in AVAILABLE:
        if re.fullmatch(pattern, key):
            break
    else:
        return ["no entry for the key %s in the table of where the contexts are available" % key]
    text = re.sub(r"'(?:[^']|'')*'", "''", expr)                       # what is between quotes is not an expression
    used = set(re.findall(r"(?<![.\w])(%s)\b(?=\s*[.\[])" % "|".join(CONTEXT_NAMES), text))      # a name after a dot is a property (steps.x.outputs.matrix), not the context of that name
    called = set(re.findall(r"(?<![.\w])([A-Za-z_]\w*)\s*\(", text)) & set(SPECIAL_FUNCTIONS)
    return (["the context %s is not available at %s" % (c, key) for c in sorted(used - set(contexts.split()))] +
            ["%s() is not available at %s" % (f, key) for f in sorted(called - set(functions.split()))])


def workflow_problems(wf):
    return ["%s: %s" % (key, p) for key, expr in workflow_expressions(wf) for p in expression_problems(key, expr)]


def at(wf, *path):
    for p in path:
        wf = wf[p]
    return wf


def workflow_checks():
    try:
        import yaml
    except ImportError:
        check("PyYAML is there to read the workflow", False, "install python3-yaml")
        return
    with open(WORKFLOW, encoding="utf-8") as f:
        wf = yaml.safe_load(f)
    exprs = workflow_expressions(wf)
    check("the workflow has expressions to judge, in more than one kind of key", len(exprs) > 10 and len({k for k, _ in exprs}) > 8, "%d expressions" % len(exprs))
    check("every expression of the workflow is where GitHub has its contexts and functions", not workflow_problems(wf), "; ".join(workflow_problems(wf)))
    # the check says no
    def mutated(path, value):
        m = copy.deepcopy(wf)
        at(m, *path[:-1])[path[-1]] = value
        return workflow_problems(m)
    for label, path, value, needle in (
            ("a runner context in the env of a job", ("jobs", "vm", "env", "WORK"), "${{ runner.temp }}/oup-work", "context runner is not available at jobs.*.env.WORK"),
            ("a steps context in runs-on", ("jobs", "vm", "runs-on"), "${{ steps.a.outputs.b }}", "context steps is not available at jobs.*.runs-on"),
            ("a secrets context in the if of a step", ("jobs", "vm", "steps", 0, "if"), "secrets.X == 'y'", "context secrets is not available at jobs.*.steps.if"),
            ("an env context in the timeout of a job", ("jobs", "vm", "timeout-minutes"), "${{ env.X }}", "context env is not available at jobs.*.timeout-minutes"),
            ("a status function in a run", ("jobs", "vm", "steps", 1, "run"), "echo ${{ always() }}", "always() is not available at jobs.*.steps.run"),
            ("a hash of files in the if of a job", ("jobs", "plan", "if"), "hashFiles('x') != ''", "hashFiles() is not available at jobs.*.if"),
            ("a job context in the name of a job", ("jobs", "vm", "name"), "${{ job.status }}", "context job is not available at jobs.*.name"),
            ("a key the table does not know", ("jobs", "vm", "bogus"), "${{ github.sha }}", "no entry for the key jobs.*.bogus"),
            ("a context in a string that is not an expression", ("jobs", "vm", "steps", 1, "run"), "echo runner.temp '${{ github.sha }}'", None)):
        got = mutated(path, value)
        check("the workflow check: %s" % label, (any(needle in g for g in got) and len(got) >= 1) if needle else not got, "got %s" % got)
    got = mutated(("jobs", "vm", "steps", 0, "if"), "always() && hashFiles('x') != '' && steps.a.outcome == 'success' && matrix.leg == 'core' && runner.os == 'Linux'")
    check("the workflow check lets through what a step's if may use", not got, "got %s" % got)
    got = mutated(("jobs", "plan", "outputs", "matrix"), "${{ steps.plan.outputs.matrix }}")
    check("the workflow check does not take a property named like a context for the context", not got, "got %s" % got)


def static_checks():
    p = subprocess.run([BASH, "-n", posix(SCRIPT)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    check("the script reads under bash -n", p.returncode == 0, p.stdout.decode("utf-8", "replace"))
    sc = shutil.which("shellcheck") if os.environ.get("SHELLCHECK") else None
    if sc:
        p = subprocess.run([sc, "-x", "-S", "warning", SCRIPT], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        check("the script passes shellcheck", p.returncode == 0, p.stdout.decode("utf-8", "replace")[:2000])
    with open(SCRIPT, newline="") as f:
        script = f.read().replace("\r\n", "\n")
    guest = re.search(r"cat >\"\$1/guest\.sh\" <<'OUP_GUEST'\n(.*?)\nOUP_GUEST\n", script, re.S)
    check("the guest tools are in the script", bool(guest))
    if guest:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n", dir=os.environ.get("TMPDIR") or None)
        tmp.write(guest.group(1) + "\n")
        tmp.close()
        p = subprocess.run([BASH, "--posix", "-n", posix(tmp.name)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        os.unlink(tmp.name)
        check("the guest tools read as POSIX sh", p.returncode == 0, p.stdout.decode("utf-8", "replace"))
        check("the guest tools use no bashism the hop could break (no [[ test, no arrays, no $'..', no <<<, no process substitution)",
              not re.search(r"\[\[\s|<<<|<\(|\$'|\bdeclare\b|\blocal -a\b|\bmapfile\b|\bEPOCHREALTIME\b", guest.group(1)), "")
    guest_checks()
    workflow_checks()
    legs = json.load(open(LEGS_JSON))
    check("the legs file names the two legs, with what the workflow needs", [l["leg"] for l in legs] == ["core", "faults"] and all(
        {"leg", "name", "label", "id", "version", "base", "image", "sums", "algo", "args", "docker_from", "timeout", "prove_timeout"} <= set(l) for l in legs), "")
    check("the faults leg has more time than the core leg, and the proof step less than the job", legs[1]["timeout"] > legs[0]["timeout"] and all(l["prove_timeout"] < l["timeout"] for l in legs), "")
    try:
        import yaml
        wf = yaml.safe_load(open(WORKFLOW, encoding="utf-8"))
    except ImportError:
        wf = None
    if wf is not None:
        triggers = wf.get("on", wf.get(True))
        check("the workflow is dispatched by hand only, with the tag and the legs", list(triggers) == ["workflow_dispatch"] and set(triggers["workflow_dispatch"]["inputs"]) == {"tag", "legs"}, str(triggers))
        check("the legs the workflow offers are the legs of the file", triggers["workflow_dispatch"]["inputs"]["legs"]["options"] == ["all", "core", "faults"], "")
        vm = wf["jobs"]["vm"]
        check("the legs run one at a time, core first, and a failing core leg cancels the faults leg", vm["strategy"]["max-parallel"] == 1 and vm["strategy"]["fail-fast"] is True, str(vm["strategy"]))
        text = open(WORKFLOW, encoding="utf-8").read()
        check("the workflow publishes nothing", not re.search(r"gh release (create|upload|edit)|git push|actions/(create-release|upload-release)", text), "")
        check("the workflow runs this test, the script and the report", "test-os-upgrade-proof.py" in text and "os-upgrade-proof.sh" in text and "os-upgrade-proof-report.py" in text, "")
        perms = wf.get("permissions")
        check("the workflow reads the repository and nothing else", perms == {"contents": "read"}, str(perms))
    check("the preparation of the guest keeps cloud-init from rewriting /etc/hosts (sudo would say it cannot look the name up, to every command)", "manage_etc_hosts: false" in script and "/etc/cloud/cloud.cfg.d/" in script, "")
    check("the start and the refresh wait five minutes for their answer", script.count('"${START_PATH}" "${body}" 300') == 2 and script.count("""POST "${REFRESH_PATH}" '{}' 300""") == 2, "")
    # the names the report gates on are the markers the script writes
    for cid in ("C1", "C2", "C3", "C4", "C5", "C6", "C7"):
        check("the script writes the markers of %s" % cid, ("marker case_%s_start" % cid) in script and ("marker case_%s_done" % cid) in script, "")
    check("the script writes the markers of the faults (one pair, by id)", 'marker "case_${id}_start"' in script and 'marker "case_${id}_done"' in script or all(("case_F%d_done" % i) in script for i in range(1, 5)), "")


def main():
    static_checks()
    if os.environ.get("STATIC_ONLY"):
        return
    keep = KEEP
    base_core = base_faults = w_core = w_faults = None
    if os.environ.get("SCENARIOS_ONLY"):
        scenarios_core()
        scenarios_faults(FaultsBase())
        return
    if keep and os.path.isdir(os.path.join(keep, "core")) and os.environ.get("REPORT_ONLY"):
        base_core, base_faults = os.path.join(keep, "core"), os.path.join(keep, "faults")
    else:
        base_core, w_core, proc = run_script("core", "")
        base_faults, w_faults, procf = run_script("faults", "")
        for label, proc_ in (("core", proc), ("faults", procf)):
            tail = proc_.stdout.decode("utf-8", "replace")[-400:]
            check("the script runs the %s leg to its end against the fake guest" % label, proc_.returncode == 0, tail)
        if keep:
            for n, src in (("core", base_core), ("faults", base_faults)):
                shutil.rmtree(os.path.join(keep, n), ignore_errors=True)
                shutil.copytree(src, os.path.join(keep, n))
        # what the fake saw of the runner: the rehearsal of the page has no safety net and comes first, the rest of the starts have the one of the hop; the kill of the second fault and the reset of the fourth
        # fell in the middle of dpkg, and the kill of the first, in a block where there is none
        check("the runner starts the rehearsal the way the page does (no safety net) first, then with the safety net of the hop", w_core.starts == [(True, ""), (True, "snapshot"), (False, "snapshot"), (False, "snapshot")], str(w_core.starts))
        check("the runner kills the second unit and resets the machine in the middle of dpkg, and kills the first before dpkg", w_faults.faultlog == [("kill", False), ("kill", True), ("reset", True)], str(w_faults.faultlog))
    rc, text = report(base_core, "core")
    check("a healthy core leg passes, every case by name", rc == 0 and head(text) == "PASS" and all(verdict_line(text, n) and verdict_line(text, n)[0] == "PASS" for n in (
        "C1 flag off", "C2 switch on", "C3 refresh", "C4 plan and rehearsal", "C5 hostname_mismatch", "C5 acks_missing", "C5 dpkg lock", "C5 run_open", "C5 os_upgrade_open",
        "C5 nothing left behind", "C6 call-off", "C7 the hop")), text[:2500])
    check("a healthy core leg prints the first real-guest numbers", all(k in text for k in ("seconds per block", "downloaded:", "free space:", "kernel:", "away at the restart:", "restart to the verdict:", "refresh:", "rehearsal:")), text[-1800:])
    rc, text = report(base_faults, "faults")
    check("a healthy faults leg passes, every fault by name", rc == 0 and head(text) == "PASS" and all(verdict_line(text, n) and verdict_line(text, n)[0] == "PASS" for n in (
        "F1 the unit killed before", "F2 the unit killed after", "F3 the network cut after", "F4 a power cut")), text[:2500])
    check("a healthy faults leg catches the interruption of the first fault with its polls of one second (the road through unwinding is for the polls that miss it)", "did not catch" not in text, text[:1500])
    check("a healthy faults leg prints the clock it corrected and the dpkg it saw", all(k in text for k in ("F1 clock:", "F2 clock:", "F4 clock:", "F4-reset clock:", "F2 dpkg:", "F4 dpkg:")), text[-1800:])
    run_mutations("core", base_core, "core", core_mutations())
    run_mutations("faults", base_faults, "faults", faults_mutations())
    if os.environ.get("REPORT_ONLY") or os.environ.get("BASE_ONLY"):
        return
    # the scenarios
    scenarios_core()
    scenarios_faults(FaultsBase(w_faults, base_faults))


def scenarios_core():
    env = {"WAIT_BOOT": "60"}
    scenario("scenario: a core that starts with the flag off", "core", "c1", {"start_ignores_beta": True}, fails("C1 flag off", "POST start answers beta_off"))
    out, _, _ = scenario("scenario: a token that runs out is renewed by the runner", "core", "c1,c2", {"token_valid_s": 100}, passes("C1 flag off"))
    if out:
        check("scenario: the runner logged in again when the token ran out", any(n.endswith(".notes") and "relogin" in read_out(out, n) for n in os.listdir(out)), "")
    scenario("scenario: a discard that leaves the timers stopped", "core", "c2,c4", {"timers_stay_stopped": True}, fails("C4 plan and rehearsal", "the timers of apt are as they were"))
    scenario("scenario: a start that dpkg's lock does not stop", "core", "c2,c4,c5", {"lock_ignored": True}, fails("C5 dpkg lock", "is refused"))
    scenario("scenario: a lock that cannot be given back", "core", "c2,c4,c5", {"lock_stays": True}, invalid_run("dpkg's lock was not given back"))
    scenario("scenario: a page restart that is carried out", "core", "c2,c4,c5", {"restart_unguarded": True}, fails("C5 os_upgrade_open", "is refused while the unit works"))
    scenario("scenario: a call-off that comes too late", "core", "c2,c4,c6", {"abort_refused": True}, invalid_run("the call-off was refused"))
    scenario("scenario: a guest that never comes back", "core", "c2,c4,c7", {"no_boot_back": True}, invalid_run("did not come back"), env_extra=env)
    scenario("scenario: a mirror that is down while the plan is made", "core", "c2,c4", {"plan_fails": True}, invalid_run("could not reach a mirror"))
    scenario("scenario: a plan that is blocked", "core", "c2,c4,c5,c6,c7", {"plan_blocked": True}, fails("C4 plan and rehearsal", "can be started from"))
    scenario("scenario: a hop that ends with findings", "core", "c2,c4,c7", {"findings": True}, passes("C7 the hop", "docker_source_unreachable"))
    scenario("scenario: a refresh that needs no restart", "core", "c2,c3", {"refresh_wants_reboot": False}, passes("C3 refresh"))
    # the start answers when it has taken the facts of the machine: a minute and a half is the ordinary case (the healthy runs have it), and more than the runner waits is not "no run"
    scenario("scenario: a start that takes four minutes to answer", "core", "c2,c4", {"start_takes_s": 240}, passes("C4 plan and rehearsal"))
    scenario("scenario: a start that answers after the runner stopped waiting began a run that nobody knows of", "core", "c2,c4", {"start_takes_s": 400}, invalid_run("a run is open that the runner does not know of (run 0b1c2d3e-0001"))
    scenario("scenario: a refresh that answers after the runner stopped waiting", "core", "c2,c3", {"start_takes_s": 400}, invalid_run("a run is open that the runner does not know of (run 0b1c2d3e-0001"))
    scenario("scenario: a hop that ends with findings of every severity but error", "core", "c2,c4,c7", {"findings": "mixed"}, passes("C7 the hop", "info docker_source_paused"))


def scenarios_faults(base):
    """each fault, on a fake that does it wrong, from the snapshot of the healthy faults run"""
    scenario("scenario: a core that repairs a killed hop by itself", "faults", "f2", {"auto_repair": True}, fails("F2 the unit killed after", "no automatic repair"), base=base)
    scenario("scenario: a network cut that the run does not feel", "faults", "f3", {"net_cut_ignored": True}, not_testable("F3 the network cut after", "nothing left to download"), base=base)
    scenario("scenario: a network that cannot be given back", "faults", "f3", {"route_stays_cut": True}, invalid_run("the network was not given back"), base=base)
    scenario("scenario: a cut that leaves the guest on the Internet", "faults", "f3", {"v6_stays": True}, invalid_run("the network could not be cut"), base=base)
    scenario("scenario: a cut that is missed by the first two ssh", "faults", "f3", {"ssh_drops": {"route_cut": 2, "route_restore": 2, "set_clock": 1}}, passes("F3 the network cut after"), base=base)
    scenario("scenario: a repair after a power cut that stops with a class", "faults", "f4", {"resume_fails": True}, passes("F4 a power cut", "FINDING"), base=base)
    scenario("scenario: a repair that stops with a class nobody knows", "faults", "f4", {"resume_fails": True, "resume_fail_class": "zzz"}, fails("F4 a power cut", "precise class"), base=base)
    scenario("scenario: a repair that does not run after a kill", "faults", "f2", {"resume_fails": True}, fails("F2 the unit killed after", "resume completes the hop"), base=base)
    # what a real guest does that a tidy one does not
    scenario("scenario: a kill whose reply is lost, and said again while the unwind holds the name", "faults", "f1", {"ssh_drops": {}, "ssh_lost": {"kill_unit": 1}}, passes("F1 the unit killed before"), base=base)
    scenario("scenario: a kill that no ssh reaches", "faults", "f1", {"ssh_drops": {"kill_unit": 3}}, invalid_run("the unit could not be killed"), base=base)
    scenario("scenario: an invocation that no ssh asks for", "faults", "f1", {"ssh_drops": {"unit_id": 3}}, invalid_run("did not answer when the invocation of the unit was asked for"), base=base)
    scenario("scenario: an interruption that the polls miss", "faults", "f1", {"notice_s": 1.5, "interrupted_s": 0.5}, passes("F1 the unit killed before", "did not catch"), base=base)
    scenario("scenario: a clock that no ssh reaches", "faults", "f1", {"ssh_drops": {"set_clock": 3}}, invalid_run("the clock of the restored guest could not be set"), base=base)
    scenario("scenario: a dpkg that is never seen at work after the point of no return", "faults", "f2", {"dpkg_never": True}, not_testable("F2 the unit killed after", "dpkg was not seen"), base=base)
    scenario("scenario: a dpkg that is never seen at work in block 6", "faults", "f4", {"dpkg_never": True}, not_testable("F4 a power cut", "dpkg was not seen"), base=base)


if __name__ == "__main__":
    main()
    print("\n%d checks, %d failed" % (count[0], len(failures)))
    for f in failures:
        print("FAILED " + f[:600])
    sys.exit(1 if failures else 0)
