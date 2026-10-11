#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PYTHON="${PYTHON:-python3}"
export PYTHONDONTWRITEBYTECODE=1
bash -n "$ROOT/deployment/bootstrap/install.sh" "$ROOT/deployment/bootstrap/uninstall.sh"
"$PYTHON" -m pytest -q "$ROOT"/ci/tests/test_bootstrap_*.py "$ROOT/ci/tests/test_agent_jobs.py" "$ROOT/apps/agent/tests/test_agent.py"
