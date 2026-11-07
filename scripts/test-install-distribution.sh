#!/usr/bin/env bash
# shellcheck disable=SC2016 # install.sh's text is grepped literally
#
# Checks what install.sh does on Debian and Ubuntu derivatives (Linux Mint, LMDE,
# Pop!_OS, Zorin, elementary), without installing anything: the functions that
# answer are copied out of install.sh as it stands and run for a few os-release
# files against a temporary directory as PREFIX, with apt-get, curl and dpkg
# replaced by recorders.
#
#   bash scripts/test-install-distribution.sh
#
# The release workflow runs this before anything is built.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# install.sh as bash reads it: a Windows checkout has CRLF line endings.
INSTALL_SH="${WORK}/install.sh"
tr -d '\r' <"${ROOT}/install.sh" >"${INSTALL_SH}"
bash -n "${INSTALL_SH}" || fail "install.sh does not parse"

for f in Docker_Base_Repo Install_Docker_From_Repo Install_Docker Distribution_Variant Check_Distribution Previous_Release; do
    eval "$(sed -n "/^${f}() {\$/,/^}\$/p" "${INSTALL_SH}")"
    declare -F "${f}" >/dev/null || fail "install.sh defines no ${f}()"
done

# What those functions take from install.sh: its Show, which ends the install on 1,
# and its globals; apt-get, curl and dpkg are recorders whose answers can be set.
sudo_cmd=""
REGION=""
CALLS="${WORK}/calls.log"
curl_status=0
dpkg_arch=""
ColorReset() { :; }
GreyStart() { :; }
Show() {
    echo "[$1] $2"
    if (($1 == 1)); then exit 1; fi
}
Check_Docker_Install_Final() { echo "Check_Docker_Install_Final" >>"${CALLS}"; }
apt-get() { echo "apt-get $*" >>"${CALLS}"; }
dpkg() { echo "${dpkg_arch:-amd64}"; }
curl() {
    echo "curl $*" >>"${CALLS}"
    case "$*" in
    *get.docker.com*) return "${get_docker_status:-0}" ;;
    esac
    if ((curl_status != 0)); then return "${curl_status}"; fi
    case "$*" in
    */gpg*) echo KEY >"${!#}" ;;
    esac
}

# A case runs in a subshell with a bare os-release and a fresh PREFIX.
fresh() {
    unset ID ID_LIKE UBUNTU_CODENAME DEBIAN_CODENAME VERSION_ID PRETTY_NAME ALLOW_PROXMOX_HOST
    PREFIX="$(mktemp -d -p "${WORK}")"
    CASA_STATE_DIR="${PREFIX}/var/lib/casaos"
    curl_status=0
    get_docker_status=0
    dpkg_arch=""
    : >"${CALLS}"
}

expect_base() { # <case> <expected output>; os-release variables already set
    local got
    got="$(Docker_Base_Repo)" || fail "$1: Docker_Base_Repo returned $?"
    [[ "${got}" == "$2" ]] || fail "$1: Docker_Base_Repo printed '${got}', expected '$2'"
}

(
    fresh
    ID=linuxmint ID_LIKE="ubuntu debian" VERSION_ID=22.3 UBUNTU_CODENAME=noble
    expect_base mint "ubuntu noble"
)
(
    fresh
    ID=elementary ID_LIKE=ubuntu VERSION_ID=8 UBUNTU_CODENAME=noble
    expect_base elementary "ubuntu noble"
)
(
    fresh
    ID=linuxmint ID_LIKE=debian VERSION_ID=7 DEBIAN_CODENAME=trixie
    expect_base lmde-codename "debian trixie"
)
(
    fresh
    ID=linuxmint ID_LIKE=debian VERSION_ID=6
    expect_base lmde-table "debian bookworm"
)
(
    fresh
    ID=devuan ID_LIKE=debian VERSION_ID=6
    out="$(Docker_Base_Repo)" && fail "devuan: Docker_Base_Repo returned 0"
    [[ -z "${out}" ]] || fail "devuan: Docker_Base_Repo printed '${out}'"
)
(
    fresh
    ID=kali ID_LIKE=debian VERSION_ID=2024.1
    out="$(Docker_Base_Repo)" && fail "kali: Docker_Base_Repo returned 0"
    [[ -z "${out}" ]] || fail "kali: Docker_Base_Repo printed '${out}'"
)

(
    fresh
    dpkg_arch=arm64
    Install_Docker_From_Repo ubuntu noble || fail "source-written: status $?"
    expected="$(printf '%s\n' "Types: deb" "URIs: https://download.docker.com/linux/ubuntu" "Suites: noble" "Components: stable" "Architectures: arm64" "Signed-By: /etc/apt/keyrings/docker.asc")"
    [[ "$(cat "${PREFIX}/etc/apt/sources.list.d/docker.sources")" == "${expected}" ]] || fail "source-written: docker.sources is not the expected text"
    [[ "$(cat "${PREFIX}/etc/apt/keyrings/docker.asc")" == "KEY" ]] || fail "source-written: key not written"
    [[ "$(grep -n apt-get "${CALLS}" | cut -d: -f2- | tr '\n' '|')" == "apt-get update|apt-get -y -qq install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin|" ]] || fail "source-written: apt-get calls are not update then install"
    ! grep -q zena "${CALLS}" || fail "source-written: a derivative codename reached apt"
)
(
    fresh
    mkdir -p "${PREFIX}/etc/apt/sources.list.d"
    echo "deb [signed-by=/x] https://download.docker.com/linux/ubuntu noble stable" >"${PREFIX}/etc/apt/sources.list.d/docker.list"
    Install_Docker_From_Repo ubuntu noble || fail "source-exists: status $?"
    [[ ! -e "${PREFIX}/etc/apt/sources.list.d/docker.sources" ]] || fail "source-exists: a second source was written"
    ! grep -q '^curl' "${CALLS}" || fail "source-exists: curl was called"
    grep -q 'apt-get -y -qq install docker-ce' "${CALLS}" || fail "source-exists: the install did not run"
)
(
    fresh
    curl_status=22
    ! Install_Docker_From_Repo ubuntu noble || fail "key-fails: returned 0"
    [[ ! -e "${PREFIX}/etc/apt/sources.list.d/docker.sources" ]] || fail "key-fails: a source was written"
)

(
    set +o pipefail # install.sh sets only -e: Install_Docker has to bring its own pipefail
    fresh
    ID=ubuntu get_docker_status=22
    out="$(Install_Docker)" && fail "get-docker-fails: Install_Docker returned 0"
    [[ "${out}" == *"[1] Installation failed"* ]] || fail "get-docker-fails: output is '${out}'"
    ! grep -q Check_Docker_Install_Final "${CALLS}" || fail "get-docker-fails: the final check ran"
)
(
    set +o pipefail
    fresh
    ID=linuxmint ID_LIKE="ubuntu debian" UBUNTU_CODENAME=noble
    Install_Docker >/dev/null || fail "derivative-path: status $?"
    ! grep -q 'get.docker.com' "${CALLS}" || fail "derivative-path: get.docker.com was used"
    [[ -e "${PREFIX}/etc/apt/sources.list.d/docker.sources" ]] || fail "derivative-path: no docker.sources"
)
(
    set +o pipefail
    fresh
    ID=kali ID_LIKE=debian VERSION_ID=2024.1
    out="$(Install_Docker)" && fail "no-base: Install_Docker returned 0"
    [[ "${out}" == *"[1] This system is based on Debian or Ubuntu but its os-release names no base release (UBUNTU_CODENAME or DEBIAN_CODENAME): install Docker by hand, then run this installer again." ]] || fail "no-base: output is '${out}'"
    ! grep -q '^curl' "${CALLS}" || fail "no-base: curl was called"
    [[ -z "$(find "${PREFIX}/etc/apt" -type f)" ]] || fail "no-base: a file was written under etc/apt"
)

# Check_Distribution: LSB_DIST and DIST are computed as install.sh does at its top.
# check <case> <expected status> <expected text> <forbidden text>: output kept in ${out}.
check() {
    local want="$2" status=0
    LSB_DIST="${ID_LIKE:-${ID}}" DIST="${ID}"
    out="$(Check_Distribution 2>&1)" || status=$?
    [[ "${status}" == "${want}" ]] || fail "$1: status ${status}, expected ${want}: ${out}"
    [[ -z "${3:-}" || "${out}" == *"$3"* ]] || fail "$1: output lacks '$3': ${out}"
    [[ -z "${4:-}" || "${out}" != *"$4"* ]] || fail "$1: output has '$4': ${out}"
}
marker() { mkdir -p "$(dirname "${PREFIX}$1")" && : >"${PREFIX}$1"; }

(
    fresh
    ID=debian
    marker /usr/bin/pveversion
    check proxmox-new 1 "--allow-proxmox-host" ""
    [[ "${out}" == *"Proxmox VE host"* ]] || fail "proxmox-new: no Proxmox VE host text"
)
(
    fresh
    ID=debian ALLOW_PROXMOX_HOST=1
    marker /usr/bin/pveversion
    check proxmox-allowed 0 "[3]"
)
(
    fresh
    ID=debian
    marker /usr/bin/pveversion
    mkdir -p "${CASA_STATE_DIR}" && echo v0.5.98 >"${CASA_STATE_DIR}/fork-release"
    check proxmox-upgrade 0 "[3]"
)
(
    fresh
    ID=debian
    marker /usr/bin/pveversion
    casaos() { :; }
    check proxmox-upstream 0 "[3]"
)
for m in /usr/bin/middlewared /usr/sbin/proxmox-backup-manager /usr/bin/pmgversion; do
    (
        fresh
        ID=debian ALLOW_PROXMOX_HOST=1
        marker "${m}"
        check "appliance ${m}" 1 "separate Debian or Ubuntu machine"
    )
done
(
    fresh
    ID=debian
    marker /etc/openmediavault
    check omv 0 "OpenMediaVault" ""
    [[ "${out}" == *"[3]"* ]] || fail "omv: no warning"
)
(
    fresh
    ID=devuan ID_LIKE=debian
    check devuan-new 1 "systemd"
)
(
    fresh
    ID=raspbian ID_LIKE=debian
    check raspbian 0 "Docker 28" ""
    [[ "${out}" == *"64-bit"* ]] || fail "raspbian: no 64-bit text"
)
for pair in "pi-os:/etc/rpi-issue" "armbian:/etc/armbian-release" "dietpi:/boot/dietpi.txt"; do
    (
        fresh
        ID=debian
        marker "${pair#*:}"
        check "${pair%%:*}" 0 "[0] Your Linux Distribution is" "[3]"
    )
done
(
    fresh
    ID=linuxmint ID_LIKE="ubuntu debian" PRETTY_NAME="Linux Mint 22.3" UBUNTU_CODENAME=noble
    check mint-name 0 "Linux Mint 22.3 (based on Ubuntu noble)"
)
sed -n '/^usage() {$/,/^}$/p' "${INSTALL_SH}" | grep -q -- '--allow-proxmox-host' || fail "usage() does not mention --allow-proxmox-host"

echo "ok: install.sh handles derivatives and a failed get.docker.com"
