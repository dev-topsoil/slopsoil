#!/usr/bin/env bash
# Print the coverage report (text table with the per-file "Missing" column) from
# an existing .coverage data file produced by `pytest --cov`.
#
# Provider-agnostic: reads .coverage from the repo root and writes the report to
# stdout. Coverage settings (source / omit / branch) come from pyproject.toml.
set -euo pipefail

cd "${GITHUB_WORKSPACE:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"

# -m adds the "Missing" column, matching pytest's --cov-report=term-missing.
python -m coverage report -m
