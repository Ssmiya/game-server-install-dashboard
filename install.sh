#!/usr/bin/env bash
set -Eeuo pipefail

# Upload the complete project directory, then run:
# sudo bash install.sh

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${PROJECT_ROOT}/deploy/install.sh" "$@"
