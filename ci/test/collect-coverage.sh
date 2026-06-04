#!/usr/bin/env bash
# GitHub adapter: assemble the artifact the post-coverage workflow needs.
#
# The privileged comment is posted from a separate workflow_run workflow that
# cannot see this run's git context, so everything it needs is bundled into an
# artifact directory here: the rendered coverage report plus the PR number and
# head commit it belongs to.
#
# Inputs (environment variables):
#   OUT_DIR     Directory to write the artifact files into. (default: coverage-report)
#   PR_NUMBER   Pull request number the report belongs to.   (required)
#   HEAD_SHA    Commit the report was generated for.         (required)
set -euo pipefail

repo_root="${GITHUB_WORKSPACE:-$(git rev-parse --show-toplevel)}"
out="${OUT_DIR:-coverage-report}"

mkdir -p "${out}"
"${repo_root}/ci/test/coverage-report.sh" > "${out}/coverage.txt"
printf '%s\n' "${PR_NUMBER:?PR_NUMBER is required}" > "${out}/pr-number.txt"
printf '%s\n' "${HEAD_SHA:?HEAD_SHA is required}"   > "${out}/head-sha.txt"
