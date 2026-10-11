#!/usr/bin/env python3
"""Fixed local entrypoint for revoking an installation-owned credential."""
import argparse
import json
from pathlib import Path

from client_setup.personal_identity import revoke_personal_identity


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--config-dir', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--test-allow-http', action='store_true')
    args = parser.parse_args(argv)
    result = revoke_personal_identity(args.config_dir, args.state_dir,
                                      test_allow_http=args.test_allow_http)
    print(json.dumps(result, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
