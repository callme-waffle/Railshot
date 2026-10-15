#!/usr/bin/env python3
"""Run a fixed application action with refreshed account credentials in one child."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from infrastructure.providers.environments.control import read_private, save

SCRIPTS = {'registration': 'applications.py', 'routes': 'application_routes.py', 'lifecycle': 'application_lifecycle.py'}


def execute(action, config_path, request_path):
    config, request = read_private(config_path), read_private(request_path, maximum=14_000_000)
    identifier = request['environment_id']
    profile = config['environments'][identifier]
    provider = profile['provider']
    if provider not in ('aws', 'gcp'):
        raise ValueError('APPLICATION_ACCOUNT_AUTHORITY_MISMATCH')
    authority_path = profile['ingress'][provider + '_authority_file']
    authority = read_private(authority_path)
    context, account = authority['context'], authority['config']
    if context['environment_id'] != identifier:
        raise ValueError('APPLICATION_ACCOUNT_AUTHORITY_MISMATCH')
    edge = read_private(profile['ingress']['cluster_edge_config_file'])
    account_key = 'account_id' if provider == 'aws' else 'project_id'
    if (edge.get(provider + '_authority_file') != authority_path or edge['cluster_id'] != identifier
            or edge['account_id'] != account[account_key] or edge['credentials_file'] != authority['credentials_file']):
        raise ValueError('APPLICATION_ACCOUNT_AUTHORITY_MISMATCH')
    if provider == 'aws':
        from infrastructure.providers.aws.native import Session
        if set(account) != {'auth', 'account_id'}:
            raise ValueError('APPLICATION_ACCOUNT_AUTHORITY_MISMATCH')
        credentials = Session(account['auth'], account['account_id']).env()
        save(Path(authority['credentials_file']), {key: credentials[key] for key in
            ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN')})
    else:
        from infrastructure.providers.gcp.auth import Session
        if set(account) != {'auth', 'project_id', 'project_number'}:
            raise ValueError('APPLICATION_ACCOUNT_AUTHORITY_MISMATCH')
        credentials = Session(account['auth'], account['project_id'], authority['state_dir']).env()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('AWS_', 'GOOGLE_', 'GCLOUD_', 'CLOUDSDK_'))}
    env.update(credentials)
    process = subprocess.run([sys.executable, str(Path(__file__).with_name(SCRIPTS[action])),
        '--config', config_path, '--request', request_path], env=env, capture_output=True, timeout=1900)
    if len(process.stdout) > 4 * 1024 * 1024:
        raise ValueError('APPLICATION_RESPONSE_TOO_LARGE')
    result = json.loads(process.stdout)
    if not isinstance(result, dict):
        raise ValueError('APPLICATION_RESPONSE_INVALID')
    return result, process.returncode


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action', choices=tuple(SCRIPTS), required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    try:
        result, code = execute(args.action, args.config, args.request)
    except Exception:
        result, code = {'status': 'unknown', 'error': {'code': 'APPLICATION_ACCOUNT_ACTION_UNVERIFIED', 'outcome_unknown': True}}, 1
    print(json.dumps(result)); sys.exit(code)
