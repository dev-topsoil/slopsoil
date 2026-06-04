#!/usr/bin/env bash
# Emit OCI image labels as newline-separated "key=value" pairs on stdout.
#
# Provider agnostic by design — see ci/docker/tags.sh for the rationale.
#
# Inputs (environment variables):
#   TITLE         Image title.                          (default: empty)
#   DESCRIPTION   Short, human-readable image summary.   (default: empty)
#   SOURCE_URL    URL of the source repository.          (default: empty)
#   REVISION      Full git commit SHA.                   (default: empty)
#   VERSION       Human version (tag or branch name).    (default: empty)
#   LICENSES      SPDX license expression.               (default: empty)
#   CREATED       RFC 3339 build timestamp.              (default: now, UTC)
set -euo pipefail

created="${CREATED:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"

emit() { printf '%s=%s\n' "$1" "$2"; }

emit "org.opencontainers.image.title"       "${TITLE:-}"
emit "org.opencontainers.image.description" "${DESCRIPTION:-}"
emit "org.opencontainers.image.source"      "${SOURCE_URL:-}"
emit "org.opencontainers.image.revision"    "${REVISION:-}"
emit "org.opencontainers.image.version"     "${VERSION:-}"
emit "org.opencontainers.image.licenses"    "${LICENSES:-}"
emit "org.opencontainers.image.created"     "${created}"
