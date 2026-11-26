#!/usr/bin/env bash
# shellcheck disable=SC2034 # the globals are read by the function copied out of install.sh
#
# Checks how install.sh leaves casaos.service: Detach_From_CasaOS_Service is
# copied out of install.sh as it stands and run with stand-ins for sudo and
# systemd-run. Run from a file, it hands a root-only copy of that same file to a
# unit of its own; read from a pipe, it has no file to hand over and goes on in
# place. It never downloads an installer to run.
#
#   bash scripts/test-install-detach.sh
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

body="$(sed -n '/^Detach_From_CasaOS_Service() {$/,/^}$/p' "${INSTALL_SH}")"
[ -n "${body}" ] || fail "install.sh defines no Detach_From_CasaOS_Service()"
if grep -vE '^ *#' <<<"${body}" | grep -qE '\| *(/bin/)?(ba)?sh|curl|wget'; then
    fail "Detach_From_CasaOS_Service downloads an installer or pipes one into a shell"
fi
! grep -q 'CASAOS_INSTALLER_SELF_URL' "${INSTALL_SH}" || fail "install.sh still names an address to download itself from"
grep -q '^readonly CASAOS_DETACHED_INSTALLER=' "${INSTALL_SH}" || fail "install.sh names no file for its detached copy"

# the function as the test runs it, in a file of its own and with these stand-ins
mkdir -p "${WORK}/bin"
cat >"${WORK}/bin/sudo" <<'STUB'
#!/bin/sh
echo "sudo $*" >>"${CALLS}"
exec "$@"
STUB
cat >"${WORK}/bin/systemd-run" <<'STUB'
#!/bin/sh
# records each word in a file of its own, and whether one holds a $ or a % that systemd would change
rm -rf "${WORK}/sdrun"
mkdir -p "${WORK}/sdrun"
n=0
for a in "$@"; do
    n=$((n + 1))
    printf '%s' "${a}" >"${WORK}/sdrun/${n}"
    case "${a}" in *'$'* | *%*) echo "${n}" >>"${WORK}/sdrun/bad" ;; esac
done
echo "systemd-run" >>"${CALLS}"
STUB
chmod +x "${WORK}/bin/"*

cat >"${WORK}/detach.sh" <<EOF
set -euo pipefail
PATH="${WORK}/bin:\${PATH}"
Show() { echo "[\$1] \$2"; if [ "\$1" = 1 ]; then exit 1; fi; }
Running_Under_CasaOS_Service() { return 0; }
sudo_cmd=sudo
NO_TELEMETRY=1
NO_MODULE_UPDATE=0
MODULES_REQUESTED=""
CASAOS_INSTALL_RELEASE_TAG=v1.2.3
CASAOS_UPDATE_LOG="${WORK}/log/upgrade.log"
CASAOS_DETACHED_INSTALLER="${WORK}/update/install-detached.sh"
${body}
Detach_From_CasaOS_Service
echo "went on in place"
EOF
export WORK CALLS="${WORK}/calls"

# run from a file
: >"${CALLS}"
out="$(cd "${WORK}" && bash detach.sh 2>&1)" || fail "run from a file: ${out}"
case "${out}" in *"went on in place"*) fail "run from a file: it did not end once handed over: ${out}" ;; esac
grep -qx systemd-run "${CALLS}" || fail "run from a file: no unit was started: ${out}"
[ ! -e "${WORK}/sdrun/bad" ] || fail "run from a file: systemd would change words $(tr '\n' ' ' <"${WORK}/sdrun/bad")of the unit's command"
copy="${WORK}/update/install-detached.sh"
cmp -s "${copy}" "${WORK}/detach.sh" || fail "run from a file: ${copy} is not a copy of the running file"
if [ "$(uname -s)" = Linux ]; then
    [ "$(stat -c %a "${copy}")" = 700 ] || fail "run from a file: the copy is $(stat -c %a "${copy}"), not 700"
    [ "$(stat -c %a "${copy%/*}")" = 700 ] || fail "run from a file: its directory is $(stat -c %a "${copy%/*}"), not 700"
fi
grep -qx -- '--setenv=CASAOS_INSTALLER_DETACHED=1' < <(for f in "${WORK}"/sdrun/[0-9]*; do cat "${f}"; echo; done) ||
    fail "run from a file: the unit is not told it is detached"
grep -qx -- '--setenv=RECASAOS_TELEMETRY=0' < <(for f in "${WORK}"/sdrun/[0-9]*; do cat "${f}"; echo; done) ||
    fail "run from a file: the opt-out of the statistics is lost"
last="$(cat "${WORK}/sdrun/$(ls "${WORK}/sdrun" | grep -c '^[0-9]*$')")"
case "${last}" in
*"exec /bin/bash ${copy}") ;;
*) fail "run from a file: the unit does not run the copy: ${last}" ;;
esac
case "${last}" in *"${WORK}/log/upgrade.log"*) ;; *) fail "run from a file: the unit does not write to the update log: ${last}" ;; esac
echo "ok: run from a file, the update goes on from a root-only copy of that file, in a unit of its own"

# read from a pipe: no file to hand over, nothing downloaded, it goes on in place
: >"${CALLS}"
rm -rf "${WORK}/sdrun" "${WORK}/update"
out="$(cd "${WORK}" && bash <detach.sh 2>&1)" || fail "read from a pipe: ${out}"
case "${out}" in *"went on in place"*) ;; *) fail "read from a pipe: it did not go on: ${out}" ;; esac
! grep -qx systemd-run "${CALLS}" || fail "read from a pipe: a unit was started"
[ ! -e "${WORK}/update" ] || fail "read from a pipe: something was copied"
echo "ok: read from a pipe, the update goes on in place and downloads nothing"
