#!/usr/bin/env bash
# A box runs an installer only when its core trusts the key that signed it.
# Builds tools/core-keys against the core in CORE_DIR and fails unless that core
# trusts exactly two distinct keys, one of them the key of PUB_FILE. A core
# without pkg/installersig checks no signature and has no keys to check.
#
#   bash scripts/check-core-keys.sh CORE_DIR PUB_FILE
set -euo pipefail

core="$1"
pub_file="$2"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -d "${core}/pkg/installersig" ]; then
    echo "The pinned core has no pkg/installersig: it checks no installer signature, so it has no keys to check."
    exit 0
fi

pub="$(tr -d '\r' <"${pub_file}" | grep -vE '^[[:space:]]*(#|$)' | head -n 1 || true)"
[ -n "${pub}" ] || {
    echo "${pub_file} holds no public key." >&2
    exit 1
}

keys="$(go -C "${core}" run "${here}/tools/core-keys/main.go")"
count="$(printf '%s' "${keys}" | grep -c . || true)"
distinct="$(printf '%s\n' "${keys}" | sort -u | grep -c . || true)"
echo "The pinned core trusts ${count} installer key(s)."
if [ "${count}" -ne 2 ] || [ "${distinct}" -ne 2 ]; then
    echo "The pinned core has to trust exactly two distinct installer keys." >&2
    exit 1
fi
grep -qxF -- "${pub}" <<<"${keys}" || {
    echo "The pinned core does not trust the key of ${pub_file}." >&2
    exit 1
}
echo "The pinned core trusts the key of ${pub_file}."
