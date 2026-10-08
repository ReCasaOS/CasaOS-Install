#!/usr/bin/env bash
# What happens to a running ReCasaOS box when Docker is upgraded.
#
# Runs on a DISPOSABLE Debian or Ubuntu machine, as a user with passwordless
# sudo, next to install.sh (the published installer). It removes Docker and
# installs it again, so it refuses to start unless the machine is a GitHub-hosted
# runner or MEASURE_DISPOSABLE=1 says so, and unless it looks untouched (no
# ReCasaOS, no container).
#
#   docker-upgrade-measure.sh generic    the dashboard's "System packages" update, run the way the core runs it
#   docker-upgrade-measure.sh targeted   only Docker's own packages, --only-upgrade
#   docker-upgrade-measure.sh major      the Docker of the machine's image, upgraded in place to the current one
#
# For generic and targeted the box starts on the previous version of Docker in the
# same major as the newest one, with the newest on offer (containerd.io one version
# behind as well, from a clean /var/lib/docker). ReCasaOS is installed, containers
# of every restart policy are started (all with --init, so that the grace period of
# a daemon stop is not decided by a process that ignores SIGTERM) beside a database
# that writes self-identifying records and acknowledges each after a sync, a
# published port, one on the host network, and two apps made through the dashboard's
# API. Then the upgrade runs while a poller writes, about once a second, what the
# daemon, AppManagement (on a route that needs Docker and one that does not), the
# published port and the running containers are doing. Then the previous version of
# docker-ce and docker-ce-cli is put back, the way back an owner would be given:
# containerd.io, buildx and compose stay at the new version.
#
# Nothing here judges: it writes timeline.tsv, the snapshots and the logs into $OUT,
# and docker-upgrade-report.py turns them into numbers, or says the run is invalid.
set -euo pipefail

[[ "${RUNNER_ENVIRONMENT:-}" == github-hosted || "${MEASURE_DISPOSABLE:-}" == 1 ]] ||
    { echo "This removes and reinstalls Docker: run it on a disposable machine (MEASURE_DISPOSABLE=1)." >&2; exit 2; }
if [ -e /etc/casaos ] || [ -e /var/lib/casaos ] || [ -n "$(sudo docker ps -aq 2>/dev/null || true)" ]; then
    echo "This machine already runs ReCasaOS or containers: not a fresh one, not touching it." >&2
    exit 2
fi

MODE="${1:-}"
case "${MODE}" in
generic | targeted | major) ;;
*) echo "usage: $0 generic|targeted|major" >&2; exit 2 ;;
esac

OUT="${OUT:-${PWD}/measure-${MODE}}"
mkdir -p "${OUT}"
: >"${OUT}/timeline.tsv"
SETTLE="${SETTLE:-150}"

now() { date -u +%s.%N; }
log() { echo "[measure $(date -u +%H:%M:%S)] $*" | tee -a "${OUT}/steps.log"; }
marker() { printf '#\t%s\t%s\n' "$(now)" "$1" >>"${OUT}/timeline.tsv"; }
# a run that cannot be measured says why and ends without failing the job: the report prints it
not_measured() { echo "$1" >"${OUT}/skipped"; log "NOT MEASURED: $1"; exit 0; }
retry() { local i; for i in 1 2 3; do "$@" && return 0; sleep 10; done; return 1; }

# the machine is made quiet, and apt waits for its lock instead of failing
export DEBIAN_FRONTEND=noninteractive
APT=(sudo env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3)
cloud-init status --wait >/dev/null 2>&1 || true
# apt's own timers would run in the middle of the measurement
sudo systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service >/dev/null 2>&1 || true
"${APT[@]}" update -qq
"${APT[@]}" install -y -q curl jq ca-certificates >/dev/null

# ---- the Docker this box starts on -------------------------------------------------

purge_docker() {
    sudo systemctl stop docker.socket docker.service containerd.service 2>/dev/null || true
    local pkgs
    pkgs="$(dpkg -l | awk '$1 == "ii" && $2 ~ /^(docker|moby|containerd|runc)/ {printf "%s ", $2}')"
    if [ -n "${pkgs}" ]; then
        # shellcheck disable=SC2086
        "${APT[@]}" purge -y -q ${pkgs}
    fi
    sudo rm -rf /var/lib/docker /var/lib/containerd /etc/docker
    hash -r
}

# major_of <apt version>: "5:29.8.2-1~ubuntu.22.04~jammy" -> 29
major_of() { sed -E 's/^[0-9]+://; s/\..*//' <<<"$1"; }
# series_of <apt version>: "1.7.29-1" -> 1.7
series_of() { sed -E 's/^[0-9]+://; s/^([0-9]+\.[0-9]+).*/\1/' <<<"$1"; }
# upstream_of <apt version>: "5:29.8.2-1~ubuntu.22.04~jammy" -> 29.8.2
upstream_of() { sed -E 's/^[0-9]+://; s/-.*//' <<<"$1"; }

prepare_docker() {
    if [ "${MODE}" = major ]; then
        log "the Docker this machine comes with: $(sudo docker version --format '{{.Server.Version}}')"
        return
    fi
    purge_docker
    retry curl -fsSL https://get.docker.com -o get-docker.sh
    sudo env DEBIAN_FRONTEND=noninteractive sh get-docker.sh >"${OUT}/get-docker-first.log" 2>&1
    local versions=() newest prev="" v cur cprev="" cversions=() pkgs
    mapfile -t versions < <(apt-cache madison docker-ce | awk '!seen[$3]++ {print $3}')
    [ "${#versions[@]}" -ge 2 ] || not_measured "apt offers fewer than two docker-ce versions"
    newest="${versions[0]}"
    for v in "${versions[@]:1}"; do
        if [ "$(major_of "${v}")" = "$(major_of "${newest}")" ]; then prev="${v}"; break; fi
    done
    [ -n "${prev}" ] && [ "${prev}" != "${newest}" ] ||
        not_measured "no docker-ce older than ${newest} in the same major (a new major's first release?)"
    echo "${newest}" >"${OUT}/newest"
    echo "${prev}" >"${OUT}/prev"

    # containerd.io lags too on a real box that has not been upgraded for a while: the
    # newest version below the installed one in the same series, if there is one
    cur="$(dpkg-query -W -f='${Version}' containerd.io)"
    mapfile -t cversions < <(apt-cache madison containerd.io | awk '!seen[$3]++ {print $3}')
    for v in "${cversions[@]}"; do
        if dpkg --compare-versions "${v}" lt "${cur}" && [ "$(series_of "${v}")" = "$(series_of "${cur}")" ]; then cprev="${v}"; break; fi
    done

    sudo systemctl stop docker.socket docker.service containerd.service
    pkgs=("docker-ce=${prev}" "docker-ce-cli=${prev}")
    if dpkg -s docker-ce-rootless-extras >/dev/null 2>&1; then pkgs+=("docker-ce-rootless-extras=${prev}"); fi
    if [ -n "${cprev}" ]; then pkgs+=("containerd.io=${cprev}"); fi
    "${APT[@]}" install -y -q --allow-downgrades "${pkgs[@]}"
    # nothing is stored yet, so the older daemon starts from nothing it could not read
    sudo systemctl stop docker.socket docker.service containerd.service
    sudo rm -rf /var/lib/docker /var/lib/containerd
    sudo systemctl start docker
    wait_docker || { log "the older docker does not start"; exit 1; }
    [ "$(sudo docker version --format '{{.Server.Version}}')" = "$(upstream_of "${prev}")" ] ||
        { log "docker is not the version it was put back to"; exit 1; }
    log "starting on docker-ce ${prev}, ${newest} on offer, containerd.io ${cprev:-left as it is}"
}

# ---- ReCasaOS, a first user, and the things that will have to come back ------------

wait_docker() {
    local end=$((SECONDS + 300))
    while [ "${SECONDS}" -lt "${end}" ]; do
        timeout 3 sudo docker info >/dev/null 2>&1 && return 0
        sleep 1
    done
    log "docker did not answer within five minutes"
    return 1
}

api() { curl -fsS --max-time 20 -H @"${AUTH_FILE}" "$@"; }

install_casaos() {
    sudo -E bash install.sh --no-telemetry 2>&1 | tee "${OUT}/install.log" >/dev/null
    test "${PIPESTATUS[0]}" -eq 0
    local port key pw
    port="$(sudo sed -nE 's/^[[:space:]]*HttpPort[[:space:]]*=[[:space:]]*([0-9]+).*/\1/p' /etc/casaos/gateway.ini | sed -n 1p)"
    CASA_URL="http://127.0.0.1:${port:-80}"
    for _ in $(seq 1 30); do
        [ "$(curl -s -o /dev/null -w '%{http_code}' "${CASA_URL}/" || true)" = 200 ] && break
        sleep 2
    done
    # a password of this run's own, never written anywhere
    pw="Aa1-$(head -c12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    key="$(curl -fsS "${CASA_URL}/v1/users/status" | jq -r '.data.key')"
    curl -fsS -X POST "${CASA_URL}/v1/users/register" -H 'content-type: application/json' \
        -d "{\"username\":\"measure\",\"password\":\"${pw}\",\"key\":\"${key}\"}" >/dev/null
    TOKEN="$(curl -fsS -X POST "${CASA_URL}/v1/users/login" -H 'content-type: application/json' \
        -d "{\"username\":\"measure\",\"password\":\"${pw}\"}" | jq -r '.data.token.access_token')"
    test -n "${TOKEN}" && test "${TOKEN}" != null
    # the token goes to curl through a file, not through every process list for the next hour
    AUTH_FILE="$(mktemp)"
    chmod 600 "${AUTH_FILE}"
    echo "Authorization: ${TOKEN}" >"${AUTH_FILE}"
}

start_things() {
    local d="sudo docker" policy
    retry ${d} pull -q busybox >/dev/null
    retry ${d} pull -q nginx:alpine >/dev/null
    for policy in always unless-stopped no on-failure:3; do
        ${d} run -d --init --name "m-${policy%%:*}" --restart "${policy}" busybox sleep 100000 >/dev/null
    done
    ${d} volume create mdb >/dev/null
    # A database that writes a record of its own identity, makes it durable (sync),
    # and only then says "ack <id>". After any restart the file must hold every id that
    # was acknowledged. It detects a volume that is lost or rolled back, not a power cut.
    ${d} run -d --init --name m-db --restart unless-stopped -v mdb:/data busybox sh -c \
        'trap "exit 0" TERM; i=0; while :; do i=$((i+1)); id="$(date +%s)-$i"; echo "$id" >> /data/log; sync; echo "ack $id"; sleep 0.1 & wait $!; done' >/dev/null
    ${d} run -d --init --name m-web --restart unless-stopped -p 18081:80 nginx:alpine >/dev/null
    ${d} run -d --init --name m-host --restart unless-stopped --network host busybox httpd -f -p 18082 -h /tmp >/dev/null
    # two apps the way the dashboard makes them: one with no restart policy of its own,
    # one with the policy the store's compose files carry
    cat >smoke.yml <<'YAML'
name: smoke
services:
  web:
    image: nginx:alpine
    container_name: smoke
x-casaos:
  main: web
  title:
    en_us: Smoke
YAML
    cat >smoke2.yml <<'YAML'
name: smoke2
services:
  web:
    image: nginx:alpine
    container_name: smoke2
    restart: unless-stopped
x-casaos:
  main: web
  title:
    en_us: Smoke two
YAML
    local f
    for f in smoke.yml smoke2.yml; do
        api -X POST "${CASA_URL}/v2/app_management/compose" -H 'content-type: application/yaml' --data-binary @"${f}" >/dev/null
    done
    for _ in $(seq 1 60); do
        [ "$(sudo docker ps --format '{{.Names}}' | grep -cxE 'smoke|smoke2')" -eq 2 ] && break
        sleep 2
    done
    [ "$(sudo docker ps --format '{{.Names}}' | grep -cxE 'smoke|smoke2')" -eq 2 ] || { log "the dashboard's apps did not start"; exit 1; }
    sleep 10
}

snapshot() { # <name>
    local f="${OUT}/snapshot-$1.txt" u
    {
        echo "# $(date -u +%FT%TZ)"
        echo "docker $(timeout 10 sudo docker version --format '{{.Server.Version}}' 2>&1 || echo unavailable)"
        echo "driver $(timeout 10 sudo docker info --format '{{.Driver}}' 2>&1 || echo unavailable)"
        echo "live_restore $(timeout 10 sudo docker info --format '{{.LiveRestoreEnabled}}' 2>&1 || echo unavailable)"
        echo "images $(timeout 10 sudo docker images -q 2>/dev/null | wc -l)"
        echo "all_containers $(timeout 10 sudo docker ps -aq 2>/dev/null | wc -l)"
        echo "packages $(dpkg -l 'docker*' 'containerd*' 2>/dev/null | awk '$1 == "ii" {printf "%s=%s ", $2, $3}')"
        echo "needrestart $(dpkg -s needrestart >/dev/null 2>&1 && echo installed || echo absent)"
        for u in docker containerd casaos-app-management casaos-message-bus casaos; do
            echo "unit ${u} $(systemctl show "${u}.service" -p MainPID -p ActiveEnterTimestamp -p NRestarts | tr '\n' ' ')"
        done
        echo "containers"
        # shellcheck disable=SC2046
        timeout 20 sudo docker inspect --format '{{.Name}} policy={{.HostConfig.RestartPolicy.Name}} state={{.State.Status}} started={{.State.StartedAt}} restarts={{.RestartCount}}' $(timeout 10 sudo docker ps -aq) 2>&1 || true
    } >"${f}" 2>&1 || true
}

# ---- the poller: about once a second, the probes in parallel -------------------------

poll() {
    local dir t next row
    dir="$(mktemp -d)"
    while :; do
        t="$(now)"
        (timeout 2 systemctl is-active docker >"${dir}/unit" 2>/dev/null || true) &
        (if n="$(timeout 2 sudo docker ps --format '{{.Names}}' 2>/dev/null | sort | paste -sd, -)"; then printf 'up\t%s' "${n:--}"; else printf 'down\t-'; fi >"${dir}/docker") &
        (curl -s -o /dev/null --max-time 2 -w '%{http_code}' -H @"${AUTH_FILE}" "${CASA_URL}/v2/app_management/categories" >"${dir}/amfree" 2>/dev/null || true) &
        (curl -s -o /dev/null --max-time 2 -w '%{http_code}' -H @"${AUTH_FILE}" "${CASA_URL}/v2/app_management/compose" >"${dir}/amdocker" 2>/dev/null || true) &
        (curl -s -o /dev/null --max-time 1 -w '%{http_code}' http://127.0.0.1:18081/ >"${dir}/web" 2>/dev/null || true) &
        wait
        # unit, docker up|down, names, AppManagement without and with Docker, the port
        row="$(printf '%s\t%s\t%s\t%s\t%s\t%s' "${t}" "$(cat "${dir}/unit")" "$(cut -f1 "${dir}/docker")" "$(cat "${dir}/amfree")" "$(cat "${dir}/amdocker")" "$(cat "${dir}/web")")"
        printf '%s\t%s\n' "${row}" "$(cut -f2 "${dir}/docker")" >>"${OUT}/timeline.tsv"
        next="$(awk -v s="${t}" -v n="$(now)" 'BEGIN {d = 1 - (n - s); if (d < 0) d = 0; printf "%.2f", d}')"
        sleep "${next}"
    done
}

# ---- the run ----------------------------------------------------------------------

prepare_docker
install_casaos
start_things
snapshot before

poll &
POLL_PID=$!
trap 'kill "${POLL_PID}" 2>/dev/null || true' EXIT
sleep 5

case "${MODE}" in
generic)
    "${APT[@]}" update -qq
    "${APT[@]}" -s --no-remove -o Dpkg::Use-Pty=0 -o Dpkg::Options::=--force-confold upgrade >"${OUT}/simulation.txt" 2>&1 || true
    # the way the core runs it: a transient unit, DEBIAN_FRONTEND=noninteractive, no terminal
    UPGRADE=(sudo systemd-run --quiet --wait --pipe --collect --property=Type=exec --setenv=DEBIAN_FRONTEND=noninteractive
        /bin/bash -o pipefail -c '/usr/bin/apt-get -y --no-remove -o Dpkg::Use-Pty=0 -o Dpkg::Options::=--force-confold upgrade')
    ;;
targeted)
    "${APT[@]}" update -qq
    UPGRADE=("${APT[@]}" install -y -q --only-upgrade docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)
    ;;
major)
    retry curl -fsSL https://get.docker.com -o get-docker.sh
    # the script waits twenty seconds when Docker is already there, which is not the upgrade
    sed -i 's/sleep 20/sleep 0/' get-docker.sh
    UPGRADE=(sudo env DEBIAN_FRONTEND=noninteractive sh get-docker.sh)
    ;;
esac

marker upgrade_start
set +e
"${UPGRADE[@]}" >"${OUT}/upgrade.log" 2>&1
echo "$?" >"${OUT}/upgrade.rc"
set -e
marker upgrade_end
wait_docker || true
sleep "${SETTLE}"
marker upgrade_settled
snapshot after

timeout 20 sudo docker logs --timestamps m-db >"${OUT}/db-logs.txt" 2>&1 || true
timeout 30 sudo docker run --rm -v mdb:/data busybox cat /data/log >"${OUT}/db-file.txt" 2>"${OUT}/db-file.err" || true
start_epoch="$(awk -F'\t' '$1 == "#" && $3 == "upgrade_start" {printf "%d", $2}' "${OUT}/timeline.tsv")"
sudo journalctl --no-pager --since "@${start_epoch:-0}" -u casaos-message-bus -u casaos -u casaos-app-management 2>/dev/null |
    grep -iE 'container-(died|exit|oom|stop|unhealth)|app:container|alerts?[: ]' >"${OUT}/alerts.txt" || true
sudo journalctl --no-pager --since "@${start_epoch:-0}" 2>/dev/null | grep -i needrestart >"${OUT}/needrestart.txt" || true

if [ "${MODE}" != major ]; then
    # the way back an owner would be given: the engine and its client only
    marker rollback_start
    prev="$(cat "${OUT}/prev")"
    back=("docker-ce=${prev}" "docker-ce-cli=${prev}")
    if dpkg -s docker-ce-rootless-extras >/dev/null 2>&1; then back+=("docker-ce-rootless-extras=${prev}"); fi
    set +e
    "${APT[@]}" install -y -q --allow-downgrades "${back[@]}" >"${OUT}/rollback.log" 2>&1
    echo "$?" >"${OUT}/rollback.rc"
    set -e
    marker rollback_end
    wait_docker || true
    sleep "${SETTLE}"
    marker rollback_settled
    snapshot rollback
fi

kill "${POLL_PID}" 2>/dev/null || true
trap - EXIT
log "done: upgrade exit status $(cat "${OUT}/upgrade.rc")"
