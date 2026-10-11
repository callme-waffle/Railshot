#!/usr/bin/env python3
"""Enroll one new VM's SSH host key through its authenticated provider API."""
import argparse
import base64
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import time

PRIVATE = tuple(ipaddress.ip_network(cidr) for cidr in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
MARKER = 'RAILSHOT_SSH_HOST_KEY '
READ_KEY = 'cloud-init status --wait >/dev/null && cat /etc/ssh/ssh_host_ed25519_key.pub'


class AccessError(Exception):
    pass


def key(value):
    """Only an actual OpenSSH Ed25519 wire key is accepted; comments are discarded."""
    if not isinstance(value, str) or len(value) > 1024:
        raise AccessError('SSH_HOST_KEY_INVALID')
    match = re.fullmatch(r'ssh-ed25519 ([A-Za-z0-9+/]+={0,2})(?: [^\r\n]*)?\n?', value)
    if not match:
        raise AccessError('SSH_HOST_KEY_INVALID')
    try:
        raw = base64.b64decode(match[1], validate=True)
    except ValueError as exc:
        raise AccessError('SSH_HOST_KEY_INVALID') from exc
    prefix = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32)
    if len(raw) != len(prefix) + 32 or not raw.startswith(prefix):
        raise AccessError('SSH_HOST_KEY_INVALID')
    return 'ssh-ed25519 ' + base64.b64encode(raw).decode()


def private_descriptor(path):
    path = Path(path)
    info = path.lstat()
    if (not path.is_absolute() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_size > 1024 * 1024):
        raise AccessError('DESCRIPTOR_FILE_INVALID')
    return json.loads(path.read_text())


def identity(descriptor):
    try:
        target = descriptor['target_id']
        if descriptor['schema_version'] != 'v1' or not re.fullmatch(r'[a-z][a-z0-9-]{2,62}', target):
            raise ValueError()
        address = str(ipaddress.IPv4Address(descriptor['addresses']['private']))
        if not any(ipaddress.ip_address(address) in network for network in PRIVATE):
            raise ValueError()
        provider = descriptor['provider_kind']
        if provider == 'aws':
            resource = re.fullmatch(r'arn:aws:ec2:([a-z]{2}-[a-z]+-\d):([0-9]{12}):instance/(i-[0-9a-f]{8,17})', descriptor['resource_id'])
            if not resource or descriptor['instance_id'] != resource[3] or descriptor['location']['region'] != resource[1]:
                raise ValueError()
            if descriptor['location'].get('account_id', resource[2]) != resource[2]:
                raise ValueError()
            return provider, target, address, resource.groups()
        if provider == 'gcp':
            resource = re.fullmatch(r'projects/([a-z][a-z0-9-]{4,28}[a-z0-9])/zones/([a-z]+-[a-z]+\d+-[a-z])/instances/([a-z][a-z0-9-]{0,62})', descriptor['resource_id'])
            if not resource or descriptor['location']['project_id'] != resource[1] or descriptor['location']['zone'] != resource[2]:
                raise ValueError()
            if not re.fullmatch(r'[0-9]{1,30}', str(descriptor['instance_id'])):
                raise ValueError()
            return provider, target, address, resource.groups()
    except (KeyError, TypeError, ValueError) as exc:
        raise AccessError('DESCRIPTOR_IDENTITY_INVALID') from exc
    raise AccessError('PROVIDER_UNSUPPORTED')


def command(argv, timeout):
    env = {name: value for name, value in os.environ.items()
           if not name.startswith(('AWS_ENDPOINT_URL', 'CLOUDSDK_API_ENDPOINT_OVERRIDES_'))}
    env.update(AWS_PAGER='', AWS_CLI_AUTO_PROMPT='off', CLOUDSDK_CORE_DISABLE_PROMPTS='1')
    try:
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AccessError('PROVIDER_OBSERVATION_FAILED') from exc
    if result.returncode:
        if argv[:3] == ['aws', 'ssm', 'get-command-invocation'] and 'InvocationDoesNotExist' in result.stderr:
            return {'Status': 'Pending'}
        raise AccessError('PROVIDER_OBSERVATION_FAILED')
    if len(result.stdout) > 2 * 1024 * 1024:
        raise AccessError('PROVIDER_RESPONSE_INVALID')
    try:
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except ValueError as exc:
        raise AccessError('PROVIDER_RESPONSE_INVALID') from exc


def enroll(descriptor, output, *, timeout_seconds=600, run=command, now=time.monotonic, sleep=time.sleep):
    if type(timeout_seconds) is not int or not 30 <= timeout_seconds <= 1200:
        raise AccessError('TIMEOUT_INVALID')
    provider, target, address, resource = identity(descriptor)
    output = Path(output)
    parent = output.parent.lstat()
    if (not output.is_absolute() or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or parent.st_mode & 0o077 or output.parent.resolve() != output.parent):
        raise AccessError('HOST_KEY_DIRECTORY_INVALID')
    if output.exists() or output.is_symlink():
        raise AccessError('HOST_KEY_FILE_EXISTS')
    deadline = now() + timeout_seconds

    def call(argv):
        remaining = deadline - now()
        if remaining <= 0:
            raise AccessError('SSH_HOST_KEY_TIMEOUT')
        return run(argv, min(30, remaining))

    def pause():
        remaining = deadline - now()
        if remaining <= 0:
            raise AccessError('SSH_HOST_KEY_TIMEOUT')
        sleep(min(2, remaining))

    if provider == 'aws':
        region, account, instance = resource
        options = ['--region', region, '--output', 'json', '--no-cli-pager']
        if call(['aws', 'sts', 'get-caller-identity', *options]).get('Account') != account:
            raise AccessError('PROVIDER_IDENTITY_MISMATCH')
        observed = call(['aws', 'ec2', 'describe-instances', '--instance-ids', instance, *options])
        instances = [node for reservation in observed.get('Reservations', []) for node in reservation.get('Instances', [])]
        if (len(instances) != 1 or instances[0].get('InstanceId') != instance
                or instances[0].get('PrivateIpAddress') != address or instances[0].get('State', {}).get('Name') != 'running'):
            raise AccessError('PROVIDER_IDENTITY_MISMATCH')
        while True:
            managed = call(['aws', 'ssm', 'describe-instance-information', '--filters',
                            json.dumps([{'Key': 'InstanceIds', 'Values': [instance]}]), *options])
            rows = managed.get('InstanceInformationList', [])
            if len(rows) == 1 and rows[0].get('InstanceId') == instance and rows[0].get('PingStatus') == 'Online':
                break
            pause()
        sent = call(['aws', 'ssm', 'send-command', '--instance-ids', instance, '--document-name', 'AWS-RunShellScript',
                     '--parameters', json.dumps({'commands': [READ_KEY], 'executionTimeout': [str(timeout_seconds)]}),
                     '--timeout-seconds', str(max(30, math.ceil(deadline - now()))), *options])
        command_id = sent.get('Command', {}).get('CommandId')
        if not isinstance(command_id, str) or not re.fullmatch(r'[0-9a-f-]{36}', command_id):
            raise AccessError('PROVIDER_RESPONSE_INVALID')
        while True:
            result = call(['aws', 'ssm', 'get-command-invocation', '--command-id', command_id, '--instance-id', instance, *options])
            if result.get('Status') == 'Success':
                public_key = key(result.get('StandardOutputContent'))
                break
            if result.get('Status') not in ('Pending', 'InProgress', 'Delayed'):
                raise AccessError('SSH_HOST_KEY_UNAVAILABLE')
            pause()
    else:
        project, zone, instance = resource
        options = ['--project', project, '--zone', zone, '--format=json', '--quiet']
        def verified_instance():
            observed = call(['gcloud', 'compute', 'instances', 'describe', instance, *options])
            if (str(observed.get('id')) != str(descriptor['instance_id']) or observed.get('name') != instance
                    or observed.get('status') != 'RUNNING'
                    or not any(network.get('networkIP') == address for network in observed.get('networkInterfaces', []))):
                raise AccessError('PROVIDER_IDENTITY_MISMATCH')
        verified_instance()
        while True:
            result = call(['gcloud', 'compute', 'instances', 'get-serial-port-output', instance, '--port=1', '--start=0', *options])
            content = result.get('contents')
            if not isinstance(content, str):
                raise AccessError('PROVIDER_RESPONSE_INVALID')
            markers = [line.split(MARKER, 1)[1].strip() for line in content.splitlines() if MARKER in line]
            if markers:
                try:
                    public_key = key(base64.b64decode(markers[-1], validate=True).decode())
                except (ValueError, UnicodeError) as exc:
                    raise AccessError('SSH_HOST_KEY_INVALID') from exc
                verified_instance()  # Reject a same-name VM replacement while reading serial output.
                break
            pause()
    data = f'{address},{target} {public_key}\n'.encode()
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        output.unlink(missing_ok=True)
        raise
    return {'status': 'succeeded', 'target_id': target}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--descriptor', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout-seconds', type=int, default=600)
    args = parser.parse_args()
    try:
        print(json.dumps(enroll(private_descriptor(args.descriptor), args.output, timeout_seconds=args.timeout_seconds)))
    except (AccessError, OSError, ValueError, KeyError, TypeError, AttributeError):
        print(json.dumps({'status': 'blocked', 'error': {'code': 'SSH_HOST_KEY_ENROLLMENT_FAILED',
              'retryable': False, 'outcome_unknown': False}}))
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
