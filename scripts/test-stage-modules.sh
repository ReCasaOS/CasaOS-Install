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
    (cd / && CHECKSUMS_BASE_URL="file://${WORK}/net" OWNERS_BASE_URL="file://${WORK}/owners" PYTHON="${PYTHON:-python3}" bash "${STAGE_MODULES}" "$@") >"${WORK}/out" 2>"${WORK}/err"
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

# ---- a module of another owner: fetched from its own repository, each archive held to checksums.txt and to the digest modules.env pins ----

FOREIGN="${WORK}/owners/inkly/Acme-Module/releases/download/${TAG}"

# publish_foreign <text>: the release of acme in the repository of inkly, whose archives hold a module.json of that text
publish_foreign() {
    local arch
    rm -rf "${FOREIGN}" "${WORK}/foreign"
    mkdir -p "${FOREIGN}" "${WORK}/foreign"
    echo "$1" >"${WORK}/foreign/module.json"
    : >"${FOREIGN}/checksums.txt"
    for arch in amd64 arm64; do
        # (the same text gives the same bytes whenever it is published: the pins of modules.env are taken once)
        tar --mtime=@0 --owner=0 --group=0 --numeric-owner -C "${WORK}/foreign" -cf - module.json | gzip -n >"${FOREIGN}/linux-${arch}-casaos-acme-${TAG}.tar.gz"
        (cd "${FOREIGN}" && sha256sum "linux-${arch}-casaos-acme-${TAG}.tar.gz") >>"${FOREIGN}/checksums.txt"
    done
}

publish
publish_foreign '{"name": "acme"}'
{
    cat "${WORK}/modules.env"
    cat <<EOF
MODULE_ACME_TAG=${TAG}
MODULE_ACME_COMMIT=$(printf 'b%.0s' $(seq 40))
MODULE_ACME_COMPONENT=Acme-Module
MODULE_ACME_TIER=community
MODULE_ACME_OWNER=inkly
MODULE_ACME_SHA256_AMD64=$(sha256sum "${FOREIGN}/linux-amd64-casaos-acme-${TAG}.tar.gz" | awk '{ print $1 }')
MODULE_ACME_SHA256_ARM64=$(sha256sum "${FOREIGN}/linux-arm64-casaos-acme-${TAG}.tar.gz" | awk '{ print $1 }')
EOF
} >"${WORK}/both.env"
stage "${WORK}/both.env" "${WORK}/stage" || fail "a module of another owner was not staged: $(cat "${WORK}/err")"
for f in checksums.txt "linux-amd64-casaos-acme-${TAG}.tar.gz" "linux-arm64-casaos-acme-${TAG}.tar.gz"; do
    cmp -s "${FOREIGN}/${f}" "${WORK}/stage/acme/${f}" || fail "${f} is not in the stage of acme as its owner published it"
done
for f in checksums.txt "linux-amd64-casaos-virt-management-${TAG}.tar.gz" "linux-arm64-casaos-virt-management-${TAG}.tar.gz"; do
    cmp -s "${RELEASE}/${f}" "${WORK}/stage/virt/${f}" || fail "${f} is not in the stage of virt as it was published, beside a module of another owner"
done
[ "$(ls "${WORK}/stage" | tr '\n' ' ')" = "acme virt " ] || fail "the stage holds something else than acme and virt: $(ls "${WORK}/stage")"
pass "a module of another owner is staged from its own repository, and a module of ReCasaOS beside it as before"

# the author replaces the release after the review: checksums.txt agrees with the new archives, modules.env does not
publish_foreign '{"name": "acme", "replaced": true}'
! stage "${WORK}/both.env" "${WORK}/stage" || fail "an archive that is not the one modules.env pins was staged"
grep -qF "Module acme: the amd64 archive linux-amd64-casaos-acme-${TAG}.tar.gz is not the one ${WORK}/both.env pins" "${WORK}/err" ||
    fail "the refusal of an archive the list does not pin does not name the module and the architecture: $(cat "${WORK}/err")"
pass "an archive whose sum checksums.txt names but modules.env does not stops everything, naming the module and the architecture"

# the arm64 archive only
publish_foreign '{"name": "acme"}'
echo "another arm64 archive" | gzip -n >"${FOREIGN}/linux-arm64-casaos-acme-${TAG}.tar.gz"
(cd "${FOREIGN}" && sha256sum "linux-amd64-casaos-acme-${TAG}.tar.gz" "linux-arm64-casaos-acme-${TAG}.tar.gz") >"${FOREIGN}/checksums.txt"
! stage "${WORK}/both.env" "${WORK}/stage" || fail "an arm64 archive that is not the one modules.env pins was staged"
grep -qF "Module acme: the arm64 archive linux-arm64-casaos-acme-${TAG}.tar.gz is not the one ${WORK}/both.env pins" "${WORK}/err" ||
    fail "the refusal of the arm64 archive does not name the architecture: $(cat "${WORK}/err")"
pass "the architecture named is the one of the archive that does not match"

# a module of another owner is never looked for under the base of ReCasaOS's modules
publish_foreign '{"name": "acme"}'
mkdir -p "${WORK}/net/Acme-Module" && cp -r "${WORK}/owners/inkly/Acme-Module/releases" "${WORK}/net/Acme-Module/"
rm -rf "${WORK}/owners"
! stage "${WORK}/both.env" "${WORK}/stage" || fail "a module of another owner was fetched from the base of ReCasaOS"
grep -q "Failed to download the checksums of module acme" "${WORK}/err" || fail "a module of another owner missing from its repository is not said: $(cat "${WORK}/err")"
pass "a module of another owner is fetched only from OWNERS_BASE_URL/<owner>/<component>"

# ---- CARRY_CATALOGUE: a version of another owner that a published catalogue already offers is not fetched again ----

# (the owner's repository is gone, as in the case above: only the file to carry from holds the module)
cat >"${WORK}/carry.json" <<EOF
{"schema": 3, "serial": 7, "generated": "2026-11-25T12:00:00Z", "withdrawn": [], "modules": [{
  "name": "acme", "component": "Acme-Module", "repository": "inkly/Acme-Module", "tier": "community", "tag": "${TAG}", "commit": "$(printf 'b%.0s' $(seq 40))",
  "assets": {
    "amd64": {"url": "https://github.com/ReCasaOS/modules/releases/download/m-acme-${TAG}/linux-amd64-casaos-acme-${TAG}.tar.gz", "sha256": "$(printf 'c%.0s' $(seq 64))"},
    "arm64": {"url": "https://github.com/ReCasaOS/modules/releases/download/m-acme-${TAG}/linux-arm64-casaos-acme-${TAG}.tar.gz", "sha256": "$(printf 'd%.0s' $(seq 64))"}},
  "manifest": {"schema": 1, "name": "acme", "title": "Acme", "description": "A module.", "component": "Acme-Module", "unit": "casaos-acme.service",
    "gateway_route": "/v1/acme_thing", "min_recasaos": "0.5.23", "os": ["debian:12"]}}]}
EOF
publish
CARRY_CATALOGUE="${WORK}/carry.json" stage "${WORK}/both.env" "${WORK}/stage" || fail "a carried module stopped the run: $(cat "${WORK}/err")"
[ "$(ls "${WORK}/stage")" = virt ] || fail "a carried module was staged, or a module that is not carried was not: $(ls "${WORK}/stage")"
pass "a module that CARRY_CATALOGUE offers at the pinned tag is not fetched, and the others are"

CARRY_CATALOGUE="" stage "${WORK}/both.env" "${WORK}/stage" && fail "an empty CARRY_CATALOGUE changed what is fetched"
grep -q "Failed to download the checksums of module acme" "${WORK}/err" || fail "an empty CARRY_CATALOGUE is not the same as none: $(cat "${WORK}/err")"
pass "an empty CARRY_CATALOGUE is none"

echo "{}" >"${WORK}/not-a-catalogue.json"
for carry_file in "${WORK}/not-a-catalogue.json" "${WORK}/nothing.json"; do
    CARRY_CATALOGUE="${carry_file}" stage "${WORK}/both.env" "${WORK}/stage" && fail "${carry_file} was taken for a catalogue to carry from"
    [ "$(wc -l <"${WORK}/err")" -ge 1 ] && grep -q "CARRY_CATALOGUE is not a readable catalogue of schema 3" "${WORK}/err" || fail "a CARRY_CATALOGUE that does not hold is not said: $(cat "${WORK}/err")"
done
pass "a CARRY_CATALOGUE that is not a readable catalogue of schema 3 fails"
