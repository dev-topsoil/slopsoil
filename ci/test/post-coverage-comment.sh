#!/usr/bin/env bash
# Post (or update) the unit-test coverage report as a comment on the PR.
#
# Runs straight from the test workflow: the unit tests leave a .coverage file in
# the workspace, this renders it and upserts a single marked comment, so pushing
# new commits edits the same comment instead of stacking new ones.
#
# Inputs (environment variables):
#   GH_TOKEN           Token with pull-requests: write (set by the workflow).
#   PR_NUMBER          Pull request number.
#   GITHUB_REPOSITORY  owner/repo (provided by Actions).
set -euo pipefail

: "${PR_NUMBER:?PR_NUMBER is required}"
repo_root="${GITHUB_WORKSPACE:-$(git rev-parse --show-toplevel)}"
marker="<!-- slopsoil-coverage-report -->"

report="$("${repo_root}/ci/test/coverage-report.sh")"

# Render the comment body to a file (rawfile avoids any escaping pitfalls).
body_file="$(mktemp)"
{
    printf '%s\n' "${marker}"
    printf '### 🧪 Coverage report\n\n'
    printf '```\n%s\n```\n' "${report}"
} > "${body_file}"

payload="$(jq -n --rawfile body "${body_file}" '{body: $body}')"

# Find a prior marked comment (first match across all pages), if any.
existing_id="$(
    gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" \
        --jq ".[] | select(.body | contains(\"${marker}\")) | .id" | head -n1
)"

if [[ -n "${existing_id}" ]]; then
    printf '%s' "${payload}" |
        gh api -X PATCH "repos/${GITHUB_REPOSITORY}/issues/comments/${existing_id}" --input -
    echo "Updated coverage comment ${existing_id}"
else
    printf '%s' "${payload}" |
        gh api -X POST "repos/${GITHUB_REPOSITORY}/issues/${PR_NUMBER}/comments" --input -
    echo "Created coverage comment on PR #${PR_NUMBER}"
fi
