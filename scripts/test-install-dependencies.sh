#!/usr/bin/env bash
# shellcheck disable=SC2016 # install.sh's text is grepped literally
#
# Checks what install.sh does when apt cannot install one of its dependencies,
# without installing anything: the functions that answer are copied out of
# install.sh as it stands and run for a few systems against a sources file in a
# temporary directory, with apt-get replaced by a recorder, and the line that
# calls them is checked to be where the install needs it.
#
#   bash scripts/test-install-dependencies.sh
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

for f in Debian_Archive_Agreed Use_Debian_Archive Package_Install_Failed; do
    eval "$(sed -n "/^${f}() {\$/,/^}\$/p" "${INSTALL_SH}")"
    declare -F "${f}" >/dev/null || fail "install.sh defines no ${f}()"
done

# What those functions take from install.sh: its Show, which ends the install on 1,
# and its globals; apt-get is a recorder whose install can be made to fail.
sudo_cmd=""
DEBIAN_ARCHIVE=0
APT_SOURCES_LIST="${WORK}/sources.list"
APT_LOG="${WORK}/apt.log"
apt_install_status=0
ColorReset() { :; }
Show() {
    echo "[$1] $2"
    if (($1 == 1)); then exit 1; fi
}
apt-get() {
    echo "$*" >>"${APT_LOG}"
    case " $* " in *" install "*) return "${apt_install_status}" ;; esac
    return 0
}

ORIGINAL="$(
    cat <<'EOF'
deb http://deb.debian.org/debian bullseye main
deb-src http://deb.debian.org/debian bullseye main
deb http://security.debian.org/debian-security bullseye-security main
deb-src http://deb.debian.org/debian-security bullseye-security main
deb http://deb.debian.org/debian bullseye-updates main
EOF
)"
MOVED="$(
    cat <<'EOF'
deb http://deb.debian.org/debian bullseye main
deb-src http://deb.debian.org/debian bullseye main
deb http://archive.debian.org/debian-security bullseye-security main
deb-src http://archive.debian.org/debian-security bullseye-security main
deb http://deb.debian.org/debian bullseye-updates main
EOF
)"

fresh() {
    echo "${ORIGINAL}" >"${APT_SOURCES_LIST}"
    rm -f "${APT_SOURCES_LIST}.recasaos.bak" "${APT_LOG}"
    DEBIAN_ARCHIVE=0
    apt_install_status=0
}

# say <expected status> <id> <version_id>: what the function prints for that
# system, in $out, and its status. "-" for both fields leaves them unset.
say() {
    local want="$1" status=0
    out="$(
        if [[ "$2" == - ]]; then unset ID VERSION_ID; else ID="$2"; VERSION_ID="$3"; fi
        Package_Install_Failed ntfs-3g 2>&1
    )" || status=$?
    [[ "${status}" -eq "${want}" ]] || fail "$2 $3: status ${status}, wanted ${want}: ${out}"
}

sources_are() {
    [[ "$(cat "${APT_SOURCES_LIST}")" == "$1" ]] || fail "$2: the sources are now $(cat "${APT_SOURCES_LIST}")"
}

# Debian 11, nobody said yes (and the test has no terminal): nothing is changed,
# the install ends, and what it says is true: why, the commands, the option.
fresh
say 1 debian 11
sources_are "${ORIGINAL}" "debian 11 without consent"
[[ ! -e "${APT_SOURCES_LIST}.recasaos.bak" && ! -e "${APT_LOG}" ]] || fail "debian 11 without consent: apt or the sources were touched"
grep -q 'Debian 11 is out of support' <<<"${out}" || fail "debian 11: no explanation"
grep -q 'ntfs-3g' <<<"${out}" || fail "debian 11: the package is not named"
grep -q 'sudo apt-get update' <<<"${out}" || fail "debian 11: no apt-get update to run after the change"
grep -q -- '--use-debian-archive' <<<"${out}" || fail "debian 11: the option is not mentioned"
cmd="$(grep -o "sudo sed -i -E '[^']*' /etc/apt/sources.list" <<<"${out}")" || fail "debian 11: no sed command"
cmd="${cmd#sudo }"
cmd="${cmd%/etc/apt/sources.list}${APT_SOURCES_LIST}"
eval "${cmd}"
sources_are "${MOVED}" "debian 11: the printed sed command"

# Debian 11, told to: the sources move, the old file is kept, apt refreshes and
# the install is tried again, and the install goes on.
fresh
DEBIAN_ARCHIVE=1
say 0 debian 11
sources_are "${MOVED}" "debian 11 with the option"
[[ "$(cat "${APT_SOURCES_LIST}.recasaos.bak")" == "${ORIGINAL}" ]] || fail "debian 11 with the option: the old file was not kept"
[[ "$(sed -n 1p "${APT_LOG}")" == *update* && "$(sed -n 2p "${APT_LOG}")" == *"install ntfs-3g"* ]] ||
    fail "debian 11 with the option: apt was not refreshed and then asked again ($(tr '\n' '|' <"${APT_LOG}"))"

# ... and a second time, once the owner has edited the file, the copy of the first
# one is not replaced by a later one.
{ echo "${ORIGINAL}"; echo "# edited since"; } >"${APT_SOURCES_LIST}"
say 0 debian 11
[[ "$(cat "${APT_SOURCES_LIST}.recasaos.bak")" == "${ORIGINAL}" ]] || fail "debian 11 twice: the kept copy was overwritten"

# Told to, but the package still cannot be had: the install ends, saying so.
fresh
DEBIAN_ARCHIVE=1
apt_install_status=1
say 1 debian 11
grep -q 'Debian 11 is out of support' <<<"${out}" || fail "debian 11, retry failing: no explanation"

# Any other system: the explanation, and the sources are nobody's business.
for system in "debian 12" "ubuntu 20.04" "- -"; do
    fresh
    DEBIAN_ARCHIVE=1
    # shellcheck disable=SC2086
    say 1 ${system}
    grep -q '404 Not Found' <<<"${out}" || fail "${system}: no mention of the 404"
    grep -q 'ntfs-3g' <<<"${out}" || fail "${system}: the package is not named"
    ! grep -q 'sed -i' <<<"${out}" || fail "${system}: gives the Debian 11 command"
    sources_are "${ORIGINAL}" "${system}"
    [[ ! -e "${APT_LOG}" ]] || fail "${system}: apt was asked for something"
done

# The option reaches the installer both ways.
grep -q 'use-debian-archive)' "${INSTALL_SH}" || fail "install.sh does not take --use-debian-archive"
grep -q 'RECASAOS_DEBIAN_ARCHIVE' "${INSTALL_SH}" || fail "install.sh does not read RECASAOS_DEBIAN_ARCHIVE"
grep -q -- '--use-debian-archive  ' "${INSTALL_SH}" || fail "--use-debian-archive is not in the usage text"

# The one place install.sh asks apt for a dependency calls it.
[[ "$(grep -c 'apt-get -y -qq install "\$packagesNeeded"' "${INSTALL_SH}")" -eq 1 ]] ||
    fail "install.sh does not have exactly one apt-get install of \$packagesNeeded"
grep -q 'apt-get -y -qq install "\$packagesNeeded" --no-upgrade || Package_Install_Failed "\$packagesNeeded"' "${INSTALL_SH}" ||
    fail "the apt-get install of \$packagesNeeded does not call Package_Install_Failed on failure"

echo "ok: what install.sh does when a dependency cannot be installed"
