#!/usr/bin/env bash
# GitHub adapter: post (or update) the sticky coverage comment on a PR.
#
# Finds a prior comment carrying our hidden marker and edits it in place, so
# repeated pushes to the PR update one comment instead of stacking new ones.
# Uses only the GitHub CLI (gh) + jq, both preinstalled on GitHub runners.
#
# Inputs:
#   $1                 Directory holding the downloaded coverage artifact.
#   GH_TOKEN           Token with pull-requests: write (set by the workflow).
#   GITHUB_REPOSITORY  owner/repo (provided by Actions).
set -euo pipefail

dir="${1:?artifact directory required}"
marker="<!-- slopsoil-coverage-report -->"

pr="$(cat "${dir}/pr-number.txt")"
report="$(cat "${dir}/coverage.txt")"
sha="$(cat "${dir}/head-sha.txt")"

# Render the comment body to a file (rawfile avoids any escaping pitfalls).
body_file="$(mktemp)"
{
    printf '%s\n' "${marker}"
    printf '### 🧪 Coverage report\n\n'
    printf '```\n%s\n```\n\n' "${report}"
    printf '_Coverage for commit %s._\n' "${sha:0:7}"
} > "${body_file}"

payload="$(jq -n --rawfile body "${body_file}" '{body: $body}')"

# Look for an existing marked comment (first match across all pages).
existing_id="$(
    gh api --paginate "repos/${GITHUB_REPOSITORY}/issues/${pr}/comments" \
        --jq ".[] | select(.body | contains(\"${marker}\")) | .id" | head -n1
)"

if [[ -n "${existing_id}" ]]; then
    echo "Updating existing coverage comment ${existing_id}"
    printf '%s' "${payload}" |
        gh api -X PATCH "repos/${GITHUB_REPOSITORY}/issues/comments/${existing_id}" --input -
else
    echo "Creating new coverage comment on PR #${pr}"
    printf '%s' "${payload}" |
        gh api -X POST "repos/${GITHUB_REPOSITORY}/issues/${pr}/comments" --input -
fi
