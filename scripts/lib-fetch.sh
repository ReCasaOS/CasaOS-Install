# The download of a release asset, shared by scripts/build-release-bundle.sh and
# scripts/stage-modules.sh: sourced, not run. The caller defines fail().

# fetch_tarball <url> <file>
# Downloads a gzip tarball to file. An empty body or one that is not a gzip
# tarball is refused rather than kept.
fetch_tarball() {
    local url="$1" file="$2"

    # GitHub's release downloads answer 5xx in bursts (2026-11-08): curl retries those, never a missing file
    curl -fsSL --retry 5 --retry-delay 10 -o "${file}" "${url}" || fail "Failed to download ${url}."
    [[ -s "${file}" ]] || fail "Empty download: ${url}"
    # read the archive from stdin: the shell opens the path, so tar never
    # parses it, and a keep directory that is a Windows drive-letter path is
    # not taken for a remote host:path.
    tar -tzf - <"${file}" >/dev/null 2>&1 || fail "Not a gzip tarball: ${url}"
}
