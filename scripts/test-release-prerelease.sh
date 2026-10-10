#!/usr/bin/env bash
# release-is-prerelease.sh must say "true" only for a checkout that carries
# release/PRERELEASE: a wrong "true" would hide a release from every box, a
# wrong "false" would offer an unproven one to all of them.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
tmp="$(mktemp -d)"
mkdir "${tmp}/release"

check() { # check <expected> <root> <what it is>
    local got
    got="$(bash "${here}/release-is-prerelease.sh" "$2")"
    if [ "${got}" != "$1" ]; then
        echo "FAIL: ${3}: said '${got}', wanted '$1'" >&2
        exit 1
    fi
}

check false "${tmp}" "a checkout without the marker is a release"
: >"${tmp}/release/PRERELEASE"
check true "${tmp}" "a checkout with the marker is a pre-release"
mkdir "${tmp}/release/PRERELEASE.d"
check true "${tmp}" "other files next to the marker change nothing"
rmdir "${tmp}/release/PRERELEASE.d"
rm "${tmp}/release/PRERELEASE"
check false "${tmp}" "removing the marker makes it a release again"
mkdir "${tmp}/release/PRERELEASE"
check false "${tmp}" "a directory named like the marker is not the marker"
rmdir "${tmp}/release/PRERELEASE"
check false "${tmp}/no-such-checkout" "a path without a release directory is a release"
rmdir "${tmp}/release"
rmdir "${tmp}"
echo "ok: release-is-prerelease.sh tells a pre-release from a release"
