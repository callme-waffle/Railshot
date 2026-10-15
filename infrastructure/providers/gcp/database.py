"""Fixed isolated PG entrypoint inheriting only the freshly authorized GCP child session."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'ansible'))
import standalone


def main():
    try:
        request = standalone.read(sys.argv[1])
        if set(request) != {'action', 'descriptor_file', 'config_file', 'context_file'}: raise ValueError()
        descriptor = standalone.read(request['descriptor_file'])
        context = standalone.read(request['context_file'])
        result = standalone.run(request['action'], descriptor, standalone.read(request['config_file']), context)
        if result.get('status') == 'succeeded' and result.get('ready'):
            result['application_bundle_file'] = standalone.application_bundle(descriptor, request['config_file'], context)
        print(json.dumps(result))
    except Exception:
        print(json.dumps({'status': 'blocked', 'ready': False, 'error': {'code': 'GCP_DATABASE_INPUT_INVALID'}}))


if __name__ == '__main__': main()
