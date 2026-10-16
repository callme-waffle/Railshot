#!/usr/bin/env python3
"""Run shared registration in an isolated, account-authenticated child process."""
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from infrastructure.providers.aws.native import private_json
from infrastructure.providers.environments.runtime import bootstrap, teardown

if __name__ == '__main__':
    if len(sys.argv) not in (3, 4) or len(sys.argv) == 4 and sys.argv[3] != 'teardown': raise SystemExit(2)
    action = teardown if len(sys.argv) == 4 else bootstrap
    print(json.dumps(action(sys.argv[1], private_json(sys.argv[2]))))
