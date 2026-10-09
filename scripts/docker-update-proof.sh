#!/usr/bin/env bash
# Proof that the dashboard's "Update Docker" button does what it says, on a real machine.
#
# Runs as root inside a DISPOSABLE Debian or Ubuntu machine (a QEMU guest in CI), next to
# install.sh (the published installer). It installs Docker at a version that has a newer
# one on offer, installs ReCasaOS, starts containers of every restart policy beside a
# database that acknowledges each write after a sync and a container that publishes a
# port, and then asks the core for the update the way the dashboard does: through the
# gateway, with the loopback secret (Authorization: Internal <secret>) the services use
# among themselves, and the dashboard's own token where the route has to take that too.
#
#   docker-update-proof.sh major   Docker pinned to DOCKER_FROM (28.0.4) from Docker's repo, containerd.io
#                                  on the 1.7 line when the repo has one, a 29 on offer: the owner's box. Docker 29
#                                  needs nftables and Docker 28 did not, and Docker's installer brings nftables in
#                                  (iptables recommends it): when apt can take it out and nothing but libraries with it,
#                                  the leg does, before the first check, so that the update has a package to bring that
#                                  the box did not have, as on the owner's box. What it did, or why not, is in
#                                  $OUT/dependency-path. After the success the rollback command the core prints after a
#                                  failure is run for real (rollback_run), and what is left of Docker 28.0.4, the containers,
#                                  the volume and the images is recorded.
#   docker-update-proof.sh minor   the previous patch of the current Docker minor (found with apt-cache
#                                  madison, never written down), the current one on offer; and last, once
#                                  the box is put back there, a second update with dockerd made unable to
#                                  start: the run has to fail with `daemon`, say how to go back, and the
#                                  status has to keep answering. With PROOF_KILL_INSTALL=1 (one minor
#                                  leg) the unit is first killed with SIGKILL in the middle of the install,
#                                  as soon as dpkg runs a maintainer script of Docker's (kill_run): the status
#                                  has to find out by itself, and the repair the dashboard names
#                                  (dpkg --configure -a, then apt-get -f install) is run.
#
# In both: the refusals first (a hold, dpkg's lock, a running package update, a plan that is not
# the plan, a body that is not a plan_id, a token in the query, a refresh token), none of which
# changes anything of the engine; then the update, polled to its end; then what is left of the
# box: versions, containers, the witness, AppManagement.
#
# Nothing here judges: it writes what it saw into $OUT and docker-update-proof-report.py turns
# that into verdicts, or says the run is invalid. It stops early (and says why, in $OUT/aborted)
# only when it cannot go on: no plan to confirm, a start version apt does not have. What dpkg had
# installed just before the update was asked for and just after it ended goes to dpkg-before.tsv
# and dpkg-after.tsv: the report sets them against the plan.
#
# Needs: PROOF_DISPOSABLE=1, EXPECT_ID and EXPECT_VERSION_ID (the system it must be, as
# /etc/os-release names it), DOCKER_FROM for the major leg, INSTALLER_ARGS (options for install.sh,
# unquoted on purpose: a list), PROOF_KILL_INSTALL (1 on the minor leg that kills the unit, else empty),
# OUT (default ./proof-out).

# ---- the parts that are only text, and can be tested without a machine -----------------------

# major_of <apt version>: "5:29.8.2-1~ubuntu.24.04~noble" -> 29
major_of() { sed -E 's/^[0-9]+://; s/\..*//' <<<"$1"; }
# upstream_of <apt version>: "5:29.8.2-1~ubuntu.24.04~noble" -> 29.8.2 (what the core calls EngineVersion)
upstream_of() { sed -E 's/^[0-9]+://; s/-.*//' <<<"$1"; }
# minor_of <apt version>: "5:29.8.2-1~..." -> 29.8
minor_of() { sed -E 's/^[0-9]+://; s/^([0-9]+\.[0-9]+).*/\1/' <<<"$1"; }

# pick_previous <newest>: reads the docker-ce versions older than <newest>, newest first (as
# apt-cache madison lists them), and prints the one to start from: the previous patch of the
# newest one's minor (29.8.2 -> 29.8.1), failing that the newest older version of the same major.
# Status 1 and nothing when there is none: the leg cannot be proven on this repository today.
pick_previous() {
    local newest="$1" nu nm v u same_major=""
    nu="$(upstream_of "${newest}")"
    nm="$(minor_of "${newest}")"
    while IFS= read -r v; do
        u="$(upstream_of "${v}")"
        # the same release packaged again is not an older one
        if [ "${u}" = "${nu}" ]; then continue; fi
        if [ "$(minor_of "${v}")" = "${nm}" ]; then
            echo "${v}"
            return 0
        fi
        if [ -z "${same_major}" ] && [ "$(major_of "${v}")" = "$(major_of "${newest}")" ]; then same_major="${v}"; fi
    done
    if [ -z "${same_major}" ]; then return 1; fi
    echo "${same_major}"
}

# removal_ok: reads what `apt-get -s remove nftables` prints and says, on stdout, what apt would remove. Status 0
# only when nftables is one of it and the rest are libraries: a box where something else needs nftables is not
# ours to take it from, and an Inst line would mean that the removal installs something too.
removal_ok() {
    awk '
        $1 == "Inst" { bad = 1 }
        $1 == "Remv" || $1 == "Purg" {
            names = names " " $2
            if ($2 ~ /^nftables(:[a-z0-9-]+)?$/) { found = 1 } else if ($2 !~ /^lib[a-z0-9+.-]*(:[a-z0-9-]+)?$/) { bad = 1 }
        }
        END { sub(/^ /, "", names); print names; if (found && !bad) exit 0; exit 1 }'
}

# previous_pins <log>: the pins (name=version, one per line) of the PREVIOUS marker of the run a log records, which is what the core
# makes its rollback command of. The nonce is the one on the log's first line (the core's), as the core reads it: a marker with
# another nonce is somebody else's text, and only the first marker counts. A word that has not the shape of a pin is dropped.
# Status 1 and nothing when there is no pin.
previous_pins() {
    awk '
        NR == 1 { if ($1 == "CASAOS_DOCKER_UPDATE_QUEUED" && length($2) == 32 && $2 ~ /^[0-9a-f]+$/) nonce = $2; next }
        nonce != "" && $1 == "CASAOS_DOCKER_UPDATE_PREVIOUS" && $2 == nonce && !seen {
            seen = 1
            for (i = 3; i <= NF; i++) if ($i ~ /^[a-z0-9][a-z0-9+.-]*=([0-9]+:)?[0-9][A-Za-z0-9.+~-]*$/) { print $i; n++ }
        }
        END { exit !(n > 0) }' "$1"
}

# ---- the machine --------------------------------------------------------------------------------

COME_BACK=(p-always p-unless-stopped p-db p-web p-host)
# a maintainer script of one of Docker's packages: while it runs, dpkg is in the middle of a package
MAINTAINER_RE='/var/lib/dpkg/info/(docker-ce|docker-ce-cli|containerd\.io|docker-ce-rootless-extras)\.(preinst|prerm|postinst|postrm)'

now() { date -u +%s.%N; }
log() { echo "[proof $(date -u +%H:%M:%S)] $*" | tee -a "${OUT}/steps.log"; }
marker() { printf '#\t%s\t%s\n' "$(now)" "$1" >>"${OUT}/timeline.tsv"; }
retry() { local i; for i in 1 2 3; do "$@" && return 0; sleep 10; done; return 1; }
# a run that cannot go on says why and ends; the report turns that into INVALID RUN
abort() { echo "$*" >>"${OUT}/aborted"; log "ABORTED: $*"; exit 0; }
not_proven() { abort "NOT PROVEN: $*"; }

wait_docker() { # [seconds]
    local end=$((SECONDS + ${1:-300}))
    while [ "${SECONDS}" -lt "${end}" ]; do
        if timeout -s KILL 5 docker info >/dev/null 2>&1; then return 0; fi
        sleep 1
    done
    log "docker did not answer within ${1:-300} s"
    return 1
}

cleanup() {
    local rc=$?
    if [ -n "${POLL_PID:-}" ]; then kill "${POLL_PID}" 2>/dev/null || true; fi
    if [ -n "${LOCK_PID:-}" ]; then kill "${LOCK_PID}" 2>/dev/null || true; fi
    apt-mark unhold docker-ce >/dev/null 2>&1 || true
    systemctl stop casaos-package-update.service >/dev/null 2>&1 || true
    if [ "${DAEMON_JSON_BROKEN:-0}" = 1 ]; then mend_daemon_json || true; fi
    exit "${rc}"
}

# ---- Docker, ReCasaOS, the things that have to come back --------------------------------------

install_docker() {
    local versions=() newest prev pkgs cur cprev
    retry curl -fsSL https://get.docker.com -o get-docker.sh
    if [ "${LEG}" = major ]; then
        retry sh get-docker.sh --version "${DOCKER_FROM}" >>"${OUT}/get-docker.log" 2>&1
        START_VERSION="${DOCKER_FROM}"
        # a box on 28.0.4 has the containerd.io of that day, which is on the 1.7 line: the
        # update then moves containerd.io across a major too, as it will on the owner's box
        cur="$(dpkg-query -W -f='${Version}' containerd.io)"
        cprev="$(apt-cache madison containerd.io | awk '$3 ~ /^1\.7\./ {print $3; exit}')"
        if [ -n "${cprev}" ] && [ "$(major_of "${cur}")" -ge 2 ]; then
            systemctl stop docker.socket docker.service containerd.service
            "${APT[@]}" install -y -q --allow-downgrades "containerd.io=${cprev}"
            systemctl stop docker.socket docker.service containerd.service
            rm -rf /var/lib/docker /var/lib/containerd
            systemctl start docker
        fi
    else
        retry sh get-docker.sh >>"${OUT}/get-docker.log" 2>&1
        mapfile -t versions < <(apt-cache madison docker-ce | awk '!seen[$3]++ {print $3}')
        newest="${versions[0]:-}"
        [ -n "${newest}" ] || not_proven "apt-cache madison lists no docker-ce"
        # a process substitution, not a pipe: pick_previous stops reading at the first match, and under
        # pipefail a printf killed by SIGPIPE would fail the lot
        prev="$(pick_previous "${newest}" < <(printf '%s\n' "${versions[@]:1}"))" ||
            not_proven "docker-ce ${newest} has no older release of the same major to start from"
        pkgs=("docker-ce=${prev}" "docker-ce-cli=${prev}")
        if dpkg -s docker-ce-rootless-extras >/dev/null 2>&1; then pkgs+=("docker-ce-rootless-extras=${prev}"); fi
        systemctl stop docker.socket docker.service containerd.service
        "${APT[@]}" install -y -q --allow-downgrades "${pkgs[@]}"
        # nothing is stored yet, so the older daemon starts from nothing it could not read
        systemctl stop docker.socket docker.service containerd.service
        rm -rf /var/lib/docker /var/lib/containerd
        systemctl start docker
        START_VERSION="$(upstream_of "${prev}")"
    fi
    wait_docker || abort "the Docker this leg starts on does not start"
    [ "$(docker version --format '{{.Server.Version}}')" = "${START_VERSION}" ] ||
        abort "docker is not the version it was put on (${START_VERSION})"
    log "starting on docker $(docker version --format '{{.Server.Version}}'), $(dpkg-query -W -f='${Package} ${Version}' containerd.io)"
}

install_casaos() {
    # shellcheck disable=SC2086
    bash install.sh --no-telemetry ${INSTALLER_ARGS:-} 2>&1 | tee "${OUT}/install.log" >/dev/null
    test "${PIPESTATUS[0]}" -eq 0
    [ "$(docker version --format '{{.Server.Version}}')" = "${START_VERSION}" ] ||
        abort "the installer moved Docker off ${START_VERSION}"
    local port key pw
    port="$(sed -nE 's/^[[:space:]]*HttpPort[[:space:]]*=[[:space:]]*([0-9]+).*/\1/p' /etc/casaos/gateway.ini | sed -n 1p)"
    CASA_URL="http://127.0.0.1:${port:-80}"
    for _ in $(seq 1 30); do
        if [ "$(curl -s -o /dev/null -w '%{http_code}' "${CASA_URL}/" || true)" = 200 ]; then break; fi
        sleep 2
    done
    # a password of this run's own, never written anywhere
    pw="Aa1-$(head -c12 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    key="$(curl -fsS "${CASA_URL}/v1/users/status" | jq -r '.data.key')"
    curl -fsS -X POST "${CASA_URL}/v1/users/register" -H 'content-type: application/json' \
        -d "{\"username\":\"proof\",\"password\":\"${pw}\",\"key\":\"${key}\"}" >/dev/null
    curl -fsS -X POST "${CASA_URL}/v1/users/login" -H 'content-type: application/json' \
        -d "{\"username\":\"proof\",\"password\":\"${pw}\"}" >"${AUTH_DIR}/login.json"
    ACCESS="$(jq -r '.data.token.access_token' "${AUTH_DIR}/login.json")"
    REFRESH="$(jq -r '.data.token.refresh_token // empty' "${AUTH_DIR}/login.json")"
    test -n "${ACCESS}" && test "${ACCESS}" != null
    [ -n "${REFRESH}" ] || abort "the login returned no refresh token: the refusal of a refresh token cannot be checked"
    # the tokens reach curl through files in a directory of root's, never through the output directory
    (umask 077; printf 'Authorization: %s\n' "${ACCESS}" >"${AUTH_DIR}/jwt"; printf 'Authorization: %s\n' "${REFRESH}" >"${AUTH_DIR}/refresh")
    write_internal_header
    log "ReCasaOS is up at ${CASA_URL}"
}

# Docker 29 needs nftables and Docker 28 did not, so a box that is updated from 28 has packages to be brought that it
# did not have. Docker's installer puts nftables on the box (iptables recommends it): on the major leg it is taken
# out here, after Docker and ReCasaOS are installed and before the first check reads the plan, when apt says that it
# takes nothing but libraries with it (first with the libraries nothing else needs, then nftables alone). Docker is
# restarted after: nftables.service flushes the whole ruleset when it stops, Docker's rules included, and dockerd
# makes them again when it starts. What was done, or why not, goes to $OUT/dependency-path, for the report to say
# whether the update's way of bringing a new package was tried. The minor leg leaves nftables alone: Docker 29
# depends on it, and removing it would remove Docker.
prepare_dependency_path() {
    local rec="${OUT}/dependency-path" sim="" names="" seen="" how flags
    if [ "${LEG}" != major ]; then
        printf 'action not-attempted\nreason only the major leg takes nftables out: Docker 29 already needs it\n' >"${rec}"
        return 0
    fi
    if ! installed nftables; then
        printf 'action skipped\nreason nftables is not installed\n' >"${rec}"
        return 0
    fi
    for how in autoremove plain; do
        flags=()
        if [ "${how}" = autoremove ]; then flags=(--autoremove); fi
        if sim="$("${APT[@]}" -s "${flags[@]}" remove nftables 2>&1)" && names="$(removal_ok <<<"${sim}")"; then
            "${APT[@]}" -y -q "${flags[@]}" remove nftables >"${OUT}/dependency-path.log" 2>&1 || abort "could not take nftables out to try the dependency path"
            ! installed nftables || abort "nftables is still installed after apt removed it"
            systemctl restart docker || abort "docker could not be restarted after nftables was taken out"
            wait_docker || abort "docker did not start again after nftables was taken out"
            printf 'action removed\npackages %s\n' "${names}" >"${rec}"
            return 0
        fi
        seen="$(removal_ok <<<"${sim}" || true)"
    done
    printf 'action skipped\nreason taking nftables out would take more than libraries with it: %s\n' "${seen:-apt listed no removal}" >"${rec}"
}

start_things() {
    local policy
    retry docker pull -q busybox >/dev/null
    retry docker pull -q nginx:alpine >/dev/null
    for policy in always unless-stopped no; do
        docker run -d --init --name "p-${policy}" --restart "${policy}" busybox sleep 100000 >/dev/null
    done
    docker volume create pdb >/dev/null
    # A database that writes a record of its own identity, makes it durable (sync), and only
    # then says "ack <id>". After the update the file must hold every id that was acknowledged.
    # It detects a volume that is lost or rolled back, not a power cut.
    docker run -d --init --name p-db --restart unless-stopped -v pdb:/data busybox sh -c \
        'trap "exit 0" TERM; i=0; while :; do i=$((i+1)); id="$(date +%s)-$i"; echo "$id" >> /data/log; sync; echo "ack $id"; sleep 0.1 & wait $!; done' >/dev/null
    docker run -d --init --name p-web --restart unless-stopped -p 18081:80 nginx:alpine >/dev/null
    docker run -d --init --name p-host --restart unless-stopped --network host busybox httpd -f -p 18082 -h /tmp >/dev/null
    sleep 5
}

# dpkg_dump <name>: every package dpkg knows, with its version and its state (`ii` is installed, `rc` removed with
# its configuration kept), for the report to set before and after against the plan
dpkg_dump() { dpkg-query -W -f='${Package}\t${Version}\t${db:Status-Abbrev}\n' >"${OUT}/dpkg-$1.tsv" || abort "dpkg-query could not list the packages"; }

# images_dump <name>: the images the daemon has (id and name:tag, sorted), for the rollback to be set against
images_dump() { timeout -s KILL 30 docker image ls --no-trunc --format '{{.ID}} {{.Repository}}:{{.Tag}}' 2>"${OUT}/images-$1.err" | sort >"${OUT}/images-$1.txt" || true; }

installed() { # <package>
    local st
    st="$(dpkg-query -W -f='${db:Status-Abbrev}' "$1" 2>/dev/null || true)"
    [[ "${st}" == ?i* ]]
}

snapshot() { # <name>
    local f="${OUT}/snapshot-$1.txt" u ts ep
    {
        echo "# $(date -u +%FT%TZ)"
        echo "docker $(timeout -s KILL 10 docker version --format '{{.Server.Version}}' 2>&1 || echo unavailable)"
        echo "driver $(timeout -s KILL 10 docker info --format '{{.Driver}}' 2>&1 || echo unavailable)"
        echo "packages $(dpkg -l 'docker*' 'containerd*' 2>/dev/null | awk '$1 == "ii" {printf "%s=%s ", $2, $3}')"
        for u in docker containerd casaos-app-management casaos-message-bus casaos casaos-gateway; do
            ts="$(systemctl show "${u}.service" -p ActiveEnterTimestamp --value 2>/dev/null || true)"
            ep=0
            if [ -n "${ts}" ]; then ep="$(date -d "${ts}" +%s 2>/dev/null || echo 0)"; fi
            echo "unit ${u} $(systemctl show "${u}.service" -p MainPID -p NRestarts | tr '\n' ' ')EnterEpoch=${ep}"
        done
        echo "containers"
        # shellcheck disable=SC2046
        timeout -s KILL 20 docker inspect --format '{{.Name}} policy={{.HostConfig.RestartPolicy.Name}} state={{.State.Status}} started={{.State.StartedAt}} restarts={{.RestartCount}}' $(timeout -s KILL 10 docker ps -aq) 2>&1 || true
    } >"${f}" 2>&1 || true
}

# ---- talking to the core ----------------------------------------------------------------------------

write_internal_header() { # the gateway rewrites the secret at each boot: read it again before every call
    local s
    s="$(cat /var/run/casaos/internal.secret)"
    (umask 077; printf 'Authorization: Internal %s\n' "${s}" >"${AUTH_DIR}/internal.new")
    mv -f "${AUTH_DIR}/internal.new" "${AUTH_DIR}/internal"
}

# call_as <internal|jwt|refresh|none> <name> <method> <path> [body]: writes <name>.json (the body),
# <name>.code (the HTTP status, 000 when there was none) and <name>.secs (how long it took) into $OUT
call_as() {
    local who="$1" name="$2" method="$3" path="$4" body="${5:-}" code="" secs=""
    local args=(-sS --max-time "${CALL_MAX:-330}" -o "${OUT}/${name}.json" -w '%{http_code} %{time_total}' -X "${method}")
    case "${who}" in
    internal) write_internal_header; args+=(-H @"${AUTH_DIR}/internal") ;;
    jwt) args+=(-H @"${AUTH_DIR}/jwt") ;;
    refresh) args+=(-H @"${AUTH_DIR}/refresh") ;;
    none) ;;
    *) abort "call_as: unknown credential ${who}" ;;
    esac
    if [ -n "${body}" ]; then args+=(-H 'content-type: application/json' -d "${body}"); fi
    read -r code secs < <(curl "${args[@]}" "${CASA_URL}${path}" 2>"${OUT}/${name}.err" || echo "000 0") || true
    echo "${code:-000}" >"${OUT}/${name}.code"
    echo "${secs:-0}" >"${OUT}/${name}.secs"
}

post_plan() { # <name> <plan id> [credential]
    CALL_MAX=60 call_as "${3:-internal}" "$1" POST /v1/sys/docker/update "$(jq -nc --arg p "$2" '{plan_id: $p}')"
}

plan_id_of() { jq -r '.data.docker.update.plan_id // empty' "${OUT}/$1.json" 2>/dev/null || true; }

# poll_status <name> <seconds>: the status endpoint every 2 s until a terminal state; <name>.jsonl has one line
# per answer (time, HTTP status, the status without its log), <name>-final.json the last answer in full
poll_status() {
    local end=$((SECONDS + $2)) body state
    : >"${OUT}/$1.jsonl"
    while :; do
        CALL_MAX=5 call_as internal "$1-now" GET /v1/sys/docker/update/status
        body="$(jq -c '.data | del(.log)' "${OUT}/$1-now.json" 2>/dev/null)" || body=""
        printf '%s\t%s\t%s\n' "$(now)" "$(cat "${OUT}/$1-now.code")" "${body:-null}" >>"${OUT}/$1.jsonl"
        state="$(jq -r '.data.state // empty' "${OUT}/$1-now.json" 2>/dev/null)" || state=""
        if [ "${state}" = succeeded ] || [ "${state}" = failed ]; then break; fi
        if [ "${SECONDS}" -ge "${end}" ]; then
            echo "$2 s" >"${OUT}/$1.timeout"
            break
        fi
        sleep 2
    done
    cp "${OUT}/$1-now.json" "${OUT}/$1-final.json" 2>/dev/null || true
}

# settle <file>: seconds until the containers that must come back are running, or "never" after 150 s
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
}

# ---- the poller: about once a second, the probes in parallel ---------------------------------------------

code_of() { # <header file or -> <url> <max seconds>
    local hdr=()
    if [ "$1" != - ]; then hdr=(-H @"$1"); fi
    curl -s -o /dev/null --max-time "$3" -w '%{http_code}' "${hdr[@]}" "$2" 2>/dev/null || true
}

poll_timeline() {
    local dir t next
    dir="$(mktemp -d)"
    while :; do
        t="$(now)"
        (timeout -s KILL 3 docker version --format '{{.Server.Version}}' >"${dir}/dv" 2>/dev/null || echo - >"${dir}/dv") &
        (timeout -s KILL 3 docker ps --format '{{.Names}}' 2>/dev/null | sort | paste -sd, - >"${dir}/names" || true) &
        (code_of "${AUTH_DIR}/internal" "${CASA_URL}/v1/sys/docker/update/status" 3 >"${dir}/status") &
        (code_of - http://127.0.0.1:18081/ 1 >"${dir}/web") &
        (code_of "${AUTH_DIR}/jwt" "${CASA_URL}/v2/app_management/categories" 2 >"${dir}/amfree") &
        (code_of "${AUTH_DIR}/jwt" "${CASA_URL}/v2/app_management/compose" 2 >"${dir}/amdocker") &
        wait
        # docker's version (or -), the status endpoint, the port, AppManagement without and with Docker, the containers
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "${t}" "$(head -n1 "${dir}/dv")" "$(cat "${dir}/status")" "$(cat "${dir}/web")" \
            "$(cat "${dir}/amfree")" "$(cat "${dir}/amdocker")" "$(cat "${dir}/names")" | sed -E 's/\t$/\t-/' >>"${OUT}/timeline.tsv"
        next="$(awk -v s="${t}" -v n="$(now)" 'BEGIN {d = 1 - (n - s); if (d < 0) d = 0; printf "%.2f", d}')"
        sleep "${next}"
    done
}

# ---- the refusals: nothing of the engine changes -----------------------------------------------------------

refusals() {
    local held_id wrong body
    wrong="$(printf '0%.0s' $(seq 1 64))"
    CALL_MAX=60 call_as internal status-idle GET /v1/sys/docker/update/status
    call_as internal packages-1 GET /v1/sys/packages
    PLAN_ID="$(plan_id_of packages-1)"
    [ -n "${PLAN_ID}" ] || abort "the first check has no docker.update.plan_id: the stack under test has no Docker update, or does not offer it on this box"
    CALL_MAX=60 call_as internal containers-before GET /v1/sys/docker/containers

    # a hold is the owner's deliberate act: the button never overrides it
    apt-mark hold docker-ce >"${OUT}/hold.txt" 2>&1
    call_as internal packages-held GET /v1/sys/packages
    held_id="$(plan_id_of packages-held)"
    post_plan held-post "${held_id:-${PLAN_ID}}"
    apt-mark unhold docker-ce >>"${OUT}/hold.txt" 2>&1

    # another process holds dpkg's lock (an fcntl lock, which is what dpkg takes and the core asks about)
    cat >"${AUTH_DIR}/lockhold.py" <<'PY'
import fcntl, sys, time
f = open(sys.argv[1], "a")
fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("locked", flush=True)
time.sleep(float(sys.argv[2]))
PY
    python3 "${AUTH_DIR}/lockhold.py" /var/lib/dpkg/lock-frontend 120 >"${AUTH_DIR}/lock.out" 2>&1 &
    LOCK_PID=$!
    for _ in $(seq 1 20); do
        if grep -q locked "${AUTH_DIR}/lock.out"; then break; fi
        sleep 0.5
    done
    grep -q locked "${AUTH_DIR}/lock.out" || abort "could not take dpkg's lock"
    post_plan lock-post "${PLAN_ID}"
    kill "${LOCK_PID}" 2>/dev/null || true
    wait "${LOCK_PID}" 2>/dev/null || true
    LOCK_PID=""

    # a generic package update is running: a stand-in that holds the unit's name, which is all the core looks at
    systemd-run --quiet --unit=casaos-package-update.service --collect --property=Type=exec /bin/sleep 300
    [ "$(systemctl is-active casaos-package-update.service)" = active ] || abort "could not start the stand-in for the package update"
    post_plan unit-post "${PLAN_ID}"
    systemctl stop casaos-package-update.service

    # a plan that is not the plan, a body that is not a plan_id, and how the credentials may arrive
    post_plan wrong-post "${wrong}"
    CALL_MAX=60 call_as internal bad-post POST /v1/sys/docker/update '{"plan_id":"not-a-plan-id"}'
    body="$(jq -nc --arg p "${wrong}" '{plan_id: $p}')"
    CALL_MAX=60 call_as none query-post POST "/v1/sys/docker/update?token=${ACCESS}" "${body}"
    post_plan refresh-post "${wrong}" refresh
    post_plan jwt-post "${wrong}" jwt

    # and the hold lifted: the same plan again, the one the update will confirm
    call_as internal packages-2 GET /v1/sys/packages
    PLAN_ID="$(plan_id_of packages-2)"
    [ -n "${PLAN_ID}" ] || abort "after the hold was lifted the check has no plan_id"
}

# ---- the update ------------------------------------------------------------------------------------------------

update_run() {
    dpkg_dump before
    marker post_start
    post_plan update-post "${PLAN_ID}"
    marker post_done
    [ "$(cat "${OUT}/update-post.code")" = 200 ] || abort "the POST of the right plan was not accepted (HTTP $(cat "${OUT}/update-post.code")): nothing to follow"
    # the unit is running now: a second press must be refused
    post_plan running-post "${PLAN_ID}"
    poll_status status 1500
    marker terminal
    settle settle
    marker settled
    log "the update ended: $(jq -c '.data | {state, outcome, error_code, from, to}' "${OUT}/status-final.json" 2>/dev/null || echo unreadable)"

    snapshot after
    dpkg_dump after
    cp "$(runtime_log)" "${OUT}/docker-update.log" 2>/dev/null || true
    collect_journal unit-journal.txt
    timeout -s KILL 20 docker logs --timestamps p-db >"${OUT}/db-logs.txt" 2>&1 || true
    timeout -s KILL 30 docker run --rm -v pdb:/data busybox cat /data/log >"${OUT}/db-file.txt" 2>"${OUT}/db-file.err" || true
    local code=000
    for _ in $(seq 1 30); do
        code="$(code_of "${AUTH_DIR}/jwt" "${CASA_URL}/v2/app_management/compose" 5)"
        if [ "${code}" = 200 ]; then break; fi
        sleep 2
    done
    echo "${code}" >"${OUT}/am-compose.code"
    code_of - http://127.0.0.1:18081/ 5 >"${OUT}/web-after.code"
    call_as internal packages-after GET /v1/sys/packages
    CALL_MAX=60 call_as internal containers-after GET /v1/sys/docker/containers
}

# collect_journal <file>: what systemd and the unit said about casaos-docker-update.service so far, every run of it. The report
# looks in it for the line systemd writes when it rewrites ${NAME} in the unit's command line and has no value for the name.
collect_journal() { journalctl -u casaos-docker-update.service --no-pager -o short-iso >"${OUT}/$1" 2>&1 || true; }

runtime_log() {
    local dir
    dir="$(sed -nE 's/^[[:space:]]*LogPath[[:space:]]*=[[:space:]]*([^[:space:]]+).*/\1/p' /etc/casaos/casaos.conf 2>/dev/null | sed -n 1p)"
    echo "${dir:-/var/log/casaos}/docker-update.log"
}

# ---- the way back, major leg: the rollback command, run for real --------------------------------------------------------

# rollback_run: after the success, the command the core prints after a failure (sudo apt-get install --allow-downgrades <the pins of the
# PREVIOUS marker>) is run, on the box that has just gone from Docker 28 to 29 and has run the apps on it since; then what is left is
# recorded: Docker's version, the containers, the volume's content, the images. -y and --force-confold are the only things added to the
# command, so that it can run with nobody to answer. A PREVIOUS without a pin leaves no command: rollback-exit says not-run, which the
# report counts as the rollback failing (the core had nothing to give). Nothing is judged here.
rollback_run() {
    local pins=() rc=0
    marker rollback_start
    mapfile -t pins < <(previous_pins "${OUT}/docker-update.log" || true)
    if [ "${#pins[@]}" -eq 0 ]; then
        echo none >"${OUT}/rollback-command"
        echo not-run >"${OUT}/rollback-exit"
        log "the log's PREVIOUS marker has no pin: no rollback command to run"
        return 0
    fi
    echo "sudo apt-get install --allow-downgrades ${pins[*]}" >"${OUT}/rollback-command"
    "${APT[@]}" install -y -q --allow-downgrades -o Dpkg::Options::=--force-confold "${pins[@]}" >"${OUT}/rollback.log" 2>&1 || rc=$?
    echo "${rc}" >"${OUT}/rollback-exit"
    marker rollback_done
    log "the rollback command ended with ${rc}"
    wait_docker 300 || true
    settle rollback-settle
    marker rollback_settled
    snapshot rollback
    images_dump rollback
    timeout -s KILL 20 docker logs --timestamps p-db >"${OUT}/rollback-db-logs.txt" 2>&1 || true
    timeout -s KILL 30 docker run --rm -v pdb:/data busybox cat /data/log >"${OUT}/rollback-db-file.txt" 2>"${OUT}/rollback-db-file.err" || true
    code_of - http://127.0.0.1:18081/ 5 >"${OUT}/rollback-web.code"
}

# ---- the unit killed in the middle of the install (one minor leg) -----------------------------------------------------------

# catch_maintainer <file> <seconds>: waits for dpkg to run a maintainer script of one of Docker's packages, and then kills the whole unit
# with SIGKILL (every process of its cgroup: the script, apt-get, dpkg), so that dpkg is left in the middle of a package. <file> gets
# `caught <time> <pid> <command line>`, or `missed <why>` when the unit ended first, nothing ran in time, or systemctl could not kill.
catch_maintainer() {
    local end=$((SECONDS + $2)) begun=${SECONDS} seen=0 hit="" state
    while :; do
        hit="$(pgrep -af "${MAINTAINER_RE}" | head -n1 || true)"
        if [ -n "${hit}" ]; then break; fi
        state="$(systemctl is-active casaos-docker-update.service 2>/dev/null || true)"
        case "${state}" in
        active | activating | deactivating) seen=1 ;;
        *)
            # systemd-run --no-block returns before the unit is up: it gets UNIT_GRACE seconds (30) to appear before it counts as gone
            if [ "${seen}" = 1 ] || [ $((SECONDS - begun)) -ge "${UNIT_GRACE:-30}" ]; then
                echo "missed the unit ended (${state:-unknown}) before dpkg ran a maintainer script of Docker's" >"$1"
                return 0
            fi
            ;;
        esac
        if [ "${SECONDS}" -ge "${end}" ]; then
            echo "missed no maintainer script of Docker's ran within $2 s" >"$1"
            return 0
        fi
        sleep 0.1
    done
    # every process of the unit's cgroup (what --kill-who defaults to; the option is spelled differently from one systemd to the next)
    if ! systemctl kill --signal=KILL casaos-docker-update.service; then
        echo "missed systemctl kill failed" >"$1"
        return 0
    fi
    echo "caught $(now) ${hit}" >"$1"
}

# audit_dpkg <file>: what `dpkg --audit` says now: its exit status on the first line, then its text (nothing when it finds nothing wrong)
audit_dpkg() {
    local out rc=0
    out="$(dpkg --audit 2>&1)" || rc=$?
    { echo "exit ${rc}"; if [ -n "${out}" ]; then printf '%s\n' "${out}"; fi; } >"$1"
}

# repair_dpkg <prefix>: the repair the dashboard names after an install that was cut short, in its order: `dpkg --configure -a`, then
# `apt-get -f install`, with dpkg --audit after each; then Docker is started, as the cut may have left it down. -y and --force-confold are
# all that is added to them, so that nobody has to answer. <prefix>-docker says whether Docker answered.
repair_dpkg() {
    local rc=0 start=${SECONDS}
    dpkg --configure -a --force-confold >"${OUT}/$1-repair-1.log" 2>&1 || rc=$?
    echo "${rc}" >"${OUT}/$1-repair-1.exit"
    audit_dpkg "${OUT}/$1-audit-1.txt"
    rc=0
    "${APT[@]}" -f install -y -q -o Dpkg::Options::=--force-confold >"${OUT}/$1-repair-2.log" 2>&1 || rc=$?
    echo "${rc}" >"${OUT}/$1-repair-2.exit"
    audit_dpkg "${OUT}/$1-audit-after.txt"
    systemctl reset-failed docker.service docker.socket containerd.service >/dev/null 2>&1 || true
    timeout 120 systemctl start containerd.service docker.socket docker.service || true
    if wait_docker 120; then
        echo "yes, after $((SECONDS - start)) s" >"${OUT}/$1-docker"
    else
        echo no >"${OUT}/$1-docker"
    fi
}

# kill_run: the box is put back, the update asked for the way the dashboard does, and as soon as dpkg runs a maintainer script of Docker's
# the unit gets SIGKILL. What the status says (it has to find that out by itself, from a log with no end), what dpkg --audit says, and the
# repair the dashboard names are recorded. A run that cannot get as far leaves `missed <why>` in kill-hit, which the report counts as a run
# that proved nothing, and goes on: the failure that follows still has its turn. Nothing is judged here.
kill_run() {
    local id
    put_back kill-putback
    call_as internal kill-packages GET /v1/sys/packages
    id="$(plan_id_of kill-packages)"
    if [ -z "${id}" ]; then
        echo "missed after the box was put back the check has no plan_id" >"${OUT}/kill-hit"
        return 0
    fi
    marker kill_post
    post_plan kill-post "${id}"
    if [ "$(cat "${OUT}/kill-post.code")" != 200 ]; then
        echo "missed the POST was not accepted (HTTP $(cat "${OUT}/kill-post.code"))" >"${OUT}/kill-hit"
        return 0
    fi
    catch_maintainer "${OUT}/kill-hit" 900
    marker kill_killed
    log "kill -9 injection: $(cat "${OUT}/kill-hit")"
    if grep -q '^caught ' "${OUT}/kill-hit"; then
        sleep 2
        audit_dpkg "${OUT}/kill-audit.txt"
    fi
    poll_status kill-status 300
    marker kill_terminal
    cp "$(runtime_log)" "${OUT}/docker-update-kill.log" 2>/dev/null || true
    log "the killed update ended: $(jq -c '.data | {state, outcome, error_code, rollback_command}' "${OUT}/kill-status-final.json" 2>/dev/null || echo unreadable)"
    if grep -q '^caught ' "${OUT}/kill-hit"; then repair_dpkg kill; fi
}

# ---- the failure, last ---------------------------------------------------------------------------------------------

mend_daemon_json() {
    if [ -e "${AUTH_DIR}/daemon.json.orig" ]; then
        cp -a "${AUTH_DIR}/daemon.json.orig" /etc/docker/daemon.json
    else
        rm -f /etc/docker/daemon.json
    fi
    DAEMON_JSON_BROKEN=0
}

# put_back <prefix>: the box is put back where the first run found it: the packages the plan moved, at the versions they had (a package
# the update brought has no version to go back to: it stays). <prefix>-downgrade.log is what apt said, <prefix>-settle how long the
# containers that start by themselves took to be back.
put_back() {
    local pins=()
    mapfile -t pins < <(jq -r '.data.docker.update.packages[] | select((.new // false) | not) | "\(.name)=\(.current_version)"' "${OUT}/packages-2.json")
    [ "${#pins[@]}" -gt 0 ] || abort "no packages to put back"
    "${APT[@]}" install -y -q --allow-downgrades "${pins[@]}" >"${OUT}/$1-downgrade.log" 2>&1 || abort "could not put the box back on ${START_VERSION}"
    wait_docker || abort "docker does not answer after the box was put back"
    settle "$1-settle"
    "${APT[@]}" update -qq
}

failure_run() {
    local fail_id start
    put_back putback
    call_as internal fail-packages GET /v1/sys/packages
    fail_id="$(plan_id_of fail-packages)"
    [ -n "${fail_id}" ] || abort "after the box was put back the check has no plan_id: the failure injection cannot run"

    # dockerd cannot start with this file; the running one does not read it again, so the button still sees a daemon
    if [ -e /etc/docker/daemon.json ]; then cp -a /etc/docker/daemon.json "${AUTH_DIR}/daemon.json.orig"; fi
    mkdir -p /etc/docker
    printf '{ "the proof": this is not json\n' >/etc/docker/daemon.json
    DAEMON_JSON_BROKEN=1
    marker fail_post
    post_plan fail-post "${fail_id}"
    [ "$(cat "${OUT}/fail-post.code")" = 200 ] || { mend_daemon_json; abort "the POST of the failure run was not accepted (HTTP $(cat "${OUT}/fail-post.code"))"; }
    poll_status fail-status 1500
    marker fail_terminal
    log "the failing update ended: $(jq -c '.data | {state, outcome, error_code, rollback_command}' "${OUT}/fail-status-final.json" 2>/dev/null || echo unreadable)"
    cp "$(runtime_log)" "${OUT}/docker-update-fail.log" 2>/dev/null || true
    journalctl -u docker.service --no-pager -n 60 >"${OUT}/docker-journal-fail.txt" 2>&1 || true

    # mend the file and bring Docker back
    mend_daemon_json
    start=${SECONDS}
    systemctl reset-failed docker.service docker.socket containerd.service >/dev/null 2>&1 || true
    timeout 120 systemctl start containerd.service docker.socket docker.service || true
    if wait_docker 120; then
        echo "yes, after $((SECONDS - start)) s" >"${OUT}/fail-repaired"
    else
        echo "no" >"${OUT}/fail-repaired"
    fi
    settle fail-settle
    marker fail_settled
}

# ---- the run -------------------------------------------------------------------------------------------------------------

main() {
    set -euo pipefail
    LEG="${1:-}"
    case "${LEG}" in
    major) [[ "${DOCKER_FROM:-}" =~ ^[0-9]{2}\.[0-9]+\.[0-9]+$ ]] || { echo "major needs DOCKER_FROM like 28.0.4" >&2; exit 2; } ;;
    minor) ;;
    *) echo "usage: $0 major|minor" >&2; exit 2 ;;
    esac
    # PROOF_KILL_INSTALL=1: this minor leg also kills the unit in the middle of the install (the report has to be told, with `kill`)
    KILL_INSTALL=0
    case "${PROOF_KILL_INSTALL:-}" in
    "" | 0) ;;
    1) KILL_INSTALL=1 ;;
    *) echo "PROOF_KILL_INSTALL is 1 or empty" >&2; exit 2 ;;
    esac
    [ "${KILL_INSTALL}" = 0 ] || [ "${LEG}" = minor ] || { echo "the kill -9 injection (PROOF_KILL_INSTALL=1) belongs to a minor leg" >&2; exit 2; }
    [[ "${PROOF_DISPOSABLE:-}" == 1 ]] ||
        { echo "This installs and removes Docker, holds dpkg's lock and breaks Docker's configuration: run it on a disposable machine (PROOF_DISPOSABLE=1)." >&2; exit 2; }
    [ "$(id -u)" -eq 0 ] || { echo "Run it as root." >&2; exit 2; }
    if [ -e /etc/casaos ] || [ -e /var/lib/casaos ] || command -v docker >/dev/null 2>&1; then
        echo "This machine already runs ReCasaOS or Docker: not a fresh one, not touching it." >&2
        exit 2
    fi
    . /etc/os-release
    test "${ID}" = "${EXPECT_ID:?}"
    test "${VERSION_ID}" = "${EXPECT_VERSION_ID:?}"

    OUT="${OUT:-${PWD}/proof-out}"
    mkdir -p "${OUT}"
    : >"${OUT}/timeline.tsv"
    echo "${PRETTY_NAME}" >"${OUT}/os"
    echo "${LEG}" >"${OUT}/leg"
    AUTH_DIR="$(mktemp -d)"
    chmod 700 "${AUTH_DIR}"
    trap cleanup EXIT

    export DEBIAN_FRONTEND=noninteractive
    APT=(env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3)
    cloud-init status --wait >/dev/null 2>&1 || true
    # apt's own timers would run in the middle of the proof, and hold the lock the proof asks about
    systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service >/dev/null 2>&1 || true
    if [ "${ID}" = debian ] && [ "${VERSION_ID}" = 11 ] &&
        grep -Eq '^[[:space:]]*deb(-src)?[[:space:]].*//(deb|security)\.debian\.org/debian-security' /etc/apt/sources.list 2>/dev/null; then
        # out of support: the change --use-debian-archive makes, made first because Docker's own installer runs apt before ReCasaOS's does
        cp -p /etc/apt/sources.list /etc/apt/sources.list.proof.bak
        sed -i -E 's#//(deb|security)\.debian\.org/debian-security#//archive.debian.org/debian-security#' /etc/apt/sources.list
    fi
    "${APT[@]}" update -qq
    "${APT[@]}" install -y -q curl jq ca-certificates python3 procps >/dev/null

    install_docker
    echo "${START_VERSION}" >"${OUT}/start-version"
    install_casaos
    prepare_dependency_path
    start_things
    "${APT[@]}" update -qq
    {
        echo "installed $(dpkg-query -W -f='${Version}' docker-ce)"
        echo "candidate $(apt-cache policy docker-ce | awk '/Candidate:/ {print $2; exit}')"
    } >"${OUT}/apt-docker-ce.txt"
    cat "${OUT}/apt-docker-ce.txt"
    snapshot before
    images_dump before

    poll_timeline &
    POLL_PID=$!
    sleep 5
    refusals
    update_run
    if [ "${LEG}" = major ]; then rollback_run; fi
    if [ "${KILL_INSTALL}" = 1 ]; then kill_run; fi
    if [ "${LEG}" = minor ]; then failure_run; fi
    collect_journal unit-journal-end.txt
    kill "${POLL_PID}" 2>/dev/null || true
    POLL_PID=""
    log "done"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then main "$@"; fi
