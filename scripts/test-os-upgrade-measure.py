#!/usr/bin/env python3
"""The measurement has to be able to say no. Each case below builds the directory a leg would leave, in a state that must come out a given
way, and checks what os-upgrade-measure-report.py says and its exit status:

    python3 scripts/test-os-upgrade-measure.py

A healthy leg of each of the four (d11-12, d12-13, u22-24, u24-26) is measured, and every row of its go/no-go table has a case in which only
the thing it decides is wrong, and that row must flip by name. A run that did not get as far as it should, or started from a box that was not
what the leg needs, must be INVALID and never GO. An Ubuntu hop that Canonical has not opened is NOT_OFFERED, neutral and visible.

The text-only functions of os-upgrade-measure.sh (the leg table, the guard on `do-release-upgrade -c`, the tool that rewrites apt's sources, the
probes made of bash alone), its phases run against stand-ins for apt, systemctl, dpkg and the rest (the hop's unit, the sandbox, the Ubuntu unit,
the poller), its runner functions run against a stand-in for ssh, and the workflow are tested too, when bash is here. No VM leg can run here: what
the real apt, systemd and Canonical's tool print is what the first dispatch of the workflow measures.

REPORT=<path>, MEASURE_SCRIPT=<path> and WORKFLOW=<path> run the cases against other copies, to see that a broken one is caught. CASES=<regex> runs
the cases whose name matches. REPORT_ONLY=1 stops after the cases of the report, GUEST_ONLY=1 skips them. TEST_BASH=<path> names a bash where plain
`bash` is not the one.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT = os.environ.get("REPORT") or os.path.join(HERE, "os-upgrade-measure-report.py")
SCRIPT = os.environ.get("MEASURE_SCRIPT") or os.path.join(HERE, "os-upgrade-measure.sh")
WORKFLOW = os.environ.get("WORKFLOW") or os.path.join(HERE, "..", ".github", "workflows", "os-upgrade-measure.yml")
ONLY = os.environ.get("CASES")
BASH = os.environ.get("TEST_BASH") or "bash"
T0 = 1790000000.0
ENV = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")

failures = []
count = [0]


def wanted(name):
    return not ONLY or re.search(ONLY, name) is not None


def report_cases_wanted():
    return not os.environ.get("GUEST_ONLY")


def ok_line(name):
    print("ok:", name)


# ---- the legs ---------------------------------------------------------------------------------------------------------------

DEB11 = dict(leg="d11-12", family="debian", old="11", new="12", old_code="bullseye", new_code="bookworm", old_pretty="Debian GNU/Linux 11 (bullseye)", new_pretty="Debian GNU/Linux 12 (bookworm)",
             apt="apt 2.2.4 (amd64)", libc=("2.31-13+deb11u11", "2.36-9+deb12u10"), systemd=("247.3-7+deb11u6", "252.39-1~deb12u1"), dbus=("1.12.28-0+deb11u1", "1.14.10-1~deb12u1"),
             kernel=("5.10.0-35-cloud-amd64", "6.1.0-37-cloud-amd64"), docker="28.0.4", dockerpkg="5:28.0.4-1~debian.11~bullseye", dockernew="5:29.8.0-1~debian.11~bullseye", frozen=True)
DEB12 = dict(leg="d12-13", family="debian", old="12", new="13", old_code="bookworm", new_code="trixie", old_pretty="Debian GNU/Linux 12 (bookworm)", new_pretty="Debian GNU/Linux 13 (trixie)",
             apt="apt 2.6.1 (amd64)", libc=("2.36-9+deb12u10", "2.41-12+deb13u1"), systemd=("252.39-1~deb12u1", "257.9-1~deb13u1"), dbus=("1.14.10-1~deb12u1", "1.16.2-2"),
             kernel=("6.1.0-37-cloud-amd64", "6.12.48+deb13-cloud-amd64"), docker="29.8.0", dockerpkg="5:29.8.0-1~debian.12~bookworm", dockernew="5:29.9.0-1~debian.13~trixie", frozen=False)
UBU22 = dict(leg="u22-24", family="ubuntu", old="22.04", new="24.04", old_code="jammy", new_code="noble", old_pretty="Ubuntu 22.04.5 LTS", new_pretty="Ubuntu 24.04.3 LTS",
             apt="apt 2.4.14 (amd64)", libc=("2.35-0ubuntu3.11", "2.39-0ubuntu8.6"), systemd=("249.11-0ubuntu3.16", "255.4-1ubuntu8.10"), dbus=("1.12.20-2ubuntu4.1", "1.14.10-4ubuntu4.1"),
             kernel=("5.15.0-1090-kvm", "6.8.0-1040-kvm"), docker="29.8.0", dockerpkg="5:29.8.0-1~ubuntu.22.04~jammy", dockernew="5:29.8.0-1~ubuntu.24.04~noble", frozen=False)
UBU24 = dict(leg="u24-26", family="ubuntu", old="24.04", new="26.04", old_code="noble", new_code="resolute", old_pretty="Ubuntu 24.04.3 LTS", new_pretty="Ubuntu 26.04.1 LTS",
             apt="apt 2.7.14 (amd64)", libc=("2.39-0ubuntu8.6", "2.43-1ubuntu1"), systemd=("255.4-1ubuntu8.10", "259-1ubuntu1"), dbus=("1.14.10-4ubuntu4.1", "1.16.2-1ubuntu2"),
             kernel=("6.8.0-1040-kvm", "7.0.0-12-kvm"), docker="29.8.0", dockerpkg="5:29.8.0-1~ubuntu.24.04~noble", dockernew="5:29.8.0-1~ubuntu.26.04~resolute", frozen=False)
LEGS = {s["leg"]: s for s in (DEB11, DEB12, UBU22, UBU24)}
CONTAINERS = {"p-always": "always", "p-unless-stopped": "unless-stopped", "p-no": "no", "p-on-failure": "on-failure", "p-db": "unless-stopped", "p-web": "unless-stopped", "p-host": "unless-stopped"}
STAYS = ("p-no", "p-on-failure")     # what does not start again by itself
ENGINE = ["docker-ce", "docker-ce-cli", "containerd.io", "docker-ce-rootless-extras", "docker-buildx-plugin", "docker-compose-plugin"]
CASA_DEPS = ["wget", "curl", "smartmontools", "parted", "ntfs-3g", "net-tools", "udevil", "samba", "cifs-utils", "mergerfs", "unzip", "git", "wsdd"]
CASA_UNITS = ["casaos.service", "casaos-gateway.service", "casaos-app-management.service", "casaos-message-bus.service", "casaos-user-service.service", "casaos-local-storage.service"]
OTHER_UNITS = ["ssh.service", "smbd.service", "nmbd.service", "docker.service", "containerd.service", "cron.service", "dbus.service", "systemd-logind.service", "osm-poller.service"]

# the time of everything, in seconds from T0
S = dict(prepare_start=0, refresh_start=60, refresh_done=240, prepare_done=900, inh1=920, inh1_back=1000, inh2=1030, inh2_back=1100, reboot_pre=1130, reboot_pre_back=1200,
         baseline_start=1215, baseline_done=1260, probe_start=1262, probe_done=1700, hop_start=1710, hop_unit_end=3500, hop_done=3520, reboot_post=3530, reboot_post_back=3600,
         after_start=3615, after_settled=3640, after_done=3680)
REBOOTS = [("inh1", "inh1_back"), ("inh2", "inh2_back"), ("reboot_pre", "reboot_pre_back"), ("reboot_post", "reboot_post_back")]


def put(d, name, text):
    with open(os.path.join(d, name), "w", newline="") as f:
        f.write(text)


def get(d, name):
    try:
        with open(os.path.join(d, name), newline="") as f:
            return f.read().replace("\r\n", "\n")   # what a guest on Windows (this harness, when it runs there) wrote with Python's own newlines
    except OSError:
        return ""


def rec(d, name, text="", rc=0):
    put(d, name + ".txt", text)
    put(d, name + ".rc", "%d\n" % rc)


def hex32(seed):
    return ("%032x" % (abs(hash(("osm", seed))) % (16 ** 32)))


def iso(t):
    return datetime.fromtimestamp(T0 + t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".%06dZ" % int((T0 + t) % 1 * 1e6)


def os_release(pretty, id_, ver, code):
    return 'PRETTY_NAME="%s"\nNAME="%s"\nVERSION_ID="%s"\nVERSION_CODENAME=%s\nID=%s\n' % (pretty, id_.title(), ver, code, id_)


def apt_summary(up, new, rem, nu, need_mb=None, disk_mb=None, total=True):
    out = ["%d upgraded, %d newly installed, %d to remove and %d not upgraded." % (up, new, rem, nu)]
    if need_mb is not None:
        out.append("Need to get %s MB of archives." % format(need_mb, ",") if not total else "Need to get 0 B/%s MB of archives." % format(need_mb, ","))
    if disk_mb is not None:
        out.append("After this operation, %s MB of additional disk space will be used." % format(disk_mb, ","))
    return "\n".join(out) + "\n"


def sim_text(s, up, new, rem, need, disk, docker_inst=False, docker_remv=False):
    lines = ["Reading package lists...", "Building dependency tree...", "Calculating upgrade...", "The following packages will be REMOVED:", "  libaio1 libgcrypt20-dev",
             "The following NEW packages will be installed:", "  libnftables1 nftables", "The following packages will be upgraded:", "  libc6 systemd dbus bash apt"]
    lines += [apt_summary(up, new, rem, 0, need, disk, total=False).rstrip("\n")]
    lines += ["Inst libc6 [%s] (%s Debian:12 [amd64])" % (s["libc"][0], s["libc"][1]), "Remv libaio1 [0.3.112-9]"]
    if docker_inst:
        lines.append("Inst docker-ce [%s] (%s Docker CE:%s [amd64])" % (s["dockerpkg"], s["dockernew"], s["old_code"]))
    if docker_remv:
        lines.append("Remv docker-ce [%s]" % s["dockerpkg"])
    lines.append("Conf libc6 (%s Debian:12 [amd64])" % s["libc"][1])
    return "\n".join(lines) + "\n"


def package_set(s, when, spec):
    """what dpkg-query -W says: before, hop (after the unit), after (after the reboot)"""
    new = when != "before"
    pk = {"libc6": s["libc"][1 if new else 0], "systemd": s["systemd"][1 if new else 0], "dbus": s["dbus"][1 if new else 0], "bash": "5.2" if new else "5.1", "apt": "2.9" if new else "2.2",
          "openssh-server": "1:9.2p1-2" if new else "1:8.4p1-5", "linux-image-cloud-amd64": s["kernel"][1 if new else 0], "osm-orphan": "1.0", "osm-orphan-held": "1.0"}
    for n in CASA_DEPS[:-1]:
        pk[n] = "1.0"
    for n in ENGINE:
        pk[n] = s["dockerpkg"]
    for i in range(30):
        pk["libfoo%d" % i] = "2.%d" % i if new else "1.%d" % i
    if new:
        pk["libnftables1"] = "1.0"
        pk["nftables"] = "1.0"
        pk.pop("libfoo1")
    if s["family"] == "ubuntu" and when != "before":
        pk.pop("osm-orphan", None)                  # the tool removes what no repository has
        if spec.get("variant", "hold") == "hold":
            pk.pop("docker-buildx-plugin", None)    # the unheld ones go, the held ones stay
            pk.pop("docker-compose-plugin", None)
            pk.pop("docker-ce-rootless-extras", None)
        else:
            pk["osm-orphan"] = "1.0"                 # RemoveObsoletes=no: nothing is removed
    return pk


def sandbox_logs(d, s):
    for v in ("paused", "kept"):
        frozen_inst = s["frozen"] and v == "kept"
        rec(d, "sandbox-%s-update" % v, "Hit:1 http://deb.debian.org/debian %s InRelease\nReading package lists...\n" % s["new_code"])
        rec(d, "sandbox-%s-upgrade" % v, sim_text(s, 120, 4, 0, 160, 60))
        rec(d, "sandbox-%s-dist-upgrade" % v, sim_text(s, 350, 38, 4, 412, 610, docker_inst=frozen_inst))
        put(d, "sandbox-%s-sources.txt" % v, "==> sources.list <==\ndeb http://deb.debian.org/debian %s main\n" % s["new_code"] + ("==> parts/docker.list <==\ndeb https://download.docker.com/linux/debian %s stable\n" % s["old_code"] if v == "kept" else ""))
        put(d, "sandbox-%s-size.txt" % v, "143\t/var/tmp/osm-sandbox.abc\n")
    rec(d, "without-new-pkgs", sim_text(s, 120, 4, 0, 160, 60))
    for h in ("etc-apt-before.sha", "etc-apt-after-sandbox.sha"):
        put(d, h, "a" * 64 + "\n")
    for h in ("lists-before.sha", "lists-after-sandbox.sha"):
        put(d, h, "b" * 64 + "\n")
    for f in ("suite", "version", "label"):
        rec(d, "allow-%s-plain" % f, "E: Repository 'http://deb.debian.org/debian %s InRelease' changed its '%s' value from 'osm-changed' to 'x'\nE: This must be accepted explicitly before updates for this repository can be applied.\n" % (s["new_code"], f.capitalize()), 100)
        rec(d, "allow-%s-field" % f, "Get:1 http://deb.debian.org/debian %s InRelease\nReading package lists...\n" % s["new_code"], 0)
        rec(d, "allow-%s-all" % f, "Get:1 http://deb.debian.org/debian %s InRelease\nReading package lists...\n" % s["new_code"], 0)


def step_log(s, name):
    if name in ("minimal", "minimal_dl"):
        return "Reading package lists...\nThe following packages will be upgraded:\n  apt bash libc6 systemd dbus\n" + apt_summary(120, 4, 0, 230, 160, 60) + "Setting up apt (2.6) ...\n"
    if name in ("full", "full_dl"):
        lib = ("Preparing to unpack .../libc6_%s_amd64.deb ...\nUnpacking libc6:amd64 (%s) over (%s) ...\nSetting up libc6:amd64 (%s) ...\n"
               "Checking for services that may need to be restarted...\nChecking init scripts...\nRestarting services possibly affected by the upgrade:\n"
               "  cron: restarting...done.\n  ssh: restarting...done.\n  smbd: restarting...done.\nServices restarted successfully.\nSetting up libc-bin (%s) ...\n") % (s["libc"][1], s["libc"][1], s["libc"][0], s["libc"][1], s["libc"][1])
        return ("Reading package lists...\nThe following packages will be REMOVED:\n  libaio1 libgcrypt20-dev\nThe following NEW packages will be installed:\n  libnftables1 nftables\n"
                "The following packages will be upgraded:\n  libc6 systemd\n" + apt_summary(230, 36, 3, 0, 252, 540) + lib)
    if name.startswith(("converge", "catchup")):
        return "Reading package lists...\n" + apt_summary(0, 0, 0, 0)
    return "Reading package lists...\nDone\n"


def build(d, s, spec=None):
    """the directory a healthy leg leaves; spec: variant (hold|keep), offered (False: an Ubuntu hop Canonical has not opened)"""
    spec = spec or {}
    deb = s["family"] == "debian"
    variant = spec.get("variant", "hold")
    skipped = not deb and spec.get("offered") is False
    put(d, "leg", s["leg"] + "\n")
    put(d, "os-before", os_release(s["old_pretty"], s["family"], s["old"], s["old_code"]))
    new_os = os_release(s["new_pretty"], s["family"], s["new"], s["new_code"])
    put(d, "start-version", s["docker"] + "\n")
    put(d, "steps.log", "[measure] prepared\n")
    put(d, "box-facts", "apt %s\ndpkg Debian 'dpkg' package management program version 1.20.14 (amd64).\nsystemd systemd 247 (%s)\nlibc6 %s\nbash 5.1.4(1)-release\npython3 Python 3.9.2\nkernel %s\nsh /usr/bin/dash\ntz +0000\ncpus 4\nmemory_mb 5900\ncloud-init /usr/bin/cloud-init 22.4.2\nneedrestart none\nupgrader %s\n" % (
        s["apt"], s["systemd"][0], s["libc"][0], s["kernel"][0], "1:22.04.21" if not deb else "none"))
    for tag, mounts in (("start", 25000), ("prepared", 21000), ("before", 20800), ("probed", 20500), ("hop", 14000), ("after", 14500)):
        put(d, "df-%s.txt" % tag, "Filesystem 1048576-blocks Used Available Capacity Mounted on\n/dev/vda1 30000 %d %d 30%% /\n/dev/vda1 30000 %d %d 30%% /boot\n/dev/vda1 30000 %d %d 30%% /var\n" % ((30000 - mounts, mounts) * 3))
    put(d, "df-steps.txt", "update / 20000\nfull / 14000\n")
    # the timeline
    items = []

    def mark(t, name):
        items.append((t, "#\t%.6f\t%s" % (T0 + t, name)))
    for name, key in (("prepare_start", "prepare_start"), ("refresh_start", "refresh_start"), ("refresh_done", "refresh_done"), ("prepare_done", "prepare_done"), ("inhibit_reboot_start", "inh1"),
                      ("inhibit_force_start", "inh2"), ("reboot_pre_start", "reboot_pre"), ("baseline_start", "baseline_start"), ("baseline_done", "baseline_done"), ("probe_start", "probe_start"), ("probe_done", "probe_done")):
        mark(S[key], name)
    for key, back in REBOOTS[:3]:
        mark(S[back] + 12, "boot_changed aaa bbb")
    if skipped:
        mark(S["hop_start"], "hop_skipped")
        put(d, "hop-skipped", "not_offered\n")
    else:
        mark(S["hop_start"], "hop_start")
        for name in ("hop_unit_end", "hop_done", "reboot_post_start", "after_start", "after_settled", "after_done"):
            mark(S[{"reboot_post_start": "reboot_post"}.get(name, name)], name)
        mark(S["reboot_post_back"] + 15, "boot_changed ccc ddd")
    # a sample a second, but where the box is down
    down = [(S[a] + 3, S[b]) for a, b in REBOOTS] if not skipped else [(S[a] + 3, S[b]) for a, b in REBOOTS[:3]]
    end = S["after_done"] if not skipped else S["hop_start"] + 30
    names = ",".join(sorted(CONTAINERS))
    t = 0
    while t <= end:
        t += 1
        if any(a <= t < b for a, b in down):
            continue
        row = dict(docker=s["docker"], ssh="up", smb="up", gw="200", core="200", am="200", ami="401", web="200", host="200", names=names)
        for _, back in REBOOTS:
            r = t - S[back]
            if 0 <= r < 30:       # the box just booted: what answers first
                row.update(docker="-" if r < 4 else s["docker"], ssh="up" if r >= 2 else "down", smb="up" if r >= 6 else "down", gw="200" if r >= 18 else "000", core="200" if r >= 20 else "000",
                           am="200" if r >= 24 else "000", ami="401" if r >= 24 else "000", web="200" if r >= 28 else "000", host="200" if r >= 28 else "000",
                           names="-" if r < 4 else ",".join(n for n in sorted(CONTAINERS) if n not in STAYS) if r >= 28 else "p-always")
        if S["hop_start"] + 800 <= t < S["hop_start"] + 803 and not skipped:
            row["ssh"] = "down"
        if S["hop_start"] + 800 <= t < S["hop_start"] + 805 and not skipped:
            row["smb"] = "down"
        if t > S["reboot_pre_back"] + 30 or t < S["inh1"]:
            pass
        # the containers that do not start by themselves are gone for good after the first reboot
        if t >= S["inh1_back"]:
            row["names"] = ",".join(n for n in names.split(",") if n not in STAYS) if row["names"] == names else row["names"]
        items.append((T0 + t - T0, "%.6f\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s" % (T0 + t, row["docker"], row["ssh"], row["smb"], row["gw"], row["core"], row["am"], row["ami"], row["web"], row["host"], row["names"])))
    put(d, "timeline.tsv", "\n".join(ln for _, ln in sorted(items, key=lambda x: x[0])) + "\n")
    # units
    def units(tag, gen, casa_gen):
        out = []
        for n in OTHER_UNITS + CASA_UNITS:
            inv = (casa_gen if n in CASA_UNITS else gen)
            out.append("%s\tactive\trunning\t%d\t%s" % (n, 100 + len(out), hex32((n, inv))))
        put(d, "units-%s.tsv" % tag, "\n".join(out) + "\n")
        put(d, "listen-%s.txt" % tag, "0.0.0.0:22\n0.0.0.0:80\n0.0.0.0:445\n")
    units("before", 1, 1)
    # the hop restarted what libc6's postinst restarts (cron, ssh, smbd, nmbd); the core and Docker were not touched
    out = []
    for n in OTHER_UNITS + CASA_UNITS:
        inv = 2 if n in ("ssh.service", "smbd.service", "nmbd.service", "cron.service") else 1
        out.append("%s\tactive\trunning\t%d\t%s" % (n, 100 + len(out), hex32((n, inv))))
    put(d, "units-hop.tsv", "\n".join(out) + "\n")
    units("boot", 3, 3)
    units("after", 3, 3)
    # packages
    for tag, when in (("before", "before"), ("hop", "hop"), ("after", "after")):
        pk = package_set(s, when, spec)
        put(d, "dpkg-%s.tsv" % tag, "".join("%s\t%s\tii \n" % (n, v) for n, v in sorted(pk.items())))
    put(d, "dpkg-hop.log", "%s 12:00:01 status half-configured libc6:amd64 %s\n%s 12:00:09 status installed libc6:amd64 %s\n" % (
        datetime.fromtimestamp(T0 + S["hop_start"] + 700, timezone.utc).strftime("%Y-%m-%d"), s["libc"][1], datetime.fromtimestamp(T0 + S["hop_start"] + 700, timezone.utc).strftime("%Y-%m-%d"), s["libc"][1]))
    # containers
    for tag, running in (("before", set(CONTAINERS)), ("hop", set(CONTAINERS)), ("boot", {n for n in CONTAINERS if n not in STAYS} - {"p-web"}), ("after", {n for n in CONTAINERS if n not in STAYS})):
        put(d, "containers-%s.txt" % tag, "".join("/%s policy=%s state=%s started=2026-10-10T19:14:01.1Z restarts=0\n" % (n, CONTAINERS[n], "running" if n in running else "exited") for n in CONTAINERS))
    for tag, ver in (("before", s["docker"]), ("hop", s["docker"]), ("after", s["docker"])):
        put(d, "docker-%s.txt" % tag, "server %s\ndriver overlay2\nactive active\npackages docker-ce=%s\n" % (ver, s["dockerpkg"]))
    # health
    for tag in ("before", "hop", "after"):
        rec(d, "audit-" + tag)
        rec(d, "aptcheck-" + tag, "Reading package lists...\n")
        rec(d, "failed-" + tag)
        rec(d, "sysstate-" + tag, "running\n")
        rec(d, "holds-" + tag, "osm-orphan-held\n")
    for tag, k in (("before", 0), ("hop", 0), ("after", 1)):
        put(d, "kernel-%s.txt" % tag, "uname %s\nboot-file /boot/vmlinuz-%s %d\nboot-file /boot/initrd.img-%s %d\nboot-total %d\ndf-boot 900 %d %d /boot\nimages linux-image-cloud-amd64=%s\nreboot-required %s\n" % (
            s["kernel"][k], s["kernel"][k], 8000000, s["kernel"][k], 60000000, 70000000 * (2 if tag != "before" else 1), 250 * (2 if tag != "before" else 1), 650, s["kernel"][k], "yes" if tag == "hop" else "no"))
    for tag in ("before", "hop", "after"):
        put(d, "deps-%s.txt" % tag, "".join("%s %s\n" % (n, "none" if n == "wsdd" else "ii ") for n in CASA_DEPS))
    put(d, "settle-baseline", "2\n")
    put(d, "baseline-started", "p-no\np-on-failure\n")
    put(d, "settle-after", "9\n")
    put(d, "jwt-claims-jwt-before.json", json.dumps({"issued": T0 + S["baseline_start"] + 20, "claims": {"id": 1, "exp": int(T0 + S["baseline_start"] + 20 + 10800), "iss": "casaos"}}) + "\n")
    for n in ("gateway", "core", "am", "jwt", "internal"):
        put(d, "%s-before.code" % n, "200\n")
        put(d, "%s-after.code" % n, "200\n")
    put(d, "jwt-fresh.code", "200\n")
    put(d, "sysstate-wait.txt", "running\n")
    put(d, "sysstate-wait.rc", "0\n")
    put(d, "analyze.txt", "Startup finished in 3.2s (kernel) + 10.1s (userspace) = 13.3s\n")
    # witnesses: an acknowledged write every 2 s, none while the box is down
    acks, t = [], 905.0
    gaps = [(S[a] + 2, S[b] + 35) for a, b in REBOOTS]
    while t < S["after_done"]:
        t += 2
        if not any(a <= t < b for a, b in gaps) and not (skipped and t > S["hop_start"] + 30):
            acks.append((t, "%d-%d" % (T0 + t, len(acks) + 1)))
    ids = "".join(i + "\n" for _, i in acks)
    for tag, cut in (("before", S["baseline_done"]), ("hop", S["hop_done"]), ("after", 1e9)):
        sel = [(t_, i) for t_, i in acks if t_ <= cut]
        put(d, "db-%s-logs.txt" % tag, "".join("%s ack %s\n" % (iso(t_), i) for t_, i in sel))
        put(d, "db-%s-file.txt" % tag, "".join(i + "\n" for _, i in sel))
        put(d, "db-%s-logs.exit" % tag, "0\n")
        put(d, "db-%s-file.exit" % tag, "0\n")
    # the inhibitor: not refused (root goes round it): the guest writes the attempt, and the box goes down under it
    for v, key in (("reboot", "inh1"), ("force", "inh2")):
        put(d, "inhibit-%s.txt" % v, "variant %s\ncommand systemd-inhibit --what=shutdown --mode=block -- systemctl reboot\nboot_id aaa\nstart %.6f\n" % (v, T0 + S[key]))
        put(d, "inhibit-%s.out" % v, "")
    # the reboots
    put(d, "reboot-pre.txt", "boot_id bbb\nissued %.6f\nmethod timer\n" % (T0 + S["reboot_pre"]))
    put(d, "reboot-post.txt", "boot_id ccc\nissued %.6f\nmethod timer\n" % (T0 + S["reboot_post"]))
    # the probes
    if deb:
        sandbox_logs(d, s)
    else:
        rec(d, "ubuntu-check", "Checking for a new Ubuntu release\nNew release '%s.5 LTS' available.\nRun 'do-release-upgrade' to upgrade to it.\n" % s["new"] if not skipped else "Checking for a new Ubuntu release\nNo new release found.\n", 0 if not skipped else 1)
        rec(d, "release-upgrades-before", "[DEFAULT]\nPrompt=lts\n")
        rec(d, "meta-release", "Dist: %s\nName: X\nVersion: %s.5 LTS\nDate: Thu, 21 Apr 2022 00:00:00 UTC\nSupported: 1\n\nDist: %s\nName: Y\nVersion: %s.1 LTS\nDate: Thu, 25 Apr 2024 00:00:00 UTC\nSupported: 1\n" % (s["old_code"], s["old"], s["new_code"], s["new"]))
        rec(d, "upgrader-tarball", "HTTP/1.1 200 OK\nLast-Modified: Mon, 07 Sep 2026 10:00:00 GMT\n")
        rec(d, "pro-status", '{"attached": false}\n')
        rec(d, "upgradable-before", "Listing...\n")
    # the hop
    if skipped:
        return
    ul = [(S["hop_start"] + 1, "START")]
    if deb:
        names_ = ["update", "minimal_dl", "minimal", "full_dl", "full", "converge1"]
    else:
        names_ = ["update", "upgrader"]
    ul.append((S["hop_start"] + 2, "SRC_START"))
    ul.append((S["hop_start"] + 4, "SRC_END"))
    tcur = S["hop_start"] + 10
    for n in names_:
        ul.append((tcur, "STEP_START %s" % n))
        put(d, "step-%s.log" % n, step_log(s, n) if deb else ("Checking for a new Ubuntu release\nrunning...\n" if n == "upgrader" else "Reading package lists...\n"))
        put(d, "step-%s.rc" % n, "0\n")
        tcur += 120 if n in ("minimal", "full") else 20
        ul.append((tcur, "STEP_END %s 0" % n))
        if n == "upgrader":
            ul.append((tcur + 1, "UPGRADER_EXIT 0"))
    ul.append((S["hop_unit_end"] - 5, "DONE"))
    put(d, "hop-unit.log", "".join("%.6f %s\n" % (T0 + t_, w) for t_, w in ul))
    for tag in ("start", "end"):
        put(d, "unit-%s.txt" % tag, "pid 4242\ninvocation %s\nmanager %s\nsystemd %s\ndbus %s\nlibc6 %s\ntty not a tty\n" % (hex32("inv"), "247" if tag == "start" else "252", s["systemd"][0 if tag == "start" else 1], s["dbus"][0 if tag == "start" else 1], s["libc"][0 if tag == "start" else 1]))
    put(d, "os-hop", new_os)
    put(d, "os-after", new_os)
    jd = datetime.fromtimestamp(T0 + S["hop_start"] + 700, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    put(d, "hop-journal.txt", "%s.123456+00:00 host systemd[1]: Stopping OpenBSD Secure Shell server...\n%s.223456+00:00 host systemd[1]: Started OpenBSD Secure Shell server.\n" % (jd, jd))
    put(d, "apt-history.log", "Start-Date: 2026-10-10  12:00:00\nCommandline: apt-get dist-upgrade\n")
    rec(d, "unit-state", "ActiveState=inactive\nResult=success\n")
    if deb:
        put(d, "sources-before.txt", "==> /etc/apt/sources.list <==\ndeb http://deb.debian.org/debian %s main\ndeb https://download.docker.com/linux/debian %s stable\n" % (s["old_code"], s["old_code"]))
        put(d, "sources-after.txt", "==> /etc/apt/sources.list <==\ndeb http://deb.debian.org/debian %s main\n" % s["new_code"])
        put(d, "sources-paused.txt", "/etc/apt/sources.list.d/docker.list\n")
    else:
        put(d, "controls.txt", "variant %s\nheld %s\n" % (variant, "osm-orphan-held docker-ce docker-ce-cli containerd.io" if variant == "hold" else "osm-orphan-held"))
        rec(d, "ubuntu-check-unit", "New release '%s.5 LTS' available.\n" % s["new"])
        rec(d, "release-upgrades-after", "[DEFAULT]\nPrompt=lts\n")
        rec(d, "pro-status-after", '{"attached": false}\n')
        put(d, "hop-sample.log", "".join("%.6f screen=0 sshd1022=0\n" % (T0 + S["hop_start"] + 100 * i) for i in range(1, 8)))
        put(d, "dist-upgrade-main.log", "2026-10-10 12:00:00,000 INFO Using ubuntu-release-upgrader 1:24.04.27\n2026-10-10 12:00:01,000 INFO release-upgrader: starting\n")
        listing = "total 8\n-rw-r--r-- 1 root root 200 Oct 10 12:00 docker.list.casaos-paused\n-rw-r--r-- 1 root root 190 Oct 10 12:30 docker.sources\n"
        rec(d, "sources-dir-after", listing)
        rec(d, "sources-dir-end", listing)
    put(d, "analyze.txt", "Startup finished in 3.2s (kernel) + 10.1s (userspace) = 13.3s\n")
    put(d, "uptime-since.txt", "2026-10-10 13:00:00\n")


# ---- the cases ----------------------------------------------------------------------------------------------------------------

_templates = {}


def run_report(d, leg):
    p = subprocess.run([sys.executable, REPORT, d, leg], capture_output=True, text=True, env=ENV, encoding="utf-8")
    return p.returncode, p.stdout + p.stderr


def table_row(text, question):
    """the answer of the go/no-go table's row whose question starts with the text"""
    for ln in text.splitlines():
        m = re.match(r"^\| (.*?) \| \*\*([A-Z_-]+)\*\* \| (.*) \|$", ln)
        if m and m.group(1).startswith(question):
            return m.group(2), m.group(3)
    return None, ""


def case(name, leg, want_code, mutate=None, spec=None, must_have=(), must_not_have=(), rows=None, empty=False):
    """rows: {question prefix: answer} the go/no-go table has to give"""
    if not wanted(name) or not report_cases_wanted():
        return
    count[0] += 1
    s = LEGS[leg]
    key = (leg, json.dumps(spec or {}, sort_keys=True))
    if key not in _templates:
        t = tempfile.mkdtemp()
        build(t, s, spec)
        _templates[key] = t
    with tempfile.TemporaryDirectory() as d:
        if not empty:
            shutil.copytree(_templates[key], d, dirs_exist_ok=True)
        if mutate:
            mutate(d)
        code, text = run_report(d, leg)
        problems = []
        if code != want_code:
            problems.append("exit %d, wanted %d" % (code, want_code))
        problems += ["missing %r" % x for x in must_have if x not in text]
        problems += ["should not say %r" % x for x in must_not_have if x in text]
        for q, a in (rows or {}).items():
            got, ev = table_row(text, q)
            if got != a:
                problems.append("row %r is %r, wanted %r (%s)" % (q, got, a, ev[:160]))
        if "Traceback" in text:
            problems.append("crashed")
        if problems:
            failures.append("%s: %s\n%s" % (name, "; ".join(problems), text[-2200:]))
        else:
            ok_line(name)


def flips(name, leg, question, want, mutate, spec=None, must_have=()):
    """one thing is wrong, and the row that decides it says so (and the run is still a measured one, not an invalid one)"""
    case(name, leg, 0, mutate=mutate, spec=spec, rows={question: want}, must_have=list(must_have), must_not_have=["INVALID RUN"])


def invalid(name, leg, reason, mutate, spec=None, empty=False, has=()):
    """a run that proves nothing: exit 1, INVALID RUN, the reason, and not one GO"""
    case(name, leg, 1, mutate=mutate, spec=spec, empty=empty, must_have=["INVALID RUN", reason] + list(has), must_not_have=["**GO**", "## The go/no-go table"])


def jedit(d, name, fn):
    j = json.loads(get(d, name))
    fn(j)
    put(d, name, json.dumps(j))


def replace(d, name, old, new, n=-1):
    text = get(d, name)
    assert old in text, "%r is not in %s" % (old, name)
    put(d, name, text.replace(old, new, n))


def ledit(d, name, fn):
    put(d, name, "\n".join(fn(get(d, name).splitlines())) + "\n")


def remove(d, *names):
    for n in names:
        os.remove(os.path.join(d, n))


def timeline_edit(d, fn):
    """fn(fields) -> fields | None (drop) for the samples of the poller; the markers are left alone"""
    out = []
    for ln in get(d, "timeline.tsv").splitlines():
        f = ln.split("\t")
        if f[0] == "#":
            out.append(ln)
            continue
        f = fn(f)
        if f is not None:
            out.append("\t".join(f))
    put(d, "timeline.tsv", "\n".join(out) + "\n")


def drop_marker(name):
    return lambda d: ledit(d, "timeline.tsv", lambda ls: [ln for ln in ls if not re.match(r"^#\t[0-9.]+\t%s( |$)" % re.escape(name), ln)])


def unit_set(d, name, fn):
    """fn(unit, fields) -> fields for the rows of a units file"""
    def each(ls):
        out = []
        for ln in ls:
            f = ln.split("\t")
            out.append("\t".join(fn(f[0], f)))
        return out
    ledit(d, name, each)


Q_SANDBOX = "The sandbox plan works"
Q_FROZEN = "The docker-ce built for"
Q_CORE = "The core, the gateway and AppManagement come back by themselves"
Q_TOKEN = "A token issued before the hop"
Q_UNIT = "The transient unit survives"
Q_HOP = "The hop did what it set out to"
Q_INH = "The product can rely on the block inhibitor"
Q_TOOL = "The Ubuntu tool can be driven from a unit (no terminal"
Q_DOCKER = "What the tool does to Docker"
Q_HOLD = "apt-mark hold keeps Docker's engine"
Q_CFG = "RemoveObsoletes=no in release-upgrades.d"

# ---- healthy legs ----------------------------------------------------------------------------------------------------------------

for leg in ("d11-12", "d12-13"):
    s = LEGS[leg]
    case("a healthy %s leg is measured: every row of the table is answered, and the inhibitor does not keep root out" % leg, leg, 0,
         must_have=["MEASURED", "## The go/no-go table", "How close the plan is to the transaction that was done", "libc6 %s to %s" % s["libc"], "cron, smbd, ssh",
                    "as documented", "Disk was NOT short in the guest"],
         must_not_have=["INVALID", "NOT_OFFERED", "Traceback"],
         rows={Q_SANDBOX: "GO", Q_FROZEN: "GO", Q_CORE: "GO", Q_TOKEN: "GO", Q_UNIT: "GO", Q_HOP: "GO", Q_INH: "NO-GO"})
for leg, variant in (("u22-24", "hold"), ("u24-26", "hold"), ("u22-24", "keep")):
    case("a healthy %s leg, variant %s, is measured" % (leg, variant), leg, 0, spec={"variant": variant},
         must_have=["MEASURED", "## The go/no-go table", "Canonical's side", "Last-Modified", "Mon, 07 Sep 2026"], must_not_have=["INVALID", "NOT_OFFERED", "Traceback"],
         rows=dict({Q_CORE: "GO", Q_TOKEN: "GO", Q_UNIT: "GO", Q_HOP: "GO", Q_TOOL: "GO", Q_DOCKER: "GO"}, **({Q_HOLD: "GO", Q_CFG: "OPEN"} if variant == "hold" else {Q_HOLD: "OPEN", Q_CFG: "GO"})))
for leg in ("u22-24", "u24-26"):
    case("%s: an upgrade Canonical has not opened is NOT_OFFERED, neutral and visible, and no row is a GO" % leg, leg, 0, spec={"offered": False},
         must_have=["NOT_OFFERED", "neither a pass nor a failure", "No new release found"], must_not_have=["INVALID", "**GO**"],
         rows={"The Ubuntu tool can be driven from a unit, and what it does to Docker": "NOT_OFFERED"})
case("the report is ASCII (a runner's console may not be UTF-8)", "d11-12", 0, must_not_have=[], mutate=None)

# ---- the rows flip ---------------------------------------------------------------------------------------------------------------------

def seq(*fns):
    return lambda d: [f(d) for f in fns] and None


def fill_boot_gap(d):
    """the box did not go down: samples through the gap of the reboot after the hop"""
    rows_ = []
    for t_ in range(S["reboot_post"] + 3, S["reboot_post_back"]):
        rows_.append("%.6f\t28.0.4\tup\tup\t200\t200\t200\t401\t200\t200\t%s" % (T0 + t_, ",".join(sorted(CONTAINERS))))
    ledit(d, "timeline.tsv", lambda ls: ls + rows_)


def no_boot_change_after(name):
    return lambda d: ledit(d, "timeline.tsv", lambda ls: [ln for ln in ls if not (ln.startswith("#") and "boot_changed" in ln and float(ln.split("\t")[1]) > T0 + S[name])])


L = "d11-12"
flips("sandbox: the sandbox's update fails", L, Q_SANDBOX, "NO-GO", lambda d: rec(d, "sandbox-paused-update", "E: The repository does not have a Release file.\n", 100), must_have=["The repository does not have a Release file"])
flips("sandbox: -s dist-upgrade fails", L, Q_SANDBOX, "NO-GO", lambda d: rec(d, "sandbox-paused-dist-upgrade", "E: Unable to correct problems, you have held broken packages.\n", 100))
flips("sandbox: -s upgrade fails", L, Q_SANDBOX, "NO-GO", lambda d: rec(d, "sandbox-paused-upgrade", "E: x\n", 100))
flips("sandbox: /etc/apt is not as it was", L, Q_SANDBOX, "NO-GO", lambda d: put(d, "etc-apt-after-sandbox.sha", "c" * 64 + "\n"), must_have=["/etc/apt CHANGED"])
flips("sandbox: apt's lists are not as they were", L, Q_SANDBOX, "NO-GO", lambda d: put(d, "lists-after-sandbox.sha", "c" * 64 + "\n"))
case("sandbox: the kept-source plan may remove Docker and the row (which asks about the paused source) does not change", L, 0,
     mutate=lambda d: replace(d, "sandbox-kept-dist-upgrade.txt", "Conf libc6", "Remv docker-ce [x]\nConf libc6"), rows={Q_SANDBOX: "GO", Q_FROZEN: "GO"},
     must_have=["the kept-source simulation removes an engine package | docker-ce"])
flips("frozen Docker: the paused simulation removes docker-ce", L, Q_FROZEN, "NO-GO", lambda d: replace(d, "sandbox-paused-dist-upgrade.txt", "Conf libc6", "Remv docker-ce [x]\nConf libc6"), must_have=["an engine package: docker-ce"])
flips("frozen Docker: the paused -s upgrade purges containerd.io", L, Q_FROZEN, "NO-GO", lambda d: replace(d, "sandbox-paused-upgrade.txt", "Conf libc6", "Purg containerd.io [x]\nConf libc6"))
flips("frozen Docker: docker-ce is not the same version after the reboot", L, Q_FROZEN, "NO-GO", lambda d: replace(d, "dpkg-after.tsv", "docker-ce\t5:28.0.4-1~debian.11~bullseye", "docker-ce\t5:29.8.0-1~debian.11~bullseye"))
flips("frozen Docker: containerd.io is gone after the reboot", L, Q_FROZEN, "NO-GO", lambda d: ledit(d, "dpkg-after.tsv", lambda ls: [ln for ln in ls if not ln.startswith("containerd.io\t")]))
flips("frozen Docker: the daemon does not answer after the reboot", L, Q_FROZEN, "NO-GO", lambda d: replace(d, "docker-after.txt", "server 28.0.4", "server unavailable"))
case("frozen Docker: with the frozen repository kept, the plan includes Docker (the owner's box: 28.0.4 with a 29 on offer), and with it paused it does not", L, 0,
     must_have=["the plan includes Docker | yes: docker-ce", "the same with the source paused | no"])
case("frozen Docker: on Debian 12 the Docker repository is not frozen: no Docker in either plan", "d12-13", 0, must_have=["the plan includes Docker | no"])
flips("core: casaos is not active after the reboot", L, Q_CORE, "NO-GO", lambda d: unit_set(d, "units-after.tsv", lambda n, f: [f[0], "inactive", "dead", "0", ""] if n == "casaos.service" else f), must_have=["casaos.service"])
flips("core: the gateway was started again between the first look and settled", L, Q_CORE, "NO-GO",
      lambda d: unit_set(d, "units-after.tsv", lambda n, f: f[:4] + [hex32("again")] if n == "casaos-gateway.service" else f), must_have=["casaos-gateway.service"])
flips("core: the core never answers 200 after the reboot", L, Q_CORE, "NO-GO", lambda d: timeline_edit(d, lambda f: f[:5] + ["502"] + f[6:] if float(f[0]) > T0 + S["reboot_post"] and f[5] == "200" else f))
flips("core: AppManagement never answers 200, with either credential, after the reboot", L, Q_CORE, "NO-GO",
      lambda d: timeline_edit(d, lambda f: f[:6] + ["401", "401"] + f[8:] if float(f[0]) > T0 + S["reboot_post"] else f))
case("core: AppManagement answering only with the loopback secret is answering", L, 0, rows={Q_CORE: "GO"},
     mutate=lambda d: timeline_edit(d, lambda f: f[:6] + ["401", "200"] + f[8:] if float(f[0]) > T0 + S["reboot_post"] else f))
flips("core: no casaos service was active before: nothing to say", L, Q_CORE, "OPEN", lambda d: unit_set(d, "units-before.tsv", lambda n, f: [f[0], "inactive"] + f[2:] if n.startswith("casaos") else f))
flips("token: refused after the reboot, and not expired", L, Q_TOKEN, "NO-GO", lambda d: put(d, "jwt-after.code", "401\n"), must_have=["the token of the baseline: rejected"])
flips("token: refused after the reboot because it expired: not decidable", L, Q_TOKEN, "OPEN",
      seq(lambda d: put(d, "jwt-after.code", "401\n"), lambda d: put(d, "jwt-claims-jwt-before.json", json.dumps({"issued": T0 + 100, "claims": {"exp": int(T0 + 200)}}))), must_have=["the token of the baseline: expired"])
flips("token: a new login is refused too, which the row says", L, Q_TOKEN, "GO", lambda d: put(d, "jwt-fresh.code", "401\n"), must_have=["a new login afterwards: NOT accepted"])
flips("unit: it never wrote DONE", L, Q_UNIT, "NO-GO", lambda d: ledit(d, "hop-unit.log", lambda ls: [ln for ln in ls if not ln.endswith(" DONE")]))
flips("unit: it is not the same process at the end", L, Q_UNIT, "NO-GO", lambda d: replace(d, "unit-end.txt", "pid 4242", "pid 9999"))
flips("unit: neither systemd, nor dbus, nor libc6 was upgraded: the question was not asked", L, Q_UNIT, "OPEN", seq(lambda d: put(d, "dpkg-hop.tsv", get(d, "dpkg-before.tsv")), lambda d: put(d, "dpkg-after.tsv", get(d, "dpkg-before.tsv"))))
flips("hop: the release after the reboot is still the old one", L, Q_HOP, "NO-GO", lambda d: put(d, "os-after", get(d, "os-before")))
flips("hop: dpkg --audit has something to say after the reboot", L, Q_HOP, "NO-GO", lambda d: rec(d, "audit-after", "The following packages are only half configured: libc6\n", 1))
flips("hop: apt-get check fails after the reboot", L, Q_HOP, "NO-GO", lambda d: rec(d, "aptcheck-after", "E: Unmet dependencies.\n", 100))
flips("hop: the last simulation has things left to do", L, Q_HOP, "NO-GO", lambda d: put(d, "step-converge1.log", "Reading package lists...\n" + apt_summary(3, 0, 0, 0)))
flips("hop: the full upgrade failed and nothing repaired it", L, Q_HOP, "NO-GO", seq(lambda d: put(d, "step-full.rc", "100\n"), lambda d: replace(d, "hop-unit.log", "STEP_END full 0", "STEP_END full 100")))
case("hop: the full upgrade failed once and the notes' failure ladder completed it: a GO that says so", L, 0, rows={Q_HOP: "GO"}, must_have=["failure ladder"],
     mutate=seq(lambda d: put(d, "step-full.rc", "100\n"), lambda d: replace(d, "hop-unit.log", "STEP_END full 0", "STEP_END full 100"), lambda d: put(d, "step-recover_full.log", "ok\n"),
                lambda d: put(d, "step-recover_full.rc", "0\n"),
                lambda d: put(d, "hop-unit.log", get(d, "hop-unit.log") + "%.6f STEP_START recover_full\n%.6f STEP_END recover_full 0\n" % (T0 + S["hop_start"] + 1000, T0 + S["hop_start"] + 1100))))
case("hop: the release was reached and the box is clean, but the unit never reached DONE", L, 0, rows={Q_HOP: "NO-GO"}, must_have=["the unit did not reach DONE"],
     mutate=lambda d: ledit(d, "hop-unit.log", lambda ls: [ln for ln in ls if not ln.endswith(" DONE")]))


def refused(variant):
    """root's reboot refused: the guest wrote rc 1 and survived, the box never went down (no gap, no new boot)"""
    def m(d):
        put(d, "inhibit-%s.txt" % variant, get(d, "inhibit-%s.txt" % variant) + "rc 1\nsurvived yes\n")
        put(d, "inhibit-%s.out" % variant, "Operation inhibited by \"os-upgrade-measure\" (reason: refusal probe)\n")
        key, back = ("inh1", "inh1_back") if variant == "reboot" else ("inh2", "inh2_back")
        ledit(d, "timeline.tsv", lambda ls: [ln for ln in ls if not (ln.startswith("#") and "boot_changed" in ln and abs(float(ln.split("\t")[1]) - T0 - S[back] - 12) < 1)])
        ledit(d, "timeline.tsv", lambda ls: ls + ["%.6f\t28.0.4\tup\tup\t200\t200\t200\t401\t200\t200\t%s" % (T0 + t_, ",".join(sorted(CONTAINERS))) for t_ in range(S[key] + 3, S[back])])
    return m


case("inhibitor: root's reboot refused, and --force refused too", L, 0, rows={Q_INH: "GO"}, mutate=seq(refused("reboot"), refused("force")), must_have=["reboot: REFUSED; --force: REFUSED"])
case("inhibitor: refused for the plain reboot and not for --force: a GO that says what --force does", L, 0, rows={Q_INH: "GO"}, mutate=refused("reboot"), must_have=["--force goes round the lock", "reboot: REFUSED; --force: NOT REFUSED"])
case("inhibitor: exit 0 and the box did not go down: unclear", L, 0, rows={Q_INH: "OPEN"},
     mutate=seq(lambda d: put(d, "inhibit-reboot.txt", get(d, "inhibit-reboot.txt") + "rc 0\nsurvived yes\n"), refused("reboot"), lambda d: put(d, "inhibit-reboot.txt", get(d, "inhibit-reboot.txt").replace("rc 1", "rc 0"))), must_have=["UNCLEAR"])
case("libc6 was not upgraded by the hop: the question is open and the report says so", L, 0, must_have=["libc6 was not upgraded by the unit"],
     mutate=seq(lambda d: put(d, "dpkg-hop.tsv", get(d, "dpkg-hop.tsv").replace(DEB11["libc"][1], DEB11["libc"][0])), lambda d: put(d, "dpkg-after.tsv", get(d, "dpkg-after.tsv").replace(DEB11["libc"][1], DEB11["libc"][0]))))
case("the services that were restarted are the units whose InvocationID changed, not whose state did", L, 0, must_have=["| cron.service |", "| smbd.service |", "| ssh.service |", "ssh bounced during the hop | yes", "docker bounced during the hop | no"],
     must_not_have=["| docker.service |"])
case("a unit that bounced is named: Docker restarted by the hop", L, 0, must_have=["| docker.service |", "docker bounced during the hop | yes"],
     mutate=lambda d: unit_set(d, "units-hop.tsv", lambda n, f: f[:4] + [hex32("dockerd")] if n == "docker.service" else f))
case("the containers: the ones that start by themselves are listed back, the others are not_returned", L, 0, must_have=["ALL running again", "p-no, p-on-failure (the same list the Docker update calls not_returned)"])
case("a container that starts by itself and does not come back is said", L, 0, must_have=["NOT running again: p-always"],
     mutate=lambda d: replace(d, "containers-after.txt", "/p-always policy=always state=running", "/p-always policy=always state=exited"))
case("the database lost nothing; a lost write is counted", L, 0, must_have=["**0 missing**"])
case("a database that lost an acknowledged write says how many", L, 0, must_have=["**1 missing**"], mutate=lambda d: ledit(d, "db-after-file.txt", lambda ls: ls[:-1]))
case("a window of silence of the database is measured", L, 0, must_have=["Longest silence during the hop: 2.0 s", "Longest silence from the reboot to the containers being back: 1"])
case("the disk: the lowest free space is said", L, 0, must_have=["Lowest free space on a mount, after any step of the hop: 14000 MB", "Disk was NOT short in the guest"])
case("the runner's own df is reproduced when the workflow wrote it", L, 0, must_have=["host-df-end.txt", "/dev/root"], mutate=lambda d: put(d, "host-df-end.txt", "Filesystem Size Used Avail Use% Mounted on\n/dev/root 72G 41G 31G 58% /\n"))
case("a different apt than the leg was meant for is said and does not fail the leg", L, 0, must_have=["the apt of this leg is meant to be 2.2.4: it is 2.2.5"], mutate=lambda d: replace(d, "box-facts", "apt 2.2.4", "apt 2.2.5"))
case("--allow-releaseinfo-change: a field that apt does not complain about without the option is not as documented", L, 0, must_have=["| suite | exit 0 (no 'changed its' message) |", "DO NOT behave as documented"],
     mutate=lambda d: rec(d, "allow-suite-plain", "Reading package lists...\n", 0))
case("--allow-releaseinfo-change: the option of the field is refused by this apt", L, 0, must_have=["DO NOT behave as documented", "| version | exit 100 | exit 100 |"], mutate=lambda d: rec(d, "allow-version-field", "E: Unknown option\n", 100))
case("--allow-releaseinfo-change was skipped for a reason, and the report says what it could not decide", L, 0, must_have=["--allow-releaseinfo-change-* was not measured: no InRelease"],
     mutate=lambda d: put(d, "allow-skipped", "no InRelease of bookworm in the sandbox's lists\n"))

def unit_aborted(d):
    """the unit found no source naming the target release and stopped before any step: START, the sources, ENDED"""
    ul = [ln for ln in get(d, "hop-unit.log").splitlines() if ln.split(" ", 1)[1] in ("START", "SRC_START", "SRC_END")]
    put(d, "hop-unit.log", "\n".join(ul) + "\n%.6f ABORTED no source names bookworm after the rewrite\n%.6f ENDED 1\n" % (T0 + S["hop_start"] + 6, T0 + S["hop_start"] + 7))
    for n in ("update", "minimal_dl", "minimal", "full_dl", "full", "converge1"):
        remove(d, "step-%s.log" % n, "step-%s.rc" % n)


case("a unit that stopped before its steps (nothing to rewrite) is a finding: the rows say NO-GO, and the run is not invalid", L, 0, mutate=unit_aborted, rows={Q_UNIT: "NO-GO", Q_HOP: "NO-GO"}, must_not_have=["INVALID"],
     must_have=["the unit never wrote DONE"])

# Ubuntu
U = "u22-24"
flips("ubuntu tool: screen was seen", U, Q_TOOL, "NO-GO", lambda d: put(d, "hop-sample.log", get(d, "hop-sample.log") + "%.6f screen=1 sshd1022=0\n" % (T0 + S["hop_start"] + 900)), must_have=["screen SEEN"])
flips("ubuntu tool: an sshd on 1022 was seen", U, Q_TOOL, "NO-GO", lambda d: put(d, "hop-sample.log", get(d, "hop-sample.log") + "%.6f screen=0 sshd1022=1\n" % (T0 + S["hop_start"] + 900)), must_have=["sshd 1022 SEEN"])
flips("ubuntu tool: the sampler wrote nothing: open, not a GO", U, Q_TOOL, "OPEN", lambda d: put(d, "hop-sample.log", ""))
flips("ubuntu tool: the release after the reboot is the old one", U, Q_TOOL, "NO-GO", lambda d: put(d, "os-after", get(d, "os-before")))
flips("ubuntu tool: dpkg --audit is not clean after the reboot", U, Q_TOOL, "NO-GO", lambda d: rec(d, "audit-after", "half configured\n", 1))
flips("ubuntu tool: the unit never reached DONE", U, Q_TOOL, "NO-GO", lambda d: ledit(d, "hop-unit.log", lambda ls: [ln for ln in ls if not ln.endswith(" DONE")]))
flips("ubuntu docker: docker-ce was removed", U, Q_DOCKER, "NO-GO", seq(lambda d: ledit(d, "dpkg-hop.tsv", lambda ls: [ln for ln in ls if not ln.startswith("docker-ce\t")]), lambda d: ledit(d, "dpkg-after.tsv", lambda ls: [ln for ln in ls if not ln.startswith("docker-ce\t")])), must_have=["REMOVED"])
flips("ubuntu docker: the daemon does not answer after the reboot", U, Q_DOCKER, "NO-GO", lambda d: replace(d, "docker-after.txt", "server 29.8.0", "server unavailable"))
flips("ubuntu hold: a held engine package was removed", U, Q_HOLD, "NO-GO", lambda d: ledit(d, "dpkg-hop.tsv", lambda ls: [ln for ln in ls if not ln.startswith("docker-ce-cli\t")]))
flips("ubuntu hold: the held control was removed", U, Q_HOLD, "NO-GO", lambda d: ledit(d, "dpkg-hop.tsv", lambda ls: [ln for ln in ls if not ln.startswith("osm-orphan-held\t")]))
flips("ubuntu keep: the drop-in was ignored: the obsolete control was removed anyway", U, Q_CFG, "NO-GO", lambda d: ledit(d, "dpkg-hop.tsv", lambda ls: [ln for ln in ls if not ln.startswith("osm-orphan\t")]), spec={"variant": "keep"})
case("ubuntu: the tool removes what no repository has, and says it (the control), and which of Docker's were unheld", U, 0,
     must_have=["yes: osm-orphan was removed", "kept: osm-orphan-held is still installed", "the unheld ones among them: docker-ce-rootless-extras, docker-buildx-plugin, docker-compose-plugin", "docker.list.casaos-paused"], must_not_have=["INVALID"])
case("ubuntu keep: nothing is removed when the drop-in is read", U, 0, spec={"variant": "keep"}, must_have=["osm-orphan (held by nothing) is still installed", "no: osm-orphan is still installed"])
case("ubuntu: the hop that was offered and run is not a NOT_OFFERED", U, 0, must_not_have=["NOT_OFFERED"])
case("ubuntu: Prompt=normal offering 26.10 to a 26.04-bound box is not an offer of 26.04", "u24-26", 0, spec={"offered": False}, rows={"The Ubuntu tool can be driven from a unit, and what it does to Docker": "NOT_OFFERED"},
     mutate=seq(lambda d: rec(d, "ubuntu-check", "New release '26.10' available.\n", 0)), must_have=["NOT_OFFERED"])
case("ubuntu: the unit asked Canonical's tool again and the answer is in the facts", U, 0, must_have=["do-release-upgrade -c asked again inside the unit"])
case("ubuntu: Pro is said not attached, and that the attached case was not measured", U, 0, must_have=["not attached (a cloud image, no token): the behaviour with Pro attached was NOT measured", "Ubuntu Pro/ESM attached: not measured"])
case("ubuntu: what could not be decided names the other variant", U, 0, must_have=["run the workflow again with ubuntu_variant keep"])

# ---- an invalid run is never a GO ------------------------------------------------------------------------------------------------------

invalid("an empty directory proves nothing", L, "INVALID RUN", None, empty=True)
invalid("the guest said it could not go on", L, "the guest stopped on its own account: NOT PROVEN: Docker's repository has no containerd.io 1.7", lambda d: put(d, "aborted", "NOT PROVEN: Docker's repository has no containerd.io 1.7 to start from\n"))
invalid("evidence of another leg", L, "the evidence is of leg 'd12-13', not 'd11-12'", lambda d: put(d, "leg", "d12-13\n"))
for leg in ("d11-12", "u22-24"):
    for m in ("prepare_start", "refresh_start", "refresh_done", "prepare_done", "inhibit_reboot_start", "inhibit_force_start", "reboot_pre_start", "baseline_start", "baseline_done", "probe_start", "probe_done",
              "hop_unit_end", "hop_done", "reboot_post_start", "after_start", "after_settled", "after_done"):
        invalid("%s: the marker %s is missing" % (leg, m), leg, "marker %s is missing" % m, drop_marker(m))
invalid("neither hop_start nor hop_skipped", L, "neither hop_start nor hop_skipped is in the timeline", drop_marker("hop_start"))
invalid("hop_timeout: the guest gave up waiting for the unit", L, "hop_timeout", lambda d: ledit(d, "timeline.tsv", lambda ls: ls + ["#\t%.6f\thop_timeout" % (T0 + S["hop_unit_end"])]))
invalid("no reboot after the hop", L, "the guest did not reboot after the hop", no_boot_change_after("reboot_post"))
invalid("no reboot before the hop", L, "the guest did not reboot before the hop", lambda d: ledit(d, "timeline.tsv", lambda ls: [ln for ln in ls if not (ln.startswith("#") and "boot_changed" in ln and float(ln.split("\t")[1]) < T0 + S["baseline_start"] + 1)]))
invalid("the box did not go down for the reboot after the hop (no gap in the poller)", L, "no gap of the poller after reboot_post_start", fill_boot_gap)
HOP_FILES = ["unit-start.txt", "unit-end.txt", "dpkg-hop.tsv", "dpkg-after.tsv", "dpkg-before.tsv", "units-before.tsv", "units-hop.tsv", "units-boot.tsv", "units-after.tsv", "containers-hop.txt", "containers-boot.txt",
             "containers-after.txt", "os-after", "reboot-pre.txt", "reboot-post.txt", "kernel-before.txt", "kernel-after.txt", "docker-before.txt", "docker-after.txt", "deps-before.txt", "deps-after.txt",
             "jwt-claims-jwt-before.json", "hop-journal.txt", "dpkg-hop.log", "inhibit-reboot.txt", "inhibit-force.txt", "box-facts", "df-start.txt", "df-before.txt"]
for name in HOP_FILES:
    invalid("a missing %s" % name, L, "%s is missing or empty" % name if name != "dpkg-before.tsv" else "dpkg-before.tsv is missing or empty", lambda d, n=name: remove(d, n))
for name in ("audit-before", "audit-hop", "audit-after", "aptcheck-before", "aptcheck-hop", "aptcheck-after", "failed-before", "failed-hop", "failed-after"):
    invalid("a missing %s.rc: the step did not run" % name, L, "%s.rc is missing" % name, lambda d, n=name: remove(d, n + ".rc"))
for name in ("gateway-after", "core-after", "internal-after", "am-after", "jwt-after", "jwt-fresh"):
    invalid("a missing %s.code: the after phase did not ask" % name, L, "%s.code is missing" % name, lambda d, n=name: remove(d, n + ".code"))
for tag in ("before", "hop", "after"):
    invalid("the database's log could not be read (%s)" % tag, L, "the database's logs could not be read", lambda d, t=tag: put(d, "db-%s-logs.exit" % t, "124\n"))
    invalid("the database's file could not be read (%s)" % tag, L, "the database's file could not be read", lambda d, t=tag: remove(d, "db-%s-file.exit" % t))
invalid("the database's file is empty after the reboot: nothing is proved of the writes", L, "the database's log or file is empty after the reboot", lambda d: put(d, "db-after-file.txt", ""))
invalid("a step of the unit that began and has no status", L, "step full of the unit has no log or no status", lambda d: remove(d, "step-full.rc"))
invalid("a step of the unit that is not in its log although it ended", L, "step converge1 is not in the unit's log", lambda d: ledit(d, "hop-unit.log", lambda ls: [ln for ln in ls if "converge1" not in ln]))
invalid("a unit with no START", L, "hop-unit.log has no START line", lambda d: ledit(d, "hop-unit.log", lambda ls: [ln for ln in ls if not ln.endswith(" START")]))
for part in ("update", "upgrade", "dist-upgrade"):
    invalid("the sandbox's %s was not run" % part, L, "sandbox-paused-%s.rc is missing" % part, lambda d, p=part: remove(d, "sandbox-paused-%s.rc" % p))
    invalid("the kept sandbox's %s was not run" % part, L, "sandbox-kept-%s.rc is missing" % part, lambda d, p=part: remove(d, "sandbox-kept-%s.rc" % p))
for h in ("etc-apt-before.sha", "etc-apt-after-sandbox.sha", "lists-before.sha", "lists-after-sandbox.sha", "sandbox-paused-sources.txt", "sandbox-kept-sources.txt", "sources-before.txt", "sources-after.txt"):
    invalid("a missing %s" % h, L, "%s is missing or empty" % h, lambda d, n=h: remove(d, n))
invalid("the releaseinfo-change experiment did not run", L, "allow-label-all.rc is missing", lambda d: remove(d, "allow-label-all.rc"))
invalid("the notes' spelling of the minimal upgrade was not tried", L, "without-new-pkgs.rc is missing", lambda d: remove(d, "without-new-pkgs.rc"))
case("apt-get that does not take the notes' spelling of the minimal upgrade says so, and it is not a failure of the leg", L, 0, must_have=["NOT accepted (exit 100): E: Command line option '--without-new-pkgs' is not understood"],
     mutate=lambda d: rec(d, "without-new-pkgs", "E: Command line option '--without-new-pkgs' is not understood in combination with the other options.\n", 100), must_not_have=["INVALID"])
for name in ("meta-release.rc", "upgrader-tarball.rc", "pro-status.rc", "release-upgrades-before.rc"):
    invalid("ubuntu: a missing %s: the probe did not run" % name, U, "%s is missing: the Ubuntu probe did not run" % name, lambda d, n=name: remove(d, n))
invalid("ubuntu: no answer of do-release-upgrade -c", U, "ubuntu-check.rc is missing", lambda d: remove(d, "ubuntu-check.rc"))
for name in ("controls.txt", "sources-dir-after.txt", "release-upgrades-before.txt", "release-upgrades-after.txt"):
    invalid("ubuntu: a missing %s" % name, U, "%s is missing or empty" % name, lambda d, n=name: remove(d, n) if n != "release-upgrades-before.txt" else put(d, n, ""))
invalid("ubuntu: the unit did not ask Canonical's tool again", U, "ubuntu-check-unit.rc is missing", lambda d: remove(d, "ubuntu-check-unit.rc"))
invalid("ubuntu: the unit ended without the upgrader", U, "the unit ended without running the upgrader", lambda d: ledit(d, "hop-unit.log", lambda ls: [ln for ln in ls if "upgrader" not in ln.lower() or "UPGRADER_EXIT" in ln and False]))
invalid("ubuntu: offered and skipped all the same", U, "do-release-upgrade -c offered 24.04 and the hop was skipped all the same", seq(lambda d: put(d, "hop-skipped", "not_offered\n"), drop_marker("hop_start"), lambda d: ledit(d, "timeline.tsv", lambda ls: ls + ["#\t%.6f\thop_skipped" % (T0 + S["hop_start"])])))
invalid("ubuntu: not offered and started all the same", U, "do-release-upgrade -c did not offer 24.04 and the hop was started all the same", lambda d: rec(d, "ubuntu-check", "No new release found.\n", 1))
invalid("ubuntu: the skip says something else than not_offered", U, "hop-skipped says 'boom', not not_offered", seq(lambda d: put(d, "hop-skipped", "boom\n"), drop_marker("hop_start"), lambda d: ledit(d, "timeline.tsv", lambda ls: ls + ["#\t%.6f\thop_skipped" % (T0 + S["hop_start"])]), lambda d: rec(d, "ubuntu-check", "No new release found.\n", 1)))
invalid("hop skipped and started in the same run", U, "the hop was skipped and started in the same run", lambda d: put(d, "hop-skipped", "not_offered\n"))
# the box before the hop is not the one the leg needs
invalid("the box was Debian 12, not 11", L, "the box before the hop was debian 12 (bookworm), not debian 11 (bullseye)", lambda d: put(d, "os-before", os_release("Debian GNU/Linux 12 (bookworm)", "debian", "12", "bookworm")))
invalid("a witness was not running before the hop", L, "these witnesses were not running before the hop: p-db", lambda d: replace(d, "containers-before.txt", "/p-db policy=unless-stopped state=running", "/p-db policy=unless-stopped state=exited"))
invalid("a witness has another policy than the harness gave it", L, "the witness p-always has restart policy 'no', not 'always'", lambda d: replace(d, "containers-before.txt", "/p-always policy=always", "/p-always policy=no"))
invalid("no container list before", L, "these witnesses were not running before the hop", lambda d: remove(d, "containers-before.txt"))
for name in ("gateway-before", "core-before", "am-before", "jwt-before"):
    invalid("%s did not answer 200 before the hop" % name, L, "%s answered 502 before the hop" % name.replace("-before", ""), lambda d, n=name: put(d, n + ".code", "502\n"))
invalid("Docker did not answer before the hop", L, "Docker did not answer before the hop", lambda d: replace(d, "docker-before.txt", "server 28.0.4", "server unavailable"))
case("the containers that the reboots before the baseline left stopped are started again at the baseline, and the report says so", L, 0, must_have=["witnesses started again at the baseline (their policy does not start them after a reboot) | p-no, p-on-failure"])
invalid("the baseline did not ask the core with the secret", L, "internal-before.code is missing", lambda d: remove(d, "internal-before.code"))
invalid("the containers were not running by themselves at the baseline", L, "the witnesses that start by themselves were not running at the baseline", lambda d: put(d, "settle-baseline", "never\n"))
invalid("no docker-ce before", L, "docker-ce was not installed before the hop", lambda d: ledit(d, "dpkg-before.tsv", lambda ls: [ln for ln in ls if not ln.startswith("docker-ce\t")]))
invalid("the control package is not there", L, "the control package osm-orphan was not installed before the hop", lambda d: ledit(d, "dpkg-before.tsv", lambda ls: [ln for ln in ls if not ln.startswith("osm-orphan\t")]))
invalid("dpkg was not clean before", L, "dpkg --audit was not clean before the hop", lambda d: rec(d, "audit-before", "half configured\n", 1))
invalid("apt-get check failed before", L, "apt-get check failed before the hop", lambda d: rec(d, "aptcheck-before", "E: broken\n", 100))
invalid("Docker did not start at 28.0.4 on Debian 11", L, "Docker did not start at 28.0.4 on Debian 11", lambda d: put(d, "start-version", "29.8.0\n"))
invalid("the guest ran out of disk space", L, "the guest ran out of disk space (step-full.log)", lambda d: put(d, "step-full.log", get(d, "step-full.log") + "dpkg: error processing archive: cannot copy extracted data: No space left on device\n"))
invalid("a pipeline of the poller that left a hole in the hop", L, "the poller left 1 hole(s)", lambda d: timeline_edit(d, lambda f: None if T0 + S["hop_start"] + 100 < float(f[0]) < T0 + S["hop_start"] + 300 else f))
invalid("a poller that never wrote", L, "the timeline has no samples of the poller", lambda d: ledit(d, "timeline.tsv", lambda ls: [ln for ln in ls if ln.startswith("#")]))
invalid("a poller that died in the hop and came back with the reboot", L, "the poller left 1 hole(s)", lambda d: timeline_edit(d, lambda f: None if T0 + S["hop_start"] + 30 < float(f[0]) < T0 + S["reboot_post"] + 3 else f))
invalid("an inhibitor attempt that was not recorded", L, "inhibit-reboot.txt does not record the attempt", lambda d: put(d, "inhibit-reboot.txt", "variant reboot\n"))
invalid("an inhibitor attempt that was cut short: no reboot, no survived line", L, "the reboot attempt of the inhibitor ended without the box going down", seq(refused("reboot"), lambda d: put(d, "inhibit-reboot.txt", get(d, "inhibit-reboot.txt").replace("survived yes\n", ""))))
invalid("the leg's claims file is not JSON", L, "jwt-claims-jwt-before.json is not JSON", lambda d: put(d, "jwt-claims-jwt-before.json", "{not json"))
case("an invalid run still tells the reasons and exits 1, and never writes a table", L, 1, must_have=["INVALID RUN", "- INVALID:"], must_not_have=["## The go/no-go table", "**GO**", "**NO-GO**"], mutate=lambda d: put(d, "aborted", "boom\n"))
case("the report is written to report.md as well", L, 0, must_have=["MEASURED"], mutate=lambda d: None)
case("a bad leg name is refused with a usage message", L, 2, empty=True, must_have=["usage"], mutate=None) if False else None

# ---- the guest script ------------------------------------------------------------------------------------------------------------------

def finish_tests():
    if failures:
        print("\n".join(failures), file=sys.stderr)
        print("%d of %d cases failed" % (len(failures), count[0]), file=sys.stderr)
        sys.exit(1)
    print("ok: the measurement can say no (%d cases)" % count[0])
    sys.exit(0)


if os.environ.get("REPORT_ONLY"):
    finish_tests()

import socket
import threading
import time

with open(SCRIPT, newline="") as f:
    guest_text = f.read().replace("\r\n", "\n")


def posix(p):
    p = p.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    return "/%s/%s" % (m.group(1).lower(), m.group(2)) if m else p


SCRIPT_P = posix(SCRIPT)


def have_bash():
    try:
        return subprocess.run([BASH, "-c", "true"], capture_output=True).returncode == 0
    except OSError:
        return False


def bash_run(snippet, *args, env=None, input=None, cwd=None, timeout=120):
    """the guest script sourced into a bash that then runs the snippet (the runner's functions and the phases are run the same way)"""
    e = dict(ENV)
    e.update(env or {})
    return subprocess.run([BASH, "-c", 'source "$1"; shift; ' + snippet, "_", SCRIPT_P] + list(args), capture_output=True, text=True, input=input, env=e, cwd=cwd, encoding="utf-8", timeout=timeout)


def check(label, ok, detail=""):
    if not wanted(label):
        return
    count[0] += 1
    if ok:
        ok_line(label)
    else:
        failures.append("%s: %s" % (label, detail))


def fake(dir_, name, body):
    path = os.path.join(dir_, name)
    with open(path, "w", newline="\n") as f:
        f.write("#!/bin/bash\n" + body)
    os.chmod(path, 0o755)


def early_quitters(script):
    """what in a shell script leaves a producer writing into a pipe nobody reads any more, the class that ended the Debian 11 leg of the Docker proof (under
    pipefail the producer's SIGPIPE is status 141): a consumer that quits at its first match. `| head`, `| grep -q` or `-m`, `| sed ... q`, an awk that
    exits outside its END block. Comment lines are left out."""
    code = "\n".join(ln for ln in script.replace("\r\n", "\n").split("\n") if not ln.lstrip().startswith("#"))
    found = re.findall(r"(?<!\|)\|(?!\|)\s*head\b[^|\n]*|(?<!\|)\|(?!\|)\s*grep\s+-[A-Za-z]*[qm][^|\n]*|(?<!\|)\|(?!\|)\s*sed\s[^|\n]*?(?<![A-Za-z])q\b[^|\n]*", code)
    found += ["awk '%s'" % prog.strip()[:60] for prog in re.findall(r"\bawk\b[^'\n]*'([^']*)'", code) if re.search(r"\bexit\b", re.sub(r"END\s*\{[^}]*\}", "", prog))]
    return found


def function_body(text, name):
    m = re.search(r"^%s\(\) \{\n(.*?)^\}" % re.escape(name), text, re.M | re.S)
    return m.group(1) if m else None


def free_port():
    with socket.socket() as sck:
        sck.bind(("127.0.0.1", 0))
        return sck.getsockname()[1]


OLD_FORMS = ("cprev=\"$(apt-cache madison containerd.io | awk '$3 ~ /^1\\.7\\./ {print $3; exit}')\"", "dpkg --version 2>&1 | head -n1", "ps | grep -q x", "ps | grep -m1 x", "seq 1 9 | sed 1q",
             "awk '$1 == 1 {print; exit}' f")
READS_ALL = ("[ -s f ] || grep -q x f", "apt-cache madison containerd.io | awk '$3 ~ /^1\\.7\\./ && !found {print $3; found = 1}'", "dpkg --version 2>&1 | sed -n 1p", "# x | head -n1\ntrue", "awk 'END { exit 1 }' f",
             "awk '{ n++ } END { if (n) exit 0; exit 1 }' f", "grep -q x f", "grep -q x <<<\"$y\"")
check("the lint of pipelines whose consumer quits early sees the forms that ended the Debian 11 leg, and not the ones that read everything",
      all(early_quitters(s_) for s_ in OLD_FORMS) and not any(early_quitters(s_) for s_ in READS_ALL), [s_ for s_ in OLD_FORMS if not early_quitters(s_)] + [s_ for s_ in READS_ALL if early_quitters(s_)])
check("no pipeline of the guest script has a consumer that quits before its producer is done (head, grep -q, sed q, an awk that exits outside END)", not early_quitters(guest_text), early_quitters(guest_text))

if not have_bash():
    print("skip: no bash that works here, the guest script's functions are not tested")
else:
    # bash -n
    count[0] += 1
    p = subprocess.run([BASH, "-n", SCRIPT_P], capture_output=True, text=True)
    if p.returncode != 0:
        failures.append("bash -n of the guest script: %s" % p.stderr[-300:])
    else:
        ok_line("the guest script parses as bash")

    # the leg table
    for leg_, want in (("d11-12", "debian 11 12 bookworm"), ("d12-13", "debian 12 13 trixie"), ("u22-24", "ubuntu 22.04 24.04 noble"), ("u24-26", "ubuntu 24.04 26.04 resolute")):
        p = bash_run('leg_facts "$1"', leg_)
        s_ = LEGS[leg_]
        check("the guest's table says %s is %s" % (leg_, want), p.stdout.strip() == want == "%s %s %s %s" % (s_["family"], s_["old"], s_["new"], s_["new_code"]), p.stdout)
    check("the guest's table has no other leg", bash_run('leg_facts "$1"; echo "status $?"', "d13-14").stdout.strip() == "status 1")

    # do-release-upgrade -c: the status AND the version
    def offered_case(label, text, rc, version, want):
        with tempfile.TemporaryDirectory() as d_:
            put(d_, "c.txt", text)
            if rc is not None:
                put(d_, "c.rc", "%d\n" % rc)
            p_ = bash_run('offered "$1" "$2"; echo "status $?"', version, posix(os.path.join(d_, "c.txt")))
            check("offered: " + label, p_.stdout.strip() == "status %d" % (0 if want else 1), p_.stdout + p_.stderr)
    OFFER = "Checking for a new Ubuntu release\nNew release '24.04.5 LTS' available.\nRun 'do-release-upgrade' to upgrade to it.\n"
    offered_case("exit 0 and the version", OFFER, 0, "24.04", True)
    offered_case("exit 0 and another version", OFFER, 0, "26.04", False)
    offered_case("exit 1 whatever it says", OFFER, 1, "24.04", False)
    offered_case("no status recorded", OFFER, None, "24.04", False)
    offered_case("a release that is not an LTS (Prompt=normal on 26.04 after 2026-10-15)", "New release '26.10' available.\n", 0, "26.04", False)
    offered_case("the version is not a prefix match of another one (24.04 is not 24.041)", "New release '24.041 LTS' available.\n", 0, "24.04", False)
    offered_case("the dot of a version is a dot", "New release '24x04.5 LTS' available.\n", 0, "24.04", False)
    offered_case("nothing on offer", "Checking for a new Ubuntu release\nNo new release found.\n", 1, "24.04", False)
    for text_, want in (("Reading package lists...\n0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n", True), ("3 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n", False),
                        ("0 upgraded, 1 newly installed, 0 to remove and 0 not upgraded.\n", False), ("0 upgraded, 0 newly installed, 0 to remove and 2 not upgraded.\n", False), ("", False)):
        with tempfile.TemporaryDirectory() as d_:
            put(d_, "s.log", text_)
            p = bash_run('converged "$1"; echo "status $?"', posix(os.path.join(d_, "s.log")))
            check("converged: %r" % text_[-60:], p.stdout.strip() == "status %d" % (0 if want else 1), p.stdout)
    claims = bash_run('jwt_claims "$1"', "eyJhbGciOiJFUzI1NiJ9.eyJpZCI6MSwiZXhwIjoxNzkwMDAwMDAwfQ.sig").stdout
    check("the claims of a token are read, the token is not kept", json.loads(claims) == {"id": 1, "exp": 1790000000}, claims)
    check("a payload that needs padding and url-safe characters is read", json.loads(bash_run('jwt_claims "$1"', "x.eyJhIjoiPz8-Pz8ifQ.s").stdout) == {"a": "??>??"})

    # the tool that rewrites apt's sources
    tool_dir = tempfile.mkdtemp()
    tool = os.path.join(tool_dir, "sources.py")
    bash_run('write_sources_tool "$1"', posix(tool))

    def rewrite(name, text, src, dst):
        path = os.path.join(tool_dir, name)
        put(path.rsplit(os.sep, 1)[0] if False else tool_dir, name, text)
        p_ = subprocess.run([sys.executable, tool, "rewrite", src, dst, path], capture_output=True, text=True, encoding="utf-8")
        return p_.returncode, p_.stdout

    ONE = ("deb http://deb.debian.org/debian bullseye main contrib non-free\ndeb-src http://deb.debian.org/debian bullseye main\n"
           "deb [arch=amd64 signed-by=/x.gpg] http://archive.debian.org/debian-security bullseye-security main non-free\ndeb http://deb.debian.org/debian bullseye-updates main\n"
           "deb http://deb.debian.org/debian bullseye-backports main\ndeb http://deb.debian.org/debian bullseye-proposed-updates main\n# a comment about bullseye\n\n"
           "deb https://download.docker.com/linux/debian bullseye stable\ndeb http://deb.debian.org/debian bookworm main non-free-firmware\n")
    ONE_WANT = ("deb http://deb.debian.org/debian bookworm main contrib non-free non-free-firmware\ndeb-src http://deb.debian.org/debian bookworm main\n"
                "deb [arch=amd64 signed-by=/x.gpg] http://deb.debian.org/debian-security bookworm-security main non-free non-free-firmware\ndeb http://deb.debian.org/debian bookworm-updates main\n"
                "# osm-removed: deb http://deb.debian.org/debian bullseye-backports main\n# osm-removed: deb http://deb.debian.org/debian bullseye-proposed-updates main\n# a comment about bullseye\n\n"
                "deb https://download.docker.com/linux/debian bullseye stable\ndeb http://deb.debian.org/debian bookworm main non-free-firmware\n")
    NODOCKER = ONE.replace("deb https://download.docker.com/linux/debian bullseye stable\n", "")
    NODOCKER_WANT = ONE_WANT.replace("deb https://download.docker.com/linux/debian bullseye stable\n", "")
    rc_, out_ = rewrite("sources.list", ONE, "bullseye", "bookworm")
    check("the sources tool rewrites a one-line file as the notes say: the suites, the security line, the archive's host, non-free-firmware beside non-free, the backports dropped, Docker's line left alone",
          rc_ == 0 and out_ == ONE_WANT, out_)
    DEB822 = ("Types: deb deb-src\nURIs: http://deb.debian.org/debian\nSuites: bookworm bookworm-updates bookworm-backports\nComponents: main non-free\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\n"
              "Types: deb\nURIs: http://security.debian.org/debian-security\nSuites: bookworm-security\nComponents: main\n\n"
              "Types: deb\nURIs: https://download.docker.com/linux/debian\nSuites: bookworm\nComponents: stable\n\n"
              "Types: deb\nURIs: http://deb.debian.org/debian\nSuites: bookworm-backports\nComponents: main\n")
    DEB822_WANT = ("Types: deb deb-src\nURIs: http://deb.debian.org/debian\nSuites: trixie trixie-updates\nComponents: main non-free non-free-firmware\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\n"
                   "Types: deb\nURIs: http://security.debian.org/debian-security\nSuites: trixie-security\nComponents: main\n\n"
                   "Types: deb\nURIs: https://download.docker.com/linux/debian\nSuites: bookworm\nComponents: stable\n\n"
                   "# osm-removed: Types: deb\n# osm-removed: URIs: http://deb.debian.org/debian\n# osm-removed: Suites: bookworm-backports\n# osm-removed: Components: main\n")
    rc_, out_ = rewrite("debian.sources", DEB822, "bookworm", "trixie")
    check("the sources tool rewrites a deb822 file the same way (and a stanza left with no suite is commented out whole)", rc_ == 0 and out_ == DEB822_WANT, out_)
    rc_, out_ = rewrite("debian.sources", "Types: deb\nURIs: http://ftp.fr.debian.org/debian\nSuites: bookworm\nComponents: main\n", "bookworm", "trixie")
    check("a country mirror under debian.org is a Debian source", "Suites: trixie" in out_, out_)
    rc_, out_ = rewrite("sources.list", "deb http://mirror.example.org/debian bullseye main\ndeb http://mirror.example.org/pub/other bullseye main\ndeb http://deb.debian.org/debian bullseye main\n", "bullseye", "bookworm")
    check("a mirror whose path is /debian is a Debian source, another path is not", out_.splitlines()[0].endswith("bookworm main") and out_.splitlines()[1].endswith("bullseye main"), out_)
    rc_, out_ = rewrite("sources.list", "deb [ arch=amd64 ] http://deb.debian.org/debian bullseye main non-free-firmware\n", "bullseye", "bookworm")
    check("a line that already has non-free-firmware does not get it twice, and an option bracket with spaces is kept", out_ == "deb [ arch=amd64 ] http://deb.debian.org/debian bookworm main non-free-firmware\n", out_)
    rc_, out_ = rewrite("sources.list", "deb http://deb.debian.org/debian stable main\n", "bullseye", "bookworm")
    check("a suite alias is not rewritten (the design refuses it before the hop)", out_ == "deb http://deb.debian.org/debian stable main\n", out_)

    def foreign(name, text, family):
        put(tool_dir, name, text)
        return subprocess.run([sys.executable, tool, "foreign", family, os.path.join(tool_dir, name)], capture_output=True, text=True).returncode
    DOCKER_L = "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bullseye stable\n"
    check("Docker's source is foreign to Debian", foreign("docker.list", DOCKER_L, "debian") == 0)
    check("a Debian source is not foreign to Debian", foreign("sources.list", ONE, "debian") == 1)
    check("Debian's source is foreign to Ubuntu", foreign("sources.list", ONE, "debian".replace("debian", "ubuntu")) == 0)
    check("Docker's deb822 source is foreign to Ubuntu", foreign("docker.sources", "Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: noble\nComponents: stable\n", "ubuntu") == 0)
    check("an Ubuntu mirror is not foreign to Ubuntu", foreign("u.list", "deb http://archive.ubuntu.com/ubuntu jammy main\ndeb http://security.ubuntu.com/ubuntu jammy-security main\n", "ubuntu") == 1)
    check("a regional Ubuntu mirror under ubuntu.com is not foreign", foreign("u.list", "deb http://azure.archive.ubuntu.com/ubuntu jammy main\n", "ubuntu") == 1)
    check("a file with no repository is not foreign to anyone (it is left where it is)", foreign("empty.list", "# nothing\n", "debian") == 1)
    check("a repository whose address the tool cannot read (a mirror+file: list, as some cloud images have) is not called foreign: it is not paused", foreign("debian.sources", "Types: deb\nURIs: mirror+file:///etc/apt/mirrors/debian.list\nSuites: bookworm\nComponents: main\n", "debian") == 1)
    check("the tool refuses what it does not know", subprocess.run([sys.executable, tool, "nonsense"], capture_output=True).returncode == 2)

    # the probes made of bash alone, against a server of our own
    port = free_port()
    srv = os.path.join(tool_dir, "srv.py")
    put(tool_dir, "srv.py", """import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code = 200 if self.path == "/ok" else 404
        if self.path == "/auth":
            code = 200 if self.headers.get("Authorization") == "Internal s3cret" else 401
        self.send_response(code); self.end_headers(); self.wfile.write(b"x")
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
""")
    server = subprocess.Popen([sys.executable, srv, str(port)])
    banner_port = free_port()
    put(tool_dir, "ban.py", """import socket, sys, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(5)
while True:
    c, _ = s.accept(); c.sendall(b"SSH-2.0-OpenSSH_9.2\\r\\n"); time.sleep(0.2); c.close()
""")
    banner = subprocess.Popen([sys.executable, os.path.join(tool_dir, "ban.py"), str(banner_port)])
    time.sleep(1.5)
    try:
        closed = free_port()
        got = bash_run('echo "$(http_code %d /ok) $(http_code %d /nope) $(http_code %d /auth) $(http_code %d /auth "Authorization: Internal s3cret") $(http_code %d /ok)"' % (port, port, port, port, closed)).stdout.strip()
        check("http_code: the status the loopback answers with, 000 when nothing answers, and a header that is sent", got == "200 404 401 200 000", got)
        got = bash_run('tcp_open %d; a=$?; tcp_open %d; b=$?; echo "$a $b [$(tcp_banner %d)] [$(tcp_banner %d)]"' % (port, closed, banner_port, closed)).stdout.replace("\n", "")   # a banner ends with CR LF, and text mode reads CR as a line end
        check("tcp_open and tcp_banner: a port that accepts, one that does not, the banner of an ssh and the lack of one", got == "0 1 [SSH-2.0-OpenSSH_9.2] []", got)
        p = bash_run('http_code %d /ok >/dev/null' % closed)
        check("a probe of a closed port writes nothing to stderr (the poller's journal is not the place for it)", p.stderr.strip() == "", p.stderr)
        # the poller: eleven columns, the words of the guest, the names sorted
        fk = tempfile.mkdtemp()
        fake(fk, "docker", 'case "$1" in version) echo 28.0.4 ;; ps) printf "p-b\\np-a\\n" ;; esac\n')
        out_ = tempfile.mkdtemp()
        auth = tempfile.mkdtemp()
        put(auth, "jwt", "Authorization: tok\n")
        put(out_, "timeline.tsv", "")
        bash_run('export PATH="%s:$PATH"; OUT="%s" AUTH_DIR="%s" CASA_PORT=%d; poller & pp=$!; sleep 6; kill $pp 2>/dev/null; wait 2>/dev/null; true' % (posix(fk), posix(out_), posix(auth), port), timeout=60)
        rows_ = [ln.split("\t") for ln in get(out_, "timeline.tsv").splitlines() if ln]
        check("the poller writes a line of eleven columns every second or so: time, Docker, ssh, samba, gateway, core, AppManagement twice, the two ports, the names", len(rows_) >= 2 and all(len(r) == 11 for r in rows_), rows_[:2])
        check("the poller says what answered: Docker's version, the gateway's answer, the names sorted", bool(rows_) and rows_[0][1] == "28.0.4" and rows_[0][10] == "p-a,p-b" and rows_[0][4] in ("200", "404"), rows_[:1])
        shutil.rmtree(fk, ignore_errors=True)
    finally:
        server.kill()
        banner.kill()

    # ---- phases, against stand-ins ----------------------------------------------------------------------------------------------

    def world(family, ubuntu_variant="hold", extra_files=None):
        """a directory with what a phase needs: OUT, apt's directory, the backup, the update-manager's, the stand-ins on PATH, a log of every call"""
        w = tempfile.mkdtemp()
        for sub in ("out", "apt/sources.list.d", "apt/apt.conf.d", "backup", "um", "bin", "state", "auth", "distup", "aptlog"):
            os.makedirs(os.path.join(w, sub), exist_ok=True)
        put(w, "calls", "")
        put(w, "out/timeline.tsv", "")
        put(w, "out/hop-unit.log", "")
        if family == "debian":
            put(w, "apt/sources.list", NODOCKER)
            put(w, "apt/sources.list.d/docker.list", DOCKER_L)
        else:
            put(w, "apt/sources.list", "deb http://archive.ubuntu.com/ubuntu jammy main universe\ndeb http://security.ubuntu.com/ubuntu jammy-security main\n")
            put(w, "apt/sources.list.d/docker.list", DOCKER_L.replace("debian bullseye", "ubuntu jammy"))
        put(w, "um/release-upgrades", "[DEFAULT]\nPrompt=never\n")
        put(w, "boot-id", "boot-1\n")
        put(w, "os-release", os_release("Debian GNU/Linux 11 (bullseye)", "debian", "11", "bullseye") if family == "debian" else os_release("Ubuntu 22.04.5 LTS", "ubuntu", "22.04", "jammy"))
        b = os.path.join(w, "bin")
        log = '[ -n "$FAKE_LOG" ] && echo "$(basename "$0") $*" >>"$FAKE_LOG"\n'
        fake(b, "apt-get", log + r'''args="$*"
n="$FAKE_STATE"
bump() { c=$(cat "$n/$1" 2>/dev/null || echo 0); c=$((c+1)); echo "$c" >"$n/$1"; echo "$c"; }
case "$args" in
*" -s dist-upgrade"*)
    c=$(bump sim)
    if [ "$c" -le "${FAKE_NOT_CONVERGED:-0}" ]; then echo "3 upgraded, 0 newly installed, 0 to remove and 0 not upgraded."; else echo "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded."; fi ;;
*--download-only*) echo "downloaded" ;;
*" dist-upgrade"*)
    c=$(bump full)
    if [ "${FAKE_FULL_FAILS:-0}" = 1 ] && [ "$c" -eq 1 ]; then echo "E: dpkg failed"; exit 100; fi
    echo "230 upgraded, 36 newly installed, 3 to remove and 0 not upgraded." ;;
*" update"*)
    case "$args" in
    *Dir::State::lists=*)
        lists=$(printf '%s\n' "$args" | sed -E 's/.*Dir::State::lists=([^ ]+).*/\1/')
        rel="$lists/deb.debian.org_debian_dists_bookworm_InRelease"
        if [ -f "$rel" ] && grep -q 'osm-changed' "$rel" && [ "$(stat -c %Y "$rel")" = 0 ]; then
            ok=0
            for f in Suite Version Label; do
                if grep -q "^$f: osm-changed" "$rel"; then
                    lf=$(printf '%s' "$f" | tr 'A-Z' 'a-z')
                    case "$args" in *"--allow-releaseinfo-change-$lf"*|*"--allow-releaseinfo-change update"*) ok=1 ;; esac
                    [ "$ok" = 1 ] || { echo "E: Repository 'http://deb.debian.org/debian bookworm InRelease' changed its '$f' value from 'x' to 'y'"; exit 100; }
                fi
            done
        fi
        printf 'Suite: oldstable\nVersion: 12.12\nLabel: Debian\n' >"$rel"
        mkdir -p "$lists" ;;
    esac
    echo "Reading package lists..." ;;
*" -s upgrade"*) echo "120 upgraded, 4 newly installed, 0 to remove and 0 not upgraded." ;;
esac
exit 0
''')
        for name in ("dpkg", "apt-mark", "journalctl", "docker", "systemd-run", "systemd-inhibit", "systemctl", "sync"):
            fake(b, name, log + "exit 0\n")
        fake(b, "tty", log + 'echo "not a tty"; exit 1\n')
        # what the unit's sampler calls in a loop is not logged: it would be written into the log while the phase writes its own lines
        fake(b, "pgrep", "exit 1\n")
        fake(b, "ss", "exit 0\n")
        fake(b, "sleep", "exec /usr/bin/sleep 0.05\n")
        fake(b, "dpkg-query", '''if [ "$#" -le 2 ]; then printf 'bash\\t5.1\\tii \\nlibc6\\t2.36\\tii \\n'; exit 0; fi
case "$*" in
*Status-Abbrev*) for p in $FAKE_INSTALLED; do for a in "$@"; do [ "$a" = "$p" ] && { printf 'ii '; exit 0; }; done; done; exit 1 ;;
*Version*) echo "1.0"; exit 0 ;;
esac
exit 0
''')
        fake(b, "do-release-upgrade", log + r'''case "$*" in
*-c*) echo "New release '24.04.5 LTS' available."; exit 0 ;;
*)
    echo "cfg dir: $(ls "$UM_DIR/release-upgrades.d" 2>/dev/null | tr '\n' ' ')" >>"$FAKE_LOG"
    echo "cfg: $(cat "$UM_DIR"/release-upgrades.d/*.cfg 2>/dev/null | tr '\n' ' ')" >>"$FAKE_LOG"
    echo "drop-in: $([ -e "$APT_DIR/apt.conf.d/99os-upgrade-measure" ] && echo yes || echo no)" >>"$FAKE_LOG"
    echo "prompt: $(grep -h '^Prompt=' "$UM_DIR/release-upgrades")" >>"$FAKE_LOG"
    echo "the tool runs"; exit 0 ;;
esac
''')
        env = dict(OUT=posix(w) + "/out", APT_DIR=posix(w) + "/apt", BACKUP_DIR=posix(w) + "/backup", UM_DIR=posix(w) + "/um", BOOT_ID_FILE=posix(w) + "/boot-id", HOP_UNIT_LOG=posix(w) + "/out/hop-unit.log",
                   SOURCES_PY=posix(w) + "/sources.py", FAKE_LOG=posix(w) + "/calls", FAKE_STATE=posix(w) + "/state", OS_RELEASE=posix(w) + "/os-release", AUTH_DIR=posix(w) + "/auth", DPKG_LOG=posix(w) + "/dpkg.log",
                   APT_LOG_DIR=posix(w) + "/aptlog", DISTUP_LOG_DIR=posix(w) + "/distup", FAMILY=family, HOP_FROM="bullseye" if family == "debian" else "jammy",
                   TARGET_CODENAME="bookworm" if family == "debian" else "noble", TARGET_VERSION_ID="12" if family == "debian" else "24.04", UBUNTU_VARIANT=ubuntu_variant, LEG="d11-12" if family == "debian" else "u22-24",
                   FAKE_INSTALLED="docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin curl wget samba git osm-orphan osm-orphan-held", CASA_PORT="80", DOCKER_FROM="", INSTALLER_ARGS="", WORK=posix(w), SELF="/x/os-upgrade-measure.sh")
        return w, env

    def in_world(w, env, snippet, **kw):
        pre = 'export PATH="%s/bin:$PATH"; write_sources_tool "$SOURCES_PY"; ' % posix(w)
        # the guest script reads these from the environment of its caller; a stand-in for load_config is not needed
        return bash_run(pre + snippet, env=env, **kw)

    def calls(w):
        return get(w, "calls").splitlines()

    # the unit, Debian
    w, env = world("debian")
    p = in_world(w, env, "hop_unit")
    c = calls(w)
    apt_calls = [x for x in c if x.startswith("apt-get ")]
    COMMON = "-y -q -o Dpkg::Use-Pty=0 -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
    want_apt = ["update --error-on=any", "--download-only upgrade", " upgrade", "--download-only dist-upgrade", " dist-upgrade", "-s dist-upgrade"]
    got_tail = [re.sub(r"^apt-get " + re.escape(COMMON) + " ?", "", x) for x in apt_calls]
    check("the Debian unit runs apt-get update --error-on=any, the minimal upgrade (downloaded first), the full upgrade (downloaded first) and the simulation that says nothing is left, with the design's options",
          p.returncode == 0 and [g.strip() for g in got_tail] == [x.strip() for x in want_apt] and all(x.startswith("apt-get " + COMMON) for x in apt_calls), (p.stderr[-400:], got_tail))
    check("the Debian unit never passes what the design forbids", not any(bad in " ".join(c) for bad in ("--no-install-recommends", "--force-yes", "--allow-", "--force-confnew", "autoremove", "Force-LoopBreak", "force-depends", "reboot", "shutdown")), c)
    src = get(w, "apt/sources.list")
    check("the Debian unit rewrote the sources: bookworm, the security line on the mirror, the backports gone, non-free-firmware beside non-free", src == NODOCKER_WANT, src)
    check("the Debian unit paused Docker's source (renamed, not rewritten, not deleted) and kept a copy of /etc/apt", os.path.exists(os.path.join(w, "apt/sources.list.d/docker.list.casaos-paused")) and not os.path.exists(os.path.join(w, "apt/sources.list.d/docker.list"))
          and get(w, "backup/apt-orig/sources.list") == NODOCKER and "docker.list" in get(w, "out/sources-paused.txt"), os.listdir(os.path.join(w, "apt/sources.list.d")))
    ul = get(w, "out/hop-unit.log")
    words = [ln.split(" ", 2)[1:] for ln in ul.splitlines()]
    order = [x[0] + ((" " + x[1].split()[0]) if x[0].startswith("STEP") else "") for x in words]
    check("the Debian unit's log has START, the sources, each step with its end and status, and DONE, in the order of the work",
          order[:3] == ["START", "SRC_START", "SRC_END"] and order[-1] == "DONE" and [o for o in order if o == "STEP_START update"] and
          [x.split()[1] for x in [o for o in order if o.startswith("STEP_START")]] == ["update", "minimal_dl", "minimal", "full_dl", "full", "converge1"], order)
    check("every step has its output and its status in the evidence, and the unit says who it was", all(os.path.exists(os.path.join(w, "out", "step-%s.%s" % (n, e))) for n in ("update", "minimal", "full", "converge1") for e in ("log", "rc"))
          and "pid " in get(w, "out/unit-start.txt") and "pid " in get(w, "out/unit-end.txt") and get(w, "out/step-update.rc").strip() == "0", os.listdir(os.path.join(w, "out")))
    shutil.rmtree(w, ignore_errors=True)

    w, env = world("debian")
    p = in_world(w, env | {"FAKE_FULL_FAILS": "1", "FAKE_NOT_CONVERGED": "1"}, "hop_unit")
    c = calls(w)
    check("when the full upgrade fails the unit runs the notes' failure ladder once (dpkg --configure -a, apt-get -f install, dist-upgrade again)", p.returncode == 0 and any(x.startswith("dpkg --configure -a --force-confdef --force-confold") for x in c)
          and any(" -f install" in x for x in c) and get(w, "out/step-full.rc").strip() == "100" and os.path.exists(os.path.join(w, "out/step-recover_full.rc")), c)
    check("a simulation that still has things to do gets a catch-up pass and a second look, and the unit says it was needed", os.path.exists(os.path.join(w, "out/step-catchup1.log")) and os.path.exists(os.path.join(w, "out/step-converge2.log")), os.listdir(os.path.join(w, "out")))
    shutil.rmtree(w, ignore_errors=True)

    w, env = world("debian")
    put(w, "apt/sources.list", "# nothing\n")
    put(w, "apt/sources.list.d/debian.sources", "Types: deb\nURIs: mirror+file:///etc/apt/mirrors/debian.list\nSuites: bullseye\nComponents: main\n")
    p = in_world(w, env, "hop_unit")
    ul = get(w, "out/hop-unit.log")
    check("a rewrite that leaves no line of the target release stops the unit before apt is run, and says why (ENDED, not DONE)", "ABORTED no source names bookworm" in ul and ul.rstrip().endswith("ENDED 1") and "DONE" not in ul
          and not [x for x in calls(w) if x.startswith("apt-get")], (ul, calls(w)))
    shutil.rmtree(w, ignore_errors=True)

    # the unit, Ubuntu
    for variant in ("hold", "keep"):
        w, env = world("ubuntu", variant)
        p = in_world(w, env, "hop_unit")
        c = calls(w)
        up = [x for x in c if x.startswith("do-release-upgrade")]
        check("the Ubuntu unit (%s) pauses Docker's source, keeps Ubuntu's, and runs the tool once, the non-interactive view and nothing else" % variant, p.returncode == 0 and os.path.exists(os.path.join(w, "apt/sources.list.d/docker.list.casaos-paused"))
              and "archive.ubuntu.com" in get(w, "apt/sources.list") and up == ["do-release-upgrade -c", "do-release-upgrade -f DistUpgradeViewNonInteractive"], (p.stderr[-300:], up))
        check("the Ubuntu unit (%s): never --allow-third-party, and no reboot" % variant, not any(bad in " ".join(c) for bad in ("--allow-third-party", "reboot", "shutdown")), c)
        held = [x for x in c if x.startswith("apt-mark hold")]
        check("the Ubuntu unit (%s) holds %s" % (variant, "the engine and the control" if variant == "hold" else "only the control"),
              held == (["apt-mark hold osm-orphan-held docker-ce docker-ce-cli containerd.io"] if variant == "hold" else ["apt-mark hold osm-orphan-held"]), held)
        manual = [x for x in c if x.startswith("apt-mark manual")]
        check("the Ubuntu unit (%s) marks the installer's dependencies as installed by hand, the ones that are there" % variant, "apt-mark manual wget" in manual and "apt-mark manual curl" in manual and "apt-mark manual samba" in manual and not any("mergerfs" in x for x in manual), manual)
        check("the Ubuntu unit (%s): the tool saw %s, the dpkg options and Prompt=lts" % (variant, "no drop-in" if variant == "hold" else "the RemoveObsoletes drop-in"),
              ("cfg dir: 10-os-upgrade-measure.cfg" if variant == "keep" else "cfg dir: \n") in "\n".join(c) + "\n" and "drop-in: yes" in c and "prompt: Prompt=lts" in c, [x for x in c if x.startswith(("cfg", "drop", "prompt"))])
        check("the Ubuntu unit (%s): the drop-in says RemoveObsoletes=no in the keep variant, and there is none in the other" % variant,
              ("cfg: [Distro] RemoveObsoletes=no " if variant == "keep" else "cfg: \n") in "\n".join(c) + "\n", [x for x in c if x.startswith("cfg")])
        check("the Ubuntu unit (%s) leaves nothing of its own behind: no drop-in for dpkg, no cfg" % variant, not os.path.exists(os.path.join(w, "apt/apt.conf.d/99os-upgrade-measure")) and not os.path.exists(os.path.join(w, "um/release-upgrades.d/10-os-upgrade-measure.cfg")))
        ul = get(w, "out/hop-unit.log")
        check("the Ubuntu unit (%s) writes what the tool said (UPGRADER_EXIT) and DONE, and records the tool's own -c again" % variant, "UPGRADER_EXIT 0" in ul and ul.rstrip().endswith("DONE") and os.path.exists(os.path.join(w, "out/ubuntu-check-unit.rc"))
              and get(w, "out/controls.txt").startswith("variant %s" % variant), ul)
        shutil.rmtree(w, ignore_errors=True)

    # the sandbox and the experiment on releaseinfo changes
    w, env = world("debian")
    sbd = os.path.join(w, "sb")
    os.makedirs(sbd)
    before = subprocess.run([BASH, "-c", 'cd "%s/apt" && find . -type f | sort | xargs cat | sha256sum' % posix(w)], capture_output=True, text=True).stdout
    p = in_world(w, env, 'sandbox paused "%s/sb"; sandbox kept "%s/sb2"; ' % (posix(w), posix(w)) + 'sb_opts "%s/sb"; printf "%%s\\n" "${SB_OPTS[@]}" >"$OUT/sbopts"' % posix(w))
    c = calls(w)
    opts = get(w, "out/sbopts").splitlines()
    check("the sandbox's apt gets its own list of sources, its own parts, its own lists and cache, and no locking", all(any(o.startswith(k) for o in opts) for k in ("Dir::Etc::sourcelist=", "Dir::Etc::sourceparts=", "Dir::State::lists=", "Dir::Cache=", "Debug::NoLocking=true")) and any("%s/sb/sources.list" % posix(w) in o for o in opts), opts)
    check("the sandbox runs update --error-on=any, then -s upgrade and -s dist-upgrade, for the paused and for the kept sources",
          [x.split(" -o ")[-1] if " -o " in x else x for x in c if "apt-get" in x and ("update" in x or " -s " in x)][:3] == ["Debug::NoLocking=true update --error-on=any", "Debug::NoLocking=true -s upgrade", "Debug::NoLocking=true -s dist-upgrade"] and len([x for x in c if "--error-on=any" in x]) == 2, c)
    check("the paused sandbox has Debian's sources only, rewritten for the target; the kept one has Docker's as it stands",
          "docker" not in get(w, "out/sandbox-paused-sources.txt") and "bookworm" in get(w, "out/sandbox-paused-sources.txt") and "bullseye-backports" not in get(w, "out/sandbox-paused-sources.txt").replace("# osm-removed: deb http://deb.debian.org/debian bullseye-backports main", "")
          and "download.docker.com/linux/debian bullseye" in get(w, "out/sandbox-kept-sources.txt"), get(w, "out/sandbox-paused-sources.txt"))
    after = subprocess.run([BASH, "-c", 'cd "%s/apt" && find . -type f | sort | xargs cat | sha256sum' % posix(w)], capture_output=True, text=True).stdout
    check("the sandbox leaves the apt directory as it was", before == after and before != "", (before, after))
    shutil.rmtree(w, ignore_errors=True)

    w, env = world("debian")
    sb = posix(w) + "/sb"
    os.makedirs(os.path.join(w, "sb/lists/partial"))
    put(w, "sb/lists/deb.debian.org_debian_dists_bookworm_InRelease", "-----BEGIN PGP SIGNED MESSAGE-----\nSuite: oldstable\nVersion: 12.12\nLabel: Debian\n")
    p = in_world(w, env, 'allow_matrix "%s"' % sb)
    got = {n: get(w, "out/allow-%s.rc" % n).strip() for n in ("suite-plain", "suite-field", "suite-all", "version-plain", "version-field", "version-all", "label-plain", "label-field", "label-all")}
    check("the releaseinfo experiment: without the option apt refuses a changed field, with the option of that field and with the general one it does not", all(got[k + "-plain"] == "100" and got[k + "-field"] == "0" and got[k + "-all"] == "0" for k in ("suite", "version", "label")), got)
    check("the experiment asks for the option of each field by its own name", all(any(("--allow-releaseinfo-change-%s update" % f) in x for x in calls(w)) for f in ("suite", "version", "label")) and any("--allow-releaseinfo-change update" in x for x in calls(w)), calls(w)[:6])
    check("the experiment says why when there is nothing to change", "allow-skipped" not in os.listdir(os.path.join(w, "out")), os.listdir(os.path.join(w, "out")))
    shutil.rmtree(w, ignore_errors=True)
    w, env = world("debian")
    os.makedirs(os.path.join(w, "sb/lists/partial"))
    p = in_world(w, env, 'allow_matrix "%s/sb"' % posix(w))
    check("with no InRelease of the target in the sandbox the experiment says so instead of making things up", os.path.exists(os.path.join(w, "out/allow-skipped")) and not os.path.exists(os.path.join(w, "out/allow-suite-plain.rc")), os.listdir(os.path.join(w, "out")))
    shutil.rmtree(w, ignore_errors=True)

    # the hop phase: an upgrade Canonical has not opened is skipped and says so
    for label, text, rc_, started in (("offered", OFFER, 0, True), ("not offered", "No new release found.\n", 1, False), ("another release on offer", "New release '26.10' available.\n", 0, False)):
        w, env = world("ubuntu")
        put(w, "out/ubuntu-check.txt", text)
        put(w, "out/ubuntu-check.rc", "%d\n" % rc_)
        put(w, "bin/systemd-run", '[ -n "$FAKE_LOG" ] && echo "$(basename "$0") $*" >>"$FAKE_LOG"\necho "0 START" >>"$HOP_UNIT_LOG"\nexit 0\n')
        os.chmod(os.path.join(w, "bin/systemd-run"), 0o755)
        p = in_world(w, env, "phase_hop", timeout=60)
        c = calls(w)
        sr = [x for x in c if x.startswith("systemd-run")]
        tl = get(w, "out/timeline.tsv")
        if started:
            check("hop (%s): the unit is started as the design says, a transient unit that holds a block inhibitor" % label, len(sr) == 1 and all(k in sr[0] for k in ("--no-block", "--collect", "--unit=casaos-os-upgrade-measure", "--property=Type=exec", "--setenv=DEBIAN_FRONTEND=noninteractive",
                  "systemd-inhibit --what=shutdown:sleep", "--mode=block", "hop-unit")) and "hop_start" in tl and not os.path.exists(os.path.join(w, "out/hop-skipped")), (p.stderr[-300:], sr))
            check("hop (%s): the Ubuntu unit gets the environment of the research (UTF-8, ucf), and not the one that would hide whether the tool needs a screen" % label, "--setenv=LC_ALL=C.UTF-8" in sr[0] and "--setenv=UCF_FORCE_CONFFOLD=1" in sr[0] and "NO_SCREEN" not in sr[0], sr)
        else:
            check("hop (%s): the hop is skipped, neutral, and nothing is started" % label, not sr and get(w, "out/hop-skipped").strip() == "not_offered" and "hop_skipped" in tl and "hop_start" not in tl, (sr, tl[-200:]))
        shutil.rmtree(w, ignore_errors=True)
    w, env = world("debian")
    p = in_world(w, env, "phase_hop", timeout=60)
    sr = [x for x in calls(w) if x.startswith("systemd-run")]
    check("hop (Debian): the Debian unit's environment is the C locale, as the design says", p.returncode in (0, 1) and (not sr or "--setenv=LC_ALL=C " in sr[0] + " "), sr)
    shutil.rmtree(w, ignore_errors=True)

    # hop-wait: the unit is gone, what it left is collected, once
    w, env = world("debian")
    put(w, "out/hop-unit.log", "1790000000.5 START\n1790000900.5 DONE\n")
    put(w, "dpkg.log", "2026-09-21 12:00:00 status installed old 1\n2026-09-21 15:00:01 status half-configured libc6:amd64 2.36\n")
    put(w, "aptlog/history.log", "Start-Date: x\n")
    fake(os.path.join(w, "bin"), "systemctl", 'echo "systemctl $*" >>"$FAKE_LOG"\ncase "$1" in is-active) echo inactive; exit 3 ;; list-units) exit 0 ;; esac\nexit 0\n')
    fake(os.path.join(w, "bin"), "journalctl", 'echo "journalctl $*" >>"$FAKE_LOG"\nprintf "2026-09-21T12:00:03.1+00:00 h systemd[1]: Stopping OpenBSD Secure Shell server...\\n2026-09-21T12:00:03.2+00:00 h kernel: noise\\n2026-09-21T12:00:04.1+00:00 h systemd[1]: Started OpenBSD Secure Shell server.\\n"\n')
    p = in_world(w, env | {"TZ": "UTC"}, "phase_hop_wait; echo \"status $?\"", timeout=90)
    tl = get(w, "out/timeline.tsv")
    jr = get(w, "out/hop-journal.txt")
    dl = get(w, "out/dpkg-hop.log")
    need_files = ("units-hop.tsv", "dpkg-hop.tsv", "audit-hop.rc", "aptcheck-hop.rc", "failed-hop.rc", "kernel-hop.txt", "deps-hop.txt", "docker-hop.txt", "containers-hop.txt", "df-hop.txt", "os-hop", "apt-history.log")
    flags = {"status": "status 0" in p.stdout, "marks": "hop_unit_end" in tl and "hop_done" in tl, "journal": "Stopping OpenBSD" in jr and "noise" not in jr,
             "dpkg slice": "half-configured libc6" in dl and "old 1" not in dl, "files": [n for n in need_files if not os.path.exists(os.path.join(w, "out", n))]}
    check("hop-wait: when the unit has ended it collects what the unit left (the services, the packages, the health, the journal of the hop, dpkg's log from the start of the hop) and says hop_done",
          flags["status"] and flags["marks"] and flags["journal"] and flags["dpkg slice"] and not flags["files"], (flags, p.stderr[-300:], jr, dl))
    c1 = len(calls(w))
    p = in_world(w, env | {"TZ": "UTC"}, 'phase_hop_wait; echo "status $?"', timeout=60)
    check("hop-wait is safe to call again: after hop_done it does nothing", "status 0" in p.stdout and len(calls(w)) == c1, (p.stdout, len(calls(w)), c1))
    shutil.rmtree(w, ignore_errors=True)
    w, env = world("debian")
    put(w, "out/aborted", "boom\n")
    p = in_world(w, env, 'phase_hop_wait; echo "status $?"; phase_hop; echo "after hop $?"', timeout=60)
    check("an aborted run has nothing to wait for and nothing to start", "status 0" in p.stdout and not [x for x in calls(w) if x.startswith(("systemd-run", "systemctl"))], (p.stdout, calls(w)))
    shutil.rmtree(w, ignore_errors=True)
    w, env = world("debian")
    fake(os.path.join(w, "bin"), "systemctl", 'case "$1" in is-active) echo active; exit 0 ;; esac\nexit 0\n')
    p = in_world(w, env | {"HOP_LIMIT": "1"}, 'phase_hop_wait; echo "status $?"', timeout=60)
    check("hop-wait gives up at its limit with status 3 and says so, and the unit is not touched", "status 3" in p.stdout and "hop_timeout" in get(w, "out/timeline.tsv"), p.stdout)
    shutil.rmtree(w, ignore_errors=True)

    # the reboot, the inhibitor, a hop that was skipped
    w, env = world("debian")
    p = in_world(w, env, '(phase_reboot pre); echo "status $?"')
    c = calls(w)
    check("reboot pre: the guest asks for its own reboot with a timer, so that the ssh session ends first, and says which boot it was", "status 0" in p.stdout and any(x.startswith("systemd-run") and "--on-active=3" in x and "systemctl reboot" in x for x in c)
          and "boot_id boot-1" in get(w, "out/reboot-pre.txt") and "method timer" in get(w, "out/reboot-pre.txt"), (p.stdout, c))
    put(w, "out/hop-skipped", "not_offered\n")
    p = in_world(w, env, '(phase_reboot post); echo "status $?"')
    check("reboot post: status 7 when the hop was skipped, and nothing is rebooted", "status 7" in p.stdout and not [x for x in calls(w) if "--unit=osm-reboot-post" in x], (p.stdout, calls(w)))
    p = in_world(w, env, '(phase_after); echo "status $?"')
    check("after: nothing to look at when the hop was skipped", "status 0" in p.stdout and "after_skipped" in get(w, "out/timeline.tsv") and not [x for x in calls(w) if x.startswith(("systemctl", "docker"))], (p.stdout, calls(w)))
    shutil.rmtree(w, ignore_errors=True)
    for variant, flag in (("reboot", False), ("force", True)):
        w, env = world("debian")
        put(w, "bin/systemd-inhibit", '[ -n "$FAKE_LOG" ] && echo "systemd-inhibit $*" >>"$FAKE_LOG"\necho "Operation inhibited by test"; exit 1\n')
        os.chmod(os.path.join(w, "bin/systemd-inhibit"), 0o755)
        p = in_world(w, env, '(phase_inhibit %s); echo "status $?"' % variant, timeout=90)
        c = [x for x in calls(w) if x.startswith("systemd-inhibit")]
        txt = get(w, "out/inhibit-%s.txt" % variant)
        check("inhibit %s: the attempt is written before it is made, root's reboot is run under a block lock on shutdown%s, and what happened is written after" % (variant, " with --force" if flag else ""),
              len(c) == 1 and "--what=shutdown --mode=block" in c[0] and c[0].rstrip().endswith("systemctl reboot --force" if flag else "systemctl reboot") and txt.startswith("variant %s\n" % variant) and "rc 1\n" in txt and txt.rstrip().endswith("survived yes")
              and "Operation inhibited" in get(w, "out/inhibit-%s.out" % variant), (c, txt))
        shutil.rmtree(w, ignore_errors=True)
    w, env = world("debian")
    p = in_world(w, env, '(phase_inhibit nonsense) 2>&1; echo "status $?"')
    check("inhibit: an unknown variant is refused", "status 2" in p.stdout, p.stdout + p.stderr)
    shutil.rmtree(w, ignore_errors=True)

    # what the guest refuses to start, and how it says so
    def run_script(*args, env=None):
        e = dict(ENV)
        e.pop("MEASURE_DISPOSABLE", None)
        e.update(env or {})
        return subprocess.run([BASH, SCRIPT_P] + list(args), capture_output=True, text=True, env=e, encoding="utf-8")
    p = run_script("prepare", env={"LEG": "d11-12"})
    check("prepare refuses a machine that is not marked disposable", p.returncode == 2 and "disposable machine" in p.stderr, (p.returncode, p.stderr))
    p = run_script("prepare", env={"LEG": "d99-100", "MEASURE_DISPOSABLE": "1"})
    check("prepare refuses a leg that is none of the four (after the check that it is root, which this test is not)", p.returncode == 2, (p.returncode, p.stderr))
    p = run_script("sideways")
    check("the script refuses a phase it does not have", p.returncode == 2 and "usage" in p.stderr, (p.returncode, p.stderr))
    p = run_script("after", env={"OUT": posix(tempfile.mkdtemp())})
    check("a phase after prepare refuses to start without what prepare keeps", p.returncode == 2 and "config.env" in p.stderr, (p.returncode, p.stderr))

    # lints of the text of the guest script
    after_body = function_body(guest_text, "phase_after") or ""
    check("the after phase never starts, restarts, reloads, enables, stops or kills a unit: ReCasaOS has to come back by itself",
          bool(after_body) and not re.search(r"\bsystemctl\b[^\n]*\b(start|restart|reload|enable|stop|kill|reset-failed|try-restart|isolate)\b", after_body), re.findall(r"[^\n]*systemctl[^\n]*", after_body))
    baseline_body = function_body(guest_text, "phase_baseline") or ""
    check("the baseline starts again the two witnesses whose policy does not start them after the reboots that came before, and says which", "docker start" in baseline_body and "baseline-started" in baseline_body and "p-no p-on-failure" in baseline_body)
    check("the container on the host network is served a file: its port answers 200", "index.html" in (function_body(guest_text, "start_things") or ""))
    check("the after phase reads the units before it asks anything", after_body.find("units_dump boot") != -1 and after_body.find("units_dump boot") < after_body.find("http_code") and after_body.find("units_dump boot") < after_body.find("wait_docker"))
    unit_bodies = "\n".join(function_body(guest_text, n) or "" for n in ("hop_unit_debian", "hop_unit_ubuntu", "hop_unit"))
    check("the unit never reboots the box (the harness does)", unit_bodies != "" and not re.search(r"\b(reboot|shutdown|poweroff|halt)\b", re.sub(r"#[^\n]*", "", unit_bodies)), re.findall(r"[^\n]*reboot[^\n]*", unit_bodies))
    unit_code = re.sub(r"#[^\n]*", "", unit_bodies)
    check("the unit uses none of the escape hatches of the notes and none of the tool's dangerous options", unit_bodies != "" and not re.search(r"--no-install-recommends|--force-yes|--allow-|--force-confnew|Force-LoopBreak|force-depends|DEBIAN_PRIORITY|autoremove|\bpurge\b|do-release-upgrade (-d|-p)|third-party", unit_code), re.findall(r"[^\n]*(?:allow|autoremove|purge)[^\n]*", unit_code))
    phase_hop_body = function_body(guest_text, "phase_hop") or ""
    check("the hop is a transient unit with --no-block --collect and the design's name, behind systemd-inhibit --what=shutdown:sleep --mode=block", all(k in phase_hop_body for k in ("systemd-run", "--no-block", "--collect", '--unit="${UNIT}"', "--property=Type=exec", "systemd-inhibit --what=shutdown:sleep", "--mode=block")) and "UNIT=casaos-os-upgrade-measure" in guest_text)
    check("the unit gets DEBIAN_FRONTEND=noninteractive, APT_LISTCHANGES_FRONTEND=none, NEEDRESTART_SUSPEND=1 and a clean PATH, and no LD_LIBRARY_PATH", all(k in phase_hop_body for k in ("--setenv=DEBIAN_FRONTEND=noninteractive", "--setenv=APT_LISTCHANGES_FRONTEND=none", "--setenv=NEEDRESTART_SUSPEND=1", "--setenv=PATH=/usr/sbin:/usr/bin:/sbin:/bin")) and "LD_LIBRARY_PATH" not in guest_text)
    check("the poller leans on bash and on Docker's CLI only: no curl, no date, no awk in it", "curl" not in (function_body(guest_text, "poller") or "x curl") and "$(date" not in (function_body(guest_text, "poller") or "") and "awk" not in (function_body(guest_text, "poller") or ""))
    leaks = [m for m in re.findall(r'OUT\}/(jwt[^"\s]*)', guest_text) if not re.match(r"^(jwt-claims-|jwt-before\.code|jwt-after\.code|jwt-fresh\.code)", m)]
    check("a token and the password never go into the evidence directory (only the claims of a token, and the codes the API answered)", not leaks and not re.search(r'>\s*"\$\{OUT\}/[^"]*(pw|password|secret)', guest_text), leaks)
    check("the guest script has no ${{ }} and no secret of the runner", "${{" not in guest_text)

    # the runner's side, against a stand-in for ssh
    def runner_world():
        r = tempfile.mkdtemp()
        os.makedirs(os.path.join(r, "bin"))
        os.makedirs(os.path.join(r, "state"))
        put(r, "calls", "")
        put(r, "state/boot", "boot-A\n")
        fake(os.path.join(r, "bin"), "sleep", "exit 0\n")
        fake(os.path.join(r, "bin"), "ssh", r'''S="$FAKE_STATE"
cmd="${@: -1}"
echo "ssh $cmd" >>"$FAKE_LOG"
case "$cmd" in
"cat /proc/sys/kernel/random/boot_id")
    if [ -f "$S/down" ]; then
        n=$(cat "$S/down"); if [ "$n" -gt 0 ]; then echo $((n-1)) >"$S/down"; exit 255; fi
        rm -f "$S/down"; echo "boot-B" >"$S/boot"
    fi
    cat "$S/boot"; exit 0 ;;
*"os-upgrade-measure.sh reboot "*) [ "$FAKE_SKIP" = 1 ] && exit 7; echo 3 >"$S/down"; exit 0 ;;
*"os-upgrade-measure.sh inhibit "*) case "$FAKE_INHIBIT" in down) echo 3 >"$S/down"; exit 255 ;; *) exit 0 ;; esac ;;
*"os-upgrade-measure.sh hop-wait"*)
    n=$(cat "$S/waits" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" >"$S/waits"
    [ "$n" -le "${FAKE_WAIT_CUTS:-0}" ] && exit 255
    [ "${FAKE_WAIT_RC:-0}" != 0 ] && exit "$FAKE_WAIT_RC"
    exit 0 ;;
*) exit 0 ;;
esac
''')
        return r, dict(FAKE_LOG=posix(r) + "/calls", FAKE_STATE=posix(r) + "/state")

    def runner(snippet, extra=None):
        r, e = runner_world()
        e.update(extra or {})
        p = bash_run('export PATH="%s/bin:$PATH"; ' % posix(r) + snippet, env=e, cwd=posix(r) if False else None, timeout=60)
        return r, p

    r, p = runner('runner_reboot pre; echo "status $?"')
    check("runner: the reboot is asked for, and the runner waits for another boot id (the guest does not answer for a while)", "status 0" in p.stdout and any("reboot pre" in x for x in get(r, "calls").splitlines()) and get(r, "state/boot").strip() == "boot-B", (p.stdout, p.stderr[-200:]))
    r, p = runner('runner_reboot post; echo "status $?"', {"FAKE_SKIP": "1"})
    check("runner: a guest that says the hop was skipped (status 7) is not waited for", "status 0" in p.stdout and "no reboot" in p.stdout and get(r, "state/boot").strip() == "boot-A", p.stdout)
    r, p = runner('runner_inhibit reboot; echo "status $?"', {"FAKE_INHIBIT": "down"})
    check("runner: a guest that went down under the inhibitor (ssh ends with 255) is waited for", "status 0" in p.stdout and get(r, "state/boot").strip() == "boot-B", p.stdout)
    r, p = runner('runner_inhibit force; echo "status $?"', {"FAKE_INHIBIT": "stays"})
    check("runner: a guest that stayed up under the inhibitor is not waited for", "status 0" in p.stdout and get(r, "state/boot").strip() == "boot-A", p.stdout)
    r, p = runner('runner_hop; echo "status $?"', {"FAKE_WAIT_CUTS": "2"})
    check("runner: the hop is started once, and the wait is made again when the connection is cut (sshd restarts while libc6 is configured)", "status 0" in p.stdout and len([x for x in get(r, "calls").splitlines() if "os-upgrade-measure.sh hop-wait" in x]) == 3
          and len([x for x in get(r, "calls").splitlines() if x.endswith("os-upgrade-measure.sh hop")]) == 1, (p.stdout, get(r, "calls")))
    r, p = runner('runner_hop; echo "status $?"', {"FAKE_WAIT_RC": "3"})
    check("runner: the guest's own limit (status 3) ends the wait and is the status of the step", "status 3" in p.stdout, p.stdout)
    r, p = runner('runner_guest after; echo "status $?"')
    check("runner: a phase is run as root with the evidence directory, and nothing else of the runner's environment", any(x == "ssh sudo env OUT=/home/casa/osm-out bash os-upgrade-measure.sh after" for x in get(r, "calls").splitlines()), get(r, "calls"))

# ---- the workflow ---------------------------------------------------------------------------------------------------------------------------

def workflow_text():
    with open(WORKFLOW, newline="") as f:
        return f.read().replace("\r\n", "\n")


def workflow_matrix(text, after=None):
    """the cells of the matrix, [dict]: the plain reading of its `include:` list (a cell starts at a `- `, values lose their quotes);
    after: the line of the job whose matrix it is, when the file has several"""
    lines = text.split("\n")
    first = next(i for i, ln in enumerate(lines) if after is None or ln.rstrip() == after)
    start = next(i for i in range(first, len(lines)) if lines[i].strip() == "include:")
    base = len(lines[start]) - len(lines[start].lstrip())
    cells = []
    for ln in lines[start + 1:]:
        if not ln.strip() or ln.strip().startswith("#"):
            continue
        if len(ln) - len(ln.lstrip()) <= base:
            break
        m = re.match(r"^\s*(- )?(\w+): (.*)$", ln)
        if m:
            if m.group(1):
                cells.append({})
            cells[-1][m.group(2)] = m.group(3).strip().strip("'\"")
    return cells


def workflow_runs(text):
    """{step name: the script of its `run: |` block, without the indentation of the YAML}"""
    lines = text.split("\n")
    runs, name, i = {}, None, 0
    while i < len(lines):
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
            runs[name] = "\n".join(b[indent:] if b.strip() else "" for b in block).rstrip("\n") + "\n"
            continue
        i += 1
    return runs


wf = workflow_text()
wf_cells = workflow_matrix(wf)
wf_runs = workflow_runs(wf)
STEPS = ["The measurement can say no", "Fetch the published installer", "The distribution's cloud image", "Boot it", "Prepare the old release", "Root's reboot under an inhibitor", "Root's forced reboot under an inhibitor",
         "Reboot before the hop", "Baseline and read-only probes", "The hop", "Reboot after the hop", "After the reboot", "Report", "What the machine says when it is not well"]
check("the workflow has the steps of a leg in the order of the work", list(wf_runs) == STEPS, list(wf_runs))
check("the workflow asks for no permission but to read the repository", re.search(r"^permissions:\n  contents: read\n", wf, re.M) is not None and ": write" not in wf)
check("the workflow is dispatched by hand only, never by a push", re.search(r"^on:\n  workflow_dispatch:", wf, re.M) is not None and "push:" not in wf and "schedule:" not in wf and "pull_request" not in wf)
check("no run block has a ${{ }} in it: what the matrix and the inputs say reaches a script through env", all("${{" not in script for script in wf_runs.values()), [n for n, s_ in wf_runs.items() if "${{" in s_])
check("no run block of the workflow has a pipeline whose consumer quits before its producer is done", all(not early_quitters(s_) for s_ in wf_runs.values()), {n: early_quitters(s_) for n, s_ in wf_runs.items() if early_quitters(s_)})
if have_bash():
    for step, script in sorted(wf_runs.items()):
        count[0] += 1
        p = subprocess.run([BASH, "-n"], input=script, capture_output=True, text=True)
        if p.returncode != 0:
            failures.append("the run block of %r does not parse as bash: %s" % (step, p.stderr[-300:]))
        else:
            ok_line("the run block of %r parses as bash" % step)
check("the matrix is four legs: Debian 11 to 12, Debian 12 to 13, Ubuntu 22.04 to 24.04, Ubuntu 24.04 to 26.04", [(c.get("leg"), c.get("id"), c.get("version")) for c in wf_cells] == [("d11-12", "debian", "11"), ("d12-13", "debian", "12"), ("u22-24", "ubuntu", "22.04"), ("u24-26", "ubuntu", "24.04")], wf_cells)
check("every leg has 120 minutes and the others of its job go on when one fails", re.search(r"^    timeout-minutes: 120$", wf, re.M) is not None and "fail-fast: false" in wf)
by_id = {"debian": ("SHA512SUMS", "sha512sum"), "ubuntu": ("SHA256SUMS", "sha256sum")}
for cell in wf_cells:
    where = "%s on %s %s" % (cell.get("leg"), cell.get("id"), cell.get("version"))
    check("the %s cell fetches the image it names, with the sums its distribution publishes" % where,
          cell.get("id") in by_id and (cell.get("sums"), cell.get("algo")) == by_id[cell["id"]] and "%s-%s-" % (cell["id"], cell["version"]) in cell.get("image", "") and cell.get("base", "").startswith("https://"), cell)
    if have_bash():
        facts_ = bash_run('leg_facts "$1"', cell.get("leg", "")).stdout.split()
        check("the %s cell is the leg the guest script means by that name" % where, facts_[:2] == [cell.get("id"), cell.get("version")], facts_)
with open(os.path.join(HERE, "..", ".github", "workflows", "install-check.yml"), newline="") as f:
    systems = workflow_matrix(f.read().replace("\r\n", "\n"), after="  vm:")
for cell in wf_cells:
    twin = next((c for c in systems if (c.get("id"), c.get("version")) == (cell.get("id"), cell.get("version"))), None)
    if twin is not None:
        check("the image and the sums of %s %s are the ones install-check.yml boots" % (cell.get("id"), cell.get("version")), all(cell.get(k) == twin.get(k) for k in ("base", "image", "sums", "algo")), (cell, twin))
check("the d11-12 cell is the owner's box: the Debian archive for its security line, and Docker 28.0.4", [c for c in wf_cells if c.get("leg") == "d11-12"][0].get("args") == "--use-debian-archive" and [c for c in wf_cells if c.get("leg") == "d11-12"][0].get("docker_from") == "28.0.4"
      and not any(c.get("args") or c.get("docker_from") for c in wf_cells if c.get("leg") != "d11-12"))
artifact = re.search(r"^\s+name: (os-upgrade-measure-.+)$", wf, re.M)
uploads = [re.sub(r"\$\{\{ matrix\.(\w+) \}\}", lambda m, c=c: c.get(m.group(1), ""), artifact.group(1)) for c in wf_cells] if artifact else []
check("each cell uploads its evidence under a name of its own, whatever happened", bool(artifact) and len(set(uploads)) == len(wf_cells) and re.search(r"upload-artifact@v4\n        if: always\(\)", wf) is not None, uploads)
check("the report and the machine's complaints run whatever happened, and the report is the one that fails the job", re.search(r"- name: Report\n        if: always\(\)", wf) is not None and re.search(r"- name: What the machine says when it is not well\n        if: always\(\)", wf) is not None)
check("the guest's disk goes to the larger of the runner's disks, df is printed first, and the guest's disk is 30 GB (sparse)", "df -h . /mnt" in wf_runs["The distribution's cloud image"] and "30G" in wf_runs["Boot it"] and 'DISK_DIR=' in wf_runs["The distribution's cloud image"])
check("the ubuntu variant is a choice of two and reaches the guest through env", "type: choice" in wf and "- hold" in wf and "- keep" in wf and "UBUNTU_VARIANT: ${{ inputs.ubuntu_variant }}" in wf and "UBUNTU_VARIANT='${UBUNTU_VARIANT}'" in wf_runs["Prepare the old release"])
check("the steps that reboot the guest or wait for the unit have the time they need, and the hop has the most", "timeout-minutes: 90" in wf and wf.count("timeout-minutes: ") >= 8)
check("the guest is told it is disposable, and which leg it is", "MEASURE_DISPOSABLE=1" in wf_runs["Prepare the old release"] and "LEG='${LEG}'" in wf_runs["Prepare the old release"])
check("the guest script and the installer are copied to the guest after it booted, and the installer's checksum is verified", "scp -F ssh_config published/install.sh scripts/os-upgrade-measure.sh vm:" in wf_runs["Boot it"] and "sha256sum --check install.sh.sha256" in wf_runs["Fetch the published installer"])
check("the run steps use the runner's functions of the guest script", all(("source scripts/os-upgrade-measure.sh" in wf_runs[n]) for n in ("Root's reboot under an inhibitor", "Root's forced reboot under an inhibitor", "Reboot before the hop", "Baseline and read-only probes", "The hop", "Reboot after the hop", "After the reboot")))
CALLS = {"Root's reboot under an inhibitor": "runner_inhibit reboot", "Root's forced reboot under an inhibitor": "runner_inhibit force", "Reboot before the hop": "runner_reboot pre",
         "Baseline and read-only probes": "runner_guest baseline\nrunner_guest probe", "The hop": "runner_hop", "Reboot after the hop": "runner_reboot post", "After the reboot": "runner_guest after"}
check("each step that drives the guest makes the call of its name, and nothing else", all(wf_runs[n].rstrip("\n").endswith(call) and wf_runs[n].count("runner_") == call.count("runner_") for n, call in CALLS.items()), {n: wf_runs[n] for n, c_ in CALLS.items() if not wf_runs[n].rstrip("\n").endswith(c_)})
check("the guest steps come in the order: inhibitor, inhibitor with --force, reboot, baseline and probes, hop, reboot, after", [n for n in wf_runs if n in STEPS[5:12]] == STEPS[5:12])
check("NOT_OFFERED stays green and says so with a warning annotation", "::warning title=NOT_OFFERED" in wf_runs["Report"])

if have_bash():
    # the report step, against a stand-in for ssh that hands over the evidence of a leg
    def report_step(leg_, spec_, mutate_=None):
        w_ = tempfile.mkdtemp()
        fx = os.path.join(w_, "fx")
        os.makedirs(fx)
        build(fx, LEGS[leg_], spec_)
        if mutate_:
            mutate_(fx)
        os.makedirs(os.path.join(w_, "bin"))
        os.makedirs(os.path.join(w_, "repo/scripts"))
        shutil.copy(REPORT, os.path.join(w_, "repo/scripts/os-upgrade-measure-report.py"))
        fake(os.path.join(w_, "bin"), "ssh", 'case "${@: -1}" in *"tar -C"*) tar -C "$FX" -cf - . ;; *) exit 0 ;; esac\n')
        script = wf_runs["Report"]
        e = dict(ENV, FX=posix(fx), LEG=leg_, GITHUB_STEP_SUMMARY=posix(os.path.join(w_, "summary")), PATH=os.environ["PATH"])
        p_ = subprocess.run([BASH, "-c", 'export PATH="%s/bin:$PATH"; cd "%s/repo"; ' % (posix(w_), posix(w_)) + script], capture_output=True, text=True, env=e, encoding="utf-8")
        summary = get(w_, "summary") if os.path.exists(os.path.join(w_, "summary")) else ""
        shutil.rmtree(w_, ignore_errors=True)
        return p_, summary
    p, summary = report_step("d11-12", {})
    check("the report step: a measured leg is green, its report is in the summary", p.returncode == 0 and "MEASURED" in summary and "::warning" not in p.stdout, (p.returncode, p.stdout[-300:], p.stderr[-300:]))
    p, summary = report_step("u22-24", {"offered": False})
    check("the report step: NOT_OFFERED is green and has its annotation", p.returncode == 0 and "::warning title=NOT_OFFERED" in p.stdout and "NOT_OFFERED" in summary, (p.returncode, p.stdout[-300:]))
    p, summary = report_step("d11-12", {}, lambda d_: put(d_, "aborted", "boom\n"))
    check("the report step: an invalid run is red and says so in the summary", p.returncode != 0 and "INVALID RUN" in summary, (p.returncode, summary[:200]))
    p, summary = report_step("d11-12", {}, lambda d_: os.remove(os.path.join(d_, "timeline.tsv")))
    check("the report step: no timeline is red, with the reason in the summary", p.returncode != 0 and "the run left no evidence" in summary, (p.returncode, summary[:200]))

try:
    import yaml
except ImportError:
    print("skip: PyYAML is not here, the workflow was read by the plain parser only")
else:
    doc = yaml.safe_load(wf)
    steps = doc["jobs"]["vm"]["steps"]
    check("PyYAML reads the workflow, with the matrix and the run blocks the plain parser found",
          doc.get("permissions") == {"contents": "read"} and set(doc[True]) == {"workflow_dispatch"} and [{k: str(v) for k, v in c.items()} for c in doc["jobs"]["vm"]["strategy"]["matrix"]["include"]] == wf_cells
          and {s_["name"]: s_["run"] for s_ in steps if "run" in s_} == wf_runs, "the two readings differ")
    check("PyYAML: four legs, 120 minutes, the choice of the variant", len(doc["jobs"]["vm"]["strategy"]["matrix"]["include"]) == 4 and doc["jobs"]["vm"]["timeout-minutes"] == 120
          and doc[True]["workflow_dispatch"]["inputs"]["ubuntu_variant"]["options"] == ["hold", "keep"] and doc[True]["workflow_dispatch"]["inputs"]["ubuntu_variant"]["default"] == "hold")
    check("PyYAML: the report, the machine's complaints and the upload run when a step failed", all(s_.get("if") == "always()" for s_ in steps if s_.get("name") in ("Report", "What the machine says when it is not well") or "upload-artifact" in str(s_.get("uses", ""))))

# ---- the end ----------------------------------------------------------------------------------------------------------------------------

finish_tests()
