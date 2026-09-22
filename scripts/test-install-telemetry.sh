#!/usr/bin/env bash
# shellcheck disable=SC2016,SC2034 # install.sh's text is grepped literally; the globals are read by the functions copied out of it
#
# Checks the anonymous-statistics parts of install.sh without installing
# anything: each function is copied out of install.sh as it stands and run
# against a temporary directory standing in for /var/lib/casaos, and the lines
# that call them are checked to be where the install needs them.
#
#   bash scripts/test-install-telemetry.sh
#
# Some checks need Linux and are skipped elsewhere: file modes (a Windows
# checkout holds none), and install.sh's own startup (it reads
# /etc/os-release). The release workflow runs this on Linux before anything
# is built.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

[[ "$(uname -s)" == Linux ]] || echo "note: not Linux, the Linux-only checks are skipped"

# install.sh as bash reads it: a Windows checkout has CRLF line endings.
INSTALL_SH="${WORK}/install.sh"
tr -d '\r' <"${ROOT}/install.sh" >"${INSTALL_SH}"
bash -n "${INSTALL_SH}" || fail "install.sh does not parse"

# The functions under test, as install.sh defines them.
FUNCTIONS=(Previous_Release Write_Telemetry_Markers Telemetry_Notice)
for f in "${FUNCTIONS[@]}"; do
    eval "$(sed -n "/^${f}() {\$/,/^}\$/p" "${INSTALL_SH}")"
    declare -F "${f}" >/dev/null || fail "install.sh defines no ${f}()"
done

# line_of <grep options> <text>: the number of the one line of install.sh that
# matches; fails when there is none, or more than one.
line_of() {
    local found
    found="$(grep -n "$@" "${INSTALL_SH}" | cut -d: -f1)" || true
    [[ -n "${found}" && "${found}" != *$'\n'* ]] ||
        fail "install.sh has no single line matching '${*: -1}' (lines: ${found:-none})"
    echo "${found}"
}

# What those functions take from install.sh's globals.
sudo_cmd=""
NO_TELEMETRY=0
GREEN_LINE="-----"
COLOUR_RESET=""
aCOLOUR=('' '' '' '' '')
Show() { echo "$2"; }

# reset <case>: a state directory that does not exist yet, and an empty
# directory to put first on PATH, for a fake casaos binary
reset() {
    CASA_STATE_DIR="${WORK}/$1/var/lib/casaos"
    BIN_DIR="${WORK}/$1/bin"
    mkdir -p "${BIN_DIR}"
}

fake_casaos() {
    printf '#!/bin/sh\necho v0.4.2\n' >"${BIN_DIR}/casaos"
    chmod +x "${BIN_DIR}/casaos"
}

# expect_mode <file> <mode>
expect_mode() {
    [[ "$(uname -s)" == Linux ]] || return 0
    local mode
    mode="$(stat -c '%a' "$1")"
    [[ "${mode}" == "$2" ]] || fail "$1 is ${mode}, expected $2"
}

# Previous_Release: the tag in fork-release, else upstream when a casaos binary
# is on PATH, else new. PATH holds BIN_DIR alone: it uses builtins only.
reset tag
mkdir -p "${CASA_STATE_DIR}"
printf 'v0.4.99\r\n' >"${CASA_STATE_DIR}/fork-release"
fake_casaos
got="$(PATH="${BIN_DIR}" Previous_Release)"
[[ "${got}" == v0.4.99 ]] || fail "fork-release v0.4.99 gave '${got}'"

reset upstream
fake_casaos
got="$(PATH="${BIN_DIR}" Previous_Release)"
[[ "${got}" == upstream ]] || fail "a casaos binary without fork-release gave '${got}'"

reset empty-marker
mkdir -p "${CASA_STATE_DIR}"
: >"${CASA_STATE_DIR}/fork-release"
fake_casaos
got="$(PATH="${BIN_DIR}" Previous_Release)"
[[ "${got}" == upstream ]] || fail "an empty fork-release beside a casaos binary gave '${got}'"

reset new
got="$(PATH="${BIN_DIR}" Previous_Release)"
[[ "${got}" == new ]] || fail "nothing installed gave '${got}'"
echo "ok: Previous_Release names the tag, upstream or new"

# Write_Telemetry_Markers: upgraded-from on every run, root's alone, and the
# line the install check looks for.
reset first-install
fake_casaos
out="$(PATH="${BIN_DIR}:${PATH}" Write_Telemetry_Markers)"
[[ "${out}" == *"Previous release: upstream"* ]] || fail "no 'Previous release: upstream' line in: ${out}"
[[ "$(cat "${CASA_STATE_DIR}/upgraded-from")" == upstream ]] || fail "upgraded-from is not 'upstream'"
expect_mode "${CASA_STATE_DIR}/upgraded-from" 600

reset rerun
mkdir -p "${CASA_STATE_DIR}"
printf 'v0.5.0\n' >"${CASA_STATE_DIR}/fork-release"
printf 'stale\n' >"${CASA_STATE_DIR}/upgraded-from"
chmod 644 "${CASA_STATE_DIR}/upgraded-from"
out="$(Write_Telemetry_Markers)"
[[ "${out}" == *"Previous release: v0.5.0"* ]] || fail "no 'Previous release: v0.5.0' line in: ${out}"
[[ "$(cat "${CASA_STATE_DIR}/upgraded-from")" == v0.5.0 ]] || fail "upgraded-from was not rewritten"
expect_mode "${CASA_STATE_DIR}/upgraded-from" 600
echo "ok: Write_Telemetry_Markers writes upgraded-from, 600, and logs it"

# Where install.sh calls it: after the loop that stops the services, before the
# release tree, the overlay's fork-release with it, is copied onto / (the loop
# that starts the services comes after that copy).
stop="$(line_of -F 'systemctl stop "${SERVICE}"')"
call="$(line_of -xF '    Write_Telemetry_Markers')"
copy="$(line_of -F 'cp -rf "${SYSROOT_DIR}"/* /')"
((stop < call && call < copy)) ||
    fail "Write_Telemetry_Markers is called at line ${call}, not between the services' stop (line ${stop}) and the copy onto / (line ${copy})"
echo "ok: install.sh writes the markers with the services stopped, before the copy onto /"

# --no-telemetry or RECASAOS_TELEMETRY=0 (both set NO_TELEMETRY): telemetry-off,
# root's alone. Without either, an earlier choice is left exactly as it is.
reset turned-off
out="$(NO_TELEMETRY=1 Write_Telemetry_Markers)"
[[ -e "${CASA_STATE_DIR}/telemetry-off" ]] || fail "NO_TELEMETRY=1 wrote no telemetry-off"
expect_mode "${CASA_STATE_DIR}/telemetry-off" 600
[[ "${out}" == *"Anonymous statistics turned off."* ]] || fail "NO_TELEMETRY=1 did not say so: ${out}"

reset kept
mkdir -p "${CASA_STATE_DIR}"
: >"${CASA_STATE_DIR}/telemetry-off"
printf '{"enabled":false,"id":"kept","notice_seen":true}\n' >"${CASA_STATE_DIR}/telemetry.json"
Write_Telemetry_Markers >/dev/null
[[ -e "${CASA_STATE_DIR}/telemetry-off" ]] || fail "a run without the flag removed telemetry-off"
[[ "$(cat "${CASA_STATE_DIR}/telemetry.json")" == '{"enabled":false,"id":"kept","notice_seen":true}' ]] ||
    fail "a run without the flag touched telemetry.json"

reset not-asked
Write_Telemetry_Markers >/dev/null
[[ ! -e "${CASA_STATE_DIR}/telemetry-off" ]] || fail "a run without the flag wrote telemetry-off"
echo "ok: telemetry-off only when asked, an earlier choice kept"

# no_telemetry <install.sh arguments>: the NO_TELEMETRY that install.sh's own
# lines give for those arguments and this environment. The lines are copied out
# of install.sh the way the functions are, so this runs anywhere.
no_telemetry() {
    (
        # shellcheck disable=SC2329 # called by the getopts loop copied below
        usage() { exit "$1"; }
        eval "$(sed -n '/^NO_TELEMETRY=0$/,/^fi$/p' "${INSTALL_SH}")"
        eval "$(sed -n '/^while getopts /,/^done$/p' "${INSTALL_SH}")"
        echo "${NO_TELEMETRY}"
    )
}
[[ "$(no_telemetry)" == 0 ]] || fail "no flag set NO_TELEMETRY"
[[ "$(no_telemetry --no-telemetry)" == 1 ]] || fail "--no-telemetry did not set NO_TELEMETRY"
[[ "$(RECASAOS_TELEMETRY=0 no_telemetry)" == 1 ]] || fail "RECASAOS_TELEMETRY=0 did not set NO_TELEMETRY"
[[ "$(RECASAOS_TELEMETRY=1 no_telemetry)" == 0 ]] || fail "RECASAOS_TELEMETRY=1 set NO_TELEMETRY"
echo "ok: --no-telemetry and RECASAOS_TELEMETRY=0 set NO_TELEMETRY, nothing else does"

# The option itself, through install.sh's own parsing.
if [[ -r /etc/os-release ]]; then
    bash "${ROOT}/install.sh" -h >"${WORK}/usage.txt" 2>/dev/null || fail "install.sh -h failed"
    grep -e '--no-telemetry' "${WORK}/usage.txt" >/dev/null || fail "usage does not name --no-telemetry"
    grep -F 'https://github.com/ReCasaOS/CasaOS-Install#anonymous-statistics' "${WORK}/usage.txt" >/dev/null ||
        fail "usage does not link the README section"
    bash "${ROOT}/install.sh" --no-telemetry -h >/dev/null 2>&1 || fail "--no-telemetry is refused"
    if bash "${ROOT}/install.sh" --no-such-option >/dev/null 2>&1; then
        fail "an unknown long option is accepted"
    fi
    echo "ok: install.sh takes --no-telemetry and refuses other long options"
fi

# Telemetry_Notice: on unless this run turned them off, that request still
# waits for the core, or the core has them off; the README section either way.
README_URL="https://github.com/ReCasaOS/CasaOS-Install#anonymous-statistics"
expect_notice() { # <on|off> <case>
    local out
    out="$(Telemetry_Notice)"
    [[ "${out}" == *"Anonymous statistics are $1."* ]] || fail "$2: the notice does not say $1: ${out}"
    [[ "${out}" == *"${README_URL}"* ]] || fail "$2: the notice does not link ${README_URL}"
}
reset notice
expect_notice on "nothing on the box yet"
NO_TELEMETRY=1 expect_notice off "--no-telemetry"
mkdir -p "${CASA_STATE_DIR}"
: >"${CASA_STATE_DIR}/telemetry-off"
expect_notice off "telemetry-off waiting for the core"
rm "${CASA_STATE_DIR}/telemetry-off"
printf '{"enabled":false,"id":"x","notice_seen":true}\n' >"${CASA_STATE_DIR}/telemetry.json"
expect_notice off "the core has them off"
printf '{\n  "enabled": false\n}\n' >"${CASA_STATE_DIR}/telemetry.json"
expect_notice off "the core has them off, indented"
printf '{"enabled":true,"id":"x","notice_seen":false}\n' >"${CASA_STATE_DIR}/telemetry.json"
expect_notice on "the core has them on"

# And it closes the run: install.sh's last line calls it.
last="$(grep -v '^[[:space:]]*$' "${INSTALL_SH}" | tail -n 1)"
[[ "${last}" == Telemetry_Notice ]] || fail "install.sh ends with '${last}', not the Telemetry_Notice call"
echo "ok: the closing notice says on or off, links the README, and ends install.sh"
