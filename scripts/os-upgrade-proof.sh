#!/usr/bin/env bash
# Proof that the ENGINE of the operating system upgrade of ReCasaOS (the unit that runs the blocks of the hop, the core that reads its log and answers the page's calls:
# /v1/sys/os/upgrade/{status,beta,refresh,plan,start,abort,ack,reboot,resume}) does what it says, on a real machine that boots, through a real reboot, and with real faults.
#
# The commands of the hop were measured (os-upgrade-measure.sh), the plan was proved (os-plan-proof.sh); the machinery around the commands (the driver in its transient unit, the log of
# markers, the reducer, the checks before the restart, the judge after it) was only ever run with stand-ins. This runs the REAL engine, through the REAL core's API, under a real systemd,
# on a Debian 11 guest, across a real restart.
#
# Unlike os-plan-proof.sh this script runs ON THE CI RUNNER and drives the guest: the guest reboots, is reset and has its network cut in the middle of a case, and the evidence has to be
# on the side that survives it. The calls to the core go through the gateway (QEMU forwards a port of the runner to the guest's port 80) with the token of a login of the box's own, the way
# the page makes them; what only the guest can say is asked of it with one verb at a time (ssh, a small POSIX sh tool that the preparation writes on the guest: dash is what runs there
# while the hop replaces bash and libc). Nothing is judged here: it writes what it saw into $OUT, and os-upgrade-proof-report.py turns that into PASS, FAIL, NOT TESTABLE (with its reason)
# or INVALID RUN. It stops early (and says why, in $OUT/aborted) only when it cannot go on; a step whose undo failed is recorded as such (invalid.txt), never as a failure of the feature.
#
#   os-upgrade-proof.sh guest-prepare     IN THE GUEST, as root: Docker 28.0.4 and the published ReCasaOS are installed as os-plan-proof.sh does it, the witness containers start,
#                                         the timers of apt are on and quiet, the tools of the proof are written
#   os-upgrade-proof.sh prepare           on the runner: pushes the files, runs guest-prepare, fetches what it wrote, makes the box's user and logs in
#   os-upgrade-proof.sh run core          on the runner: the path of a person who runs the upgrade, in this order
#   os-upgrade-proof.sh run faults        on the runner: the start state of the faults, a snapshot of the whole machine, and the four faults
#
# Leg core:
#   C1 flag off      a CONTROL window (what the box changes by itself between two snapshots), then, with /etc/casaos/os-upgrade.beta absent: the status, a start and a refresh; nothing of the
#                    engine may have been made or started
#   C2 switch on     POST beta
#   C3 refresh       the refresh of Debian 11 (and the restart it may ask for)
#   C4 plan, rehearsal   the plan, a rehearsal to `rehearsed` (the sources of Debian 12 in place, the timers stopped), the discard, the close
#   C5 refusals      a wrong hostname, a missing acknowledgement, dpkg's lock held, a second start while a run is open, the page's restart while the unit works
#   C6 call-off      the hop is started and called off in block 3 or 4: the machine is put back
#   C7 the hop       the real one: the blocks, the restart, the check, the close; the first real numbers of the engine
# Leg faults (every fault starts from a snapshot of the whole guest, memory included, taken after C4, so the plan is still live):
#   F1 the unit killed before the point of no return   F2 the unit killed after it, with dpkg at work   F3 the network cut after it   F4 a power cut (the QEMU monitor's reset) in block 6, with dpkg at work
# Each fault starts with the clock of the restored guest set from the runner's; the verbs that change the guest (the kill, the cut, the clock) are said up to three times and their effect is seen.
#
# The helpers of the other proofs are reused, not rewritten: os-readiness-proof.sh is SOURCED in the guest (Docker, the installer, the witnesses, the waits for the services, the
# tolerant apt-get update, the dpkg lock holder), and the preparation is os-plan-proof.sh's main() up to its window, copied with the changes this proof needs (marked where they are).
# Needs, on the runner: bash 5 (EPOCHREALTIME), curl, python3, ssh, timeout. On the guest: what the preparation installs.
#
# Environment (runner): OUT (default ./oup-out), WORK (default ./oup-work: the token, never in OUT), SSH_CONFIG (ssh_config) and SSH_HOST (vm), GW_PORT (8080: the runner's end of the
# forward to the guest's port 80), MON_SOCK (mon.sock: the QEMU monitor), CASES (a comma list of the cases to run, c1..c7 or f1..f4; all by default), OUP_FAST and OUP_TICK (the tests of
# this script skip the sleeps and tell their stand-in of the guest how much time went by), and the waits and sleeps below.

# shellcheck disable=SC2034
# (the fields of a poll are read into P_* whether or not a case looks at each of them)
OUP_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export LC_ALL=C

# ---- the parts that are only text, and can be tested without a machine -----------------------------------------------------------------

STATUS_PATH=/v1/sys/os/upgrade/status
BETA_PATH=/v1/sys/os/upgrade/beta
REFRESH_PATH=/v1/sys/os/upgrade/refresh
PLAN_PATH=/v1/sys/os/upgrade/plan
START_PATH=/v1/sys/os/upgrade/start
ABORT_PATH=/v1/sys/os/upgrade/abort
ACK_PATH=/v1/sys/os/upgrade/ack
REBOOT_PATH=/v1/sys/os/upgrade/reboot
RESUME_PATH=/v1/sys/os/upgrade/resume
RESTART_PATH=/v1/sys/state/restart
GUEST_TOOLS_DIR=/usr/local/lib/oup
PROOF_USER=oupproof

# write_guest_tools <dir>: what the runner asks of the guest, one verb a call. POSIX sh (dash): the hop replaces bash and libc while some of these run. The `snap` verb prints the
# machine in sections ("@@ <name>" lines, the last one "@@ end": a snapshot that was cut short has no end and is not believed).
write_guest_tools() {
    mkdir -p "$1"
    cat >"$1/guest.sh" <<'OUP_GUEST'
#!/bin/sh
# What the proof asks of the guest, one verb a call. POSIX sh (dash).
LC_ALL=C; export LC_ALL
PATH=/usr/sbin:/usr/bin:/sbin:/bin; export PATH
RUN=/var/lib/casaos/os-upgrade
LIB=/usr/local/lib/oup
OSR=/usr/local/lib/osr
UNIT=casaos-os-upgrade.service
TIMERS="apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service"
# what a login makes under /run/systemd (a scope, a slice and drop-ins, under a new number every time, this tool's own ssh login included) is not a change of the machine
NAMES_SKIP='^/run/systemd/(transient|system)/(session-|user-|user@)'

sec() { printf '@@ %s\n' "$1"; }
yn() { if "$@" >/dev/null 2>&1; then echo yes; else echo no; fi; }
in_sources() { cat /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources 2>/dev/null | grep -v '^[[:space:]]*#' | grep -c "$1" || true; }

snap() {
    sec facts
    printf 'boot_id %s\n' "$(cat /proc/sys/kernel/random/boot_id)"
    printf 'uptime_s %s\n' "$(cut -d. -f1 /proc/uptime)"
    printf 'uname %s\n' "$(uname -r)"
    printf 'hostname %s\n' "$(hostname)"
    (. /etc/os-release 2>/dev/null; printf 'version_id %s\nversion_codename %s\n' "${VERSION_ID:-}" "${VERSION_CODENAME:-}")
    for u in $TIMERS; do
        a="$(systemctl is-active "$u" 2>/dev/null)"
        e="$(systemctl is-enabled "$u" 2>/dev/null)"
        printf 'timer %s %s %s\n' "$u" "${a:-none}" "${e:-none}"
    done
    printf 'busy_flag %s\n' "$(yn test -e "$RUN/busy")"
    printf 'run_dir %s\n' "$(yn test -e "$RUN")"
    printf 'beta_file %s\n' "$(yn test -e /etc/casaos/os-upgrade.beta)"
    printf 'timers_state_file %s\n' "$(yn test -e /var/backups/casaos-os-upgrade-timers.state)"
    printf 'bk_dirs %s\n' "$(find /var/backups -maxdepth 1 -type d -name 'casaos-os-upgrade-*' 2>/dev/null | sort | tr '\n' ' ')"
    printf 'run_bk %s\n' "$(find "$RUN" -maxdepth 2 -type d \( -name bk-done -o -name bk-aborted \) 2>/dev/null | sort | tr '\n' ' ')"
    printf 'sources_bookworm %s\nsources_bullseye %s\n' "$(in_sources bookworm)" "$(in_sources bullseye)"
    printf 'df_root_kb %s\n' "$(df -Pk / | awk 'NR == 2 {print $4}')"
    printf 'df_boot_kb %s\n' "$(df -Pk /boot | awk 'NR == 2 {print $4}')"
    printf 'rx_bytes %s\n' "$(awk -F'[: ]+' 'NR > 2 && $2 != "lo" {s += $3} END {print s + 0}' /proc/net/dev)"
    printf 'cache_bytes %s\n' "$(du -sb /var/cache/apt/archives 2>/dev/null | cut -f1)"
    printf 'witnesses %s\n' "$(docker ps --format '{{.Names}}' 2>/dev/null | sort | tr '\n' ',')"
    printf 'failed_units %s\n' "$(systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}' | sort | tr '\n' ' ')"
    printf 'system_state %s\n' "$(systemctl is-system-running 2>/dev/null)"
    printf 'dpkg_lock %s\n' "$(python3 "$OSR/lockprobe.py" /var/lib/dpkg/lock-frontend 2>/dev/null)"
    printf 'dpkg_audit %s\n' "$(dpkg --audit 2>&1 | wc -l)"
    printf 'reboot_required %s\n' "$(yn test -e /var/run/reboot-required)"
    sec services
    systemctl list-units --type=service --all --plain --no-legend 'casaos*' 'ssh*' 'docker*' 'containerd*' 'smbd*' 'cron*' 2>/dev/null | awk '{print $1, $3, $4}' | sort
    sec dpkg
    dpkg-query -W -f='${Package}\t${Version}\t${db:Status-Abbrev}\n' 2>/dev/null | sort
    sec aptfiles
    find /etc/apt \( -type f -o -type l \) 2>/dev/null | sort | while IFS= read -r f; do
        if [ -L "$f" ]; then
            printf '%s\tlink:%s\n' "$f" "$(readlink "$f")"
        else
            printf '%s\t%s\n' "$f" "$(sha256sum <"$f" | cut -d' ' -f1)"
        fi
    done
    sec names
    find /etc /var/lib/casaos /var/backups /var/lib/dpkg /var/lib/apt /var/cache/apt /run/systemd/transient /run/systemd/system -xdev 2>/dev/null | sort | grep -Ev "$NAMES_SKIP"
    units_section
    pid1_section
    runs_section
    sec end
}

# The sections that are empty when all is well say that they were asked ("# asked", "# lines <n>"): an empty section that nothing wrote is not a machine without a unit of the engine.
units_section() {
    sec units
    if u="$(systemctl list-units --all --plain --no-legend 'casaos-os-upgrade*' 2>/dev/null)"; then
        echo "# asked"
        [ -z "$u" ] || printf '%s\n' "$u"
    fi
}

pid1_section() {
    sec pid1
    j="$(journalctl -b -o cat _PID=1 --no-pager 2>/dev/null)"
    n=0
    [ -z "$j" ] || n="$(printf '%s\n' "$j" | wc -l | tr -d ' ')"
    printf '# lines %s\n' "$n"
    if [ "$n" -gt 0 ]; then printf '%s\n' "$j" | grep casaos-os-upgrade || true; fi
}

# the kept runs: for each, the folder of its backup (bk-path), whether it is still there, and where it was moved to (bk-done, bk-aborted)
runs_section() {
    sec runs
    for d in "$RUN"/*/; do
        [ -d "$d" ] || continue
        p="$(cat "${d}bk-path" 2>/dev/null)"
        left=no
        if [ -n "$p" ] && [ -e "$p" ]; then left=yes; fi
        moved=-
        if [ -d "${d}bk-aborted" ]; then moved=bk-aborted; fi
        if [ -d "${d}bk-done" ]; then moved=bk-done; fi
        printf '%s\t%s\t%s\t%s\n' "$(basename "$d")" "${p:--}" "$left" "$moved"
    done
}

lock_start() {
    nohup python3 "$OSR/lockhold.py" /var/lib/dpkg/lock-frontend 900 >"$LIB/lock.out" 2>&1 </dev/null &
    echo $! >"$LIB/lock.pid"
    i=0
    while [ "$i" -lt 20 ]; do
        if grep -q locked "$LIB/lock.out" 2>/dev/null; then echo held; return 0; fi
        sleep 0.5
        i=$((i + 1))
    done
    echo "the lock could not be taken: $(cat "$LIB/lock.out" 2>/dev/null)"
    return 1
}

lock_stop() {
    if [ -s "$LIB/lock.pid" ]; then kill "$(cat "$LIB/lock.pid")" 2>/dev/null; : >"$LIB/lock.pid"; fi
    i=0
    while [ "$i" -lt 20 ]; do
        if [ "$(python3 "$OSR/lockprobe.py" /var/lib/dpkg/lock-frontend 2>/dev/null)" = free ]; then echo free; return 0; fi
        sleep 0.5
        i=$((i + 1))
    done
    echo "the lock is still held"
    return 1
}

# the routes that carry the guest out: every default route of both families, and the site-local prefix (fec0::/64) that the router advertisement of QEMU's slirp brings
routes() {
    ip -4 route show default | sed 's/^/4 /'
    ip -6 route show 2>/dev/null | grep -E '^(default|fec0:)' | sed 's/^/6 /'
}

# route_cut: remembers the routes (once: said again it keeps what the first time saved), takes them all away, and proves the cut with a request from the guest that has to fail
route_cut() {
    if [ ! -s "$LIB/route.saved" ]; then routes >"$LIB/route.saved"; fi
    if [ ! -s "$LIB/route.saved" ]; then echo "no default route"; return 3; fi
    while [ -n "$(ip -4 route show default)" ] && ip -4 route del default; do :; done
    while [ -n "$(ip -6 route show default)" ] && ip -6 route del default; do :; done
    while ip -6 route show | grep -q '^fec0:' && ip -6 route del fec0::/64; do :; done
    if curl -sS -m 10 -o /dev/null http://deb.debian.org/debian/dists/bookworm/InRelease 2>/dev/null; then
        echo "still reachable"
        return 1
    fi
    echo cut
}

# route_restore: puts back what route_cut took away (the IPv4 default route is what must be there after; the others come back with the router advertisement if they do not)
route_restore() {
    had="$(ip -4 route show default)"
    if [ -s "$LIB/route.saved" ]; then
        while read -r fam rest; do
            # shellcheck disable=SC2086
            ip -"$fam" route add $rest 2>/dev/null || true
        done <"$LIB/route.saved"
    fi
    if [ -z "$(ip -4 route show default)" ]; then
        echo "no default route"
        return 1
    fi
    : >"$LIB/route.saved"
    if [ -n "$had" ]; then echo present; else echo restored; fi
}

# unit_id: the invocation of the unit of the engine, nothing when none is active. The unwind and the repair of a stop run under the same name, as another invocation.
unit_id() { systemctl show -p InvocationID --value "$UNIT" 2>/dev/null; }

# kill_unit <invocation>: kills the unit only while it is the invocation that was asked for, so that it can be said again (a second try after a lost reply must not kill the unwind that took the name),
# says whether dpkg was at work, and ends with "gone" or "alive"
kill_unit() {
    want="$1"
    if [ -z "$want" ]; then echo "no invocation given"; return 2; fi
    if [ "$(unit_id)" = "$want" ]; then
        if pgrep -x dpkg >/dev/null 2>&1; then echo "dpkg_at_kill yes"; else echo "dpkg_at_kill no"; fi
        systemctl kill "$UNIT"
        echo "rc $?"
    fi
    i=0
    while [ "$i" -lt "${KILL_WAIT:-100}" ]; do
        if [ "$(unit_id)" != "$want" ]; then echo gone; return 0; fi
        sleep 1
        i=$((i + 1))
    done
    echo alive
    return 1
}

# dpkg_wait <seconds>: looks every second for a dpkg at work and says "seen <second>" when it has been there for three looks in a row (a dpkg that comes and goes is not the one to cut off in the middle),
# "never <seconds>" when it was not
dpkg_wait() {
    max="${1:-180}"
    i=0
    run=0
    while [ "$i" -lt "$max" ]; do
        if pgrep -x dpkg >/dev/null 2>&1; then run=$((run + 1)); else run=0; fi
        if [ "$run" -ge 3 ]; then echo "seen $i"; return 0; fi
        sleep 1
        i=$((i + 1))
    done
    echo "never $max"
    return 1
}

# set_clock <epoch>: a guest that was restored from a snapshot has the clock of the snapshot, and a Release file of a mirror dated after it is not valid yet. The services that would fight the step are
# stopped for it and started again, and the seconds the clock was behind are said
set_clock() {
    case "$1" in '' | *[!0-9]*) echo "no epoch"; return 2 ;; esac
    before="$(date -u +%s)"
    stopped=""
    for svc in systemd-timesyncd chrony chronyd; do
        if systemctl is-active --quiet "$svc" 2>/dev/null; then
            systemctl stop "$svc" 2>/dev/null
            stopped="$stopped $svc"
        fi
    done
    if ! date -u -s "@$1" >/dev/null; then echo "the clock could not be set"; return 1; fi
    now="$(date -u +%s)"
    for svc in $stopped; do systemctl start "$svc" 2>/dev/null; done
    echo "skew $(($1 - before))"
    off=$((now - $1))
    if [ "$off" -lt 0 ]; then off=$((0 - off)); fi
    if [ "$off" -gt 10 ]; then echo "the clock is $off s from the runner's"; return 1; fi
    echo set
}

runfiles() {
    echo "== $RUN"
    ls -la "$RUN" 2>&1
    l="$(ls -td "$RUN"/*/ 2>/dev/null | sed -n 1p)"
    if [ -n "$l" ]; then
        echo "== newest run folder: $l"
        ls -la "$l" 2>&1
        echo "== markers"
        sed -E 's/^(CASAOS_OSU_[A-Z_]+) [0-9a-f]+ /\1 NONCE /' "${l}markers" 2>&1 | tail -n 200
        echo "== output.log (end)"
        tail -n 150 "${l}output.log" 2>&1
    fi
    echo "== backups"
    find /var/backups -maxdepth 1 -name 'casaos-os-upgrade-*' 2>&1
    echo "== units"
    systemctl list-units --all --plain --no-legend 'casaos-os-upgrade*' 2>&1
}

diag() {
    systemctl --failed --no-pager 2>&1
    echo "== core"
    journalctl -u casaos.service -u casaos-gateway.service -u casaos-app-management.service --no-pager -o short-iso 2>&1 | tail -n 100
    echo "== unit"
    journalctl -u casaos-os-upgrade.service --no-pager -o short-iso 2>&1 | tail -n 60
    echo "== df"
    df -h 2>&1
    echo "== dpkg --audit"
    dpkg --audit 2>&1 | head -n 30
}

verb="${1:-}"
[ "$#" -eq 0 ] || shift
case "${verb}" in
boot_id) cat /proc/sys/kernel/random/boot_id ;;
hostname) hostname ;;
snap) snap ;;
flag) yn test -e "$RUN/busy" ;;
unit) systemctl is-active "$UNIT" 2>/dev/null || true ;;
unit_id) unit_id ;;
kill_unit) kill_unit "$@" ;;
dpkg_wait) dpkg_wait "$@" ;;
set_clock) set_clock "$@" ;;
lock_start) lock_start ;;
lock_stop) lock_stop ;;
lock_state) python3 "$OSR/lockprobe.py" /var/lib/dpkg/lock-frontend ;;
route_cut) route_cut ;;
route_restore) route_restore ;;
net_check) curl -fsS -m 20 -o /dev/null -w '%{http_code}\n' http://deb.debian.org/debian/dists/bookworm/InRelease ;;
runfiles) runfiles ;;
diag) diag ;;
*) echo "unknown verb: ${verb}" >&2; exit 2 ;;
esac
OUP_GUEST
    chmod 755 "$1/guest.sh"
}

# write_oup_py <file>: the JSON of the core's answers, read and written by Python (the runner has no jq the proof can rely on). Subcommands:
#   get FILE PATH                      a value of a JSON file by its dotted path ("-" when it is not there)
#   row FILE HTTP CURL FLAG T0 T1      the row of a poll of the status (polls-<tag>.tsv)
#   planrow FILE HTTP CURL T0 T1       the row of a poll of the plan
#   startbody PLANFILE HOST REHEARSAL ACKS NET   the body of a start, built from the plan as the page builds it (ACKS: full or none; NET: the safety net, empty for the rehearsal of the page)
#   snapsplit FILE PREFIX              the sections of a snapshot into PREFIX.<section> (status 1 when it has no end)
write_oup_py() {
    cat >"$1" <<'OUP_PY'
import json
import re
import sys


def load(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return None


def show(v):
    if v is None:
        return "-"
    if isinstance(v, str):
        return v if v != "" else "-"
    return json.dumps(v, separators=(",", ":"))


def clean(s):
    return re.sub(r"[\t\r\n]+", " ", s)


def walk(d, path):
    for k in path.split("."):
        d = d[int(k)] if isinstance(d, list) else d[k]
    return d


def data_of(path):
    body = load(path)
    d = body.get("data") if isinstance(body, dict) else None
    return d if isinstance(d, dict) else {}


def sub(d, key):
    v = d.get(key)
    return v if isinstance(v, dict) else {}


def cmd_get(path, dotted):
    try:
        print(clean(show(walk(load(path), dotted))))
    except Exception:
        print("-")


def cmd_row(path, http, curl, flag, t0, t1):
    d = data_of(path)
    blk, fail, hint = sub(d, "block"), sub(d, "failure"), sub(d, "hint")
    acts = d.get("actions")
    acts = ",".join(a for a in acts if isinstance(a, str)) if isinstance(acts, list) and acts else "-"
    cells = [t0, t1, http, curl, d.get("state"), d.get("reason"), d.get("cause"), blk.get("index"), blk.get("id"), d.get("pnr"), d.get("half_upgraded"), d.get("busy"),
             d.get("attempt"), fail.get("class"), hint.get("code"), flag, d.get("run_id"), d.get("beta_enabled"), acts]
    print("\t".join(clean(show(c)) for c in cells))


def cmd_planrow(path, http, curl, t0, t1):
    d = data_of(path)
    print("\t".join(clean(show(c)) for c in (t0, t1, http, curl, d.get("state"), d.get("phase"))))


def cmd_startbody(path, host, rehearsal, acks, net):
    plan = data_of(path).get("plan")
    if not isinstance(plan, dict):
        sys.exit(1)
    removals = sorted({p["data"]["name"] for p in plan.get("problems") or [] if p.get("code") == "removal_manual" and isinstance(p.get("data"), dict) and p["data"].get("name")})
    paused = sorted({x["file"] + (":%d" % x["line"] if x.get("line", 0) > 0 else "") for x in plan.get("paused") or []})
    reh = rehearsal == "true"
    given = ["no_interruption"] + ([] if reh else ["no_way_back"]) + (["sources_paused"] if paused else []) + (["removals_read"] if removals else [])
    body = {"plan_id": plan.get("plan_id"), "rehearsal": reh, "safety_net": net, "hostname": host, "accept_removals": removals, "paused_sources": paused,
            "acks": given if acks == "full" else []}
    print(json.dumps(body, separators=(",", ":")))


def cmd_snapsplit(path, prefix):
    sections, name, seen_end = {}, None, False
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("@@ "):
                name = line[3:].strip()
                sections.setdefault(name, [])
                seen_end = seen_end or name == "end"
            elif name is not None:
                sections[name].append(line)
    if not seen_end or "facts" not in sections:
        sys.exit(1)
    for name, lines in sections.items():
        if name != "end":
            with open("%s.%s" % (prefix, name), "w", encoding="utf-8", newline="\n") as f:
                f.write("".join(l + "\n" for l in lines))


COMMANDS = {"get": (cmd_get, 2), "row": (cmd_row, 6), "planrow": (cmd_planrow, 5), "startbody": (cmd_startbody, 5), "snapsplit": (cmd_snapsplit, 2)}
if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS or len(sys.argv) - 2 != COMMANDS[sys.argv[1]][1]:
    sys.stderr.write("usage: oup.py get|row|planrow|startbody|snapsplit ...\n")
    sys.exit(2)
COMMANDS[sys.argv[1]][0](*sys.argv[2:])
OUP_PY
}

# ---- the guest: its preparation ------------------------------------------------------------------------------------------------------

# guest_prepare: os-plan-proof.sh's main() up to its window, with these changes: the timers of apt are put back ON at the end (the engine stops them and starts them again, which a
# box that has them off cannot show), with a fragment that makes what they run do nothing (APT::Periodic at 0), so that no apt run of the box's own falls in the middle of a case; the
# guest tools of this proof are written; the gateway must be on port 80 (the runner forwards one port).
guest_prepare() {
    [[ "${OUP_DISPOSABLE:-}" == 1 ]] ||
        { echo "This installs Docker, takes the box through an operating system upgrade, cuts its network and restarts it: run it on a disposable machine (OUP_DISPOSABLE=1)." >&2; exit 2; }
    [ "$(id -u)" -eq 0 ] || { echo "Run it as root." >&2; exit 2; }
    if [ -e /etc/casaos ] || [ -e /var/lib/casaos ] || command -v docker >/dev/null 2>&1; then
        echo "This machine already runs ReCasaOS or Docker: not a fresh one, not touching it." >&2
        exit 2
    fi
    # shellcheck source=os-readiness-proof.sh
    . "${READINESS_LIB:-${OUP_HERE}/os-readiness-proof.sh}"
    FAMILY=debian FROM_VERSION=11 FROM_CODENAME=bullseye TO_VERSION=12 TO_CODENAME=bookworm
    # shellcheck source=/dev/null
    . "${OS_RELEASE}"
    test "${ID}" = "${FAMILY}"
    test "${VERSION_ID}" = "${FROM_VERSION}"
    test "${VERSION_CODENAME}" = "${FROM_CODENAME}"

    export LC_ALL=C
    OUT="${OUT:-${PWD}/oup-guest-out}"
    mkdir -p "${OUT}" "${AUTH_DIR}" "${LIBDIR}"
    chmod 700 "${AUTH_DIR}"
    : >"${OUT}/timeline.tsv"
    echo "${PRETTY_NAME}" >"${OUT}/os"
    PENDING_UNDO=""
    guard_run
    [ -n "${EPOCHREALTIME:-}" ] || abort "this bash has no EPOCHREALTIME (bash 5 is needed)"

    export DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none NEEDRESTART_SUSPEND=1
    APT=(env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3)
    marker prepare_start
    cloud-init status --wait >/dev/null 2>&1 || true
    # apt's own timers would run in the middle of the installation; they are put back at the end
    systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service >/dev/null 2>&1 || true
    if grep -Eq '^[[:space:]]*deb(-src)?[[:space:]].*//(deb|security)\.debian\.org/debian-security' "${APT_DIR}/sources.list" 2>/dev/null; then
        # out of support: the change --use-debian-archive makes, made first because Docker's own installer runs apt before ReCasaOS's does
        cp -p "${APT_DIR}/sources.list" "${APT_DIR}/sources.list.osr.bak"
        sed -i -E 's#//(deb|security)\.debian\.org/debian-security#//archive.debian.org/debian-security#' "${APT_DIR}/sources.list"
    fi
    update_old
    "${APT[@]}" install -y -q curl jq ca-certificates python3 procps iproute2 >/dev/null
    write_lock_tools "${LIBDIR}"
    write_guest_tools "${GUEST_TOOLS_DIR}"
    HOST_NAME="oup-$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    hostnamectl set-hostname "${HOST_NAME}"
    # sudo and the services look the name up: without a line for it every `sudo` of the run says "unable to resolve host"
    echo "127.0.1.1 ${HOST_NAME}" >>/etc/hosts
    # cloud-init rewrites /etc/hosts at every start (and takes this line out): sudo would then say on stderr that it cannot look the name up, to every command the runner sends
    mkdir -p /etc/cloud/cloud.cfg.d
    printf 'manage_etc_hosts: false\npreserve_hostname: true\n' >/etc/cloud/cloud.cfg.d/99-oup-hosts.cfg
    install_docker
    install_casaos
    start_things
    "${APT[@]}" update -qq
    wait_services 180 || abort "the gateway, the core and AppManagement do not all answer after the installation"
    timeout 120 systemctl is-system-running --wait >/dev/null 2>&1 || true
    box_facts
    # AppManagement's check of the images lands in /var/lib/casaos three minutes after its start: it is over before the first snapshot of the cases
    wait_image_check 240 || not_proven "AppManagement did not write ${IMAGE_UPDATES} within 240 s: its own check of the images (3 minutes after its start) could fall in a window of the proof"
    [ "${CASA_PORT}" = 80 ] || abort "the gateway is on port ${CASA_PORT}: the runner forwards the guest's port 80 only"
    # the timers of apt, on, as a box of a person who runs the upgrade has them, and quiet: the units are what the engine stops and starts again, what they would do is not the proof's business
    printf 'APT::Periodic::Update-Package-Lists "0";\nAPT::Periodic::Download-Upgradeable-Packages "0";\nAPT::Periodic::AutocleanInterval "0";\nAPT::Periodic::Unattended-Upgrade "0";\n' >"${APT_DIR}/apt.conf.d/99oup-quiet"
    systemctl enable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true
    systemctl enable --now unattended-upgrades.service >/dev/null 2>&1 || true
    sleep 5
    {
        for u in apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service; do
            echo "${u} $(systemctl is-active "${u}" 2>/dev/null || true) $(systemctl is-enabled "${u}" 2>/dev/null || true)"
        done
    } >"${OUT}/timers-on"
    marker prepare_done
    date -u +%FT%TZ >"${OUT}/prepared"
    log "prepared"
}

# ---- the runner: the clock, the guest, the core --------------------------------------------------------------------------------------

# the clock the evidence uses: the shell's own, plus the time a fast test skipped (OUP_FAST: a test of this script has no guest to wait for)
VOFF=0
now() {
    local e="${EPOCHREALTIME/[.,]/}"
    e=$((e + VOFF))
    printf '%d.%06d' $((e / 1000000)) $((e % 1000000))
}
now_s() {
    local e="${EPOCHREALTIME/[.,]/}"
    e=$((e + VOFF))
    NOW_S=$((e / 1000000))
}
# nap <seconds> (whole): a sleep that a test can skip, telling its stand-in of the guest how much time went by
nap() {
    if [ -n "${OUP_FAST:-}" ]; then
        VOFF=$((VOFF + $1 * 1000000))
        if [ -n "${OUP_TICK:-}" ]; then "${OUP_TICK}" "$1" >/dev/null 2>&1 || true; fi
    else
        sleep "$1"
    fi
}

log() { printf '[proof %(%H:%M:%S)T] %s\n' -1 "$*" | tee -a "${OUT}/steps.log" >&2; }
marker() { printf '#\t%s\t%s\n' "$(now)" "$1" >>"${OUT}/timeline.tsv"; }
# the run cannot go on: it says why and ends (status 0, so that the report is made); the report turns that into INVALID RUN
abort() { echo "$*" >>"${OUT}/aborted"; log "ABORTED: $*"; exit 0; }
# a step of the harness (not of the engine) that failed, or whose undo failed: the report makes the run invalid, never a failure of the engine
harness_invalid() { printf '%s\t%s\n' "$1" "$2" >>"${OUT}/invalid.txt"; log "INVALID: $1: $2"; }

py() { python3 "${WORK}/oup.py" "$@"; }

SSH_CONFIG="${SSH_CONFIG:-ssh_config}"
SSH_HOST="${SSH_HOST:-vm}"
SSH_LIMIT="${SSH_LIMIT:-120}"
# gssh <command...>: a command on the guest, as the login user (a hung guest costs SSH_LIMIT seconds, not the job)
gssh() { timeout -k 5 "${SSH_LIMIT}" ssh -n -F "${SSH_CONFIG}" -o ConnectTimeout=8 -o BatchMode=yes "${SSH_HOST}" "$@"; }
# g <verb> [words]: a verb of the guest's tool
g() { gssh "sudo sh ${GUEST_TOOLS_DIR}/guest.sh $*"; }
boot_id() { g boot_id 2>/dev/null | tr -d '\r\n'; }
# g3 <file> <verb> [words]: a verb of the guest's tool said up to three times (an ssh is missed now and then while dpkg keeps the four processors busy; the verbs that change the guest can be said again), until one ends
# well. What each try printed goes into <file> after a "# try n" line, what it said on stderr into <file>.err (sudo may say what it likes there: the verdicts read stdout only). Status 0: a try ended well.
g3() {
    local out="$1" try rc=1
    shift
    : >"${out}"
    : >"${out}.err"
    for try in 1 2 3; do
        echo "# try ${try}" >>"${out}"
        g "$@" >>"${out}" 2>>"${out}.err"
        rc=$?
        [ "${rc}" -ne 0 ] || return 0
        nap 3
    done
    return "${rc}"
}

# qmon <HMP command...>: the QEMU monitor (savevm, loadvm, system_reset), its reply on stdout, status 1 when it does not answer or says an error
qmon() {
    if [ -n "${QMON:-}" ]; then "${QMON}" "$@"; return; fi
    python3 - "${MON_SOCK:-mon.sock}" "$*" "${QMON_LIMIT:-600}" <<'OUP_MON'
import re
import socket
import sys

path, cmd, limit = sys.argv[1], sys.argv[2], float(sys.argv[3])
s = socket.socket(socket.AF_UNIX)
s.settimeout(limit)
s.connect(path)


def until_prompt():
    buf = b""
    while not buf.endswith(b"(qemu) "):
        d = s.recv(65536)
        if not d:
            break
        buf += d
    return buf


until_prompt()
s.sendall(cmd.encode() + b"\n")
out = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]|\r", b"", until_prompt()).decode(errors="replace")
print(out)
sys.exit(1 if re.search(r"error|unknown command|not (found|supported)", out, re.I) else 0)
OUP_MON
}

GW_PORT="${GW_PORT:-8080}"
GW="http://127.0.0.1:${GW_PORT}"
auth_header() { cat "${WORK}/auth" 2>/dev/null || echo 'Authorization: none'; }

# login: the access token of the box's own user, kept for the header in $WORK (never in $OUT); status 1 when there is none
login() {
    local pw tok
    pw="$(cat "${WORK}/pw" 2>/dev/null)" || return 1
    curl -sS --max-time 30 -X POST "${GW}/v1/users/login" -H 'Content-Type: application/json' -d "$(printf '{"username":"%s","password":"%s"}' "${PROOF_USER}" "${pw}")" \
        -o "${WORK}/login.json" 2>/dev/null || return 1
    tok="$(py get "${WORK}/login.json" data.token.access_token)"
    [ -n "${tok}" ] && [ "${tok}" != - ] || return 1
    (umask 077; printf 'Authorization: %s\n' "${tok}" >"${WORK}/auth")
}

# call <name> <GET|POST|PUT> <path> [body] [seconds]: one request through the gateway with the token. Written into $OUT: <name>.req (what was asked), .json (the body), .hdr (the headers),
# .code (the HTTP status, 000 when there was none), .err (what curl said), .t0 and .t1 (the time before and after, in seconds since the epoch), and .curl last (how curl ended: 0, or why it
# got no answer). A 401 (the token ran out: the clock of a restored guest is not the clock of the day) logs in again and asks once more. CALL_CODE has the status.
call() {
    local name="$1" method="$2" path="$3" body="${4:-}" max="${5:-45}" tries=0 hdr
    local opts
    while :; do
        tries=$((tries + 1))
        hdr="$(auth_header)"
        rm -f "${OUT}/${name}.json" "${OUT}/${name}.hdr"
        printf '%s %s\n%s\n' "${method}" "${path}" "${body}" >"${OUT}/${name}.req"
        now >"${OUT}/${name}.t0"
        opts=(-sS --max-time "${max}" -X "${method}" -D "${OUT}/${name}.hdr" -o "${OUT}/${name}.json" -w '%{http_code}' -H "${hdr}")
        if [ -n "${body}" ]; then
            opts+=(-H 'Content-Type: application/json' --data-binary "${body}")
        elif [ "${method}" != GET ]; then
            opts+=(--data-binary '')
        fi
        CALL_RC=0
        CALL_CODE="$(curl "${opts[@]}" "${GW}${path}" 2>"${OUT}/${name}.err")" || CALL_RC=$?
        now >"${OUT}/${name}.t1"
        CALL_CODE="${CALL_CODE:-000}"
        printf '%s\n' "${CALL_CODE}" >"${OUT}/${name}.code"
        printf '%s\n' "${CALL_RC}" >"${OUT}/${name}.curl"
        if [ "${CALL_CODE}" = 401 ] && [ "${tries}" -lt 2 ] && login; then
            echo relogin >>"${OUT}/${name}.notes"
            continue
        fi
        return 0
    done
}

# snap <tag>: the machine as it is now (the guest's `snap` verb), split into snap-<tag>.<section>; three tries. A snapshot that could not be had says so (snap-<tag>.failed).
snap() {
    local tag="$1" try
    for try in 1 2 3; do
        if g snap >"${OUT}/snap-${tag}.raw" 2>"${OUT}/snap-${tag}.err" && py snapsplit "${OUT}/snap-${tag}.raw" "${OUT}/snap-${tag}"; then
            rm -f "${OUT}/snap-${tag}.raw" "${OUT}/snap-${tag}.failed"
            return 0
        fi
        nap 10
    done
    echo "the guest's snapshot did not come back whole in three tries" >"${OUT}/snap-${tag}.failed"
    return 1
}

# evidence <tag>: the run folder's markers and the end of the unit's output, and the units, as the guest has them (best effort; for whoever reads the report)
evidence() { g runfiles >"${OUT}/runfiles-$1.txt" 2>&1 || true; }

# after_silence <name>: a call that begins something (a start, a refresh) and got no answer may have begun it all the same (the engine answers once it has taken the facts of the machine, which is minutes on a loaded one):
# the status says. A run that is open and that the runner does not know of ends the run of the proof, with its id: "no answer" is not "no run".
after_silence() {
    local name="$1" state
    [ "${CALL_CODE}" = 000 ] || return 0
    call "${name}-lookup" GET "${STATUS_PATH}" "" 30
    state="$(py get "${OUT}/${name}-lookup.json" data.state)"
    [ "${CALL_CODE}" = 200 ] || abort "the call ${name} got no answer in its time (curl ${CALL_RC}) and the status did not answer either: whether a run was begun is not known"
    [ "${state}" = idle ] || abort "the call ${name} got no answer in its time and a run is open that the runner does not know of (run $(py get "${OUT}/${name}-lookup.json" data.run_id), state ${state})"
    CALL_CODE=000
}

# the rows of the status polls: P_* of the latest poll
P_STATE=- P_REASON=- P_CAUSE=- P_BIDX=- P_BID=- P_PNR=- P_HALF=- P_BUSY=- P_ATTEMPT=- P_FCLASS=- P_HINT=- P_FLAG=- P_RUN=- P_BETA=- P_ACTIONS=- P_HTTP=000 P_CURL=0 P_T0=0 P_T1=0
# poll_once <tag>: GET of the status, the busy flag as the guest sees it, one row in polls-<tag>.tsv (t0, t1, HTTP, curl, state, reason, cause, block index, block id, pnr, half_upgraded, busy,
# attempt, failure class, hint, flag, run id, beta_enabled, actions)
poll_once() {
    local tag="$1" row flag="-"
    call poll-now GET "${STATUS_PATH}" "" 20
    if [ "${POLL_FLAG:-1}" = 1 ]; then
        flag="$(g flag 2>/dev/null | tr -d '\r\n')"
        case "${flag}" in yes | no) ;; *) flag="-" ;; esac
    fi
    row="$(py row "${OUT}/poll-now.json" "${CALL_CODE}" "${CALL_RC}" "${flag}" "$(cat "${OUT}/poll-now.t0")" "$(cat "${OUT}/poll-now.t1")")"
    printf '%s\n' "${row}" >>"${OUT}/polls-${tag}.tsv"
    IFS=$'\t' read -r P_T0 P_T1 P_HTTP P_CURL P_STATE P_REASON P_CAUSE P_BIDX P_BID P_PNR P_HALF P_BUSY P_ATTEMPT P_FCLASS P_HINT P_FLAG P_RUN P_BETA P_ACTIONS <<<"${row}"
}

# wait_for <tag> <seconds> <wanted states> <predicate or ""> <stop states>: polls the status every POLL_EVERY seconds until the state is a wanted one, or the predicate (a function that reads
# P_*) says yes: status 0. 1: the time ran out (<tag>.timeout says so). 3: the state is a stop state (P_STATE says which). The rows are in polls-<tag>.tsv.
wait_for() {
    local tag="$1" max="$2" wanted="$3" pred="$4" stop="$5" end
    : >"${OUT}/polls-${tag}.tsv"
    rm -f "${OUT}/${tag}.timeout"
    now_s
    end=$((NOW_S + max))
    while :; do
        poll_once "${tag}"
        case " ${wanted} " in *" ${P_STATE} "*) return 0 ;; esac
        if [ -n "${pred}" ] && "${pred}"; then return 0; fi
        case " ${stop} " in *" ${P_STATE} "*) return 3 ;; esac
        now_s
        if [ "${NOW_S}" -ge "${end}" ]; then
            echo "${max}" >"${OUT}/${tag}.timeout"
            return 1
        fi
        nap "${POLL_EVERY:-3}"
    done
}

# wait_flag_gone <tag> <seconds>: the busy flag, asked of the guest every few seconds, until it is gone (the core lets go of it at its next look, within half a minute). Rows in flag-<tag>.tsv.
wait_flag_gone() {
    local tag="$1" max="$2" end f
    : >"${OUT}/flag-${tag}.tsv"
    rm -f "${OUT}/flag-${tag}.timeout"
    now_s
    end=$((NOW_S + max))
    while :; do
        f="$(g flag 2>/dev/null | tr -d '\r\n')"
        case "${f}" in yes | no) ;; *) f="-" ;; esac
        printf '%s\t%s\n' "$(now)" "${f}" >>"${OUT}/flag-${tag}.tsv"
        if [ "${f}" = no ]; then return 0; fi
        now_s
        if [ "${NOW_S}" -ge "${end}" ]; then echo "${max}" >"${OUT}/flag-${tag}.timeout"; return 1; fi
        nap 3
    done
}

# wait_boot <tag> <old boot id> <seconds>: the guest's boot id every two seconds (boot-<tag>.tsv: t0, t1, the id or -) until it answers with another one. Status 1 when it does not in time.
wait_boot() {
    local tag="$1" old="$2" max="$3" end id t0
    : >"${OUT}/boot-${tag}.tsv"
    now_s
    end=$((NOW_S + max))
    while :; do
        t0="$(now)"
        id="$(boot_id)"
        printf '%s\t%s\t%s\n' "${t0}" "$(now)" "${id:--}" >>"${OUT}/boot-${tag}.tsv"
        if [ -n "${id}" ] && [ "${id}" != "${old}" ]; then return 0; fi
        now_s
        if [ "${NOW_S}" -ge "${end}" ]; then echo "${max}" >"${OUT}/boot-${tag}.timeout"; return 1; fi
        nap 2
    done
}

# reboot_cycle <tag>: POST reboot, then wait for the guest to go away and come back with another boot id. Status 1: the restart was not accepted (the call is <tag>-post, the report judges it);
# 2: the guest did not come back.
reboot_cycle() {
    local tag="$1" old
    old="$(boot_id)"
    echo "${old:--}" >"${OUT}/${tag}.boot-old"
    call "${tag}-post" POST "${REBOOT_PATH}" '{}' 30
    [ "${CALL_CODE}" = 202 ] || return 1
    wait_boot "${tag}" "${old}" "${WAIT_BOOT:-1500}" || return 2
    return 0
}

# the plan: plan_make <tag> POSTs a plan and polls it to its end (polls-<tag>.tsv, the last answer is <tag>-get.*); plan_file is the file of the live plan that start_run builds its body from
PLAN_FILE=""
plan_make() {
    local tag="$1" end row state
    PLAN_FILE=""
    call "${tag}-post" POST "${PLAN_PATH}" "" 30
    case "${CALL_CODE}" in 202 | 200) ;; *) return 1 ;; esac
    : >"${OUT}/polls-${tag}.tsv"
    rm -f "${OUT}/${tag}.timeout"
    now_s
    end=$((NOW_S + ${WAIT_PLAN:-900}))
    while :; do
        call "${tag}-get" GET "${PLAN_PATH}" "" 20
        PLAN_FILE="${OUT}/${tag}-get.json"
        row="$(py planrow "${OUT}/${tag}-get.json" "${CALL_CODE}" "${CALL_RC}" "$(cat "${OUT}/${tag}-get.t0")" "$(cat "${OUT}/${tag}-get.t1")")"
        printf '%s\n' "${row}" >>"${OUT}/polls-${tag}.tsv"
        state="$(py get "${OUT}/${tag}-get.json" data.state)"
        case "${state}" in done | failed) break ;; esac
        now_s
        if [ "${NOW_S}" -ge "${end}" ]; then echo "${WAIT_PLAN:-900}" >"${OUT}/${tag}.timeout"; return 1; fi
        nap 3
    done
    [ "${state}" = done ]
}

# plan_startable: the plan of PLAN_FILE is one that a start can be made from (a blocked plan is refused at the start, whatever the body)
plan_startable() { [ -s "${PLAN_FILE}" ] && [ "$(py get "${PLAN_FILE}" data.state)" = done ] && [ "$(py get "${PLAN_FILE}" data.plan.verdict)" != blocked ]; }

# ensure_plan <tag>: the live plan (the core keeps it in its memory and nowhere else), made again when there is none; status 1 when there is none that a start can be made from
ensure_plan() {
    local tag="$1"
    call "${tag}-planlive" GET "${PLAN_PATH}" "" 30
    if [ "$(py get "${OUT}/${tag}-planlive.json" data.state)" = done ] && [ "$(py get "${OUT}/${tag}-planlive.json" data.plan.plan_id)" != - ]; then
        PLAN_FILE="${OUT}/${tag}-planlive.json"
    else
        plan_make "${tag}-plan" || true
    fi
    plan_startable
}

# start_run <name> <rehearsal: true|false> [acks: full|none] [hostname] [safety net]: POST start, with the body the page builds from the live plan (plan_id, the name of the machine, the removals and the
# sources put aside as the plan lists them, the acknowledgements it needs, the safety net: `snapshot` unless one is given, and an empty one is given by the page for a rehearsal). The start answers
# when it has taken the facts of the machine (two minutes for the facts alone): five minutes. Status 1 when there is no plan to build it from.
start_run() {
    local name="$1" reh="$2" acks="${3:-full}" host="${4:-${GUEST_HOST}}" net="${5-snapshot}" body
    [ -s "${PLAN_FILE}" ] || return 1
    body="$(py startbody "${PLAN_FILE}" "${host}" "${reh}" "${acks}" "${net}")" || return 1
    call "${name}" POST "${START_PATH}" "${body}" 300
    after_silence "${name}"
    return 0
}

# close_run <tag> [seconds]: whatever run is open is brought to idle, by the button its state allows: called off, discarded, closed (best effort; <tag>.closed says what came of it)
close_run() {
    local tag="$1" end
    : >"${OUT}/polls-${tag}-close.tsv"
    now_s
    end=$((NOW_S + ${2:-900}))
    while :; do
        poll_once "${tag}-close"
        case "${P_STATE}" in
        idle) echo idle >"${OUT}/${tag}.closed"; return 0 ;;
        refreshing | rehearsing | running) if [[ ",${P_ACTIONS}," == *,abort,* ]]; then call "${tag}-close-abort" POST "${ABORT_PATH}" '{}' 30; fi ;;
        rehearsed) call "${tag}-close-discard" POST "${ACK_PATH}" '{"action":"discard"}' 30 ;;
        refreshed | rehearsal_done | succeeded | succeeded_with_findings | reboot_first | failed)
            if [[ ",${P_ACTIONS}," == *,acknowledge,* ]]; then call "${tag}-close-ack" POST "${ACK_PATH}" '{"action":"acknowledge"}' 30; fi
            ;;
        esac
        now_s
        if [ "${NOW_S}" -ge "${end}" ]; then echo "state ${P_STATE}" >"${OUT}/${tag}.closed"; return 1; fi
        nap 3
    done
}

# ---- the cases -------------------------------------------------------------------------------------------------------------------

GUEST_HOST=""
WAIT_REFRESH="${WAIT_REFRESH:-3000}"
WAIT_REHEARSE="${WAIT_REHEARSE:-2400}"
WAIT_HOP="${WAIT_HOP:-7200}"
WAIT_VERIFY="${WAIT_VERIFY:-3000}"
WAIT_UNWIND="${WAIT_UNWIND:-900}"
CONTROL_SECONDS="${CONTROL_SECONDS:-200}"
FLAG_SECONDS="${FLAG_SECONDS:-120}"
# the states from which a case that waits for something else will not get there on its own
STOP_STATES="failed needs_attention stopped_at_waypoint interrupted manual manual_done"

case_c1() {
    local body
    marker case_C1_start
    body="$(printf '{"plan_id":"p1-%032d","rehearsal":false,"safety_net":"snapshot","hostname":"%s","accept_removals":[],"paused_sources":[],"acks":["no_interruption","no_way_back"]}' 0 "${GUEST_HOST}")"
    snap c1-ctl-a || return 1
    log "the control waits ${CONTROL_SECONDS} s: what the box changes by itself falls in it"
    nap "${CONTROL_SECONDS}"
    snap c1-ctl-b || return 1
    marker c1_window
    snap c1-before || return 1
    call c1-status-before GET "${STATUS_PATH}"
    call c1-start POST "${START_PATH}" "${body}" 300
    after_silence c1-start
    call c1-refresh POST "${REFRESH_PATH}" '{}' 300
    after_silence c1-refresh
    call c1-status-after GET "${STATUS_PATH}"
    snap c1-after || return 1
    marker case_C1_done
}

case_c2() {
    marker case_C2_start
    call c2-beta POST "${BETA_PATH}" '{"enabled":true}'
    call c2-status GET "${STATUS_PATH}"
    snap c2-after || return 1
    marker case_C2_done
}

case_c3() {
    marker case_C3_start
    snap c3-before || return 1
    call c3-refresh POST "${REFRESH_PATH}" '{}' 300
    after_silence c3-refresh
    if [ "${CALL_CODE}" = 202 ]; then
        wait_for c3-run "${WAIT_REFRESH}" "refreshed reboot_first" "" "${STOP_STATES}"
        evidence c3-run
        if [ "${P_STATE}" = reboot_first ]; then
            call c3-status-reboot-first GET "${STATUS_PATH}"
            if reboot_cycle c3-reboot; then
                wait_for c3-after-reboot 600 "refreshed" "" "${STOP_STATES}"
            fi
        fi
        if [ "${P_STATE}" = refreshed ]; then
            call c3-status-refreshed GET "${STATUS_PATH}"
            wait_flag_gone c3 "${FLAG_SECONDS}"
            snap c3-after || return 1
            call c3-ack POST "${ACK_PATH}" '{"action":"acknowledge"}' 60
            call c3-status-closed GET "${STATUS_PATH}"
        else
            call c3-status-end GET "${STATUS_PATH}"
            snap c3-after || return 1
        fi
    fi
    marker case_C3_done
}

# the plan, a rehearsal to `rehearsed`, the discard, the close
case_c4() {
    marker case_C4_start
    plan_make c4-plan || true
    if plan_startable; then
        snap c4-before || return 1
        start_run c4-start true full "${GUEST_HOST}" ""
        if [ "${CALL_CODE}" = 202 ]; then
            wait_for c4-run "${WAIT_REHEARSE}" "rehearsed" "" "${STOP_STATES}"
            evidence c4-run
            if [ "${P_STATE}" = rehearsed ]; then
                call c4-status-rehearsed GET "${STATUS_PATH}"
                snap c4-rehearsed || return 1
                call c4-discard POST "${ACK_PATH}" '{"action":"discard"}' 60
                wait_for c4-unwind "${WAIT_UNWIND}" "rehearsal_done" "" "failed needs_attention stopped_at_waypoint interrupted manual manual_done"
                if [ "${P_STATE}" = rehearsal_done ]; then
                    call c4-status-done GET "${STATUS_PATH}"
                    wait_flag_gone c4 "${FLAG_SECONDS}"
                    snap c4-after || return 1
                    call c4-ack POST "${ACK_PATH}" '{"action":"acknowledge"}' 60
                    call c4-status-closed GET "${STATUS_PATH}"
                else
                    call c4-status-end GET "${STATUS_PATH}"
                    snap c4-after || return 1
                fi
            else
                call c4-status-end GET "${STATUS_PATH}"
                snap c4-after || return 1
            fi
        fi
    fi
    marker case_C4_done
}

# the refusals: a wrong hostname and a missing acknowledgement (refused before anything is done), dpkg's lock held (a rehearsal body: a start that is not refused is a reversible one), and with
# a rehearsal that works, a second start and the page's restart
pred_rehearsing() { [ "${P_STATE}" = rehearsing ]; }
case_c5() {
    local before after ext
    marker case_C5_start
    ensure_plan c5 || { marker case_C5_done; return 0; }
    snap c5-before || return 1
    start_run c5-hostname false full "${GUEST_HOST}x"
    start_run c5-acks false none
    if g lock_start >"${OUT}/c5-lock.start" 2>"${OUT}/c5-lock.start.err"; then
        echo pending >"${WORK}/pending-lock"
        g lock_state >"${OUT}/c5-lock.during" 2>"${OUT}/c5-lock.during.err" || true
        start_run c5-lock true
        g lock_stop >"${OUT}/c5-lock.stop" 2>"${OUT}/c5-lock.stop.err" || true
        g lock_state >"${OUT}/c5-lock.undone" 2>"${OUT}/c5-lock.undone.err" || true
        if grep -qx free "${OUT}/c5-lock.undone"; then rm -f "${WORK}/pending-lock"; else harness_invalid C5 "dpkg's lock was not given back"; fi
        # a start that the lock did not stop began a rehearsal: it is called off and closed
        if [ "$(cat "${OUT}/c5-lock.code" 2>/dev/null)" = 202 ]; then close_run c5-lock 900 || true; fi
    else
        harness_invalid C5 "dpkg's lock could not be held: $(tr -d '\r' <"${OUT}/c5-lock.start" | tr '\n' ' ')"
    fi
    # a run that is open: a rehearsal; its second start and the page's restart are asked for while it works
    before="$(boot_id)"
    echo "${before:--}" >"${OUT}/c5-restart.boot-before"
    start_run c5-open true
    if [ "${CALL_CODE}" = 202 ]; then
        POLL_EVERY=1 wait_for c5-open-run 300 "" pred_rehearsing "${STOP_STATES} rehearsed"
        if [ "${P_STATE}" = rehearsing ]; then
            # the page's restart first, the moment the unit is seen working (were it done by then, the restart would be allowed and carried out: the report reads the answers for that),
            # the status of that moment is the one of the poll
            call c5-restart PUT "${RESTART_PATH}" "" 60
            for ext in json code curl t0 t1; do cp "${OUT}/poll-now.${ext}" "${OUT}/c5-status-before-second.${ext}"; done
            start_run c5-second true
            call c5-status-after-restart GET "${STATUS_PATH}"
            nap 25
            after="$(boot_id)"
            echo "${after:--}" >"${OUT}/c5-restart.boot-after"
            if [ -z "${after}" ] || [ "${after}" != "${before}" ]; then
                # the page's restart was carried out: the guest is going away. The case ends here, and so does the run
                echo "the guest restarted" >"${OUT}/c5-restart.rebooted"
                wait_boot c5-restart "${before}" "${WAIT_BOOT:-1500}" || true
                abort "C5: the page's restart was carried out while the unit worked (the boot id of the guest changed): what follows cannot be run on this guest"
            fi
        fi
        close_run c5-open 900 || harness_invalid C5 "the rehearsal of the case was not closed: $(cat "${OUT}/c5-open.closed" 2>/dev/null)"
    fi
    snap c5-after || return 1
    marker case_C5_done
}

# the call-off: the hop is started and called off while block 3 or 4 works (the sources of Debian 12 written, the timers stopped: there is something to put back)
pred_early_block() { [ "${P_BID}" = sources ] || [ "${P_BID}" = download ]; }
case_c6() {
    marker case_C6_start
    ensure_plan c6 || { marker case_C6_done; return 0; }
    snap c6-before || return 1
    start_run c6-start false
    if [ "${CALL_CODE}" = 202 ]; then
        wait_for c6-run 900 "" pred_early_block "${STOP_STATES} awaiting_reboot"
        if pred_early_block; then
            call c6-abort POST "${ABORT_PATH}" '{}' 60
            if [ "${CALL_CODE}" != 202 ]; then
                evidence c6-abort
                abort "C6: the call-off was refused (HTTP ${CALL_CODE}) while block ${P_BID} worked: the hop goes on, and the guest will not be Debian 11 again"
            fi
            # (a run that is called off is stopped, and passes through `interrupted` on its way to being put back: that is not a stop)
            wait_for c6-after 900 "failed" "" "needs_attention stopped_at_waypoint awaiting_reboot manual manual_done"
            evidence c6-after
            call c6-status-failed GET "${STATUS_PATH}"
            wait_flag_gone c6 "${FLAG_SECONDS}"
            snap c6-after || return 1
            call c6-ack POST "${ACK_PATH}" '{"action":"acknowledge"}' 60
            call c6-status-closed GET "${STATUS_PATH}"
        else
            evidence c6-late
            abort "C6: the hop was past block 4 (state ${P_STATE}, block ${P_BID}) before it could be called off: the guest will not be Debian 11 again"
        fi
    fi
    marker case_C6_done
}

# from `awaiting_reboot` to the verdict and the close: the machine as it waits, the restart, the check, the machine as it is. <p> is the prefix of the names. The core computes the checks
# that stand before the restart at its next look (every 30 seconds): until it has, the status has no barrier and does not offer `reboot`, and the proof waits for that (BARRIER_WAIT)
pred_reboot_offered() { [[ ",${P_ACTIONS}," == *,reboot,* ]]; }
hop_tail() {
    local p="$1"
    wait_for "${p}-barrier" "${BARRIER_WAIT:-240}" "" pred_reboot_offered "failed needs_attention stopped_at_waypoint interrupted manual manual_done"
    call "${p}-status-awaiting" GET "${STATUS_PATH}"
    snap "${p}-awaiting" || return 1
    evidence "${p}-awaiting"
    if reboot_cycle "${p}-reboot"; then
        wait_for "${p}-verify" "${WAIT_VERIFY}" "succeeded succeeded_with_findings" "" "failed needs_attention stopped_at_waypoint interrupted manual manual_done"
        evidence "${p}-verify"
        call "${p}-status-final" GET "${STATUS_PATH}"
        case "${P_STATE}" in
        succeeded | succeeded_with_findings)
            wait_flag_gone "${p}" "${FLAG_SECONDS}"
            snap "${p}-after" || return 1
            call "${p}-ack" POST "${ACK_PATH}" '{"action":"acknowledge"}' 60
            call "${p}-status-closed" GET "${STATUS_PATH}"
            snap "${p}-final" || return 1
            ;;
        esac
    else
        evidence "${p}-reboot"
    fi
    return 0
}

case_c7() {
    marker case_C7_start
    ensure_plan c7 || { marker case_C7_done; return 0; }
    snap c7-before || return 1
    start_run c7-start false
    if [ "${CALL_CODE}" = 202 ]; then
        wait_for c7-run "${WAIT_HOP}" "awaiting_reboot" "" "${STOP_STATES}"
        evidence c7-run
        if [ "${P_STATE}" = awaiting_reboot ]; then hop_tail c7 || return 1; fi
    fi
    marker case_C7_done
}

# ---- the faults ---------------------------------------------------------------------------------------------------------------------

SNAP_NAME=oupbase
FAULT_OBSERVE="${FAULT_OBSERVE:-180}"
F4_BLOCK="${F4_BLOCK:-full}"
# (block 5 lasted a minute in the measured hop; block 6 is longer, and apt solves and downloads before dpkg begins: the wait is for the block, and ends when dpkg is seen)
DPKG_WAIT="${DPKG_WAIT:-600}"

# guest_clock <name>: a guest restored from a snapshot has the clock of the instant of the snapshot (minutes to hours behind), and a Release file of a mirror dated after it is "not valid yet" to the first apt-get update of
# the hop: the clock is set from the runner's, the epoch taken again for each of three tries. <name>.clock says how far behind it was ("skew <seconds>") and "set". Status 1: it could not be set (recorded as the harness's).
guest_clock() {
    local name="$1" try
    : >"${OUT}/${name}.clock"
    for try in 1 2 3; do
        now_s
        echo "# try ${try}" >>"${OUT}/${name}.clock"
        if g set_clock "${NOW_S}" >>"${OUT}/${name}.clock" 2>>"${OUT}/${name}.clock.err"; then return 0; fi
        nap 3
    done
    harness_invalid "${name}" "the clock of the restored guest could not be set from the runner's: $(tr -d '\r' <"${OUT}/${name}.clock" | tr '\n' ' ' | cut -c1-200)"
    return 1
}

# dpkg_gate <id>: waits until dpkg is at work (the guest looks every second, up to DPKG_WAIT seconds, and wants it there for three looks in a row), so that the cut that follows lands in the middle of dpkg and not in
# apt's solver or its download. <id>.dpkg says what was seen ("seen <second>" or "never <seconds>"). Status 1: it was not seen.
dpkg_gate() {
    local id="$1" try
    : >"${OUT}/${id}.dpkg"
    : >"${OUT}/${id}.dpkg.err"
    for try in 1 2 3; do
        SSH_LIMIT=$((DPKG_WAIT + 90)) g dpkg_wait "${DPKG_WAIT}" >>"${OUT}/${id}.dpkg" 2>>"${OUT}/${id}.dpkg.err" || true
        grep -qE '^(seen|never) ' "${OUT}/${id}.dpkg" && break
    done
    grep -q '^seen ' "${OUT}/${id}.dpkg"
}

# kill_unit_now <id>: kills the unit of the run as it is now (its invocation is asked first and given to the kill, which makes the kill safe to say again), and sees it gone. <id>.kill has what the tries said.
# Status 0: gone. 2: no unit was active. 1: it could not be killed (recorded as the harness's).
kill_unit_now() {
    local id="$1" inv
    if ! g3 "${OUT}/${id}.unit-id" unit_id; then
        harness_invalid "${id}" "the guest did not answer when the invocation of the unit was asked for"
        return 1
    fi
    inv="$(grep -v '^#' "${OUT}/${id}.unit-id" | tail -n 1 | tr -d '\r\n ')"
    [ -n "${inv}" ] || return 2
    if SSH_LIMIT=200 g3 "${OUT}/${id}.kill" kill_unit "${inv}"; then return 0; fi
    harness_invalid "${id}" "the unit could not be killed: $(grep -v '^#' "${OUT}/${id}.kill" | tr -d '\r' | tr '\n' ' ' | cut -c1-200)"
    return 1
}

# fault_begin <id>: the whole machine as it was after C4 (the snapshot is of the memory too: the plan is live), the guest waited for, the core and the plan asked
fault_begin() {
    local id="$1"
    marker "case_${id}_start"
    if ! qmon loadvm "${SNAP_NAME}" >"${OUT}/${id}.loadvm" 2>&1; then
        harness_invalid "${id}" "the snapshot could not be restored: $(tr -d '\r' <"${OUT}/${id}.loadvm" | tr '\n' ' ' | cut -c1-300)"
        marker "case_${id}_done"
        return 1
    fi
    wait_boot "${id}-restored" none 600 || true
    guest_clock "${id}" || { marker "case_${id}_done"; return 1; }
    wait_for "${id}-idle" 300 "idle" "" "${STOP_STATES}"
    call "${id}-planlive" GET "${PLAN_PATH}" "" 30
    PLAN_FILE="${OUT}/${id}-planlive.json"
    snap "${id}-base" || return 1
    return 0
}

pred_pnr() { [ "${P_PNR}" = true ]; }
pred_f4_block() { [ "${P_BID}" = "${F4_BLOCK}" ]; }

# the tail of a fault after the unit is gone and `resume` is due: wait for what the run does, resume it, wait for the restart's turn
fault_resume() {
    local id="$1"
    call "${id}-resume" POST "${RESUME_PATH}" '{}' 60
    if [ "${CALL_CODE}" = 202 ]; then
        wait_for "${id}-recover" "${WAIT_HOP}" "awaiting_reboot" "" "failed needs_attention stopped_at_waypoint manual manual_done"
        evidence "${id}-recover"
        if [ "${P_STATE}" = awaiting_reboot ]; then
            hop_tail "${id}" || return 1
        else
            call "${id}-status-end" GET "${STATUS_PATH}"
        fi
    fi
    return 0
}

fault_f1() {
    local rc
    fault_begin F1 || return 0
    start_run f1-start false
    if [ "${CALL_CODE}" = 202 ]; then
        wait_for f1-run 900 "" pred_early_block "${STOP_STATES} awaiting_reboot"
        if pred_early_block; then
            kill_unit_now F1
            rc=$?
            if [ "${rc}" -eq 2 ]; then
                echo "no unit of the engine was active when the kill was due (state ${P_STATE}, block ${P_BID})" >"${OUT}/F1.untestable"
            elif [ "${rc}" -eq 0 ]; then
                # the core sees the unit gone at its next look, and `interrupted` lasts until the look after (it launches the unwind then): polled every second, and with nothing else asked of the guest, it is caught
                # most of the time; the report also takes the road through unwinding to failed with the reason interrupted_killed as the proof of the interruption
                POLL_EVERY=1 POLL_FLAG=0 wait_for f1-after "${WAIT_UNWIND}" "failed" "" "needs_attention stopped_at_waypoint awaiting_reboot manual manual_done"
                evidence f1-after
                call f1-status-failed GET "${STATUS_PATH}"
                wait_flag_gone f1 "${FLAG_SECONDS}"
                snap f1-after || return 1
                if [ "${P_STATE}" = failed ]; then
                    call f1-ack POST "${ACK_PATH}" '{"action":"acknowledge"}' 60
                    call f1-status-closed GET "${STATUS_PATH}"
                fi
            fi
        else
            echo "the hop was past block 4 (state ${P_STATE}, block ${P_BID}) before the unit could be killed" >"${OUT}/F1.untestable"
        fi
    fi
    marker case_F1_done
}

# observe <tag> <seconds>: the status every POLL_EVERY seconds for that long (polls-<tag>.tsv): a run that stays as it is
observe() {
    local tag="$1" end
    : >"${OUT}/polls-${tag}.tsv"
    now_s
    end=$((NOW_S + $2))
    while :; do
        poll_once "${tag}"
        now_s
        [ "${NOW_S}" -lt "${end}" ] || return 0
        nap "${POLL_EVERY:-3}"
    done
}

fault_f2() {
    local rc
    fault_begin F2 || return 0
    start_run f2-start false
    if [ "${CALL_CODE}" = 202 ]; then
        wait_for f2-run "${WAIT_HOP}" "" pred_pnr "${STOP_STATES} awaiting_reboot"
        if pred_pnr; then
            if dpkg_gate F2; then
                echo "$(now) ${P_BID}" >"${OUT}/f2.kill-at"
                kill_unit_now F2
                rc=$?
                if [ "${rc}" -eq 2 ]; then
                    echo "no unit of the engine was active when the kill was due: the hop had ended" >"${OUT}/F2.untestable"
                elif [ "${rc}" -eq 0 ]; then
                    wait_for f2-gone 300 "interrupted" "" "failed needs_attention stopped_at_waypoint awaiting_reboot manual manual_done"
                    call f2-status-interrupted GET "${STATUS_PATH}"
                    evidence f2-interrupted
                    # nothing repairs it by itself: the shipped setting allows no automatic try
                    observe f2-observe "${FAULT_OBSERVE}"
                    call f2-status-observed GET "${STATUS_PATH}"
                    fault_resume f2 || return 1
                fi
            else
                echo "dpkg was not seen running within ${DPKG_WAIT} s of the point of no return: the unit was not killed, and a kill outside dpkg is not the fault of this case" >"${OUT}/F2.untestable"
            fi
        else
            echo "the hop did not reach its point of no return (state ${P_STATE}, block ${P_BID}) before the unit could be killed" >"${OUT}/F2.untestable"
        fi
    fi
    marker case_F2_done
}

fault_f3() {
    fault_begin F3 || return 0
    start_run f3-start false
    if [ "${CALL_CODE}" = 202 ]; then
        wait_for f3-run "${WAIT_HOP}" "" pred_pnr "${STOP_STATES} awaiting_reboot"
        if pred_pnr; then
            echo pending >"${WORK}/pending-route"
            echo "$(now) ${P_BID}" >"${OUT}/f3.cut-at"
            # the verb takes the routes of both families away and proves the cut with a request that has to fail; a cut that could not be made ends the case, with the network given back
            if g3 "${OUT}/f3.cut" route_cut; then
                wait_for f3-stopped "${WAIT_HOP}" "stopped_at_waypoint" "" "failed needs_attention interrupted awaiting_reboot manual manual_done"
                call f3-status-stopped GET "${STATUS_PATH}"
                evidence f3-stopped
            else
                harness_invalid F3 "the network could not be cut: $(grep -v '^#' "${OUT}/f3.cut" | tr -d '\r' | tr '\n' ' ' | cut -c1-200)"
            fi
            g3 "${OUT}/f3.restore" route_restore || true
            g net_check >"${OUT}/f3.netcheck" 2>/dev/null || true
            if grep -qxE 'restored|present' "${OUT}/f3.restore"; then rm -f "${WORK}/pending-route"; else harness_invalid F3 "the network was not given back: $(tr -d '\r' <"${OUT}/f3.restore" | tr '\n' ' ' | cut -c1-200)"; fi
            if [ "${P_STATE}" = stopped_at_waypoint ]; then
                fault_resume f3 || return 1
            fi
        else
            echo "the hop did not reach its point of no return (state ${P_STATE}, block ${P_BID}) before the network could be cut" >"${OUT}/F3.untestable"
        fi
    fi
    marker case_F3_done
}

fault_f4() {
    local old
    fault_begin F4 || return 0
    start_run f4-start false
    if [ "${CALL_CODE}" = 202 ]; then
        wait_for f4-run "${WAIT_HOP}" "" pred_f4_block "${STOP_STATES} awaiting_reboot"
        if pred_f4_block; then
            old="$(boot_id)"
            echo "${old:--}" >"${OUT}/f4.boot-old"
            # the power goes when dpkg is at work (apt's solver and download come first in the block), not at a time after the block began
            if dpkg_gate F4; then
                poll_once f4-before-reset
                echo "$(now) ${P_STATE} ${P_BID}" >"${OUT}/f4.reset-at"
                if qmon system_reset >"${OUT}/f4.reset" 2>&1; then
                    if wait_boot f4-reset "${old}" "${WAIT_BOOT:-1500}"; then
                        guest_clock F4-reset || return 1
                        wait_for f4-gone 600 "interrupted" "" "failed needs_attention stopped_at_waypoint awaiting_reboot manual manual_done"
                        call f4-status-interrupted GET "${STATUS_PATH}"
                        evidence f4-interrupted
                        fault_resume f4 || return 1
                    else
                        evidence f4-noboot
                    fi
                else
                    harness_invalid F4 "the reset could not be sent to the monitor: $(tr -d '\r' <"${OUT}/f4.reset" | tr '\n' ' ' | cut -c1-200)"
                fi
            else
                echo "dpkg was not seen running within ${DPKG_WAIT} s in block ${F4_BLOCK}: the power was not cut, and a cut outside dpkg is not the fault of this case" >"${OUT}/F4.untestable"
            fi
        else
            echo "the hop did not reach block ${F4_BLOCK} (state ${P_STATE}, block ${P_BID}) before the power could be cut" >"${OUT}/F4.untestable"
        fi
    fi
    marker case_F4_done
}

# ---- the runner's side of the run ------------------------------------------------------------------------------------------------

want_case() {
    [ -z "${CASES:-}" ] && return 0
    [[ ",${CASES}," == *",$1,"* ]]
}

run_case() {
    local id="$1" fn="$2"
    want_case "${id}" || return 0
    "${fn}" || abort "the harness could not finish the case ${id}: a snapshot of the guest could not be had (see snap-*.failed)"
}

init_out() {
    OUT="${OUT:-${PWD}/oup-out}"
    WORK="${WORK:-${PWD}/oup-work}"
    mkdir -p "${OUT}" "${WORK}"
    chmod 700 "${WORK}"
    [ -s "${WORK}/oup.py" ] || write_oup_py "${WORK}/oup.py"
}

cleanup() {
    local rc=$?
    if [ -n "${OUT:-}" ] && [ -d "${OUT}" ]; then
        # an undo that is pending is made once more: the guest is not left with its network cut or dpkg's lock held
        if [ -e "${WORK}/pending-lock" ]; then g lock_stop >/dev/null 2>&1 && rm -f "${WORK}/pending-lock"; fi
        if [ -e "${WORK}/pending-route" ]; then g route_restore >/dev/null 2>&1 && rm -f "${WORK}/pending-route"; fi
        if [ -e "${WORK}/pending-lock" ] || [ -e "${WORK}/pending-route" ]; then harness_invalid run "an undo was still pending when the run ended"; fi
        if [ "${rc}" -ne 0 ] && [ ! -s "${OUT}/aborted" ]; then
            echo "the runner script ended on exit ${rc} (its output says where)" >>"${OUT}/aborted"
        fi
    fi
    exit "${rc}"
}

runner_run() {
    local leg="${1:-}" rc
    case "${leg}" in core | faults) ;; *) echo "usage: $0 run core|faults" >&2; exit 2 ;; esac
    set -u
    init_out
    : >"${OUT}/timeline.tsv"
    echo "${leg}" >"${OUT}/leg"
    echo "${TAG:-}" >"${OUT}/tag"
    date -u +%FT%TZ >"${OUT}/started"
    trap cleanup EXIT
    [ -n "${EPOCHREALTIME:-}" ] || abort "this bash has no EPOCHREALTIME (bash 5 is needed)"
    login || abort "the box's user could not log in: no token"
    GUEST_HOST="$(g hostname | tr -d '\r\n')"
    [ -n "${GUEST_HOST}" ] || abort "the guest does not answer ssh"
    echo "${GUEST_HOST}" >"${OUT}/hostname"
    marker run_start
    # the first call of the engine's routes: a release that has none answers 404 to it and to every call that follows, and the run ends there
    call probe-status GET "${STATUS_PATH}" "" 30
    [ "${CALL_CODE}" != 404 ] || abort "the release under test has no ${STATUS_PATH}"
    case "${leg}" in
    core)
        run_case c1 case_c1
        run_case c2 case_c2
        run_case c3 case_c3
        run_case c4 case_c4
        run_case c5 case_c5
        run_case c6 case_c6
        run_case c7 case_c7
        ;;
    faults)
        # the start state: Debian 11, refreshed, rehearsed, the plan live (the cases of the core leg, run again; the report holds them to being reached and not to the rest)
        run_case prelude prelude_faults
        if want_case snapshot; then
            if login && qmon savevm "${SNAP_NAME}" >"${OUT}/savevm.txt" 2>&1; then echo ok >"${OUT}/savevm.ok"; else abort "the snapshot of the whole machine could not be taken: $(tr -d '\r' <"${OUT}/savevm.txt" | tr '\n' ' ' | cut -c1-300)"; fi
        fi
        want_case f1 && fault_f1
        want_case f2 && fault_f2
        want_case f3 && fault_f3
        want_case f4 && fault_f4
        ;;
    esac
    date -u +%FT%TZ >"${OUT}/finished"
    log "done"
    rc=0
    return "${rc}"
}

prelude_faults() {
    case_c2 && case_c3 && case_c4
}

# runner_prepare: the files go to the guest, the guest prepares itself, what it wrote comes back, the box's user is made and logs in
runner_prepare() {
    local pw key
    set -u
    init_out
    : >"${OUT}/timeline.tsv"
    : "${INSTALL_SH:?INSTALL_SH names the published install.sh}"
    scp -F "${SSH_CONFIG}" "${INSTALL_SH}" "${OUP_HERE}/os-readiness-proof.sh" "${OUP_HERE}/os-upgrade-proof.sh" "${SSH_HOST}:" ||
        abort "the files could not be copied to the guest"
    SSH_LIMIT=3600 gssh "sudo env OUP_DISPOSABLE=1 INSTALLER_ARGS='${INSTALLER_ARGS:-}' DOCKER_FROM='${DOCKER_FROM:-}' OUT=/home/casa/oup-guest-out bash os-upgrade-proof.sh guest-prepare" || true
    mkdir -p "${OUT}/guest"
    gssh 'sudo tar -C /home/casa/oup-guest-out -cf - .' | tar -xf - -C "${OUT}/guest" || true
    [ ! -s "${OUT}/guest/aborted" ] || abort "the guest's preparation stopped: $(tr '\n' ' ' <"${OUT}/guest/aborted")"
    [ -s "${OUT}/guest/prepared" ] || abort "the guest's preparation did not reach its end"
    # the box's own user: the one login the page would make
    pw="Aa1-$(head -c12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    (umask 077; printf '%s' "${pw}" >"${WORK}/pw")
    curl -sS --max-time 30 "${GW}/v1/users/status" -o "${WORK}/status.json" || abort "the gateway does not answer through the forward (port ${GW_PORT})"
    key="$(py get "${WORK}/status.json" data.key)"
    [ "${key}" != - ] || abort "the box has no registration key: $(head -c 200 "${WORK}/status.json")"
    curl -sS --max-time 30 -X POST "${GW}/v1/users/register" -H 'Content-Type: application/json' \
        -d "$(printf '{"username":"%s","password":"%s","key":"%s"}' "${PROOF_USER}" "${pw}" "${key}")" -o "${WORK}/register.json" || abort "the box's user could not be made"
    login || abort "the box's user could not log in: $(head -c 200 "${WORK}/login.json" 2>/dev/null)"
    date -u +%FT%TZ >"${OUT}/prepared"
}

main() {
    case "${1:-}" in
    guest-prepare) guest_prepare ;;
    prepare) runner_prepare ;;
    run) shift; runner_run "$@" ;;
    *) echo "usage: $0 guest-prepare | prepare | run core|faults" >&2; exit 2 ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then main "$@"; fi
