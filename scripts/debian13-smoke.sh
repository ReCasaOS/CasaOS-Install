#!/usr/bin/env bash
# Runs inside a Debian 13 machine, as a normal user with passwordless sudo, next
# to install.sh (the published installer), expected.env (the pins of that
# release, from components.lock) and nothing else. It installs the way the
# README says and then asks the questions install-check.yml asks of a fresh
# Ubuntu: the services are up and are this release, the dashboard answers on
# both ports, a first user can sign in, loopback alone is not a service.
set -euo pipefail

. /etc/os-release
echo "os: ${PRETTY_NAME} (VERSION_ID=${VERSION_ID})"
test "${VERSION_ID}" = 13

set -a
. ./expected.env
set +a

sudo -E bash install.sh --no-telemetry 2>&1 | tee install.log
test "${PIPESTATUS[0]}" -eq 0

# only for the questions below; the installer ran without them
sudo apt-get install -y -qq curl jq openssl >/dev/null

echo "docker $(sudo docker version --format '{{.Server.Version}}'), storage driver $(sudo docker info --format '{{.Driver}}')"

for unit in casaos-gateway casaos-message-bus casaos-user-service casaos-local-storage casaos-app-management rclone casaos; do
  state="$(systemctl is-active "${unit}.service" || true)"
  echo "${unit}: ${state}"
  test "${state}" = active
done
marker="$(cat /var/lib/casaos/fork-release)"
echo "fork-release marker: ${marker}"
test "${marker}" = "${CASAOS_RELEASE_TAG}"
core="$(casaos -v)"
echo "casaos -v: ${core}"
test "${core}" = "${CASAOS_TAG}"
version="$(rclone --version | sed -n 1p)"
echo "rclone: ${version}"
test "${version}" = "rclone ${RCLONE_TAG}"

if grep -n 'Job for' install.log; then
  echo 'a service failed its first start'
  exit 1
fi
echo 'no first-start failure in the install log'

port="$(sudo sed -nE 's/^[[:space:]]*HttpPort[[:space:]]*=[[:space:]]*([0-9]+).*/\1/p' /etc/casaos/gateway.ini | sed -n 1p)"
port="${port:-80}"
url="http://127.0.0.1:${port}"
echo "dashboard: ${url}"
code=000
for _ in $(seq 1 30); do
  code="$(curl -s -o /dev/null -w '%{http_code}' "${url}/" || true)"
  [ "${code}" = 200 ] && break
  sleep 2
done
echo "dashboard answers: ${code}"
test "${code}" = 200

for _ in $(seq 1 20); do
  curl -k -fsS -o /dev/null https://127.0.0.1:443/ && break
  sleep 3
done
curl -k -fsS https://127.0.0.1:443/ping | grep pong >/dev/null
echo 'https on 443 answers'

key="$(curl -fsS "${url}/v1/users/status" | jq -r '.data.key')"
test -n "${key}" && test "${key}" != null
curl -fsS -X POST "${url}/v1/users/register" -H 'content-type: application/json' \
  -d "{\"username\":\"smoke\",\"password\":\"Smoke-test-1\",\"key\":\"${key}\"}" >/dev/null
token="$(curl -fsS -X POST "${url}/v1/users/login" -H 'content-type: application/json' \
  -d '{"username":"smoke","password":"Smoke-test-1"}' | jq -r '.data.token.access_token')"
test -n "${token}" && test "${token}" != null
echo 'first user registered and signed in'

for path in /v2/app_management/compose /v1/users/current /v1/storage; do
  code="$(curl -s -o /dev/null -w '%{http_code}' "${url}${path}")"
  echo "${path} without a token: ${code}"
  test "${code}" = 401
done
code="$(curl -s -o /dev/null -w '%{http_code}' "${url}/v1/users/current" -H "Authorization: ${token}")"
echo "/v1/users/current with the token: ${code}"
test "${code}" = 200

echo 'debian 13: all questions answered'
