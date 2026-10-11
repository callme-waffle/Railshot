#!/usr/bin/env python3
"""Fixed SSH forced command; stdout contains exactly one bounded JSON response."""
import argparse
import json
import os
from pathlib import Path
import select
import sys
import time

if __package__:
    from .protocol import COMMAND, MAX_REQUEST_BYTES, ProtocolError, decode_request, sanitize_instances, success_response, error_response
else:
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    from protocol import COMMAND, MAX_REQUEST_BYTES, ProtocolError, decode_request, sanitize_instances, success_response, error_response


def execute_request(raw, original_command, credential_loader, cli_factory):
    request = None
    if original_command != COMMAND:
        return error_response('command_rejected')
    try:
        request = decode_request(raw)
        try:
            credentials = credential_loader()
            if not isinstance(credentials,dict):
                raise ValueError()
        except Exception:
            return error_response('credentials_unavailable',request)
        # Configured cloud authentication never comes from the incoming request.
        rows = cli_factory(credentials).run(['server','list'])
        secrets = tuple(v for k,v in credentials.items() if any(s in k.lower() for s in ('secret','password','token')) and isinstance(v,str))
        return success_response(request,sanitize_instances(rows,secrets))
    except ProtocolError as exc:
        return error_response(exc.code,request)
    except Exception:
        return error_response('execution_failed',request)


def _read_bounded(stream):
    try:
        descriptor = stream.fileno()
    except (AttributeError,OSError):
        return stream.read(MAX_REQUEST_BYTES+1)
    deadline = time.monotonic()+10
    data = bytearray()
    while len(data) <= MAX_REQUEST_BYTES:
        remaining = deadline-time.monotonic()
        if remaining <= 0 or not select.select([descriptor],[],[],remaining)[0]:
            raise ProtocolError('invalid_request')
        chunk = os.read(descriptor, min(4096,MAX_REQUEST_BYTES+1-len(data)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    return bytes(data)


def run_stream(stdin,stdout,original_command,credential_loader,cli_factory):
    # Refuse arbitrary SSH commands before reading input or opening the vault.
    if original_command != COMMAND:
        response = error_response('command_rejected')
    else:
        try:
            raw = _read_bounded(stdin)
            response = execute_request(raw,original_command,credential_loader,cli_factory)
        except ProtocolError as exc:
            response = error_response(exc.code)
        except Exception:
            response = error_response('invalid_request')
    stdout.write(json.dumps(response,separators=(',',':'))+'\n')
    stdout.flush()
    return 0 if response['ok'] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo',type=Path,required=True)
    parser.add_argument('--config-dir',type=Path,default=Path('/etc/jasmin'))
    args = parser.parse_args()
    # Paths are fixed in root-owned authorized_keys, never supplied by server JSON.
    sys.path.insert(0,str(args.repo))
    sys.path.insert(0,str(args.repo/'deployment/bootstrap'))
    def load_credentials():
        from client_setup.credentials import CredentialStore
        return CredentialStore(args.config_dir).load()
    def make_cli(auth):
        from infrastructure.providers.openstack.cli import OpenStackCLI
        return OpenStackCLI(auth,timeout=30)
    return run_stream(sys.stdin.buffer,sys.stdout,os.environ.get('SSH_ORIGINAL_COMMAND',''),
                      load_credentials,make_cli)


if __name__=='__main__':
    raise SystemExit(main())
