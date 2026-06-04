#!/usr/bin/env bash
# Compute the full set of image references (registry/repo:tag) to build and push.
#
# This script is intentionally CI-provider agnostic: it reads its inputs from
# environment variables and writes newline-separated image references to stdout.
# A thin CI adapter (e.g. the GitHub composite action in
# .github/actions/docker-meta) maps provider-specific context into these
# variables and consumes the output. Keeping the logic here means switching or
# adding a CI provider only requires a new adapter, not a rewrite.
#
# Inputs (environment variables):
#   IMAGES          Comma- or whitespace-separated image repositories WITHOUT a
#                   tag, e.g. "ghcr.io/owner/app docker.io/owner/app".
#   REF_TYPE        "tag" or "branch".
#   REF_NAME        The git ref name, e.g. "v1.2.3" or "main".
#   SHA             The full git commit SHA (a short form is derived from it).
#   DEFAULT_BRANCH  Integration branch whose builds are published as "latest".
#                   Default: "main".
#
# Output (stdout): one "registry/repo:tag" per line.
set -euo pipefail

: "${IMAGES:?IMAGES is required}"
: "${REF_TYPE:?REF_TYPE is required}"
: "${REF_NAME:?REF_NAME is required}"
: "${SHA:?SHA is required}"
DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"

# Normalise the image list: accept commas, newlines or spaces as separators.
# Newlines are converted up front because `read` would otherwise stop at the
# first one (the workflow passes images as a newline-separated block).
normalised="${IMAGES//,/ }"
read -r -a images <<< "${normalised//$'\n'/ }"

short_sha="${SHA:0:7}"

# Bare tags (without the repository prefix) that we want to apply.
tags=()

if [[ "${REF_TYPE}" == "tag" && "${REF_NAME}" =~ ^v?([0-9]+)\.([0-9]+)\.([0-9]+)(.*)$ ]]; then
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
    patch="${BASH_REMATCH[3]}"
    suffix="${BASH_REMATCH[4]}"  # e.g. "-rc1" on a pre-release tag

    if [[ -n "${suffix}" ]]; then
        # Pre-release: publish only the exact, immutable version. Never move the
        # floating major/minor tags onto an unstable build.
        tags+=( "${major}.${minor}.${patch}${suffix}" )
    else
        # Stable release: floating version tags only. "latest" deliberately does
        # NOT track releases — it follows the integration branch (see below).
        tags+=( "${major}.${minor}.${patch}" )
        tags+=( "${major}.${minor}" )
        tags+=( "${major}" )
    fi
elif [[ "${REF_TYPE}" == "branch" && "${REF_NAME}" == "${DEFAULT_BRANCH}" ]]; then
    # Integration branch (e.g. develop): its newest build is published as "latest".
    tags+=( "latest" )
else
    # Any other branch: a tag named after the (sanitised) branch.
    tags+=( "${REF_NAME//[^a-zA-Z0-9._-]/-}" )
fi

# Always include an immutable, commit-pinned tag for traceability.
tags+=( "sha-${short_sha}" )

for image in "${images[@]}"; do
    [[ -z "${image}" ]] && continue
    for tag in "${tags[@]}"; do
        printf '%s:%s\n' "${image}" "${tag}"
    done
done
