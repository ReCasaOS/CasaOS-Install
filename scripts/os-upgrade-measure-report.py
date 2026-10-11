#!/usr/bin/env python3
"""Turn what os-upgrade-measure.sh wrote into facts and the go/no-go table of the design, or say the run is invalid.

    os-upgrade-measure-report.py <out dir> <d11-12|d12-13|u22-24|u24-26>

Prints a markdown report (and writes it to <out dir>/report.md).

Exit status: 0 for a run that was measured, whatever the numbers say (a hop that fails is a finding, not a broken harness), and
for an Ubuntu hop that Canonical has not opened (NOT_OFFERED: neutral, visible, and never a pass); 1 for an INVALID RUN (the
harness could not do what it set out to: a phase did not run, the evidence is missing or contradicts itself, the box was not what
the leg needs, the poller left holes). An invalid run proves nothing and says no GO: a step that did not run is INVALID, not a
quiet pass.

The answers of the go/no-go table (os-upgrade-design.md, section 8) are GO, NO-GO, OPEN (what was measured cannot decide it, and
the row says why) and NOT_OFFERED. Nothing is judged in the guest: it writes what it saw, and this file decides.

The legs, each one hop from a cloud image of the OLD release with Docker and ReCasaOS installed:
    d11-12  Debian 11 (the archive's security line, Docker 28.0.4) to 12      apt 2.2.4 in the sandbox against bookworm's InRelease
    d12-13  Debian 12 to 13                                                    apt 2.6.1 against trixie's
    u22-24  Ubuntu 22.04 to 24.04, do-release-upgrade -f DistUpgradeViewNonInteractive, guarded by `do-release-upgrade -c`
    u24-26  Ubuntu 24.04 to 26.04, the same
"""
import difflib
import json
import os
import re
import sys
from datetime import datetime

out_dir, leg = sys.argv[1], sys.argv[2]

# what each leg is, from its own table: the report does not take that from the guest
LEGS = {
    "d11-12": dict(family="debian", old="11", new="12", old_code="bullseye", new_code="bookworm", apt="2.2.4"),
    "d12-13": dict(family="debian", old="12", new="13", old_code="bookworm", new_code="trixie", apt="2.6.1"),
    "u22-24": dict(family="ubuntu", old="22.04", new="24.04", old_code="jammy", new_code="noble"),
    "u24-26": dict(family="ubuntu", old="24.04", new="26.04", old_code="noble", new_code="resolute"),
}
if leg not in LEGS:
    print("usage: os-upgrade-measure-report.py <out dir> d11-12|d12-13|u22-24|u24-26", file=sys.stderr)
    sys.exit(2)
L = LEGS[leg]
DEBIAN = L["family"] == "debian"

# what the guest starts, with the restart policy it gives each
CONTAINERS = {"p-always": "always", "p-unless-stopped": "unless-stopped", "p-no": "no", "p-on-failure": "on-failure",
              "p-db": "unless-stopped", "p-web": "unless-stopped", "p-host": "unless-stopped"}
COMES_BACK = [n for n, p in CONTAINERS.items() if p in ("always", "unless-stopped")]
ENGINE = ["docker-ce", "docker-ce-cli", "containerd.io", "docker-ce-rootless-extras", "docker-buildx-plugin", "docker-compose-plugin", "docker-model-plugin"]
ENGINE_CORE = ["docker-ce", "docker-ce-cli", "containerd.io"]
# the names whose removal the design's guard forbids (os-upgrade-design.md, section 6, "Garde-fou de retrait"), as far as the legs install them
PROTECTED = ENGINE + ["docker.io"]
CASA_UNITS_RE = re.compile(r"^casaos[A-Za-z0-9@._-]*\.service$")
RESTART_WATCH = {"ssh.service": "ssh", "sshd.service": "ssh", "smbd.service": "samba", "nmbd.service": "samba", "docker.service": "docker",
                 "containerd.service": "containerd", "cron.service": "cron", "dbus.service": "dbus", "systemd-logind.service": "logind"}
HOLE = 60.0       # seconds without a sample of the poller, outside a reboot: the poller is dead or the box is not answering at all
REBOOT_GRACE = 120.0   # a gap that begins within this long of a request to reboot is the box going down
CURL_NONE = "000"

lines = []
P = lines.append
problems = []
rows_out = []      # (question, answer, evidence): the go/no-go table
facts = []         # (what, value): the other measurements
undecided = []     # what could not be decided, said plainly


def read(name, default=""):
    try:
        with open(os.path.join(out_dir, name), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def has(name):
    return os.path.exists(os.path.join(out_dir, name))


def invalid(reason):
    if reason not in problems:
        problems.append(reason)


def need(name, why):
    """evidence that has to be there: a missing or empty file is an invalid run, with the step that was to write it"""
    if not read(name).strip():
        invalid("%s is missing or empty: %s" % (name, why))
        return False
    return True


def rc_of(name):
    s = read(name + ".rc").strip()
    return int(s) if s.isdigit() else None


def need_rc(name, why):
    if rc_of(name) is None:
        invalid("%s.rc is missing: %s" % (name, why))
        return None
    return rc_of(name)


def answer(question, ans, evidence):
    rows_out.append((question, ans, evidence))


def fact(what, value):
    facts.append((what, value))


def open_q(why):
    undecided.append(why)


def kv(name):
    """'key value' lines -> {key: value}"""
    out = {}
    for ln in read(name).splitlines():
        k, _, v = ln.partition(" ")
        if k:
            out[k] = v.strip()
    return out


def first_error(text):
    for ln in text.splitlines():
        if ln.startswith(("E:", "Err:")) or "No space left" in ln:
            return ln.strip()[:200]
    return ""


def fmt_s(x):
    return "n/a" if x is None else "%.0f s" % x


def fmt_bytes(b):
    if b is None:
        return "n/a"
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if abs(b) >= size:
            return "%.1f %s" % (b / size, unit)
    return "%d B" % b


def to_bytes(num, unit):
    mult = {"B": 1, "kB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}[unit]
    return int(float(num.replace(",", "")) * mult)


# ---- the timeline ----------------------------------------------------------------------------------------

markers = {}      # name -> [(time, rest of the line)]
rows = []         # one dict per sample of the poller
for ln in read("timeline.tsv").splitlines():
    f = ln.split("\t")
    try:
        if f[0] == "#" and len(f) >= 3:
            name, _, rest = f[2].partition(" ")
            markers.setdefault(name, []).append((float(f[1]), rest))
        elif len(f) == 11:
            rows.append(dict(t=float(f[0]), docker=f[1], ssh=f[2], smb=f[3], gw=f[4], core=f[5], am=f[6], ami=f[7], web=f[8], host=f[9],
                             names=set(f[10].split(",")) if f[10] != "-" else set()))
    except ValueError:
        pass
rows.sort(key=lambda r: r["t"])


def mt(name, nth=0):
    m = markers.get(name, [])
    return m[nth][0] if len(m) > nth else None


def span(a, b):
    ta, tb = mt(a), mt(b)
    return None if ta is None or tb is None else tb - ta


def window(a, b):
    ta, tb = mt(a), mt(b)
    if ta is None or tb is None:
        return []
    return [r for r in rows if ta <= r["t"] <= tb]


def episodes(win, is_down, need_down=2, need_up=3):
    """runs of at least need_down consecutive down samples (or a run that never ends), each ended by the first of need_up consecutive
    up samples; [(start, end or None)]"""
    eps, i, n = [], 0, len(win)
    while i < n:
        if is_down(win[i]):
            j = i
            while j < n and is_down(win[j]):
                j += 1
            if j - i >= need_down or j == n:
                k, end = j, None
                while k < n:
                    if len(win[k:k + need_up]) == need_up and all(not is_down(w) for w in win[k:k + need_up]):
                        end = win[k]["t"]
                        break
                    k += 1
                eps.append((win[i]["t"], end))
                i = next((idx for idx in range(j, n) if win[idx]["t"] >= (end if end is not None else 1e18)), n)
                continue
            i = j
        else:
            i += 1
    return eps


def total_down(eps, until):
    return sum(((e if e is not None else until) - s) for s, e in eps)


def am_up(r):
    return r["am"] == "200" or r["ami"] == "200"


def apt_parse(text):
    """what apt prints of a transaction (LC_ALL=C): the summary line, the sizes, the lists, the lines of a simulation"""
    d = {"counts": None, "need": None, "disk": None, "lists": {}, "inst": [], "remv": [], "purg": []}
    m = re.search(r"^(\d+) upgraded, (\d+) newly installed, (\d+) to remove and (\d+) not upgraded\.$", text, re.M)
    if m:
        d["counts"] = tuple(int(x) for x in m.groups())
    m = re.search(r"^Need to get ([0-9.,]+) ([kMGT]?B)(?:/([0-9.,]+) ([kMGT]?B))? of archives\.$", text, re.M)
    if m:
        d["need"] = to_bytes(m.group(3), m.group(4)) if m.group(3) else to_bytes(m.group(1), m.group(2))
    m = re.search(r"^After this operation, ([0-9.,]+) ([kMGT]?B) of (additional disk space will be used|disk space will be freed)\.$", text, re.M)
    if m:
        d["disk"] = to_bytes(m.group(1), m.group(2)) * (1 if m.group(3).startswith("additional") else -1)
    cur = None
    for ln in text.splitlines():
        h = re.match(r"^The following (.*?):$", ln)
        if h:
            cur = h.group(1)
            d["lists"][cur] = []
        elif cur and ln.startswith("  "):
            d["lists"][cur] += ln.split()
        else:
            cur = None
    for ln in text.splitlines():
        m = re.match(r"^(Inst|Remv|Purg) (\S+)", ln)
        if m:
            d[{"Inst": "inst", "Remv": "remv", "Purg": "purg"}[m.group(1)]].append(m.group(2))
    return d


def listed(d, word):
    """the packages of the list whose title has the word (REMOVED, NEW, upgraded), for a real run"""
    return [n for k, v in d["lists"].items() if word in k for n in v]


# ---- units, packages, containers -------------------------------------------------------------------------------

def load_units(tag):
    out = {}
    for ln in read("units-%s.tsv" % tag).splitlines():
        f = ln.split("\t")
        if len(f) == 5:
            out[f[0]] = dict(active=f[1], sub=f[2], pid=f[3], inv=f[4])
    return out


def load_dpkg(tag):
    """{package: version} of what dpkg had installed (its second state letter is `i`)"""
    out = {}
    for ln in read("dpkg-%s.tsv" % tag).splitlines():
        f = ln.split("\t")
        if len(f) == 3 and f[1] and f[2].strip()[1:2] == "i":
            out[f[0]] = f[1]
    return out


def load_containers(tag):
    out = {}
    for ln in read("containers-%s.txt" % tag).splitlines():
        m = re.match(r"^/(\S+) policy=(\S*) state=(\S+) started=(\S+) restarts=(\d+)", ln)
        if m:
            out[m.group(1)] = dict(policy=m.group(2), state=m.group(3), started=m.group(4), restarts=int(m.group(5)))
    return out


def load_boot(tag):
    d = {"files": {}, "images": "", "uname": "", "reboot": ""}
    for ln in read("kernel-%s.txt" % tag).splitlines():
        f = ln.split(" ")
        if f[0] == "uname" and len(f) > 1:
            d["uname"] = f[1]
        elif f[0] == "boot-file" and len(f) == 3 and f[2].isdigit():
            d["files"][f[1]] = int(f[2])
        elif f[0] == "boot-total" and len(f) > 1 and f[1].isdigit():
            d["total"] = int(f[1])
        elif f[0] == "df-boot" and len(f) >= 5:
            d["df"] = f[1:5]
        elif f[0] == "images":
            d["images"] = " ".join(f[1:])
        elif f[0] == "reboot-required":
            d["reboot"] = f[1] if len(f) > 1 else ""
    return d


def osrel(name):
    return {k: v.strip('"') for k, _, v in (ln.partition("=") for ln in read(name).splitlines()) if k}


def audit_clean(tag):
    rc = need_rc("audit-%s" % tag, "dpkg --audit of the %s state was not run" % tag)
    if rc is None:
        return None
    return rc == 0 and not read("audit-%s.txt" % tag).strip()


def aptcheck_ok(tag):
    rc = need_rc("aptcheck-%s" % tag, "apt-get check of the %s state was not run" % tag)
    return None if rc is None else rc == 0


def failed_units(tag):
    need_rc("failed-%s" % tag, "systemctl --failed of the %s state was not run" % tag)
    return {ln.split()[0] for ln in read("failed-%s.txt" % tag).splitlines() if ln.split()}


# ---- is this run to be trusted ----------------------------------------------------------------------------------

aborted = read("aborted").strip()
if aborted:
    invalid("the guest stopped on its own account: %s" % aborted.replace("\n", " / "))
if read("leg").strip() != leg:
    invalid("the evidence is of leg %r, not %r" % (read("leg").strip() or "none", leg))
if not rows:
    invalid("the timeline has no samples of the poller")

skipped = has("hop-skipped")
ran_hop = mt("hop_start") is not None
base_markers = ["prepare_start", "prepare_done", "inhibit_reboot_start", "inhibit_force_start", "reboot_pre_start", "refresh_start", "refresh_update_done", "refresh_done",
                "baseline_start", "baseline_done", "probe_start", "probe_done"]
for m in base_markers:
    if mt(m) is None:
        invalid("marker %s is missing: the run did not get that far (a phase of the guest did not run)" % m)
if not skipped and not ran_hop:
    invalid("neither hop_start nor hop_skipped is in the timeline: the hop phase did not run")
if skipped and ran_hop:
    invalid("the hop was skipped and started in the same run")
if ran_hop:
    for m in ("hop_unit_end", "hop_done", "reboot_post_start", "after_start", "after_settled", "after_done"):
        if mt(m) is None:
            invalid("marker %s is missing: the run did not get that far (a phase of the guest did not run)" % m)
    if mt("reboot_post_start") is not None and mt("after_start") is not None and not [b for b in markers.get("boot_changed", []) if mt("reboot_post_start") < b[0] <= mt("after_start")]:
        invalid("the guest did not reboot after the hop (no boot_changed between reboot_post_start and after_start)")
if mt("reboot_pre_start") is not None and mt("refresh_start") is not None and not [b for b in markers.get("boot_changed", []) if mt("reboot_pre_start") < b[0] <= mt("refresh_start")]:
    invalid("the guest did not reboot before the hop (no boot_changed between reboot_pre_start and refresh_start)")
# the refresh says whether it needs a reboot (a new kernel, a reboot-required file), and the runner makes it before the baseline
refresh_reboot = read("refresh-reboot.txt").strip()
if refresh_reboot.startswith("yes") and mt("refresh_done") is not None:
    t_rr = mt("reboot_refresh_start")
    if t_rr is None:
        invalid("the refresh asked for a reboot (%s) and the guest did not make it (no reboot_refresh_start)" % refresh_reboot)
    elif mt("baseline_start") is not None and not [b for b in markers.get("boot_changed", []) if t_rr < b[0] <= mt("baseline_start")]:
        invalid("the guest did not reboot after the refresh asked for it (no boot_changed between reboot_refresh_start and baseline_start)")
if "hop_timeout" in markers:
    invalid("the guest stopped waiting for the unit of the hop (hop_timeout)")

# the box is what the leg needs
ob = osrel("os-before")
if not ob:
    invalid("os-before is missing")
elif ob.get("ID") != L["family"] or ob.get("VERSION_ID") != L["old"] or ob.get("VERSION_CODENAME") != L["old_code"]:
    invalid("the box before the hop was %s %s (%s), not %s %s (%s)" % (ob.get("ID"), ob.get("VERSION_ID"), ob.get("VERSION_CODENAME"), L["family"], L["old"], L["old_code"]))
before_containers = load_containers("before")
not_running = [n for n in CONTAINERS if before_containers.get(n, {}).get("state") != "running"]
if not before_containers or not_running:
    invalid("these witnesses were not running before the hop: %s" % (", ".join(not_running) or "none were listed"))
for n, pol in CONTAINERS.items():
    if n in before_containers and before_containers[n]["policy"] != pol:
        invalid("the witness %s has restart policy %r, not %r" % (n, before_containers[n]["policy"], pol))
for code in ("gateway-before", "core-before", "am-before", "jwt-before", "internal-before"):
    got = read(code + ".code").strip()
    if code == "internal-before":
        if not got:
            invalid("%s.code is missing: the baseline did not ask" % code)
    elif got != "200":
        invalid("%s answered %s before the hop (it has to answer 200 for the hop to be measured)" % (code.replace("-before", ""), got or "nothing"))
if read("settle-baseline").strip() in ("", "never"):
    invalid("the witnesses that start by themselves were not running at the baseline (settle-baseline: %s)" % (read("settle-baseline").strip() or "missing"))
dpkg_before = load_dpkg("before")
if not dpkg_before:
    invalid("dpkg-before.tsv is missing or empty")
elif "docker-ce" not in dpkg_before:
    invalid("docker-ce was not installed before the hop")
for c in ("osm-orphan", "osm-orphan-held"):
    if dpkg_before and c not in dpkg_before:
        invalid("the control package %s was not installed before the hop" % c)
docker_b = kv("docker-before.txt").get("server", "")
if has("docker-before.txt") and (not docker_b or docker_b == "unavailable" or docker_b.startswith(("Cannot", "error", "Error", "command"))):
    invalid("Docker did not answer before the hop (docker-before.txt says %r)" % docker_b)
if audit_clean("before") is False:
    invalid("dpkg --audit was not clean before the hop: the box was not a clean start")
if aptcheck_ok("before") is False:
    invalid("apt-get check failed before the hop: the box was not a clean start")
if DEBIAN and leg == "d11-12":
    start_v = read("start-version").strip()
    if start_v != "28.0.4":
        invalid("Docker did not start at 28.0.4 on Debian 11 (it was %r)" % start_v)
if has("df-start.txt") and not read("df-start.txt").strip():
    invalid("df-start.txt is empty")

# disk: said plainly, and a guest that ran out of it is not a measurement of the hop
no_space = [n for n in sorted(os.listdir(out_dir)) if (n.startswith(("step-", "refresh")) or n.endswith(".log")) and
            re.search(r"No space left on device|You don't have enough free space", read(n))]
if no_space:
    invalid("the guest ran out of disk space (%s): the hop was measured on a disk that was too small" % ", ".join(no_space[:4]))

# the poller: no holes but the reboots
reboot_marks = [t for n in ("reboot_pre_start", "reboot_refresh_start", "reboot_post_start", "inhibit_reboot_start", "inhibit_force_start") for t, _ in markers.get(n, [])]
holes = []
for a, b in zip(rows, rows[1:]):
    if b["t"] - a["t"] > HOLE and not any(t - 5 <= a["t"] <= t + REBOOT_GRACE for t in reboot_marks):
        holes.append((a["t"], b["t"] - a["t"]))
if holes:
    invalid("the poller left %d hole(s) of more than %.0f s that no reboot explains, the longest %.0f s" % (len(holes), HOLE, max(h for _, h in holes)))


# ---- what the guest recorded of the tools -----------------------------------------------------------------------------

BOX = kv("box-facts")
for key in ("apt", "dpkg", "systemd", "libc6", "kernel"):
    if not BOX.get(key):
        invalid("box-facts has no %s line" % key)
APT_NOW = (re.search(r"apt (\d+\.\d+\.\d+)", BOX.get("apt", "")) or [None, None])[1]

# ---- the hop's own evidence: what the unit did -------------------------------------------------------------------------

unit_log = []
for ln in read("hop-unit.log").splitlines():
    f = ln.split(" ", 2)
    try:
        unit_log.append((float(f[0]), f[1] if len(f) > 1 else "", f[2] if len(f) > 2 else ""))
    except ValueError:
        pass


def unit_time(word, nth=0):
    m = [t for t, w, _ in unit_log if w == word]
    return m[nth] if len(m) > nth else None


steps = {}   # name -> dict(start, end, rc)
for t, w, rest in unit_log:
    if w == "STEP_START":
        steps[rest.strip()] = dict(start=t, end=None, rc=None)
    elif w == "STEP_END":
        parts = rest.split()
        if parts and parts[0] in steps:
            steps[parts[0]]["end"] = t
            steps[parts[0]]["rc"] = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
unit_done = unit_time("DONE") is not None
if ran_hop:
    if unit_time("START") is None:
        invalid("hop-unit.log has no START line: the unit of the hop did not start")
    for name, s in steps.items():
        if s["end"] is None:
            continue
        if not has("step-%s.log" % name) or rc_of("step-%s" % name) is None:
            invalid("step %s of the unit has no log or no status" % name)


def step_text(name):
    return read("step-%s.log" % name)


def step_rc(name):
    return steps.get(name, {}).get("rc")


# ---- the Ubuntu offer ---------------------------------------------------------------------------------------------------

offered = None
if not DEBIAN:
    chk = read("ubuntu-check.txt")
    chk_rc = need_rc("ubuntu-check", "do-release-upgrade -c was not run in the probe")
    vre = re.escape(L["new"])
    offered = chk_rc == 0 and re.search(r"New release '%s(\.[0-9]+)? LTS' available" % vre, chk) is not None if chk_rc is not None else None
    if offered is False and not skipped:
        invalid("do-release-upgrade -c did not offer %s and the hop was started all the same" % L["new"])
    if offered is True and skipped:
        invalid("do-release-upgrade -c offered %s and the hop was skipped all the same" % L["new"])
    if skipped and read("hop-skipped").strip() != "not_offered":
        invalid("hop-skipped says %r, not not_offered" % read("hop-skipped").strip())

# ---- the hop's facts (need the after-reboot evidence) -------------------------------------------------------------------------

if ran_hop:
    for name, why in (("unit-start.txt", "the unit's first facts"), ("dpkg-hop.tsv", "the packages after the unit"),
                      ("dpkg-after.tsv", "the packages after the reboot"), ("units-before.tsv", "the services before"), ("units-hop.tsv", "the services after the unit"),
                      ("units-boot.tsv", "the services at the first look after the reboot"), ("units-after.tsv", "the services once settled"),
                      ("containers-hop.txt", "the containers after the unit"), ("containers-boot.txt", "the containers at the first look"), ("containers-after.txt", "the containers once settled"),
                      ("os-after", "the release after the reboot"), ("reboot-pre.txt", "the reboot before the hop"), ("reboot-post.txt", "the reboot after it"),
                      ("kernel-before.txt", "the kernel before"), ("kernel-after.txt", "the kernel after"), ("docker-before.txt", "Docker before"), ("docker-after.txt", "Docker after"),
                      ("deps-before.txt", "the installer's dependencies before"), ("deps-after.txt", "the installer's dependencies after"),
                      ("jwt-claims-jwt-before.json", "the claims of the token of the baseline"), ("hop-journal.txt", "the journal of the hop"), ("dpkg-hop.log", "dpkg's own log of the hop")):
        need(name, why)
    if unit_done:
        need("unit-end.txt", "the unit's last facts")
    for tag in ("hop", "after"):
        audit_clean(tag)
        aptcheck_ok(tag)
        failed_units(tag)
    for name in ("gateway-after", "core-after", "internal-after", "am-after", "jwt-after", "jwt-fresh"):
        if not read(name + ".code").strip():
            invalid("%s.code is missing: the after phase did not ask" % name)
    for tag in ("before", "hop", "after"):
        for part in ("logs", "file"):
            if read("db-%s-%s.exit" % (tag, part)).strip() != "0":
                invalid("the database's %s could not be read (%s, exit %s): what it proves of the writes cannot be said" % (part, tag, read("db-%s-%s.exit" % (tag, part)).strip() or "missing"))
    if DEBIAN:
        for name in ("update", "minimal_dl", "minimal", "full_dl", "full", "converge1"):
            if name not in steps:
                if unit_done:
                    invalid("step %s is not in the unit's log although the unit ended" % name)
        for name in ("sources-before.txt", "sources-after.txt"):
            need(name, "the sources before and after the unit's rewrite")
    else:
        if "upgrader" not in steps and unit_done:
            invalid("the unit ended without running the upgrader")
        for name in ("controls.txt", "sources-dir-after.txt", "release-upgrades-before.txt", "release-upgrades-after.txt"):
            need(name, "what the Ubuntu tool and the controls were set up with")
        for name in ("ubuntu-check-unit.rc",):
            if not has(name):
                invalid("%s is missing: the unit did not ask Canonical's tool again" % name)

for name, why in (("refresh-update.log", "apt-get update of the refresh"), ("refresh-upgrade.log", "the full upgrade of the refresh"), ("dpkg-refresh-before.tsv", "the packages before the refresh"),
                  ("dpkg-refresh-after.tsv", "the packages after the refresh"), ("kernel-refresh-before.txt", "the kernel before the refresh"), ("kernel-refresh-after.txt", "the kernel after the refresh"),
                  ("refresh-reboot.txt", "what the refresh decided about a reboot"),
                  ("inhibit-reboot.txt", "the first probe of the inhibitor"), ("inhibit-force.txt", "the second probe of the inhibitor"), ("box-facts", "the tools of the box"),
                  ("df-start.txt", "the disk at the start"), ("df-before.txt", "the disk before the hop"), ("units-before.tsv", "the services before"),
                  ("jwt-claims-jwt-before.json", "the claims of the token of the baseline"), ("dpkg-before.tsv", "the packages before"), ("docker-before.txt", "Docker before")):
    need(name, why)

if DEBIAN:
    for v in ("paused", "kept"):
        for part in ("update", "upgrade", "dist-upgrade"):
            need_rc("sandbox-%s-%s" % (v, part), "the %s sandbox's %s was not run in the probe" % (v, part))
        need("sandbox-%s-sources.txt" % v, "the sources of the %s sandbox" % v)
    need_rc("without-new-pkgs", "the notes' spelling of the minimal upgrade was not tried in the probe")
    for h in ("etc-apt-before.sha", "etc-apt-after-sandbox.sha", "lists-before.sha", "lists-after-sandbox.sha"):
        need(h, "the check that the sandbox left apt as it was")
    if not has("allow-skipped"):
        for f in ("suite", "version", "label"):
            for mode in ("plain", "field", "all"):
                need_rc("allow-%s-%s" % (f, mode), "the --allow-releaseinfo-change experiment was not run")
else:
    for name in ("meta-release.rc", "upgrader-tarball.rc", "pro-status.rc", "release-upgrades-before.rc"):
        if not has(name):
            invalid("%s is missing: the Ubuntu probe did not run" % name)

# ---- the report -----------------------------------------------------------------------------------------------------------

after_os = osrel("os-after")
head_name = "%s %s to %s %s" % (L["family"].title(), L["old"], L["family"].title(), L["new"])


def show_row(label, *cells):
    P("| %s | %s |" % (label, " | ".join(str(c) for c in cells)))


P("## The box")
P("")
P("| | |")
P("|---|---|")
show_row("before", ob.get("PRETTY_NAME", "?"))
if ran_hop:
    show_row("after the reboot", after_os.get("PRETTY_NAME", "?") or "?")
for key in ("apt", "dpkg", "systemd", "libc6", "kernel", "bash", "python3", "sh", "tz", "cpus", "memory_mb", "needrestart", "upgrader", "cloud-init"):
    if BOX.get(key):
        show_row(key, BOX[key])
if DEBIAN and APT_NOW and APT_NOW != L["apt"]:
    show_row("note", "the apt of this leg is meant to be %s: it is %s" % (L["apt"], APT_NOW))
show_row("Docker the box started on", read("start-version").strip() or "?")
started = read("baseline-started").split()
if started:
    show_row("witnesses started again at the baseline (their policy does not start them after a reboot)", ", ".join(started))
notes = read("prepare-notes").strip()
if notes:
    show_row("what the old release needed to be refreshed", notes.replace("\n", "; "))
P("")


def df_row(tag):
    out = []
    for ln in read("df-%s.txt" % tag).splitlines():
        f = ln.split()
        if len(f) == 6 and f[1].isdigit():
            out.append((f[5], int(f[3])))
    return out


P("## Disk")
P("")
P("A hosted runner has about 14 GB; the guest's disk is a sparse 30 GB. Free space of the guest, in MB, as `df` said at each point (the runner's own `df` is in host-df-*.txt when the workflow wrote it):")
P("")
P("| point | mount: free MB |")
P("|---|---|")
low = None
for tag in ("start", "prepared", "before", "probed", "hop", "after"):
    cells = df_row(tag)
    if cells:
        show_row(tag, ", ".join("%s: %d" % c for c in cells[:3]))
        low = min([c[1] for c in cells] + ([low] if low is not None else []))
steps_df = [ln.split() for ln in read("df-steps.txt").splitlines() if len(ln.split()) == 3]
if steps_df:
    mn = min(int(x[2]) for x in steps_df if x[2].isdigit())
    low = mn if low is None else min(low, mn)
    P("")
    P("Lowest free space on a mount, after any step of the hop: %d MB." % mn)
P("")
P("%s" % ("Disk was NOT short in the guest: the lowest free space seen is %d MB." % low if low is not None else "No free space figure was read."))
for hf in ("host-df-start.txt", "host-df-end.txt"):
    if has(hf):
        P("")
        P("The runner's disk, %s:" % hf)
        P("")
        for ln in read(hf).splitlines()[:6]:
            P("    " + ln)
P("")

# ---- timings -----------------------------------------------------------------------------------------------------------------

P("## Durations")
P("")
P("| phase | duration |")
P("|---|---|")
show_row("prepare (Docker, ReCasaOS and the witnesses on the old release)", fmt_s(span("prepare_start", "prepare_done")))
show_row("refresh of the old release (the dashboard's step 'update the current release': apt update, a full upgrade, Docker's source paused)", fmt_s(span("refresh_start", "refresh_done")))
show_row("  of which apt update", fmt_s(span("refresh_start", "refresh_update_done")))
show_row("  of which the full upgrade", fmt_s(span("refresh_update_done", "refresh_done")))
if mt("reboot_refresh_start") is not None:
    back = next((b[0] for b in markers.get("boot_changed", []) if b[0] > mt("reboot_refresh_start")), None)
    show_row("  the reboot the refresh asked for (the request to the next phase seeing the new boot)", fmt_s(None if back is None else back - mt("reboot_refresh_start")))
show_row("probes of the old release (sandbox, releaseinfo-change, or Canonical's metadata)", fmt_s(span("probe_start", "probe_done")))
if ran_hop:
    show_row("the unit (the hop, without the reboot)", fmt_s(span("hop_start", "hop_unit_end")))
    for name, s in steps.items():
        if s["end"] is not None:
            show_row("  step %s (exit %s)" % (name, s["rc"]), fmt_s(s["end"] - s["start"]))
P("")

# ---- the refresh of the old release -------------------------------------------------------------------------------------------

P("## The refresh of the old release")
P("")
P("As the product's refresh step will do it before any hop (the release notes of the next release and the Ubuntu launcher want the box up to date): `apt-get update`, then a full upgrade of the "
  "CURRENT release with Docker's source paused, after the rehearsal reboot and just before the baseline.")
P("")
rf_before, rf_after = load_dpkg("refresh-before"), load_dpkg("refresh-after")
if rf_before and rf_after:
    rf_added = sorted(set(rf_after) - set(rf_before))
    rf_removed = sorted(set(rf_before) - set(rf_after))
    rf_up = sorted(n for n in set(rf_before) & set(rf_after) if rf_before[n] != rf_after[n])
    rf_apt = apt_parse(read("refresh-upgrade.log"))
    P("dpkg's own count, before and after the refresh: %d upgraded, %d added, %d removed (of %d installed before, %d after). apt's own summary: %s; need to get %s; after this operation %s." % (
        len(rf_up), len(rf_added), len(rf_removed), len(rf_before), len(rf_after), ("%d upgraded, %d new, %d to remove, %d not upgraded" % rf_apt["counts"]) if rf_apt["counts"] else "no summary line",
        fmt_bytes(rf_apt["need"]), fmt_bytes(rf_apt["disk"])))
    P("")
    for label, names_ in (("Upgraded", rf_up), ("Added", rf_added), ("Removed", rf_removed)):
        P("%s: %s." % (label, (", ".join(names_[:40]) + (" ... (%d more)" % (len(names_) - 40) if len(names_) > 40 else "")) or "none"))
        P("")
    fact("the refresh of the old release", "%d upgraded, %d added, %d removed, in %s; a reboot is %s" % (len(rf_up), len(rf_added), len(rf_removed), fmt_s(span("refresh_start", "refresh_done")),
                                                                                                         "needed: " + refresh_reboot[4:].strip() if refresh_reboot.startswith("yes") else "not needed"))
rk_before, rk_after = load_boot("refresh-before"), load_boot("refresh-after")
P("Kernel images before the refresh `%s`, after it `%s`; the system asks for a reboot after it: %s. The refresh's own answer about a reboot: `%s`." % (
    rk_before["images"], rk_after["images"], rk_after["reboot"] or "?", refresh_reboot or "none"))
P("")
still = [ln.split("/")[0] for ln in read("upgradable-refresh.txt").splitlines() if "[upgradable from" in ln]
P("Still upgradable after the refresh: %s." % (("%d (%s)" % (len(still), ", ".join(still[:20]))) if still else "nothing"))
fact("packages still upgradable after the refresh", ", ".join(still[:20]) or "none")
P("")

# ---- the sandbox (Debian) -----------------------------------------------------------------------------------------------------

sandbox_ok = None
if DEBIAN:
    P("## The sandbox plan (read-only, /etc/apt untouched)")
    P("")
    P("`apt-get -o Dir::Etc::sourcelist=<tmp> -o Dir::Etc::sourceparts=<tmp> -o Dir::State::lists=<tmp> -o Dir::Cache=<tmp> -o Debug::NoLocking=true update --error-on=any`, then `-s upgrade` and `-s dist-upgrade`, "
      "on %s's own apt (%s) against %s's InRelease. `paused`: Debian's sources only, as the hop leaves them. `kept`: Docker's source as it stands on the box.\n" % (L["old_code"], APT_NOW or "?", L["new_code"]))
    P("| | update | -s upgrade | -s dist-upgrade | Docker lines in the simulation | size |")
    P("|---|---|---|---|---|---|")
    sims = {}
    for v in ("paused", "kept"):
        rcs = [rc_of("sandbox-%s-%s" % (v, p)) for p in ("update", "upgrade", "dist-upgrade")]
        sim = read("sandbox-%s-dist-upgrade.txt" % v)
        sims[v] = apt_parse(sim)
        sims[v]["rcs"] = rcs
        sims[v]["upgrade"] = apt_parse(read("sandbox-%s-upgrade.txt" % v))
        dock = ["%s %s" % (m.group(1), m.group(2)) for m in re.finditer(r"^(Inst|Remv|Purg) (docker\S*|containerd\S*)", sim, re.M)]
        show_row(v, *rcs, "; ".join(dock) or "none", read("sandbox-%s-size.txt" % v).split("\t")[0].strip() + " MB" if read("sandbox-%s-size.txt" % v).strip() else "n/a")
    P("")
    same_etc = read("etc-apt-before.sha").strip() == read("etc-apt-after-sandbox.sha").strip() and bool(read("etc-apt-before.sha").strip())
    same_lists = read("lists-before.sha").strip() == read("lists-after-sandbox.sha").strip() and bool(read("lists-before.sha").strip())
    P("/etc/apt before and after the sandbox: %s. /var/lib/apt/lists: %s." % ("identical" if same_etc else "DIFFERENT", "identical" if same_lists else "DIFFERENT"))
    P("")
    pr = sims["paused"]["rcs"]
    if None not in pr:
        sandbox_ok = pr == [0, 0, 0] and same_etc and same_lists
        why = []
        for part, text in (("update", read("sandbox-paused-update.txt")), ("-s upgrade", read("sandbox-paused-upgrade.txt")), ("-s dist-upgrade", read("sandbox-paused-dist-upgrade.txt"))):
            e = first_error(text)
            if e:
                why.append("%s: %s" % (part, e))
        answer("The sandbox plan works (else one-phase confirmation, the guards staying in the unit)", "GO" if sandbox_ok else "NO-GO",
               ("update, -s upgrade and -s dist-upgrade exit %s, /etc/apt and the lists untouched" % "/".join(str(x) for x in pr)) if sandbox_ok else
               "exits %s; /etc/apt %s, lists %s%s" % ("/".join(str(x) for x in pr), "untouched" if same_etc else "CHANGED", "untouched" if same_lists else "CHANGED", ("; " + "; ".join(why)) if why else ""))
        # how close is it to the real transaction
        if ran_hop and "minimal" in steps and "full" in steps:
            real = {k: apt_parse(step_text(k)) for k in ("minimal", "full")}
            P("### How close the plan is to the transaction that was done")
            P("")
            P("The plan is made on the old release, before the minimal upgrade (the notes say `dist-upgrade` may even fail there); the real transaction is the minimal upgrade followed by the full one.")
            P("")
            P("| | upgraded | newly installed | to remove | need to get | disk |")
            P("|---|---|---|---|---|---|")

            def crow(label, d):
                c = d["counts"] or (None,) * 4
                show_row(label, c[0], c[1], c[2], fmt_bytes(d["need"]), fmt_bytes(d["disk"]))
            crow("sandbox -s upgrade", sims["paused"]["upgrade"])
            crow("real minimal upgrade", real["minimal"])
            crow("sandbox -s dist-upgrade", sims["paused"])
            crow("real full upgrade", real["full"])
            tot = [sum((real[k]["counts"] or (0, 0, 0, 0))[i] for k in real) for i in range(3)]
            sd = sims["paused"]["counts"] or (None, None, None, None)
            show_row("real, both together", tot[0], tot[1], tot[2], fmt_bytes(sum(real[k]["need"] or 0 for k in real)), fmt_bytes(sum(real[k]["disk"] or 0 for k in real)))
            if None not in sd and sd[0] + sd[1]:
                diff = [abs(a - b) for a, b in zip(sd[:3], tot)]
                P("")
                P("Counts of the sandbox's dist-upgrade against the two real steps together: upgraded %+d, newly installed %+d, to remove %+d." % (sd[0] - tot[0], sd[1] - tot[1], sd[2] - tot[2]))
                fact("the sandbox's counts against the real transaction (upgraded, new, removed)", "%s against %s: differences %s" % (tuple(sd[:3]), tuple(tot), tuple(diff)))
            P("")
    else:
        sandbox_ok = None
    wnp = rc_of("without-new-pkgs")
    fact("`apt-get --without-new-pkgs upgrade` (the notes' spelling, for apt) on apt %s" % (APT_NOW or "?"), "accepted" if wnp == 0 else "NOT accepted (exit %s): %s" % (wnp, first_error(read("without-new-pkgs.txt")) or "no message"))
    # the documented switches
    P("### --allow-releaseinfo-change-{suite,version,label} on apt %s" % (APT_NOW or "?"))
    P("")
    if has("allow-skipped"):
        P("Not run: %s." % read("allow-skipped").strip())
        open_q("--allow-releaseinfo-change-* was not measured: %s" % read("allow-skipped").strip())
    else:
        P("A stored InRelease of %s gets one field changed (and a time of 0, so that apt asks for the file again), then `apt-get update --error-on=any` runs without the option, with the option of that field, and, after another change, with `--allow-releaseinfo-change`.\n" % L["new_code"])
        P("| field | without | with the field's option | with the general option | as documented |")
        P("|---|---|---|---|---|")
        all_doc = True
        for f in ("suite", "version", "label"):
            plain, one, allv = (rc_of("allow-%s-%s" % (f, m)) for m in ("plain", "field", "all"))
            said = re.search(r"changed its '%s' value" % f.capitalize(), read("allow-%s-plain.txt" % f)) is not None
            doc = plain not in (None, 0) and said and one == 0 and allv == 0
            all_doc = all_doc and doc
            show_row(f, "exit %s%s" % (plain, "" if said else " (no 'changed its' message)"), "exit %s" % one, "exit %s" % allv, "yes" if doc else "NO")
        P("")
        fact("--allow-releaseinfo-change-{suite,version,label} on apt %s" % APT_NOW, "behave as documented" if all_doc else "DO NOT behave as documented (see the table)")
    P("")

# ---- what changed in the packages ----------------------------------------------------------------------------------------------

P("## The packages")
P("")
d_before, d_hop, d_after = load_dpkg("before"), load_dpkg("hop"), load_dpkg("after")
if ran_hop and d_hop:
    added = sorted(set(d_hop) - set(d_before))
    removed = sorted(set(d_before) - set(d_hop))
    upgraded = sorted(n for n in set(d_before) & set(d_hop) if d_before[n] != d_hop[n])
    P("dpkg's own count, before and after the unit (before the reboot): %d upgraded, %d added, %d removed (of %d installed before, %d after)." % (len(upgraded), len(added), len(removed), len(d_before), len(d_hop)))
    P("")
    P("Removed: %s." % (", ".join(removed[:80]) or "none"))
    if len(removed) > 80:
        P("(%d more)" % (len(removed) - 80))
    P("")
    P("Added: %s." % ((", ".join(added[:60]) + (" ... (%d more)" % (len(added) - 60) if len(added) > 60 else "")) or "none"))
    P("")
    P("Installed by hand (the controls and Docker's engine) and what became of them:")
    P("")
    P("| package | before | after the unit | after the reboot |")
    P("|---|---|---|---|")
    for n in ["osm-orphan", "osm-orphan-held"] + ENGINE:
        if n in d_before or n in d_hop:
            show_row(n, d_before.get(n, "-"), d_hop.get(n, "REMOVED" if n in d_before else "-"), d_after.get(n, "REMOVED" if n in d_before else "-"))
    P("")
    gone_prot = [n for n in removed if n in PROTECTED]
    fact("protected names of the design's guard that the hop removed", ", ".join(gone_prot) or "none")
    for n in ("full", "minimal", "full_dl", "minimal_dl") if DEBIAN else ("upgrader",):
        if n in steps and steps[n]["end"] is not None:
            txt = step_text(n)
            ap = apt_parse(txt)
            if ap["counts"] or ap["need"]:
                P("- step `%s` (exit %s): %s; need to get %s; after this operation %s." % (n, step_rc(n), ("%d upgraded, %d new, %d to remove, %d not upgraded" % ap["counts"]) if ap["counts"] else "no summary line", fmt_bytes(ap["need"]), fmt_bytes(ap["disk"])))
    P("")
    if DEBIAN and "full" in steps:
        rem = listed(apt_parse(step_text("full")), "REMOVED")
        P("apt's list of packages it was going to REMOVE in the full upgrade: %s." % (", ".join(rem[:60]) or "none"))
        P("")

# ---- services that bounced -------------------------------------------------------------------------------------------------------

P("## Which services were restarted while dpkg ran")
P("")
u_before, u_hop = load_units("before"), load_units("hop")
restarted = {}
if ran_hop and u_before and u_hop:
    for name, u in u_before.items():
        h = u_hop.get(name)
        if h and u["inv"] and h["inv"] and u["inv"] != h["inv"] and name not in ("osm-poller.service",) and not name.startswith("osm-reboot"):
            restarted[name] = (u["active"], h["active"])
    P("A service whose InvocationID is not the same before and after the unit was started again in between (%d of %d services present at both ends)." % (len(restarted), len(set(u_before) & set(u_hop))))
    P("")
    P("| unit | state before | state after |")
    P("|---|---|---|")
    for name in sorted(restarted):
        show_row(name, *restarted[name])
    P("")
    bounced = sorted({RESTART_WATCH[n] for n in restarted if n in RESTART_WATCH})
    fact("ssh, samba and Docker/containerd restarted during the hop", "yes: %s" % ", ".join(bounced) if bounced else "none of ssh, samba, docker, containerd, cron, dbus, logind was restarted")
    for k, label in (("ssh", "ssh"), ("samba", "samba"), ("docker", "docker"), ("containerd", "containerd")):
        fact("  %s bounced during the hop" % label, "yes" if k in bounced else "no")
    jr = [ln for ln in read("hop-journal.txt").splitlines()]
    P("systemd's journal of the hop has %d lines of Stopping/Starting; the first ones:" % len(jr))
    P("")
    for ln in jr[:12]:
        P("    " + ln[:170])
    P("")
    # libc6: the window of dpkg's own log, and what was said in it
    cfg = {}
    for ln in read("dpkg-hop.log").splitlines():
        f = ln.split()
        if len(f) >= 5 and f[2] == "status" and f[4].split(":")[0] == "libc6":
            cfg.setdefault(f[3], f[0] + " " + f[1])
    ver_from, ver_to = d_before.get("libc6"), d_hop.get("libc6")
    P("### libc6 %s to %s" % (ver_from, ver_to))
    P("")
    if ver_from == ver_to:
        P("libc6 was not upgraded by the unit.")
        open_q("which services libc6 restarts: libc6 was not upgraded in this leg")
    else:
        t0s, t1s = cfg.get("half-configured"), cfg.get("installed")
        P("dpkg's log: libc6 half-configured at %s, installed at %s." % (t0s or "n/a", t1s or "n/a"))
        lib = []
        for step in ("full", "minimal", "recover_full"):
            txt = step_text(step)
            m = re.search(r"Setting up libc6[^\n]*\n((?:(?!Setting up |Processing triggers|Preparing to unpack)[^\n]*\n)*)", txt)
            if m:
                lib += [ln.rstrip() for ln in m.group(0).splitlines()]
        names = sorted({re.sub(r"[:.].*", "", ln.strip().split()[0]) for ln in lib if re.match(r"^\s+[a-z0-9._@-]+: (restarting|stopping|starting|\.\.\.)", ln)})
        inline = re.findall(r"Restarting services possibly affected by the upgrade:\n((?:\s+\S.*\n)+)", "\n".join(lib) + "\n")
        for blk in inline:
            names += [ln.split(":")[0].strip() for ln in blk.splitlines() if ":" in ln]
        names = sorted(set(names))
        P("Services named by libc6's postinst in apt's output: %s." % (", ".join(names) or "none (the output of the step has no such line)"))
        P("")
        for ln in lib[:14]:
            P("    " + ln[:150])
        P("")
        fact("services libc6 %s -> %s restarted (its postinst, non-interactive)" % (ver_from, ver_to), ", ".join(names) or "none named")
        if not t0s or not t1s:
            open_q("when libc6 was configured: dpkg's log of the hop has no half-configured/installed pair for it")

# ---- the witnesses ------------------------------------------------------------------------------------------------------------------

P("## The witnesses")
P("")


def db_report(tag):
    logs, filed = read("db-%s-logs.txt" % tag), set(read("db-%s-file.txt" % tag).split())
    acked = re.findall(r"^(\S+) ack (\S+)$", logs, re.M)
    return acked, filed


def ts(s):
    m = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z$", s)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1) + "+00:00").timestamp() + (float("0." + m.group(2)) if m.group(2) else 0.0)
    except ValueError:
        return None


db_lost = db_gap = None
if ran_hop:
    acked, filed = db_report("after")
    if not acked or not filed:
        invalid("the database's log or file is empty after the reboot (%d acknowledged, %d in the file)" % (len(acked), len(filed)))
    else:
        stamps = [ts(a) for a, _ in acked]
        if None in stamps:
            invalid("a timestamp of the database's log could not be read")
        else:
            lost = [i for _, i in acked if i not in filed]
            gaps = sorted(((b - a, a) for a, b in zip(stamps, stamps[1:])), reverse=True)
            db_lost = lost
            db_gap = gaps[0] if gaps else (0.0, 0.0)
            P("The database acknowledged %d writes; %d are in its file; **%d missing** (this finds a lost or rolled-back volume, not a power cut)." % (len(acked), len(filed), len(lost)))
            P("")
            P("Longest silence between two acknowledged writes: %.1f s (at %s)." % (db_gap[0], datetime.utcfromtimestamp(db_gap[1]).strftime("%H:%M:%S")))
            hop_w = (mt("hop_start"), mt("hop_unit_end"))
            reb_w = (mt("reboot_post_start"), mt("after_settled"))
            for label, (a, b) in (("during the hop", hop_w), ("from the reboot to the containers being back", reb_w)):
                if a is not None and b is not None:
                    inside = [(y - x) for x, y in zip(stamps, stamps[1:]) if a - 5 <= x <= b]
                    P("Longest silence %s: %s." % (label, ("%.1f s" % max(inside)) if inside else "no write was acknowledged then"))
                    fact("database: longest silence %s" % label, ("%.1f s" % max(inside)) if inside else "no write was acknowledged")
    P("")

if ran_hop:
    P("### The published ports, Docker and the gateway, in the samples of the poller (about one a second)")
    P("")
    P("| window | docker | published port 18081 | host-network port 18082 | ssh | samba | gateway | core | AppManagement |")
    P("|---|---|---|---|---|---|---|---|---|")
    hop_win = window("hop_start", "hop_unit_end")
    for label, win in (("the hop", hop_win),):
        cols = (lambda r: r["docker"] == "-", lambda r: r["web"] != "200", lambda r: r["host"] != "200", lambda r: r["ssh"] != "up", lambda r: r["smb"] != "up",
                lambda r: r["gw"] != "200", lambda r: r["core"] != "200", lambda r: not am_up(r))
        cells = []
        end_t = mt("hop_unit_end") or 0
        for fn in cols:
            eps = episodes(win, fn)
            cells.append("%d episode(s), %s" % (len(eps), fmt_s(total_down(eps, end_t))) if eps else "never down for two samples")
        show_row(label, *cells)
    P("")
    # the reboot: how long each answer was away, counted from the request
    t_req = mt("reboot_post_start")
    after_rows = [r for r in rows if t_req is not None and r["t"] > t_req]
    # the first sample after the box came back is the first one after the poller's gap
    boot_row = None
    for a, b in zip(rows, rows[1:]):
        if t_req is not None and a["t"] >= t_req - 1 and b["t"] - a["t"] > 5:
            boot_row = b
            break
    reboot_times = {}
    if boot_row is not None:
        last_before = max((r for r in rows if r["t"] < boot_row["t"]), key=lambda r: r["t"])
        reboot_times["the box was away (last sample before, first after)"] = boot_row["t"] - last_before["t"]
        later = [r for r in rows if r["t"] >= boot_row["t"]]
        for label, fn in (("ssh answers", lambda r: r["ssh"] == "up"), ("samba accepts", lambda r: r["smb"] == "up"), ("Docker answers", lambda r: r["docker"] != "-"),
                          ("the gateway answers 200", lambda r: r["gw"] == "200"), ("the core answers 200", lambda r: r["core"] == "200"),
                          ("AppManagement answers 200", am_up), ("the published port answers 200", lambda r: r["web"] == "200"),
                          ("every container that starts by itself is running", lambda r: all(n in r["names"] for n in COMES_BACK))):
            hit = next((r for r in later if fn(r)), None)
            reboot_times[label] = None if hit is None else hit["t"] - boot_row["t"]
        P("### After the reboot, seconds from the first sample of the box (%s)" % datetime.utcfromtimestamp(boot_row["t"]).strftime("%H:%M:%S"))
        P("")
        P("| | seconds |")
        P("|---|---|")
        for k, v in reboot_times.items():
            show_row(k, "never" if v is None else "%.0f s" % v)
        P("")
        P("From the request to reboot to the first sample of the poller: %s." % fmt_s(boot_row["t"] - t_req))
        fact("reboot after the hop, request to the box answering", fmt_s(boot_row["t"] - t_req))
    else:
        invalid("no gap of the poller after reboot_post_start: the box did not go down, or the poller did not come back")
    P("")
    P("### Containers, by restart policy")
    P("")
    P("| container | policy | before | after the unit | first look after the reboot | once settled | restarts |")
    P("|---|---|---|---|---|---|---|")
    cb, ch, cbt, ca = before_containers, load_containers("hop"), load_containers("boot"), load_containers("after")
    for n in CONTAINERS:
        show_row(n, CONTAINERS[n], cb.get(n, {}).get("state", "?"), ch.get(n, {}).get("state", "?"), cbt.get(n, {}).get("state", "?"), ca.get(n, {}).get("state", "?"), ca.get(n, {}).get("restarts", "?"))
    P("")
    not_back = [n for n in COMES_BACK if ca.get(n, {}).get("state") != "running"]
    stayed = [n for n in CONTAINERS if CONTAINERS[n] in ("no", "on-failure") and ca.get(n, {}).get("state") != "running"]
    P("The containers whose policy starts them again by themselves: %s." % ("ALL running again" if not not_back else "NOT running again: " + ", ".join(not_back)))
    P("The others (`no`, `on-failure`) that were not running again: %s (the same list the Docker update calls not_returned)." % (", ".join(stayed) or "none"))
    P("")
    settle_s = read("settle-after").strip()
    fact("containers that start by themselves, back after", settle_s + " s" if settle_s.isdigit() else settle_s or "n/a")

# ---- health, kernel, /boot -------------------------------------------------------------------------------------------------------------

if ran_hop:
    P("## Health and the kernel")
    P("")
    P("| | before | after the unit | after the reboot |")
    P("|---|---|---|---|")
    for label, fn in (("dpkg --audit is empty", lambda t: audit_clean(t)), ("apt-get check passes", lambda t: aptcheck_ok(t))):
        show_row(label, *("?" if fn(t) is None else ("yes" if fn(t) else "NO") for t in ("before", "hop", "after")))
    fb, fh, fa = failed_units("before"), failed_units("hop"), failed_units("after")
    show_row("systemctl --failed", len(fb), len(fh), len(fa))
    P("")
    new_failed = sorted(fa - fb)
    P("Failed units that were not failed before: %s." % (", ".join(new_failed) or "none"))
    P("")
    kb, kh, ka = load_boot("before"), load_boot("hop"), load_boot("after")
    P("| | before | after the unit | after the reboot |")
    P("|---|---|---|---|")
    show_row("running kernel", kb["uname"] or "?", kh["uname"] or "?", ka["uname"] or "?")
    show_row("/boot, bytes in files", kb.get("total", "?"), kh.get("total", "?"), ka.get("total", "?"))
    show_row("/boot: size, used, free (MB)", *(" ".join(k.get("df", ["?"])[:3]) for k in (kb, kh, ka)))
    show_row("a reboot is asked for", kb["reboot"] or "?", kh["reboot"] or "?", ka["reboot"] or "?")
    P("")
    P("Kernel images: before `%s`; after `%s`." % (kb["images"], ka["images"]))
    P("")
    seq = read("sysstate-wait.txt").strip()
    P("`systemctl is-system-running --wait` after the reboot said: %s (exit %s). systemd-analyze: %s" % (seq or "?", read("sysstate-wait.rc").strip() or "?", " ".join(read("analyze.txt").split())[:200] or "n/a"))
    P("")
    miss_after = [ln.split()[0] for ln in read("deps-after.txt").splitlines() if ln.split() and ln.split()[1:] and ln.split()[1] not in ("ii", "hi")]
    miss_before = [ln.split()[0] for ln in read("deps-before.txt").splitlines() if ln.split() and ln.split()[1:] and ln.split()[1] not in ("ii", "hi")]
    gone_deps = sorted(set(miss_after) - set(miss_before))
    P("The installer's dependencies (and wsdd, its optional one) that were installed and are not any more: %s." % (", ".join(gone_deps) or "none"))
    fact("the installer's dependencies that the hop removed", ", ".join(gone_deps) or "none")
    P("")

# ---- the inhibitor ------------------------------------------------------------------------------------------------------------------------


def inhibit(variant):
    """what the attempt did: the guest writes the attempt before it makes it, `rc` when the command returned, `survived yes` after 40 s of the box still being there;
    a box that went down under it has neither, and a reboot that came after the start is the evidence that it went down"""
    kvs = kv("inhibit-%s.txt" % variant)
    start = mt("inhibit_%s_start" % variant)
    survived = kvs.get("survived") == "yes"
    # the next phase says the box is on another boot before it writes its own start: the attempt owns the boot changes between its start and that one
    nxt = mt("inhibit_force_start") if variant == "reboot" else mt("reboot_pre_start")
    changed = [b for b in markers.get("boot_changed", []) if start is not None and nxt is not None and start < b[0] <= nxt]
    rc = kvs.get("rc")
    out_txt = " ".join(read("inhibit-%s.out" % variant).split())
    if not kvs or start is None or "boot_id" not in kvs:
        invalid("inhibit-%s.txt does not record the attempt" % variant)
        return None, out_txt
    if not survived and not changed:
        invalid("the %s attempt of the inhibitor ended without the box going down and without the line that says it stayed up (the phase was cut short)" % variant)
        return None, out_txt
    if changed:
        return "NOT REFUSED (the box went down and came back with another boot)", out_txt
    if rc is not None and rc.isdigit() and int(rc) != 0:
        return "REFUSED", out_txt
    return "UNCLEAR (exit 0, and the box did not go down within 40 s)", out_txt


inh = {v: inhibit(v) for v in ("reboot", "force")}
for v, (res, txt) in inh.items():
    fact("root's `systemctl reboot%s` under systemd-inhibit --what=shutdown --mode=block" % (" --force" if v == "force" else ""), "%s%s" % (res, (": " + txt[:200]) if txt and res == "REFUSED" else ""))
if all(res for res, _ in inh.values()):
    r1, r2 = inh["reboot"][0], inh["force"][0]
    answer("The product can rely on the block inhibitor to keep root's reboot out (the design does not count it as a guard)", "NO-GO" if r1.startswith("NOT") else "GO" if r1 == "REFUSED" else "OPEN",
           "reboot: %s; --force: %s" % (r1, r2))
    if r1 == "REFUSED" and not r2.startswith("REFUSED"):
        open_q("--force goes round the lock, as documented: only a refusal of the plain reboot is a guard against an accident")
    if "UNCLEAR" in r1 + r2:
        open_q("the inhibitor: a reboot that returned 0 and did not take the box down within 40 s cannot be classified")

# ---- the unit's survival --------------------------------------------------------------------------------------------------------------------

if ran_hop:
    us, ue = kv("unit-start.txt"), kv("unit-end.txt")
    up = {n: (d_before.get(n), d_hop.get(n)) for n in ("systemd", "dbus", "libc6") if d_before.get(n) != d_hop.get(n)}
    same_pid = bool(us.get("pid")) and us.get("pid") == ue.get("pid")
    if not unit_done:
        where = ", ".join(n for n, st in steps.items() if st["end"] is None) or "its first step"
        answer("The transient unit survives the upgrade of systemd, dbus and libc6", "NO-GO", "the unit never wrote DONE: it was in %s when it ended" % where)
    elif not up:
        answer("The transient unit survives the upgrade of systemd, dbus and libc6", "OPEN", "none of systemd, dbus, libc6 was upgraded in this leg")
        open_q("the unit's survival: systemd, dbus and libc6 were not upgraded by this hop")
    else:
        answer("The transient unit survives the upgrade of systemd, dbus and libc6", "GO" if same_pid else "NO-GO",
               "%s the same process (pid %s) from START to DONE; upgraded meanwhile: %s; the manager was %s, is %s" % ("it was" if same_pid else "it was NOT", us.get("pid"), ", ".join("%s %s -> %s" % (n, a, b) for n, (a, b) in up.items()), us.get("manager") or "?", ue.get("manager") or "?"))

# ---- Docker, the sandbox's 'kept' and 'frozen' ------------------------------------------------------------------------------------------------

if DEBIAN and sandbox_ok is not None:
    paused = sims["paused"]
    kept = sims["kept"]
    pr_rem = [n for n in paused["remv"] + paused["purg"] + paused["upgrade"]["remv"] + paused["upgrade"]["purg"] if n.split(":")[0] in PROTECTED]
    kp_rem = [n for n in kept["remv"] + kept["purg"] + kept["upgrade"]["remv"] + kept["upgrade"]["purg"] if n.split(":")[0] in PROTECTED]
    kp_inst = [n for n in kept["inst"] + kept["upgrade"]["inst"] if n.split(":")[0] in PROTECTED]
    pa_inst = [n for n in paused["inst"] + paused["upgrade"]["inst"] if n.split(":")[0] in PROTECTED]
    fact("the paused-source simulation removes an engine package", ", ".join(sorted(set(pr_rem))) or "no Remv or Purg of docker-ce, docker-ce-cli, containerd.io or a docker plugin")
    fact("the kept-source simulation removes an engine package", ", ".join(sorted(set(kp_rem))) or "no Remv or Purg of an engine package")
    fact("the frozen Docker repository (%s's) with the source kept: the plan includes Docker" % L["old_code"], ("yes: " + ", ".join(sorted(set(kp_inst)))) if kp_inst else "no (nothing of Docker's is upgraded or installed)")
    fact("the same with the source paused", ("yes: " + ", ".join(sorted(set(pa_inst)))) if pa_inst else "no")

if ran_hop:
    dock_kept = all(d_hop.get(n) == d_before.get(n) and n in d_hop for n in ENGINE_CORE if n in d_before)
    dock_kept_after = all(d_after.get(n) == d_before.get(n) and n in d_after for n in ENGINE_CORE if n in d_before)
    dsv = {k: kv("docker-%s.txt" % k).get("server") for k in ("before", "after")}
    answers_after = dsv["after"] and dsv["after"] != "unavailable" and not dsv["after"].startswith(("Cannot", "error", "Error", "command"))
    if DEBIAN and sandbox_ok is not None:
        no_remv = not pr_rem
        okay = no_remv and dock_kept_after and dsv["before"] == dsv["after"]
        answer("The docker-ce built for %s survives the hop (its source paused)" % L["old_code"], "GO" if okay else "NO-GO",
               "the paused simulation removes %s; after the reboot docker-ce/-cli/containerd.io are %s; the engine answers %s (before %s)" % (
                   ("an engine package: " + ", ".join(sorted(set(pr_rem)))) if pr_rem else "no engine package", "the same installed versions" if dock_kept_after else "NOT all the same installed versions", dsv["after"] or "nothing", dsv["before"]))
    elif DEBIAN:
        answer("The docker-ce built for %s survives the hop (its source paused)" % L["old_code"], "OPEN", "the sandbox plan could not be read")
    # the box after the reboot answers for itself
    fact("Docker after the reboot", "answers %s (before %s); packages: %s" % (dsv["after"], dsv["before"], kv("docker-after.txt").get("packages", "")[:200]))

# ---- core, gateway, AppManagement, a token -------------------------------------------------------------------------------------------------------

if ran_hop:
    ub, uh, ub2, ua = load_units("before"), load_units("hop"), load_units("boot"), load_units("after")
    casa = sorted(n for n in ub if CASA_UNITS_RE.match(n) and ub[n]["active"] == "active")
    P("## ReCasaOS after the reboot")
    P("")
    P("The after phase starts nothing: it reads the units first (`units-boot`), waits, then asks. (The harness's own test checks that the phase has no start, restart or enable of a unit.)")
    P("")
    P("| unit | before | first look | settled | restarted between the first look and settled |")
    P("|---|---|---|---|---|")
    down_after, moved = [], []
    for n in casa:
        a, b = ub2.get(n, {}), ua.get(n, {})
        again = bool(a.get("inv")) and bool(b.get("inv")) and a["inv"] != b["inv"]
        if b.get("active") != "active":
            down_after.append(n)
        if again:
            moved.append(n)
        show_row(n, ub[n]["active"], a.get("active", "?"), b.get("active", "?"), "YES" if again else "no")
    P("")
    codes = {k: read("%s-after.code" % k).strip() for k in ("gateway", "core", "internal", "am", "jwt")}
    P("Asked once settled: gateway %s, core %s, core with the loopback secret %s, AppManagement with the token of the baseline's login %s." % (codes["gateway"], codes["core"], codes["internal"], codes["am"]))
    P("")
    answered = {k: reboot_times.get(lbl) for k, lbl in (("gateway", "the gateway answers 200"), ("core", "the core answers 200"), ("am", "AppManagement answers 200"))} if boot_row is not None else {}
    unaided = bool(casa) and not down_after and not moved and all(answered.get(k) is not None for k in ("gateway", "core", "am"))
    if not casa:
        answer("The core, the gateway and AppManagement come back by themselves", "OPEN", "no active casaos service in units-before")
        open_q("the core coming back: units-before lists no casaos service")
    else:
        answer("The core, the gateway and AppManagement come back by themselves", "GO" if unaided else "NO-GO",
               ("all %d casaos units active and not restarted by the harness; first answer after the first sample of the box: gateway %s, core %s, AppManagement %s" % (
                   len(casa), fmt_s(answered.get("gateway")), fmt_s(answered.get("core")), fmt_s(answered.get("am")))) if unaided else
               "not active after the reboot: %s; restarted after the first look: %s; answered: %s" % (", ".join(down_after) or "none", ", ".join(moved) or "none", ", ".join("%s %s" % (k, fmt_s(v)) for k, v in answered.items()) or "n/a"))
    # a token issued before the hop
    claims = {}
    try:
        claims = json.loads(read("jwt-claims-jwt-before.json") or "{}")
    except ValueError:
        invalid("jwt-claims-jwt-before.json is not JSON")
    exp = (claims.get("claims") or {}).get("exp")
    t_check = mt("after_settled") or mt("after_start")
    if codes["jwt"] == "200":
        tok = "accepted"
    elif codes["jwt"] in ("401", "403") and isinstance(exp, (int, float)) and t_check is not None and exp < t_check:
        tok = "expired"
    elif codes["jwt"] in ("401", "403"):
        tok = "rejected"
    else:
        tok = "unknown (%s)" % (codes["jwt"] or "no answer")
    fresh = read("jwt-fresh.code").strip()
    answer("A token issued before the hop is still accepted afterwards (the browser session survives the reboot)", {"accepted": "GO", "rejected": "NO-GO"}.get(tok, "OPEN"),
           "the token of the baseline: %s%s; a new login afterwards: %s" % (tok, (" (it expired at %s)" % datetime.utcfromtimestamp(exp).strftime("%H:%M:%S")) if tok == "expired" and exp else "", "accepted" if fresh == "200" else "NOT accepted (%s)" % fresh))
    if tok.startswith(("expired", "unknown")):
        open_q("the browser session surviving the reboot: the token of the baseline %s" % tok)

# ---- the hop's own result ---------------------------------------------------------------------------------------------------------------------------

if ran_hop:
    conv = None
    ladder = "recover_full" in steps and steps["recover_full"]["rc"] == 0
    if DEBIAN:
        conv_steps = sorted(n for n in steps if n.startswith("converge"))
        conv = bool(conv_steps) and bool(re.search(r"^0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded\.$", step_text(conv_steps[-1]), re.M))
    bad_steps = [n for n, s in steps.items() if s["rc"] not in (0, None) and not n.startswith(("recover", "converge", "catchup"))]
    if ladder:
        bad_steps = [n for n in bad_steps if n != "full"]
    version_ok = after_os.get("VERSION_ID") == L["new"] and after_os.get("VERSION_CODENAME") == L["new_code"]
    clean = audit_clean("after") is True and aptcheck_ok("after") is True
    ev = ["os-release says %s %s (%s)" % (after_os.get("ID"), after_os.get("VERSION_ID"), after_os.get("VERSION_CODENAME")), "dpkg --audit and apt-get check %s" % ("clean" if clean else "NOT clean")]
    if DEBIAN:
        ev.append("the simulation converged to 0/0/0/0: %s" % ("yes" if conv else "NO"))
    if bad_steps:
        ev.append("steps that did not exit 0: %s" % ", ".join("%s (%s)" % (n, steps[n]["rc"]) for n in bad_steps))
    if ladder:
        ev.append("the full upgrade failed once and the notes' failure ladder (dpkg --configure -a, apt-get -f install, dist-upgrade) completed it")
    if not unit_done:
        ev.append("the unit did not reach DONE")
    ok = version_ok and clean and (conv if DEBIAN else True) and unit_done and not bad_steps
    answer("The hop did what it set out to (%s)" % head_name, "GO" if ok else "NO-GO", "; ".join(ev))
    if DEBIAN:
        P("")
        P("### The sources, before and after the unit's rewrite")
        P("")
        diff = list(difflib.unified_diff(read("sources-before.txt").splitlines(), read("sources-after.txt").splitlines(), "before", "after", lineterm="", n=0))
        for ln in diff[:40]:
            P("    " + ln[:160])
        P("")
        paused_list = read("sources-paused.txt").split()
        P("Sources paused (renamed .casaos-paused) for the hop: %s." % (", ".join(paused_list) or "none"))
        P("")

# ---- the Ubuntu tool ------------------------------------------------------------------------------------------------------------------------------

if not DEBIAN:
    P("## Canonical's side")
    P("")
    chk = read("ubuntu-check.txt").strip()
    P("`LC_ALL=C do-release-upgrade -c`, exit %s: `%s`" % (read("ubuntu-check.rc").strip(), " ".join(chk.split())[:240]))
    P("")
    meta = read("meta-release.txt")
    stanzas = [dict(re.findall(r"^([A-Za-z-]+): (.*)$", b, re.M)) for b in meta.split("\n\n")]
    on_offer = [(z["Dist"], z.get("Version", "?"), z.get("Supported", "?")) for z in stanzas if z.get("Dist") in (L["old_code"], L["new_code"])]
    if on_offer:
        P("meta-release-lts (what Canonical says is on offer): " + "; ".join("%s %s%s" % (n, v, "" if s == "1" else " (Supported: %s)" % s) for n, v, s in on_offer))
    fact("meta-release-lts for %s and %s" % (L["old_code"], L["new_code"]), "; ".join("%s %s Supported=%s" % x for x in on_offer) or "not read, or not listed (exit %s)" % read("meta-release.rc").strip())
    tb = read("upgrader-tarball.txt")
    lm = re.search(r"^[Ll]ast-[Mm]odified: (.*)$", tb, re.M)
    fact("the dist-upgrader tarball of %s: Last-Modified" % L["new_code"], lm.group(1).strip() if lm else "not read (%s)" % (rc_of("upgrader-tarball"),))
    fact("ubuntu-release-upgrader-core on the box", BOX.get("upgrader", "?"))
    pro = read("pro-status.txt")
    fact("Ubuntu Pro / ESM", "attached" if re.search(r'"attached":\s*true', pro) else "not attached (a cloud image, no token): the behaviour with Pro attached was NOT measured" if re.search(r'"attached":\s*false', pro) else "not readable (%s)" % (" ".join(pro.split())[:100] or "no output"))
    open_q("Ubuntu Pro/ESM attached: not measured (no token in a CI guest)")
    fact("do-release-upgrade -c asked again inside the unit, before the upgrade", "exit %s: %s" % (read("ubuntu-check-unit.rc").strip() or "not asked", " ".join(read("ubuntu-check-unit.txt").split())[:140]))
    for tagf in ("release-upgrades-before.txt", "release-upgrades-after.txt"):
        pr = re.findall(r"^(Prompt=\S+)", read(tagf), re.M)
        fact("/etc/update-manager/%s" % tagf.replace(".txt", "").replace("release-upgrades-", "release-upgrades (") + ")", ", ".join(pr) or "no Prompt line")
    mainlog = [ln for ln in read("dist-upgrade-main.log").splitlines() if ln.strip()][:3]
    fact("the first lines of /var/log/dist-upgrade/main.log (the tool's own version is in them)", " / ".join(x[:100] for x in mainlog) or "no main.log was copied")
    P("")
    if skipped or offered is False:
        answer("The Ubuntu tool can be driven from a unit, and what it does to Docker", "NOT_OFFERED",
               "Canonical has not opened %s -> %s on this box (or has suspended it): `do-release-upgrade -c` exit %s. Neutral and visible; neither a pass nor a failure" % (L["old"], L["new"], read("ubuntu-check.rc").strip()))
        open_q("everything about the Ubuntu hop: it was not offered, so nothing was run")
    elif ran_hop:
        up_rc = steps.get("upgrader", {}).get("rc")
        kvc = kv("controls.txt")
        variant = kvc.get("variant", "?")
        samples = [ln for ln in read("hop-sample.log").splitlines() if "screen=" in ln]
        screen = [s for s in samples if "screen=1" in s]
        s1022 = [s for s in samples if "sshd1022=1" in s]
        tty = kv("unit-start.txt").get("tty", "")
        version_ok = after_os.get("VERSION_ID") == L["new"] and after_os.get("VERSION_CODENAME") == L["new_code"]
        clean = audit_clean("after") is True and aptcheck_ok("after") is True
        driven = unit_done and up_rc is not None and version_ok and clean
        P("The unit's `tty`: `%s`. Samples every 20 s of `screen` and of an sshd on 1022 while the tool ran: %d samples, screen seen in %d, sshd 1022 seen in %d." % (tty, len(samples), len(screen), len(s1022)))
        P("")
        answer("The Ubuntu tool can be driven from a unit (no terminal, no screen, no sshd on 1022)", "NO-GO" if (not driven or screen or s1022) else "GO" if samples else "OPEN",
               "tool exit %s, os-release %s %s, dpkg --audit and apt-get check %s; screen %s, sshd 1022 %s over %d samples" % (
                   up_rc, after_os.get("VERSION_ID"), after_os.get("VERSION_CODENAME"), "clean" if clean else "NOT clean", "SEEN" if screen else "not seen", "SEEN" if s1022 else "not seen", len(samples)))
        if driven and not samples:
            open_q("whether the tool starts screen or an sshd on 1022: the unit's sampler wrote no sample")
        # Docker
        d_ce = d_after.get("docker-ce")
        engine_before = [n for n in ENGINE if n in d_before]
        engine_after_unit = {n: (d_hop.get(n) is not None) for n in engine_before}
        held_core = [n for n in ENGINE_CORE if n in d_before]
        survive = d_ce is not None and bool(answers_after)
        P("### What the tool did to Docker (variant `%s`, held: %s)" % (variant, kvc.get("held", "?")))
        P("")
        P("| package | before | after the unit | after the reboot | held |")
        P("|---|---|---|---|---|")
        held_set = set(kvc.get("held", "").split())
        for n in ["osm-orphan", "osm-orphan-held"] + engine_before:
            show_row(n, d_before.get(n, "-"), d_hop.get(n, "REMOVED"), d_after.get(n, "REMOVED"), "yes" if n in held_set else "no")
        P("")
        P("Docker's source files after the unit (`sources-dir-after.txt`) and after the reboot (`sources-dir-end.txt`):")
        P("")
        for tagf in ("sources-dir-after.txt", "sources-dir-end.txt"):
            names_ = sorted({m.group(1) for m in re.finditer(r"\s(\S*docker\S*)", read(tagf))})
            P("- %s: %s" % (tagf, ", ".join(names_) or "no file with docker in its name"))
            fact("final state of Docker's apt source (%s)" % tagf, ", ".join(names_) or "none")
        P("")
        unheld_removed = [n for n in engine_before if n not in held_set and n not in d_hop]
        held_removed = [n for n in held_set if n in d_before and n not in d_hop]
        orphan_gone = "osm-orphan" not in d_hop
        orphan_held_gone = "osm-orphan-held" not in d_hop
        fact("obsolete packages are removed without a question (the control that no repository has)", "yes: osm-orphan was removed" if orphan_gone else "no: osm-orphan is still installed")
        fact("apt-mark hold on a package the tool wants to remove", ("kept: osm-orphan-held is still installed" if not orphan_held_gone else "NOT respected: osm-orphan-held was removed") + "; held engine packages removed: %s" % (", ".join(sorted(held_removed)) or "none"))
        if variant == "hold":
            answer("apt-mark hold keeps Docker's engine through the tool", "GO" if not held_removed and not orphan_held_gone else "NO-GO",
                   "held and removed: %s; the unheld Docker packages removed: %s (the same rule that removes osm-orphan: %s)" % (", ".join(sorted(held_removed)) or "none", ", ".join(unheld_removed) or "none", "removed" if orphan_gone else "not removed"))
            answer("RemoveObsoletes=no in release-upgrades.d/*.cfg is honoured", "OPEN", "not tested in this run: run it again with ubuntu_variant keep")
            open_q("RemoveObsoletes=no and release-upgrades.d/*.cfg: this run is the `hold` variant; run the workflow again with ubuntu_variant keep")
        elif variant == "keep":
            honoured = not orphan_gone
            answer("RemoveObsoletes=no in release-upgrades.d/*.cfg is honoured", "GO" if honoured else "NO-GO",
                   "osm-orphan (held by nothing) is %s; Docker's packages removed: %s" % ("still installed" if honoured else "REMOVED anyway", ", ".join(unheld_removed) or "none"))
            answer("apt-mark hold keeps Docker's engine through the tool", "OPEN", "not tested in this run: run it again with ubuntu_variant hold")
            open_q("apt-mark hold on the engine: this run is the `keep` variant; run the workflow again with ubuntu_variant hold")
        answer("What the tool does to Docker: the engine survives (docker-ce installed, the daemon answering after the reboot)", "GO" if survive else "NO-GO",
               "docker-ce %s -> %s; the daemon %s; removed: %s%s" % (d_before.get("docker-ce"), d_ce or "REMOVED", ("answers " + dsv["after"]) if answers_after else "does NOT answer", ", ".join(sorted(n for n in engine_before if n not in d_after)) or "no engine package",
                                                                      "; the unheld ones among them: " + ", ".join(unheld_removed) if unheld_removed else ""))
        if not survive:
            open_q("Ubuntu hop: Docker did not survive in this variant; the design's stop criterion ('Docker cannot be protected') is not decided until the other variant has been run")

# ---- reboot facts ----------------------------------------------------------------------------------------------------------------------------------------

# ---- the end ---------------------------------------------------------------------------------------------------------------------------------------------

# the four rows of the design's table first, in its order, then the rest
ORDER = ("The sandbox plan works", "The docker-ce built for", "The core, the gateway", "The Ubuntu tool can be driven from a unit", "What the tool does to Docker", "apt-mark hold", "RemoveObsoletes",
         "The transient unit", "A token issued", "The product can rely", "The hop did what")


def order_key(row):
    for i, prefix in enumerate(ORDER):
        if row[0].startswith(prefix):
            return i
    return len(ORDER)


def finish():
    rows_out.sort(key=order_key)
    not_offered = bool(not DEBIAN and (skipped or offered is False)) and not problems
    head = "INVALID RUN" if problems else "NOT_OFFERED" if not_offered else "MEASURED"
    text = ["# OS upgrade measure, %s (%s): %s" % (leg, head_name, head), ""]
    if problems:
        text += ["Nothing below that depends on the missing or untrustworthy evidence can be believed, and there is no go/no-go table for an invalid run:", ""]
        text += ["- INVALID: " + p for p in problems] + [""]
    if not_offered:
        text += ["**NOT_OFFERED**: Canonical's `do-release-upgrade -c` does not offer Ubuntu %s on this box. The leg measured the old release (and said so below) and did not run the hop: neutral, and not a pass." % L["new"], ""]
    text += lines
    if not problems:
        text += ["## The go/no-go table (os-upgrade-design.md, section 8)", "", "| Question | Answer | What was measured |", "|---|---|---|"]
        text += ["| %s | **%s** | %s |" % (q, a, e.replace("|", "/")) for q, a, e in rows_out]
        text += ["", "## Other measurements", "", "| What | Result |", "|---|---|"]
        text += ["| %s | %s |" % (w, str(v).replace("|", "/")) for w, v in facts]
        text += ["", "## What could not be decided", ""]
        text += ["- " + u for u in undecided] if undecided else ["- nothing that this leg was meant to decide was left open"]
        text += ["- whatever the answers above: a cloud image in QEMU does not prove the names of real network cards, firmware, DKMS drivers, RAID/LVM/LUKS or ZFS roots, real BIOS/UEFI boot, a real power cut, slow disks, arm64 or third-party repositories (os-upgrade-design.md, section 8)"]
    out = "\n".join(text) + "\n"
    try:
        with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
            f.write(out)
    except OSError:
        pass
    print(out)
    sys.exit(1 if problems else 0)


finish()
