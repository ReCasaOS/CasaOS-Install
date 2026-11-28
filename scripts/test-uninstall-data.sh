#!/bin/bash
#
# Checks what casaos-uninstall does about the storage merge on /DATA, without
# unmounting or removing anything: Unmount_Data is copied out of casaos-uninstall
# as it stands and run here against a stand-in for /proc/mounts and for umount.
#
#   bash scripts/test-uninstall-data.sh
#
# UNINSTALL_SH_UNDER_TEST=<path> checks another copy of casaos-uninstall (a
# mutant, to see that each check below can say no). The release workflow runs
# this before anything is built.

set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

pass() { echo "ok: $*"; }

# the script as the shell reads it: a Windows checkout has CRLF line endings
UNINSTALL_SH="${WORK}/casaos-uninstall"
tr -d '\r' <"${UNINSTALL_SH_UNDER_TEST:-${ROOT}/casaos-uninstall}" >"${UNINSTALL_SH}"
bash -n "${UNINSTALL_SH}" || fail "casaos-uninstall does not parse"

sed -n '/^Unmount_Data() {$/,/^}$/p' "${UNINSTALL_SH}" >"${WORK}/unmount.sh"
[ -s "${WORK}/unmount.sh" ] || fail "casaos-uninstall has no Unmount_Data"
# shellcheck disable=SC1091
. "${WORK}/unmount.sh"

sudo_cmd=""
Show() { echo "$1 $2" >>"${WORK}/shown"; }

export CASA_PROC_MOUNTS="${WORK}/mounts"
BUSY=""  # mount points a plain umount refuses
STUCK="" # mount points no umount takes off

# the stand-in: records the call, and takes the line off the mounts file as the kernel would
umount() {
    local lazy=false point
    echo "umount $*" >>"${WORK}/calls"
    if [ "$1" = "-l" ]; then
        lazy=true
        shift
    fi
    point="$1"
    case " ${STUCK} " in *" ${point} "*) return 1 ;; esac
    if [ "${lazy}" = false ]; then
        case " ${BUSY} " in *" ${point} "*) return 1 ;; esac
    fi
    awk -v point="${point}" '$2 != point' "${CASA_PROC_MOUNTS}" >"${WORK}/mounts.new"
    mv "${WORK}/mounts.new" "${CASA_PROC_MOUNTS}"
}

run() {
    : >"${WORK}/calls"
    : >"${WORK}/shown"
    status=0
    Unmount_Data || status=$?
}

calls() { tr '\n' ';' <"${WORK}/calls"; }

BASE='/dev/sda1 / ext4 rw 0 0
/dev/sdc1 /DATA/USB_Storage_stick ext4 rw 0 0'
MERGE='1:2 /DATA fuse.mergerfs rw 0 0
/dev/sda1 /DATA/AppData ext4 rw 0 0'

# ---- no merge: nothing is unmounted --------------------------------------------

printf '%s\n' "${BASE}" >"${CASA_PROC_MOUNTS}"
run
[ "${status}" -eq 0 ] && [ -z "$(calls)" ] || fail "with no merge on /DATA: status ${status}, calls $(calls)"

printf '%s\n/dev/sdb1 /DATA ext4 rw 0 0\n' "${BASE}" >"${CASA_PROC_MOUNTS}"
run
[ "${status}" -eq 0 ] && [ -z "$(calls)" ] || fail "a disk mounted on /DATA is not a merge: status ${status}, calls $(calls)"
pass "a /DATA that is no merge is left alone"

# ---- the merge and its AppData folder, deepest first, and nothing else ---------

printf '%s\n%s\n' "${BASE}" "${MERGE}" >"${CASA_PROC_MOUNTS}"
BUSY="" STUCK=""
run
[ "${status}" -eq 0 ] || fail "the merge was unmounted and the answer is ${status}"
[ "$(calls)" = "umount /DATA/AppData;umount /DATA;" ] || fail "unmounted: $(calls)"
grep -q ' /DATA/USB_Storage_stick ' "${CASA_PROC_MOUNTS}" || fail "a drive mounted under /DATA was unmounted"
pass "the AppData folder, then the merge, are unmounted; a drive under /DATA is not"

# ---- in use: detached -----------------------------------------------------------

printf '%s\n%s\n' "${BASE}" "${MERGE}" >"${CASA_PROC_MOUNTS}"
BUSY="/DATA" STUCK=""
run
[ "${status}" -eq 0 ] || fail "a merge in use was detached and the answer is ${status}"
[ "$(calls)" = "umount /DATA/AppData;umount /DATA;umount -l /DATA;" ] || fail "with /DATA in use: $(calls)"
pass "a merge that is in use is detached"

# ---- still there: said, and answered -------------------------------------------

printf '%s\n%s\n' "${BASE}" "${MERGE}" >"${CASA_PROC_MOUNTS}"
BUSY="" STUCK="/DATA"
run
[ "${status}" -ne 0 ] || fail "the merge is still mounted and the answer is 0"
grep -q '^3 /DATA could not be unmounted' "${WORK}/shown" || fail "a merge that stays is not said"
pass "a merge that cannot be unmounted is reported"

# ---- what the uninstall does with the answer -----------------------------------

n_stop="$(grep -n 'systemctl stop "${SERVICE}"' "${UNINSTALL_SH}" | head -n 1 | cut -d: -f1)"
n_unmount="$(grep -n '^    Unmount_Data || DATA_UNMOUNTED=false$' "${UNINSTALL_SH}" | cut -d: -f1)"
n_rm="$(grep -n 'rm -fr /DATA/AppData' "${UNINSTALL_SH}" | cut -d: -f1)"
[ -n "${n_stop}" ] && [ -n "${n_unmount}" ] && [ -n "${n_rm}" ] && [ "${n_stop}" -lt "${n_unmount}" ] && [ "${n_unmount}" -lt "${n_rm}" ] ||
    fail "the order is not: stop the services, unmount /DATA, delete AppData"
[ "$(grep -c 'rm -fr /DATA/AppData' "${UNINSTALL_SH}")" -eq 1 ] || fail "AppData is deleted in more than one place"
[ "$(sed -n "$((n_rm - 1))p" "${UNINSTALL_SH}")" = '        if [[ ${DATA_UNMOUNTED} = true ]]; then' ] ||
    fail "AppData is deleted without asking whether the merge is off /DATA"
pass "AppData is deleted only once the merge is off /DATA"
