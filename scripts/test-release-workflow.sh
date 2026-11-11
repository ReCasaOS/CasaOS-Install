#!/usr/bin/env bash
# release.yml must not offer a release to boxes before the install check has
# passed, and must not hand its write token to code it does not name:
#   - what it publishes is always a pre-release, and only a job that waits for
#     the install check makes it the latest release;
#   - every action is named by its commit, not by a tag its owner can move;
#   - no checkout leaves the token behind for the steps that follow.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workflow="${1:-${here}/../.github/workflows/release.yml}"

fail() {
    echo "FAIL: $1" >&2
    exit 1
}

# job <name>: the lines of that job
job() {
    awk -v name="$1" '
        /^  [A-Za-z0-9_-]+:/ { inside = ($1 == name ":") }
        inside' "${workflow}"
}

grep -qE '^ +prerelease: true$' "${workflow}" ||
    fail "the release is not always published as a pre-release"
if grep -E '^ +(draft|make_latest): ' "${workflow}" | grep -vqE ': false$'; then
    fail "the publishing step can make the release a draft or the latest one"
fi

promote="$(job promote)"
[ -n "${promote}" ] || fail "no job promotes the release"
needs="$(printf '%s\n' "${promote}" | grep -E '^    needs:')" || fail "the promotion waits for nothing"
case "${needs}" in
*install-check*) ;;
*) fail "the promotion does not wait for the install check" ;;
esac
if printf '%s\n' "${promote}" | grep -E '^    if:' | grep -qE 'always\(\)|failure\(\)|cancelled\(\)'; then
    fail "the promotion runs even when the install check did not pass"
fi
if job release | grep -vE '^ *#' | grep -qE 'gh release edit|--latest'; then
    fail "the job that publishes also promotes"
fi

unpinned="$(grep -E '^ +(- )?uses: ' "${workflow}" | grep -vE 'uses: (\./|[^@ ]+@[0-9a-f]{40}( |$))' || true)"
[ -z "${unpinned}" ] || fail "actions that are not named by their commit:
${unpinned}"

checkouts="$(grep -cE 'uses: actions/checkout@' "${workflow}" || true)"
without_token="$(grep -cE '^ +persist-credentials: false$' "${workflow}" || true)"
[ "${checkouts}" -gt 0 ] && [ "${checkouts}" = "${without_token}" ] ||
    fail "${checkouts} checkouts, ${without_token} of them leave no token behind"

echo "ok: release.yml publishes a pre-release, promotes it after the install check, and names its actions by commit"
