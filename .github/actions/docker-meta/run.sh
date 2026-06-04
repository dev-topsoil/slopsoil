#!/usr/bin/env bash
# GitHub Actions adapter for the provider-agnostic ci/docker metadata scripts.
#
# It runs tags.sh and labels.sh and exposes their stdout as the composite
# action's `tags` and `labels` outputs. The GitHub-specific glue (writing to
# $GITHUB_OUTPUT) lives here, not in ci/docker/*, so the core scripts stay
# portable across CI systems.
set -euo pipefail

repo_root="${GITHUB_WORKSPACE:-$(git rev-parse --show-toplevel)}"

tags="$("${repo_root}/ci/docker/tags.sh")"
labels="$("${repo_root}/ci/docker/labels.sh")"

# Multi-line step outputs use a randomised heredoc delimiter to avoid the output
# being truncated or injected if a value happened to contain the delimiter.
delim="EOF_$(openssl rand -hex 8)"
{
    printf 'tags<<%s\n%s\n%s\n' "${delim}" "${tags}" "${delim}"
    printf 'labels<<%s\n%s\n%s\n' "${delim}" "${labels}" "${delim}"
} >> "${GITHUB_OUTPUT}"
