#!/usr/bin/env bash
#
# Checks scripts/stage-modules.sh, the fetch of the modules' releases that the
# release build and the repository of the online catalogue share, without a
# network: the releases are a tree of files that curl reads over file://.
#
#   bash scripts/test-stage-modules.sh
#
# STAGE_MODULES_UNDER_TEST=<path> checks another copy (a mutant, to see that
# each check below can say no); it must sit beside lib-fetch.sh and
# build-modules-catalogue.py, as the script does.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE_MODULES="${STAGE_MODULES_UNDER_TEST:-${ROOT}/scripts/stage-modules.sh}"
WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

pass() { echo "ok: $*"; }

TAG=v0.1.1
RELEASE="${WORK}/net/CasaOS-VirtManagement/releases/download/${TAG}"

# publish: two archives that hold a module.json, and the checksums.txt that names them
publish() {
    local arch
    rm -rf "${RELEASE}" "${WORK}/content"
    mkdir -p "${RELEASE}" "${WORK}/content"
    echo '{"name": "virt"}' >"${WORK}/content/module.json"
    : >"${RELEASE}/checksums.txt"
    for arch in amd64 arm64; do
        tar -C "${WORK}/content" -cf - module.json | gzip -n >"${RELEASE}/linux-${arch}-casaos-virt-management-${TAG}.tar.gz"
        (cd "${RELEASE}" && sha256sum "linux-${arch}-casaos-virt-management-${TAG}.tar.gz") >>"${RELEASE}/checksums.txt"
    done
}

cat >"${WORK}/modules.env" <<EOF
# modules
MODULE_VIRT_TAG=${TAG}
MODULE_VIRT_COMMIT=$(printf 'a%.0s' $(seq 40))
MODULE_VIRT_COMPONENT=CasaOS-VirtManagement
MODULE_VIRT_TIER=core
EOF

# stage: the script run as another repository runs it, from elsewhere, with its two variables
stage() {
    rm -rf "${WORK}/stage"
    (cd / && CHECKSUMS_BASE_URL="file://${WORK}/net" PYTHON="${PYTHON:-python3}" bash "${STAGE_MODULES}" "$@") >"${WORK}/out" 2>"${WORK}/err"
}

publish
stage "${WORK}/modules.env" "${WORK}/stage" || fail "a good release was not staged: $(cat "${WORK}/err")"
for f in checksums.txt "linux-amd64-casaos-virt-management-${TAG}.tar.gz" "linux-arm64-casaos-virt-management-${TAG}.tar.gz"; do
    cmp -s "${RELEASE}/${f}" "${WORK}/stage/virt/${f}" || fail "${f} is not in the stage of virt as it was published"
done
[ "$(ls "${WORK}/stage")" = virt ] && [ "$(ls "${WORK}/stage/virt" | wc -l)" -eq 3 ] || fail "the stage holds something else than the three files of virt: $(ls -R "${WORK}/stage")"
pass "a good release is staged in STAGE_DIR/<name>/, as published"

publish
echo "not the archive" | gzip -n >"${RELEASE}/linux-arm64-casaos-virt-management-${TAG}.tar.gz"
! stage "${WORK}/modules.env" "${WORK}/stage" || fail "an archive that is not the one checksums.txt names was staged"
grep -q "Module virt: linux-arm64-casaos-virt-management-${TAG}.tar.gz is not the archive that checksums.txt names" "${WORK}/err" ||
    fail "the refusal of an archive does not name the module and the archive: $(cat "${WORK}/err")"
pass "an archive whose sum does not match fails, naming the module"

publish
rm "${RELEASE}/checksums.txt"
! stage "${WORK}/modules.env" "${WORK}/stage" || fail "a release with no checksums.txt was staged"
grep -q "Failed to download the checksums of module virt" "${WORK}/err" || fail "a missing checksums.txt is not said: $(cat "${WORK}/err")"
pass "a release with no checksums.txt fails, naming the module"

publish
rm "${RELEASE}/linux-amd64-casaos-virt-management-${TAG}.tar.gz"
! stage "${WORK}/modules.env" "${WORK}/stage" || fail "a release with an archive missing was staged"
grep -q "Failed to download file://.*linux-amd64-casaos-virt-management-${TAG}.tar.gz" "${WORK}/err" || fail "a missing archive is not said: $(cat "${WORK}/err")"
pass "a release with an archive missing fails, naming the archive"

publish
set +e
stage "${WORK}/modules.env"
code=$?
set -e
[ "${code}" -eq 2 ] && grep -q '^usage: ' "${WORK}/err" || fail "one argument is not a usage error (exit ${code})"
pass "the wrong number of arguments is a usage error"
