#!/usr/bin/env bash
#
# Assemble a CasaOS-Install release from components that have already been
# released by their own repositories.
#
# Nothing is compiled here. Each Go service publishes its own tarballs and a
# checksums.txt from its own release workflow; this script fetches those
# checksums and writes them into install.sh, so the digests the installer
# verifies against are never typed by hand. Two consecutive releases once
# existed only to fix a mistyped digest - that is the failure this removes.
# The dashboard's release and IceWhale's App Store release publish none; those
# two digests are computed here from the tarball as published (see
# compute_checksum).
#
# The App Store seed is the one asset still fetched from IceWhale, and only at
# release time: it is the snapshot the installer needs for a new box to come up
# with a populated store. It is mirrored into OUTPUT_DIR, published as an asset
# of our own release, and pinned by the digest of the copy we publish, so a new
# install no longer depends on IceWhale's release still existing. This changes
# nothing about who curates the catalogue: AppManagement keeps polling
# IceWhale's live store feed at run time.
#
# What it produces, in OUTPUT_DIR:
#   install.sh            the installer with every tag and digest filled in
#   casaos-uninstall      the uninstaller, served from our release rather than
#                         from a third party's web server; its digest is
#                         written into install.sh like the packages'
#   linux-all-appstore-<tag>.tar.gz
#                         IceWhale's App Store seed as published, mirrored so a
#                         new install does not depend on their release
#   linux-zz-casaos-compat-overlay-<tag>.tar.gz
#                         the setup scripts of the six components, plus the
#                         release marker read by the in-app updater
#   version.json          what the in-app updater polls: the tag, when this
#                         bundle was built, and the CHANGELOG.md section of
#                         this release
#   release-notes.md      that same section, the body of the GitHub release
#   components.lock       the exact commits and tags this release was cut from
#   checksums.txt         digests of everything above
#
# Inputs:
#   release/components.env   tags and commits of every component
#   WORKSPACE_ROOT           directory holding the six component checkouts,
#                            each at the commit pinned in components.env
#                            (default: the parent of this repository)
#   GITHUB_OWNER             account the component releases live under
#                            (default: ReCasaOS)
#   CHECKSUMS_BASE_URL       where to fetch <repo>/releases/download/<tag>/checksums.txt
#                            of the six Go services, and the dashboard tarball,
#                            from; defaults to GitHub under GITHUB_OWNER. A
#                            file:// URL pointing at a local tree lets the whole
#                            thing be exercised before any release exists.
#   UPSTREAM_BASE_URL        where to fetch the App Store seed tarball from
#                            (default: https://github.com/IceWhaleTech)

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly INSTALLER_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
readonly WORKSPACE_ROOT="${WORKSPACE_ROOT:-$(cd "${INSTALLER_ROOT}/.." && pwd)}"
readonly COMPONENT_LOCK="${INSTALLER_ROOT}/release/components.env"
readonly GITHUB_OWNER="${GITHUB_OWNER:-ReCasaOS}"
readonly CHECKSUMS_BASE_URL="${CHECKSUMS_BASE_URL:-https://github.com/${GITHUB_OWNER}}"
readonly UPSTREAM_BASE_URL="${UPSTREAM_BASE_URL:-https://github.com/IceWhaleTech}"
readonly RCLONE_BASE_URL="${RCLONE_BASE_URL:-https://github.com/rclone}"

# shellcheck source=../release/components.env
source "${COMPONENT_LOCK}"

readonly RELEASE_TAG="${CASAOS_RELEASE_TAG}"
readonly OUTPUT_DIR="${1:-${INSTALLER_ROOT}/dist}"
readonly OVERLAY_FILE="linux-zz-casaos-compat-overlay-${RELEASE_TAG}.tar.gz"
readonly APPSTORE_SEED_FILE="linux-all-appstore-${CASAOS_APPSTORE_TAG}.tar.gz"

readonly COMPONENT_DIRS=(
    "CasaOS-Gateway:${CASAOS_GATEWAY_COMMIT}"
    "CasaOS-UserService:${CASAOS_USER_SERVICE_COMMIT}"
    "CasaOS:${CASAOS_COMMIT}"
    "CasaOS-LocalStorage:${CASAOS_LOCAL_STORAGE_COMMIT}"
    "CasaOS-MessageBus:${CASAOS_MESSAGE_BUS_COMMIT}"
    "CasaOS-AppManagement:${CASAOS_APP_MANAGEMENT_COMMIT}"
)

fail() {
    echo "$*" >&2
    exit 1
}

verify_repo_commit() {
    local repo_name="$1"
    local expected_commit="$2"
    local repo_path="${WORKSPACE_ROOT}/${repo_name}"
    local actual_commit

    [[ -d "${repo_path}/.git" ]] || fail "${repo_name} is not checked out at ${repo_path}."

    actual_commit="$(git -C "${repo_path}" rev-parse HEAD)"
    if [[ "${actual_commit}" != "${expected_commit}"* ]]; then
        fail "${repo_name} is at ${actual_commit}; expected ${expected_commit}."
    fi

    if ! git -C "${repo_path}" diff --quiet -- || ! git -C "${repo_path}" diff --cached --quiet --; then
        fail "${repo_name} has uncommitted tracked changes; refusing to package it."
    fi
}

for entry in "${COMPONENT_DIRS[@]}"; do
    verify_repo_commit "${entry%%:*}" "${entry#*:}"
done

readonly STAGING_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/casaos-release-bundle.XXXXXX")"
trap 'rm -rf "${STAGING_ROOT}"' EXIT

mkdir -p "${OUTPUT_DIR}"

# fetch_checksum <base url> <repo> <tag> <asset> [checksum file]
# Prints the SHA-256 of one asset as published by that repository's release.
# The file is checksums.txt, as GoReleaser names it, unless the release names
# it otherwise (rclone publishes SHA256SUMS).
fetch_checksum() {
    local base="$1" repo="$2" tag="$3" asset="$4" file="${5:-checksums.txt}"
    local url="${base}/${repo}/releases/download/${tag}/${file}"
    local sum

    sum="$(curl -fsSL "${url}" | awk -v a="${asset}" '($2 == a || $2 == "*" a) { print $1; exit }')"
    [[ -n "${sum}" ]] || fail "No digest for ${asset} in ${url}."
    [[ "${sum}" =~ ^[0-9a-f]{64}$ ]] || fail "Malformed digest for ${asset}: ${sum}"

    echo "${sum}"
}

# compute_checksum <base url> <repo> <tag> <asset> [keep dir]
# Prints the SHA-256 of one asset computed from the asset itself, for the two
# releases that publish no checksums.txt (the dashboard, the App Store). This
# pins the asset as it is at bundle time: an asset replaced after the bundle is
# refused by every later install; an asset already replaced when the bundle is
# made is pinned as found. An empty body or one that is not a gzip tarball is
# refused rather than pinned. A keep directory leaves the download there to be
# republished, so the digest pinned is the digest of the copy we serve.
compute_checksum() {
    local base="$1" repo="$2" tag="$3" asset="$4" keep_dir="${5:-${STAGING_ROOT}}"
    local url="${base}/${repo}/releases/download/${tag}/${asset}"
    local file="${keep_dir}/${asset}"

    curl -fsSL -o "${file}" "${url}" || fail "Failed to download ${url}."
    [[ -s "${file}" ]] || fail "Empty download: ${url}"
    # read the archive from stdin: the shell opens the path, so tar never
    # parses it, and a keep directory that is a Windows drive-letter path is
    # not taken for a remote host:path.
    tar -tzf - <"${file}" >/dev/null 2>&1 || fail "Not a gzip tarball: ${url}"

    sha256sum "${file}" | awk '{ print $1 }'
}

# create_archive <stage dir> <output file>
# The overlay's digest is written into install.sh, so the same tree must give
# the same bytes wherever it is packaged: entries are sorted by name, owner and
# group are 0 with no names, and every mtime is a fixed instant in UTC rather
# than the local clock; gzip -n leaves the name and time out of the header.
# The format is gnu, not ustar, because ustar is not stable across tar
# releases: for a regular file tar 1.34 writes devmajor and devminor as octal
# zeros where tar 1.35 leaves them null, which is 259 differing bytes and a
# different digest for the same tree. Both versions leave them null in gnu
# format, so the release runner and a local rebuild agree.
create_archive() {
    local stage_dir="$1"
    local output_file="$2"

    COPYFILE_DISABLE=1 tar --format=gnu --sort=name --owner=0 --group=0 --numeric-owner \
        --mtime='2020-01-01 00:00:00 UTC' -C "${stage_dir}" -cf - build | gzip -n >"${output_file}"
}

package_overlay() {
    local stage_dir="${STAGING_ROOT}/overlay"
    local setup_dir="${stage_dir}/build/scripts/setup/script.d"

    mkdir -p "${setup_dir}"
    install -m 0755 "${WORKSPACE_ROOT}/CasaOS-Gateway/build/scripts/setup/script.d/01-setup-gateway.sh" "${setup_dir}/"
    install -m 0755 "${WORKSPACE_ROOT}/CasaOS-UserService/build/scripts/setup/script.d/02-setup-user-service.sh" "${setup_dir}/"
    install -m 0755 "${WORKSPACE_ROOT}/CasaOS/build/scripts/setup/script.d/03-setup-casaos.sh" "${setup_dir}/"
    install -m 0755 "${WORKSPACE_ROOT}/CasaOS-LocalStorage/build/scripts/setup/script.d/04-setup-local-storage.sh" "${setup_dir}/"
    install -m 0755 "${WORKSPACE_ROOT}/CasaOS-MessageBus/build/scripts/setup/script.d/05-setup-message-bus.sh" "${setup_dir}/"
    install -m 0755 "${WORKSPACE_ROOT}/CasaOS-AppManagement/build/scripts/setup/script.d/06-setup-app-management.sh" "${setup_dir}/"

    mkdir -p "${stage_dir}/build/sysroot/var/lib/casaos"
    printf '%s\n' "${RELEASE_TAG}" >"${stage_dir}/build/sysroot/var/lib/casaos/fork-release"

    create_archive "${stage_dir}" "${OUTPUT_DIR}/${OVERLAY_FILE}"
    echo "Packaged compatibility overlay as ${OVERLAY_FILE}"
}

# fill_installer <output install.sh>
# Every value install.sh needs at run time is a placeholder in the committed
# script and is written here from components.env and the published checksums.
# Each digest goes through an assignment, not straight into sed's argument: a
# failed command substitution inside an argument is not a failure of the
# command under set -e, and would have written an empty digest.
fill_installer() {
    local target="$1"
    local key value sum

    install -m 0755 "${INSTALLER_ROOT}/install.sh" "${target}"

    while IFS='=' read -r key value; do
        [[ -n "${key}" && "${key}" != \#* ]] || continue
        sed -i "s|__${key}__|${value}|g" "${target}"
    done <"${COMPONENT_LOCK}"

    local overlay_sum
    overlay_sum="$(sha256sum "${OUTPUT_DIR}/${OVERLAY_FILE}" | awk '{ print $1 }')"
    sed -i "s|__CASAOS_COMPAT_OVERLAY_SHA256__|${overlay_sum}|g" "${target}"

    # the uninstaller as it will be served, already copied into OUTPUT_DIR
    local uninstall_sum
    uninstall_sum="$(sha256sum "${OUTPUT_DIR}/casaos-uninstall" | awk '{ print $1 }')"
    sed -i "s|__CASAOS_UNINSTALL_SHA256__|${uninstall_sum}|g" "${target}"

    # base url, placeholder stem, repository, tag, asset name stem: one line
    # per package released per architecture with a checksums.txt
    local spec base stem repo tag name
    local arch upper
    for arch in amd64 arm64 arm-7; do
        upper="${arch//-/}"
        upper="${upper^^}"

        for spec in \
            "${CHECKSUMS_BASE_URL} CASAOS_GATEWAY_SHA256 CasaOS-Gateway ${CASAOS_GATEWAY_TAG} casaos-gateway" \
            "${CHECKSUMS_BASE_URL} CASAOS_MESSAGE_BUS_SHA256 CasaOS-MessageBus ${CASAOS_MESSAGE_BUS_TAG} casaos-message-bus" \
            "${CHECKSUMS_BASE_URL} CASAOS_USER_SERVICE_SHA256 CasaOS-UserService ${CASAOS_USER_SERVICE_TAG} casaos-user-service" \
            "${CHECKSUMS_BASE_URL} CASAOS_LOCAL_STORAGE_SHA256 CasaOS-LocalStorage ${CASAOS_LOCAL_STORAGE_TAG} casaos-local-storage" \
            "${CHECKSUMS_BASE_URL} CASAOS_APP_MANAGEMENT_SHA256 CasaOS-AppManagement ${CASAOS_APP_MANAGEMENT_TAG} casaos-app-management" \
            "${CHECKSUMS_BASE_URL} CASAOS_CORE_SHA256 CasaOS ${CASAOS_TAG} casaos"; do
            read -r base stem repo tag name <<<"${spec}"
            sum="$(fetch_checksum "${base}" "${repo}" "${tag}" "linux-${arch}-${name}-${tag}.tar.gz")"
            sed -i "s|__${stem}_${upper}__|${sum}|g" "${target}"
        done
    done

    sum="$(compute_checksum "${CHECKSUMS_BASE_URL}" CasaOS-UI "${CASAOS_UI_TAG}" "linux-all-casaos-${CASAOS_UI_TAG}.tar.gz")"
    sed -i "s|__CASAOS_UI_SHA256__|${sum}|g" "${target}"

    # rclone, from its own release. Until now the installer ran rclone.org's
    # install script, which installs whatever is current that day, while
    # claiming a target of v1.61.1 that nothing ever installed -- so every
    # upgrade found a version other than the one it named, removed it, and
    # installed the current one again. It is pinned like everything else now:
    # one version, one digest per architecture, verified before it is unpacked.
    # rclone names the 32-bit ARM build arm-v7 where the components say arm-7.
    local rclone_arch
    for arch in amd64 arm64 arm-7; do
        upper="${arch//-/}"
        upper="${upper^^}"
        rclone_arch="${arch/arm-7/arm-v7}"
        sum="$(fetch_checksum "${RCLONE_BASE_URL}" rclone "${RCLONE_TAG}" "rclone-${RCLONE_TAG}-linux-${rclone_arch}.zip" SHA256SUMS)"
        sed -i "s|__RCLONE_SHA256_${upper}__|${sum}|g" "${target}"
    done
    sum="$(compute_checksum "${UPSTREAM_BASE_URL}" CasaOS-AppStore "${CASAOS_APPSTORE_TAG}" "${APPSTORE_SEED_FILE}" "${OUTPUT_DIR}")"
    sed -i "s|__CASAOS_APPSTORE_SHA256__|${sum}|g" "${target}"

    if grep -qE '__[A-Z][A-Z0-9_]*__' "${target}"; then
        echo "Unfilled placeholders remain in install.sh:" >&2
        grep -oE '__[A-Z][A-Z0-9_]*__' "${target}" | sort -u >&2
        exit 1
    fi

    bash -n "${target}"
}

# release_notes
# Prints the CHANGELOG.md section of this release: its "## [x.y.z]" heading
# up to the next release heading. The updater dialog renders it as Markdown.
release_notes() {
    local heading="## [${RELEASE_TAG#v}]"
    local notes

    notes="$(awk -v h="${heading}" '/^## /{p=(substr($0,1,length(h))==h)} p' "${INSTALLER_ROOT}/CHANGELOG.md")"
    [[ -n "${notes}" ]] || fail "CHANGELOG.md has no section for ${RELEASE_TAG}."

    echo "${notes}"
}

# json_string <text>
# Prints text as a JSON string literal.
# ponytail: escapes what Markdown prose contains; other control characters
# would need jq, which the local machines this runs on do not have.
json_string() {
    local s="$1"

    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\r'/\\r}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\t'/\\t}"

    printf '"%s"' "${s}"
}

write_version_manifest() {
    local notes
    notes="$(release_notes)"
    printf '%s\n' "${notes}" >"${OUTPUT_DIR}/release-notes.md"

    # published_at is when this bundle was built, in UTC: a box that updates
    # itself installs a release only once it is 48 hours old, and never one
    # without it. Cores older than automatic updates ignore the key.
    printf '{\n  "version": "%s",\n  "published_at": "%s",\n  "change_log": %s\n}\n' \
        "${RELEASE_TAG}" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$(json_string "${notes}"$'\n\n'"https://github.com/${GITHUB_OWNER}/CasaOS-Install/releases/tag/${RELEASE_TAG}")" \
        >"${OUTPUT_DIR}/version.json"
}

write_checksums() {
    (
        cd "${OUTPUT_DIR}"
        sha256sum "${OVERLAY_FILE}" "${APPSTORE_SEED_FILE}" install.sh casaos-uninstall components.lock version.json >checksums.txt
        sha256sum install.sh >install.sh.sha256
    )
}

package_overlay
install -m 0755 "${INSTALLER_ROOT}/casaos-uninstall" "${OUTPUT_DIR}/casaos-uninstall"
fill_installer "${OUTPUT_DIR}/install.sh"
install -m 0644 "${COMPONENT_LOCK}" "${OUTPUT_DIR}/components.lock"
write_version_manifest
write_checksums

echo "Release bundle written to ${OUTPUT_DIR}"
