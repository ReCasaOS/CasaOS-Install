#!/usr/bin/env bash
# Proof of the software RAID repair and creation of ReCasaOS on a real machine. A RAID 1 is made with mdadm on two blank virtio disks (test scaffolding, not something the
# product does), and LocalStorage is checked against it: its status (GET /v1/disks/raid), a failed disk, its removal (DELETE /v1/disks/raid/md1/disks/<member>),
# the refusals, a third disk added (POST /v1/disks/raid/md1/disks) and rebuilt onto, the data on the array, then a second array MADE BY THE PRODUCT (POST /v1/disks/raid)
# from two other blank disks, its refusals, its filesystem and mount, a RAID 0 made the same way from two more (refused without the consent that one failed disk loses
# everything on it), and a restart of the guest after which all three arrays and their data must be back.
#
# It runs ON THE CI RUNNER (raid-proof.yml boots the guest), like stats-proof.sh: it drives the guest over ssh and calls LocalStorage through the gateway (QEMU
# forwards the runner's GW_PORT to the guest's port 80) with the token of a user of the box's own. The same file is copied to the guest, where
# `raid-proof.sh guest <verb>` does, as root, what only the guest can do. It prints one line per check, PASS <check> or FAIL <check>: <detail>, and ends on
# status 1 when a check failed. Its own notes go to standard error, prefixed with #.
#
# Needs, on the runner: bash, curl, jq, ssh, scp, timeout. Environment: INSTALL_SH (published/install.sh, the published installer, named install.sh), SSH_CONFIG
# (ssh_config, with a host named vm), GW_PORT (8080). The guest has seven blank disks with the virtio serials rp1 to rp7.
# shellcheck disable=SC2016
# (the jq filters name their variables, $d1 to $d7, in single quotes on purpose)
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
    disks) # the kernel's names of the seven blank disks (by their serials), and of the disk that holds /
        for s in rp1 rp2 rp3 rp4 rp5 rp6 rp7; do
            p="$(readlink -e "/dev/disk/by-id/virtio-${s}")" || return 1
            out="${out}${p#/dev/} "
        done
        echo "${out}$(lsblk -ndo PKNAME "$(findmnt -no SOURCE /)")" ;;
    setup) # setup <disk1> <disk2>: the RAID 1, ext4 on /DATA/Mirror with a file of random bytes, the array named in mdadm.conf as an administrator would; prints the file's sha256
        {
            apt-get -o DPkg::Lock::Timeout=300 update -qq &&
                apt-get -o DPkg::Lock::Timeout=300 install -y -q --no-install-recommends mdadm &&
                mdadm --create /dev/md1 --level=1 --raid-devices=2 --run "/dev/$2" "/dev/$3" </dev/null || return 1
            mdadm --wait /dev/md1 # status 1 when the resync was already over
            [ "$(cat /sys/block/md1/md/sync_action)" = idle ] &&
                mkfs.ext4 -q /dev/md1 && mkdir -p /DATA/Mirror &&
                echo "UUID=$(blkid -s UUID -o value /dev/md1) /DATA/Mirror ext4 defaults,nofail 0 2" >>/etc/fstab &&
                mount /DATA/Mirror && head -c 8M /dev/urandom >/DATA/Mirror/proof.bin && sync &&
                mdadm --detail --scan >>/etc/mdadm/mdadm.conf && update-initramfs -u || return 1
        } >&2
        sha256sum /DATA/Mirror/proof.bin | cut -d' ' -f1 ;;
    fail) mdadm /dev/md1 --fail "/dev/$2" ;;
    unplug) mdadm --zero-superblock "/dev/$2" ;; # what pulling the failed disk out amounts to: its old RAID header cannot come back at the next boot
    mdstat) cat /proc/mdstat ;;
    sum) # the file's sha256, read from the disks and not from the page cache
        sync && echo 3 >/proc/sys/vm/drop_caches
        mountpoint -q /DATA/Mirror || { echo "/DATA/Mirror was not mounted at boot: mounting it" >&2; mount /DATA/Mirror; } || return 1
        sha256sum /DATA/Mirror/proof.bin | cut -d' ' -f1 ;;
    has_mdadm) command -v mdadm ;;
    blank) # blank <disk>...: none of them carries a signature (a refused call wiped nothing)
        for s in "${@:2}"; do ! blkid -p "/dev/$s" >/dev/null 2>&1 || { echo "/dev/$s carries a signature: $(blkid -p "/dev/$s")" >&2; return 1; }; done ;;
    made_write) # made_write <storage>: a file of random bytes on the storage the product made; prints its sha256
        p="$(findmnt -rno TARGET | grep -m1 "^/media/$2")" || return 1
        head -c 4M /dev/urandom >"${p}/proof.bin" && sync && sha256sum "${p}/proof.bin" | cut -d' ' -f1 ;;
    made_mount) findmnt -rno SOURCE,FSTYPE,TARGET | grep -m1 " /media/$2" ;;
    made_sum) # made_sum <storage>: the file's sha256, read from the disks; the product mounts its storages when it starts, so it is given 2 minutes to have done it
        for s in $(seq 1 40); do p="$(findmnt -rno TARGET | grep -m1 "^/media/$2")" && break; sleep 3; done
        [ -n "${p:-}" ] || return 1
        sync && echo 3 >/proc/sys/vm/drop_caches
        sha256sum "${p}/proof.bin" | cut -d' ' -f1 ;;
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
FAILS=0 TOKEN="" PASSWORD="" CODE=000 WORK="" D1="" D2="" D3="" D4="" D5="" D6="" D7="" SYS=""
# the array raid() looks for: the one that has PICK_DISK as a member (the proof's mirror, which has disk1, to begin with)
PICK_DISK=""

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

# raid: GET /v1/disks/raid, and the array looked for into $WORK/md.json: the one having PICK_DISK as a member (with no mdadm.conf entry a restart names it md127)
raid() {
    : >"${WORK}/md.json"
    api GET /v1/disks/raid
    [ "${CODE}" = 200 ] &&
        jq -c --arg d "${PICK_DISK:-${D1}}" '[.data[] | select(any(.members[]?; .disk == $d))][0] // empty' "${WORK}/body.json" >"${WORK}/md.json" 2>/dev/null &&
        [ -s "${WORK}/md.json" ]
}
# holds <jq test>: the test on the array, with the disks' names as $d1 to $d7
holds() { jq -e --arg d1 "${D1}" --arg d2 "${D2}" --arg d3 "${D3}" --arg d4 "${D4}" --arg d5 "${D5}" --arg d6 "${D6}" --arg d7 "${D7}" "$1" "${WORK}/md.json" >/dev/null 2>&1; }
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
# create_body <name> <level> <JSON list of disk paths> <confirm> [true: the loss of everything on a failed disk is accepted, which a RAID 0 asks for]
create_body() { printf '{"name":"%s","level":"%s","disks":%s,"confirm":"%s"%s}' "$1" "$2" "$3" "$4" "${5:+,\"accept_data_loss\":true}"; }
same_data() { local now; now="$(g sum)"; if [ "${now}" = "$2" ]; then pass "$1"; else fail "$1" "sha256 ${now:-none}, ${2} was written"; fi; }
member_of() { jq -r --arg d "$1" '.members[]? | select(.disk == $d) | .name' "${WORK}/md.json" 2>/dev/null; }

run() {
    local key sum msum fsum made fast now both pair had m1 m2 old id end
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

    read -r D1 D2 D3 D4 D5 D6 D7 SYS <<<"$(g disks)"
    [ -n "${SYS}" ] || die disks "the blank disks (serials rp1 to rp7) or the system disk were not found: '${D1} ${D2} ${D3} ${D4} ${D5} ${D6} ${D7}'"
    log "disk1 ${D1}, disk2 ${D2}, disk3 ${D3}, disk4 ${D4}, disk5 ${D5}, disk6 ${D6}, disk7 ${D7}, the system disk ${SYS}"

    log "0. a RAID 1 of ${D4} and ${D5}, made by the product before anything installs mdadm; what it refuses first"
    if g has_mdadm >/dev/null 2>&1; then had=1; log "mdadm is already on this image: the product's installation of it is not exercised on this leg"; else had=0; fi
    both="[\"/dev/${D4}\",\"/dev/${D5}\"]"
    refuse refuse-create-wrong-confirm POST /v1/disks/raid "$(create_body Made raid1 "${both}" Mad)"
    refuse refuse-create-raid0-without-consent POST /v1/disks/raid "$(create_body Made raid0 "${both}" Made)"
    refuse refuse-create-linear POST /v1/disks/raid "$(create_body Made linear "${both}" Made true)"
    refuse refuse-create-too-few POST /v1/disks/raid "$(create_body Made raid5 "${both}" Made)"
    refuse refuse-create-system-disk POST /v1/disks/raid "$(create_body Made raid1 "[\"/dev/${D4}\",\"/dev/${SYS}\"]" Made)"
    refuse refuse-create-bad-name POST /v1/disks/raid "$(create_body 'Made;' raid1 "${both}" 'Made;')"
    if [ "${had}" = 0 ]; then if g has_mdadm >/dev/null 2>&1; then fail refusals-installed-nothing "a refused call installed mdadm"; else pass refusals-installed-nothing; fi; fi
    if g blank "${D4}" "${D5}" >&2; then pass refusals-wiped-nothing; else fail refusals-wiped-nothing "a refused call touched ${D4} or ${D5}"; fi
    api POST /v1/disks/raid "$(create_body Made raid1 "${both}" Made)"
    code_is create-array 200
    if [ "${had}" = 0 ]; then if g has_mdadm >/dev/null 2>&1; then pass mdadm-installed-by-product; else fail mdadm-installed-by-product "the array was made but mdadm is not on the guest: $(body)"; fi; fi
    PICK_DISK="${D4}"
    check created-array 120 '.level == "raid1" and .disks == 2 and .missing == 0 and (.state == "ok" or .state == "resyncing") and any(.members[]; .disk == $d4) and any(.members[]; .disk == $d5)'
    if made="$(g made_mount Made)" && [[ "${made}" =~ ^/dev/md[0-9]+\ ext4\ /media/Made ]]; then pass created-array-mounted; else fail created-array-mounted "findmnt: '${made:-nothing}'"; fi
    msum="$(g made_write Made)" && [ -n "${msum}" ] || fail created-array-written "no file could be written on /media/Made"
    api GET /v1/storage
    if [ "${CODE}" = 200 ] && jq -e '[.. | objects | select((.mount_point? // "") | tostring | startswith("/media/Made"))] | length > 0' "${WORK}/body.json" >/dev/null 2>&1; then pass created-array-listed; else fail created-array-listed "HTTP ${CODE}: $(body)"; fi
    PICK_DISK=""

    log "1. a RAID 1 of ${D1} and ${D2} (the first free name, md0, is the product's array: this one is md1), ext4 on /DATA/Mirror, a file of known content"
    sum="$(LIMIT=900 g setup "${D1}" "${D2}" | tail -n 1)" && [ -n "${sum}" ] || die setup "the RAID 1 could not be made (mdadm's words are above)"

    log "2. its status"
    check status-ok 60 "${OK}"

    log "3. ${D2} fails"
    g fail "${D2}" >&2 || die fail-disk "mdadm could not fail ${D2}"
    check status-degraded-faulty 30 '.state == "degraded" and any(.members[]; .disk == $d2 and .state == "faulty")'

    log "4. the failed member is taken out"
    m2="$(member_of "${D2}")"
    api DELETE "/v1/disks/raid/md1/disks/${m2:-${D2}}"
    code_is remove-faulty 200
    check status-after-remove 30 '.state == "degraded" and .missing == 1 and all(.members[]; .state != "faulty" and .disk != $d2)'
    g unplug "${D2}" >&2 || log "the RAID header of ${D2} could not be wiped: the restart may see it"

    log "5. the refusals"
    m1="$(member_of "${D1}")"
    refuse refuse-wrong-confirm POST /v1/disks/raid/md1/disks "$(add_body "${D3}" "not-${D3}")"
    refuse refuse-system-disk POST /v1/disks/raid/md1/disks "$(add_body "${SYS}" "${SYS}")"
    refuse refuse-active-member POST /v1/disks/raid/md1/disks "$(add_body "${D1}" "${D1}")"
    refuse refuse-create-active-member POST /v1/disks/raid "$(create_body Made raid1 "[\"/dev/${D3}\",\"/dev/${D1}\"]" Made)"
    refuse refuse-remove-active DELETE "/v1/disks/raid/md1/disks/${m1:-${D1}}"

    log "6. ${D3} is added, and the array rebuilds onto it"
    api POST /v1/disks/raid/md1/disks "$(add_body "${D3}" "${D3}")"
    code_is add-disk 200
    check rebuilt 300 "${OK}"' and any(.members[]; .disk == $d3)'

    log "7. the data"
    same_data data-after-rebuild "${sum}"

    log "7b. a RAID 0 of ${D6} and ${D7}, made by the product: refused without the consent, made with it (md0 and md1 are taken, this one is md2)"
    pair="[\"/dev/${D6}\",\"/dev/${D7}\"]"
    refuse refuse-raid0-without-consent POST /v1/disks/raid "$(create_body Fast raid0 "${pair}" Fast)"
    refuse refuse-raid0-consent-for-one-disk POST /v1/disks/raid "$(create_body Fast raid0 "[\"/dev/${D6}\"]" Fast true)"
    if g blank "${D6}" "${D7}" >&2; then pass raid0-refusals-wiped-nothing; else fail raid0-refusals-wiped-nothing "a refused call touched ${D6} or ${D7}"; fi
    api POST /v1/disks/raid "$(create_body Fast raid0 "${pair}" Fast true)"
    code_is create-raid0 200
    PICK_DISK="${D6}"
    check created-raid0 120 '.level == "raid0" and .disks == 2 and .missing == 0 and .state == "ok" and any(.members[]; .disk == $d6) and any(.members[]; .disk == $d7)'
    if fast="$(g made_mount Fast)" && [[ "${fast}" =~ ^/dev/md[0-9]+\ ext4\ /media/Fast ]]; then pass created-raid0-mounted; else fail created-raid0-mounted "findmnt: '${fast:-nothing}'"; fi
    fsum="$(g made_write Fast)" && [ -n "${fsum}" ] || fail created-raid0-written "no file could be written on /media/Fast"
    PICK_DISK=""

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
    read -r D1 D2 D3 D4 D5 D6 D7 SYS <<<"$(g disks)"
    check reboot-assembled 300 "${OK}"' and any(.members[]; .disk == $d1) and any(.members[]; .disk == $d3)'
    [ "$(jq -r .name "${WORK}/md.json" 2>/dev/null)" = md1 ] || log "the array came back as $(jq -r .name "${WORK}/md.json" 2>/dev/null): accepted by its members"
    same_data data-after-reboot "${sum}"
    # the storage the product made is assembled and mounted again by itself, with its file
    PICK_DISK="${D4}"
    check made-reassembled 300 '.level == "raid1" and .disks == 2 and .missing == 0 and any(.members[]; .disk == $d4) and any(.members[]; .disk == $d5)'
    now="$(g made_sum Made)"
    if [ -n "${msum:-}" ] && [ "${now}" = "${msum}" ]; then pass made-data-after-reboot; else fail made-data-after-reboot "sha256 ${now:-none} (the storage was not mounted again within 2 minutes?), ${msum:-none} was written"; fi
    # the RAID 0 too: no redundancy to rebuild from, so it comes back whole or not at all
    PICK_DISK="${D6}"
    check raid0-reassembled 300 '.level == "raid0" and .disks == 2 and .missing == 0 and .state == "ok" and any(.members[]; .disk == $d6) and any(.members[]; .disk == $d7)'
    now="$(g made_sum Fast)"
    if [ -n "${fsum:-}" ] && [ "${now}" = "${fsum}" ]; then pass raid0-data-after-reboot; else fail raid0-data-after-reboot "sha256 ${now:-none} (the storage was not mounted again within 2 minutes?), ${fsum:-none} was written"; fi

    log "${FAILS} check(s) failed"
    [ "${FAILS}" -eq 0 ]
}

if [ "${1:-}" = guest ]; then
    shift
    guest "$@"
else
    run
fi
