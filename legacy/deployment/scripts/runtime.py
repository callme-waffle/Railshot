#!/usr/bin/env python3
"""JSON stdin/file -> adapter -> engine -> one JSON stdout response."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from engine import DeploymentEngine
from input_adapter import InputError, parse_input
from models import DeploymentResult


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError(message)


def load(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InputError(f'duplicate JSON key: {key}')
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs,
                      parse_constant=lambda x: (_ for _ in ()).throw(InputError(f'invalid JSON constant: {x}')))


def main(argv=None):
    provider, environment, action = None, None, None
    schema_version = '0.1'
    try:
        parser = Parser(description='Railshot local Linux deployment runtime (provisional JSON contracts 0.1/0.2)')
        parser.add_argument('action', choices=('deploy', 'verify', 'cleanup'))
        parser.add_argument('--input', default='-', help='JSON path or - for stdin')
        parser.add_argument('--all', action='store_true', help='remove managed K3s/Cilium, not just workload')
        parser.add_argument('--disposable-node', action='store_true')
        parser.add_argument('--mode', choices=('auto', 'online', 'offline'), help='default auto; explicit flag opts into output 0.2')
        parser.add_argument('--bundle', help='absolute local bundle directory; opts into output 0.2')
        parser.add_argument('--bundle-sha256', help='trusted onboarding SHA256 of bundle-manifest.json')
        args = parser.parse_args(argv)
        action = args.action
        if (args.all or args.disposable_node) and args.action != 'cleanup':
            raise InputError('--all/--disposable-node apply only to cleanup')
        text = sys.stdin.read(1048577) if args.input == '-' else Path(args.input).read_text()
        if len(text.encode()) > 1048576:
            raise InputError('input exceeds 1 MiB')
        data = load(text)
        if isinstance(data, dict):
            schema_version = data.get('schema_version', '0.1')
        if args.mode is not None or args.bundle is not None or args.bundle_sha256 is not None:
            if not isinstance(data, dict) or not isinstance(data.get('runtime', {}), dict):
                raise InputError('input/runtime must be an object')
            schema_version = data['schema_version'] = '0.2'
            options = data.setdefault('runtime', {})
            for key, value in (('mode', args.mode), ('bundle_path', args.bundle), ('bundle_sha256', args.bundle_sha256)):
                if value is not None:
                    options[key] = value
        request = parse_input(data)
        schema_version = request.context.schema_version
        provider, environment = request.context.provider, request.spec.environment_id
        result = DeploymentEngine(request.spec).execute(action, args.all, args.disposable_node)
    except (InputError, json.JSONDecodeError, OSError, UnicodeError) as exc:
        result = DeploymentResult(error={'code': 'INVALID_INPUT', 'stage': 'INPUT_ADAPTER', 'message': str(exc),
                                         'exit_code': None, 'diagnostics': None}, states=[{'state': 'FAILED', 'timestamp': None}])
    fields = asdict(result)
    if schema_version != '0.2':
        schema_version = '0.1'
        legacy = ('status', 'cluster_status', 'cilium_status', 'workload_status', 'endpoint', 'endpoint_scope', 'error', 'states')
        fields = {key: fields[key] for key in legacy}
    payload = {'schema_version': schema_version, 'action': action, 'provider': provider, 'environment_id': environment, **fields}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if result.status in ('ready', 'cleaned') else 1


if __name__ == '__main__':
    sys.exit(main())
