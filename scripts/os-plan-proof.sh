#!/usr/bin/env bash
# Proof that the PLAN of the operating system upgrade of ReCasaOS (POST and GET /v1/sys/os/upgrade/plan, GET /v1/sys/os/upgrade/runbook) changes nothing on the machine,
# says what apt would do, and refuses what it should, on a real machine.
#
# Runs as root inside a DISPOSABLE Debian 11 machine (a QEMU guest in CI), next to install.sh (the published installer) and os-readiness-proof.sh, whose functions it
# SOURCES and does not copy (the way the machine is prepared, the control and the snapshots of the window, the lock of dpkg, the dirt that goes with an undo). It installs
# Docker (pinned to DOCKER_FROM: the owner's box, with a newer one on offer) and ReCasaOS, starts a few witness containers, and then asks the core for the plan the way
# the dashboard does: through the gateway, with the loopback secret (Authorization: Internal <secret>) the services use among themselves.
#
#   os-plan-proof.sh d11   Debian 11 (installed with --use-debian-archive): the plan of the hop to Debian 12
#
# In this order:
#   0. the machine as the readiness proof prepares it (timers off, the installer, Docker 28.0.4, the four witness containers that come back by themselves, PackageKit
#      quiet, AppManagement's own check of the images waited for), the box's own sources and apt configuration copied into $OUT (the report computes from them what
#      the plan has to say), and the names that the answers must not say (the box's hostname, addresses, MAC and user: leak-needles.tsv);
#   1. a CONTROL of CONTROL_SECONDS (320): a snapshot, two calls of GET /v1/sys/version/current, the wait, a snapshot, and PID 1's journal of it: what changes by itself;
#   2. the WINDOW: a snapshot (the seven trees: the six of the readiness proof and /var/log/apt, the units, the sockets, the listing of /var/tmp, the witness containers), GET of the plan before any
#      plan (the first call: a release that has no such route answers 404, and the run ends there), GET of the runbook before any plan, POST, a second POST at once,
#      GET every second until the job is done (15 minutes at most; the sandbox directories seen in /var/tmp while it runs), the same snapshot;
#   3. the runbook of that plan: GET, and every block of its text read by `sh -n` (never run: it would do the upgrade);
#   4. a second plan at once (the same plan_id), and a plan with base-files on hold (another plan_id, which apt cannot carry out: blocked, and no runbook); a POST that is
#      refused ends there: there is no job to watch, and the plan the core answers afterwards is an earlier one;
#   5. the DIRTY matrix, one change at a time: the dirt, POST (a refusal at once, or a job that fails: watched to its end, and the runbook asked for), the undo (always:
#      a trap runs it when the script dies in the middle of a step), a check that the undo held, a look for a sandbox directory left in /var/tmp. Three of its steps are
#      CANARIES: a fragment of apt's configuration with a real hook of `apt-get update` in it, written so that a filter of the text of the fragment does not see it (apt
#      reads its configuration as a language: a comment spliced into a word, an #include), and a fragment with the option of the gpgv that apt runs as _apt, which names
#      a file for it to write (Acquire::gpgv::Options, an Acquire:: name that the allow-list of the sandbox does not have). The plan has to leave the fragment out and name
#      it, and the file that the hook (or gpgv) makes must not be there when the plan is done: a sandbox that ran the hook, or kept the option, would have made it;
#   6. a last plan after the undos: it has the plan_id of the first.
#
# Nothing here judges: it writes what it saw into $OUT and os-plan-proof-report.py turns that into verdicts, or says the run is invalid. It stops early (and says why,
# in $OUT/aborted) only when it cannot go on. A step whose dirt could not be made, or whose undo failed, is recorded as such: the report makes the leg INVALID, never a
# FAIL of the feature.
#
# Needs: PLAN_DISPOSABLE=1, INSTALLER_ARGS (options for install.sh, unquoted on purpose: a list), DOCKER_FROM (the Docker the box starts on), OUT (default ./plan-out), and
# os-readiness-proof.sh next to this file (READINESS_LIB names another).

HERE_PLAN="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# the trees of the invariance window: the readiness proof's six, and /var/log/apt (the planner of apt writes its log of a simulated transaction there, which the sandbox's
# apt-config turns off, and which nothing else of the window would see). Only /var/log/casaos may change. The report has the same list (TREES)
if [ -z "${INV_TREES+x}" ]; then INV_TREES=(/etc /var/lib/dpkg /var/lib/apt /var/cache/apt /var/lib/casaos /var/log/apt /var/log/casaos); fi
# shellcheck source=os-readiness-proof.sh
. "${READINESS_LIB:-${HERE_PLAN}/os-readiness-proof.sh}"

# ---- the parts that are only text, and can be tested without a machine -----------------------------------------------------------------

# the steps of the dirty matrix, in the order it runs: the ones that fail in the first phase, the refusals, the one that fetches the lists of the target and fails, then the
# three canaries (each makes a whole plan). os-plan-proof-report.py has the same list (STEPS).
PLAN_STEPS=(format cdrom alias target mixed busy noroom deadmirror canarysplice canaryinclude canarygpgv)
# what a plan may take at the most before the guest says it did not end (the core's own job has 20 minutes; its commands have sixteen)
PLAN_WAIT="${PLAN_WAIT:-900}"
# the package whose hold changes the plan_id, and that the minimal upgrade touches
HOLD_PACKAGE="${HOLD_PACKAGE:-base-files}"
PLAN_PATH=/v1/sys/os/upgrade/plan
RUNBOOK_PATH=/v1/sys/os/upgrade/runbook
SANDBOX_GLOB="${SANDBOX_GLOB:-casaos-os-plan.*}"
TMP_DIR="${TMP_DIR:-/var/tmp}"

# the names of the files that apt reads in sources.list.d and apt.conf.d, as the core reads them (osreadiness.AptSourceName, osplan.aptFragmentName)
SRC_NAME_RE='^[A-Za-z0-9_:-][A-Za-z0-9_:.-]*\.(list|sources)$'
CONF_NAME_RE='^[A-Za-z0-9_.:-]+$'
# the biggest fragment of apt's configuration that the sandbox keeps
CONF_MAX_BYTES=65536
# where apt-get update puts its stamps, and the hooks of the canaries touch their files; and the file that the include of the second canary names
PERIODIC_DIR="${PERIODIC_DIR:-/var/lib/apt/periodic}"
CANARY_INCLUDE_FILE="${CANARY_INCLUDE_FILE:-${APT_DIR}/osp-canary-include.conf}"
# where the gpgv of the third canary is told to write its log: the box's own lists of apt, in the directory of the files that are being fetched (owned by _apt)
LISTS_PARTIAL="${LISTS_PARTIAL:-/var/lib/apt/lists/partial}"
CANARY_GPGV_FILE="${CANARY_GPGV_FILE:-${LISTS_PARTIAL}/osp-canary}"

# ---- talking to the core ------------------------------------------------------------------------------------------------------------------

# call_core <name> <GET|POST> <path> [seconds]: one request through the gateway, with the loopback secret. Written into $OUT: <name>.json (the body), .hdr (the headers),
# .code (the HTTP status, 000 when there was none), .err (what curl said), .t0 and .t1 (the time before and after, in seconds since the epoch), and .curl last (how curl ended:
# 0, or why it got no answer; a call that was cut short leaves none, which the report reads as a call that did not run). A POST has no body.
call_core() {
    local name="$1" method="$2" path="$3" max="${4:-45}" code="" rc=0
    local opts=()
    if [ "${method}" = POST ]; then opts=(-X POST --data-binary ''); fi
    printf '%s\n' "${EPOCHREALTIME}" >"${OUT}/${name}.t0"
    code="$(curl -sS --max-time "${max}" "${opts[@]}" -D "${OUT}/${name}.hdr" -o "${OUT}/${name}.json" -w '%{http_code}' -H "$(internal_header)" "http://127.0.0.1:${CASA_PORT}${path}" 2>"${OUT}/${name}.err")" || rc=$?
    printf '%s\n' "${EPOCHREALTIME}" >"${OUT}/${name}.t1"
    printf '%s\n' "${code:-000}" >"${OUT}/${name}.code"
    printf '%s\n' "${rc}" >"${OUT}/${name}.curl"
}

# code_in <name>: the HTTP status the call left
code_in() { cat "${OUT}/$1.code" 2>/dev/null || echo 000; }

# json_field <file> <jq filter>: what the filter says of a body, "-" when it is not JSON or says nothing
json_field() { jq -r "$2" "$1" 2>/dev/null || echo "-"; }

# sandbox_dirs: the sandbox directories of the plan that are in /var/tmp now, one a line (nothing when there are none). The paths are written as /var/tmp/...: where TMP_DIR is
# another directory (the harness's), the report sees the paths of a box all the same
sandbox_dirs() { find "${TMP_DIR}" -maxdepth 1 -type d -name "${SANDBOX_GLOB}" 2>/dev/null | sed "s#^${TMP_DIR}#/var/tmp#" | LC_ALL=C sort; }

# plan_wait <prefix>: GET of the plan every second until the job is done or failed, PLAN_WAIT seconds at the most (<prefix>.timeout says it did not end). One row a poll in
# <prefix>.polls.tsv: the time before and after the call, the HTTP status, how curl ended, the state and the phase (- when the answer says none); the sandbox directories seen
# while the job ran in <prefix>.sandbox-seen; and the last answer is kept as <prefix>-get (the files of a call). The prefix is plan-p1 for a run, step-<id> for a step.
plan_wait() {
    local prefix="$1" end=$((SECONDS + PLAN_WAIT)) state phase ext
    : >"${OUT}/${prefix}.polls.tsv"
    : >"${OUT}/${prefix}.sandbox-seen"
    rm -f "${OUT}/${prefix}.timeout"
    while :; do
        call_core poll-now GET "${PLAN_PATH}" 20
        state="$(json_field "${OUT}/poll-now.json" '.data.state // "-"')"
        phase="$(json_field "${OUT}/poll-now.json" '.data.phase // "-"')"
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$(cat "${OUT}/poll-now.t0")" "$(cat "${OUT}/poll-now.t1")" "$(cat "${OUT}/poll-now.code")" "$(cat "${OUT}/poll-now.curl")" "${state}" "${phase}" >>"${OUT}/${prefix}.polls.tsv"
        if [ "${state}" = running ]; then sandbox_dirs >>"${OUT}/${prefix}.sandbox-seen"; fi
        if [ "${state}" = done ] || [ "${state}" = failed ]; then break; fi
        if [ "${SECONDS}" -ge "${end}" ]; then
            echo "${PLAN_WAIT}" >"${OUT}/${prefix}.timeout"
            break
        fi
        sleep 1
    done
    for ext in json hdr err t0 t1 code curl; do cp "${OUT}/poll-now.${ext}" "${OUT}/${prefix}-get.${ext}" 2>/dev/null || true; done
}

# probe_plan_route: the first call of the plan routes, the first call of the window. A release that has none (every release before the one that carries them) answers 404 to it
# and to every call that follows: the run ends there, before the window's second snapshot and the dirty matrix. Recorded as plan-idle, which the report judges like the others.
probe_plan_route() {
    call_core plan-idle GET "${PLAN_PATH}" 30
    [ "$(code_in plan-idle)" != 404 ] || abort "the release under test has no ${PLAN_PATH}"
}

# plan_run <run>: POST and wait for the job, as the run p2, p3 or p4 is made. A POST that was refused (busy, no room) began no job: there is nothing to watch, and the plan that the
# core answers is the one it made before, which the report must not take for this run's (it says what the core refused, and the run is invalid)
plan_run() {
    call_core "plan-$1-post" POST "${PLAN_PATH}" 20
    if [ "$(code_in "plan-$1-post")" = 202 ]; then plan_wait "plan-$1"; fi
}

# runbook_shn <call>: the text of the runbook that <call> got, split into its blocks by their own markers, each block read by `sh -n` (the blocks are never run: they would do
# the upgrade). <call>.shn.tsv has a row for each block: its index, its id and what sh -n said (0: it reads it).
runbook_shn() {
    local tsv="${OUT}/$1.shn.tsv" i id code
    rm -f "${OUT}"/rb-block-*.sh "${OUT}"/rb-block-*.err "${OUT}/rb-index.tsv"
    : >"${tsv}"
    : >"${OUT}/rb-index.tsv"
    json_field "${OUT}/$1.json" '.data.text // empty' >"${OUT}/$1.text"
    awk -v dir="${OUT}" '
        /^# >>> BLOCK [0-9]+ / { file = dir "/rb-block-" $4 ".sh"; print $4 "\t" $5 >> (dir "/rb-index.tsv"); inside = 1; next }
        /^# <<< END BLOCK / { if (inside) close(file); inside = 0; next }
        inside { print > file }' "${OUT}/$1.text"
    while IFS=$'\t' read -r i id; do
        if sh -n "${OUT}/rb-block-${i}.sh" 2>"${OUT}/rb-block-${i}.err"; then code=0; else code=$?; fi
        printf '%s\t%s\t%s\n' "${i}" "${id}" "${code}" >>"${tsv}"
    done <"${OUT}/rb-index.tsv"
}

# ---- what is written down -------------------------------------------------------------------------------------------------------------------

# copy_sources: the files that apt reads its sources from, as the core reads them (sources.list, then the files of sources.list.d whose names are apt's), copied whole: the
# report computes from them what the plan has to say. sources.index names them, src-main is sources.list and src-d-<name> a file of sources.list.d
copy_sources() {
    local f name
    : >"${OUT}/sources.index"
    if [ -f "${APT_DIR}/sources.list" ]; then
        echo sources.list >>"${OUT}/sources.index"
        cp "${APT_DIR}/sources.list" "${OUT}/src-main"
    fi
    for f in "${APT_DIR}"/sources.list.d/*; do
        [ -f "${f}" ] || continue
        name="${f##*/}"
        [[ "${name}" =~ ${SRC_NAME_RE} ]] || continue
        echo "sources.list.d/${name}" >>"${OUT}/sources.index"
        cp "${f}" "${OUT}/src-d-${name}"
    done
}

# copy_aptconf: the fragments of apt's configuration that apt reads, and apt.conf: the report works out from them which files the plan has to say it left out of the sandbox's
# configuration (it keeps a file only when every line of it is a comment or a statement of a very small kind), and for which reason. aptconf.index names the fragments,
# aptconf-d-<name> is each one, aptconf.large the ones of more than 64 KiB (not copied), aptconf.nonregular the ones that are a link, a fifo or a device (not copied: the core
# does not read them, and names them), aptconf.dirs the ones that are a directory (not copied: apt passes over a directory of apt.conf.d without a word, and so does the core;
# it counts among the 200 that are read), aptconf-main is apt.conf (aptconf.nonregular names it `apt.conf` when it is not a regular file: a directory too, there)
copy_aptconf() {
    local f name
    : >"${OUT}/aptconf.index"
    : >"${OUT}/aptconf.large"
    : >"${OUT}/aptconf.nonregular"
    : >"${OUT}/aptconf.dirs"
    for f in "${APT_DIR}"/apt.conf.d/*; do
        [ -e "${f}" ] || [ -L "${f}" ] || continue
        name="${f##*/}"
        [[ "${name}" =~ ${CONF_NAME_RE} ]] || continue
        echo "${name}" >>"${OUT}/aptconf.index"
        if [ -L "${f}" ]; then
            echo "${name}" >>"${OUT}/aptconf.nonregular"
        elif [ -d "${f}" ]; then
            echo "${name}" >>"${OUT}/aptconf.dirs"
        elif [ ! -f "${f}" ]; then
            echo "${name}" >>"${OUT}/aptconf.nonregular"
        elif [ "$(wc -c <"${f}")" -gt "${CONF_MAX_BYTES}" ]; then
            echo "${name}" >>"${OUT}/aptconf.large"
        else
            cp "${f}" "${OUT}/aptconf-d-${name}"
        fi
    done
    if [ -L "${APT_DIR}/apt.conf" ] || { [ -e "${APT_DIR}/apt.conf" ] && [ ! -f "${APT_DIR}/apt.conf" ]; }; then
        echo apt.conf >>"${OUT}/aptconf.nonregular"
    elif [ -f "${APT_DIR}/apt.conf" ]; then
        cp "${APT_DIR}/apt.conf" "${OUT}/aptconf-main"
    fi
}

# tmp_snapshot <tag>: the listing of /var/tmp (paths, sorted): a sandbox directory that the plan leaves behind is a path of it
tmp_snapshot() { find "${TMP_DIR}" -mindepth 1 2>/dev/null | sed "s#^${TMP_DIR}#/var/tmp#" | LC_ALL=C sort >"${OUT}/tmp-$1.txt"; }

# witnesses <tag>: the containers that run, each with its name, the time it started at and its pid (a container that was restarted has another)
witnesses() {
    docker ps -q | xargs -r docker inspect --format "{{.Name}}"$'\t'"{{.State.StartedAt}}"$'\t'"{{.State.Pid}}" | sed 's#^/##' | LC_ALL=C sort >"${OUT}/witnesses-$1.txt"
}

# window_snapshot <tag>: the files of the six trees, the units and the sockets (the readiness proof's), and the listing of /var/tmp
window_snapshot() {
    inv_snapshot "$1"
    tmp_snapshot "$1"
}

# ---- the window -----------------------------------------------------------------------------------------------------------------------------

window() {
    marker control_start
    window_snapshot ctl-a
    api ctl-core-1 /v1/sys/version/current 20
    api ctl-core-2 /v1/sys/version/current 20
    # the wait is the control: a unit that the box starts every few minutes by itself has to fall in it (the report reads PID 1's journal of it right below)
    log "the control waits ${CONTROL_SECONDS} s: what the box starts by itself every few minutes falls in it"
    sleep "${CONTROL_SECONDS}"
    window_snapshot ctl-b
    pid1_window control_start pid1-control.jsonl
    systemctl list-timers --all --no-legend --plain 2>/dev/null | awk 'NF {print $NF}' | sort -u >"${OUT}/timer-units.txt" || true
    marker invariance_start
    window_snapshot before
    witnesses before
    probe_plan_route
    call_core runbook-idle GET "${RUNBOOK_PATH}" 30
    call_core plan-p1-post POST "${PLAN_PATH}" 20
    [ "$(code_in plan-p1-post)" = 202 ] || abort "the first plan was not accepted: HTTP $(code_in plan-p1-post), $(json_field "${OUT}/plan-p1-post.json" '.data.error_code // .message // "-"')"
    call_core plan-p1-post-2 POST "${PLAN_PATH}" 20
    plan_wait plan-p1
    window_snapshot after
    witnesses after
    pid1_window invariance_start pid1-window.jsonl
    marker invariance_done
}

# ---- the dirty matrix: dirty_<id>, undo_<id>, undone_<id> for every id of PLAN_STEPS (and for the hold) ------------------------------------------------------

# a deb822 file: apt reads it, the plan does not
dirty_format() { mkdir -p "${APT_DIR}/sources.list.d" && printf 'Types: deb\nURIs: http://deb.debian.org/debian\nSuites: %s\nComponents: main\n' "${FROM_CODENAME}" >"${APT_DIR}/sources.list.d/osp-deb822.sources"; }
undo_format() { rm -f "${APT_DIR}/sources.list.d/osp-deb822.sources"; }
undone_format() { [ ! -e "${APT_DIR}/sources.list.d/osp-deb822.sources" ]; }

dirty_cdrom() { src_dirty osp-cdrom.list "deb cdrom:[Plan proof disc 1]/ ${FROM_CODENAME} main"; }
undo_cdrom() { src_undo osp-cdrom.list; }
undone_cdrom() { src_undone osp-cdrom.list; }

# a suite that is an alias of the archive, where the box is on a codename: the plan will not guess which release it means
dirty_alias() { src_dirty osp-alias.list "deb http://deb.debian.org/debian stable main"; }
undo_alias() { src_undo osp-alias.list; }
undone_alias() { src_undone osp-alias.list; }

# a line of the distribution that is at the target already
dirty_target() { src_dirty osp-target.list "deb http://deb.debian.org/debian ${TO_CODENAME} main"; }
undo_target() { src_undo osp-target.list; }
undone_target() { src_undone osp-target.list; }

# a file that cannot be put aside as a file and rewritten as a file: a line of the distribution and one of another repository (Docker's own: a third party by its host name, which the
# plan decides without a Release file; a host that is not Debian's archive and has none in apt's lists is "undecided" for the plan, which refuses it for that and not for this)
dirty_mixed() { mkdir -p "${APT_DIR}/sources.list.d" && printf 'deb http://deb.debian.org/debian %s main\ndeb https://download.docker.com/linux/debian bullseye stable\n' "${FROM_CODENAME}" >"${APT_DIR}/sources.list.d/osp-mixed.list"; }
undo_mixed() { src_undo osp-mixed.list; }
undone_mixed() { src_undone osp-mixed.list; }

# a mirror of the distribution that resolves to nothing: its name is one of the country mirrors of Debian's own list (ftp<n>.<cc>.debian.org: there is no country zz), which the plan takes
# for the distribution's by that name, so that it rewrites it and asks for its lists, and the sandbox's update fails
dirty_deadmirror() { src_dirty osp-deadmirror.list "deb http://ftp9.zz.debian.org/debian ${FROM_CODENAME} main"; }
undo_deadmirror() { src_undo osp-deadmirror.list; }
undone_deadmirror() { src_undone osp-deadmirror.list; }

# a package manager at work: dpkg's lock held by another process (a POSIX record lock, which is what the core asks the kernel about: a flock(1) would not be seen)
dirty_busy() { lock_hold_start; }
undo_busy() { lock_hold_stop; }
undone_busy() { [ "$(lock_state)" = free ]; }

# no room for the sandbox: a small tmpfs over /var/tmp. The undo takes it away again (three tries: a unit that started in the meantime may hold it for a second)
dirty_noroom() {
    mount -t tmpfs -o size=64m,mode=1777 tmpfs "${TMP_DIR}"
    findmnt -n "${TMP_DIR}" >/dev/null
}
undo_noroom() {
    local i
    for i in 1 2 3; do
        if umount "${TMP_DIR}"; then return 0; fi
        sleep 1
    done
    return 1
}
undone_noroom() { ! findmnt -n "${TMP_DIR}" >/dev/null 2>&1; }

# the hold: a package that the minimal upgrade touches is on hold, which changes the plan_id and makes apt keep it back in the full upgrade
dirty_hold() { apt-mark hold "${HOLD_PACKAGE}"; }
undo_hold() { apt-mark unhold "${HOLD_PACKAGE}"; }
undone_hold() { [ -z "$(apt-mark showhold)" ]; }

# the canaries: a fragment of apt's configuration with a REAL hook of `apt-get update` in it (APT::Update::Post-Invoke-Success, which touches a file of /var/lib/apt/periodic),
# written so that a filter of its text does not see it, because apt reads its configuration as a language. canarysplice has a comment spliced into the key (`Post-In/**/voke-Success`
# is the key Post-Invoke-Success); canaryinclude has an #include of another file, which holds the hook. A box that has the fragment and no sandbox makes the file when its
# apt-get update ends (tried on apt 2.2.4 with a local repository: both do), a sandbox that moved apt's configuration does not. The plan has to leave the fragment out and name
# it, and the file must not be there when the plan is done. after_<id> is what a step looks at once its plan is done (plan_step runs it before the undo)
dirty_canarysplice() {
    mkdir -p "${PERIODIC_DIR}" "${APT_DIR}/apt.conf.d"
    rm -f "${PERIODIC_DIR}/osp-canary-splice"
    printf '%s\n' 'APT::Update::Post-In/**/voke-Success {"touch /var/lib/apt/periodic/osp-canary-splice";};' >"${APT_DIR}/apt.conf.d/99osp-canary-splice"
}
undo_canarysplice() { rm -f "${APT_DIR}/apt.conf.d/99osp-canary-splice" "${PERIODIC_DIR}/osp-canary-splice"; }
undone_canarysplice() { [ ! -e "${APT_DIR}/apt.conf.d/99osp-canary-splice" ] && [ ! -e "${PERIODIC_DIR}/osp-canary-splice" ]; }
after_canarysplice() { if [ -e "${PERIODIC_DIR}/osp-canary-splice" ]; then echo present; else echo absent; fi >"${OUT}/step-canarysplice.canary"; }

dirty_canaryinclude() {
    mkdir -p "${PERIODIC_DIR}" "${APT_DIR}/apt.conf.d"
    rm -f "${PERIODIC_DIR}/osp-canary-include"
    printf '%s\n' 'APT::Update::Post-Invoke-Success {"touch /var/lib/apt/periodic/osp-canary-include";};' >"${CANARY_INCLUDE_FILE}"
    printf '%s\n' '#include "/etc/apt/osp-canary-include.conf";' >"${APT_DIR}/apt.conf.d/99osp-canary-include"
}
undo_canaryinclude() { rm -f "${APT_DIR}/apt.conf.d/99osp-canary-include" "${CANARY_INCLUDE_FILE}" "${PERIODIC_DIR}/osp-canary-include"; }
undone_canaryinclude() { [ ! -e "${APT_DIR}/apt.conf.d/99osp-canary-include" ] && [ ! -e "${CANARY_INCLUDE_FILE}" ] && [ ! -e "${PERIODIC_DIR}/osp-canary-include" ]; }
after_canaryinclude() { if [ -e "${PERIODIC_DIR}/osp-canary-include" ]; then echo present; else echo absent; fi >"${OUT}/step-canaryinclude.canary"; }

# the third canary is not a hook: Acquire::gpgv::Options is read by the gpgv that apt runs (as _apt) for the signatures of the lists, and `--log-file=<file>` makes it write that file
# wherever it is told. It is an Acquire:: name, which an open Acquire:: prefix in the sandbox's grammar would let through whole. Tried on apt 2.2.4 in a Debian 11 chroot, with the sandbox's own four options
# and the fragment copied into the sandbox's apt.conf.d: the sandbox's `apt-get update` made /var/lib/apt/lists/partial/osp-canary in the BOX's lists (owner _apt), and nothing in the sandbox's;
# with the fragment left out, nothing. (A plain `apt-get update` of the box does not leave it: it cleans partial of what it does not know, which the sandbox's update does to its own
# lists and not to the box's. So this canary has no control of its own on the guest, as the two hooks have: the chroot run that is described here is its control.) The plan has to leave the fragment out and
# name it (not_allowed_setting) and the file must not be there when the plan is done
dirty_canarygpgv() {
    mkdir -p "${APT_DIR}/apt.conf.d"
    rm -f "${CANARY_GPGV_FILE}"
    printf '%s\n' 'Acquire::gpgv::Options::0 "--log-file=/var/lib/apt/lists/partial/osp-canary";' >"${APT_DIR}/apt.conf.d/99osp-canary-gpgv"
}
undo_canarygpgv() { rm -f "${APT_DIR}/apt.conf.d/99osp-canary-gpgv" "${CANARY_GPGV_FILE}"; }
undone_canarygpgv() { [ ! -e "${APT_DIR}/apt.conf.d/99osp-canary-gpgv" ] && [ ! -e "${CANARY_GPGV_FILE}" ]; }
after_canarygpgv() { if [ -e "${CANARY_GPGV_FILE}" ]; then echo present; else echo absent; fi >"${OUT}/step-canarygpgv.canary"; }

# step_end <id>: the undo, the check that it held, and a look for a sandbox directory left in /var/tmp. The undo is pending from before the dirt to after it, and the trap of
# the script runs it when the script dies in between (a failed undo stays pending, so that the trap tries it once more)
step_end() {
    local id="$1"
    in_subshell "undo_${id}" >"${OUT}/step-${id}.undo.log" 2>&1
    echo "${STATUS}" >"${OUT}/step-${id}.undo-rc"
    if [ "${STATUS}" -eq 0 ]; then PENDING_UNDO=""; fi
    in_subshell "undone_${id}" >>"${OUT}/step-${id}.undo.log" 2>&1
    if [ "${STATUS}" -eq 0 ]; then echo yes >"${OUT}/step-${id}.undone"; else echo no >"${OUT}/step-${id}.undone"; fi
    sandbox_dirs >"${OUT}/step-${id}.leftover"
    marker "step_${id}_done"
    log "step ${id}: dirty exit $(cat "${OUT}/step-${id}.dirty-rc"), undo exit $(cat "${OUT}/step-${id}.undo-rc"), undone $(cat "${OUT}/step-${id}.undone")"
}

# plan_step <id>: the dirt, POST (a job that begins is watched to its end, and the runbook is asked for), what the step looks at once the plan is done (after_<id>, when it has
# one), the undo, the check that it held
plan_step() {
    local id="$1"
    echo "${id}" >>"${OUT}/steps.list"
    marker "step_${id}"
    PENDING_UNDO="${id}"
    in_subshell "dirty_${id}" >"${OUT}/step-${id}.dirty.log" 2>&1
    echo "${STATUS}" >"${OUT}/step-${id}.dirty-rc"
    call_core "step-${id}-post" POST "${PLAN_PATH}" 20
    if [ "$(code_in "step-${id}-post")" = 202 ]; then
        plan_wait "step-${id}"
        call_core "step-${id}-runbook" GET "${RUNBOOK_PATH}" 30
        if declare -F "after_${id}" >/dev/null; then
            in_subshell "after_${id}" >"${OUT}/step-${id}.after.log" 2>&1
            echo "${STATUS}" >"${OUT}/step-${id}.after-rc"
        fi
    fi
    step_end "${id}"
}

# plan_hold: the plan with the hold (p3), and the runbook that a blocked plan does not have
plan_hold() {
    marker step_hold
    PENDING_UNDO=hold
    in_subshell dirty_hold >"${OUT}/step-hold.dirty.log" 2>&1
    echo "${STATUS}" >"${OUT}/step-hold.dirty-rc"
    plan_run p3
    call_core runbook-p3 GET "${RUNBOOK_PATH}" 30
    step_end hold
}

# ---- the run ------------------------------------------------------------------------------------------------------------------------------------------

main() {
    set -euo pipefail
    LEG="${1:-}"
    local facts
    [ "${LEG}" = d11 ] || { echo "usage: $0 d11" >&2; exit 2; }
    facts="$(leg_facts "${LEG}")" || { echo "usage: $0 d11" >&2; exit 2; }
    read -r FAMILY FROM_VERSION FROM_CODENAME TO_VERSION TO_CODENAME <<<"${facts}"
    [[ "${PLAN_DISPOSABLE:-}" == 1 ]] ||
        { echo "This installs Docker, holds dpkg's lock, mounts over /var/tmp, puts a package on hold and plans an upgrade: run it on a disposable machine (PLAN_DISPOSABLE=1)." >&2; exit 2; }
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
    OUT="${OUT:-${PWD}/plan-out}"
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
    # apt's own timers would run in the middle of the proof, and hold the lock the plan asks about
    systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service >/dev/null 2>&1 || true
    if grep -Eq '^[[:space:]]*deb(-src)?[[:space:]].*//(deb|security)\.debian\.org/debian-security' "${APT_DIR}/sources.list" 2>/dev/null; then
        # out of support: the change --use-debian-archive makes, made first because Docker's own installer runs apt before ReCasaOS's does
        cp -p "${APT_DIR}/sources.list" "${APT_DIR}/sources.list.osr.bak"
        sed -i -E 's#//(deb|security)\.debian\.org/debian-security#//archive.debian.org/debian-security#' "${APT_DIR}/sources.list"
    fi
    update_old
    "${APT[@]}" install -y -q curl jq ca-certificates python3 procps iproute2 >/dev/null
    write_manifest_tool "${LIBDIR}/manifest.py"
    write_lock_tools "${LIBDIR}"
    HOST_NAME="osp-$(head -c4 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    hostnamectl set-hostname "${HOST_NAME}"
    # sudo and the services look the name up: without a line for it every `sudo` of the run says "unable to resolve host"
    echo "127.0.1.1 ${HOST_NAME}" >>/etc/hosts
    install_docker
    install_casaos
    start_things
    "${APT[@]}" update -qq
    PK_AFTER_APT="$(unit_state packagekit.service)"
    quiet_packagekit || echo "packagekit.service was still there 30 s after it was stopped: it may leave in the window" >>"${OUT}/prepare-notes"
    wait_services 180 || abort "the gateway, the core and AppManagement do not all answer after the installation"
    timeout 120 systemctl is-system-running --wait >/dev/null 2>&1 || true
    box_facts
    leak_needles
    # AppManagement's check of the images lands in /var/lib/casaos three minutes after its start: it is over before the control looks at what changes by itself
    wait_image_check 240 || not_proven "AppManagement did not write ${IMAGE_UPDATES} within 240 s: its own check of the images (3 minutes after its start) could fall in the window, and be taken for a write of the plan"
    copy_sources
    copy_aptconf
    marker prepare_done
    # what the installation started has a few seconds to be quiet: the control below looks at what still changes by itself
    sleep 20

    window
    call_core runbook-p1 GET "${RUNBOOK_PATH}" 30
    runbook_shn runbook-p1
    plan_run p2
    plan_hold
    local id
    for id in "${PLAN_STEPS[@]}"; do plan_step "${id}"; done
    marker plan4_start
    plan_run p4
    date -u +%FT%TZ >"${OUT}/finished"
    log "done"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then main "$@"; fi
