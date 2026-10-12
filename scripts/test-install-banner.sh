#!/usr/bin/env bash
# shellcheck disable=SC2034 # the globals are read by the function copied out of install.sh
#
# Checks the port the install's closing banner shows: Get_Gateway_Port is copied
# out of install.sh as it stands and run against gateway.ini files of the shapes
# the gateway writes (the file has `port` and `httpsport`, and a plain grep for
# "port" took both: "http://192.168.1.15:https 80").
#
#   bash scripts/test-install-banner.sh
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

eval "$(sed -n '/^Get_Gateway_Port() {$/,/^}$/p' "${INSTALL_SH}")"
declare -F Get_Gateway_Port >/dev/null || fail "install.sh defines no Get_Gateway_Port()"

# what the function takes from install.sh's globals
sudo_cmd=""
CASA_CONF_PATH="${WORK}/gateway.ini"

check() { # <what> <wanted port> <file content>
    local what="$1" want="$2" got
    printf '%b' "$3" >"${CASA_CONF_PATH}"
    got="$(Get_Gateway_Port)"
    [[ "${got}" == "${want}" ]] || fail "${what}: wanted '${want}', got '${got}'"
    echo "ok: ${what}"
}

check "the port alone" 80 '[gateway]\nport=80\n'
check "the https port with no value comes first (the file of the bug report)" 80 '[common]\nruntimepath=/var/run/casaos\n\n[gateway]\nhttpsport=\nport=80\n'
check "the https port comes after" 80 '[gateway]\nport=80\nhttpsport=\n'
check "the https port has a value" 8080 '[gateway]\nhttpsport=443\nport=8080\n'
check "an address and a certificate in the file" 80 '[gateway]\nport=80\naddress=\ntlscert=/etc/casaos/c.pem\ntlskey=/etc/casaos/k.pem\nhttpsport=443\n'
check "spaces around the equal sign and a Windows line ending" 8081 '[gateway]\r\n  port = 8081 \r\nhttpsport = 443\r\n'
check "no value for the port" 80 '[gateway]\nport=\nhttpsport=443\n'
check "a port that is not a number" 80 '[gateway]\nport=https\n'
check "no port key at all" 80 '[gateway]\nhttpsport=443\n'
check "an empty file" 80 ''

rm -f "${CASA_CONF_PATH}"
[[ "$(Get_Gateway_Port)" == 80 ]] || fail "a file that is not there is port 80"
echo "ok: a file that is not there"

# the banner reads the port through it, and nothing else reads gateway.ini by a bare grep for port
grep -q 'PORT=\$(Get_Gateway_Port)' "${INSTALL_SH}" || fail "Get_IPs does not read the port through Get_Gateway_Port"
if grep -nE "grep port|grep -i port" "${INSTALL_SH}" | grep -v "^[0-9]*:#"; then
    fail "install.sh still reads a port with a bare grep for port"
fi
echo "ok: no bare grep for port is left"

echo "all banner checks passed"
