#!/usr/bin/env bash
# Prints "true" when the checkout carries release/PRERELEASE, "false" otherwise.
# release.yml always publishes the installer release as a GitHub pre-release:
# boxes and the one-line install command read releases/latest, which GitHub
# works out without pre-releases. When this prints false the release is made
# the latest one as soon as the install check has passed; when it prints true
# it stays a pre-release until somebody promotes it by hand.
set -euo pipefail

root="${1:-.}"
if [ -f "${root}/release/PRERELEASE" ]; then
    echo true
else
    echo false
fi
