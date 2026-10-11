#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON=python3
if [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then PYTHON="${SCRIPT_DIR}/.venv/bin/python"; fi
export PYTHONPATH="${SCRIPT_DIR}"
exec "$PYTHON" -m client_setup.main uninstall "$@"
