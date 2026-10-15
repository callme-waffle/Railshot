#!/usr/bin/env python3
"""Run shared registration in an isolated, account-authenticated child process."""
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from infrastructure.providers.aws.native import private_json
from infrastructure.providers.environments.runtime import bootstrap

if __name__ == '__main__':
    print(json.dumps(bootstrap(sys.argv[1], private_json(sys.argv[2]))))
