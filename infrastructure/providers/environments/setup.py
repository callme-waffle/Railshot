#!/usr/bin/env python3
"""Create private non-secret input templates or inspect their shape offline.

This command never calls provider APIs, provisions resources or prints values.
Fill credential files in your own secret manager / secure editor, not chat or git.
"""
import argparse
import importlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from infrastructure.providers.environments.control import ControlPlane, private_dir, read_private, save
from infrastructure.providers.environments.contract import ProviderError, PROVIDERS, REGIONS, identifier


def initialize(directory, provider, owner):
    directory = Path(directory).absolute()
    repository = Path(__file__).resolve().parents[3]
    if directory == repository or repository in directory.parents:
        raise ProviderError('INPUT_DIRECTORY_MUST_BE_OUTSIDE_REPOSITORY', 422)
    directory = private_dir(directory)
    config = directory / 'control.json'
    if config.exists() or (directory / 'INPUTS.txt').exists():
        raise ProviderError('INPUT_FILE_ALREADY_EXISTS', 409)
    account = {'id': provider + '-account', 'owner_id': owner, 'provider': provider, 'ownership': 'owned',
               'credential_ref': provider + '-credential', 'regions': [REGIONS.get(provider, 'RegionOne')],
               'allow_apply': False, 'adapter': {}}
    module = 'onprem' if provider == 'openstack' else provider
    try:
        template = importlib.import_module('infrastructure.providers.environments.' + module).config_template(str(directory))
    except (ImportError, AttributeError):
        raise ProviderError('PROVIDER_INPUT_SCHEMA_UNAVAILABLE', 503) from None
    if not isinstance(template, dict) or not isinstance(template.get('adapter'), dict) or not isinstance(template.get('files', {}), dict):
        raise ProviderError('PROVIDER_INPUT_SCHEMA_INVALID', 503)
    account['adapter'] = template['adapter']
    for name in template.get('files', {}):
        if not isinstance(name, str) or Path(name).name != name or not name.endswith('.json') or (directory / name).exists():
            raise ProviderError('INPUT_FILE_ALREADY_EXISTS_OR_INVALID', 409)
    for name, content in template.get('files', {}).items():
        save(directory / name, content)
    guide = directory / 'INPUTS.txt'
    if guide.exists():
        raise ProviderError('INPUT_FILE_ALREADY_EXISTS', 409)
    notes = template.get('notes', [])
    text = ('Private operator input templates. Execution remains disabled.\n'
            'Do not send secret values through chat, git, shell arguments or PR comments.\n'
            'Use an operator secure editor or secret manager; keep directory 0700 and files 0600.\n'
            'Owner: GET /api/v1/logins as the intended user, then prefix account_id with account:.\n'
            'Offline validate reports field names and codes only; it never provisions resources.\n'
            'CI credential JSON: {"token":""}; profile.ingress.ci_credentials_file references this file.\n'
            'Observer: profile.ingress.observability_config_file references the existing collector registration profile.\n')
    text += '\n'.join(note for note in notes if isinstance(note, str)) + '\n'
    fd = os.open(guide, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
    save(config, {'version': 1, 'policy_revision': 'initial', 'state_dir': str(directory / 'state'), 'accounts': [account]})
    # Template contains only names and disabled policy, never guessed cloud values or secret placeholders.
    return {'valid': True, 'created': ['control.json', 'INPUTS.txt', *template.get('files', {}).keys()], 'verification': 'template_only', 'execution_enabled': False}


def validate(config):
    registry = read_private(config)
    missing, errors = [], []
    try:
        ControlPlane(config)
    except (ProviderError, KeyError, TypeError):
        return {'valid': False, 'missing': [], 'errors': ['REGISTRY_INVALID'], 'verification': 'configuration_only'}
    for index, account in enumerate(registry['accounts']):
        module = 'onprem' if account['provider'] == 'openstack' else account['provider']
        prefix = 'accounts.' + str(index) + '.adapter.'
        try:
            provider = importlib.import_module('infrastructure.providers.environments.' + module)
            checker = getattr(provider, 'validate_config', None)
            if checker is None:
                errors.append('PROVIDER_OFFLINE_VALIDATOR_UNAVAILABLE')
                continue
            result = checker(account['adapter'])
            if not isinstance(result, dict):
                errors.append('PROVIDER_VALIDATOR_RESPONSE_INVALID')
                continue
            missing.extend(prefix + field for field in result.get('missing', [])
                           if isinstance(field, str) and all(c.isalnum() or c in '._[]' for c in field))
            errors.extend(code for code in result.get('errors', [])
                          if isinstance(code, str) and code.isupper() and len(code) < 96)
            if result.get('valid') is not True and not result.get('missing') and not result.get('errors'):
                errors.append('PROVIDER_CONFIGURATION_INVALID')
        except Exception:
            errors.append('PROVIDER_CONFIGURATION_UNVERIFIED')
    return {'valid': not missing and not errors, 'missing': missing, 'errors': sorted(set(errors)),
            'verification': 'configuration_only', 'live_calls': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    init = actions.add_parser('init'); init.add_argument('--directory', required=True)
    init.add_argument('--provider', choices=sorted(PROVIDERS), required=True); init.add_argument('--owner-id', required=True)
    check = actions.add_parser('validate'); check.add_argument('--config', required=True)
    args = parser.parse_args()
    try:
        if args.action == 'init':
            if not identifier(args.owner_id):
                raise ProviderError('OWNER_ID_INVALID', 422)
            result = initialize(args.directory, args.provider, args.owner_id)
        else:
            result = validate(args.config)
        print(json.dumps(result)); return 0 if result['valid'] else 1
    except Exception as exc:
        print(json.dumps({'valid': False, 'errors': [exc.code if isinstance(exc, ProviderError) else 'PRIVATE_INPUT_INVALID'], 'live_calls': False}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
