#!/usr/bin/env bash
# Proof that the read-only OS-upgrade READINESS report of ReCasaOS (GET /v1/sys/os/readiness) changes nothing and says what it should, on a real machine.
#
# Runs as root inside a DISPOSABLE Debian or Ubuntu machine (a QEMU guest in CI), next to install.sh (the published installer). It installs Docker (pinned
# to DOCKER_FROM on the Debian 11 leg: the owner's box) and ReCasaOS, starts a few witness containers, and then asks the core for the report the way the
# dashboard does: through the gateway, with the loopback secret (Authorization: Internal <secret>) the services use among themselves.
#
#   os-readiness-proof.sh d11   Debian 11 (installed with --use-debian-archive): a hop to Debian 12
#   os-readiness-proof.sh u22   Ubuntu 22.04: a hop to Ubuntu 24.04
#
# In this order:
#   0. a WAIT: until AppManagement has made its own check of the images of the apps (it does, three minutes after it starts, and ends it by renaming a new
#      /var/lib/casaos/image_updates.json into place: a write that would fall in the window below and be taken for the report's);
#   1. a CONTROL: a snapshot of the box, two calls of GET /v1/sys/version/current, a few seconds, a snapshot: what changes by itself is known and is not held
#      against the report;
#   2. the INVARIANCE window: a snapshot (name, size, mtime, ctime, mode, owner and content hash of every file under /etc, /var/lib/dpkg, /var/lib/apt,
#      /var/cache/apt, /var/lib/casaos and /var/log/casaos; every service unit with its state, main pid and invocation id; the listening sockets), then
#      GET /v1/sys/os, GET /v1/sys/os/readiness three times, and once more while ANOTHER process holds dpkg's lock (the call must neither wait for the lock
#      nor take it), then the same snapshot, then one more call: the answer of the unchanged box that the dirty matrix is compared with. Nothing else is
#      hashed: not /tmp, not /run, not /home or /root; a tool that writes there (`dkms status` may, in $TMPDIR) is not seen;
#   3. the DIRTY MATRIX, one way of dirtying the box at a time: the dirt, a call, the undo (always: a trap runs it when the script dies in the middle of a
#      step), a check that the undo held, a call. Every step is a trio of functions (dirty_<id>, undo_<id>, undone_<id>), each run in a subshell that stops at
#      its first failing command, so that the status of a step says whether the box was really dirtied and really put back.
#
# Nothing here judges: it writes what it saw into $OUT and os-readiness-proof-report.py turns that into verdicts, or says the run is invalid. It stops early
# (and says why, in $OUT/aborted) only when it cannot go on. A step whose dirt could not be made, or whose undo failed, is recorded as such: the report
# makes the leg INVALID, never a FAIL of the feature.
#
# Needs: READINESS_DISPOSABLE=1, INSTALLER_ARGS (options for install.sh, unquoted on purpose: a list), DOCKER_FROM (d11 only: the Docker the box starts on),
# OUT (default ./readiness-out).

# ---- the parts that are only text, and can be tested without a machine -----------------------------------------------------------------

# leg_facts <leg>: "<id> <version it is on> <its codename> <version it goes to> <the codename it goes to>", or status 1
leg_facts() {
    case "$1" in
    d11) echo "debian 11 bullseye 12 bookworm" ;;
    u22) echo "ubuntu 22.04 jammy 24.04 noble" ;;
    *) return 1 ;;
    esac
}

# major_of <apt version>: "5:29.8.2-1~ubuntu.24.04~noble" -> 29
major_of() { sed -E 's/^[0-9]+://; s/\..*//' <<<"$1"; }

# containerd_17: the first (newest, as apt-cache madison lists them) containerd.io of the 1.7 line, nothing when apt has none. Every producer is read to its
# end: under pipefail a consumer that quits at its first match kills its producer with SIGPIPE, which is status 141 (the Debian 11 leg of the Docker proof died so)
containerd_17() { apt-cache madison containerd.io | awk '$3 ~ /^1\.7\./ && !found {print $3; found = 1}'; }

# The tool that makes the manifest of a tree of files: one row for every path (its name as a JSON string, its type, size, mtime and ctime in nanoseconds, mode,
# owner and group, and the sha256 of its content, or where it points, or - for a directory), the filesystem of the tree only, nothing written anywhere
# but its stdout. Python, because a path may have anything in its name and a row has to stay a row. A path that cannot be read says so in its row.
write_manifest_tool() {
    cat >"$1" <<'PY'
import hashlib
import json
import os
import stat
import sys


ROOT = os.environ.get("INV_ROOT", "")


def shown(path):
    """the path as the report sees it: with INV_ROOT (the tests of this script point it at a tree of their own) taken off the front"""
    if not ROOT:
        return path
    rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
    return "/" if rel == "." else "/" + rel


def digest(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError as e:
        return "unreadable:%s" % e.errno
    return h.hexdigest()


def row(path, st):
    mode = st.st_mode
    kind = "f" if stat.S_ISREG(mode) else "d" if stat.S_ISDIR(mode) else "l" if stat.S_ISLNK(mode) else "o"
    what = "-"
    if kind == "f":
        what = digest(path)
    elif kind == "l":
        try:
            what = "->" + os.readlink(path)
        except OSError as e:
            what = "unreadable:%s" % e.errno
    return "\t".join([json.dumps(shown(path)), kind, str(st.st_size), str(st.st_mtime_ns), str(st.st_ctime_ns), "%o" % stat.S_IMODE(mode), "%d:%d" % (st.st_uid, st.st_gid), what])


def walk(top):
    st = os.lstat(top)
    yield top, st
    stack = [top]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name)
        except FileNotFoundError:
            continue
        except OSError as e:
            print("%s\td\t0\t0\t0\t0\t0:0\tunreadable:%s" % (json.dumps(shown(d) + "/?"), e.errno))
            continue
        for e in entries:
            p = os.path.join(d, e.name)
            try:
                s = e.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as err:
                print("%s\to\t0\t0\t0\t0\t0:0\tunreadable:%s" % (json.dumps(shown(p)), err.errno))
                continue
            yield p, s
            # a directory of another filesystem is listed and not entered (os.lstat, because a DirEntry does not say its device on every platform)
            if stat.S_ISDIR(s.st_mode) and os.lstat(p).st_dev == st.st_dev:
                stack.append(p)


for tree in sys.argv[1:]:
    for p, s in walk(tree):
        print(row(p, s))
PY
}

# The process that holds dpkg's lock the way apt and dpkg do (a POSIX record lock: fcntl), and the one that asks whether it is held. Both open the file
# for reading and writing without creating it: a lock file that is not there is a box that is not what the proof needs, and creating it would be a
# write the invariance window would hold against the report.
write_lock_tools() {
    cat >"$1/lockhold.py" <<'PY'
import fcntl
import sys
import time

f = open(sys.argv[1], "r+")
fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("locked", flush=True)
time.sleep(float(sys.argv[2]))
PY
    cat >"$1/lockprobe.py" <<'PY'
import fcntl
import sys

try:
    f = open(sys.argv[1], "r+")
except OSError:
    print("missing")
    sys.exit(0)
try:
    fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    print("held")
else:
    print("free")
PY
}

# ---- the machine ------------------------------------------------------------------------------------------------------------------------

LIBDIR="${LIBDIR:-/usr/local/lib/osr}"
AUTH_DIR="${AUTH_DIR:-/root/.osr-auth}"
APT_DIR="${APT_DIR:-/etc/apt}"
FSTAB="${FSTAB:-/etc/fstab}"
BOOT_DIR="${BOOT_DIR:-/boot}"
OS_RELEASE="${OS_RELEASE:-/etc/os-release}"
# the file of digits only that apt itself counts as an unfinished dpkg journal ("dpkg was interrupted")
DPKG_JOURNAL="${DPKG_JOURNAL:-/var/lib/dpkg/updates/0001}"
DPKG_LOCK="${DPKG_LOCK:-/var/lib/dpkg/lock-frontend}"
REBOOT_REQUIRED="${REBOOT_REQUIRED:-/var/run/reboot-required}"
INTERNAL_SECRET_FILE="${INTERNAL_SECRET_FILE:-/var/run/casaos/internal.secret}"
# what AppManagement writes when it has checked the images of the apps (a temporary file in /var/lib/casaos, renamed over this one)
IMAGE_UPDATES="${IMAGE_UPDATES:-/var/lib/casaos/image_updates.json}"
# the six trees of the invariance window, the first five of which may not change
if [ -z "${INV_TREES+x}" ]; then INV_TREES=(/etc /var/lib/dpkg /var/lib/apt /var/cache/apt /var/lib/casaos /var/log/casaos); fi
# the witnesses that start by themselves after a restart of Docker (its own rule for always and unless-stopped)
COME_BACK=(p-always p-unless-stopped p-db p-web)
# the dirty matrix, in the order it runs: the lightest first, Docker and AppManagement last. os-readiness-proof-report.py has the same list.
STEP_IDS=(held held_docker broken_dpkg sources_cdrom sources_file sources_alias sources_pin sources_pin_prefs sources_proposed sources_third_party
    foreign_arch_empty foreign_arch reboot_first fstab_unresolved containers_policy containers_ports_host containers_ports_publish busy_lock busy_unit
    disk_boot apps docker_down)

# EPOCHREALTIME and printf's %()T are the shell's own: a clock that does not need `date` to be installed, or to work
now() { printf '%s' "${EPOCHREALTIME}"; }
log() { printf '[proof %(%H:%M:%S)T] %s\n' -1 "$*" | tee -a "${OUT}/steps.log"; }
marker() { printf '#\t%s\t%s\n' "${EPOCHREALTIME}" "$1" >>"${OUT}/timeline.tsv"; }
retry() { local i; for i in 1 2 3; do "$@" && return 0; sleep 10; done; return 1; }
# a run that cannot go on says why and ends; the report turns that into INVALID RUN
abort() { echo "$*" >>"${OUT}/aborted"; log "ABORTED: $*"; exit 0; }
not_proven() { abort "NOT PROVEN: $*"; }

installed() { # <package>
    local st
    st="$(dpkg-query -W -f='${db:Status-Abbrev}' "$1" 2>/dev/null || true)"
    [[ "${st}" == ?i* ]]
}

wait_docker() { # [seconds]
    local end=$((SECONDS + ${1:-300}))
    while [ "${SECONDS}" -lt "${end}" ]; do
        if timeout -s KILL 5 docker info >/dev/null 2>&1; then return 0; fi
        sleep 1
    done
    log "docker did not answer within ${1:-300} s"
    return 1
}

# in_subshell <function> [args]: runs it in a subshell that stops at its first failing command, and leaves its status in STATUS. Always returns 0. errexit is
# ignored inside a function that is run as the left side of `||` or in an `if`, so a step called that way would have the status of its LAST command and hide
# a failure of the first: it is called as a plain command, with errexit off around a subshell that turns it on again.
in_subshell() {
    set +e
    (
        set -e
        "$@"
    )
    STATUS=$?
    set -e
    return 0
}

cleanup() {
    local rc=$?
    if [ -n "${OUT:-}" ]; then
        # a step that was in the middle of its dirt, or whose undo failed, is undone once more: the box is not left dirty, whatever stopped the run
        if [ -n "${PENDING_UNDO:-}" ]; then
            in_subshell "undo_${PENDING_UNDO}" >>"${OUT}/step-${PENDING_UNDO}.undo.log" 2>&1
            echo "${STATUS}" >"${OUT}/step-${PENDING_UNDO}.undo-rc-exit"
        fi
        # a command that failed under set -e ended the run with nothing said, and the report would judge what is left as if it were a run
        if [ "${rc}" -ne 0 ] && [ ! -s "${OUT}/aborted" ]; then
            echo "the guest script ended on a failing command (exit ${rc}): ${FAILED_AT:-no command recorded}" >>"${OUT}/aborted"
        fi
    fi
    lock_hold_stop >/dev/null 2>&1 || true
    exit "${rc}"
}

# guard_run: from here a command that fails ends the run (in functions too), and the report is told which one. A tool of the harness that fails (jq, curl,
# apt, docker) is not a verdict on the feature: a run that ended on one is an invalid run, and says so.
guard_run() {
    set -Eeuo pipefail
    trap 'FAILED_AT="line ${LINENO}: ${BASH_COMMAND}"' ERR
    trap cleanup EXIT
}

# ---- Docker, ReCasaOS, the witnesses ---------------------------------------------------------------------------------------------------

install_docker() {
    local cur cprev
    retry curl -fsSL https://get.docker.com -o get-docker.sh
    if [ -n "${DOCKER_FROM:-}" ]; then
        # d11: the owner's box, Docker 28.0.4 from Docker's own repository, with a 29 on offer. A box on 28.0.4 has the containerd.io of that day, which is on
        # the 1.7 line (the Docker update proof and the OS upgrade measurement do the same)
        retry sh get-docker.sh --version "${DOCKER_FROM}" >>"${OUT}/get-docker.log" 2>&1
        START_VERSION="${DOCKER_FROM}"
        cur="$(dpkg-query -W -f='${Version}' containerd.io)"
        cprev="$(containerd_17)"
        if [ "$(major_of "${cur}")" -ge 2 ]; then
            [ -n "${cprev}" ] || not_proven "Docker's repository has no containerd.io 1.7 to start from"
            systemctl stop docker.socket docker.service containerd.service
            "${APT[@]}" install -y -q --allow-downgrades "containerd.io=${cprev}"
            systemctl stop docker.socket docker.service containerd.service
            rm -rf /var/lib/docker /var/lib/containerd
            systemctl start docker
        fi
    else
        retry sh get-docker.sh >>"${OUT}/get-docker.log" 2>&1
        wait_docker || abort "the Docker that was installed does not start"
        START_VERSION="$(docker version --format '{{.Server.Version}}')"
    fi
    wait_docker || abort "the Docker this leg starts on does not start"
    [ "$(docker version --format '{{.Server.Version}}')" = "${START_VERSION}" ] || abort "docker is not the version it was put on (${START_VERSION})"
    echo "${START_VERSION}" >"${OUT}/start-version"
    log "starting on docker ${START_VERSION}, $(dpkg-query -W -f='${Package} ${Version}' containerd.io)"
}

install_casaos() {
    local port
    # shellcheck disable=SC2086
    bash install.sh --no-telemetry ${INSTALLER_ARGS:-} 2>&1 | tee "${OUT}/install.log" >/dev/null
    test "${PIPESTATUS[0]}" -eq 0
    [ "$(docker version --format '{{.Server.Version}}')" = "${START_VERSION}" ] || abort "the installer moved Docker off ${START_VERSION}"
    port="$(sed -nE 's/^[[:space:]]*HttpPort[[:space:]]*=[[:space:]]*([0-9]+).*/\1/p' /etc/casaos/gateway.ini | sed -n 1p)"
    CASA_PORT="${port:-80}"
    log "ReCasaOS is installed, its gateway is on port ${CASA_PORT}"
}

# the witnesses: a container of each restart policy that comes back by itself, a database that writes a record, makes it durable (sync) and only then says
# "ack <id>", and one that publishes a port. None of them has restart policy `no`, none is on the host network and none publishes 53, 80 or 443: a clean box
# has no warning about its containers, and the dirty matrix brings each of them in on purpose.
start_things() {
    retry docker pull -q busybox >/dev/null
    retry docker pull -q nginx:alpine >/dev/null
    docker run -d --init --name p-always --restart always busybox sleep 100000 >/dev/null
    docker run -d --init --name p-unless-stopped --restart unless-stopped busybox sleep 100000 >/dev/null
    docker volume create pdb >/dev/null
    docker run -d --init --name p-db --restart unless-stopped -v pdb:/data busybox sh -c \
        'trap "exit 0" TERM; i=0; while :; do i=$((i+1)); id="$(date +%s)-$i"; echo "$id" >> /data/log; sync; echo "ack $id"; sleep 0.1 & wait $!; done' >/dev/null
    docker run -d --init --name p-web --restart unless-stopped -p 18081:80 nginx:alpine >/dev/null
    sleep 5
}

# settle <file>: seconds until the containers that come back by themselves are running, or "never" after 150 s
settle() {
    local start=${SECONDS} have n ok
    while [ $((SECONDS - start)) -lt 150 ]; do
        have="$(timeout -s KILL 5 docker ps --format '{{.Names}}' 2>/dev/null || true)"
        ok=1
        for n in "${COME_BACK[@]}"; do
            if ! grep -qxF "${n}" <<<"${have}"; then ok=0; fi
        done
        if [ "${ok}" = 1 ]; then
            echo "$((SECONDS - start))" >"${OUT}/$1"
            return 0
        fi
        sleep 2
    done
    echo never >"${OUT}/$1"
    return 1
}

# ---- talking to the core ------------------------------------------------------------------------------------------------------------------

internal_header() { # the gateway rewrites the secret at each start: read it again before every call
    local s=""
    IFS= read -r s 2>/dev/null <"${INTERNAL_SECRET_FILE}" || true
    printf 'Authorization: Internal %s' "${s}"
}

# api <name> <path> [seconds]: one GET, through the gateway, with the loopback secret. Written into $OUT: <name>.json (the body), .code (the HTTP status, 000 when
# there was none), .err (what curl said), .t0 and .t1 (the time before and after, in seconds since the epoch), and .curl last (how curl ended: 0, or why it got
# no answer; a call that was cut short leaves none, which the report reads as a call that did not run)
api() {
    local name="$1" path="$2" max="${3:-45}" code="" rc=0
    printf '%s\n' "${EPOCHREALTIME}" >"${OUT}/${name}.t0"
    code="$(curl -sS --max-time "${max}" -o "${OUT}/${name}.json" -w '%{http_code}' -H "$(internal_header)" "http://127.0.0.1:${CASA_PORT}${path}" 2>"${OUT}/${name}.err")" || rc=$?
    printf '%s\n' "${EPOCHREALTIME}" >"${OUT}/${name}.t1"
    printf '%s\n' "${code:-000}" >"${OUT}/${name}.code"
    printf '%s\n' "${rc}" >"${OUT}/${name}.curl"
}

# the call under test: longer than the core's 20 s budget, so that a call that blocks shows as one
ready_call() { api "$1" /v1/sys/os/readiness 45; }

# code_of <path> [seconds]: the HTTP status of a GET with the loopback secret, 000 when nothing answered
code_of() { curl -s -o /dev/null --max-time "${2:-5}" -w '%{http_code}' -H "$(internal_header)" "http://127.0.0.1:${CASA_PORT}$1" 2>/dev/null || true; }

# wait_services [seconds]: until the gateway, the core and AppManagement each answer 200, 180 s at the most. Status 1 when they do not.
wait_services() {
    local end=$((SECONDS + ${1:-180})) gw core am
    while :; do
        gw="$(code_of /)"
        core="$(code_of /v1/sys/version/current)"
        am="$(code_of /v2/app_management/categories)"
        if [ "${gw}" = 200 ] && [ "${core}" = 200 ] && [ "${am}" = 200 ]; then return 0; fi
        if [ "${SECONDS}" -ge "${end}" ]; then
            log "the gateway, the core and AppManagement did not all answer 200 (gateway ${gw}, core ${core}, AppManagement ${am})"
            return 1
        fi
        sleep 2
    done
}

# wait_image_check [seconds]: until AppManagement has made its own check of the images, 240 s at the most. It makes it three minutes after it starts and every six
# hours, and the check ends with a new /var/lib/casaos/image_updates.json renamed into place: a change of one of the trees the window hashes, which has nothing to
# do with the report. Once the file is there the next one is hours away. Status 1 when it did not come.
wait_image_check() {
    local end=$((SECONDS + ${1:-240}))
    while [ ! -e "${IMAGE_UPDATES}" ]; do
        if [ "${SECONDS}" -ge "${end}" ]; then return 1; fi
        sleep 5
    done
}

# ---- what is written down -------------------------------------------------------------------------------------------------------------------

# box_facts: the box and the tools the report leans on, for the report to show
box_facts() {
    {
        echo "kernel $(uname -r)"
        echo "cpus $(nproc)"
        echo "memory_mb $(free -m | awk '$1 == "Mem:" {print $2}')"
        if [ -d /sys/firmware/efi ]; then echo "boot efi"; else echo "boot bios"; fi
        echo "root $(findmnt -no FSTYPE,SOURCE / 2>&1 | tr -s ' ' ' ')"
        echo "boot_on $(df -P "${BOOT_DIR}" | awk 'NR == 2 {print $1}') (root: $(df -P / | awk 'NR == 2 {print $1}'))"
        echo "virt $(systemd-detect-virt 2>&1 || true)"
        echo "docker $(docker version --format '{{.Server.Version}}' 2>&1)"
        echo "apt $(apt-get --version 2>&1 | sed -n 1p)"
        echo "dpkg $(dpkg --version 2>&1 | sed -n 1p)"
        echo "systemd $(systemctl --version 2>&1 | sed -n 1p)"
    } >"${OUT}/box-facts" 2>&1 || true
    cp "${OS_RELEASE}" "${OUT}/os-release"
    df -P -m / /boot /var >"${OUT}/df-start.txt" 2>&1 || true
}

# leak_needles: what the text of the report must not say, because it is this box's: its name (given a name of this run's own, so that it cannot be a word),
# every address and MAC of its interfaces but the loopback, the user it logs in as
leak_needles() {
    {
        printf 'hostname\t%s\n' "${HOST_NAME}"
        ip -o -4 addr show 2>/dev/null | awk '$2 != "lo" {split($4, a, "/"); print "ipv4\t" a[1]}'
        ip -o -6 addr show 2>/dev/null | awk '$2 != "lo" {split($4, a, "/"); print "ipv6\t" a[1]}'
        ip -o link show 2>/dev/null | awk '$2 != "lo:" {for (i = 1; i < NF; i++) if ($i == "link/ether") print "mac\t" $(i + 1)}'
        printf 'user\t%s\n' "${SUDO_USER:-casa}"
    } >"${OUT}/leak-needles.tsv"
}

# units_dump <tag>: every service unit with its state, its main pid and its InvocationID (which changes each time systemd starts the unit again: a unit whose
# InvocationID differs between two dumps was started again in between)
units_dump() {
    local names=()
    mapfile -t names < <(systemctl list-units --type=service --all --no-legend --plain 2>/dev/null | awk '{print $1}')
    if [ "${#names[@]}" -gt 0 ]; then
        systemctl show -p Id -p ActiveState -p SubState -p MainPID -p InvocationID "${names[@]}" 2>/dev/null |
            awk -F= 'BEGIN {OFS = "\t"}
                /^$/ { if (id != "") print id, a, s, m, inv; id = a = s = m = inv = ""; next }
                $1 == "Id" { id = substr($0, 4) }
                $1 == "ActiveState" { a = $2 }
                $1 == "SubState" { s = $2 }
                $1 == "MainPID" { m = $2 }
                $1 == "InvocationID" { inv = $2 }
                END { if (id != "") print id, a, s, m, inv }' >"${OUT}/units-$1.tsv" || true
    else
        : >"${OUT}/units-$1.tsv"
    fi
}

# listen_dump <tag>: the listening sockets, TCP and UDP, as "<protocol> <local address:port>", sorted
listen_dump() { ss -Hltun 2>/dev/null | awk '{print $1, $5}' | sort >"${OUT}/listen-$1.txt" || true; }

# inv_snapshot <tag>: the files of the six trees, the units and the sockets, as they are now
inv_snapshot() {
    python3 "${LIBDIR}/manifest.py" "${INV_TREES[@]}" >"${OUT}/inv-$1.tsv" || abort "the manifest of the files could not be made"
    units_dump "$1"
    listen_dump "$1"
}

# ---- dpkg's lock ----------------------------------------------------------------------------------------------------------------------------

# lock_state: held, free or missing, asked by a process of its own (a POSIX lock is per process: it conflicts with the holder's)
lock_state() { python3 "${LIBDIR}/lockprobe.py" "${DPKG_LOCK}"; }

# lock_hold_start: another process takes dpkg's lock and keeps it for 300 s; its pid is kept in a file, because the dirt of a step is made in a subshell
lock_hold_start() {
    local i
    python3 "${LIBDIR}/lockhold.py" "${DPKG_LOCK}" 300 >"${AUTH_DIR}/lock.out" 2>&1 &
    echo "$!" >"${AUTH_DIR}/lock.pid"
    for i in $(seq 1 20); do
        if grep -q locked "${AUTH_DIR}/lock.out"; then return 0; fi
        sleep 0.5
    done
    echo "could not take dpkg's lock: $(cat "${AUTH_DIR}/lock.out")" >&2
    return 1
}

# lock_hold_stop: lets go of it, and waits until it is free
lock_hold_stop() {
    local pid i
    [ -f "${LIBDIR}/lockprobe.py" ] || return 0
    if [ -s "${AUTH_DIR}/lock.pid" ]; then
        pid="$(cat "${AUTH_DIR}/lock.pid")"
        kill "${pid}" 2>/dev/null || true
        : >"${AUTH_DIR}/lock.pid"
    fi
    for i in $(seq 1 20); do
        if [ "$(lock_state)" = free ]; then return 0; fi
        sleep 0.5
    done
    return 1
}

# ---- the invariance window ----------------------------------------------------------------------------------------------------------------------

invariance() {
    marker control_start
    inv_snapshot ctl-a
    api ctl-core-1 /v1/sys/version/current 20
    api ctl-core-2 /v1/sys/version/current 20
    sleep 5
    inv_snapshot ctl-b
    systemctl list-timers --all --no-legend --plain 2>/dev/null | awk 'NF {print $NF}' | sort -u >"${OUT}/timer-units.txt" || true
    marker invariance_start
    inv_snapshot before
    api sysos-inv /v1/sys/os 30
    ready_call ready-inv-1
    ready_call ready-inv-2
    ready_call ready-inv-3
    # the call under dpkg's lock, held by another process: it has to answer, inside its budget, and leave the lock where it found it
    lock_hold_start
    lock_state >"${OUT}/lock-probe-during"
    ready_call ready-inv-lock
    lock_state >"${OUT}/lock-probe-after"
    lock_hold_stop || abort "dpkg's lock was not given back"
    inv_snapshot after
    marker invariance_done
    # the answer of the box as it is, with nothing held: what each step of the matrix is compared with
    ready_call ready-base
}

# ---- the dirty matrix: dirty_<id>, undo_<id>, undone_<id> for every id of STEP_IDS (skip_<id> says why a step does not exist on this system) --------------------------

# a source file of apt: src_dirty <file name> <line>, src_undo <file name>, src_undone <file name>
src_dirty() { mkdir -p "${APT_DIR}/sources.list.d" && printf '%s\n' "$2" >"${APT_DIR}/sources.list.d/$1"; }
src_undo() { rm -f "${APT_DIR}/sources.list.d/$1"; }
src_undone() { [ ! -e "${APT_DIR}/sources.list.d/$1" ]; }

# held: libc6 is held, as someone who does not want it to move would do (the check names it)
dirty_held() { apt-mark hold libc6; }
undo_held() { apt-mark unhold libc6; }
undone_held() { [ -z "$(apt-mark showhold)" ]; }

# held_docker: the Docker family on hold is harmless (the source is paused for the hop): it is `held_docker`, info, and not `held`
dirty_held_docker() { apt-mark hold docker-ce; }
undo_held_docker() { apt-mark unhold docker-ce; }
undone_held_docker() { [ -z "$(apt-mark showhold)" ]; }

dirty_broken_dpkg() { : >"${DPKG_JOURNAL}"; }
undo_broken_dpkg() { rm -f "${DPKG_JOURNAL}"; }
undone_broken_dpkg() { [ ! -e "${DPKG_JOURNAL}" ]; }

dirty_sources_cdrom() { src_dirty osr-cdrom.list "deb cdrom:[Readiness proof disc 1]/ ${FROM_CODENAME} main"; }
undo_sources_cdrom() { src_undo osr-cdrom.list; }
undone_sources_cdrom() { src_undone osr-cdrom.list; }

dirty_sources_file() { src_dirty osr-file.list "deb [trusted=yes] file:/srv/osr-local-repo ./"; }
undo_sources_file() { src_undo osr-file.list; }
undone_sources_file() { src_undone osr-file.list; }

# a source that names a release by an alias where the box is on a codename: Debian's `stable`, Ubuntu's `devel` (dists/devel is there on archive.ubuntu.com,
# and the core only reads the line: nothing is fetched)
dirty_sources_alias() {
    if [ "${FAMILY}" = ubuntu ]; then
        src_dirty osr-alias.list "deb http://archive.ubuntu.com/ubuntu devel main"
    else
        src_dirty osr-alias.list "deb http://deb.debian.org/debian stable main"
    fi
}
undo_sources_alias() { src_undo osr-alias.list; }
undone_sources_alias() { src_undone osr-alias.list; }

# a Default-Release that names the box's own release, and a pin of the preferences that names the target: a Default-Release that no source offers (the target's,
# which the sources do not carry before the hop) makes apt itself refuse to build its policy ("E: The value 'bookworm' is invalid for APT::Default-Release as such
# a release is not available in the sources", status 100), in apt-get check and in apt-cache policy, which the core runs: they would fail with the dirt, and the
# core with them. The core refuses any Default-Release, so the box's own is as good. An unmatched `Pin: release n=` is no error of apt's.
dirty_sources_pin() { mkdir -p "${APT_DIR}/apt.conf.d" && printf 'APT::Default-Release "%s";\n' "${FROM_CODENAME}" >"${APT_DIR}/apt.conf.d/99osr-default-release"; }
undo_sources_pin() { rm -f "${APT_DIR}/apt.conf.d/99osr-default-release"; }
undone_sources_pin() { [ ! -e "${APT_DIR}/apt.conf.d/99osr-default-release" ]; }

dirty_sources_pin_prefs() { mkdir -p "${APT_DIR}/preferences.d" && printf 'Package: *\nPin: release n=%s\nPin-Priority: 100\n' "${TO_CODENAME}" >"${APT_DIR}/preferences.d/99osr-pin"; }
undo_sources_pin_prefs() { rm -f "${APT_DIR}/preferences.d/99osr-pin"; }
undone_sources_pin_prefs() { [ ! -e "${APT_DIR}/preferences.d/99osr-pin" ]; }

dirty_sources_proposed() {
    if [ "${FAMILY}" = ubuntu ]; then
        src_dirty osr-proposed.list "deb http://archive.ubuntu.com/ubuntu ${FROM_CODENAME}-proposed main universe"
    else
        src_dirty osr-proposed.list "deb http://deb.debian.org/debian ${FROM_CODENAME}-proposed-updates main"
    fi
}
undo_sources_proposed() { src_undo osr-proposed.list; }
undone_sources_proposed() { src_undone osr-proposed.list; }

# a source that is neither Debian's nor Ubuntu's, with a file name the report must name. Docker's own source is one too, and is on the clean box already.
dirty_sources_third_party() { src_dirty osr-third-party.list "deb https://repo.example.invalid/osr-third-party stable main"; }
undo_sources_third_party() { src_undo osr-third-party.list; }
undone_sources_third_party() { src_undone osr-third-party.list; }

# an architecture that is added and has no package: nothing to say. Then one package of it, a package that no repository has and that needs no network:
# the check refuses (a libc6 of another architecture is what shifted the libc6 of a dist-upgrade: Debian bug 1108193).
dirty_foreign_arch_empty() { dpkg --add-architecture i386; }
undo_foreign_arch_empty() { dpkg --remove-architecture i386; }
undone_foreign_arch_empty() { [ -z "$(dpkg --print-foreign-architectures)" ]; }

dirty_foreign_arch() {
    local d
    dpkg --add-architecture i386
    d="$(mktemp -d)"
    mkdir -p "${d}/DEBIAN"
    printf 'Package: osr-foreign\nVersion: 1.0\nArchitecture: i386\nMaintainer: os-readiness-proof <root@localhost>\nDescription: no repository has this package\n' >"${d}/DEBIAN/control"
    dpkg-deb --build "${d}" "${d}.deb" >/dev/null
    dpkg -i "${d}.deb" >/dev/null
    rm -rf "${d}" "${d}.deb"
}
undo_foreign_arch() {
    dpkg --purge osr-foreign:i386
    dpkg --remove-architecture i386
}
undone_foreign_arch() { [ -z "$(dpkg --print-foreign-architectures)" ] && ! installed osr-foreign; }

dirty_reboot_first() { : >"${REBOOT_REQUIRED}"; }
undo_reboot_first() { rm -f "${REBOOT_REQUIRED}"; }
undone_reboot_first() { [ ! -e "${REBOOT_REQUIRED}" ]; }

# a line of fstab, not nofail, for a device that does not exist: the machine would stop in rescue mode at its first boot. No daemon-reload: systemd does not see it.
dirty_fstab_unresolved() {
    [ ! -e "${AUTH_DIR}/fstab.orig" ]
    cp -p "${FSTAB}" "${AUTH_DIR}/fstab.orig"
    printf 'UUID=0a1b2c3d-0000-4000-8000-000000000001 /mnt/osr-missing ext4 defaults 0 2\n' >>"${FSTAB}"
}
undo_fstab_unresolved() { cp -p "${AUTH_DIR}/fstab.orig" "${FSTAB}"; }
undone_fstab_unresolved() { cmp -s "${AUTH_DIR}/fstab.orig" "${FSTAB}" && rm -f "${AUTH_DIR}/fstab.orig"; }

# a container that is running and will not come back after a reboot (restart policy no)
dirty_containers_policy() { docker run -d --init --name p-no --restart no busybox sleep 100000 >/dev/null; }
undo_containers_policy() { docker rm -f p-no >/dev/null; }
undone_containers_policy() { [ -z "$(docker ps -aq --filter name=^p-no$)" ]; }

# the machine may be the DNS or the web server: a container on the host network, one that publishes port 53 (the policy of both comes back by itself). Not 80 nor 443:
# ReCasaOS's own gateway listens on *:80 and *:443, and Docker cannot bind them again. Not 53 on every address either: systemd-resolved holds 127.0.0.53:53, and a
# bind of 0.0.0.0:53 fails with it. 127.0.0.2:53 is free on Debian 11 and on Ubuntu 22.04, and the core reports a host port of 53 whatever the address.
dirty_containers_ports_host() { docker run -d --init --name p-hostnet --restart unless-stopped --network host busybox sleep 100000 >/dev/null; }
undo_containers_ports_host() { docker rm -f p-hostnet >/dev/null; }
undone_containers_ports_host() { [ -z "$(docker ps -aq --filter name=^p-hostnet$)" ]; }

dirty_containers_ports_publish() { docker run -d --init --name p-pub53 --restart unless-stopped -p 127.0.0.2:53:80 nginx:alpine >/dev/null; }
undo_containers_ports_publish() { docker rm -f p-pub53 >/dev/null; }
undone_containers_ports_publish() { [ -z "$(docker ps -aq --filter name=^p-pub53$)" ]; }

# a package manager at work: dpkg's lock held by another process; and a package update that is running (a stand-in that holds the unit's name, which is all the
# core looks at: the Docker update proof does the same)
dirty_busy_lock() { lock_hold_start; }
undo_busy_lock() { lock_hold_stop; }
undone_busy_lock() { [ "$(lock_state)" = free ]; }

dirty_busy_unit() {
    systemd-run --quiet --unit=casaos-package-update.service --collect --property=Type=exec /bin/sleep 300
    [ "$(systemctl is-active casaos-package-update.service)" = active ]
}
undo_busy_unit() { systemctl stop casaos-package-update.service; }
undone_busy_unit() { [ "$(systemctl is-active casaos-package-update.service || true)" != active ]; }

# a file that leaves 200 MiB free on the filesystem of /boot: fallocate takes the blocks without writing them. On these images /boot is on the root filesystem,
# so the disk checks of / and of apt's archives fire with it.
dirty_disk_boot() {
    local avail size
    avail="$(df -Pm "${BOOT_DIR}" | awk 'NR == 2 {print $4}')"
    [ "${avail}" -gt 400 ]
    size=$((avail - 200))
    fallocate -l "${size}M" "${BOOT_DIR}/osr-fill"
    echo "a file of ${size} MiB on a filesystem that had ${avail} MiB free" >"${OUT}/step-disk_boot.note"
}
undo_disk_boot() { rm -f "${BOOT_DIR}/osr-fill"; }
undone_disk_boot() { [ ! -e "${BOOT_DIR}/osr-fill" ]; }

# AppManagement stopped: the core cannot ask which apps have an operation, and fails closed
dirty_apps() { systemctl stop casaos-app-management.service; }
undo_apps() {
    systemctl start casaos-app-management.service
    wait_services 120
}
undone_apps() { [ "$(code_of /v2/app_management/categories)" = 200 ]; }

# Docker stopped (and its socket: it would start it again at the first request)
dirty_docker_down() { systemctl stop docker.socket docker.service; }
undo_docker_down() {
    systemctl start docker.socket docker.service
    wait_docker 120
    settle step-docker_down.settle
    wait_services 120
}
undone_docker_down() { timeout -s KILL 5 docker info >/dev/null 2>&1; }

# run_step <id>: the dirt, a call, the undo, a check that the undo held, a call. The undo is pending from before the dirt to after the undo, and the trap of
# the script runs it when the script dies in between.
run_step() {
    local id="$1" reason=""
    echo "${id}" >>"${OUT}/steps.list"
    if declare -F "skip_${id}" >/dev/null; then reason="$("skip_${id}" || true)"; fi
    if [ -n "${reason}" ]; then
        printf '%s\n' "${reason}" >"${OUT}/step-${id}.skipped"
        log "step ${id}: not on this system (${reason})"
        return 0
    fi
    marker "step_${id}"
    PENDING_UNDO="${id}"
    in_subshell "dirty_${id}" >"${OUT}/step-${id}.dirty.log" 2>&1
    echo "${STATUS}" >"${OUT}/step-${id}.dirty-rc"
    ready_call "step-${id}-dirty"
    in_subshell "undo_${id}" >"${OUT}/step-${id}.undo.log" 2>&1
    echo "${STATUS}" >"${OUT}/step-${id}.undo-rc"
    # an undo that failed stays pending: the trap of the script tries it once more, so that the box is not left dirty
    if [ "${STATUS}" -eq 0 ]; then PENDING_UNDO=""; fi
    in_subshell "undone_${id}" >>"${OUT}/step-${id}.undo.log" 2>&1
    if [ "${STATUS}" -eq 0 ]; then echo yes >"${OUT}/step-${id}.undone"; else echo no >"${OUT}/step-${id}.undone"; fi
    ready_call "step-${id}-clean"
    marker "step_${id}_done"
    log "step ${id}: dirty exit $(cat "${OUT}/step-${id}.dirty-rc"), undo exit $(cat "${OUT}/step-${id}.undo-rc"), undone $(cat "${OUT}/step-${id}.undone")"
}

# ---- the run ------------------------------------------------------------------------------------------------------------------------------------------

# update_old: apt-get update, as tolerant as the old release needs and no more: a changed Label, Origin or Codename is accepted, and a backports line that its
# mirror no longer serves is commented out. What was done goes to $OUT/prepare-notes.
update_old() {
    local rc=0 log="${OUT}/prepare-update.log" f
    "${APT[@]}" update -qq >"${log}" 2>&1 || rc=$?
    if [ "${rc}" -ne 0 ] && grep -Eq "^E: .*changed its '(Label|Origin|Codename)' value" "${log}"; then
        echo "update: --allow-releaseinfo-change was needed" >>"${OUT}/prepare-notes"
        rc=0
        "${APT[@]}" update -qq --allow-releaseinfo-change >>"${log}" 2>&1 || rc=$?
    fi
    if [ "${rc}" -ne 0 ] && grep -q 'backports' "${log}"; then
        echo "update: the backports lines were commented out (their repository answers 404)" >>"${OUT}/prepare-notes"
        for f in "${APT_DIR}/sources.list" "${APT_DIR}/sources.list.d"/*.list; do
            if [ -f "${f}" ]; then sed -i -E 's/^([[:space:]]*deb(-src)?[[:space:]].*-backports.*)$/# \1/' "${f}"; fi
        done
        rc=0
        "${APT[@]}" update -qq >>"${log}" 2>&1 || rc=$?
    fi
    [ "${rc}" -eq 0 ] || abort "apt-get update fails (${log})"
}

main() {
    set -euo pipefail
    LEG="${1:-}"
    local facts
    facts="$(leg_facts "${LEG}")" || { echo "usage: $0 d11|u22" >&2; exit 2; }
    read -r FAMILY FROM_VERSION FROM_CODENAME TO_VERSION TO_CODENAME <<<"${facts}"
    [[ "${READINESS_DISPOSABLE:-}" == 1 ]] ||
        { echo "This installs Docker, holds dpkg's lock, fills a disk and stops Docker: run it on a disposable machine (READINESS_DISPOSABLE=1)." >&2; exit 2; }
    [ "$(id -u)" -eq 0 ] || { echo "Run it as root." >&2; exit 2; }
    if [ -e /etc/casaos ] || [ -e /var/lib/casaos ] || command -v docker >/dev/null 2>&1; then
        echo "This machine already runs ReCasaOS or Docker: not a fresh one, not touching it." >&2
        exit 2
    fi
    # shellcheck source=/dev/null
    . "${OS_RELEASE}"
    test "${ID}" = "${FAMILY}"
    test "${VERSION_ID}" = "${FROM_VERSION}"
    test "${VERSION_CODENAME}" = "${FROM_CODENAME}"

    export LC_ALL=C
    OUT="${OUT:-${PWD}/readiness-out}"
    mkdir -p "${OUT}" "${AUTH_DIR}" "${LIBDIR}"
    chmod 700 "${AUTH_DIR}"
    : >"${OUT}/timeline.tsv"
    echo "${LEG}" >"${OUT}/leg"
    echo "${PRETTY_NAME}" >"${OUT}/os"
    PENDING_UNDO=""
    guard_run
    [ -n "${EPOCHREALTIME:-}" ] || abort "this bash has no EPOCHREALTIME (bash 5 is needed)"

    export DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none NEEDRESTART_SUSPEND=1
    APT=(env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3)
    marker prepare_start
    cloud-init status --wait >/dev/null 2>&1 || true
    # a first boot of Ubuntu is still seeding its snaps for a while
    if command -v snap >/dev/null 2>&1; then timeout 300 snap wait system seed.loaded >/dev/null 2>&1 || true; fi
    # apt's own timers would run in the middle of the proof, and hold the lock the report asks about
    systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service >/dev/null 2>&1 || true
    if [ "${ID}" = debian ] && [ "${VERSION_ID}" = 11 ] &&
        grep -Eq '^[[:space:]]*deb(-src)?[[:space:]].*//(deb|security)\.debian\.org/debian-security' "${APT_DIR}/sources.list" 2>/dev/null; then
        # out of support: the change --use-debian-archive makes, made first because Docker's own installer runs apt before ReCasaOS's does
        cp -p "${APT_DIR}/sources.list" "${APT_DIR}/sources.list.osr.bak"
        sed -i -E 's#//(deb|security)\.debian\.org/debian-security#//archive.debian.org/debian-security#' "${APT_DIR}/sources.list"
    fi
    update_old
    "${APT[@]}" install -y -q curl jq ca-certificates python3 procps iproute2 >/dev/null
    write_manifest_tool "${LIBDIR}/manifest.py"
    write_lock_tools "${LIBDIR}"
    # a name of this run's own: it cannot be a word of the text, so that finding it there is a leak and nothing else
    HOST_NAME="osr-$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    hostnamectl set-hostname "${HOST_NAME}"
    # sudo and the services look the name up: without a line for it every `sudo` of the run says "unable to resolve host"
    echo "127.0.1.1 ${HOST_NAME}" >>/etc/hosts
    install_docker
    install_casaos
    start_things
    "${APT[@]}" update -qq
    wait_services 180 || abort "the gateway, the core and AppManagement do not all answer after the installation"
    timeout 120 systemctl is-system-running --wait >/dev/null 2>&1 || true
    box_facts
    leak_needles
    # AppManagement's check of the images lands in /var/lib/casaos three minutes after its start: it is over before the control looks at what changes by itself
    wait_image_check 240 || not_proven "AppManagement did not write ${IMAGE_UPDATES} within 240 s: its own check of the images (3 minutes after its start) could fall in the invariance window, and be taken for a write of the report"
    marker prepare_done
    # what the installation started has a few seconds to be quiet: the control below looks at what still changes by itself
    sleep 20

    invariance
    local id
    for id in "${STEP_IDS[@]}"; do run_step "${id}"; done
    date -u +%FT%TZ >"${OUT}/finished"
    log "done"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then main "$@"; fi
