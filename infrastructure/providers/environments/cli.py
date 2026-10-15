#!/usr/bin/env python3
"""Private file protocol for the API process; stdout never contains native errors."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from infrastructure.providers.environments.contract import ProviderError
from infrastructure.providers.environments.control import ControlPlane, read_private


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    try:
        result = ControlPlane(args.config).dispatch(read_private(args.request))
        print(json.dumps(result))
        return 0
    except Exception as exc:
        error = exc if isinstance(exc, ProviderError) else ProviderError('CONTROL_PLANE_FAILED', 503)
        print(json.dumps({'error': {'code': error.code, 'status': error.status,
                                    'outcome_unknown': error.outcome_unknown}}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
