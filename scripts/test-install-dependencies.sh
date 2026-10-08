#!/usr/bin/env bash
# shellcheck disable=SC2016 # install.sh's text is grepped literally
#
# Checks what install.sh does when apt cannot install one of its dependencies,
# without installing anything: the functions that answer are copied out of
# install.sh as it stands and run for a few systems against a sources file in a
# temporary directory, with apt-get replaced by a recorder, under the same
# `set -e` and the same `cmd || Package_Install_Failed` call as the install, and
# the line that calls them is checked to be where the install needs it.
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

for f in Answer_Is_Yes Debian_Archive_Agreed Use_Debian_Archive Package_Install_Failed Check_Docker_Install Check_Docker_Install_Final Update_Package_Resource; do
    eval "$(sed -n "/^${f}() {\$/,/^}\$/p" "${INSTALL_SH}")"
    declare -F "${f}" >/dev/null || fail "install.sh defines no ${f}()"
done

# What those functions take from install.sh: its Show, which ends the install on 1,
# and its globals; apt-get is a recorder whose answers can be set.
sudo_cmd=""
LINE_BREAK=''
DEBIAN_ARCHIVE=0
APT_SOURCES_LIST="${WORK}/sources.list"
APT_LOG="${WORK}/apt.log"
APT_404='E: Failed to fetch http://security.debian.org/debian-security/pool/updates/main/n/ntfs-3g/ntfs-3g_2017.3.23AR.3-4%2bdeb11u5_amd64.deb  404  Not Found [IP: 151.101.202.132 80]'
apt_download_text="${APT_404}"
apt_install_status=0
apt_update_status=0
ColorReset() { :; }
Show() {
    echo "[$1] $2"
    if (($1 == 1)); then exit 1; fi
}
apt-get() {
    echo "$*" >>"${APT_LOG}"
    case " $* " in
    *" --download-only "*)
        echo "${apt_download_text}"
        return 100
        ;;
    *" install "*) return "${apt_install_status}" ;;
    *" update "*) return "${apt_update_status}" ;;
    esac
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
    apt_download_text="${APT_404}"
    apt_install_status=0
    apt_update_status=0
}

# say <expected status> <id> <version_id>: what the function prints for that
# system, in $out, and its status, run the way the install runs it: under set -e,
# as the right-hand side of a failing command. "-" for both fields leaves them unset.
say() {
    local want="$1" status=0
    out="$(
        exec 2>&1
        set -e
        if [[ "$2" == - ]]; then unset ID VERSION_ID; else ID="$2"; VERSION_ID="$3"; fi
        false || Package_Install_Failed ntfs-3g
    )" || status=$?
    [[ "${status}" -eq "${want}" ]] || fail "$2 $3: status ${status}, wanted ${want}: ${out}"
}

sources_are() {
    [[ "$(cat "${APT_SOURCES_LIST}")" == "$1" ]] || fail "$2: the sources are now $(cat "${APT_SOURCES_LIST}")"
}

apt_calls() { tr '\n' '|' <"${APT_LOG}"; }

# Debian 11, apt said 404, nobody said yes (and the test has no terminal): nothing
# is changed, the install ends, and what it says is true: why, the commands, the option.
fresh
say 1 debian 11
sources_are "${ORIGINAL}" "debian 11 without consent"
[[ ! -e "${APT_SOURCES_LIST}.recasaos.bak" ]] || fail "debian 11 without consent: a copy was made"
[[ "$(apt_calls)" == "-y -qq --download-only install ntfs-3g --no-upgrade|" ]] ||
    fail "debian 11 without consent: apt was asked for more than the look at the cause ($(apt_calls))"
grep -q 'Debian 11 is out of support' <<<"${out}" || fail "debian 11: no explanation"
grep -q 'ntfs-3g' <<<"${out}" || fail "debian 11: the package is not named"
grep -q 'sudo apt-get update' <<<"${out}" || fail "debian 11: no apt-get update to run after the change"
grep -q -- 'bash -s -- --use-debian-archive' <<<"${out}" || fail "debian 11: the option is not given in a form that works through curl | sudo bash"
cmd="$(grep -o "sudo sed -i[^ ]* -E '[^']*' /etc/apt/sources.list" <<<"${out}")" || fail "debian 11: no sed command"
grep -q -- '-i.recasaos.bak' <<<"${cmd}" || fail "debian 11: the printed sed keeps no copy of the file"
cmd="${cmd#sudo }"
cmd="${cmd%/etc/apt/sources.list}${APT_SOURCES_LIST}"
eval "${cmd}"
sources_are "${MOVED}" "debian 11: the printed sed command"
[[ "$(cat "${APT_SOURCES_LIST}.recasaos.bak")" == "${ORIGINAL}" ]] || fail "debian 11: the printed sed command kept no copy"

# Debian 11, apt said 404, told to: the sources move, the first file is kept, apt
# refreshes and the install is tried again, and the install goes on.
fresh
DEBIAN_ARCHIVE=1
say 0 debian 11
sources_are "${MOVED}" "debian 11 with the option"
[[ "$(cat "${APT_SOURCES_LIST}.recasaos.bak")" == "${ORIGINAL}" ]] || fail "debian 11 with the option: the old file was not kept"
[[ "$(apt_calls)" == *"--download-only"*"update -qq"*"|-y -qq install ntfs-3g --no-upgrade|" ]] ||
    fail "debian 11 with the option: apt was not asked, refreshed and asked again ($(apt_calls))"

# ... and a second time, once the owner has edited the file, the copy of the first
# one is not replaced by a later one.
{ echo "${ORIGINAL}"; echo "# edited since"; } >"${APT_SOURCES_LIST}"
say 0 debian 11
[[ "$(cat "${APT_SOURCES_LIST}.recasaos.bak")" == "${ORIGINAL}" ]] || fail "debian 11 twice: the kept copy was overwritten"

# A repository that cannot be refreshed (a dead backports line) is not the end: the
# install is tried again with the lists apt has.
fresh
DEBIAN_ARCHIVE=1
apt_update_status=100
say 0 debian 11
sources_are "${MOVED}" "debian 11, update failing"
grep -q 'could not be refreshed' <<<"${out}" || fail "debian 11, update failing: nothing said about it"

# Told to and moved, but the package still cannot be had: the install ends, and it
# does not tell the owner to do what has just been done.
fresh
DEBIAN_ARCHIVE=1
apt_install_status=1
say 1 debian 11
grep -q 'still could not be installed' <<<"${out}" || fail "debian 11, retry failing: not said"
! grep -q "Point apt at Debian's archive" <<<"${out}" || fail "debian 11, retry failing: asks for what was done"

# Debian 11 but apt did not say 404 (a lock, a full disk, no network): the sources
# are nobody's business, even with the option.
fresh
DEBIAN_ARCHIVE=1
apt_download_text='E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 612 (apt-get)'
say 1 debian 11
sources_are "${ORIGINAL}" "debian 11, not a 404"
[[ ! -e "${APT_SOURCES_LIST}.recasaos.bak" ]] || fail "debian 11, not a 404: a copy was made"
[[ "$(apt_calls)" == "-y -qq --download-only install ntfs-3g --no-upgrade|" ]] || fail "debian 11, not a 404: apt was asked to change things ($(apt_calls))"
! grep -q 'Debian 11 is out of support' <<<"${out}" || fail "debian 11, not a 404: blames the release"

# Lines that are not the Debian 11 security repository are left alone: a comment,
# another mirror's name that would not resolve under the archive.
for line in '# deb http://security.debian.org/debian-security bullseye-security main' \
    'deb http://cdn-fastly.deb.debian.org/debian-security bullseye-security main'; do
    fresh
    echo "${line}" >"${APT_SOURCES_LIST}"
    DEBIAN_ARCHIVE=1
    status=0
    (Use_Debian_Archive) >/dev/null 2>&1 || status=$?
    [[ "${status}" -eq 1 ]] || fail "'${line}': the sources were taken as movable"
    [[ "$(cat "${APT_SOURCES_LIST}")" == "${line}" && ! -e "${APT_SOURCES_LIST}.recasaos.bak" ]] || fail "'${line}': the file was touched"
done

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

# What the owner types: yes in a few spellings, nothing else.
for answer in y Y yes YES yEs; do Answer_Is_Yes "${answer}" || fail "'${answer}' is not taken as a yes"; done
for answer in "" n N no "y " yy yep; do ! Answer_Is_Yes "${answer}" || fail "'${answer}' is taken as a yes"; done

# The prompt is only for a stdout that is a terminal and a /dev/tty that opens, and
# the steps that change the file stop the function when they fail.
agreed="$(sed -n '/^Debian_Archive_Agreed() {$/,/^}$/p' "${INSTALL_SH}")"
grep -Fq '[[ -t 1 ]] && { : </dev/tty; } 2>/dev/null || return 1' <<<"${agreed}" || fail "Debian_Archive_Agreed asks without checking for a terminal"
grep -Fq 'read -r -t 120' <<<"${agreed}" || fail "Debian_Archive_Agreed waits for ever"
use="$(sed -n '/^Use_Debian_Archive() {$/,/^}$/p' "${INSTALL_SH}")"
[[ "$(grep -c 'recasaos.bak" || return 1\|"${APT_SOURCES_LIST}" || return 1' <<<"${use}")" -eq 2 ]] || fail "Use_Debian_Archive goes on after a failed copy or sed"

# The option reaches the installer both ways.
grep -q 'use-debian-archive)' "${INSTALL_SH}" || fail "install.sh does not take --use-debian-archive"
grep -q 'RECASAOS_DEBIAN_ARCHIVE' "${INSTALL_SH}" || fail "install.sh does not read RECASAOS_DEBIAN_ARCHIVE"
grep -q -- '--use-debian-archive  ' "${INSTALL_SH}" || fail "--use-debian-archive is not in the usage text"

# The one place install.sh asks apt for a dependency calls it.
[[ "$(grep -c 'apt-get -y -qq install "\$packagesNeeded"' "${INSTALL_SH}")" -eq 1 ]] ||
    fail "install.sh does not have exactly one apt-get install of \$packagesNeeded"
grep -q 'apt-get -y -qq install "\$packagesNeeded" --no-upgrade || Package_Install_Failed "\$packagesNeeded"' "${INSTALL_SH}" ||
    fail "the apt-get install of \$packagesNeeded does not call Package_Install_Failed on failure"

# Docker already on the box: said plainly when it is too old or does not answer, left
# alone when it is fine, installed only when there is none. A docker is a script on a
# PATH of its own here, answering as the test sets DOCKER_SAYS.
MINIMUM_DOCKER_VERSION=20
Install_Docker() { echo "[installed docker]"; }
mkdir -p "${WORK}/bin" "${WORK}/nobin"
cat >"${WORK}/bin/docker" <<'EOF'
#!/bin/sh
[ "${DOCKER_SAYS}" != down ] || exit 1
echo "${DOCKER_SAYS}"
EOF
chmod +x "${WORK}/bin/docker"
docker_says() { # <what docker answers, or "down"; "none" for no docker> <expected status>
    local status=0 path="${WORK}/bin"
    [[ "$1" != none ]] || path="${WORK}/nobin"
    out="$(
        exec 2>&1
        set -e
        export PATH="${path}" DOCKER_SAYS="$1"
        Check_Docker_Install
    )" || status=$?
    [[ "${status}" -eq "$2" ]] || fail "docker '$1': status ${status}, wanted $2: ${out}"
}
docker_says 28.0.4 0
grep -q 'Current Docker version is 28.0.4' <<<"${out}" || fail "docker 28.0.4: not said to be fine"
docker_says "26.1.5+dfsg1" 0
docker_says 20.10.24 0
docker_says 19.03.15 1
grep -q 'Recommended minimum Docker version' <<<"${out}" || fail "docker 19: no explanation of the minimum"
docker_says 5.0.0 1
grep -q 'Recommended minimum Docker version' <<<"${out}" || fail "docker 5.0.0: a one-digit major is not compared"
! grep -qi 'syntax error\|invalid arithmetic' <<<"${out}" || fail "docker 5.0.0: the comparison broke: ${out}"
docker_says down 1
grep -q 'does not answer' <<<"${out}" || fail "docker down: said nothing"
! grep -q 'installed docker' <<<"${out}" || fail "docker down: a Docker was installed over one that is only stopped"
docker_says none 0
grep -q 'installed docker' <<<"${out}" || fail "no docker: none was installed"
grep -Fq ',\nCurrent Docker version is' "${INSTALL_SH}" || fail "the minimum-version message lost the line break before 'Current'"
! grep -Fq ',\Current' "${INSTALL_SH}" || fail "install.sh still has the backslash-Current typo"
! grep -Fq 'Docker_Version:0:2' "${INSTALL_SH}" || fail "install.sh still compares the first two characters of the Docker version"

# The check after Docker was installed: the same, and it never installs Docker again.
Check_Docker_Running() { echo "[docker running]"; }
final_says() { # <what docker answers, or "down"> <expected status>
    local status=0
    out="$(
        exec 2>&1
        set -e
        export PATH="${WORK}/bin" DOCKER_SAYS="$1"
        Check_Docker_Install_Final
    )" || status=$?
    [[ "${status}" -eq "$2" ]] || fail "final check, docker '$1': status ${status}, wanted $2: ${out}"
}
final_says 29.8.2 0
grep -q 'docker running' <<<"${out}" || fail "final check: Docker is not checked to be running"
final_says 19.03.15 1
grep -q 'Recommended minimum Docker version' <<<"${out}" || fail "final check, docker 19: no explanation of the minimum"
final_says 5.0.0 1
final_says down 1
grep -q 'does not answer' <<<"${out}" || fail "final check, docker down: said nothing"
! grep -q 'installed docker' <<<"${out}" || fail "final check, docker down: Docker was installed again"

# Refreshing apt's lists is bounded: a mirror that says nothing costs the timeout, not
# the run, and the install goes on with the lists the host has. apt-get is a script on
# a PATH of its own, answering as the test sets FAKE_APT.
GreyStart() { :; }
APT_UPDATE_TIMEOUT=2
mkdir -p "${WORK}/aptbin"
cat >"${WORK}/aptbin/apt-get" <<'EOF'
#!/bin/sh
case "${FAKE_APT}" in
stall) sleep 20 ;;
fail) exit 100 ;;
esac
exit 0
EOF
chmod +x "${WORK}/aptbin/apt-get"
refresh_says() { # <stall|fail|ok> <longest it may take, in seconds>
    local started="${SECONDS}"
    out="$(
        exec 2>&1
        set -e
        unset -f apt-get # the recorder above would hide the script from command -v
        export PATH="${WORK}/aptbin:${PATH}" FAKE_APT="$1"
        Update_Package_Resource
    )" || fail "refresh '$1': the install ended: ${out}"
    [[ $((SECONDS - started)) -le "$2" ]] || fail "refresh '$1': took $((SECONDS - started)) s, more than $2"
}
refresh_says stall 15
grep -q 'did not finish refreshing the package lists in 2 seconds' <<<"${out}" || fail "refresh stalled: nothing said about it: ${out}"
refresh_says fail 15
grep -q 'Could not refresh the package lists (exit 100)' <<<"${out}" || fail "refresh failing: ${out}"
refresh_says ok 15
grep -q 'Update package manager complete' <<<"${out}" || fail "refresh working: ${out}"
grep -Fq 'timeout "${APT_UPDATE_TIMEOUT}" apt-get update -qq' "${INSTALL_SH}" || fail "install.sh does not bound apt-get update"

echo "ok: what install.sh does when a dependency cannot be installed"
