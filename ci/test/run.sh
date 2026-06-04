#!/usr/bin/env bash
# Install test dependencies and run the unit test suite.
#
# Provider-agnostic by design: a CI adapter (the .github/actions/unit-tests
# composite action) handles language/runtime setup, while this script only
# installs the Python packages the tests need and invokes pytest. The same
# command therefore works locally and under any CI provider.
#
# The dependency list is intentionally a SUBSET of requirements.txt: the unit
# tests stub out davey/dave.py (see davey_compat.py) and never exercise yt-dlp,
# so those heavier packages are skipped to keep the run fast.
#
# Coverage is collected on every run (config lives in pyproject.toml under
# [tool.coverage.*]). The resulting .coverage data file is what the PR coverage
# report is generated from; it is simply ignored where it isn't consumed.
#
# Inputs (environment variables):
#   PYTEST_ARGS   Extra arguments forwarded to pytest. (default: empty)
set -euo pipefail

runtime_deps=(discord.py-self PyNaCl python-dotenv)
test_deps=(pytest pytest-asyncio pytest-mock pytest-cov)

python -m pip install "${runtime_deps[@]}" "${test_deps[@]}"

# shellcheck disable=SC2086  # deliberate word-splitting of the optional args
python -m pytest --cov --cov-report=term-missing ${PYTEST_ARGS:-}
