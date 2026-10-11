#!/usr/bin/env bash
set -Eeuo pipefail
exec "$(dirname -- "${BASH_SOURCE[0]}")/../scripts/deploy.sh" --mode offline "$@"
