#!/usr/bin/env bash
# release.yml must not offer a release to boxes before the install check has
# passed, and must not hand its write token to code it does not name:
#   - what it publishes is always a pre-release, and only a job that waits for
#     the install check makes it the latest release;
#   - every action is named by its commit, not by a tag its owner can move;
#   - no checkout leaves the token behind for the steps that follow;
#   - install.sh is signed in a job of its own, in the release-signing
#     environment, after the tests and before anything is published, and its
#     signature is published next to it.
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
if job publish | grep -vE '^ *#' | grep -qE 'gh release edit|--latest'; then
    fail "the job that publishes also promotes"
fi

# needs_of <job>: the needs line of that job
needs_of() { job "$1" | grep -E '^    needs:' || true; }

for name in assemble sign publish; do
    [ -n "$(job "${name}")" ] || fail "no ${name} job"
done
[ -z "$(job release)" ] || fail "the old release job is still there"
job sign | grep -qE '^    environment: release-signing$' || fail "the sign job is not in the release-signing environment"
case "$(needs_of sign)" in *assemble*) ;; *) fail "the sign job does not wait for the assembly and its tests" ;; esac
needs_of publish | grep -qE '[[:space:][,]sign([],[:space:]]|$)' || fail "the publish job does not wait for the signature"
if job publish | grep -E '^    if:' | grep -qE 'always\(\)|failure\(\)|cancelled\(\)'; then
    fail "the publication runs even when the signature did not pass"
fi
# publish checks that it publishes the install.sh that was signed, and that the
# build is recent: the automatic update counts its one-day wait from published_at
job sign | grep -qE '^      install_sha256: \$\{\{ steps\.[a-z_-]+\.outputs\.sha256 \}\}$' || fail "the sign job gives no digest of what it signed"
job publish | grep -qE 'needs\.sign\.outputs\.install_sha256' || fail "the publish job does not compare install.sh with what was signed"
job publish | grep -qF 'now - 21600' || fail "the publish job does not refuse a build older than 6 hours"
job publish | grep -qF 'the build is older than 6 hours: use Re-run all jobs to rebuild and sign again' || fail "the publish job does not say how to rebuild an old build"
job assemble | grep -qE '^          retention-days: 30$' || fail "dist is not kept for 30 days, as long as an approval can wait"
case "$(needs_of install-check)" in *publish*) ;; *) fail "the install check does not wait for the publication" ;; esac
job publish | grep -qE '^ +dist/install\.sh\.sig$' || fail "install.sh.sig is not among the published files"
job sign | grep -qE 'verify --pub release/installer-signing\.pub' || fail "the sign job does not check its signature with release/installer-signing.pub"
# the private key is given to the sign job only
others="$(grep -nE 'secrets\.INSTALLER_SIGNING_KEY' "${workflow}" | wc -l)"
in_sign="$(job sign | grep -cE 'secrets\.INSTALLER_SIGNING_KEY' || true)"
[ "${in_sign}" -ge 1 ] && [ "${others}" = "${in_sign}" ] || fail "the signing key is given to a job other than sign, or to none"
# only what publishes or promotes can write to the repository
grep -qE '^  contents: read$' "${workflow}" || fail "the workflow's own permissions are not read only"
for name in assemble sign install-check; do
    if job "${name}" | grep -qE '^ +contents: write$'; then fail "the ${name} job can write to the repository"; fi
done

# build provenance and an SBOM: attested by the job that publishes, which alone
# gets the OIDC token
for p in id-token attestations; do
    job publish | grep -qE "^      ${p}: write$" || fail "the publish job cannot write ${p}"
    for name in assemble sign install-check promote; do
        if job "${name}" | grep -qE "^ +${p}: write$"; then fail "the ${name} job can write ${p}"; fi
    done
done
for a in attest-build-provenance attest-sbom sbom-action; do
    job publish | grep -qE "uses: [^ ]*${a}@" || fail "the publish job does not use ${a}"
done
job publish | grep -qE '^ +dist/install\.sh\.sig$' &&
    job publish | grep -qE '^ +subject-path: \|$' || fail "the publish job attests no archive"
job publish | grep -qE '^ +installer-.*\.spdx\.json$' || fail "the SBOM is not among the published files"

# a release ships only from a tag: a workflow_dispatch is a dry run of assemble
for name in sign publish; do
    job "${name}" | grep -qxF "    if: github.ref_type == 'tag'" || fail "the ${name} job runs on more than a tag"
done

# step_names <job>: the names of that job's steps, in order
step_names() { job "$1" | sed -n 's/^      - name: //p'; }

# right after the bundle is assembled, the pinned core has to trust the key of
# release/installer-signing.pub, and a tagged install.sh has to announce its tag
gate="Check that the pinned core trusts the signing key"
# held in a variable: grep -q would stop reading a long job and fail its pipe under pipefail
assemble="$(job assemble)"
[ "$(step_names assemble | grep -A1 -xF 'Assemble the release bundle' | tail -n 1)" = "${gate}" ] ||
    fail "the check of the core's keys does not come right after the assembly"
grep -qF 'work/CasaOS/pkg/installersig/keys.go' <<<"${assemble}" || fail "assemble does not look at the keys the pinned core trusts"
grep -qF 'release/installer-signing.pub | head -n 1' <<<"${assemble}" || fail "assemble does not read the key of release/installer-signing.pub"
grep -qF "grep -qE '^\s*keyA\s*=\s*\"\"' \"\${keys}\"" <<<"${assemble}" || fail "assemble lets through a core whose keyA is empty"
grep -qF '! grep -qx "readonly CASAOS_INSTALL_RELEASE_TAG=\"${GITHUB_REF_NAME}\"" dist/install.sh' <<<"${assemble}" ||
    fail "assemble does not check that a tagged install.sh announces its tag"

# what is published is compared with what was signed after every step that could
# touch dist, just before it is published
[ "$(step_names publish | grep -B1 -xF 'Publish release assets' | head -n 1)" = "Check that what is published is what was signed, and recent" ] ||
    fail "the digest check is not the step just before the publication"
files="$(job publish | sed -n '/^          files: |$/,/^          [a-z_]*:/p')"
[ "$(printf '%s\n' "${files}" | grep -xE ' +dist/install\.sh(\.sig)?' | tr -d ' ' | paste -sd' ')" = "dist/install.sh.sig dist/install.sh" ] ||
    fail "install.sh.sig is not published before install.sh"

# release.yml and the install check it calls, which runs with the same token
install_check="${2:-$(dirname "${workflow}")/install-check.yml}"
unpinned="$(grep -HE '^ +(- )?uses: ' "${workflow}" "${install_check}" | grep -vE 'uses: (\./|[^@ ]+@[0-9a-f]{40}( |$))' || true)"
[ -z "${unpinned}" ] || fail "actions that are not named by their commit:
${unpinned}"

checkouts="$(grep -cE 'uses: actions/checkout@' "${workflow}" || true)"
without_token="$(grep -cE '^ +persist-credentials: false$' "${workflow}" || true)"
[ "${checkouts}" -gt 0 ] && [ "${checkouts}" = "${without_token}" ] ||
    fail "${checkouts} checkouts, ${without_token} of them leave no token behind"

echo "ok: release.yml signs install.sh in release-signing, publishes a pre-release with its signature, promotes it after the install check, and names its actions by commit"
