#!/usr/bin/env bash
# Checks that an update started from the dashboard (CASAOS_INSTALLER_DETACHED=1)
# never installs Docker: Refuse_Docker_Install_On_Update is copied out of
# install.sh as it stands and run with and without a docker on the PATH. A
# first install typed by hand goes on to install Docker as before.
#
#   bash scripts/test-install-docker-update.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

INSTALL_SH="${WORK}/install.sh"
tr -d '\r' <"${ROOT}/install.sh" >"${INSTALL_SH}"
bash -n "${INSTALL_SH}" || fail "install.sh does not parse"

body="$(sed -n '/^Refuse_Docker_Install_On_Update() {$/,/^}$/p' "${INSTALL_SH}")"
[ -n "${body}" ] || fail "install.sh defines no Refuse_Docker_Install_On_Update()"
# it runs right after the detach, before the first step that changes the box
order="$(grep -nE '^(Detach_From_CasaOS_Service|Refuse_Docker_Install_On_Update|Update_Package_Resource|Check_Docker_Install)$' "${INSTALL_SH}" | cut -d: -f2 | paste -sd' ')"
[ "${order}" = "Detach_From_CasaOS_Service Refuse_Docker_Install_On_Update Update_Package_Resource Check_Docker_Install" ] ||
    fail "the check does not run right after the detach and before anything is installed: ${order}"

mkdir -p "${WORK}/with" "${WORK}/without"
printf '#!/bin/sh\nexit 0\n' >"${WORK}/with/docker"
chmod +x "${WORK}/with/docker"
cat >"${WORK}/run.sh" <<SH
set -euo pipefail
Show() { echo "[\$1] \$2"; if [ "\$1" = 1 ]; then exit 1; fi; }
${body}
Refuse_Docker_Install_On_Update
echo "went on"
SH

# run <detached> <dir on PATH>: the output; the status in ${status}
run() {
    status=0
    out="$(env -i PATH="$2:/usr/bin:/bin" ${1:+CASAOS_INSTALLER_DETACHED=$1} bash "${WORK}/run.sh" 2>&1)" || status=$?
}

[ -z "$(PATH=/usr/bin:/bin command -v docker || true)" ] || echo "note: this machine has a docker in /usr/bin or /bin; the cases without docker are not meaningful here" >&2

run 1 "${WORK}/without"
[ "${status}" -ne 0 ] || fail "self-update without docker: it went on: ${out}"
[ "${out}" = "[1] Docker is missing on this box: install Docker, then update ReCasaOS from a terminal." ] ||
    fail "self-update without docker: not said: ${out}"
echo "ok: an update from the dashboard on a box without Docker stops and says how to go on"

run 1 "${WORK}/with"
[ "${status}" -eq 0 ] && [ "${out}" = "went on" ] || fail "self-update with docker: ${out}"
echo "ok: an update from the dashboard on a box with Docker goes on"

run "" "${WORK}/without"
[ "${status}" -eq 0 ] && [ "${out}" = "went on" ] || fail "install by hand without docker: ${out}"
run 0 "${WORK}/without"
[ "${status}" -eq 0 ] && [ "${out}" = "went on" ] || fail "install by hand (CASAOS_INSTALLER_DETACHED=0) without docker: ${out}"
echo "ok: an install typed by hand goes on to install Docker"
