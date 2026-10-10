#!/usr/bin/env bash
# Prints "true" when the checkout carries release/PRERELEASE, "false" otherwise.
# release.yml publishes the installer release as a GitHub pre-release when it
# prints true: boxes and the one-line install command read releases/latest,
# which GitHub works out without pre-releases, so a candidate can be installed
# by its tag and proven before anybody is offered it.
set -euo pipefail

root="${1:-.}"
if [ -f "${root}/release/PRERELEASE" ]; then
    echo true
else
    echo false
fi
