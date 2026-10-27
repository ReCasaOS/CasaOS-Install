#!/usr/bin/env bash
# Proof of the software RAID repair of ReCasaOS on a real machine. A RAID 1 is made with mdadm on two blank virtio disks (test scaffolding, not something the
# product does), and LocalStorage is checked against it: its status (GET /v1/disks/raid), a failed disk, its removal (DELETE /v1/disks/raid/md0/disks/<member>),
# the refusals, a third disk added (POST /v1/disks/raid/md0/disks) and rebuilt onto, the data on the array, and a restart of the guest.
#
# It runs ON THE CI RUNNER (raid-proof.yml boots the guest), like stats-proof.sh: it drives the guest over ssh and calls LocalStorage through the gateway (QEMU
# forwards the runner's GW_PORT to the guest's port 80) with the token of a user of the box's own. The same file is copied to the guest, where
# `raid-proof.sh guest <verb>` does, as root, what only the guest can do. It prints one line per check, PASS <check> or FAIL <check>: <detail>, and ends on
# status 1 when a check failed. Its own notes go to standard error, prefixed with #.
#
# Needs, on the runner: bash, curl, jq, ssh, scp, timeout. Environment: INSTALL_SH (published/install.sh, the published installer, named install.sh), SSH_CONFIG
# (ssh_config, with a host named vm), GW_PORT (8080). The guest has three blank disks with the virtio serials rp1, rp2 and rp3.
# shellcheck disable=SC2016
# (the jq filters name their variables, $d1 $d2 $d3, in single quotes on purpose)
set -uo pipefail
export LC_ALL=C

# ---- in the guest, as root ---------------------------------------------------------------------------------------------------------------

guest() {
    local s p out=""
    export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1
    case "$1" in
    install)
        cloud-init status --wait >/dev/null 2>&1 || true
        # apt's own timers would take dpkg's lock in the middle of the run
        systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service >/dev/null 2>&1 || true
        bash install.sh --no-telemetry >install.log 2>&1 || { tail -n 40 install.log; return 1; } ;;
    disks) # the kernel's names of the three blank disks (by their serials), and of the disk that holds /
        for s in rp1 rp2 rp3; do
            p="$(readlink -e "/dev/disk/by-id/virtio-${s}")" || return 1
            out="${out}${p#/dev/} "
        done
        echo "${out}$(lsblk -ndo PKNAME "$(findmnt -no SOURCE /)")" ;;
    setup) # setup <disk1> <disk2>: the RAID 1, ext4 on /DATA/Mirror with a file of random bytes, the array named in mdadm.conf as an administrator would; prints the file's sha256
        {
            apt-get -o DPkg::Lock::Timeout=300 update -qq &&
                apt-get -o DPkg::Lock::Timeout=300 install -y -q --no-install-recommends mdadm &&
                mdadm --create /dev/md0 --level=1 --raid-devices=2 --run "/dev/$2" "/dev/$3" </dev/null || return 1
            mdadm --wait /dev/md0 # status 1 when the resync was already over
            [ "$(cat /sys/block/md0/md/sync_action)" = idle ] &&
                mkfs.ext4 -q /dev/md0 && mkdir -p /DATA/Mirror &&
                echo "UUID=$(blkid -s UUID -o value /dev/md0) /DATA/Mirror ext4 defaults,nofail 0 2" >>/etc/fstab &&
                mount /DATA/Mirror && head -c 8M /dev/urandom >/DATA/Mirror/proof.bin && sync &&
                mdadm --detail --scan >>/etc/mdadm/mdadm.conf && update-initramfs -u || return 1
        } >&2
        sha256sum /DATA/Mirror/proof.bin | cut -d' ' -f1 ;;
    fail) mdadm /dev/md0 --fail "/dev/$2" ;;
    unplug) mdadm --zero-superblock "/dev/$2" ;; # what pulling the failed disk out amounts to: its old RAID header cannot come back at the next boot
    mdstat) cat /proc/mdstat ;;
    sum) # the file's sha256, read from the disks and not from the page cache
        sync && echo 3 >/proc/sys/vm/drop_caches
        mountpoint -q /DATA/Mirror || { echo "/DATA/Mirror was not mounted at boot: mounting it" >&2; mount /DATA/Mirror; } || return 1
        sha256sum /DATA/Mirror/proof.bin | cut -d' ' -f1 ;;
    boot_id) cat /proc/sys/kernel/random/boot_id ;;
    reboot) nohup sh -c 'sleep 2; systemctl reboot' >/dev/null 2>&1 </dev/null & echo scheduled ;;
    *) echo "unknown verb: $1" >&2; return 2 ;;
    esac
}

# ---- on the runner -----------------------------------------------------------------------------------------------------------------------

SSH_CONFIG="${SSH_CONFIG:-ssh_config}"
GW="http://127.0.0.1:${GW_PORT:-8080}"
PROOF_USER=raidproof
# the array is whole: two active members and nothing missing
OK='.state == "ok" and .level == "raid1" and .disks == 2 and .missing == 0 and .component_size > 0 and (.members | length) == 2 and all(.members[]; .state == "active")'
FAILS=0 TOKEN="" PASSWORD="" CODE=000 WORK="" D1="" D2="" D3="" SYS=""

log() { printf '# %s\n' "$*" >&2; }
pass() { echo "PASS $1"; }
fail() { echo "FAIL $1: $2"; FAILS=$((FAILS + 1)); }
# die <check> <detail>: a step the proof cannot go on without
die() { fail "$1" "$2"; exit 1; }

# g <verb> [words]: a verb of this script on the guest, as root (a hung guest costs LIMIT seconds, not the job)
g() { timeout -k 5 "${LIMIT:-300}" ssh -n -F "${SSH_CONFIG}" -o ConnectTimeout=8 -o BatchMode=yes vm "sudo bash raid-proof.sh guest $*"; }

# login: the access token of the box's user, the way stats-proof.sh gets it (POST /v1/users/login, the token as the Authorization header)
login() {
    TOKEN="$(curl -sS --max-time 30 -X POST "${GW}/v1/users/login" -H 'Content-Type: application/json' \
        -d "$(printf '{"username":"%s","password":"%s"}' "${PROOF_USER}" "${PASSWORD}")" 2>/dev/null | jq -r '.data.token.access_token // empty' 2>/dev/null)"
    [ -n "${TOKEN}" ]
}

# api <METHOD> <path> [JSON body]: one call through the gateway with the token. CODE has the HTTP status (000 for none), $WORK/body.json the body. A 401 (the
# token ran out, the guest restarted) logs in again and asks once more.
api() {
    local try opts
    for try in 1 2; do
        opts=(-sS --max-time 60 -X "$1" -H "Authorization: ${TOKEN}" -o "${WORK}/body.json" -w '%{http_code}')
        [ -z "${3:-}" ] || opts+=(-H 'Content-Type: application/json' --data-binary "$3")
        : >"${WORK}/body.json"
        CODE="$(curl "${opts[@]}" "${GW}$2" 2>/dev/null)" || CODE=000
        { [ "${CODE}" = 401 ] && [ "${try}" = 1 ] && login; } || break
    done
}
body() { head -c 300 "${WORK}/body.json"; }

# raid: GET /v1/disks/raid, and the proof's array into $WORK/md.json: md0, or the array that has disk1 as a member (with no mdadm.conf entry a restart names it md127)
raid() {
    : >"${WORK}/md.json"
    api GET /v1/disks/raid
    [ "${CODE}" = 200 ] &&
        jq -c --arg d1 "${D1}" '[.data[] | select(.name == "md0" or any(.members[]?; .disk == $d1))][0] // empty' "${WORK}/body.json" >"${WORK}/md.json" 2>/dev/null &&
        [ -s "${WORK}/md.json" ]
}
# holds <jq test>: the test on the array, with the disks' names as $d1 $d2 $d3
holds() { jq -e --arg d1 "${D1}" --arg d2 "${D2}" --arg d3 "${D3}" "$1" "${WORK}/md.json" >/dev/null 2>&1; }
seen() {
    if [ -s "${WORK}/md.json" ]; then jq -c '{name, state, level, disks, missing, component_size, action, members}' "${WORK}/md.json"; else echo "GET /v1/disks/raid answered ${CODE}: $(body)"; fi
}

# check <check> <seconds> <jq test>: GET every 3 s until the array passes the test (PASS), or the time is out (FAIL, with what was seen last)
check() {
    local end=$((SECONDS + $2))
    until raid && holds "$3"; do
        if [ "${SECONDS}" -ge "${end}" ]; then fail "$1" "$(seen)"; return 1; fi
        sleep 3
    done
    pass "$1"
}
code_is() { if [ "${CODE}" = "$2" ]; then pass "$1"; else fail "$1" "HTTP ${CODE}, ${2} expected: $(body)"; fi; }

# state: the array as LocalStorage and as the kernel see it
state() { raid; jq -c '{name, state, disks, missing, members}' "${WORK}/md.json" 2>/dev/null; g mdstat | tr '\n' ' '; }
# refuse <check> <METHOD> <path> [body]: the call answers 4xx and leaves the array as it was
refuse() {
    local before after code b
    before="$(state)"
    api "$2" "$3" "${4:-}"
    code="${CODE}" b="$(body)"
    after="$(state)"
    case "${code}" in
    4??) if [ "${before}" = "${after}" ]; then pass "$1"; else fail "$1" "HTTP ${code}, but the array changed: ${before} -> ${after}"; fi ;;
    *) fail "$1" "HTTP ${code}, a 4xx was expected: ${b}" ;;
    esac
}
add_body() { printf '{"path":"/dev/%s","confirm":"%s"}' "$1" "$2"; }
same_data() { local now; now="$(g sum)"; if [ "${now}" = "$2" ]; then pass "$1"; else fail "$1" "sha256 ${now:-none}, ${2} was written"; fi; }
member_of() { jq -r --arg d "$1" '.members[]? | select(.disk == $d) | .name' "${WORK}/md.json" 2>/dev/null; }

run() {
    local key sum m1 m2 old id end
    WORK="$(mktemp -d)"
    PASSWORD="Aa1-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"

    log "the published installer and this script go to the guest, and ReCasaOS is installed"
    scp -q -F "${SSH_CONFIG}" "${INSTALL_SH:-published/install.sh}" "${BASH_SOURCE[0]}" vm: || die install "the files could not be copied to the guest"
    LIMIT=3600 g install >&2 || die install "the installer failed (the end of its log is above)"
    end=$((SECONDS + 300))
    until key="$(curl -sS --max-time 10 "${GW}/v1/users/status" 2>/dev/null | jq -r '.data.key // empty' 2>/dev/null)" && [ -n "${key}" ]; do
        [ "${SECONDS}" -lt "${end}" ] || die install "no registration key through the gateway (port ${GW_PORT:-8080}) within 300 s"
        sleep 5
    done
    curl -sS --max-time 30 -X POST "${GW}/v1/users/register" -H 'Content-Type: application/json' \
        -d "$(printf '{"username":"%s","password":"%s","key":"%s"}' "${PROOF_USER}" "${PASSWORD}" "${key}")" >/dev/null
    login || die login "the box's user could not log in"

    read -r D1 D2 D3 SYS <<<"$(g disks)"
    [ -n "${SYS}" ] || die disks "the blank disks (serials rp1 rp2 rp3) or the system disk were not found: '${D1} ${D2} ${D3}'"
    log "disk1 ${D1}, disk2 ${D2}, disk3 ${D3}, the system disk ${SYS}"

    log "1. a RAID 1 of ${D1} and ${D2}, ext4 on /DATA/Mirror, a file of known content"
    sum="$(LIMIT=900 g setup "${D1}" "${D2}" | tail -n 1)" && [ -n "${sum}" ] || die setup "the RAID 1 could not be made (mdadm's words are above)"

    log "2. its status"
    check status-ok 60 "${OK}"

    log "3. ${D2} fails"
    g fail "${D2}" >&2 || die fail-disk "mdadm could not fail ${D2}"
    check status-degraded-faulty 30 '.state == "degraded" and any(.members[]; .disk == $d2 and .state == "faulty")'

    log "4. the failed member is taken out"
    m2="$(member_of "${D2}")"
    api DELETE "/v1/disks/raid/md0/disks/${m2:-${D2}}"
    code_is remove-faulty 200
    check status-after-remove 30 '.state == "degraded" and .missing == 1 and all(.members[]; .state != "faulty" and .disk != $d2)'
    g unplug "${D2}" >&2 || log "the RAID header of ${D2} could not be wiped: the restart may see it"

    log "5. the refusals"
    m1="$(member_of "${D1}")"
    refuse refuse-wrong-confirm POST /v1/disks/raid/md0/disks "$(add_body "${D3}" "not-${D3}")"
    refuse refuse-system-disk POST /v1/disks/raid/md0/disks "$(add_body "${SYS}" "${SYS}")"
    refuse refuse-active-member POST /v1/disks/raid/md0/disks "$(add_body "${D1}" "${D1}")"
    refuse refuse-remove-active DELETE "/v1/disks/raid/md0/disks/${m1:-${D1}}"

    log "6. ${D3} is added, and the array rebuilds onto it"
    api POST /v1/disks/raid/md0/disks "$(add_body "${D3}" "${D3}")"
    code_is add-disk 200
    check rebuilt 300 "${OK}"' and any(.members[]; .disk == $d3)'

    log "7. the data"
    same_data data-after-rebuild "${sum}"

    log "8. the guest restarts"
    old="$(g boot_id)"
    [ -n "${old}" ] || die reboot "the guest gave no boot id"
    g reboot >&2
    end=$((SECONDS + 300))
    until id="$(LIMIT=20 g boot_id 2>/dev/null)" && [ -n "${id}" ] && [ "${id}" != "${old}" ]; do
        [ "${SECONDS}" -lt "${end}" ] || die reboot "the guest did not come back within 300 s"
        sleep 5
    done
    # the kernel may name the disks otherwise after a restart
    read -r D1 D2 D3 SYS <<<"$(g disks)"
    check reboot-assembled 300 "${OK}"' and any(.members[]; .disk == $d1) and any(.members[]; .disk == $d3)'
    [ "$(jq -r .name "${WORK}/md.json" 2>/dev/null)" = md0 ] || log "the array came back as $(jq -r .name "${WORK}/md.json" 2>/dev/null): accepted by its members"
    same_data data-after-reboot "${sum}"

    log "${FAILS} check(s) failed"
    [ "${FAILS}" -eq 0 ]
}

if [ "${1:-}" = guest ]; then
    shift
    guest "$@"
else
    run
fi
