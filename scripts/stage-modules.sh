#!/usr/bin/env bash
#
# Fetches what the release of each module of MODULES_ENV published, for
# scripts/build-modules-catalogue.py build to judge:
#
#   stage-modules.sh MODULES_ENV STAGE_DIR
#
# For each module, STAGE_DIR/<name>/ gets the checksums.txt of its release and
# the two archives that file names, each checked against the SHA-256 it lists;
# the archives of a module of another owner than ReCasaOS also against the
# SHA-256 that MODULES_ENV pins for each, which is what was reviewed.
# One module that cannot be fetched or does not match stops the whole run, with
# the module named. scripts/build-release-bundle.sh runs it for a release, and
# the repository of the online catalogue runs it from a checkout of this one: it
# needs only lib-fetch.sh and build-modules-catalogue.py beside it.
#
#   CHECKSUMS_BASE_URL   where to fetch <repo>/releases/download/<tag>/ from
#                        (default: https://github.com/ReCasaOS; a file:// URL
#                        reads a local tree)
#   OWNERS_BASE_URL      where to fetch <owner>/<repo>/releases/download/<tag>/
#                        from, for a module of another owner (default:
#                        https://github.com)
#   CARRY_CATALOGUE      a catalogue of schema 3 that was published before (a
#                        file): a module of another owner that it already offers
#                        at the pinned tag is carried over by the builder, and is
#                        not fetched here (default: none)
#   PYTHON               the interpreter of build-modules-catalogue.py (python3)

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly CHECKSUMS_BASE_URL="${CHECKSUMS_BASE_URL:-https://github.com/ReCasaOS}"
readonly OWNERS_BASE_URL="${OWNERS_BASE_URL:-https://github.com}"
readonly CARRY_CATALOGUE="${CARRY_CATALOGUE:-}"
readonly PYTHON="${PYTHON:-python3}"

fail() {
    echo "$*" >&2
    exit 1
}

# shellcheck source=lib-fetch.sh
source "${SCRIPT_DIR}/lib-fetch.sh"

[[ $# -eq 2 ]] || { echo "usage: stage-modules.sh MODULES_ENV STAGE_DIR" >&2; exit 2; }
modules_env="$1"
stage="$2"

carry=()
[[ -z "${CARRY_CATALOGUE}" ]] || carry=(--carry "${CARRY_CATALOGUE}")
list="$("${PYTHON}" "${SCRIPT_DIR}/build-modules-catalogue.py" list "${modules_env}" ${carry[@]+"${carry[@]}"})" ||
    fail "${modules_env} is not valid, or cannot be read, or CARRY_CATALOGUE is not a readable catalogue of schema 3, or ${PYTHON} could not run."

# (owner and component passed the builder's patterns before `list` printed them)
while read -r name component tag commit tier owner; do
    [[ -n "${name}" ]] || continue
    base="${CHECKSUMS_BASE_URL}"
    [[ "${owner}" == ReCasaOS ]] || base="${OWNERS_BASE_URL}/${owner}"
    mkdir -p "${stage}/${name}"
    curl -fsSL --retry 5 --retry-delay 10 -o "${stage}/${name}/checksums.txt" "${base}/${component}/releases/download/${tag}/checksums.txt" ||
        fail "Failed to download the checksums of module ${name} (${component} ${tag})."
    archives="$("${PYTHON}" "${SCRIPT_DIR}/build-modules-catalogue.py" archives "${stage}/${name}/checksums.txt" "${tag}")" ||
        fail "Module ${name}: its checksums.txt names no usable archives."
    arches=(amd64 arm64) # the order in which `archives` prints them
    i=0
    for archive in ${archives}; do
        arch="${arches[i]}"
        i=$((i + 1))
        fetch_tarball "${base}/${component}/releases/download/${tag}/${archive}" "${stage}/${name}/${archive}"
        want="$(awk -v a="${archive}" '($2 == a || $2 == "*" a) { print $1; exit }' "${stage}/${name}/checksums.txt")"
        got="$(sha256sum "${stage}/${name}/${archive}" | awk '{ print $1 }')"
        [[ -n "${want}" && "${got}" == "${want}" ]] || fail "Module ${name}: ${archive} is not the archive that checksums.txt names."
        [[ "${owner}" != ReCasaOS ]] || continue
        pinned="$("${PYTHON}" "${SCRIPT_DIR}/build-modules-catalogue.py" digest "${modules_env}" "${name}" "${arch}")" ||
            fail "Module ${name}: the digest of its ${arch} archive cannot be read from ${modules_env}."
        [[ -n "${pinned}" && "${got}" == "${pinned}" ]] || fail "Module ${name}: the ${arch} archive ${archive} is not the one ${modules_env} pins."
    done
done <<<"${list}"
