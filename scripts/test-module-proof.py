#!/usr/bin/env python3
"""The proof has to be able to say no. A FAKE guest, with the core's own answers and a clock that only moves when the runner sleeps, plays the machine: the real runner script
(module-proof.sh) is run against it, with stand-ins for ssh, for curl's server and for sleep, and what it records is handed to the real report (module-proof-report.py). Then the
recordings are mutated, one fault at a time, and the report has to say what it should of each:

    python3 scripts/test-module-proof.py

A healthy run passes. Every verdict has a mutation in which only the thing it judges is wrong, and that mutation must fail by that name; a run that did not get as far as it should (the runner
stopped early, a snapshot of the guest is missing, an undo failed, the release has no assistant, a mirror failed) must be INVALID and never green; what this guest cannot reach is NOT
TESTABLE, with its reason, and neither a pass nor a fail.

The fake plays the answers the assistant and the core give (the JSON of list, plan and status, the markers and the sentences of the log in the grammar of pkg/modulecmd, the envelope of every
route, the job reduced from the log and the unit as docs/os-modules.md says) and a systemd that shows a unit for a moment only. It models what a real guest does that a tidy one does not: a login
makes a scope of its own that comes and goes, an ssh is missed now and then (the first call of some verbs), a unit stays for a while after the process in it is killed, apt goes on when the assistant
that started it is killed, a state is visible for a second. What the real tools, systemd, apt and the assistant say on a real guest is what the first dispatch of the workflow shows: nothing here
proves that.

The kit is the real one: the script's own build-kit is run against a copy of the module's tree with a stand-in for `go`, and what it writes is held against the rules of the core's readers as this
file states them (the catalogue, the manifest, the unpack of an archive): the good archive is accepted and each hostile one is refused with the code the case expects.

REPORT=<path>, SCRIPT=<path> and WORKFLOW=<path> run the cases against other copies, to see that a broken one is caught. CASES=<regex> runs the checks whose name matches.
KEEP=<dir> keeps the recordings of the healthy runs there; with REPORT_ONLY=1 they are used again instead of being made (only the mutations of the recordings are then run: minutes, not the
time that the runs of the script take), and BASE_ONLY=1 stops after the mutations, before the scenarios. SHELLCHECK=1 also runs shellcheck on the script (the workflow does not).
TEST_BASH=<path> names a bash where plain `bash` is not the one (on Windows: the MSYS bash of Git, with a PATH that has no WindowsApps stub of python3: this harness puts a `python3` of its own,
and the directories of that bash's tools, first in the PATH of the script).
"""
import copy
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.environ.get("REPORT") or os.path.join(HERE, "module-proof-report.py")
SCRIPT = os.environ.get("SCRIPT") or os.path.join(HERE, "module-proof.sh")
WORKFLOW = os.environ.get("WORKFLOW") or os.path.join(HERE, "..", ".github", "workflows", "module-proof.yml")
LEGS_JSON = os.path.join(HERE, "module-proof-legs.json")
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


def sha(data):
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


# ---- the module, as far as the kit needs it ---------------------------------------------------------------------------------------------------

MODULE_JSON = """{
  "schema": 1,
  "name": "virt",
  "title": "Virtual machines",
  "component": "CasaOS-VirtManagement",
  "unit": "casaos-virt-management.service",
  "gateway_route": "/v2/virt_management",
  "packages": {
    "common": ["qemu-utils", "libvirt-daemon-system", "libvirt-clients"],
    "amd64": ["qemu-system-x86", "ovmf"],
    "arm64": ["qemu-system-arm", "qemu-efi-aarch64"]
  },
  "min_recasaos": "0.5.23",
  "os": ["debian:12", "debian:13", "ubuntu:24.04", "ubuntu:26.04"]
}
"""
UNIT_TEXT = """[Unit]
After=casaos-gateway.service
After=casaos-message-bus.service
After=libvirtd.service virtqemud.service
Wants=libvirtd.service
Description=CasaOS Virtual Machine Management Service

[Service]
ExecStartPre=/usr/bin/casaos-virt-management -v
ExecStart=/usr/bin/casaos-virt-management -c /etc/casaos/virt-management.conf
PIDFile=/var/run/casaos/virt-management.pid
Restart=always
Type=notify

[Install]
WantedBy=multi-user.target
"""
SAMPLE_TEXT = """[common]
RuntimePath = /var/run/casaos

[libvirt]
; empty: try /run/libvirt/libvirt-sock, then /run/libvirt/virtqemud-sock
Socket =
; empty: qemu:///system (test:///default is libvirt's test driver, for tests)
URI =

[storage]
Path = /DATA/VM
"""
GO_STUB = """#!/bin/sh
# a stand-in for the go command: it records how it was called, and writes a program where `go build -o` is told to
echo "CGO_ENABLED=$CGO_ENABLED GOOS=$GOOS GOARCH=$GOARCH go $*" >>"$GO_CALLS"
case "$1" in
build)
    while [ "$#" -gt 0 ]; do
        if [ "$1" = -o ]; then out="$2"; fi
        shift
    done
    mkdir -p "$(dirname "$out")"
    printf '#!/bin/sh\\necho v0.1.0\\n' >"$out"
    chmod +x "$out"
    ;;
esac
exit 0
"""


def make_module(root):
    """a tree of the module as its repository has it, committed in git"""
    mod = os.path.join(root, "module")
    for rel, text in (("module.json", MODULE_JSON), ("LICENSE", "Apache License\n"), ("NOTICE", "CasaOS Virtual Machine Management\n"),
                      ("build/sysroot/usr/lib/systemd/system/casaos-virt-management.service", UNIT_TEXT), ("build/sysroot/etc/casaos/virt-management.conf.sample", SAMPLE_TEXT),
                      ("main.go", "package main\n")):
        path = os.path.join(mod, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="\n") as f:
            f.write(text)
    env = dict(ENV, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    for argv in (["init", "-q", "."], ["add", "-A"], ["commit", "-q", "-m", "module"]):
        subprocess.run(["git"] + argv, cwd=mod, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)
    return mod


# ---- GitHub's table of what a context is available where --------------------------------------------------------------------------------------

CONTEXTS = ("github", "needs", "strategy", "matrix", "job", "runner", "env", "vars", "secrets", "steps", "inputs")
# the rows of "Context availability" of the documentation of workflow syntax: the key, and the contexts an expression there may use. A key not in the table has no expression.
AVAILABLE = [
    (r"^run-name$", {"github", "inputs", "vars"}),
    (r"^concurrency(\..*)?$", {"github", "inputs", "vars"}),
    (r"^env(\..*)?$", {"github", "secrets", "inputs", "vars"}),
    (r"^jobs\.[^.]+\.concurrency(\..*)?$", {"github", "needs", "strategy", "matrix", "inputs", "vars"}),
    (r"^jobs\.[^.]+\.container$", {"github", "needs", "strategy", "matrix", "vars", "inputs"}),
    (r"^jobs\.[^.]+\.container\.credentials(\..*)?$", {"github", "needs", "strategy", "matrix", "env", "vars", "secrets", "inputs"}),
    (r"^jobs\.[^.]+\.container\.env(\..*)?$", {"github", "needs", "strategy", "matrix", "job", "runner", "env", "vars", "secrets", "inputs"}),
    (r"^jobs\.[^.]+\.continue-on-error$", {"github", "needs", "strategy", "vars", "matrix", "inputs"}),
    (r"^jobs\.[^.]+\.defaults\.run(\..*)?$", {"github", "needs", "strategy", "matrix", "env", "vars", "inputs"}),
    (r"^jobs\.[^.]+\.env(\..*)?$", {"github", "needs", "strategy", "matrix", "vars", "secrets", "inputs"}),
    (r"^jobs\.[^.]+\.environment(\.name)?$", {"github", "needs", "strategy", "matrix", "vars", "inputs"}),
    (r"^jobs\.[^.]+\.environment\.url$", {"github", "needs", "strategy", "matrix", "job", "runner", "env", "vars", "steps", "inputs"}),
    (r"^jobs\.[^.]+\.if$", {"github", "needs", "vars", "inputs"}),
    (r"^jobs\.[^.]+\.name$", {"github", "needs", "strategy", "matrix", "vars", "inputs"}),
    (r"^jobs\.[^.]+\.outputs\..*$", {"github", "needs", "strategy", "matrix", "job", "runner", "env", "vars", "secrets", "steps", "inputs"}),
    (r"^jobs\.[^.]+\.runs-on(\..*)?$", {"github", "needs", "strategy", "matrix", "vars", "inputs"}),
    (r"^jobs\.[^.]+\.secrets(\..*)?$", {"github", "needs", "strategy", "matrix", "secrets", "inputs", "vars"}),
    (r"^jobs\.[^.]+\.services$", {"github", "needs", "strategy", "matrix", "vars", "inputs"}),
    (r"^jobs\.[^.]+\.services\.[^.]+\.credentials(\..*)?$", {"github", "needs", "strategy", "matrix", "env", "vars", "secrets", "inputs"}),
    (r"^jobs\.[^.]+\.services\.[^.]+\.env(\..*)?$", {"github", "needs", "strategy", "matrix", "job", "runner", "env", "vars", "secrets", "inputs"}),
    (r"^jobs\.[^.]+\.steps\.\d+\.(continue-on-error|env(\..*)?|name|run|timeout-minutes|with(\..*)?|working-directory)$",
     {"github", "needs", "strategy", "matrix", "job", "runner", "env", "vars", "secrets", "steps", "inputs"}),
    (r"^jobs\.[^.]+\.steps\.\d+\.if$", {"github", "needs", "strategy", "matrix", "job", "runner", "env", "vars", "steps", "inputs"}),
    (r"^jobs\.[^.]+\.strategy(\..*)?$", {"github", "needs", "vars", "inputs"}),
    (r"^jobs\.[^.]+\.timeout-minutes$", {"github", "needs", "strategy", "matrix", "vars", "inputs"}),
]
# the special functions: where each may be used (the status check functions in the `if` of a job or a step, hashFiles in a step)
STATUS_FUNCTIONS = ("always", "cancelled", "success", "failure")
IF_KEY = re.compile(r"^jobs\.[^.]+\.(steps\.\d+\.)?if$")
STEP_KEY = re.compile(r"^jobs\.[^.]+\.steps\.\d+\.")


def walk_workflow(node, path=""):
    """(key path, string) of every string value of a workflow; a list's items are numbered"""
    if isinstance(node, dict):
        for k, v in node.items():
            k = str(k)
            yield from walk_workflow(v, path + "." + k if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from walk_workflow(v, "%s.%d" % (path, i))
    elif isinstance(node, str):
        yield path, node


def expressions_of(path, value):
    """the expressions of a value: the ${{ }} parts, or the whole of an `if`"""
    found = re.findall(r"\$\{\{(.*?)\}\}", value, re.S)
    if not found and IF_KEY.match(path):
        found = [value]
    return found


def context_problems(workflow):
    """the expressions of a workflow that use a context its key may not, or a function it may not, or sit where the table has no row"""
    problems = []
    for path, value in walk_workflow(workflow):
        if path.startswith("on.") or path.startswith("True.") or path == "name":
            continue
        for expr in expressions_of(path, value):
            bare = re.sub(r"'[^']*'", "''", expr)
            used = set(re.findall(r"(?<![\w.'-])(%s)\s*[.\[]" % "|".join(CONTEXTS), bare))
            funcs = set(re.findall(r"\b([A-Za-z]+)\s*\(", bare))
            row = next((ctx for rx, ctx in AVAILABLE if re.match(rx, path)), None)
            if row is None:
                problems.append("%s: an expression where the table has no row: %s" % (path, expr.strip()[:60]))
                continue
            bad = sorted(used - row)
            if bad:
                problems.append("%s: the context %s is not available here: %s" % (path, ", ".join(bad), expr.strip()[:60]))
            for f in funcs & set(STATUS_FUNCTIONS):
                if not IF_KEY.match(path):
                    problems.append("%s: the function %s() is only for an `if`: %s" % (path, f, expr.strip()[:60]))
            if "hashFiles" in funcs and not STEP_KEY.match(path):
                problems.append("%s: hashFiles() is only for the steps: %s" % (path, expr.strip()[:60]))
    return problems


# ---- the guest and its core: the answers of the real ones --------------------------------------------------------------------------------------------

EPOCH0 = 1790000000.0
OS_BY_LEG = {"d12": ("debian", "12"), "u24": ("ubuntu", "24.04")}
CORE_UNITS = ["casaos.service", "casaos-gateway.service", "casaos-user-service.service", "casaos-message-bus.service", "casaos-local-storage.service", "casaos-app-management.service", "rclone.service"]
BASE_PKGS = ["libc6", "systemd", "apt", "dash", "curl", "openssh-server", "docker-ce", "samba", "python3", "cloud-image-utils"]
DEPS = {"libvirt-daemon-system": ["libvirt-daemon", "libvirt-daemon-driver-qemu", "libvirt-daemon-config-network"], "qemu-system-x86": ["qemu-system-common", "qemu-system-data", "qemu-block-extra"],
        "libvirt-clients": ["libvirt0"]}
PKG_FILES = {"libvirt-daemon-system": ["/etc/libvirt", "/etc/libvirt/libvirt.conf", "/etc/libvirt/libvirtd.conf", "/usr/bin/virt-admin", "/usr/lib/systemd/system/libvirtd.service"],
             "qemu-utils": ["/usr/bin/qemu-img"], "libvirt-clients": ["/usr/bin/virsh"], "qemu-system-x86": ["/usr/bin/qemu-system-x86_64"], "ovmf": ["/usr/share/OVMF"],
             "libvirt-daemon": ["/usr/sbin/libvirtd"], "libvirt-daemon-driver-qemu": [], "libvirt-daemon-config-network": [], "qemu-system-common": [], "qemu-system-data": [], "qemu-block-extra": [], "libvirt0": []}
BASE_NAMES = ["/etc", "/etc/apt", "/etc/casaos", "/etc/casaos/gateway.ini", "/usr/bin", "/usr/bin/casaos", "/usr/lib/systemd", "/usr/lib/systemd/system", "/var/lib/casaos", "/var/lib/casaos/db",
              "/var/lib/casaos/fork-release", "/var/lib/dpkg", "/var/lib/dpkg/status", "/var/lib/apt", "/var/cache/apt", "/var/log/casaos", "/var/log/apt", "/usr/share/casaos", "/DATA", "/var/tmp",
              "/run/systemd/transient", "/run/systemd/system"]
BASE_ETC = {"/etc/hostname": "h1", "/etc/passwd": "p1", "/etc/group": "g1", "/etc/casaos/gateway.ini": "gw1", "/etc/os-release": "os1", "/etc/apt/sources.list": "s1", "/etc/resolv.conf": "r1"}
ROLLBACK = ("stop", "files", "manifest", "reload")
HOSTILE = ("dotdot", "systemctl", "varlib", "usrshare", "etcmod")


def iso(t):
    return datetime.fromtimestamp(EPOCH0 + t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Event:
    def __init__(self, time_, seq, fn):
        self.time, self.seq, self.fn, self.cancelled = time_, seq, fn, False

    def cancel(self):
        self.cancelled = True


class Run:
    """one run of `casaos module install` or `remove`: by hand (in a foreground ssh) or in the transient unit of the core; its steps are events of the clock of the fake"""

    def __init__(self, w, kind, variant, nonce, via):
        self.w, self.kind, self.variant, self.nonce, self.via = w, kind, variant, nonce, via
        self.out = []                      # the lines of the standard output (the log, less what the tools print)
        self.finished, self.dead, self.rc = False, False, None
        self.cur, self.timer, self.step_begin = None, None, 0.0
        self.unpack_begun, self.packages_done, self.apt_started, self.work = False, False, False, False
        self.last_sign = w.t
        self.phases = []

    # ---- what a run says
    def say(self, line, tool=False):
        self.w.log_lines.append(line)
        self.w.log_mtime = self.w.t
        if not tool:
            self.out.append(line)
        self.last_sign = self.w.t
        self.w.on_log(self, line)

    def marker(self, step, phase):
        self.say("CASAOS_MODULE_STEP %s virt %s %s %s" % (self.nonce, step, phase, iso(self.w.t)))

    def result(self, verdict, step, code, undo):
        self.say("CASAOS_MODULE_RESULT %s virt %s %s %s %s %s" % (self.nonce, verdict, step or "-", code or "-", undo, iso(self.w.t)))

    def kept(self):
        if not self.packages_done:
            return "unknown"
        return " ".join(sorted(self.w.kit["packages"])) or "none"

    # ---- the run
    def start(self):
        w = self.w
        w.cur_run = self
        w.log_lines = []
        w.state_dir, w.state_mtime, w.log_exists, w.log_mtime = True, w.t, True, w.t
        self.timer = None
        if self.kind == "install":
            self.begin_install()
        else:
            self.begin_remove()

    def refuse(self, code):
        self.say("refused: %s" % code)
        self.result("refused", "-", code, "-")
        self.exit(2)

    def exit(self, rc):
        self.finished, self.rc = True, rc
        w = self.w
        w.tmp = []
        if w.cur_run is self:
            w.cur_run = None

    def begin_install(self):
        w = self.w
        plan = w.plan("virt", self.variant != "none", self.variant, "root")
        if self.expect_plan and plan.get("plan_id") != self.expect_plan and not plan.get("refused"):
            return self.refuse("plan_changed")
        if plan.get("refused"):
            return self.refuse(plan["refused"])
        if w.cfg.get("state_unsafe") or (w.symlink and w.cfg.get("symlink_unsafe")):
            return self.refuse("state_unsafe")
        if w.symlink:
            w.symlink = False                     # swept away, not followed
            if w.cfg.get("follow_symlink"):
                w.canary = (sha("overwritten"), "600", w.t)
        ss = w.cfg["step_seconds"]
        self.work = any(p not in w.installed for p in w.kit["packages"])
        self.phases = [("packages", ss["packages_work"] if self.work else ss["packages"], self.b_packages, self.e_packages, None),
                       ("download", ss["download"], self.b_download, None, None),
                       ("verify", ss["verify"], None, None, "checksum_mismatch" if self.variant == "corrupt" else None),
                       ("unpack", ss["unpack"], self.b_unpack, self.e_unpack, "unpack_path" if self.variant in HOSTILE else None),
                       ("units", ss["units"], None, None, None),
                       ("manifest", ss["manifest"], None, self.e_manifest, None),
                       ("start", ss["start"], None, self.e_start, None),
                       ("route", ss["route"], None, self.e_route, "route_timeout" if w.cfg.get("route_never") else None)]
        self.next_phase(0)

    expect_plan = None

    def next_phase(self, i):
        if self.finished or self.dead:
            return
        if i >= len(self.phases):
            self.marker("done", "done")
            if self.kind == "install":
                self.say("installed: virt %s" % self.w.kit["tag"])
                self.result("ok", "done", "-", "-")
            else:
                self.result("ok", "done", "-", "-")
                self.say("removed: virt")
                for ln in ("left in place: /DATA/VM, the libvirt network and pool, the machines", "  virsh list --all                     the machines (virsh destroy <machine>; virsh undefine <machine> --remove-all-storage)",
                           "  rm -r /DATA/VM                       the disks of the machines", "left in place: the packages %s" % " ".join(sorted(self.w.kit["packages"])),
                           "  apt-get purge %s" % " ".join(sorted(self.w.kit["packages"])), "nothing above was run"):
                    self.say(ln)
            return self.exit(0)
        name, dur, begin, end, fail = self.phases[i]
        self.cur, self.step_begin = name, self.w.t
        self.marker(name, "begin")
        if begin:
            begin()
        if self.dead or self.finished:
            return
        self.timer = self.w.at(dur, lambda: self.end_phase(i))

    def end_phase(self, i):
        if self.finished or self.dead:
            return
        name, dur, begin, end, fail = self.phases[i]
        if fail:
            return self.fail(name, fail)
        if end:
            end()
        self.marker(name, "done")
        self.next_phase(i + 1)

    def fail(self, step, code):
        w = self.w
        self.marker(step, "failed")
        self.say("failed at step %s: %s" % (step, code))
        if self.kind == "install" and self.unpack_begun:
            for s in ROLLBACK:
                self.marker("rollback_" + s, "begin")
                if s == "stop":
                    w.unit_active, w.unit_enabled = False, False
                    w.fs.pop("/etc/systemd/system/multi-user.target.wants/" + w.kit["unit"], None)
                if s == "files":
                    w.remove_module_files()
                if s == "manifest":
                    w.remove_manifest()
                if s == "reload":
                    w.fs.pop("/var/lib/casaos/modules/virt.files", None)
                    w.fs.pop("/var/lib/casaos/modules/virt.dirs", None)
                self.marker("rollback_" + s, "done")
            self.say("rolled back")
        if self.kind == "install" and code == "interrupted" and step == "packages" and self.apt_started:
            self.say("apt-get was ended while it worked: dpkg --configure -a is probably needed")
            if w.cfg.get("dpkg_interrupted") and self.work:
                w.dpkg_audit = 2
        if self.kind == "install":
            self.say("kept_packages: %s" % self.kept())
        self.timer = self.w.at(w.cfg["step_seconds"]["rollback"], lambda: (self.result("failed", step, code, "rolled_back" if self.kind == "install" else "-"), self.exit(3)))

    # ---- the signals
    def kill(self):
        """SIGKILL: nothing more is said, nothing is undone; apt, if it was started, goes on"""
        if self.finished or self.dead:
            return
        w = self.w
        if self.timer:
            self.timer.cancel()
        self.dead = True
        if w.cur_run is self:
            w.cur_run = None
        w.tmp = list(w.tmp)
        if self.cur == "packages" and self.work and w.cfg.get("orphan_apt", True):
            w.orphan_until = w.t + w.cfg["orphan_s"]
            w.at(w.cfg["orphan_s"], w.orphan_done)
        w.dead_unit_until = w.t + (w.cfg["orphan_s"] if w.orphan_until > w.t else 0.0)

    def term(self):
        """SIGTERM or a stop of the unit: the step in which it is fails with interrupted and the work is rolled back"""
        if self.finished or self.dead:
            return
        if self.timer:
            self.timer.cancel()
        self.apt_started = self.cur == "packages"
        self.fail(self.cur or "packages", "interrupted")

    def dpkg_active(self):
        return (not self.finished and not self.dead and self.cur == "packages" and self.work and 8.0 <= self.w.t - self.step_begin < self.w.cfg["step_seconds"]["packages_work"] - 1.0)

    # ---- the steps of an install
    def b_packages(self):
        self.apt_started = True
        self.say("| Reading package lists...", tool=True)
        self.say("| Setting up libvirt-daemon-system ...", tool=True)

    def e_packages(self):
        self.w.apt_install()
        self.packages_done = True

    def b_download(self):
        self.w.tmp = ["archive.tar.gz"]

    def b_unpack(self):
        w = self.w
        self.unpack_begun = True
        w.tmp = ["archive.tar"]
        if self.variant in HOSTILE:
            return
        w.write_lists(final=False)

    def e_unpack(self):
        self.w.write_module_files()
        self.w.write_lists(final=True)

    def e_manifest(self):
        self.w.write_manifest()

    def e_start(self):
        w = self.w
        w.fs["/etc/systemd/system/multi-user.target.wants/" + w.kit["unit"]] = dict(kind="l", mode="-", owner="-", target="/usr/lib/systemd/system/" + w.kit["unit"])
        w.unit_enabled, w.unit_active = True, True
        w.at(w.cfg["step_seconds"]["start"] / 2, w.route_up)

    def e_route(self):
        pass

    # ---- a remove
    def begin_remove(self):
        w = self.w
        present = any(p in w.fs for p in ("/etc/casaos/modules/virt.json", "/var/lib/casaos/modules/virt.files", "/var/lib/casaos/modules/virt.dirs", "/var/lib/casaos/modules/virt.version"))
        if not present:
            return self.refuse("not_installed")
        if w.lock_held or w.osflag:
            return self.refuse("busy")
        ss = w.cfg["step_seconds"]
        self.phases = [("stop", ss["rem_stop"], None, self.r_stop, None), ("files", ss["rem_files"], None, self.r_files, None), ("manifest", ss["rem_manifest"], None, self.r_manifest, None),
                       ("reload", ss["rem_reload"], None, self.r_reload, None)]
        self.next_phase(0)

    def r_stop(self):
        w = self.w
        w.unit_active, w.unit_enabled = False, False
        w.gw = False
        w.fs.pop("/etc/systemd/system/multi-user.target.wants/" + w.kit["unit"], None)

    def r_files(self):
        self.w.remove_module_files()

    def r_manifest(self):
        self.w.remove_manifest()

    def r_reload(self):
        self.w.fs.pop("/var/lib/casaos/modules/virt.files", None)
        self.w.fs.pop("/var/lib/casaos/modules/virt.dirs", None)
        if self.w.cfg.get("remove_leaves_file"):
            self.w.fs["/usr/bin/casaos-virt-management"] = dict(kind="f", mode="755", owner="0:0", sha=sha("left"))


class World:
    """the guest and its core, as the runner sees them: the clock moves only when the runner sleeps (tick)"""

    def __init__(self, kit_dir, leg, cfg=None):
        self.kit_dir = kit_dir
        with open(os.path.join(kit_dir, "kit.json"), encoding="utf-8") as f:
            self.kit = json.load(f)
        self.leg = leg
        self.cfg = dict(core_version="v0.4.75", shipped_catalogue=False, orphan_s=12, token_valid_s=10 ** 9, ssh_drops={"watch_start": 1, "api_env_on": 1, "snap": 1}, down_seconds=45,
                        step_seconds=dict(packages=6.0, packages_work=30.0, download=1.5, verify=0.1, unpack=0.4, units=1.2, manifest=0.1, start=4.0, route=2.5, rollback=3.0, rem_stop=1.5,
                                          rem_files=0.3, rem_manifest=0.1, rem_reload=0.8))
        self.cfg.update(cfg or {})
        self.t = 0.0
        self.heap, self.seq = [], 0
        self.lock = threading.RLock()
        self.boot = str(uuid.uuid4())
        self.down_until = 0.0
        self.core_down_until = 0.0
        self.hostname = "mp-1a2b3c4d"
        self.osid, self.osver = OS_BY_LEG[leg]
        self.fs = {}
        self.etc = dict(BASE_ETC)
        self.tmp = []
        self.installed, self.hold, self.auto = set(BASE_PKGS), set(), set()
        self.unseat_owned = set()
        self.apt = {"/var/lib/apt/extended_states": dict(size=10, mtime=5, sha=sha("ext0")), "/var/log/apt/history.log": dict(size=100, mtime=5, sha=sha("hist0")),
                    "/var/lib/dpkg/status": dict(size=1000, mtime=5, sha=sha("status0")), "/var/log/apt/eipp.log.xz": None}
        self.unit_enabled, self.unit_active, self.gw = False, False, False
        self.dirt_unit = False
        self.lock_held, self.osflag, self.osflag_dir, self.tmpfs, self.symlink = False, False, False, False, False
        self.canary = (sha("canary\n"), "600", 5)
        self.witness, self.vm_dir = False, False
        self.conf_sha = None
        self.manager_env, self.core_env = 0, 0
        self.inv = {u: "inv-%s-1" % u[:6] for u in CORE_UNITS}
        self.cur_run, self.log_lines, self.orphan_until, self.dead_unit_until = None, [], 0.0, 0.0
        self.api_job = False           # the log is a job of the core's
        self.journal = []
        self.dummies = set()
        self.watch = None
        self.token, self.user, self.pw = None, None, None
        self.log = []
        self.ssh_n, self.ssh_seen = 0, {}
        self.snap_n = 0
        self.dpkg_audit = 0
        self.reserved = {"/usr/bin/systemctl": sha("systemctl"), "/usr/bin/apt-get": sha("apt-get"), "/usr/bin/dpkg": sha("dpkg"), "/usr/bin/casaos": sha("casaos"), "/usr/bin/env": sha("env")}
        self.escaped = []
        self.reboot_count = 0
        self.state_dir, self.state_mtime, self.log_exists, self.log_mtime = False, 0.0, False, 0.0
        self.etc_modules_dir, self.etc_modules_mtime = False, 0.0
        self.token_born = 0.0
        self.verbs = []

    # ---- the clock
    def at(self, delay, fn):
        self.seq += 1
        ev = Event(self.t + delay, self.seq, fn)
        self.heap.append(ev)
        return ev

    def tick(self, dt):
        with self.lock:
            target = self.t + dt
            while True:
                due = sorted((e for e in self.heap if not e.cancelled and e.time <= target + 1e-9), key=lambda e: (e.time, e.seq))
                if not due:
                    break
                ev = due[0]
                self.heap.remove(ev)
                self.t = max(self.t, ev.time)
                ev.fn()
            self.t = target
            self.heap = [e for e in self.heap if not e.cancelled]

    def up(self):
        return self.t >= self.down_until

    def core_up(self):
        return self.up() and self.t >= self.core_down_until

    # ---- the module on the box
    def unit_path(self):
        return "/usr/lib/systemd/system/" + self.kit["unit"]

    def write_module_files(self):
        for f in self.kit["files"]:
            mode = f["mode"]
            if self.cfg.get("wrong_mode") and f["path"] == self.unit_path():
                mode = "4777"
            sha_ = f["sha256"] if not (self.cfg.get("wrong_content") and f["path"] == self.unit_path()) else sha("other")
            self.fs[f["path"]] = dict(kind="f", mode=mode, owner="0:0", sha=sha_)

    def write_lists(self, final):
        files = [(f["path"], f["sha256"]) for f in self.kit["files"]]
        files.sort(key=lambda x: x[0] == self.unit_path())     # the unit last
        text = "casaos-module-files 1\n" + "".join("f %s %s\n" % (s, p) for p, s in files)
        self.fs["/var/lib/casaos/modules/virt.files"] = dict(kind="f", mode="644", owner="0:0", sha=sha(text), text=text)
        dirs = self.cfg.get("dirs") or []
        if dirs:
            t = "".join(d + "\n" for d in dirs)
            self.fs["/var/lib/casaos/modules/virt.dirs"] = dict(kind="f", mode="644", owner="0:0", sha=sha(t), text=t)

    def write_manifest(self):
        self.etc_modules_dir, self.etc_modules_mtime = True, self.t
        self.fs["/var/lib/casaos/modules/virt.version"] = dict(kind="f", mode="644", owner="0:0", sha=sha(self.kit["tag"]), text=self.kit["tag"] + "\n")
        self.fs["/etc/casaos/modules/virt.json"] = dict(kind="f", mode="644", owner="0:0", sha=self.kit["manifest_sha256"])

    def remove_module_files(self):
        for f in self.kit["files"]:
            if self.cfg.get("remove_leaves_unit") and f["path"] == self.unit_path():
                continue
            self.fs.pop(f["path"], None)

    def remove_manifest(self):
        self.fs.pop("/etc/casaos/modules/virt.json", None)
        self.fs.pop("/var/lib/casaos/modules/virt.version", None)

    def route_up(self):
        if self.unit_active:
            self.gw = True
            self.vm_dir = True

    def apt_changed(self, what):
        for key in ("/var/lib/dpkg/status", "/var/log/apt/history.log", "/var/lib/apt/extended_states"):
            if key == "/var/lib/apt/extended_states" and what != "auto":
                continue
            e = self.apt[key]
            e["sha"], e["mtime"], e["size"] = sha("%s%s" % (key, self.t)), int(self.t) + 5, e["size"] + 7

    def apt_install(self):
        pk = self.kit["packages"]
        for p in pk:
            self.installed.add(p)
            self.auto.discard(p)
        for p in pk:
            for d in DEPS.get(p, []):
                if d not in self.installed:
                    self.installed.add(d)
                    self.auto.add(d)
        self.apt_changed("auto")
        if "libvirt-daemon-system" in self.installed:
            self.etc["/etc/passwd"] = "p2"
            self.etc["/etc/group"] = "g2"
            for p in ("/etc/libvirt/libvirt.conf", "/etc/libvirt/libvirtd.conf"):
                self.etc[p] = "libvirt"
        if self.cfg.get("etc_unexplained"):
            self.etc["/etc/hostname"] = "changed"

    def orphan_done(self):
        if self.orphan_until > 0:
            self.apt_install()
            self.orphan_until = 0.0

    def dpkg_busy(self):
        return self.lock_held or self.orphan_until > self.t or (self.cur_run is not None and self.cur_run.dpkg_active())

    def apt_sim(self):
        pk = self.kit["packages"]
        held = [p for p in pk if p in self.hold and p not in self.installed]
        if held:
            return dict(failure="held_package", held=held)
        new = [p for p in pk if p not in self.installed] + [d for p in pk if p not in self.installed for d in DEPS.get(p, []) if d not in self.installed]
        new = sorted(set(new))
        manual = [p for p in pk if p in self.auto and p in self.installed]
        return dict(failure=None, new=new, upgrade=sorted(self.cfg.get("upgrades", [])), manual=manual, download=900000 * len(new), extra=3100000 * len(new), uris=900000 * len(new))

    # ---- the assistant: what it reads, what it says
    def catalogue(self, env_test, variant):
        if env_test:
            v = self.kit["variants"].get(variant)
            if v is None:
                return None
            with open(os.path.join(self.kit_dir, v["archive"]), "rb") as f:
                return dict(sha=sha(f.read()))
        if self.cfg.get("shipped_catalogue"):
            return dict(sha=self.kit["archive"]["sha256"])
        return None

    def installed_manifest(self):
        return "/etc/casaos/modules/virt.json" in self.fs

    def plan(self, name, env_test, variant, user, expect=None):
        cat = self.catalogue(env_test, variant)
        order = ["catalogue", "os", "arch", "recasaos", "already_installed", "foreign_unit", "busy", "dpkg_state", "disk", "apt"]
        if name != "virt" or cat is None:
            checks = [dict(code="catalogue", ok=False, detail="not_in_catalogue")] + [dict(code=c, ok=False, detail="not_checked") for c in order[1:]]
            return dict(name=name, tag="", tier="", checks=checks, refused="catalogue", packages=dict(to_install=[], to_upgrade=[], download_bytes=0, extra_bytes=0, known=False), archive=dict(url=""), plan_id="")
        k = self.kit
        checks = [dict(code="catalogue", ok=True, detail="")]
        checks.append(dict(code="os", ok="%s:%s" % (self.osid, self.osver) in k["os"], detail=""))
        checks.append(dict(code="arch", ok=True, detail=""))
        checks.append(dict(code="recasaos", ok=True, detail=""))
        checks.append(dict(code="already_installed", ok=not self.installed_manifest(), detail="" if not self.installed_manifest() else "installed"))
        foreign = self.dirt_unit
        checks.append(dict(code="foreign_unit", ok=not foreign, detail="not_owned /etc/systemd/system/%s" % k["unit"] if foreign else ""))
        if self.osflag:
            checks.append(dict(code="busy", ok=False, detail="os_upgrade"))
        elif user != "root":
            checks.append(dict(code="busy", ok=False, detail="not_checked: needs root, the install checks it as root"))
        elif self.dpkg_busy():
            checks.append(dict(code="busy", ok=False, detail="package_manager"))
        else:
            checks.append(dict(code="busy", ok=True, detail=""))
        checks.append(dict(code="dpkg_state", ok=True, detail=""))
        standing = all(c["ok"] or c["detail"].startswith("not_checked") for c in checks)
        refusal = {}
        for c in checks:
            if not c["ok"] and not c["detail"].startswith("not_checked"):
                refusal.setdefault("code", c["code"])
        sim = self.apt_sim() if standing else None
        pk = dict(to_install=[], to_upgrade=[], download_bytes=0, extra_bytes=0, known=False)
        if standing and sim["failure"]:
            checks.append(dict(code="disk", ok=False, detail="not_checked"))
            checks.append(dict(code="apt", ok=False, detail="held %s" % " ".join(sim["held"])))
            refusal.setdefault("code", "held_package")
        elif standing:
            if self.tmpfs:
                checks.append(dict(code="disk", ok=False, detail="need %d bytes on /var/lib/casaos/modules/tmp, 1000000 free" % (sim["extra"] + 3 * k["archive"]["bytes"])))
                refusal.setdefault("code", "disk_short")
            else:
                checks.append(dict(code="disk", ok=True, detail=""))
            checks.append(dict(code="apt", ok=True, detail=""))
            pk = dict(to_install=sim["new"], to_upgrade=sim["upgrade"], download_bytes=sim["download"], extra_bytes=sim["extra"], known=True)
            if sim["manual"] and self.cfg.get("plan_writes_apt_state"):
                self.apt_changed("auto")
            if self.cfg.get("plan_writes_eipp"):
                self.apt["/var/log/apt/eipp.log.xz"] = dict(size=50, mtime=int(self.t), sha=sha("eipp"))
        else:
            checks.append(dict(code="disk", ok=False, detail="not_checked"))
            checks.append(dict(code="apt", ok=False, detail="not_checked"))
        refused = refusal.get("code")
        plan_id = ""
        if not refused:
            plan_id = "m1-" + sha(json.dumps([k["name"], k["tag"], cat["sha"], pk["to_install"], self.osid, self.osver]))[:32]
        return dict(name=name, tag=k["tag"], tier="community", checks=checks, refused=refused, packages=pk, archive=dict(url=k["archive"]["url"]), plan_id=plan_id)

    def list_json(self, env_test, variant):
        cat = self.catalogue(env_test, variant)
        out = dict(catalogue=dict(ok=True) if cat else dict(ok=False, error="none"), modules=[])
        if cat:
            k = self.kit
            out["modules"].append(dict(name="virt", title=k["title"], description="", tier="community", tag=k["tag"], catalogue=True,
                                      installed=dict(version=self.version()) if self.installed_manifest() else None, system=dict(supported=True, reasons=[])))
        elif self.installed_manifest():
            out["modules"].append(dict(name="virt", title=self.kit["title"], description="", tier="", tag="", catalogue=False, installed=dict(version=self.version()), system=dict(supported=False, reasons=["catalogue"])))
        return out

    def version(self):
        e = self.fs.get("/var/lib/casaos/modules/virt.version")
        return e["text"].strip() if e else None

    def status_json(self, name):
        if name != "virt" or not self.installed_manifest():
            return dict(name=name, installed=False, version=None, unit=dict(active=False, enabled=False), route=dict(answers=False))
        return dict(name=name, installed=True, version=self.version(), unit=dict(active=self.unit_active, enabled=self.unit_enabled), route=dict(answers=self.gw and self.unit_active))

    def hand(self, variant, user, args):
        """`casaos module <args>` run by hand: (exit status, standard output)"""
        env_test = variant != "none"
        verb = args[0] if args else ""
        if verb == "list":
            return 0, json.dumps(self.list_json(env_test, variant)) + "\n"
        if verb == "plan":
            return 0, json.dumps(self.plan(args[1], env_test, variant, user)) + "\n"
        if verb == "status":
            if len(args) == 1:
                return 0, json.dumps(dict(modules=[self.status_json("virt")] if self.installed_manifest() else [])) + "\n"
            return 0, json.dumps(self.status_json(args[1])) + "\n"
        if verb in ("install", "remove"):
            if user != "root":
                return 2, "refused: not_root\nCASAOS_MODULE_RESULT - virt refused - not_root - %s\n" % iso(self.t)
            self.state_dir, self.state_mtime = True, max(self.state_mtime, self.t)
            if self.cur_run is not None:
                return 2, "refused: busy\nCASAOS_MODULE_RESULT - virt refused - busy - %s\n" % iso(self.t)
            run = Run(self, verb, variant, "-", "hand")
            if "--plan-id" in args:
                run.expect_plan = args[args.index("--plan-id") + 1]
            run.start()
            while not run.finished and not run.dead:
                self.tick(1.0)
            return (run.rc if run.rc is not None else 137), "".join(l + "\n" for l in run.out)
        return 2, "usage\n"

    # ---- the core: the job of a module, reduced from its log and its unit
    def unit_state(self):
        if self.cur_run is not None and self.cur_run.via == "unit":
            return "active"
        if self.dead_unit_until > self.t:
            return "deactivating"
        return "inactive"

    def reduce_job(self):
        job = dict(name="virt", state="idle", step="", code="", undo="", kept_packages=None, started_at="", ended_at="", log_tail=[])
        steps, results, kept, sentences = [], [], None, []
        nonce = None
        for ln in self.log_lines:
            m = re.match(r"^CASAOS_MODULE_(QUEUED|STEP|RESULT) (\S+) ", ln)
            if m:
                nonce = nonce or m.group(2)
                if m.group(1) == "STEP":
                    steps.append(ln.split())
                elif m.group(1) == "RESULT":
                    results.append(ln.split())
            elif ln.startswith("kept_packages: "):
                v = ln[len("kept_packages: "):]
                kept = None if v == "unknown" else ([] if v == "none" else v.split())
            if not ln.startswith("CASAOS_MODULE_"):
                sentences.append(re.sub(r"[^\x20-\x7e]", "?", ln)[:200])
        if nonce is None or nonce == "-":
            return job
        job["log_tail"] = sentences[-20:]
        qline = [ln for ln in self.log_lines if ln.startswith("CASAOS_MODULE_")]
        if qline:
            job["started_at"] = qline[0].split()[-1] if re.search(r"\d{4}-\d\d-\d\dT", qline[0]) else ""
        if results:
            r = results[0]
            verdict, step, code, undo = r[3], r[4], r[5], r[6]
            job["ended_at"] = r[7]
            job.update(state={"ok": "succeeded", "refused": "refused", "failed": "failed"}[verdict], step=step if step != "-" else "", code=code if code != "-" else "", undo=undo if undo != "-" else "")
            if verdict == "ok":
                job["step"] = "done"
            if verdict == "failed":
                job["kept_packages"] = kept
            return job
        job["step"] = steps[-1][3] if steps else ""
        alive = self.unit_state() != "inactive" or (self.cur_run is not None)
        last_sign = self.cur_run.last_sign if self.cur_run else self.last_sign_seen
        job["state"] = "running" if alive or self.t - last_sign < 30 else "interrupted"
        return job

    last_sign_seen = 0.0

    def on_log(self, run, line):
        self.last_sign_seen = self.t
        wt = self.watch
        if wt and not wt["fired"] and run.via == "unit" and line.startswith("CASAOS_MODULE_STEP ") and (" virt %s begin " % wt["step"]) in line:
            wt["fired"] = True
            wt["seen"] = line
            delay = 0.02
            if self.cfg.get("kill_lands_late") and wt["mode"] == "kill":
                delay = run.phases[[p[0] for p in run.phases].index(wt["step"])][1] + 0.05 if wt["step"] in [p[0] for p in run.phases] else 0.02
            if wt["mode"] == "kill":
                self.at(delay, lambda: self.watch_kill(run, wt))
            elif wt["mode"] == "stop":
                self.at(0.02, lambda: self.watch_stop(run, wt, 0.0))

    def watch_kill(self, run, wt):
        if run.finished:
            wt["out"].append("no process of the assistant to signal")
            wt["done"] = True
            return
        if wt["arg"] == "TERM":
            run.term()
        else:
            run.kill()
        wt["out"].append("signalled pid=4242 signal=%s rc=0" % wt["arg"])
        self.at(1.0, lambda: self.watch_finish(wt))

    def watch_stop(self, run, wt, waited):
        if wt["arg"] == "dpkg" and not run.dpkg_active() and waited < 180:
            self.at(0.5, lambda: self.watch_stop(run, wt, waited + 0.5))
            return
        wt["dpkg_seen"] = "yes" if run.dpkg_active() else "no"
        wt["stop_at"] = self.t
        run.term()
        # the unit stops when the assistant has exited
        self.at(0.1, lambda: self.watch_stop_wait(run, wt))

    def watch_stop_wait(self, run, wt):
        if not run.finished and not run.dead:
            self.at(0.5, lambda: self.watch_stop_wait(run, wt))
            return
        wt["stopped_secs"] = int(self.t - wt["stop_at"])
        self.at(1.0, lambda: self.watch_finish(wt))

    def watch_finish(self, wt):
        out = list(wt["out"])
        if wt["mode"] == "stop":
            if wt["arg"] == "dpkg":
                out.append("dpkg_seen=%s" % wt.get("dpkg_seen", "no"))
            out.append("stopped rc=0 seconds=%d" % wt.get("stopped_secs", 0))
        out.append("seen=%s" % wt["seen"])
        markers = [l for l in self.log_lines if l.startswith("CASAOS_MODULE_STEP ") or l.startswith("CASAOS_MODULE_RESULT ")]
        out.append("after=%s" % (markers[-1] if markers else ""))
        out.append("dpkg_audit=%d" % (self.dpkg_audit if wt["mode"] == "stop" else 0))
        wt["out"] = out
        wt["done"] = True

    # ---- the core's API, through the gateway ---------------------------------------------------------------------------------------------------------
    def answer(self, status, data, message=None):
        return status, {"success": status, "message": message or ("accepted" if status == 202 else "ok"), "data": data}

    def refuse(self, code, detail, data=None, status=409):
        d = {"error_code": code, "error": detail}
        if data is not None:
            d["data"] = data
        return status, {"success": status, "message": detail, "data": d}

    def busy_reason(self):
        if self.cur_run is not None:
            return "a job of the module virt is running"
        if self.dummies:
            return "a unit of a job of a module is active"
        if self.dpkg_busy():
            return "a package manager is at work"
        if self.osflag:
            return "the upgrade of the operating system is open or at work"
        return None

    def launch_unit(self, verb, plan_id):
        nonce = uuid.uuid4().hex
        self.log_lines = ["CASAOS_MODULE_QUEUED %s virt %s" % (nonce, iso(self.t))]
        self.last_sign_seen = self.t
        run = Run(self, verb, "good" if self.manager_env else "none", nonce, "unit")
        run.expect_plan = plan_id
        self.journal.append("Started /usr/bin/casaos module %s virt --yes." % verb)
        self.cur_run = run
        self.at(0.3, run.start)

    def job_status(self):
        return {"job": self.reduce_job(), "status": self.status_json("virt")}

    def route(self, method, path, auth, body):
        """(HTTP status, JSON) of a request to the gateway; None when the guest does not answer"""
        with self.lock:
            if not self.up():
                return None
            self.log.append((method, path))
            if path == "/v1/users/status":
                return self.answer(200, {"key": "reg-key-1", "initialized": self.user is not None}) if self.core_up() else None
            if path == "/v1/users/register":
                req = json.loads(body or "{}")
                if req.get("key") != "reg-key-1":
                    return self.refuse("bad_key", "bad key", status=400)
                self.user, self.pw = req.get("username"), req.get("password")
                return self.answer(200, {"id": 1})
            if path == "/v1/users/login":
                if not self.core_up():
                    return None
                req = json.loads(body or "{}")
                if self.user is None or (req.get("username"), req.get("password")) != (self.user, self.pw):
                    return self.refuse("login_failed", "wrong user or password", status=400)
                self.token, self.token_born = "tok-%d" % len(self.log), self.t
                return self.answer(200, {"token": {"access_token": self.token, "refresh_token": "refresh-" + self.token}, "user": {"username": self.user}})
            if path == "/v1/sys/version/current":
                return self.answer(200, self.cfg["core_version"]) if self.core_up() else None
            if path.startswith(self.kit["route"]):
                rest = path[len(self.kit["route"]):]
                if not (self.gw and self.unit_active):
                    return 404, {"message": "not found"}
                if rest not in ("/host",):
                    # the real gateway matches the start of the path, and the module's own validator answers what it does not know
                    return 400, {"message": "no matching operation was found"}
                if auth != self.token or self.t - self.token_born > self.cfg["token_valid_s"]:
                    return 401, {"message": "token expired or not valid"}
                return 200, {"message": "ok", "data": {"libvirt": "available", "libvirt_version": "10.0.0", "storage_path": "/DATA/VM", "network": "casaos"}}
            if not self.core_up():
                return None
            if auth != self.token or self.t - getattr(self, "token_born", 0) > self.cfg["token_valid_s"]:
                return 401, {"success": 401, "message": "token expired or not valid"}
            if not path.startswith("/v1/sys/modules"):
                return 404, {"success": 404, "message": "not found"}
            if self.cfg.get("no_modules_api"):
                return 404, {"success": 404, "message": "not found"}
            parts = path[len("/v1/sys/modules"):].strip("/").split("/")
            if parts == [""]:
                return self.answer(200, self.list_json(self.core_env > 0, "good")) if method == "GET" else (405, {})
            name, action = parts[0], (parts[1] if len(parts) > 1 else "")
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name):
                return self.refuse("bad_request", "a field of the request is not valid: name", {"field": "name"}, status=400)
            if action == "status" and method == "GET":
                return self.answer(200, self.job_status())
            if action == "plan" and method == "POST":
                self.tick(2.0)
                return self.answer(200, self.plan(name, self.core_env > 0, "good", "root"))
            if action in ("install", "remove") and method == "POST":
                try:
                    req = json.loads(body) if body else {}
                except ValueError:
                    return self.refuse("bad_request", "a field of the request is not valid: body", {"field": "body"}, status=400)
                if action == "install" and not re.fullmatch(r"m1-[0-9a-f]{32}", str(req.get("plan_id", ""))):
                    return self.refuse("bad_request", "a field of the request is not valid: plan_id", {"field": "plan_id"}, status=400)
                reason = None if self.cfg.get("busy_ignored") else self.busy_reason()
                if reason:
                    return self.refuse("busy", "Another job or package operation is running on this box: " + reason, {"reason": reason})
                if action == "remove":
                    if not any(p in self.fs for p in ("/etc/casaos/modules/virt.json", "/var/lib/casaos/modules/virt.files", "/var/lib/casaos/modules/virt.dirs", "/var/lib/casaos/modules/virt.version")):
                        return self.refuse("not_installed", "nothing of the module is on the box", {})
                    self.launch_unit("remove", None)
                    self.tick(self.cfg.get("start_takes_s", 0.0))
                    return self.answer(202, self.job_status())
                plan = self.plan(name, self.core_env > 0, "good", "root")
                if plan["refused"]:
                    return self.refuse(plan["refused"], "The module cannot be installed: %s." % plan["refused"], {"checks": plan["checks"]})
                if plan["plan_id"] != req["plan_id"]:
                    return self.refuse("plan_changed", "The plan is no longer the one that was read", {})
                self.launch_unit("install", req["plan_id"])
                self.tick(self.cfg.get("start_takes_s", 0.0))
                return self.answer(202, self.job_status())
            return 404, {"success": 404, "message": "not found"}

    # ---- the verbs of the guest's tool --------------------------------------------------------------------------------------------------------------
    def footprint_paths(self):
        out = []
        for p in self.kit["footprint"]:
            if p in self.fs:
                out.append(p)
        if self.unit_path() in self.fs and "/lib/systemd/system/" + self.kit["unit"] not in out:
            out.append("/lib/systemd/system/" + self.kit["unit"])
        if self.unit_enabled:
            out.append("enabled:" + self.kit["unit"])
        return out

    def tracked(self, p):
        return any(p == r or p.startswith(r + "/") for r in ("/etc", "/usr/bin", "/usr/lib/systemd", "/var/lib/casaos", "/var/lib/dpkg", "/var/lib/apt", "/var/log/casaos", "/var/log/apt", "/var/cache/apt",
                                                                "/usr/share/casaos", "/run/systemd/transient", "/run/systemd/system", "/DATA", "/var/tmp"))

    def owned_files(self, pkgs):
        out = []
        for p in pkgs:
            out += PKG_FILES.get(p, ["/usr/bin/" + p])
        return out

    def names(self):
        n = set(BASE_NAMES)
        n |= set(p for p in self.fs)
        if self.state_dir:
            n |= {"/var/lib/casaos/modules", "/var/lib/casaos/modules/.lock", "/var/lib/casaos/modules/tmp"}
        n |= {"/var/lib/casaos/modules/tmp/" + x for x in self.tmp}
        if self.etc_modules_dir:
            n.add("/etc/casaos/modules")
        if self.log_exists:
            n.add("/var/log/casaos/module-virt.log")
        if self.osflag_dir:
            n.add("/var/lib/casaos/os-upgrade")
        if self.osflag:
            n.add("/var/lib/casaos/os-upgrade/busy")
        if self.dirt_unit:
            n.add("/etc/systemd/system/" + self.kit["unit"])
        if self.vm_dir:
            n.add("/DATA/VM")
        if self.witness:
            n.add("/DATA/VM/.mp-witness")
        if self.unit_state() == "active":
            n.add("/run/systemd/transient/casaos-module-virt.service")
        for d in self.dummies:
            n.add("/run/systemd/transient/%s" % d)
        for p in self.installed - set(BASE_PKGS):
            n |= set(self.owned_files([p]))
        if "cloud-image-utils" in self.installed:
            n.add("/usr/bin/cloud-localds")
        n.add("/var/tmp/systemd-private-%s-logind.service-x" % self.boot[:8])
        for p in self.etc:
            n.add(p)
        n |= set(self.escaped)
        if self.snap_n % 2 == 1:
            n.add("/var/lib/casaos/flipper")
        n.add("/run/systemd/transient/session-%d.scope" % (self.ssh_n + 20))
        return sorted(p for p in n if self.tracked(p))

    def dpkg_rows(self):
        rows = {p: ("1.0", "ii ") for p in self.installed}
        return rows

    def snap_text(self, full):
        self.snap_n += 1
        k = self.kit
        known = self.unit_path() in self.fs or self.dirt_unit
        o = ["@@ facts", "boot_id " + self.boot, "uptime_s %d" % int(self.t), "uname 6.1.0-test", "hostname " + self.hostname, "os_id " + self.osid, "os_version " + self.osver,
             "casaos_v " + self.cfg["core_version"], "catalogue_file " + ("yes" if self.cfg.get("shipped_catalogue") else "no"), "osflag " + ("yes" if self.osflag else "no"),
             "osflag_dir " + ("yes" if self.osflag_dir else "no"), "dpkg_lock " + ("held" if self.dpkg_busy() else "free"), "dpkg_audit %d" % self.dpkg_audit,
             "tmp_exists yes", "tmp_entries %d" % len(self.tmp), "tmp_mount " + ("yes" if self.tmpfs else "no"), "unit_known %d" % (1 if known else 0),
             "unit_active " + ("active" if self.unit_active else "inactive"), "unit_enabled " + ("enabled" if self.unit_enabled else ("disabled" if known else "none")),
             "job_unit " + self.unit_state(), "conf " + ("yes" if k["conf_path"] in self.fs else "no"), "vm_dir " + ("yes" if self.vm_dir else "no"), "witness " + ("yes" if self.witness else "no"),
             "manager_env %d" % self.manager_env, "core_env %d" % self.core_env, "system_state running", "@@ pkgs"]
        for p in k["packages"]:
            o.append("pkg %s %s" % (p, ("hi" if p in self.hold else "ii") if p in self.installed else "none"))
        o += ["auto " + p for p in k["packages"] if p in self.auto and p in self.installed]
        o.append("@@ files")
        for p in self.footprint_paths() + ([k["conf_path"]] if k["conf_path"] in self.fs else []):
            if p.startswith("enabled:"):
                continue
            e = self.fs.get(p) or self.fs.get(self.unit_path())
            if e is None:
                continue
            o.append("\t".join([p, "l", "-", "-", e["target"]]) if e["kind"] == "l" else "\t".join([p, "f", e["mode"], e["owner"], e["sha"]]))
        o += ["@@ lists_files"] + ([self.fs["/var/lib/casaos/modules/virt.files"]["text"].rstrip("\n")] if "/var/lib/casaos/modules/virt.files" in self.fs else [])
        o += ["@@ lists_dirs"] + ([self.fs["/var/lib/casaos/modules/virt.dirs"]["text"].rstrip("\n")] if "/var/lib/casaos/modules/virt.dirs" in self.fs else [])
        o += ["@@ lists_version"] + ([self.fs["/var/lib/casaos/modules/virt.version"]["text"].rstrip("\n")] if "/var/lib/casaos/modules/virt.version" in self.fs else [])
        o += ["@@ tmp"] + list(self.tmp)
        o.append("@@ state")
        if self.etc_modules_dir:
            o.append("/etc/casaos/modules\td\t%d.0\t4096" % int(self.etc_modules_mtime))
            if "/etc/casaos/modules/virt.json" in self.fs:
                o.append("/etc/casaos/modules/virt.json\tf\t%d.0\t300" % int(self.etc_modules_mtime))
        else:
            o.append("/etc/casaos/modules\tmissing")
        if self.state_dir:
            o.append("/var/lib/casaos/modules\td\t%d.0\t4096" % int(self.state_mtime))
            o += ["/var/lib/casaos/modules/.lock\tf\t%d.0\t0" % int(self.state_mtime), "/var/lib/casaos/modules/tmp\td\t%d.0\t4096" % int(self.state_mtime)]
        else:
            o.append("/var/lib/casaos/modules\tmissing")
        if self.log_exists:
            o.append("/var/log/casaos/module-virt.log\tf\t%d\t%d" % (int(self.log_mtime), len(self.log_lines) * 80))
        o.append("@@ aptstate")
        for key, e in self.apt.items():
            o.append("%s\t%d\t%d\t%s" % (key, e["size"], e["mtime"], e["sha"]) if e else "%s\tmissing" % key)
        o += ["@@ reserved"] + ["%s\t%s" % (p, h) for p, h in self.reserved.items()]
        o.append("@@ units")
        for u in CORE_UNITS:
            o.append("unit %s active running %s 0" % (u, self.inv[u]))
        if self.unit_state() != "inactive":
            o.append("job casaos-module-virt.service loaded %s running a job" % self.unit_state())
        for d in sorted(self.dummies):
            o.append("job %s loaded active running dummy" % d)
        o += ["list %s active running" % u for u in sorted(CORE_UNITS)] + (["list %s active running" % self.kit["unit"]] if self.unit_active else [])
        o.append("@@ etccasaos")
        ec = {"/etc/casaos/gateway.ini": "gw1", "/etc/casaos/gateway.ini.sample": "gws1"}
        for p, e in self.fs.items():
            if p.startswith("/etc/casaos/") and e["kind"] == "f":
                ec[p] = e["sha"]
        o += ["%s\t%s" % (p, h) for p, h in sorted(ec.items())]
        o += ["@@ names"] + self.names()
        if full:
            o += ["@@ dpkg"] + ["%s\t%s\t%s" % (n, v, s) for n, (v, s) in sorted(self.dpkg_rows().items())]
            o.append("@@ etc")
            etc = dict(self.etc)
            for p, e in self.fs.items():
                if p.startswith("/etc/") and e["kind"] == "f":
                    etc[p] = e["sha"]
                elif p.startswith("/etc/") and e["kind"] == "l":
                    etc[p] = "link:" + e["target"]
            if self.snap_n % 2 == 1:
                etc["/etc/resolv.conf"] = "r2"
            o += ["%s\t%s" % (p, h) for p, h in sorted(etc.items())]
        o.append("@@ end")
        return "\n".join(o) + "\n"

    def aptsim(self):
        sim = self.apt_sim()
        o = ["rc 0"]
        if sim["failure"]:
            o += ["held " + p for p in sim["held"]]
            return "\n".join(o) + "\n"
        o += ["new " + p for p in sim["new"]] + ["upgrade " + p for p in sim["upgrade"]] + ["manual " + p for p in sim["manual"]]
        o.append("uris_total %d" % sim["uris"])
        if sim["new"]:
            o += ["need Need to get 1.2 MB of archives.", "need After this operation, 3.4 MB of additional disk space will be used."]
        return "\n".join(o) + "\n"

    def core_restart(self, env):
        self.manager_env = 3 if env else 0
        self.core_env = 0 if (not env or self.cfg.get("api_env_missing")) else 3
        self.core_down_until = self.t + 8.0
        self.inv["casaos.service"] = "inv-casaos-%d" % int(self.t)

    def do_reboot(self):
        self.boot = str(uuid.uuid4())
        self.reboot_count += 1
        self.down_until = self.t + self.cfg["down_seconds"]
        self.core_down_until = self.down_until + 15.0
        self.manager_env = self.core_env = 0
        self.cur_run, self.lock_held, self.tmpfs = None, False, False
        self.gw, self.unit_active = False, False
        self.orphan_until = self.dead_unit_until = 0.0
        self.dummies = set()
        self.heap = [e for e in self.heap if e.fn == self.do_reboot]
        for u in CORE_UNITS:
            self.inv[u] = "inv-%s-b%d" % (u[:6], self.reboot_count)
        if self.unit_enabled and not self.cfg.get("reboot_unit_down"):
            self.at(self.cfg["down_seconds"] + 22.0, self.after_boot_unit)

    def after_boot_unit(self):
        self.unit_active = True
        self.at(5.0, self.route_up)

    def ssh(self, cmd):
        """(exit status, stdout) of a command the runner sends to the guest"""
        with self.lock:
            if not self.up():
                return 255, ""
            self.ssh_n += 1
            m = re.match(r"^sudo sh /usr/local/lib/mp/guest\.sh (\w+)(.*)$", cmd.strip())
            if not m:
                return (0, "") if cmd.strip() == "true" else (127, "")
            verb, args = m.group(1), m.group(2).split()
            seen = self.ssh_seen.get(verb, 0)
            self.ssh_seen[verb] = seen + 1
            if seen < self.cfg["ssh_drops"].get(verb, 0):
                return 255, ""
            return self.verb(verb, args)

    def verb(self, verb, a):
        k = self.kit
        self.verbs.append(verb)
        if verb == "boot_id":
            return 0, self.boot + "\n"
        if verb == "hostname":
            return 0, self.hostname + "\n"
        if verb == "casaos_v":
            return 0, self.cfg["core_version"] + "\n"
        if verb == "mod":
            if a[0] != "none" and a[0] not in k["variants"]:
                return 2, ""
            rc, out = self.hand(a[0], a[1], a[2:])
            return rc, out
        if verb == "footprint":
            return 0, "".join(p + "\n" for p in self.footprint_paths())
        if verb == "snap":
            return 0, self.snap_text(a[:1] == ["full"])
        if verb == "conf_write":
            self.fs[k["conf_path"]] = dict(kind="f", mode="644", owner="0:0", sha=k["conf_sha256"])
            return 0, "ok %s\n" % k["conf_sha256"]
        if verb == "conf_remove":
            self.fs.pop(k["conf_path"], None)
            return 0, "no\n"
        if verb == "witness_on":
            self.witness, self.vm_dir = True, True
            return 0, "ok\n"
        if verb == "witness_off":
            self.witness = False
            return 0, "off\n"
        if verb == "lock_start":
            self.lock_held = True
            return 0, "held\n"
        if verb == "lock_stop":
            self.lock_held = bool(self.cfg.get("lock_stays"))
            return (1, "the lock is still held\n") if self.lock_held else (0, "free\n")
        if verb == "lock_state":
            return 0, ("held" if self.dpkg_busy() else "free") + "\n"
        if verb == "osflag_on":
            self.osflag_dir, self.osflag = True, True
            return 0, "on\n"
        if verb == "osflag_off":
            self.osflag, self.osflag_dir = False, False
            return 0, "off flag=no dir=no\n"
        if verb == "unit_dirt_on":
            self.dirt_unit = True
            return 0, "on\n"
        if verb == "unit_dirt_off":
            self.dirt_unit = bool(self.cfg.get("dirt_stays"))
            return 0, "off file=%s\n" % ("yes" if self.dirt_unit else "no")
        if verb == "tmpfs_on":
            self.tmpfs = True
            return 0, "mounted yes\n"
        if verb == "tmpfs_off":
            self.tmpfs = False
            return 0, "unmounted no\n"
        if verb == "symlink_on":
            self.symlink = True
            return 0, "planted /usr/local/lib/mp/canary\n"
        if verb == "symlink_off":
            self.symlink = False
            return 0, "off link=no\n"
        if verb == "canary_state":
            return 0, "canary %s %s %d\n" % self.canary
        if verb in ("pkg_unseat", "pkg_unseat_all"):
            gone = list(a if verb == "pkg_unseat" else k["packages"])
            if "qemu-utils" in gone and "cloud-image-utils" in self.installed:
                gone.append("cloud-image-utils")        # what depends on a package goes with it
                self.unseat_owned |= {"/usr/bin/cloud-localds"}
            for p in gone:
                self.installed.discard(p)
                self.auto.discard(p)
            self.apt_changed("auto")
            return 0, "rc=0\nRemoving ...\nwill_remove %s \n" % " ".join(gone)
        if verb == "unseat_owned":
            return 0, "".join(p + "\n" for p in sorted(self.unseat_owned)) + "done\n"
        if verb == "pkg_reseat":
            for p in a:
                self.installed.add(p)
            self.hold -= set(a)
            self.apt_changed("x")
            return 0, "rc=0\nSetting up ...\n"
        if verb == "hold_on":
            self.hold.add(a[0])
            return 0, "held hold ok %s\n" % ("installed" if a[0] in self.installed else "not-installed")
        if verb == "hold_off":
            self.hold.discard(a[0])
            return 0, "unheld install ok %s\n" % ("installed" if a[0] in self.installed else "not-installed")
        if verb == "auto_on":
            self.auto.add(a[0])
            self.apt["/var/lib/apt/extended_states"].update(sha=sha("auto+" + a[0]), mtime=int(self.t))
            return 0, "auto %s\n" % a[0]
        if verb == "auto_off":
            self.auto.discard(a[0])
            self.apt["/var/lib/apt/extended_states"].update(sha=sha("auto-" + a[0]), mtime=int(self.t))
            return 0, "manual auto=\n"
        if verb == "aptsim":
            return 0, self.aptsim()
        if verb == "watch_start":
            self.watch = dict(mode=a[0], step=a[1], arg=a[2], fired=False, done=False, out=[])
            return 0, "started\n"
        if verb == "watch_state":
            return 0, ("done" if self.watch and self.watch["done"] else "running") + "\n"
        if verb == "watch_result":
            return 0, "".join(l + "\n" for l in (self.watch["out"] if self.watch else []))
        if verb == "wait_clean":
            limit, waited = int(a[0]) if a else 600, 0
            while (self.dpkg_busy() or self.cur_run is not None or self.unit_state() != "inactive") and waited < limit:
                self.tick(1.0)
                waited += 1
            if waited >= limit:
                return 1, "not clean after %d s: unit\n" % waited
            return 0, "clean after %d s\n" % waited
        if verb == "dummy_start":
            if "casaos-module-mpdummy%s.service" % a[0] in self.dummies:
                return 1, ""        # systemd-run: the unit exists
            self.dummies.add("casaos-module-mpdummy%s.service" % a[0])
            return 0, "".join("%s loaded active running\n" % d for d in sorted(self.dummies))
        if verb == "dummy_stop":
            self.dummies = set() if not self.cfg.get("dummies_stay") else self.dummies
            return 0, "left=%d\n" % len(self.dummies)
        if verb == "glob_show":
            units = sorted(self.dummies) + (["casaos-module-virt.service"] if self.unit_state() == "active" else [])
            rc = 1 if (self.cfg.get("glob_none_rc1") and not units) else 0
            return 0, "== values\n%src=%d\n== properties\n%src=%d\n" % ("".join("active\n" + ("\n" if i < len(units) - 1 else "") for i in range(len(units))), rc,
                                                                      "".join("Id=%s\nActiveState=active\nLoadState=loaded\n\n" % u for u in units), rc)
        if verb == "unit_props":
            on = self.unit_state() == "active"
            mode = "control-group" if self.cfg.get("props_wrong") else "mixed"
            show = ("KillMode=%s\nTimeoutStopUSec=7min\nType=exec\nLoadState=loaded\nActiveState=active\n" % mode) if on else "KillMode=control-group\nTimeoutStopUSec=1min 30s\nType=simple\nLoadState=not-found\nActiveState=inactive\n"
            cat = "# /run/systemd/transient/casaos-module-virt.service\n[Service]\nType=exec\nKillMode=mixed\nTimeoutStopSec=420\nEnvironment=\"CASAOS_MODULE_NONCE=0\"\nExecStart=/usr/bin/casaos module install virt\n" if on else "No files found for casaos-module-virt.service.\n"
            return 0, "== show\n%src=0\n== loadstate\n%s\n== cat\n%s" % (show, "loaded" if on else "not-found", cat)
        if verb == "job_unit":
            return 0, self.unit_state() + "\n"
        if verb == "api_env_on":
            self.core_restart(True)
            return 0, "restarted\n"
        if verb == "api_env_off":
            self.core_restart(False)
            return 0, "restarted\n"
        if verb == "api_env_state":
            return 0, "manager %d\ncore %d\n" % (self.manager_env, self.core_env)
        if verb == "escape_state":
            return 0, "".join(p + "\n" for p in self.escaped) + "done\n"
        if verb == "pkg_files":
            return 0, "".join(("/bin/virsh" if p == "/usr/bin/virsh" else p) + "\n" for p in sorted(set(self.owned_files(a))))
        if verb == "modlog":
            return 0, "".join(l + "\n" for l in self.log_lines)
        if verb == "journal":
            return 0, "".join(l + "\n" for l in self.journal[-40:])
        if verb == "reboot":
            self.at(2.0, self.do_reboot)
            return 0, "scheduled\n"
        if verb == "diag":
            return 0, "0 loaded units listed.\n"
        return 2, "unknown verb: %s\n" % verb


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
            else:
                rc, out = w.ssh(body)
            data = ("%d\n%s" % (rc, out)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        res = w.route(self.command, path, self.headers.get("Authorization", ""), body)
        if res is None:
            return      # the guest is away: nothing answers
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
# a stand-in for ssh: the last word is the command for the guest, which the fake plays
cmd="${*: -1}"
body="$(curl -sS --max-time 900 --data-binary "$cmd" "http://127.0.0.1:${FAKE_PORT}/_ssh" 2>/dev/null; printf x)"
body="${body%x}"
rc="${body%%$'\\n'*}"
case "$rc" in ''|*[!0-9]*) exit 255 ;; esac
printf '%s' "${body#*$'\\n'}"
exit "$rc"
''',
    "tick": '''#!/usr/bin/env bash
curl -sS --max-time 900 "http://127.0.0.1:${FAKE_PORT}/_tick?s=$1" >/dev/null 2>&1
''',
    "python3": '''#!/usr/bin/env bash
exec "%s" "$@"
''' % posix(sys.executable),
    "go": GO_STUB,
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
    for name, text in SHIMS.items():
        with open(os.path.join(d, name), "w", newline="\n") as f:
            f.write(text)
        os.chmod(os.path.join(d, name), 0o755)
    return d


def read_out(out, name, default=""):
    try:
        with open(os.path.join(out, name), encoding="utf-8", newline="") as f:
            return f.read()
    except OSError:
        return default


def write_out(out, name, text):
    path = os.path.join(out, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.remove(path)         # (a recording that was copied by a hard link is not written through: the base recordings stay as they were)
    except OSError:
        pass
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def tmproot(prefix):
    return tempfile.mkdtemp(prefix=prefix, dir=os.environ.get("TMPDIR") or None)


def build_kit(root=None):
    """the real build-kit of the script, against a module tree and a stand-in for go: (the kit directory, the calls of go, the module directory, the process)"""
    root = root or tmproot("mp-kit-")
    mod = make_module(root)
    bindir = make_bin(root)
    kit = os.path.join(root, "kit")
    calls = os.path.join(root, "go.calls")
    env = dict(ENV, PATH=os.pathsep.join([bindir] + tool_dirs() + [ENV.get("PATH", "")]), GO_CALLS=posix(calls), WORK=posix(os.path.join(root, "work")))
    proc = subprocess.run([BASH, posix(SCRIPT), "build-kit", posix(mod), posix(kit)], env=env, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
    return kit, read_out(root, "go.calls"), mod, proc


def run_script(kit, leg="d12", cases="", cfg=None, root=None, env_extra=None):
    """the real runner script, against a fake guest: (the directory of its recordings, the world, the finished process)"""
    root = root or tmproot("mp-")
    out, work = os.path.join(root, "out"), os.path.join(root, "work")
    os.makedirs(out, exist_ok=True)
    os.makedirs(work, exist_ok=True)
    world = World(kit, leg, cfg)
    srv, port = serve(world)
    bindir = make_bin(root)
    # what the preparation leaves for the runner: the evidence of the guest, and the box's user
    osid, osver = OS_BY_LEG[leg]
    write_out(out, "guest/os-release", 'PRETTY_NAME="Test OS"\nID=%s\nVERSION_ID="%s"\n' % (osid, osver))
    write_out(out, "guest/box-facts", "kernel 6.1.0-test\ncpus 4\nmemory_mb 5945\ndocker 28.0.4\napt apt 2.6.1 (amd64)\nsystemd systemd 252\n")
    write_out(out, "guest/os", "Test OS\n")
    write_out(out, "guest/prepared", "2026-10-18T10:00:00Z\n")
    write_out(out, "guest/pins", "CASAOS_RELEASE_TAG=v0.5.31\nCASAOS_TAG=%s\n" % world.cfg["core_version"])
    with open(os.path.join(work, "pw"), "w") as f:
        f.write("Aa1-test")
    world.user, world.pw = "mpproof", "Aa1-test"
    env = dict(ENV, PATH=os.pathsep.join([bindir] + tool_dirs() + [ENV.get("PATH", "")]), OUT=posix(out), WORK=posix(work), KIT=posix(kit), SSH_CONFIG="ssh_config", SSH_HOST="vm",
               GW_PORT=str(port), FAKE_PORT=str(port), MP_FAST="1", MP_TICK=posix(os.path.join(bindir, "tick")), TAG="v0.0.0-test", CASES=cases, POLL_EVERY="2", CONTROL_SECONDS="320",
               WAIT_BOOT="600")
    env.update(env_extra or {})
    try:
        proc = subprocess.run([BASH, posix(SCRIPT), "run", leg], env=env, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=int(os.environ.get("RUN_TIMEOUT", "3000")))
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
    m = re.match(r"^# Module proof, \w+ leg: (.*)$", text.splitlines()[0])
    return m.group(1) if m else "?"


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
        ok = bool(line) and rc == 1 and "FAIL" in h and "INVALID" not in h and exp.needle in " ".join(line)
        if ok and others_same:
            changed = sorted(n for n, s in st.items() if not n.startswith(exp.name) and not any(n.startswith(a) for a in exp.also) and base_status.get(n) != s)
            ok = not changed
            detail = "other verdicts changed: %s" % changed
        else:
            detail = "head %s, rc %s, wanted a FAIL of %r with %r; verdicts %s; got %s" % (h, rc, exp.name, exp.needle, {n: s for n, s in st.items() if s != "PASS"}, " ".join(line)[:300])
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


# ---- how the recordings are mutated --------------------------------------------------------------------------------------------------------------

def copytree(src):
    """a copy of a directory of recordings that costs a link to each file (a mutation writes by removing the file first, see write_out)"""
    dst = tmproot("mp-m-")
    shutil.rmtree(dst)
    try:
        shutil.copytree(src, dst, copy_function=os.link)
    except OSError:
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst)
    return dst


def subst(d, name, old, new, count=1):
    text = read_out(d, name)
    if text.count(old) < 1:
        raise AssertionError("%s has no %r" % (name, old))
    write_out(d, name, text.replace(old, new, count))


def code(d, name, n):
    write_out(d, name + ".code", "%d\n" % n)


def jdoc(d, name):
    return json.loads(read_out(d, name + ".json"))


def jedit(d, name, fn):
    """the body of a recorded call, changed by fn (which gets the whole document)"""
    doc = jdoc(d, name)
    fn(doc)
    write_out(d, name + ".json", json.dumps(doc))


def jdata(d, name, fn):
    jedit(d, name, lambda doc: fn(doc["data"]))


def gjson(d, name, fn):
    """the standard output of a guest verb that is one JSON object, changed by fn"""
    doc = json.loads(read_out(d, name + ".out"))
    fn(doc)
    write_out(d, name + ".out", json.dumps(doc) + "\n")


def rc(d, name, n):
    write_out(d, name + ".rc", "%d\n" % n)


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


def add_line(d, name, line):
    text = read_out(d, name)
    write_out(d, name, text + line + "\n")


def drop_line(d, name, prefix):
    lines = read_out(d, name).splitlines()
    kept = [ln for ln in lines if not ln.startswith(prefix)]
    if len(kept) == len(lines):
        raise AssertionError("%s has no line that starts with %r" % (name, prefix))
    write_out(d, name, "\n".join(kept) + "\n")


def replace_line(d, name, prefix, new):
    lines = read_out(d, name).splitlines()
    hit = [i for i, ln in enumerate(lines) if ln.startswith(prefix)]
    if not hit:
        raise AssertionError("%s has no line that starts with %r" % (name, prefix))
    lines[hit[0]] = new
    write_out(d, name, "\n".join(lines) + "\n")


def drop_marker(d, name):
    lines = read_out(d, "timeline.tsv").splitlines()
    write_out(d, "timeline.tsv", "".join(ln + "\n" for ln in lines if not ln.endswith("\t" + name)))


def rows_edit(d, tag, fn):
    rows = [ln.split("\t") for ln in read_out(d, "polls-%s.tsv" % tag).splitlines()]
    out = fn(rows)
    write_out(d, "polls-%s.tsv" % tag, "".join("\t".join(r) + "\n" for r in (out if out is not None else rows)))


COL = dict(t0=0, t1=1, http=2, curl=3, state=4, step=5, code=6, undo=7, kept=8, installed=9, version=10, active=11, enabled=12, route=13, unit=14)


def map_rows(d, tag, match, **kw):
    def fn(rows):
        for r in rows:
            if r[COL["state"]] == match:
                for k, v in kw.items():
                    r[COL[k]] = v
    rows_edit(d, tag, fn)


def set_last(d, tag, **kw):
    def fn(rows):
        r = [x for x in rows if x[COL["http"]] != "000"][-1]
        for k, v in kw.items():
            r[COL[k]] = v
    rows_edit(d, tag, fn)


def marker_edit(d, name, old, new):
    """a marker line of a recorded run (the standard output of a command, or the log), changed"""
    subst(d, name, old, new)


# ---- the rules of the core's readers, written again here from their source (pkg/osmodule), to hold the kit against ----------------------------------------------------------

GO_ALLOWED_DIRS = ("usr/bin", "usr/lib/systemd/system", "lib/systemd/system", "etc/casaos")
GO_RESERVED = {"systemctl", "systemd-run", "journalctl", "apt-get", "apt", "apt-cache", "apt-config", "apt-key", "apt-mark", "dpkg", "dpkg-query", "dpkg-deb", "dpkg-divert", "dpkg-statoverride",
               "update-rc.d", "invoke-rc.d", "deb-systemd-helper", "deb-systemd-invoke", "sh", "dash", "bash", "env"}


def go_safe_element(el):
    return bool(el) and len(el) <= 255 and el.strip(".") != "" and re.fullmatch(r"[A-Za-z0-9._+@:-]+", el) is not None


def go_scan(raw, unit):
    """the code pkg/osmodule would refuse the archive with, or None when it takes it (classify and the order of its checks)"""
    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
        members = list(tf)
    except (tarfile.TarError, OSError, EOFError):
        return "unpack_corrupt"
    seen_manifest = False
    for m in members:
        if not (m.isreg() or m.isdir() or m.issym()):
            return "unpack_path"
        name = m.name.rstrip("/") if m.isdir() else m.name
        if name in ("module.json", "LICENSE", "NOTICE"):
            if not m.isreg():
                return "unpack_path"
            seen_manifest = seen_manifest or name == "module.json"
            continue
        if m.isdir() and name in ("build", "build/sysroot"):
            continue
        if not name.startswith("build/sysroot/"):
            return "unpack_path"
        rel = name[len("build/sysroot/"):]
        if not rel or len(rel) > 1024 or not all(go_safe_element(el) for el in rel.split("/")):
            return "unpack_path"
        under = next((d for d in GO_ALLOWED_DIRS if rel.startswith(d + "/")), None)
        if under:
            leaf = rel[len(under) + 1:]
            if "/" in leaf:
                return "unpack_path"
            if under.endswith("systemd/system"):
                if not (m.isreg() and leaf == unit):
                    return "unpack_path"
            elif under == "etc/casaos":
                if not (m.isreg() and leaf.endswith(".sample")):
                    return "unpack_path"
            elif m.isdir() or (under == "usr/bin" and leaf in GO_RESERVED):
                return "unpack_path"
        elif m.isdir() and any(d == rel or d.startswith(rel + "/") for d in GO_ALLOWED_DIRS):
            continue
        else:
            return "unpack_path"
    return None if seen_manifest else "unpack_path"


def go_catalogue(raw, expect_sha, archive_name):
    """whether the core's reader of a catalogue takes the text, by the rules of catalogue.go and manifest.go (the ones that the kit's catalogues could break)"""
    c = json.loads(raw)
    if set(c) != {"schema", "modules"} or c["schema"] != 1 or len(c["modules"]) != 1:
        return False
    e = c["modules"][0]
    if set(e) != {"name", "component", "tier", "tag", "commit", "assets", "manifest"}:
        return False
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", e["name"]) or e["tier"] not in ("core", "community") or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", e["tag"]) \
            or not re.fullmatch(r"[0-9a-f]{40}", e["commit"]):
        return False
    a = e["assets"].get("amd64")
    if set(e["assets"]) != {"amd64"} or a is None or a["sha256"] != expect_sha:
        return False
    if a["url"] != "https://github.com/ReCasaOS/%s/releases/download/%s/%s" % (e["component"], e["tag"], archive_name):
        return False
    if not re.fullmatch(r"linux-amd64-casaos-[a-z-]+-v[0-9.]+\.tar\.gz", archive_name):
        return False
    m = e["manifest"]
    return m["name"] == e["name"] and m["component"] == e["component"] and m["schema"] == 1


# ---- the checks that need no guest ---------------------------------------------------------------------------------------------------------------

def guest_tools_text():
    with open(SCRIPT, newline="") as f:
        script = f.read().replace("\r\n", "\n")
    g = re.search(r"cat >\"\$1/guest\.sh\" <<'MP_GUEST'\n(.*?)\nMP_GUEST\n", script, re.S)
    p = re.search(r"cat >\"\$1\" <<'MP_PY'\n(.*?)\nMP_PY\n", script, re.S)
    return script, g.group(1) + "\n" if g else None, p.group(1) + "\n" if p else None


def static_checks():
    p = subprocess.run([BASH, "-n", posix(SCRIPT)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    check("the script reads under bash -n", p.returncode == 0, p.stdout.decode("utf-8", "replace"))
    sc = shutil.which("shellcheck") if os.environ.get("SHELLCHECK") else None
    if sc:
        p = subprocess.run([sc, "-x", "-S", "warning", SCRIPT], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        check("the script passes shellcheck", p.returncode == 0, p.stdout.decode("utf-8", "replace")[:2000])
    script, guest, pyhelp = guest_tools_text()
    check("the guest tools are in the script", guest is not None)
    check("the helper of the runner is in the script", pyhelp is not None)
    if guest:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n", dir=os.environ.get("TMPDIR") or None)
        tmp.write(guest)
        tmp.close()
        p = subprocess.run([BASH, "--posix", "-n", posix(tmp.name)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        check("the guest tools read as POSIX sh", p.returncode == 0, p.stdout.decode("utf-8", "replace"))
        dash = shutil.which("dash")
        if dash:
            p = subprocess.run([dash, "-n", posix(tmp.name)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            check("the guest tools read under dash -n", p.returncode == 0, p.stdout.decode("utf-8", "replace"))
        os.unlink(tmp.name)
        check("the guest tools use no bashism (no [[ test, no arrays, no $'..', no <<<, no process substitution, no local, no EPOCHREALTIME, no ${..//..})",
              not re.search(r"\[\[\s|<<<|<\(|\$'|\bdeclare\b|^\s*local\s|\bmapfile\b|\bEPOCHREALTIME\b|\$\{[A-Za-z_]+//|\[ [^\]]*==", guest, re.M), "")
    if pyhelp:
        try:
            compile(pyhelp, "mp.py", "exec")
            ok = True
        except SyntaxError as e:
            ok = False
            print(e)
        check("the helper of the runner is Python that compiles", ok)
    legs = json.load(open(LEGS_JSON))
    check("the legs file names the two legs, with what the workflow needs", [l["leg"] for l in legs] == ["d12", "u24"] and all(
        {"leg", "name", "label", "id", "version", "base", "image", "sums", "algo", "timeout", "prove_timeout"} <= set(l) for l in legs), "")
    check("the proof step has less time than the job", all(l["prove_timeout"] < l["timeout"] for l in legs) and all(l["timeout"] <= 360 for l in legs), "")
    check("the legs are the systems the module supports", [(l["id"], l["version"]) for l in legs] == [("debian", "12"), ("ubuntu", "24.04")] and all("amd64" in l["image"] for l in legs), "")
    try:
        import yaml
        wf = yaml.safe_load(open(WORKFLOW, encoding="utf-8"))
    except ImportError:
        wf = None
    if wf is not None:
        triggers = wf.get("on", wf.get(True))
        check("the workflow is dispatched by hand only, with the tag, the ref of the module and the legs", list(triggers) == ["workflow_dispatch"] and set(triggers["workflow_dispatch"]["inputs"]) == {"tag", "module_ref", "legs"}, str(triggers))
        check("the module ref defaults to main", triggers["workflow_dispatch"]["inputs"]["module_ref"]["default"] == "main", "")
        check("the legs the workflow offers are the legs of the file", triggers["workflow_dispatch"]["inputs"]["legs"]["options"] == ["all", "d12", "u24"], "")
        text = open(WORKFLOW, encoding="utf-8").read()
        check("the workflow publishes nothing", not re.search(r"gh release (create|upload|edit)|git push|actions/(create-release|upload-release)", text), "")
        check("the workflow runs this test, the script and the report", "test-module-proof.py" in text and "module-proof.sh" in text and "module-proof-report.py" in text, "")
        perms = wf.get("permissions")
        check("the workflow reads the repository and nothing else", perms == {"contents": "read"}, str(perms))
        problems = context_problems(wf)
        check("every expression of the workflow uses a context its key may use (GitHub's table of context availability)", not problems, "; ".join(problems))
        # the checker itself can say no: the context that the proof of the upgrade of the system once named in an `env` of a job
        bad = {"jobs": {"vm": {"env": {"WORK": "${{ runner.temp }}/x"}, "steps": [{"run": "echo ${{ matrix.leg }}", "if": "always() && inputs.a"}]}}}
        check("the checker finds a runner context in the env of a job", any("runner" in p for p in context_problems(bad)), str(context_problems(bad)))
        check("the checker finds hashFiles in a job's if and a status function in a run", len(context_problems({"jobs": {"a": {"if": "hashFiles('x')", "steps": [{"run": "${{ always() }}"}]}}})) >= 2, "")
        check("the checker lets a legal workflow through", context_problems({"jobs": {"a": {"name": "${{ matrix.x }}", "env": {"A": "${{ github.workspace }}/out"},
                                                                                         "steps": [{"run": "echo ${{ runner.temp }} ${{ env.A }}", "if": "failure() && steps.x.outcome == 'success'"}]}}}) == [], "")
        steps = wf["jobs"]["vm"]["steps"]
        names = [s.get("name", s.get("uses", "")) for s in steps]
        i_test = names.index("The proof can say no")
        i_build = names.index("Build the module and the kit")
        i_prep = names.index("Prepare the guest")
        check("the proof is tried before anything is built or booted, and the kit is built before the guest is prepared", i_test < i_build < i_prep, str(names))
        check("the ref of the module reaches only the checkout (never a shell)", "inputs.module_ref" in text and text.count("${{ inputs.module_ref }}") == 2 and not re.search(r"run:[^\n]*inputs\.module_ref", text), "")
        env = wf["jobs"]["vm"]["env"]
        check("the job's env has no runner context (it is not available there)", not any("runner." in str(v) for v in env.values()), str(env))
    # the names the report gates on are the markers the script writes
    for cid in ["M%d" % i for i in range(1, 12) if i not in (8, 9)]:
        check("the script writes the markers of %s" % cid, ("marker case_%s_start" % cid) in script and ("marker case_%s_done" % cid) in script, "")
    check("the script writes the markers of the cases of M8 and M9 (one pair, by id)", 'marker "case_${id}_start"' in script and 'marker "case_M9_${id}_start"' in script, "")
    check("the script never runs `casaos module` before it has the version of the core (M1 is the first case and aborts on an old core)",
          script.index("run_case m1 case_m1") < script.index("run_case m2 case_m2") and "abort \"M1:" in script, "")
    check("the script runs the cases in order", [script.index("run_case m%d " % i) for i in range(1, 12)] == sorted(script.index("run_case m%d " % i) for i in range(1, 12)), "")
    words = ["revi" + "ew", "rou" + "nd", "sp" + "ec", "rul" + "ing", "the ow" + "ner", "scrat" + "ch", "_r" + "v-", "_in" + "st-", "Co-Auth" + "ored", "Genera" + "ted with"]
    check("the script has none of the words that belong to how it was made (a person who reviews, a ruling, a path of somebody's scratch directory, an attribution line)",
          not [w for w in words if re.search(r"(?i)\b%s\b" % re.escape(w), script)], str([w for w in words if re.search(r"(?i)\b%s\b" % re.escape(w), script)]))


def build_kit_broken(mutate):
    """build-kit on a module tree that `mutate` has spoiled: (exit status, what it said)"""
    root = tmproot("mp-kitb-")
    mod = make_module(root)
    mutate(mod)
    bindir = make_bin(root)
    env = dict(ENV, PATH=os.pathsep.join([bindir] + tool_dirs() + [ENV.get("PATH", "")]), GO_CALLS=posix(os.path.join(root, "go.calls")), WORK=posix(os.path.join(root, "work")))
    p = subprocess.run([BASH, posix(SCRIPT), "build-kit", posix(mod), posix(os.path.join(root, "kit"))], env=env, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
    shutil.rmtree(root, ignore_errors=True)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def kit_checks(kit, calls, mod, proc):
    check("build-kit ends well", proc.returncode == 0, proc.stdout.decode("utf-8", "replace")[-400:])
    lines = [ln for ln in calls.splitlines() if ln.strip()]
    verbs = [ln.split(" go ", 1)[1].split()[0:2] if " go " in ln else [] for ln in lines]
    check("the program is built as the module's release builds it: go generate, go mod tidy, go build, in this order", [v[0] for v in verbs] == ["generate", "mod", "build"] and verbs[1] == ["mod", "tidy"], str(lines))
    build = lines[-1] if lines else ""
    commit = subprocess.run(["git", "-C", mod, "rev-parse", "HEAD"], stdout=subprocess.PIPE, env=ENV).stdout.decode().strip()
    check("the build is CGO_ENABLED=0 for linux/amd64", "CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build" in build, build)
    check("the build has the module's ldflags: the commit, the date, -s -w", ("-X main.commit=%s" % commit) in build and re.search(r"-X main\.date=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", build) is not None and " -s -w " in build, build)
    check("the program is built where the archive takes it from (build/sysroot/usr/bin)", "-o build/sysroot/usr/bin/casaos-virt-management" in build, build)
    check("the program says its version when it is asked (-v), and the kit keeps what it said", read_out(kit, "binary-version").strip() == "v0.1.0", read_out(kit, "binary-version"))
    info = json.loads(read_out(kit, "kit.json"))
    good = open(os.path.join(kit, "archive.tar.gz"), "rb").read()
    check("kit.json names the version, the tag, the unit, the route and the archive's sha256", info["tag"] == "v0.1.1" and info["unit"] == "casaos-virt-management.service" and info["route"] == "/v2/virt_management"
          and info["archive"]["sha256"] == sha(good) and info["commit"] == commit, "")
    check("the archive is named for a release: linux-amd64-casaos-virt-management-v0.1.1.tar.gz, at the url of the release", info["archive"]["url"] ==
          "https://github.com/ReCasaOS/CasaOS-VirtManagement/releases/download/v0.1.1/linux-amd64-casaos-virt-management-v0.1.1.tar.gz", info["archive"]["url"])
    tf = tarfile.open(fileobj=io.BytesIO(good), mode="r:gz")
    names = [m.name for m in tf]
    check("the archive holds the tree build/sysroot, module.json, LICENSE and NOTICE, from its root", all(n.startswith("build") or n in ("module.json", "LICENSE", "NOTICE") for n in names)
          and {"module.json", "LICENSE", "NOTICE", "build/sysroot/usr/bin/casaos-virt-management", "build/sysroot/usr/lib/systemd/system/casaos-virt-management.service",
               "build/sysroot/etc/casaos/virt-management.conf.sample"} <= set(names), str(names))
    check("every entry of the archive is root's, and the program is 0755, the others 0644", all(m.uid == 0 and m.gid == 0 for m in tf) and
          {m.name: m.mode for m in tf if m.isreg()}["build/sysroot/usr/bin/casaos-virt-management"] == 0o755 and {m.name: m.mode for m in tf if m.isreg()}["module.json"] == 0o644, "")
    check("the archive is accepted by the rules of the core's unpack", go_scan(good, info["unit"]) is None, str(go_scan(good, info["unit"])))
    check("the files the kit expects on the box are the archive's, with sha256 and mode", sorted(f["path"] for f in info["files"]) == ["/etc/casaos/virt-management.conf.sample", "/usr/bin/casaos-virt-management",
          "/usr/lib/systemd/system/casaos-virt-management.service"] and all(re.fullmatch(r"[0-9a-f]{64}", f["sha256"]) and f["mode"] in ("644", "755") for f in info["files"]), "")
    name = "linux-amd64-casaos-virt-management-v0.1.1.tar.gz"
    check("the test catalogue is accepted by the rules of the core's reader, and names the archive's sha256", go_catalogue(read_out(kit, "catalogue.json"), sha(good), name), read_out(kit, "catalogue.json")[:300])
    cat = json.loads(read_out(kit, "catalogue.json"))
    check("the manifest in the catalogue is the module.json of the archive, whole", cat["modules"][0]["manifest"] == json.loads(MODULE_JSON), "")
    check("the entry is of the community tier, at the commit of the checkout", cat["modules"][0]["tier"] == "community" and cat["modules"][0]["commit"] == commit, "")
    corrupt = open(os.path.join(kit, "corrupt.tar.gz"), "rb").read()
    check("the corrupted archive is the good one with a byte changed, and its sha256 is not the catalogue's", len(corrupt) == len(good) and sum(a != b for a, b in zip(corrupt, good)) == 1 and sha(corrupt) != sha(good), "")
    for variant, v in sorted(info["variants"].items()):
        if variant in ("good", "corrupt"):
            continue
        raw = open(os.path.join(kit, v["archive"]), "rb").read()
        check("the %s archive is refused unpack_path by the rules of the unpack (and nothing but its extra entry is wrong)" % variant, go_scan(raw, info["unit"]) == "unpack_path"
              and go_scan(raw.replace(b"", b""), info["unit"]) == "unpack_path", "")
        ents = [m.name for m in tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")]
        check("the %s archive is the good one and the entry %s" % (variant, v["entry"]), ents[:-1] == names and ents[-1] == v["entry"], str(ents[-2:]))
        check("the catalogue of the %s archive is accepted by the reader, with the archive's own sha256" % variant, go_catalogue(read_out(kit, v["catalogue"]), sha(raw), name), "")
    check("the configuration for libvirt's test driver is the sample with `URI = test:///default`", "\nURI = test:///default" in read_out(kit, "virt-management.conf").replace("\r\n", "\n")
          and sha(read_out(kit, "virt-management.conf")) == info["conf_sha256"] and info["conf_path"] == "/etc/casaos/virt-management.conf", "")
    check("the list of the packages is the manifest's common and amd64 ones", sorted(read_out(kit, "packages.list").split()) == sorted(["qemu-utils", "libvirt-daemon-system", "libvirt-clients", "qemu-system-x86", "ovmf"]), "")
    check("the footprint has the three files, the unit under both spellings, the link that enables it, the manifest and the lists",
          {"/usr/bin/casaos-virt-management", "/lib/systemd/system/casaos-virt-management.service", "/usr/lib/systemd/system/casaos-virt-management.service",
           "/etc/systemd/system/multi-user.target.wants/casaos-virt-management.service", "/etc/casaos/modules/virt.json", "/var/lib/casaos/modules/virt.files", "/var/lib/casaos/modules/virt.dirs",
           "/var/lib/casaos/modules/virt.version"} <= set(read_out(kit, "footprint.list").split()), "")
    check("the manifest's sha256 is that of module.json", info["manifest_sha256"] == sha(MODULE_JSON), "")
    # a module that cannot make a kit says so, and exits
    rcode, said = build_kit_broken(lambda m: os.remove(os.path.join(m, "LICENSE")))
    check("build-kit refuses a module that has no LICENSE", rcode != 0 and "LICENSE" in said, said[-200:])
    rcode, said = build_kit_broken(lambda m: open(os.path.join(m, "build/sysroot/etc/casaos/virt-management.conf.sample"), "w").write("[libvirt]\nURI = qemu:///system\n"))
    check("build-kit refuses a sample that has no empty `URI =` line to put the test driver in", rcode != 0 and "URI" in said, said[-200:])
    rcode, said = build_kit_broken(lambda m: open(os.path.join(m, "module.json"), "w").write(MODULE_JSON.replace('"schema": 1', '"schema": 2')))
    check("build-kit refuses a manifest the reader would refuse", rcode != 0, said[-200:])


# ---- what is mutated --------------------------------------------------------------------------------------------------------------------------------

EXPECTED = ["M1 the release carries the assistant", "M2 list", "M2 plan", "M2 status", "M2 nothing written", "M3 plan checks", "M3 plan packages", "M3 plan writes nothing", "M3 plan as nobody",
            "M3 list", "M4 install markers", "M4 files and state", "M4 status and list", "M4 gateway route and host", "M5 list", "M5 second install refused", "M5 plan", "M5 status",
            "M5 plan_changed", "M6 the job", "M6 what is gone and what stays", "M6 the rest of the box is untouched", "M7 the glob of the busy check", "M7 job unit properties",
            "M7 install through the API", "M8 checksum_mismatch", "M8 unpack_path ../", "M8 unpack_path usr/bin/systemctl", "M8 unpack_path var/lib/casaos", "M8 unpack_path usr/share/casaos",
            "M8 unpack_path etc/casaos/modules", "M8 foreign_unit", "M8 busy (dpkg's lock)", "M8 busy (the flag of the upgrade)", "M8 disk_short", "M8 link in the area of the install",
            "M8 held_package", "M8 a package marked automatic"] + [
    "M9 SIGKILL at the step %s" % s for s in ("packages", "download", "verify", "unpack", "units", "manifest", "start", "route")] + [
    "M9 systemctl stop during the step packages", "M9 systemctl stop during the step start", "M9 SIGTERM at the step start", "M10 the restart", "M10 remove after the restart",
    "M11 /etc/casaos", "M11 dpkg", "M11 /etc", "M11 the rest of the file system"]
# the kills whose signal lands in the step they name on the fake (the steps of a tidy fake last long enough for the watcher): all of them
KILL = {s: "M9 SIGKILL at the step %s" % s for s in ("packages", "download", "verify", "unpack", "units", "manifest", "start", "route")}


def lines_of(d, name):
    return read_out(d, name).splitlines()


def edit_lines(d, name, fn):
    write_out(d, name, "".join(l + "\n" for l in fn(lines_of(d, name))))


def swap_lines(d, name, a_prefix, b_prefix):
    """two lines of a recorded text change places"""
    ls = lines_of(d, name)
    ia = [i for i, l in enumerate(ls) if l.startswith(a_prefix)][0]
    ib = [i for i, l in enumerate(ls) if l.startswith(b_prefix)][0]
    ls[ia], ls[ib] = ls[ib], ls[ia]
    write_out(d, name, "".join(l + "\n" for l in ls))


def dpkg_line(d, tag, pkg, abbr):
    subst(d, "snap-%s.pkgs" % tag, "pkg %s ii" % pkg, "pkg %s %s" % (pkg, abbr))


def core_mutations():
    M = []

    def m(label, expect, fn):
        M.append((label, fn, expect))

    P4 = "m4-install.out"
    # the run as a whole
    m("no `finished` file and no reason", invalid_run("no `finished` file"), lambda d: os.remove(os.path.join(d, "finished")))
    m("a run that stopped early", invalid_run("the run stopped early: boom"), lambda d: write_out(d, "aborted", "boom\n"))
    m("a step of the harness whose undo failed", invalid_run("could not be undone (M8_foreign): the unit stayed"), lambda d: add_line(d, "invalid.txt", "M8_foreign\tthe unit stayed"))
    m("a guest that is not the system of the leg", invalid_run("the guest is ubuntu 24.04, the leg d12 needs debian 12"), lambda d: write_out(d, "guest/os-release", 'ID=ubuntu\nVERSION_ID="24.04"\n'))
    m("a leg file that names another leg", invalid_run("says it ran leg"), lambda d: write_out(d, "leg", "u24\n"))
    m("a snapshot of the guest that is missing", invalid_run("the snapshot m3-after is missing"), lambda d: [os.remove(os.path.join(d, f)) for f in os.listdir(d) if f.startswith("snap-m3-after.")])
    m("a snapshot of the guest that could not be had", invalid_run("snapshot m4-before"), lambda d: write_out(d, "snap-m4-before.failed", "three tries\n"))
    m("a call that got no answer", invalid_run("m5-list"), lambda d: write_out(d, "m5-list.curl", "7\n"))
    m("a verb of the guest that got no answer from ssh", invalid_run("m4-install"), lambda d: rc(d, "m4-install", 255))
    m("a case that began and did not end", invalid_run("M5 (the API) began and did not end"), lambda d: drop_marker(d, "case_M5_done"))
    m("a kit.json that is missing", invalid_run("kit.json is missing"), lambda d: os.remove(os.path.join(d, "kit.json")))
    m("a program that was built and does not say its version", invalid_run("does not say its version"), lambda d: write_out(d, "binary-version", "Segmentation fault\n"))
    m("the guest's preparation that stopped", invalid_run("the guest's preparation stopped: no space"), lambda d: write_out(d, "guest/aborted", "no space\n"))
    m("a row of the polls that is cut", invalid_run("fields, not 15"), lambda d: add_line(d, "polls-m7-job.tsv", "1\t2\t3"))

    # M1
    m("M1: a core older than the first that has the assistant", invalid_run("older than v0.4.75"), lambda d: (write_out(d, "m1-version.out", "v0.4.74\n"), write_out(d, "guest/pins", "CASAOS_TAG=v0.4.74\n")))
    m("M1: a core that is not the one the release pins", invalid_run("is not the one the release pins"), lambda d: write_out(d, "m1-version.out", "v0.4.80\n"))
    m("M1: a version that is not one", invalid_run("is not a release of the core"), lambda d: write_out(d, "m1-version.out", "casaos: command not found\n"))

    m("M1: a pin written with quotes is the pin", passes("M1 the release carries the assistant"), lambda d: write_out(d, "guest/pins", 'CASAOS_TAG="v0.4.75"\n'))
    m("M3: a download_bytes that holds what apt has cached is not a fault", passes("M3 plan packages"), lambda d: gjson(d, "m3-plan-root", lambda x: x["packages"].update(download_bytes=x["packages"]["download_bytes"] * 2)))

    # M2
    m("M2: list prints two lines", fails("M2 list", "one JSON object"), lambda d: write_out(d, "m2-list-root.out", read_out(d, "m2-list-root.out") + "banner\n"))
    m("M2: list as nobody ends with status 3", fails("M2 list", "ends with status 0"), lambda d: rc(d, "m2-list-nobody", 3))
    m("M2: list says there is a catalogue", fails("M2 list", "says that there is no catalogue"), lambda d: gjson(d, "m2-list-root", lambda x: x["catalogue"].update(ok=True)))
    m("M2: plan is refused another code", fails("M2 plan", "refused catalogue"), lambda d: gjson(d, "m2-plan-nobody", lambda x: x.update(refused="os")))
    m("M2: a plan that is refused has a plan_id", fails("M2 plan", "no plan_id"), lambda d: gjson(d, "m2-plan-root", lambda x: x.update(plan_id="m1-" + "0" * 32)))
    m("M2: the module is installed", fails("M2 status", "not installed"), lambda d: gjson(d, "m2-status-root", lambda x: x.update(installed=True)))
    m("M2: status lists a module", fails("M2 status", "lists no module"), lambda d: write_out(d, "m2-status-all.out", '{"modules": [{"name": "virt"}]}\n'))
    m("M2: a name that the control did not show changing", fails("M2 nothing written", "that the control did not show"), lambda d: add_line(d, "snap-m2-after.names", "/var/lib/casaos/modules/virt.files"))
    m("M2: a directory of the assistant is made", fails("M2 nothing written", "are what they were"), lambda d: replace_line(d, "snap-m2-after.state", "/var/lib/casaos/modules\tmissing", "/var/lib/casaos/modules\td\t1.0\t4096"))
    m("M2: a file of apt is written", fails("M2 nothing written", "files of apt and dpkg"), lambda d: subst(d, "snap-m2-after.aptstate", "/var/lib/apt/extended_states\t10\t5", "/var/lib/apt/extended_states\t11\t6"))
    m("M2: the box has a catalogue of its own: the answer of a release without one is not asked for", not_testable("M2 no catalogue", "ships a catalogue"), lambda d: fact(d, "m2-before", "catalogue_file", "yes"))

    # M3
    m("M3: a package missing from to_install", fails("M3 plan packages", "to_install is what", also=("M11 dpkg", "M3 plan as nobody")), lambda d: gjson(d, "m3-plan-root", lambda x: x["packages"]["to_install"].pop()))
    m("M3: a package apt would upgrade", fails("M3 plan packages", "to_upgrade is what"), lambda d: gjson(d, "m3-plan-root", lambda x: x["packages"].update(to_upgrade=["libc6"])))
    m("M3: the sizes are not known", fails("M3 plan packages", "apt gave the sizes"), lambda d: gjson(d, "m3-plan-root", lambda x: x["packages"].update(known=False)))
    m("M3: download_bytes far from the sizes of --print-uris", fails("M3 plan packages", "download_bytes is at least"), lambda d: gjson(d, "m3-plan-root", lambda x: x["packages"].update(download_bytes=5)))
    m("M3: a check is not ok", fails("M3 plan checks", "every check is ok"), lambda d: gjson(d, "m3-plan-root", lambda x: x["checks"][7].update(ok=False)))
    m("M3: the checks are in another order", fails("M3 plan checks", "in this order"), lambda d: gjson(d, "m3-plan-root", lambda x: x["checks"].reverse()))
    m("M3: the plan_id is not one", fails("M3 plan checks", "plan_id is m1-", also=("M3 plan as nobody",)), lambda d: gjson(d, "m3-plan-root", lambda x: x.update(plan_id="p1-123")))
    m("M3: the archive is another", fails("M3 plan checks", "the archive is the one the catalogue names"), lambda d: gjson(d, "m3-plan-root", lambda x: x["archive"].update(url="https://github.com/x/y")))
    m("M3: extended_states is written by the plan", fails("M3 plan writes nothing", "extended_states is as it was after the plans"), lambda d: subst(d, "snap-m3-after.aptstate", "/var/lib/apt/extended_states\t10\t5\t", "/var/lib/apt/extended_states\t11\t6\t"))
    m("M3: the log of the planner appears", fails("M3 plan writes nothing", "eipp.log.xz is as it was"), lambda d: replace_line(d, "snap-m3-after.aptstate", "/var/log/apt/eipp.log.xz", "/var/log/apt/eipp.log.xz\t50\t9\tabc"))
    m("M3: the sandbox of the plan is left in /var/tmp", fails("M3 plan writes nothing", "/var/tmp holds what it held"), lambda d: add_line(d, "snap-m3-after.names", "/var/tmp/casaos-plan-1"))
    m("M3: the proof's own simulation writes the file it proves", fails("M3 plan writes nothing", "the proof's own simulation"), lambda d: subst(d, "snap-m3-after-sim.aptstate", "/var/log/apt/history.log\t100\t5", "/var/log/apt/history.log\t120\t9"))
    m("M3: apt could not be asked", invalid_run("apt could not be asked"), lambda d: write_out(d, "m3-apt.out", "rc 100\n"))
    m("M3: nobody's plan has the busy check ok", fails("M3 plan as nobody", "marked as not made"), lambda d: gjson(d, "m3-plan-nobody", lambda x: x["checks"][6].update(ok=True, detail="")))
    m("M3: nobody's plan is another", fails("M3 plan as nobody", "plan_id is the root's"), lambda d: gjson(d, "m3-plan-nobody", lambda x: x.update(plan_id="m1-" + "1" * 32)))
    m("M3: the list does not know the module", fails("M3 list", "the module is listed"), lambda d: gjson(d, "m3-list-root", lambda x: x.update(modules=[])))
    m("M3: the module is listed installed", fails("M3 list", "the module is listed"), lambda d: gjson(d, "m3-list-root", lambda x: x["modules"][0].update(installed={"version": "v0.1.1"})))

    # M4
    m("M4: the install ends with status 3", fails("M4 install markers", "ends with status 0"), lambda d: rc(d, "m4-install", 3))
    m("M4: two steps are out of order", fails("M4 install markers", "the steps go in order"), lambda d: swap_lines(d, P4, "CASAOS_MODULE_STEP - virt verify begin", "CASAOS_MODULE_STEP - virt download begin"))
    m("M4: a step has no end", fails("M4 install markers", "the steps go in order"), lambda d: drop_line(d, P4, "CASAOS_MODULE_STEP - virt manifest done"))
    m("M4: the result is not ok", fails("M4 install markers", "one RESULT line, ok"), lambda d: subst(d, P4, "virt ok done - -", "virt failed done - -"))
    m("M4: a marker with a nonce of the core", fails("M4 install markers", "the nonce is -"), lambda d: subst(d, P4, "CASAOS_MODULE_STEP - virt route begin", "CASAOS_MODULE_STEP " + "a" * 32 + " virt route begin"))
    m("M4: a marker whose time goes back", fails("M4 install markers", "the times of the markers do not go back"), lambda d: subst(d, P4, "virt route begin 2", "virt route begin 1"))
    m("M4: a line that looks like a marker and is not", fails("M4 install markers", "the grammar"), lambda d: add_line(d, P4, "CASAOS_MODULE_STEP - virt route lost"))
    m("M4: the log is not the output", fails("M4 install markers", "the log holds the lines of the standard output"), lambda d: drop_line(d, "m4-log.out", "installed: virt"))
    m("M4: the unit file has another mode", fails("M4 files and state", "the mode of /usr/lib/systemd/system/casaos-virt-management.service"), lambda d: subst(d, "snap-m4-after.files", "service\tf\t644", "service\tf\t666"))
    m("M4: the program is not root's", fails("M4 files and state", "/usr/bin/casaos-virt-management is root's"), lambda d: subst(d, "snap-m4-after.files", "casaos-virt-management\tf\t755\t0:0", "casaos-virt-management\tf\t755\t1000:1000"))
    m("M4: the sample has other content", fails("M4 files and state", "the content of /etc/casaos/virt-management.conf.sample"), lambda d: subst(d, "snap-m4-after.files", "sample\tf\t644\t0:0\t", "sample\tf\t644\t0:0\t0"))
    m("M4: a file of the archive is not on the box", fails("M4 files and state", "/usr/bin/casaos-virt-management is a file"), lambda d: drop_line(d, "snap-m4-after.files", "/usr/bin/casaos-virt-management\t"))
    m("M4: the unit is not enabled by a link", fails("M4 files and state", "enabled by a link"), lambda d: drop_line(d, "snap-m4-after.files", "/etc/systemd/system/multi-user.target.wants"))
    m("M4: the list of the files lacks a file", fails("M4 files and state", "the list names exactly the files"), lambda d: drop_line(d, "snap-m4-after.lists_files", "f ") if False else edit_lines(d, "snap-m4-after.lists_files", lambda ls: ls[:2] + ls[3:]))
    m("M4: the unit is not last in the list", fails("M4 files and state", "the unit is last in the list"), lambda d: edit_lines(d, "snap-m4-after.lists_files", lambda ls: [ls[0], ls[3], ls[1], ls[2]]))
    m("M4: the list of the files is of the earlier format", fails("M4 files and state", "the list of the files is whole"), lambda d: edit_lines(d, "snap-m4-after.lists_files", lambda ls: ls[1:]))
    m("M4: a directory of the list that is not clean", fails("M4 files and state", "the list of the directories"), lambda d: write_out(d, "snap-m4-after.lists_dirs", "/usr/../etc\n"))
    m("M4: the version file says another tag", fails("M4 files and state", "the version file says v0.1.1"), lambda d: write_out(d, "snap-m4-after.lists_version", "v9.9.9\n"))
    m("M4: the manifest is another", fails("M4 files and state", "the manifest is the module.json of the archive"), lambda d: subst(d, "snap-m4-after.files", "/etc/casaos/modules/virt.json\tf\t644\t0:0\t", "/etc/casaos/modules/virt.json\tf\t644\t0:0\t0"))
    m("M4: the configuration of the administrator is rewritten", fails("M4 files and state", "was not touched"), lambda d: subst(d, "snap-m4-after.files", "/etc/casaos/virt-management.conf\tf\t644\t0:0\t", "/etc/casaos/virt-management.conf\tf\t644\t0:0\t0"))
    m("M4: the unit is not active", fails("M4 files and state", "the unit is enabled and active"), lambda d: fact(d, "m4-after", "unit_active", "inactive"))
    m("M4: a package is not installed", fails("M4 files and state", "the packages of the manifest are installed"), lambda d: dpkg_line(d, "m4-after", "ovmf", "none"))
    m("M4: the area of the install is not empty", fails("M4 files and state", "the area of the install is empty"), lambda d: fact(d, "m4-after", "tmp_entries", "2"))
    m("M4: dpkg is left interrupted", fails("M4 files and state", "dpkg --audit finds nothing"), lambda d: fact(d, "m4-after", "dpkg_audit", "3"))
    m("M4: the module had files before", fails("M4 files and state", "had nothing of the module before"), lambda d: add_line(d, "snap-m4-before.files", "/usr/bin/casaos-virt-management\tf\t755\t0:0\tabc"))
    m("M4: status says the route does not answer", fails("M4 status and list", "status says installed"), lambda d: gjson(d, "m4-status", lambda x: x["route"].update(answers=False)))
    m("M4: list shows no version", fails("M4 status and list", "list shows the module installed"), lambda d: gjson(d, "m4-list", lambda x: x["modules"][0].update(installed=None)))
    m("M4: the host is a 404 through the gateway", fails("M4 gateway route and host", "is answered 200"), lambda d: code(d, "m4-host", 404))
    m("M4: libvirt is unavailable", fails("M4 gateway route and host", "libvirt is available"), lambda d: jdata(d, "m4-host", lambda x: x.update(libvirt="unavailable", reason="no socket")))
    m("M4: the route without a token is a 404", fails("M4 gateway route and host", "refused by the module, not 404"), lambda d: code(d, "m4-host-noauth", 404))
    m("M4: a route that is not registered answers", fails("M4 gateway route and host", "is 404 through the same gateway"), lambda d: code(d, "m4-ghost", 200))
    m("M4: a route that is not registered is a 400 (it reached the module)", fails("M4 gateway route and host", "is 404 through the same gateway"), lambda d: code(d, "m4-ghost", 400))
    m("M4: the configuration was not written before the install", fails("M4 install markers", "libvirt's test driver was put in the configuration"), lambda d: write_out(d, "m4-conf.out", "no\n"))
    m("M4: the install was not given a plan_id", fails("M4 install markers", "--plan-id"), lambda d: write_out(d, "m4-plan-id", "\n"))
    return M


def core_mutations_api():
    M = []

    def m(label, expect, fn):
        M.append((label, fn, expect))

    # M5
    m("M5: the test variables did not reach the core", invalid_run("did not reach the manager and the core"), lambda d: write_out(d, "api-state.out", "manager 3\ncore 0\n"))
    m("M5: the core has no Modules API", invalid_run("does not carry the Modules API"), lambda d: code(d, "m5-list", 404))
    m("M5: the catalogue is not read by the core", fails("M5 list", "answers 200 with the catalogue read"), lambda d: jdata(d, "m5-list", lambda x: x["catalogue"].update(ok=False)))
    m("M5: the list shows no version", fails("M5 list", "installed, with its version"), lambda d: jdata(d, "m5-list", lambda x: x["modules"][0].update(installed={"version": None})))
    m("M5: a call without a token is answered", fails("M5 list", "refused 401"), lambda d: code(d, "m5-unauth", 200))
    m("M5: a second install is accepted", fails("M5 second install refused", "refused already_installed"), lambda d: code(d, "m5-second", 202))
    m("M5: the refusal has another code", fails("M5 second install refused", "refused already_installed"), lambda d: jdata(d, "m5-second", lambda x: x.update(error_code="busy")))
    m("M5: the refusal has no checks", fails("M5 second install refused", "carries the checks"), lambda d: jdata(d, "m5-second", lambda x: x["data"].update(checks=[])))
    m("M5: the plan is not refused", fails("M5 plan", "refused already_installed"), lambda d: jdata(d, "m5-plan", lambda x: x.update(refused=None)))
    m("M5: the plan has a plan_id", fails("M5 plan", "has no plan_id"), lambda d: jdata(d, "m5-plan", lambda x: x.update(plan_id="m1-" + "2" * 32)))
    m("M5: the status has a job for a run by hand", fails("M5 status", "has no job"), lambda d: jdata(d, "m5-status", lambda x: x["job"].update(state="running")))
    m("M5: the status says the unit is not active", fails("M5 status", "status: installed at its version"), lambda d: jdata(d, "m5-status", lambda x: x["status"]["unit"].update(active=False)))
    m("M5: a bad name is not a 400", fails("M5 status", "400 bad_request"), lambda d: code(d, "m5-badname", 200))
    m("M5: a stale plan_id is accepted", fails("M5 plan_changed", "plan_changed", also=("M7 install through the API",)), lambda d: code(d, "m7-stale", 202))

    # M6
    m("M6: the remove is refused", fails("M6 remove through the API", "POST remove is accepted", also=("M6 the job", "M6 what is gone", "M6 the rest")), lambda d: code(d, "m6-remove", 409))
    m("M6: the job never ends succeeded", fails("M6 the job", "ends succeeded"), lambda d: set_last(d, "m6-job", state="failed", code="remove_incomplete"))
    m("M6: the steps are not those of a remove", fails("M6 the job", "the steps go in order"), lambda d: subst(d, "m6-log.out", " virt files begin", " virt packages begin"))
    m("M6: the markers carry no nonce (it ran by hand)", fails("M6 the job", "the markers carry the nonce the core passed"), lambda d: write_out(d, "m6-log.out", read_out(d, "m6-log.out").replace(" virt ", " virt ").replace(re.search(r"STEP ([0-9a-f]{32}) virt", read_out(d, "m6-log.out")).group(1), "-")))
    m("M6: the job unit was never seen and is not in the journal", fails("M6 the job", "the job ran as"), lambda d: (write_out(d, "m6-journal.out", "nothing\n"), map_rows(d, "m6-job", "running", unit="inactive")))
    m("M6: a file of the module is left", fails("M6 what is gone and what stays", "nothing of the module"), lambda d: add_line(d, "snap-m6-after.files", "/usr/bin/casaos-virt-management\tf\t755\t0:0\tabc"))
    m("M6: the unit is still active", fails("M6 what is gone and what stays", "the unit is gone, stopped and not enabled"), lambda d: fact(d, "m6-after", "unit_active", "active"))
    m("M6: a package is gone", fails("M6 what is gone and what stays", "the packages stay"), lambda d: dpkg_line(d, "m6-after", "ovmf", "none"))
    m("M6: the data of the module is gone", fails("M6 what is gone and what stays", "/DATA/VM and the file"), lambda d: fact(d, "m6-after", "witness", "no"))
    m("M6: the last line is not the one that says nothing was run", fails("M6 what is gone and what stays", "nothing above was run"), lambda d: drop_line(d, "m6-log.out", "nothing above was run"))
    m("M6: the packages are not named", fails("M6 what is gone and what stays", "the packages stay, and name them"), lambda d: subst(d, "m6-log.out", "left in place: the packages", "left in place: the things"))
    m("M6: the core was restarted", fails("M6 the rest of the box is untouched", "casaos.service is the same invocation"), lambda d: subst(d, "snap-m6-after.units", "unit casaos.service active running inv-casaos", "unit casaos.service active running inv-other"))
    m("M6: a unit of ReCasaOS is gone from the list", fails("M6 the rest of the box is untouched", "the list of the units of ReCasaOS"), lambda d: drop_line(d, "snap-m6-after.units", "list casaos-gateway.service"))
    m("M6: a file of /etc/casaos changed", fails("M6 the rest of the box is untouched", "every file of /etc/casaos"), lambda d: subst(d, "snap-m6-after.etccasaos", "/etc/casaos/gateway.ini\tgw1", "/etc/casaos/gateway.ini\tgw2"))
    m("M6: the sample of the module is left in /etc/casaos", fails("M6 the rest of the box is untouched", "every file of /etc/casaos"), lambda d: add_line(d, "snap-m6-after.etccasaos", "/etc/casaos/virt-management.conf.sample" + chr(9) + "abc"))
    m("M6: the sample of another component is gone", fails("M6 the rest of the box is untouched", "every file of /etc/casaos"), lambda d: drop_line(d, "snap-m6-after.etccasaos", "/etc/casaos/gateway.ini.sample"))
    m("M6: the status after the job still has the module", fails("M6 what is gone and what stays", "the status after the job"), lambda d: jdata(d, "m6-status", lambda x: x["status"].update(installed=True)))

    # M7
    m("M7: the glob with no unit fails", fails("M7 the glob of the busy check", "ends with status 0"), lambda d: subst(d, "m7-glob-none.out", "rc=0", "rc=1"))
    m("M7: the glob with no unit prints something", fails("M7 the glob of the busy check", "the values of ActiveState are nothing"), lambda d: subst(d, "m7-glob-none.out", "== values\n", "== values\ninactive\n"))
    m("M7: the glob with two units shows one", fails("M7 the glob of the busy check", "two units"), lambda d: subst(d, "m7-glob-two.out", "active\n\nactive\n", "active\n"))
    m("M7: the glob with one unit names none", fails("M7 the glob of the busy check", "the properties name"), lambda d: subst(d, "m7-glob-one.out", "Id=casaos-module-mpdummy1.service\n", ""))
    m("M7: a unit named like a job does not make the box busy", fails("M7 the glob of the busy check", "refused busy, with its reason"), lambda d: code(d, "m7-busy-dummy", 202))
    m("M7: KillMode is not mixed", fails("M7 job unit properties", "KillMode is mixed"), lambda d: subst(d, "m7-run-props.out", "KillMode=mixed", "KillMode=control-group"))
    m("M7: the unit has 90 s to stop", fails("M7 job unit properties", "420 s to stop"), lambda d: subst(d, "m7-run-props.out", "TimeoutStopUSec=7min", "TimeoutStopUSec=1min 30s"))
    m("M7: the unit has the type simple", fails("M7 job unit properties", "Type is exec"), lambda d: subst(d, "m7-run-props.out", "Type=exec", "Type=simple", 1))
    m("M7: the unit carries the test variables", fails("M7 job unit properties", "does not carry the test variables"), lambda d: subst(d, "m7-run-props.out", "ExecStart=", "Environment=CASAOS_MODULE_TEST=1\nExecStart="))
    m("M7: the unit is not collected", fails("M7 job unit properties", "collected"), lambda d: subst(d, "m7-props-end.out", "== loadstate\nnot-found", "== loadstate\nloaded"))
    m("M7: the unit was never seen active", not_testable("M7 job unit properties", "never seen active"), lambda d: [os.remove(os.path.join(d, f)) for f in os.listdir(d) if f.startswith("m7-run-props.")])
    m("M7: the plan on a clean box is refused", fails("M7 install through the API", "the plan by API on a clean box"), lambda d: jdata(d, "m7-plan", lambda x: x.update(refused="disk_short")))
    m("M7: a stale plan_id is accepted by the install", fails("M7 install through the API", "plan_changed", also=("M5 plan_changed",)), lambda d: jdata(d, "m7-stale", lambda x: x.update(error_code="busy")))
    m("M7: the install is refused", fails("M7 install through the API", "the install is accepted"), lambda d: code(d, "m7-install", 409))
    m("M7: the job never succeeds", fails("M7 install through the API", "the job ends succeeded"), lambda d: set_last(d, "m7-job", state="failed", code="start_failed"))
    m("M7: the job goes back a step", fails("M7 install through the API", "the step only goes forward"), lambda d: rows_edit(d, "m7-job", lambda rows: [r if i != 6 else r[:COL["step"]] + ["packages"] + r[COL["step"] + 1:] for i, r in enumerate(rows)]))
    m("M7: the nonce is in the answer of the core", fails("M7 install through the API", "the nonce is in no answer"), lambda d: write_out(d, "m7-status.json", read_out(d, "m7-status.json") + " " + re.search(r"STEP ([0-9a-f]{32}) virt", read_out(d, "m7-log.out")).group(1)))
    m("M7: the finished job has a code", fails("M7 install through the API", "the status of the finished job"), lambda d: jdata(d, "m7-status", lambda x: x["job"].update(code="route_timeout")))
    m("M7: the job ended before it started", fails("M7 install through the API", "started_at and ended_at"), lambda d: jdata(d, "m7-status", lambda x: x["job"].update(ended_at="2001-01-01T00:00:00Z")))
    m("M7: the log_tail has a marker", fails("M7 install through the API", "log_tail"), lambda d: jdata(d, "m7-status", lambda x: x["job"].update(log_tail=["CASAOS_MODULE_STEP - virt done done 2026-01-01T00:00:00Z"])))
    m("M7: the module is not installed after the job", fails("M7 install through the API", "after the install through the API: the unit is enabled and active"), lambda d: fact(d, "m7-after", "unit_enabled", "disabled"))
    m("M7: the host does not answer", fails("M7 install through the API", "the host answers"), lambda d: code(d, "m7-host", 404))
    return M


def dirty_mutations():
    M = []

    def m(label, expect, fn):
        M.append((label, fn, expect))

    FP = "/usr/bin/casaos-virt-management\tf\t755\t0:0\tabc"

    def watch_line(d, name, prefix, new):
        replace_line(d, name, prefix, new)

    # M8
    m("M8 checksum: the install is accepted", fails("M8 checksum_mismatch", "ends with status 3"), lambda d: rc(d, "m8-checksum-install", 0))
    m("M8 checksum: the plan is refused", fails("M8 checksum_mismatch", "the plan passes"), lambda d: gjson(d, "m8-checksum-plan", lambda x: x.update(refused="catalogue")))
    m("M8 checksum: it fails at another step", fails("M8 checksum_mismatch", "the steps go up to verify"), lambda d: subst(d, "m8-checksum-install.out", "verify failed", "unpack failed"))
    m("M8 checksum: the code is another", fails("M8 checksum_mismatch", "the RESULT line says failed verify checksum_mismatch"), lambda d: subst(d, "m8-checksum-install.out", "failed verify checksum_mismatch", "failed verify download_failed"))
    m("M8 checksum: the packages are not said to be kept", fails("M8 checksum_mismatch", "the last sentence before the result"), lambda d: drop_line(d, "m8-checksum-install.out", "kept_packages"))
    m("M8 checksum: a file of the module is left", fails("M8 checksum_mismatch", "nothing of the module is on the box"), lambda d: add_line(d, "snap-m8-checksum-after.files", FP))
    m("M8 checksum: the corrupted archive is kept in the area", fails("M8 checksum_mismatch", "the area of the install is empty"), lambda d: fact(d, "m8-checksum-after", "tmp_entries", "1"))
    m("M8 checksum: a program of the system changed", fails("M8 checksum_mismatch", "the programs the assistant runs"), lambda d: subst(d, "snap-m8-checksum-after.reserved", "/usr/bin/dpkg\t", "/usr/bin/dpkg\t0"))
    m("M8 dotdot: a file was written by the `../`", fails("M8 unpack_path ../", "nothing named mp-escape"), lambda d: add_line(d, "m8-dotdot-escape.out", "/tmp/mp-escape"))
    m("M8 dotdot: the rollback does not run", fails("M8 unpack_path ../", "then the rollback runs"), lambda d: edit_lines(d, "m8-dotdot-install.out", lambda ls: [l for l in ls if "rollback_files" not in l]))
    m("M8 dotdot: the install is refused before the unpack", fails("M8 unpack_path ../", "the steps go up to unpack"), lambda d: edit_lines(d, "m8-dotdot-install.out", lambda ls: [l for l in ls if "virt unpack" not in l]))
    m("M8 systemctl: the program of the system is replaced", fails("M8 unpack_path usr/bin/systemctl", "the programs the assistant runs"), lambda d: subst(d, "snap-m8-systemctl-after.reserved", "/usr/bin/systemctl\t", "/usr/bin/systemctl\t0"))
    m("M8 systemctl: the code is path_exists", fails("M8 unpack_path usr/bin/systemctl", "the RESULT line says failed unpack unpack_path"), lambda d: subst(d, "m8-systemctl-install.out", "unpack_path rolled_back", "file_exists rolled_back"))
    m("M8 varlib: the file of the state area appears", fails("M8 unpack_path var/lib/casaos", "was not written"), lambda d: add_line(d, "snap-m8-varlib-after.names", "/var/lib/casaos/mp-var.txt"))
    m("M8 usrshare: the file appears", fails("M8 unpack_path usr/share/casaos", "was not written"), lambda d: add_line(d, "snap-m8-usrshare-after.names", "/usr/share/casaos/mp-share.txt"))
    m("M8 etcmod: the file appears", fails("M8 unpack_path etc/casaos/modules", "was not written"), lambda d: add_line(d, "snap-m8-etcmod-after.names", "/etc/casaos/modules/mp-mod.json"))
    m("M8 etcmod: the unit stays active", fails("M8 unpack_path etc/casaos/modules", "the unit is neither active nor enabled"), lambda d: fact(d, "m8-etcmod-after", "unit_active", "active"))
    m("M8 foreign: the plan is not refused", fails("M8 foreign_unit", "the plan is refused foreign_unit"), lambda d: gjson(d, "m8-foreign-plan", lambda x: x.update(refused=None)))
    m("M8 foreign: the check says another thing", fails("M8 foreign_unit", "says not_owned"), lambda d: gjson(d, "m8-foreign-plan", lambda x: x["checks"][5].update(detail="unreadable")))
    m("M8 foreign: the install goes through", fails("M8 foreign_unit", "ends with status 2"), lambda d: rc(d, "m8-foreign-install", 0))
    m("M8 foreign: a step began", fails("M8 foreign_unit", "no step began"), lambda d: write_out(d, "m8-foreign-install.out", "CASAOS_MODULE_STEP - virt packages begin 2026-09-21T14:00:00Z\n" + read_out(d, "m8-foreign-install.out")))
    m("M8 foreign: the unit was not taken away", invalid_run("could not be taken away again"), lambda d: fact(d, "m8-foreign-after", "unit_known", "1"))
    m("M8 busy dpkg: the lock was not held", fails("M8 busy (dpkg's lock)", "the proof held dpkg's lock"), lambda d: write_out(d, "m8-busy-dpkg-dirt.out", "the lock could not be taken\n"))
    m("M8 busy dpkg: the plan says another thing", fails("M8 busy (dpkg's lock)", "plan: the check busy"), lambda d: gjson(d, "m8-busy-dpkg-plan", lambda x: x["checks"][6].update(detail="unreadable")))
    m("M8 busy dpkg: the install goes through", fails("M8 busy (dpkg's lock)", "ends with status 2"), lambda d: rc(d, "m8-busy-dpkg-install", 3))
    m("M8 busy dpkg: the API starts the job", fails("M8 busy (dpkg's lock)", "refused busy, with its reason"), lambda d: code(d, "m8-busy-dpkg-api", 202))
    m("M8 busy dpkg: the lock is not given back", invalid_run("the lock was not given back"), lambda d: write_out(d, "m8-busy-dpkg-undo.out", "the lock is still held\n"))
    m("M8 busy osu: the flag is not put up", fails("M8 busy (the flag of the upgrade)", "the flag of the upgrade of the operating system was in place"), lambda d: write_out(d, "m8-busy-osu-dirt.out", "no\n"))
    m("M8 busy osu: the plan is refused for another reason", fails("M8 busy (the flag of the upgrade)", "plan: the plan is refused busy"), lambda d: gjson(d, "m8-busy-osu-plan", lambda x: x.update(refused="disk_short")))
    m("M8 busy osu: the API starts the job", fails("M8 busy (the flag of the upgrade)", "refused busy, with its reason"), lambda d: code(d, "m8-busy-osu-api", 202))
    m("M8 busy osu: the flag stays", invalid_run("the flag was not taken away again"), lambda d: fact(d, "m8-busy-osu-after", "osflag", "yes"))
    m("M8 disk: the plan is not refused", fails("M8 disk_short", "the plan is refused disk_short"), lambda d: gjson(d, "m8-disk-plan", lambda x: x.update(refused=None)))
    m("M8 disk: no tmpfs was mounted", fails("M8 disk_short", "a tmpfs of 1 MiB"), lambda d: write_out(d, "m8-disk-dirt.out", "mounted no\n"))
    m("M8 disk: the install is refused another code", fails("M8 disk_short", "install: the RESULT line says refused - disk_short"), lambda d: subst(d, "m8-disk-install.out", "disk_short", "disk_unknown", 2))
    m("M8 disk: the tmpfs stays", invalid_run("the tmpfs could not be taken away again"), lambda d: fact(d, "m8-disk-after", "tmp_mount", "yes"))
    m("M8 symlink: the file the link points to changed", fails("M8 link in the area of the install", "did not follow the link"), lambda d: write_out(d, "m8-symlink-canary-after.out", "canary 0000 600 9\n"))
    m("M8 symlink: the install fails with a code of the download", fails("M8 link in the area of the install", "went through"), lambda d: (rc(d, "m8-symlink-install", 3), subst(d, "m8-symlink-install.out", "ok done - -", "failed download download_failed rolled_back")))
    m("M8 symlink: the link was not planted", fails("M8 link in the area of the install", "was planted"), lambda d: write_out(d, "m8-symlink-dirt.out", "no\n"))
    m("M8 symlink: the link stays", invalid_run("the link could not be taken away again"), lambda d: write_out(d, "m8-symlink-undo.out", "off link=yes\n"))
    m("M8 held: the plan passes", fails("M8 held_package", "refused held_package"), lambda d: gjson(d, "m8-held-plan", lambda x: x.update(refused=None)))
    m("M8 held: the check does not say a package is held", fails("M8 held_package", "says that a package is held"), lambda d: gjson(d, "m8-held-plan", lambda x: x["checks"][9].update(detail="apt gave no answer")))
    m("M8 held: the install is accepted", fails("M8 held_package", "ends with status 2"), lambda d: rc(d, "m8-held-install", 0))
    m("M8 held: the package could not be held", invalid_run("could not be put in place"), lambda d: write_out(d, "m8-held-dirt.out", "no\n"))
    m("M8 held: the package is not back", invalid_run("could not be put back as it was"), lambda d: dpkg_line(d, "m8-held-after", "ovmf", "none"))
    m("M8 manual: the plan is refused", fails("M8 a package marked automatic", "the plan passes"), lambda d: gjson(d, "m8-manual-plan", lambda x: x.update(refused="apt_failed")))
    m("M8 manual: the list of the automatic packages is written", fails("M8 a package marked automatic", "files of apt and dpkg"), lambda d: subst(d, "snap-m8-manual-after.aptstate", "/var/lib/apt/extended_states\t", "/var/lib/apt/extended_states\t9"))
    m("M8 manual: the package is marked manual", fails("M8 a package marked automatic", "still marked automatically installed"), lambda d: drop_line(d, "snap-m8-manual-after.pkgs", "auto qemu-utils"))
    m("M8 manual: the package was not automatic when it was asked", invalid_run("was not marked automatically installed"), lambda d: drop_line(d, "snap-m8-manual-before.pkgs", "auto qemu-utils"))
    m("M8 manual: apt does not say `set to manually installed`", not_testable("M8 a package marked automatic", "does not say"), lambda d: write_out(d, "m8-manual-aptsim.out", "rc 0\n"))
    m("M8 held: apt does not see the hold", not_testable("M8 held_package", "apt itself does not say"), lambda d: write_out(d, "m8-held-aptsim.out", "rc 0\nnew ovmf\n"))
    return M


def kill_mutations():
    M = []

    def m(label, expect, fn):
        M.append((label, fn, expect))

    K = lambda s: "m9-kill-%s" % s
    T = lambda s: KILL[s]
    # the kill at the units step stands for the kills; the others have the checks that are theirs
    m("M9 kill: the job reads succeeded", fails(T("units"), "never reads succeeded"), lambda d: rows_edit(d, K("units") + "-job", lambda rows: rows + [rows[-1][:COL["state"]] + ["succeeded"] + rows[-1][COL["state"] + 1:]]))
    m("M9 kill: the job reads failed, not interrupted", fails(T("units"), "reads interrupted in the end"), lambda d: set_last(d, K("units") + "-job", state="failed"))
    m("M9 kill: the job stays running", fails(T("units"), "reaches a final state"), lambda d: (set_last(d, K("units") + "-job", state="running"), write_out(d, K("units") + "-job.timeout", "240\n")))
    m("M9 kill: the step of the interrupted job is another", fails(T("units"), "the step of the last marker"), lambda d: set_last(d, K("units") + "-job", step="route"))
    m("M9 kill: the log says the end", fails(T("units"), "no RESULT line"), lambda d: add_line(d, K("units") + "-log.out", "CASAOS_MODULE_RESULT %s virt ok done - - 2026-09-21T14:00:00Z" % re.search(r"STEP ([0-9a-f]{32}) virt", read_out(d, K("units") + "-log.out")).group(1)))
    m("M9 kill: apt or dpkg is still at work", fails(T("units"), "no assistant, apt or dpkg is at work"), lambda d: (write_out(d, K("units") + "-wait.out", "not clean after 700 s: dpkg\n"), rc(d, K("units") + "-wait", 1)))
    m("M9 kill: dpkg is left interrupted", fails(T("units"), "dpkg --audit finds nothing once the box is quiet"), lambda d: fact(d, K("units") + "-dead", "dpkg_audit", "2"))
    m("M9 kill: remove is refused", fails(T("units"), "remove finishes what the interrupted install left"), lambda d: rc(d, K("units") + "-remove", 2))
    m("M9 kill: remove leaves a file", fails(T("units"), "after the remove: nothing of the module"), lambda d: add_line(d, "snap-" + K("units") + "-clean.files", "/usr/bin/casaos-virt-management\tf\t755\t0:0\tabc"))
    m("M9 kill: remove leaves the list", fails(T("units"), "after the remove: nothing of the module"), lambda d: write_out(d, "snap-" + K("units") + "-clean.lists_files", "casaos-module-files 1\n"))
    m("M9 kill: a fresh install fails", fails(T("units"), "a fresh install succeeds"), lambda d: rc(d, K("units") + "-fresh", 3))
    m("M9 kill: the module is not taken off for the next case", fails(T("units"), "taken off again for the next case"), lambda d: write_out(d, K("units") + "-fp2.out", "/usr/bin/casaos-virt-management\n"))
    m("M9 kill: the start is refused", fails(T("units"), "POST install is accepted"), lambda d: code(d, K("units") + "-start", 409))
    m("M9 kill: the signal could not be sent", invalid_run("the signal could not be sent"), lambda d: subst(d, K("units") + "-watch-result.out", "rc=0", "rc=1"))
    m("M9 kill: no process to signal", invalid_run("no process of the assistant"), lambda d: write_out(d, K("units") + "-watch-result.out", "no process of the assistant to signal\n"))
    m("M9 kill: the step is too short to be seen", not_testable(T("verify"), "shorter than that"), lambda d: (write_out(d, K("verify") + "-watch-result.out", "never saw the step verify begin\n"), rows_edit(d, K("verify") + "-job", lambda rows: rows[:-1] + [rows[-1][:COL["state"]] + ["succeeded", "done"] + rows[-1][COL["state"] + 2:]])))
    m("M9 kill: nothing to signal because the install was over", not_testable(T("route"), "was over"), lambda d: (write_out(d, K("route") + "-watch-result.out", "no process of the assistant to signal" + chr(10)), rows_edit(d, K("route") + "-job", lambda rows: rows[:-1] + [rows[-1][:COL["state"]] + ["succeeded", "done"] + rows[-1][COL["state"] + 2:]])))
    m("M9 kill: the install failed before the step", invalid_run("without reaching the step"), lambda d: (write_out(d, K("verify") + "-watch-result.out", "never saw the step verify begin\n"), set_last(d, K("verify") + "-job", state="failed", code="packages_failed")))
    m("M9 kill: the signal landed after the step", not_testable(T("manifest"), "the log ended at"), lambda d: edit_lines(d, K("manifest") + "-watch-result.out", lambda ls: [l.replace("virt manifest begin", "virt start begin") if l.startswith("after=") else l for l in ls]))
    m("M9 kill: the install had ended", not_testable(T("route"), "had ended"), lambda d: edit_lines(d, K("route") + "-watch-result.out", lambda ls: [("after=CASAOS_MODULE_RESULT %s virt ok done - - 2026-09-21T14:00:00Z" % ("0" * 32)) if l.startswith("after=") else l for l in ls]))
    m("M9 kill at packages: apt is left interrupted", fails(T("packages"), "dpkg --audit finds nothing once the box is quiet"), lambda d: fact(d, K("packages") + "-dead", "dpkg_audit", "1"))
    m("M9 kill at packages: the box is not quiet", fails(T("packages"), "no assistant, apt or dpkg is at work"), lambda d: (write_out(d, K("packages") + "-wait.out", "not clean after 700 s: dpkg unit\n"), rc(d, K("packages") + "-wait", 1)))
    m("M9 kill: the first poll is lost", fails(T("download"), "reads interrupted in the end"), lambda d: set_last(d, K("download") + "-job", state="running"))
    # the SIGTERM
    m("M9 term: the job is interrupted, not rolled back", fails("M9 SIGTERM at the step start", "reads failed in the end"), lambda d: set_last(d, "m9-term-start-job", state="interrupted"))
    m("M9 term: the undo is not said", fails("M9 SIGTERM at the step start", "failed with the code interrupted, rolled back"), lambda d: set_last(d, "m9-term-start-job", undo="-"))
    m("M9 term: the packages are not kept", fails("M9 SIGTERM at the step start", "the packages are kept"), lambda d: set_last(d, "m9-term-start-job", kept="-"))
    m("M9 term: it failed at another step", fails("M9 SIGTERM at the step start", "it failed at the step it was in"), lambda d: set_last(d, "m9-term-start-job", step="packages"))
    # the stop
    m("M9 stop: the unit took the whole of its 420 s", fails("M9 systemctl stop during the step start", "before systemd's 420 s ran out"), lambda d: subst(d, "m9-stop-start-watch-result.out", "seconds=3", "seconds=420"))
    m("M9 stop: systemctl stop failed", fails("M9 systemctl stop during the step start", "ends with status 0"), lambda d: subst(d, "m9-stop-start-watch-result.out", "stopped rc=0", "stopped rc=5"))
    m("M9 stop: the rollback is not in the log", fails("M9 systemctl stop during the step start", "the steps of the rollback"), lambda d: edit_lines(d, "m9-stop-start-log.out", lambda ls: [l for l in ls if "rollback_" not in l]))
    m("M9 stop: the job is rolled back and the result is another", fails("M9 systemctl stop during the step start", "the RESULT line says failed start interrupted rolled_back"), lambda d: subst(d, "m9-stop-start-log.out", "failed start interrupted rolled_back", "failed start units_failed rolled_back"))
    m("M9 stop at packages: dpkg was not running", not_testable("M9 systemctl stop during the step packages", "dpkg was not running"), lambda d: subst(d, "m9-stop-packages-watch-result.out", "dpkg_seen=yes", "dpkg_seen=no"))
    m("M9 stop at packages: dpkg is left interrupted", fails("M9 systemctl stop during the step packages", "dpkg --audit finds nothing a second after the stop"), lambda d: subst(d, "m9-stop-packages-watch-result.out", "dpkg_audit=0", "dpkg_audit=2"))
    m("M9 stop at packages: the box left broken is said once, the cases that cannot start after it are not INVALID", fails("M9 systemctl stop during the step packages", "dpkg --audit finds nothing a second after the stop", also=("M9 systemctl stop during the step start", "NOT REACHED")),
      lambda d: (subst(d, "m9-stop-packages-watch-result.out", "dpkg_audit=0", "dpkg_audit=2"), rc(d, "m9-stop-packages-fresh", 3), write_out(d, "m9-stop-start-watch-result.out", "never saw the step start begin\n"),
                 set_last(d, "m9-stop-start-job", state="failed", code="packages_failed")))
    m("M9 stop at packages: the job reads succeeded", fails("M9 systemctl stop during the step packages", "never reads succeeded"), lambda d: rows_edit(d, "m9-stop-packages-job", lambda rows: rows + [rows[-1][:COL["state"]] + ["succeeded"] + rows[-1][COL["state"] + 1:]]))
    return M


def end_mutations():
    M = []

    def m(label, expect, fn):
        M.append((label, fn, expect))

    # M10
    m("M10: the restart was not seen", invalid_run("the guest did not come back"), lambda d: write_out(d, "m10-reboot.failed", "the guest did not come back, or the core did not answer\n"))
    m("M10: the module was not running before", invalid_run("not installed and running before the restart"), lambda d: fact(d, "m10-before", "unit_active", "inactive"))
    m("M10: the same boot id", fails("M10 the restart", "another boot id"), lambda d: write_out(d, "boot-m10-reboot.tsv", "".join("%s\t%s\t%s\n" % (r.split("\t")[0], r.split("\t")[1], read_out(d, "m10-reboot.boot-old").strip()) for r in read_out(d, "boot-m10-reboot.tsv").splitlines())))
    m("M10: the unit is not active after the restart", fails("M10 the restart", "enabled and active again"), lambda d: fact(d, "m10-after", "unit_active", "inactive"))
    m("M10: the unit is not enabled after the restart", fails("M10 the restart", "enabled and active again"), lambda d: fact(d, "m10-after", "unit_enabled", "disabled"))
    m("M10: the route does not answer", fails("M10 the restart", "status after the restart"), lambda d: gjson(d, "m10-status", lambda x: x["route"].update(answers=False)))
    m("M10: the host does not answer", fails("M10 the restart", "the host answers"), lambda d: code(d, "m10-host", 404))
    m("M10: the test variables survive the restart", fails("M10 the restart", "neither the environment of the manager nor the core's"), lambda d: fact(d, "m10-after", "manager_env", "3"))
    m("M10: the remove after the restart fails", fails("M10 remove after the restart", "remove after the restart works"), lambda d: rc(d, "m10-remove", 3))
    m("M10: the remove leaves a file", fails("M10 remove after the restart", "nothing of the module"), lambda d: add_line(d, "snap-m10-removed.files", "/usr/bin/casaos-virt-management\tf\t755\t0:0\tabc"))
    m("M10: the footprint is not empty", fails("M10 remove after the restart", "nothing of the module is left"), lambda d: write_out(d, "m10-fp.out", "/etc/casaos/modules/virt.json\n"))
    # M11
    m("M11: a file of /etc/casaos changed", fails("M11 /etc/casaos", "no file of /etc/casaos"), lambda d: subst(d, "snap-m11-end.etccasaos", "/etc/casaos/gateway.ini\tgw1", "/etc/casaos/gateway.ini\tgw9"))
    m("M11: a file of the module is left in /etc/casaos", fails("M11 /etc/casaos", "no file of /etc/casaos", also=("M11 /etc",)), lambda d: (add_line(d, "snap-m11-end.etccasaos", "/etc/casaos/virt-management.conf.sample\tabc"), add_line(d, "snap-m11-end.etc", "/etc/casaos/virt-management.conf.sample\tabc")))
    m("M11: a package was removed", fails("M11 dpkg", "no package was removed"), lambda d: drop_line(d, "snap-m11-end.dpkg", "libc6\t"))
    m("M11: a package changed version without the plan", fails("M11 dpkg", "whose version changed"), lambda d: subst(d, "snap-m11-end.dpkg", "libc6\t1.0", "libc6\t2.0"))
    m("M11: a package nobody planned was installed", fails("M11 dpkg", "that are new"), lambda d: add_line(d, "snap-m11-end.dpkg", "nmap\t7.0\tii "))
    m("M11: a package of the manifest is not installed", fails("M11 dpkg", "the packages of the manifest are installed", also=()), lambda d: drop_line(d, "snap-m11-end.dpkg", "ovmf\t"))
    m("M11: the drop-in that libvirt's own script makes in /etc/dnsmasq.d is explained", passes("M11 /etc"), lambda d: add_line(d, "snap-m11-end.etc", "/etc/dnsmasq.d/libvirt-daemon	abc"))
    m("M11: another file of /etc/dnsmasq.d is not", fails("M11 /etc", "every file of /etc"), lambda d: add_line(d, "snap-m11-end.etc", "/etc/dnsmasq.d/other	abc"))
    m("M11: a file of /etc that nothing explains", fails("M11 /etc", "every file of /etc"), lambda d: add_line(d, "snap-m11-end.etc", "/etc/cron.d/evil\tabc"))
    m("M11: a file of /etc that was there is gone", fails("M11 /etc", "every file of /etc"), lambda d: drop_line(d, "snap-m11-end.etc", "/etc/hostname\t"))
    m("M11: a name that nothing explains", fails("M11 the rest of the file system", "every name that is new or gone"), lambda d: add_line(d, "snap-m11-end.names", "/usr/bin/stray-program"))
    m("M11: the sandbox of a plan is left in /var/tmp", fails("M11 the rest of the file system", "/var/tmp holds what it held", also=()), lambda d: add_line(d, "snap-m11-end.names", "/var/tmp/casaos-plan-1"))
    m("M11: dpkg lists no file of the packages", invalid_run("lists no file of the module's packages"), lambda d: write_out(d, "m11-owned.out", ""))
    m("M11: the removal of a package was not the proof's", fails("M11 dpkg", "no package was removed", also=("M11 the rest of the file system",)), lambda d: [write_out(d, f, read_out(d, f).replace("will_remove", "gone")) for f in os.listdir(d) if f.endswith("-unseat.out")])
    m("M11: a file of a merged /usr that nothing explains", fails("M11 the rest of the file system", "every name that is new or gone"), lambda d: subst(d, "m11-owned.out", "/bin/virsh", "/bin/other"))
    m("M11: the control is missing", invalid_run("m2-ctl-b is missing"), lambda d: [os.remove(os.path.join(d, f)) for f in os.listdir(d) if f.startswith("snap-m2-ctl-b.")])
    return M


def replace_line_fn(d, name, old, new):
    replace_line(d, name, old, new)


def run_mutations(label, base_out, leg, mutations):
    rc_, text = report(base_out, leg)
    base = statuses(text)
    check("%s: the recordings of a healthy run pass" % label, rc_ == 0 and head(text) == "PASS", head(text) + "\n" + text[:1500])
    for name, fn, exp in mutations:
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


# ---- the scenarios: the fake plays a core or a guest that does it wrong, and the script and the report are run on it ------------------------------------------------------

def scenario(label, kit, cases, cfg, exps, leg="d12", env_extra=None, keep=False):
    """the real script against a fake guest that misbehaves one way: what the report says of the recordings it makes"""
    out, w, proc = run_script(kit, leg, cases, cfg, env_extra=env_extra)
    rc_, text = report(out, leg)
    ok = True
    for i, exp in enumerate(exps if isinstance(exps, (list, tuple)) else [exps]):
        ok = judged("%s%s" % (label, "" if i == 0 else " (%d)" % (i + 1)), {}, rc_, text, exp, others_same=False) and ok
    if not ok:
        print(text[:3000])
        print(proc.stdout.decode("utf-8", "replace")[-1500:])
    if not keep:
        shutil.rmtree(os.path.dirname(out), ignore_errors=True)
    return out, w, text


def healthy_checks(label, out, w, proc, leg):
    """what a healthy run has to have done, beside what the report says of it"""
    check("%s: the script runs to its end against the fake guest" % label, proc.returncode == 0 and not read_out(out, "aborted").strip(), proc.stdout.decode("utf-8", "replace")[-400:] + read_out(out, "aborted"))
    marks = {ln.split("\t")[2] for ln in read_out(out, "timeline.tsv").splitlines() if ln.count("\t") == 2}
    want = {"case_M%d_start" % i for i in range(1, 12) if i not in (8, 9)} | {"case_M%d_done" % i for i in range(1, 12) if i not in (8, 9)}
    want |= {"case_M8_%s_%s" % (s, e) for s in ("checksum", "dotdot", "systemctl", "varlib", "usrshare", "etcmod", "foreign", "busy_dpkg", "busy_osu", "disk", "symlink", "held", "manual") for e in ("start", "done")}
    want |= {"case_M9_%s_%s" % (s, e) for s in ("kill_packages", "kill_download", "kill_verify", "kill_unpack", "kill_units", "kill_manifest", "kill_start", "kill_route", "stop_packages", "stop_start", "term_start")
             for e in ("start", "done")}
    check("%s: the timeline has the markers of every case" % label, want <= marks, str(sorted(want - marks)))
    check("%s: the first thing asked of the guest after its name is the version of the core (nothing runs `module` before it)" % label, w.verbs[:2] == ["hostname", "casaos_v"], str(w.verbs[:4]))
    check("%s: the test variables are out of the environment of the manager and of the core at the end" % label, w.manager_env == 0 and w.core_env == 0, "%s %s" % (w.manager_env, w.core_env))
    check("%s: every piece of dirt is undone at the end" % label, not (w.lock_held or w.osflag or w.tmpfs or w.dirt_unit or w.symlink or w.hold or w.dummies or w.dpkg_busy()), "")
    check("%s: the box ends as it began: nothing of the module, its packages installed" % label, not w.footprint_paths() and set(w.kit["packages"]) <= w.installed, str(w.footprint_paths()))
    check("%s: the token and the password never reach the recordings" % label, not any(("tok-" in read_out(out, f) or "Aa1-test" in read_out(out, f)) for f in os.listdir(out) if os.path.isfile(os.path.join(out, f)) and f.endswith((".req", ".out", ".err", ".tsv", ".txt", ".log"))), "")
    check("%s: no pending undo is left" % label, not [f for f in os.listdir(os.path.join(os.path.dirname(out), "work")) if f.startswith("pending-")], "")


def main():
    static_checks()
    kit, calls, mod, proc = build_kit()
    kit_checks(kit, calls, mod, proc)
    if os.environ.get("STATIC_ONLY"):
        return
    keep = KEEP
    if keep and os.path.isdir(os.path.join(keep, "d12")) and os.environ.get("REPORT_ONLY"):
        base_d12, base_u24 = os.path.join(keep, "d12"), os.path.join(keep, "u24")
    else:
        base_d12, w12, proc12 = run_script(kit, "d12", "")
        base_u24, w24, proc24 = run_script(kit, "u24", "")
        healthy_checks("d12", base_d12, w12, proc12, "d12")
        healthy_checks("u24", base_u24, w24, proc24, "u24")
        if keep:
            for n, src in (("d12", base_d12), ("u24", base_u24)):
                shutil.rmtree(os.path.join(keep, n), ignore_errors=True)
                shutil.copytree(src, os.path.join(keep, n))
    for leg, base in (("d12", base_d12), ("u24", base_u24)):
        rc_, text = report(base, leg)
        check("a healthy %s leg passes, every case by name" % leg, rc_ == 0 and head(text) == "PASS" and all(verdict_line(text, n) and verdict_line(text, n)[0] == "PASS" for n in EXPECTED), text[:2500])
        check("a healthy %s leg prints the first real-guest numbers" % leg, all(k in text for k in ("The first real-guest numbers", "time of the plan", "install by hand, seconds per step", "remove through the API",
                                                                                                   "install through the API", "restart:", "the box quiet after", "systemctl stop during")), text[-1500:])
        check("a healthy %s leg has no verdict that is not testable" % leg, "NOT TESTABLE" not in text, text[-800:])
    run_mutations("d12", base_d12, "d12", core_mutations() + core_mutations_api() + dirty_mutations() + kill_mutations() + end_mutations())
    if os.environ.get("REPORT_ONLY") or os.environ.get("BASE_ONLY"):
        return
    scenarios(kit)
    if not keep:
        for d in (os.path.dirname(base_d12), os.path.dirname(base_u24), os.path.dirname(kit)):
            shutil.rmtree(d, ignore_errors=True)


def scenarios(kit):
    scenario("scenario: a core older than the first that has the assistant: nothing but its version is asked", kit, "m1,m2", {"core_version": "v0.4.74"}, invalid_run("older than v0.4.75"))
    out, w, _ = scenario("scenario: a core older than the first that has the assistant: the module word is never given to it", kit, "m1,m2", {"core_version": "v0.4.74"}, invalid_run("older than v0.4.75"), keep=True)
    check("scenario: the guest was never asked to run `casaos module` on that core", "mod" not in w.verbs, str(w.verbs))
    shutil.rmtree(os.path.dirname(out), ignore_errors=True)
    scenario("scenario: a release that ships a catalogue", kit, "m1,m2", {"shipped_catalogue": True}, not_testable("M2 no catalogue", "ships a catalogue"))
    out, _, _ = scenario("scenario: a token that runs out is renewed by the runner", kit, "m1,m2,m3,m4,m5", {"token_valid_s": 100}, passes("M5 list"), keep=True)
    check("scenario: the runner logged in again when the token ran out", any(n.endswith(".notes") and "relogin" in read_out(out, n) for n in os.listdir(out)), "")
    shutil.rmtree(os.path.dirname(out), ignore_errors=True)
    scenario("scenario: the test variables do not reach the core", kit, "m3,m4,m5", {"api_env_missing": True}, invalid_run("did not reach the manager and the core"))
    scenario("scenario: a core that has no Modules API", kit, "m3,m4,m5", {"no_modules_api": True}, invalid_run("does not carry the Modules API"))
    scenario("scenario: a plan that writes the log of the planner", kit, "m1,m2,m3", {"plan_writes_eipp": True}, fails("M3 plan writes nothing", "eipp.log.xz"))
    scenario("scenario: an install that writes the unit with a setuid mode", kit, "m3,m4", {"wrong_mode": True}, fails("M4 files and state", "the mode of"))
    scenario("scenario: an install that writes a file that is not the archive's", kit, "m3,m4", {"wrong_content": True}, fails("M4 files and state", "the content of"))
    scenario("scenario: a route that never answers", kit, "m3,m4", {"route_never": True}, [fails("M4 install markers", "ends with status 0")])
    scenario("scenario: a systemd and a core that behave other than the busy check assumes", kit, "m3,m4,m5,m6,m7", {"glob_none_rc1": True, "props_wrong": True},
             [fails("M7 the glob of the busy check", "ends with status 0"), fails("M7 job unit properties", "KillMode is mixed")])
    scenario("scenario: a core that does not refuse when something runs", kit, "m3,m4,m5,m6,m7", {"busy_ignored": True}, fails("M7 the glob of the busy check", "refused busy, with its reason"))
    scenario("scenario: a remove that leaves the unit", kit, "m3,m4,m5,m6", {"remove_leaves_unit": True}, fails("M6 what is gone and what stays", "nothing of the module"))
    scenario("scenario: dirty cases against an assistant that writes the list of automatic packages and follows a link", kit, "m3,m4,m8",
             {"plan_writes_apt_state": True, "follow_symlink": True},
             [fails("M8 a package marked automatic", "files of apt and dpkg"), fails("M8 link in the area of the install", "did not follow the link")], env_extra={"M8_ONLY": "manual,symlink"})
    scenario("scenario: a kill that lands after the step", kit, "m3,m4,m5,m9", {"kill_lands_late": True}, not_testable("M9 SIGKILL at the step manifest", "the log ended at"), env_extra={"M9_ONLY": "kill_manifest"})
    scenario("scenario: a stop that leaves dpkg interrupted", kit, "m3,m4,m5,m9", {"dpkg_interrupted": True}, fails("M9 systemctl stop during the step packages", "dpkg --audit"), env_extra={"M9_ONLY": "stop_packages"})
    scenario("scenario: a unit that does not come back after the restart", kit, "m3,m4,m10", {"reboot_unit_down": True}, fails("M10 the restart", "enabled and active again"))
    scenario("scenario: a guest that never comes back", kit, "m3,m4,m10", {"down_seconds": 10 ** 7}, invalid_run("did not come back"), env_extra={"WAIT_BOOT": "60"})
    scenario("scenario: an install that changes a file of /etc nobody explains", kit, "m2,m3,m4,m11", {"etc_unexplained": True}, fails("M11 /etc", "every file of /etc"))
    quick = dict(packages=0.2, packages_work=0.4, download=0.1, verify=0.05, unpack=0.1, units=0.1, manifest=0.05, start=0.2, route=0.1, rollback=0.1, rem_stop=0.1, rem_files=0.1, rem_manifest=0.05, rem_reload=0.1)
    scenario("scenario: jobs so quick that their unit is gone before the first look: the journal and the nonce say it ran, the properties cannot be had", kit, "m3,m4,m5,m6,m7",
             {"step_seconds": quick, "start_takes_s": 3.0}, [passes("M6 the job"), not_testable("M7 job unit properties", "never seen active")])
    scenario("scenario: a lock that cannot be given back", kit, "m3,m4,m8", {"lock_stays": True}, invalid_run("the lock was not given back"), env_extra={"M8_ONLY": "busy_dpkg"})
    scenario("scenario: units that stand for jobs that cannot be stopped", kit, "m3,m4,m5,m6,m7", {"dummies_stay": True}, invalid_run("could not be stopped"))


if __name__ == "__main__":
    main()
    print("\n%d checks, %d failed" % (count[0], len(failures)))
    for f in failures:
        print("FAILED " + f[:600])
    sys.exit(1 if failures else 0)
