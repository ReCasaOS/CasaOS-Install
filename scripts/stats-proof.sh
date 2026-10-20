#!/usr/bin/env bash
# Proof of the statistics of ReCasaOS on a real machine: the machine's own (the core, /v1/sys/stats), the containers' (AppManagement on a real Docker, /v2/app_management/stats)
# and the virtual machines' (the virt module with libvirt's test driver, /v2/virt_management/stats), with their history files, their alerts and the hub that tells them.
#
# Like module-proof.sh, whose runner helpers it sources (the clock, ssh to the guest, the calls through the gateway with the token of the box's own user, the restart of the guest), this
# script runs ON THE CI RUNNER and drives the guest: the core, Docker and libvirt are stopped on purpose and the guest restarts, so the evidence is kept on the side that survives it. What
# only the guest can say is asked of a small POSIX sh tool the preparation writes on it. Nothing is judged here: it writes what it saw into $OUT, and stats-proof-report.py turns that into
# PASS, FAIL, UNVERIFIED or INVALID RUN. A step of the harness that could not be made is recorded as such (invalid.txt), never as a failure of the services.
#
#   stats-proof.sh guest-prepare     IN THE GUEST, as root: the published release with its installer, the virt module from the release's catalogue, the tools of the proof
#   stats-proof.sh prepare           on the runner: pushes the files, runs guest-prepare, fetches what it wrote, makes the box's user and logs in
#   stats-proof.sh run d12|u24       on the runner: the cases, in the order below
#   stats-proof.sh check             the guest tool parses (sh -n), for the CI step that runs before anything else
#
# The order (each case leaves the machine as the next one needs it):
#   V3 begins: the module is pointed at a test driver with no domain (an empty node XML); its list is read 3 minutes later
#   H1 the routes of the core; H4 its alert rules, a CPU hog on every vCPU, the alert fires and resolves; the hub's category and, for C1, a channel of the proof's own on the guest
#   V3 ends, and the module is pointed at test:///default
#   A1 a hand-launched busy container, A2 a compose project (a cpus: 0.5 service, a service without a limit), then H1's series, H2's files, A1, A2 and V1 (the VM of the test driver), V5
#   A3 a container alert that fires, then a stop (137), a stop that ends on SIGTERM (143), a kill: stopped, the alert resolved, a removed container's history by id
#   H3 the core stopped for more than two minutes and started: the earlier minutes stay, the minutes it was away are null
#   A4 Docker stopped for more than two minutes and started; A5 AppManagement restarted
#   H5, A6, V6 the CPU of the three services over 3 minutes, with 10 more containers running
#   V2 the guest restarts: the VM's history stays; V4 libvirtd restarts: the route comes back, a gap, no spike
#   C1 what reached the proof's channel; C2 the restarts and panics of the units over the run
#
# Needs, on the runner: bash 5, curl, python3, ssh, scp, timeout. Environment: as module-proof.sh (OUT, WORK, SSH_CONFIG, SSH_HOST, GW_PORT, MP_FAST), plus HOG_SECONDS (150),
# CORE_DOWN (130), DOCKER_DOWN (130), COST_SECONDS (180), V3_WAIT (180).

SP_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=module-proof.sh
. "${SP_HERE}/module-proof.sh"

# shellcheck disable=SC2034
# (GUEST_TOOLS_DIR and PROOF_USER are read by the helpers of module-proof.sh: g, login)
GUEST_TOOLS_DIR=/usr/local/lib/sp
# shellcheck disable=SC2034
PROOF_USER=spproof
REPORT="${SP_HERE}/stats-proof-report.py"
HOG_SECONDS="${HOG_SECONDS:-150}"
CORE_DOWN="${CORE_DOWN:-130}"
DOCKER_DOWN="${DOCKER_DOWN:-130}"
COST_SECONDS="${COST_SECONDS:-180}"
V3_WAIT="${V3_WAIT:-180}"
AM=/v2/app_management/stats
VT=/v2/virt_management/stats
CORE=/v1/sys/stats
HOOK_URL='generic://127.0.0.1:18089/hook?disabletls=yes'

# sp_write_guest_tools <dir>: what the runner asks of the guest, one verb a call (POSIX sh: dash runs it), and the small files the cases use
sp_write_guest_tools() {
    mkdir -p "$1/stproof"
    cat >"$1/guest.sh" <<'SP_GUEST'
#!/bin/sh
# What the statistics proof asks of the guest, one verb a call. POSIX sh (dash).
LC_ALL=C; export LC_ALL
PATH=/usr/sbin:/usr/bin:/sbin:/bin; export PATH
SP=/usr/local/lib/sp
STATS=/var/lib/casaos/stats
VCONF=/etc/casaos/virt-management.conf
UNITS="casaos.service casaos-gateway.service casaos-user-service.service casaos-message-bus.service casaos-local-storage.service casaos-app-management.service casaos-virt-management.service"

stamp() { printf 'stamp\t%s\t%s\n' "$1" "$(date +%s.%N)"; }

facts() {
    printf 'hostname\t%s\n' "$(hostname)"
    (. /etc/os-release 2>/dev/null; printf 'os\t%s %s\n' "${ID:-}" "${VERSION_ID:-}")
    printf 'kernel\t%s\n' "$(uname -r)"
    printf 'nproc\t%s\n' "$(nproc)"
    printf 'cgroup\t%s\n' "$(stat -fc %T /sys/fs/cgroup)"
    printf 'clk_tck\t%s\n' "$(getconf CLK_TCK)"
    printf 'docker\t%s\n' "$(docker version --format '{{.Server.Version}}' 2>&1 | head -n 1)"
    printf 'compose\t%s\n' "$(docker compose version --short 2>&1 | head -n 1)"
    printf 'casaos\t%s\n' "$(casaos -v 2>&1 | head -n 1)"
    printf 'app_management\t%s\n' "$(casaos-app-management -v 2>&1 | head -n 1)"
    printf 'virt_management\t%s\n' "$(casaos-virt-management -v 2>&1 | head -n 1)"
    printf 'libvirt\t%s\n' "$(virsh --version 2>&1 | head -n 1)"
}

guest_now() { stamp now; }
btime() { awk '$1 == "btime" {print "btime\t" $2}' /proc/stat; }

# ring_ls <service>: the directory of the service's history (its mode and owner) and each file in it (name, size, mode, owner, type)
ring_ls() {
    d="$STATS/$1"
    if [ ! -d "$d" ]; then echo nodir; return 0; fi
    printf 'dir\t%s\t%s\n' "$(stat -c %a "$d")" "$(stat -c %U:%G "$d")"
    find "$d" -mindepth 1 -maxdepth 1 -printf 'file\t%f\t%s\t%m\t%u:%g\t%y\n' | sort
}

# cpu: the CPU time of the three services' main processes (utime, stime, cutime, cstime in ticks) and how many containers run
cpu() {
    printf 'clk_tck\t%s\n' "$(getconf CLK_TCK)"
    printf 'uptime\t%s\n' "$(cut -d' ' -f1 /proc/uptime)"
    for u in casaos.service casaos-app-management.service casaos-virt-management.service; do
        pid="$(systemctl show -p MainPID --value "$u" 2>/dev/null)"
        if [ -n "$pid" ] && [ "$pid" != 0 ] && [ -r "/proc/$pid/stat" ]; then
            # shellcheck disable=SC2046
            set -- $(sed 's/^.*) //' "/proc/$pid/stat")
            printf 'unit\t%s\t%s\t%s\t%s\t%s\t%s\n' "$u" "$pid" "${12}" "${13}" "${14}" "${15}"
        else
            printf 'unit\t%s\t-\t-\t-\t-\t-\n' "$u"
        fi
    done
    printf 'containers\t%s\n' "$(docker ps -q 2>/dev/null | wc -l | tr -d ' ')"
}

units() {
    for u in $UNITS docker.service libvirtd.service; do
        printf 'unit\t%s\t%s\t%s\t%s\n' "$u" "$(systemctl show -p NRestarts --value "$u" 2>/dev/null)" "$(systemctl show -p ActiveState --value "$u" 2>/dev/null)" \
            "$(systemctl show -p InvocationID --value "$u" 2>/dev/null)"
    done
}

# journal_scan: the lines of this boot that say panic, per unit, and the lines of the three stats components that name stats at error level (their journal and their log file)
journal_scan() {
    for u in $UNITS; do
        printf 'panic\t%s\t%s\n' "$u" "$(journalctl -b -u "$u" --no-pager -o cat 2>/dev/null | grep -ci 'panic')"
    done
    for u in casaos.service casaos-app-management.service casaos-virt-management.service; do
        journalctl -b -u "$u" --no-pager -o cat 2>/dev/null | grep -i stats | grep -iE 'error|"level":"err' | tail -n 20 | sed "s|^|error\t$u\t|"
    done
    for f in /var/log/casaos/casaos.log /var/log/casaos/app-management.log /var/log/casaos/virt-management.log; do
        if [ -f "$f" ]; then grep -i stats "$f" | grep -iE '"level":"error"|error' | tail -n 20 | sed "s|^|error\t$f\t|"; fi
    done
}

hog_start() {
    stamp before
    systemd-run --unit=sp-hog --collect -p RuntimeMaxSec="$1" /bin/sh -c 'n=$(nproc); i=0; while [ "$i" -lt "$n" ]; do yes >/dev/null & i=$((i + 1)); done; wait' 2>&1
    stamp after
}
hog_stop() { printf 'was\t%s\n' "$(systemctl is-active sp-hog.service 2>/dev/null)"; systemctl stop sp-hog.service 2>/dev/null; stamp after; }

hook_start() {
    systemd-run --unit=sp-hook --collect python3 "$SP/hook.py" "$SP/hook.log" 2>&1
    i=0
    while [ "$i" -lt 20 ]; do
        if curl -s -o /dev/null -X POST --data 'probe' http://127.0.0.1:18089/probe; then echo listening; return 0; fi
        sleep 1; i=$((i + 1))
    done
    echo "not listening"; return 1
}
hook_log() { cat "$SP/hook.log" 2>/dev/null; }

busy_start() { docker run -d --name statproof busybox sh -c 'while :; do :; done'; }
aux_start() { docker run -d --init --name stattrm busybox sleep 100000 && docker run -d --name statkill busybox sleep 100000; }
compose_up() { docker compose -p stproof -f "$SP/stproof/compose.yml" up -d 2>&1; }
# stop_all: statproof's shell ignores SIGTERM (it is PID 1 with no handler): `docker stop` ends it on SIGKILL (137); stattrm's sleep, under --init, ends on SIGTERM (143); statkill is killed (137)
stop_all() {
    stamp before
    docker stop -t 3 statproof >/dev/null && docker stop -t 20 stattrm >/dev/null && docker kill statkill >/dev/null
    rc=$?
    stamp after
    docker inspect -f '{{.Name}} {{.State.Status}} {{.State.ExitCode}}' statproof stattrm statkill | sed 's|^/||; s|^|exit\t|'
    return "$rc"
}
rm_trm() { docker rm -f stattrm; }
idle_start() { i=1; while [ "$i" -le "$1" ]; do docker run -d --init --name "sp-idle-$i" busybox sleep 100000 >/dev/null || return 1; i=$((i + 1)); done; docker ps -q | wc -l; }
idle_stop() { i=1; while [ "$i" -le 10 ]; do docker rm -f "sp-idle-$i" >/dev/null 2>&1; i=$((i + 1)); done; echo removed; }
docker_down() { stamp before; systemctl stop docker.socket docker.service; stamp after; printf 'active\t%s\n' "$(systemctl is-active docker.service)"; }
docker_up() { stamp before; systemctl start docker.service; stamp after; printf 'active\t%s\n' "$(systemctl is-active docker.service)"; }
core_stop() { stamp before; systemctl stop casaos.service; stamp after; printf 'active\t%s\n' "$(systemctl is-active casaos.service)"; }
core_start() { stamp before; systemctl start casaos.service; stamp after; printf 'active\t%s\n' "$(systemctl is-active casaos.service)"; }
am_restart() { stamp before; systemctl restart casaos-app-management.service; stamp after; printf 'active\t%s\n' "$(systemctl is-active casaos-app-management.service)"; }
libvirtd_restart() { stamp before; systemctl restart libvirtd.service; stamp after; printf 'active\t%s\n' "$(systemctl is-active libvirtd.service)"; }
unit_state() { systemctl is-active "$1"; }

uri_of() { case "$1" in default) echo 'test:///default' ;; empty) echo "test://$SP/empty.xml" ;; *) return 2 ;; esac; }

# virt_conf <default|empty>: the module's libvirt URI, and the module started again on it
virt_conf() {
    uri="$(uri_of "$1")" || { echo "unknown driver: $1"; return 2; }
    [ -f "$VCONF" ] || { echo "no $VCONF"; return 1; }
    sed -i "s|^URI[[:space:]]*=.*\$|URI = $uri|" "$VCONF"
    stamp before
    systemctl restart casaos-virt-management.service
    stamp after
    printf 'uri\t%s\n' "$(sed -n 's/^URI[[:space:]]*=[[:space:]]*//p' "$VCONF")"
    printf 'active\t%s\n' "$(systemctl is-active casaos-virt-management.service)"
}

# virsh_list <default|empty>: what the test driver itself holds (every domain's uuid, and the uuid of the domain named test)
virsh_list() {
    uri="$(uri_of "$1")" || return 2
    virsh -c "$uri" list --all --uuid >"$SP/virsh.out" 2>"$SP/virsh.err" || { cat "$SP/virsh.err"; return 1; }
    sed -n 's/^\([0-9a-f-]\{36\}\)$/uuid\t\1/p' "$SP/virsh.out"
    t="$(virsh -c "$uri" domuuid test 2>/dev/null | sed -n 1p)"
    if [ -n "$t" ]; then printf 'test\t%s\n' "$t"; fi
    echo done
}

# undo_all: whatever a case left stopped is started again, whatever it started is stopped (the end of a run that stopped early)
undo_all() {
    systemctl stop sp-hog.service 2>/dev/null
    systemctl start docker.socket docker.service casaos.service casaos-app-management.service casaos-virt-management.service libvirtd.service 2>/dev/null
    echo undone
}

diag() {
    systemctl --failed --no-pager 2>&1
    for u in casaos.service casaos-app-management.service casaos-virt-management.service libvirtd.service docker.service; do
        echo "== $u"
        journalctl -b -u "$u" --no-pager -o short-iso 2>&1 | tail -n 40
    done
    for f in /var/log/casaos/casaos.log /var/log/casaos/app-management.log /var/log/casaos/virt-management.log; do
        echo "== $f"
        tail -n 40 "$f" 2>&1
    done
    echo "== docker ps"
    docker ps -a 2>&1 | head -n 30
    echo "== stats"
    ls -la "$STATS"/* 2>&1
}

verb="${1:-}"
[ "$#" -eq 0 ] || shift
case "${verb}" in
boot_id) cat /proc/sys/kernel/random/boot_id ;;
hostname) hostname ;;
reboot) nohup sh -c 'sleep 2; systemctl reboot' >/dev/null 2>&1 </dev/null & echo scheduled ;;
facts | guest_now | btime | cpu | units | journal_scan | hook_log | busy_start | aux_start | compose_up | stop_all | rm_trm | idle_stop | docker_down | docker_up | core_stop | \
    core_start | am_restart | libvirtd_restart | hog_stop | hook_start | undo_all | diag) "$verb" ;;
ring_ls | hog_start | idle_start | unit_state | virt_conf | virsh_list) "$verb" "$@" ;;
*) echo "unknown verb: ${verb}" >&2; exit 2 ;;
esac
SP_GUEST
    chmod 755 "$1/guest.sh"
    # a webhook of the proof's own: every request the hub's channel makes is one JSON line in hook.log
    cat >"$1/hook.py" <<'SP_HOOK'
import http.server
import json
import sys
import time

LOG = sys.argv[1]


class Hook(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(min(n, 1 << 16)).decode("utf-8", "replace")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "path": self.path, "body": body}) + "\n")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    do_PUT = do_POST

    def log_message(self, *args):
        pass


http.server.HTTPServer(("127.0.0.1", 18089), Hook).serve_forever()
SP_HOOK
    # a test driver with no domain at all
    printf '<node>\n</node>\n' >"$1/empty.xml"
    cat >"$1/stproof/compose.yml" <<'SP_COMPOSE'
services:
  lim:
    image: busybox
    command: ["sh", "-c", "while :; do :; done"]
    cpus: 0.5
    restart: unless-stopped
  free:
    image: busybox
    command: ["sleep", "100000"]
    init: true
    restart: unless-stopped
SP_COMPOSE
}

# ---- the guest: its preparation ----------------------------------------------------------------------------------------------------------

sp_guest_prepare() {
    [[ "${SP_DISPOSABLE:-}" == 1 ]] ||
        { echo "This installs ReCasaOS and a module, stops Docker, the core and libvirt and restarts the machine: run it on a disposable machine (SP_DISPOSABLE=1)." >&2; exit 2; }
    [ "$(id -u)" -eq 0 ] || { echo "Run it as root." >&2; exit 2; }
    if [ -e /etc/casaos ] || [ -e /var/lib/casaos ]; then
        echo "This machine already runs ReCasaOS: not a fresh one, not touching it." >&2
        exit 2
    fi
    # shellcheck source=os-readiness-proof.sh
    . "${READINESS_LIB:-${MP_HERE}/os-readiness-proof.sh}"
    # shellcheck source=/dev/null
    . "${OS_RELEASE}"
    export LC_ALL=C
    OUT="${OUT:-${PWD}/sp-guest-out}"
    mkdir -p "${OUT}"
    : >"${OUT}/timeline.tsv"
    echo "${PRETTY_NAME}" >"${OUT}/os"
    # shellcheck disable=SC2034
    PENDING_UNDO=""
    guard_run
    [ -n "${EPOCHREALTIME:-}" ] || abort "this bash has no EPOCHREALTIME (bash 5 is needed)"
    export DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none NEEDRESTART_SUSPEND=1
    APT=(env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3)
    marker prepare_start
    cloud-init status --wait >/dev/null 2>&1 || true
    systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service >/dev/null 2>&1 || true
    printf 'APT::Periodic::Update-Package-Lists "0";\nAPT::Periodic::Download-Upgradeable-Packages "0";\nAPT::Periodic::AutocleanInterval "0";\nAPT::Periodic::Unattended-Upgrade "0";\n' >"${APT_DIR}/apt.conf.d/99sp-quiet"
    retry "${APT[@]}" update -qq
    "${APT[@]}" install -y -q curl jq ca-certificates python3 procps iproute2 util-linux >/dev/null
    sp_write_guest_tools "${GUEST_TOOLS_DIR}"
    mp_install_casaos
    start_services_wait
    # the module as the release names it: its own catalogue, its own pinned archive, no test variable
    env -u CASAOS_MODULE_TEST -u CASAOS_MODULE_CATALOGUE -u CASAOS_MODULE_ARCHIVE_FILE /usr/bin/casaos module install virt --yes >"${OUT}/module-install.log" 2>&1 ||
        abort "the virt module could not be installed from the release's catalogue (module-install.log)"
    local i
    for i in $(seq 1 60); do [ -f /etc/casaos/virt-management.conf ] && break; sleep 1; done
    [ -f /etc/casaos/virt-management.conf ] || abort "the virt module wrote no /etc/casaos/virt-management.conf"
    command -v virsh >/dev/null 2>&1 || "${APT[@]}" install -y -q libvirt-clients >/dev/null
    retry docker pull -q busybox >/dev/null
    docker compose version >"${OUT}/compose-version" 2>&1 || abort "docker compose is not on the box: the compose case cannot be made"
    sh "${GUEST_TOOLS_DIR}/guest.sh" facts >"${OUT}/facts" 2>&1 || true
    cp /var/lib/casaos/fork-release "${OUT}/fork-release" 2>/dev/null || true
    if [ -s "${COMPONENTS_LOCK:-/home/casa/components.lock}" ]; then
        grep -E '^(CASAOS_RELEASE_TAG|CASAOS_TAG|CASAOS_APP_MANAGEMENT_TAG|CASAOS_UI_TAG)=' "${COMPONENTS_LOCK:-/home/casa/components.lock}" >"${OUT}/pins" || true
    fi
    marker prepare_done
    date -u +%FT%TZ >"${OUT}/prepared"
    log "prepared"
}

# ---- the runner: small helpers -------------------------------------------------------------------------------------------------------

hp() { python3 "${REPORT}" helper "$@"; }

# wait_since <name> <seconds>: naps until <seconds> have gone by since <name>.t1 (the runner's clock)
wait_since() {
    local t1 left
    t1="$(cut -d. -f1 "${OUT}/$1.t1" 2>/dev/null)"
    now_s
    left=$((${t1:-0} + $2 - NOW_S))
    if [ "${left}" -gt 0 ]; then nap "${left}"; fi
}

# poll <tag> <seconds> <every> <path> <predicate> [words]: a GET of path every <every> seconds until the predicate holds (status 0) or the time is out (status 1). Each answer is a row of
# <tag>.tsv: t0, t1, HTTP, yes|no, what the helper saw; the last answer is <tag>-now.*
poll() {
    local tag="$1" max="$2" every="$3" path="$4" end v
    shift 4
    : >"${OUT}/${tag}.tsv"
    now_s
    end=$((NOW_S + max))
    while :; do
        call "${tag}-now" GET "${path}" "" 20
        v="$(hp "$1" "${OUT}/${tag}-now.json" "${CALL_CODE}" "${@:2}" 2>/dev/null)"
        printf '%s\t%s\t%s\t%s\n' "$(cat "${OUT}/${tag}-now.t0")" "$(cat "${OUT}/${tag}-now.t1")" "${CALL_CODE}" "${v:-$'no	helper failed'}" >>"${OUT}/${tag}.tsv"
        case "${v}" in yes*) return 0 ;; esac
        now_s
        [ "${NOW_S}" -lt "${end}" ] || return 1
        nap "${every}"
    done
}

rule() { printf '{"rules":[{"key":"%s","enabled":true,"threshold":%s,"minutes":%s}]}' "$1" "$2" "$3"; }

VM_ID=""
VM_PATH=""

# ---- the cases -----------------------------------------------------------------------------------------------------------------------

case_begin() {
    marker case_begin
    gx facts facts
    gx clock-start guest_now
    gx c2-units-start units
    gx h2-a ring_ls core
}

case_v3_begin() {
    marker case_V3_start
    gx v3-virsh virsh_list empty
    gx v3-conf virt_conf empty
    [ "${GX_RC}" = 0 ] || harness_invalid V3 "the module could not be pointed at the empty test driver: $(tr '\n' ' ' <"${OUT}/v3-conf.out" | cut -c1-200)"
    gx v3-ring-a ring_ls virt_management
}

case_h1_routes() {
    marker case_H1_start
    call h1-list GET "${CORE}"
    CALL_NOAUTH=1 call h1-noauth GET "${CORE}"
    call h1-live GET "${CORE}/host?range=live"
    call h1-2d GET "${CORE}/host?range=2d"
    call h1-nosuch GET "${CORE}/nosuch"
}

case_h4() {
    marker case_H4_start
    call h4-get GET "${CORE}/host/alerts"
    call h4-bad-t0 PUT "${CORE}/host/alerts" "$(rule cpu_high 0 1)"
    call h4-bad-t101 PUT "${CORE}/host/alerts" "$(rule cpu_high 101 1)"
    call h4-bad-m0 PUT "${CORE}/host/alerts" "$(rule cpu_high 20 0)"
    call h4-bad-m121 PUT "${CORE}/host/alerts" "$(rule cpu_high 20 121)"
    call h4-put PUT "${CORE}/host/alerts" "$(rule cpu_high 20 1)"
    # the hub, before the proof gives it a channel: the category of the resources, on by default
    call h4-hub-get GET /v1/sys/alerts
    gx c1-hook hook_start
    call c1-hub-put PUT /v1/sys/alerts "$(printf '{"channels":[{"name":"stats-proof","url":"%s"}]}' "${HOOK_URL}")"
    call c1-test POST /v1/sys/alerts/test '{}' 60
    # an idle machine first: an alert already active would prove nothing about the hog
    poll h4-calm 150 10 "${CORE}/host/alerts" calm cpu_high || harness_invalid H4 "cpu_high (20 % for 1 min) is active on the idle machine: the hog would prove nothing"
    printf '%s\n' "${HOG_SECONDS}" >"${OUT}/h4-hog-seconds"
    gx h4-hog hog_start "${HOG_SECONDS}"
    [ "${GX_RC}" = 0 ] || harness_invalid H4 "the CPU hog could not be started"
    poll h4-fire $((HOG_SECONDS + 60)) 10 "${CORE}/host/alerts" active cpu_high || true
    wait_since h4-hog "${HOG_SECONDS}"
    gx h4-hog-stop hog_stop
    poll h4-resolve 240 10 "${CORE}/host/alerts" calm cpu_high || true
    # the defaults again: the busy containers of the next cases must not keep the machine's alert firing
    call h4-restore PUT "${CORE}/host/alerts" "$(rule cpu_high 90 10)"
}

case_v3_end() {
    wait_since v3-conf "${V3_WAIT}"
    call v3-list GET "${VT}"
    gx v3-ring-b ring_ls virt_management
    marker case_V3_done
    gx v1-conf virt_conf default
    [ "${GX_RC}" = 0 ] || harness_invalid V1 "the module could not be pointed at test:///default"
    gx v1-virsh virsh_list default
}

case_a_begin() {
    marker case_A_start
    gx a1-run busy_start
    [ "${GX_RC}" = 0 ] || harness_invalid A1 "the busy container did not start: $(tr '\n' ' ' <"${OUT}/a1-run.err" | cut -c1-200)"
    poll a1-seen 30 2 "${AM}" items running container:statproof || true
    gx a2-up compose_up
    [ "${GX_RC}" = 0 ] || harness_invalid A2 "the compose project did not start: $(tr '\n' ' ' <"${OUT}/a2-up.out" | cut -c1-200)"
    gx a3-run aux_start
    [ "${GX_RC}" = 0 ] || harness_invalid A3 "the containers to stop and kill did not start"
    poll a2-seen 60 3 "${AM}" items running app:stproof:lim:1 app:stproof:free:1 || true
}

case_series() {
    marker case_series_start
    wait_since a1-run 100
    call h1-1h GET "${CORE}/host?range=1h"
    gx h2-b ring_ls core
    call a1-list GET "${AM}"
    call a1-15m GET "${AM}/container%3Astatproof?range=15m"
    CALL_NOAUTH=1 call a1-noauth GET "${AM}"
    call a1-2d GET "${AM}/container%3Astatproof?range=2d"
    call a1-unknown GET "${AM}/container%3Asp-nosuch?range=15m"
    call a2-lim GET "${AM}/app%3Astproof%3Alim%3A1?range=15m"
    call a2-free GET "${AM}/app%3Astproof%3Afree%3A1?range=15m"
    gx a5-ring-a ring_ls app_management
    wait_since v1-conf 150
    call v1-list GET "${VT}"
    VM_ID="$(hp vmid "${OUT}/v1-virsh.out" "${OUT}/v1-list.json")"
    printf '%s\n' "${VM_ID}" >"${OUT}/vm-id"
    if [ "${VM_ID}" = - ]; then
        harness_invalid V1 "no VM: virsh named no domain test and the list has no vm: item"
        return 0
    fi
    VM_PATH="${VT}/vm%3A${VM_ID}"
    call v1-15m GET "${VM_PATH}?range=15m"
    call v1-2d GET "${VM_PATH}?range=2d"
    call v1-unknown GET "${VT}/vm%3A00000000-0000-0000-0000-000000000000?range=15m"
    CALL_NOAUTH=1 call v1-noauth GET "${VT}"
    marker case_V5_start
    call v5-get GET "${VM_PATH}/alerts"
    call v5-put PUT "${VM_PATH}/alerts" '{"rules":[{"key":"mem_high","enabled":true,"threshold":50,"minutes":1},{"key":"cpu_high","enabled":true,"threshold":20,"minutes":1}]}'
    call v5-get2 GET "${VM_PATH}/alerts"
    call v5-bad PUT "${VM_PATH}/alerts" "$(rule cpu_high 0 1)"
}

case_a3() {
    marker case_A3_start
    call a3-put PUT "${AM}/container%3Astatproof/alerts" "$(rule cpu_high 10 1)"
    poll a3-fire 240 10 "${AM}/container%3Astatproof/alerts" active cpu_high || true
    gx a3-stop stop_all
    poll a3-stopped 45 3 "${AM}" items stopped container:statproof container:stattrm container:statkill || true
    poll a3-resolve 60 5 "${AM}/container%3Astatproof/alerts" calm cpu_high || true
    call a3-15m GET "${AM}/container%3Astatproof?range=15m"
    gx a3-rm rm_trm
    nap 30
    call a3-list-rm GET "${AM}"
    call a3-gone GET "${AM}/container%3Astattrm?range=15m"
}

case_h3() {
    marker case_H3_start
    call h3-pre GET "${CORE}/host?range=1h"
    gx h3-stop core_stop
    nap "${CORE_DOWN}"
    gx h3-start core_start
    wait_up 240 || harness_invalid H3 "the core does not answer after its start"
    login || true
    nap 130
    call h3-post GET "${CORE}/host?range=1h"
}

case_a4() {
    marker case_A4_start
    call a4-pre GET "${AM}/app%3Astproof%3Alim%3A1?range=15m"
    gx a4-down docker_down
    poll a4-out "${DOCKER_DOWN}" 5 "${AM}" never || true
    call a4-series-down GET "${AM}/app%3Astproof%3Alim%3A1?range=15m"
    gx a4-up docker_up
    [ "${GX_RC}" = 0 ] || harness_invalid A4 "Docker did not start again"
    poll a4-back 90 3 "${AM}" items running app:stproof:lim:1 || true
    nap 140
    call a4-post GET "${AM}/app%3Astproof%3Alim%3A1?range=15m"
}

case_a5() {
    marker case_A5_start
    gx a5-ring-b ring_ls app_management
    call a5-pre GET "${AM}/app%3Astproof%3Alim%3A1?range=15m"
    gx a5-restart am_restart
    poll a5-up 90 3 "${AM}" items running app:stproof:lim:1 || true
    call a5-post GET "${AM}/app%3Astproof%3Alim%3A1?range=15m"
}

case_cost() {
    marker case_cost_start
    gx cost-idle idle_start 10
    [ "${GX_RC}" = 0 ] || harness_invalid A6 "the 10 idle containers did not all start"
    nap 30
    gx cost-a cpu
    printf '%s\n' "${COST_SECONDS}" >"${OUT}/cost-seconds"
    nap "${COST_SECONDS}"
    gx cost-b cpu
    gx v6-ring ring_ls virt_management
    gx cost-idle-stop idle_stop
}

case_v2() {
    marker case_V2_start
    [ -n "${VM_PATH}" ] || { harness_invalid V2 "no VM id (V1)"; return 0; }
    call v2-pre GET "${VM_PATH}?range=1h"
    gx v2-ring-a ring_ls virt_management
    gx c2-units-prereboot units
    gx c2-journal-prereboot journal_scan
    if reboot_cycle v2-reboot; then
        gx c2-units-postreboot units
        gx v2-btime btime
        poll v2-up 300 5 "${VT}" items - "vm:${VM_ID}" || true
        nap 70
        call v2-post GET "${VM_PATH}?range=1h"
        gx v2-ring-b ring_ls virt_management
    else
        echo "the guest did not come back, or the core did not answer" >"${OUT}/v2-reboot.failed"
        harness_invalid V2 "the guest did not come back from its restart"
    fi
}

case_v4() {
    marker case_V4_start
    [ -n "${VM_PATH}" ] || { harness_invalid V4 "no VM id (V1)"; return 0; }
    nap 30
    gx v4-restart libvirtd_restart
    poll v4-back 40 2 "${VM_PATH}?range=live" ok || true
    nap 40
    gx v4-unit unit_state casaos-virt-management.service
    call v4-live GET "${VM_PATH}?range=live"
}

case_end() {
    marker case_end
    gx c2-units-end units
    gx c2-journal-end journal_scan
    gx c1-hook-log hook_log
    call c1-hub-end GET /v1/sys/alerts
    gx clock-end guest_now
}

# ---- the runner's side of the run ------------------------------------------------------------------------------------------------

sp_init() {
    OUT="${OUT:-${PWD}/sp-out}"
    WORK="${WORK:-${PWD}/sp-work}"
    mkdir -p "${OUT}"
    init_work
}

sp_cleanup() {
    local rc=$?
    if [ -n "${OUT:-}" ] && [ -d "${OUT}" ]; then
        g undo_all >"${OUT}/undo-final.out" 2>&1 || true
        if [ "${rc}" -ne 0 ] && [ ! -s "${OUT}/aborted" ]; then
            echo "the runner script ended on exit ${rc} (its output says where)" >>"${OUT}/aborted"
        fi
    fi
    exit "${rc}"
}

sp_run() {
    local leg="${1:-}"
    case "${leg}" in d12 | u24) ;; *) echo "usage: $0 run d12|u24" >&2; exit 2 ;; esac
    set -u
    sp_init
    : >"${OUT}/timeline.tsv"
    printf '%s\n' "${leg}" >"${OUT}/leg"
    printf '%s\n' "${TAG:-}" >"${OUT}/tag"
    date -u +%FT%TZ >"${OUT}/started"
    trap sp_cleanup EXIT
    [ -n "${EPOCHREALTIME:-}" ] || abort "this bash has no EPOCHREALTIME (bash 5 is needed)"
    login || abort "the box's user could not log in: no token"
    [ -n "$(g hostname | tr -d '\r\n')" ] || abort "the guest does not answer ssh"
    marker run_start
    case_begin
    case_v3_begin
    case_h1_routes
    case_h4
    case_v3_end
    case_a_begin
    case_series
    case_a3
    case_h3
    case_a4
    case_a5
    case_cost
    case_v2
    case_v4
    case_end
    date -u +%FT%TZ >"${OUT}/finished"
    log "done"
    return 0
}

sp_prepare() {
    local pw key
    set -u
    sp_init
    : >"${OUT}/timeline.tsv"
    : "${INSTALL_SH:?INSTALL_SH names the published install.sh}"
    : "${COMPONENTS_LOCK_FILE:?COMPONENTS_LOCK_FILE names the published components.lock}"
    scp -F "${SSH_CONFIG}" "${INSTALL_SH}" "${COMPONENTS_LOCK_FILE}" "${SP_HERE}/os-readiness-proof.sh" "${SP_HERE}/module-proof.sh" "${SP_HERE}/stats-proof.sh" "${SSH_HOST}:" ||
        abort "the files could not be copied to the guest"
    SSH_LIMIT=3600 gssh "sudo env SP_DISPOSABLE=1 INSTALLER_ARGS='${INSTALLER_ARGS:-}' OUT=/home/casa/sp-guest-out bash stats-proof.sh guest-prepare" || true
    mkdir -p "${OUT}/guest"
    gssh 'sudo tar -C /home/casa/sp-guest-out -cf - .' | tar -xf - -C "${OUT}/guest" || true
    [ ! -s "${OUT}/guest/aborted" ] || abort "the guest's preparation stopped: $(tr '\n' ' ' <"${OUT}/guest/aborted")"
    [ -s "${OUT}/guest/prepared" ] || abort "the guest's preparation did not reach its end"
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

# sp_check: the guest tool is written to a temporary directory and parsed by sh
sp_check() {
    local d rc
    d="$(mktemp -d)"
    sp_write_guest_tools "${d}"
    sh -n "${d}/guest.sh"
    rc=$?
    rm -f "${d}/guest.sh" "${d}/hook.py" "${d}/empty.xml" "${d}/stproof/compose.yml"
    rmdir "${d}/stproof" "${d}"
    return "${rc}"
}

sp_main() {
    case "${1:-}" in
    guest-prepare) sp_guest_prepare ;;
    prepare) sp_prepare ;;
    run) shift; sp_run "$@" ;;
    check) sp_check ;;
    *) echo "usage: $0 guest-prepare | prepare | run d12|u24 | check" >&2; exit 2 ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then sp_main "$@"; fi
