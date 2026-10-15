"""Fixed cloud-authenticated child boundary for shared registration/teardown."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from infrastructure.providers.gcp.native import private_json
from infrastructure.providers.environments.runtime import bootstrap, teardown

if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] not in ('bootstrap', 'teardown'): raise SystemExit(2)
    print(json.dumps({'bootstrap': bootstrap, 'teardown': teardown}[sys.argv[1]](sys.argv[2], private_json(sys.argv[3]))))
