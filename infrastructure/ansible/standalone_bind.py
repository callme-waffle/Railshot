"""Private fixed subprocess for application role binding. Never accepts guest commands."""
import argparse
import json
import re
import standalone


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', required=True); parser.add_argument('--binding', required=True); parser.add_argument('--application-id', required=True)
    args = parser.parse_args()
    try:
        if not re.fullmatch(r'app-[a-f0-9]{24}', args.application_id): raise ValueError()
        bundle = standalone.read(args.bundle)
        if set(bundle) != {'version', 'descriptor_file', 'config_file', 'context_file'} or bundle['version'] != 1: raise ValueError()
        context = standalone.read(bundle['context_file'])
        context['operation_id'] = 'bind-' + args.application_id
        result = standalone.run({'action': 'bind', 'binding_file': args.binding}, standalone.read(bundle['descriptor_file']), standalone.read(bundle['config_file']), context)
    except Exception: result = {'status': 'blocked', 'ready': False, 'error': {'code': 'DATABASE_BINDING_INVALID'}}
    print(json.dumps({key: result[key] for key in ('status', 'ready', 'error') if key in result}))
    return 0 if result['status'] == 'succeeded' and result.get('ready') else 1


if __name__ == '__main__': raise SystemExit(main())
