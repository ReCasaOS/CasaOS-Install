#!/usr/bin/env bash

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly INSTALLER_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly WORKSPACE_ROOT="$(cd "${INSTALLER_ROOT}/.." && pwd)"
readonly CASAOS_ROOT="${CASAOS_ROOT:-${WORKSPACE_ROOT}/CasaOS}"
readonly COMPONENT_LOCK="${INSTALLER_ROOT}/release/components.env"

# shellcheck source=../release/components.env
source "${COMPONENT_LOCK}"

readonly OUTPUT_DIR="${1:-${CASAOS_ROOT}/dist/fork}"
readonly ACTUAL_COMMIT="$(git -C "${CASAOS_ROOT}" rev-parse HEAD)"
readonly BUILD_DATE="$(git -C "${CASAOS_ROOT}" show -s --format=%cI HEAD)"

if [[ "${ACTUAL_COMMIT}" != "${CASAOS_COMMIT}" ]]; then
    echo "CasaOS is at ${ACTUAL_COMMIT}; expected ${CASAOS_COMMIT}." >&2
    exit 1
fi

if ! git -C "${CASAOS_ROOT}" diff --quiet -- || ! git -C "${CASAOS_ROOT}" diff --cached --quiet --; then
    echo "CasaOS has uncommitted tracked changes; refusing to build it." >&2
    exit 1
fi

mkdir -p "${OUTPUT_DIR}"
rm -f \
    "${OUTPUT_DIR}/casaos-amd64" \
    "${OUTPUT_DIR}/casaos-arm64" \
    "${OUTPUT_DIR}/casaos-arm-7" \
    "${OUTPUT_DIR}/metadata.json"

build_target() {
    local target_arch="$1"
    local goarch="$2"
    local goarm="${3:-}"
    local output_file="${OUTPUT_DIR}/casaos-${target_arch}"

    echo "Building CasaOS for linux/${target_arch}..."
    (
        cd "${CASAOS_ROOT}"
        CGO_ENABLED=0 \
        GOOS=linux \
        GOARCH="${goarch}" \
        GOARM="${goarm}" \
        go build \
            -buildvcs=false \
            -trimpath \
            -tags "musl netgo osusergo" \
            -ldflags "-s -w -X main.commit=${ACTUAL_COMMIT} -X main.date=${BUILD_DATE} -X github.com/ReCasaOS/CasaOS/common.VERSION=${CASAOS_TAG#v}" \
            -o "${output_file}" \
            .
    )
}

build_target amd64 amd64
build_target arm64 arm64
build_target arm-7 arm 7

printf '{"commit":"%s","built_at":"%s","go":"%s"}\n' \
    "${ACTUAL_COMMIT}" \
    "${BUILD_DATE}" \
    "$(go version | awk '{print $3}')" \
    >"${OUTPUT_DIR}/metadata.json"

echo "CasaOS binaries written to ${OUTPUT_DIR}"
