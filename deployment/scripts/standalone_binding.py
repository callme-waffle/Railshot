"""Generate and bind an app database through the approved environment DB bundle."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

import environment as runtime


def prepare(profile, environment_id, application_id, home):
    reference = profile['ingress'].get('standalone_database')
    if not reference: return None
    bundle = runtime.read_private(reference)
    if set(bundle) != {'version', 'descriptor_file', 'config_file', 'context_file'} or bundle['version'] != 1:
        raise ValueError('invalid standalone database bundle')
    descriptor = runtime.read_private(bundle['descriptor_file'])
    config = runtime.read_private(bundle['config_file'])
    context = runtime.read_private(bundle['context_file'])
    if context['environment_id'] != environment_id: raise ValueError('database environment differs')
    host = descriptor.get('private_ipv4') or descriptor['addresses']['private']
    suffix = hashlib.sha256(application_id.encode()).hexdigest()[:24]
    identity = {'environment_id': environment_id, 'application_id': application_id, 'bundle': bundle, 'resource_id': descriptor['resource_id'], 'host': host}
    binding_file = home / 'database-binding.json'
    identity_file = home / 'database-identity.json'
    if identity_file.exists() and runtime.read_private(identity_file) != identity: raise ValueError('database binding authority changed')
    if binding_file.exists():
        if runtime.read_private(identity_file) != identity: raise ValueError('database binding authority changed')
        binding = runtime.read_private(binding_file)
    else:
        binding = {'version': 1, 'host': host, 'port': 5432, 'database': 'db_' + suffix, 'sslmode': 'verify-full', 'sslrootcert': config['tls']['ca_file'],
            'migration': {'username': 'own_' + suffix, 'password': secrets.token_urlsafe(36)},
            'runtime': {'username': 'run_' + suffix, 'password': secrets.token_urlsafe(36)}}
        runtime.save(identity_file, identity); runtime.save(binding_file, binding)
    ca = runtime.read_private(binding['sslrootcert'], raw=True).decode('ascii')
    if '-----BEGIN CERTIFICATE-----' not in ca: raise ValueError('database CA missing')
    return {'reference': reference, 'binding_file': str(binding_file), 'binding': {**binding, 'ca': ca}, 'application_id': application_id,
        'target': {'host': host, 'port': 5432, 'runtime_secret': 'database-runtime', 'migration_secret': 'database-migration', 'ca_secret': 'database-ca'}}


def install(prepared):
    command = [sys.executable, str(runtime.ROOT / 'infrastructure/ansible/standalone_bind.py'), '--bundle', prepared['reference'], '--binding', prepared['binding_file'], '--application-id', prepared['application_id']]
    # The fixed environment auth wrapper supplied the scoped short-lived cloud environment.
    # standalone.run independently removes cloud secrets from the Ansible/guest environment.
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=1850, check=False, env=dict(os.environ), text=True)
    if result.returncode or len(result.stdout) > 65536: raise ValueError('standalone application bind failed')
    receipt = json.loads(result.stdout)
    if receipt.get('status') != 'succeeded' or receipt.get('ready') is not True: raise ValueError('standalone application bind not proven')
    return {'status': 'succeeded', 'ready': True}
