"""Authenticated host-key enrollment contract; no cloud calls or SSH connections."""
import base64
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import access

PUBLIC = 'ssh-ed25519 ' + base64.b64encode(struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32) + bytes(range(32))).decode()
AWS = {'schema_version': 'v1', 'provider_kind': 'aws', 'target_id': 'aws-db1', 'instance_id': 'i-0123456789abcdef0',
       'resource_id': 'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-0123456789abcdef0',
       'location': {'account_id': '123456789012', 'region': 'ap-northeast-2'}, 'addresses': {'private': '10.1.0.4'}}
GCP = {'schema_version': 'v1', 'provider_kind': 'gcp', 'target_id': 'gcp-db1', 'instance_id': '12345',
       'resource_id': 'projects/railshot-test/zones/asia-northeast3-a/instances/db1',
       'location': {'project_id': 'railshot-test', 'zone': 'asia-northeast3-a'}, 'addresses': {'private': '10.2.0.4'}}
COMMAND_ID = '00000000-0000-0000-0000-000000000001'


class AccessTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name).resolve() / 'known_hosts'
        self.calls = []
        self.clock = 0
        self.serial = access.MARKER + base64.b64encode((PUBLIC + ' root@new-vm\n').encode()).decode() + '\n'
        self.gcp_id = GCP['instance_id']
        self.account = AWS['location']['account_id']
        self.managed_polls = 0

    def provider_run(self, argv, timeout):
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 30)
        self.calls.append(argv)
        if argv[:3] == ['aws', 'sts', 'get-caller-identity']:
            return {'Account': self.account}
        if argv[:3] == ['aws', 'ec2', 'describe-instances']:
            return {'Reservations': [{'Instances': [{'InstanceId': AWS['instance_id'], 'PrivateIpAddress': '10.1.0.4', 'State': {'Name': 'running'}}]}]}
        if argv[:3] == ['aws', 'ssm', 'describe-instance-information']:
            self.managed_polls += 1
            return {'InstanceInformationList': [] if self.managed_polls == 1 else [{'InstanceId': AWS['instance_id'], 'PingStatus': 'Online'}]}
        if argv[:3] == ['aws', 'ssm', 'send-command']:
            params = json.loads(argv[argv.index('--parameters') + 1])
            self.assertEqual(params['commands'], ['cloud-init status --wait >/dev/null && cat /etc/ssh/ssh_host_ed25519_key.pub'])
            return {'Command': {'CommandId': COMMAND_ID}}
        if argv[:3] == ['aws', 'ssm', 'get-command-invocation']:
            return {'Status': 'Success', 'StandardOutputContent': PUBLIC + ' root@new-vm\n'}
        if argv[:4] == ['gcloud', 'compute', 'instances', 'describe']:
            return {'id': self.gcp_id, 'name': 'db1', 'status': 'RUNNING', 'networkInterfaces': [{'networkIP': '10.2.0.4'}]}
        if argv[:4] == ['gcloud', 'compute', 'instances', 'get-serial-port-output']:
            return {'contents': self.serial}
        self.fail(str(argv))

    def sleep(self, seconds):
        self.clock += seconds

    def enroll(self, descriptor):
        return access.enroll(descriptor, self.output, timeout_seconds=30, run=self.provider_run,
                             now=lambda: self.clock, sleep=self.sleep)

    def test_aws_account_bound_ssm_read_waits_for_agent_and_writes_private_key_record(self):
        self.assertEqual(self.enroll(AWS), {'status': 'succeeded', 'target_id': 'aws-db1'})
        self.assertEqual(self.output.read_text(), f'10.1.0.4,aws-db1 {PUBLIC}\n')
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.managed_polls, 2)
        self.assertEqual(sum(argv[:3] == ['aws', 'ssm', 'send-command'] for argv in self.calls), 1)

    def test_gcp_serial_marker_is_bound_before_and_after_to_same_instance(self):
        self.assertEqual(self.enroll(GCP)['status'], 'succeeded')
        self.assertEqual(self.output.read_text(), f'10.2.0.4,gcp-db1 {PUBLIC}\n')
        self.assertEqual(sum(argv[:4] == ['gcloud', 'compute', 'instances', 'describe'] for argv in self.calls), 2)

    def test_account_or_replaced_instance_never_trusts_key(self):
        self.account = '999999999999'
        with self.assertRaisesRegex(access.AccessError, 'IDENTITY_MISMATCH'):
            self.enroll(AWS)
        self.assertFalse(any(argv[:3] == ['aws', 'ssm', 'send-command'] for argv in self.calls))
        self.gcp_id = '67890'
        with self.assertRaisesRegex(access.AccessError, 'IDENTITY_MISMATCH'):
            self.enroll(GCP)
        self.assertFalse(self.output.exists())

    def test_gcp_replacement_during_serial_read_is_rejected(self):
        def changing_instance(argv, timeout):
            result = self.provider_run(argv, timeout)
            if argv[:4] == ['gcloud', 'compute', 'instances', 'get-serial-port-output']:
                self.gcp_id = '67890'
            return result
        with self.assertRaisesRegex(access.AccessError, 'IDENTITY_MISMATCH'):
            access.enroll(GCP, self.output, timeout_seconds=30, run=changing_instance)
        self.assertFalse(self.output.exists())

    def test_public_ip_and_mismatched_resource_rejected_before_cloud(self):
        for descriptor in (dict(AWS, instance_id='i-99999999999999999'), dict(GCP, addresses={'private': '8.8.8.8'})):
            with self.assertRaises(access.AccessError):
                self.enroll(descriptor)
        self.assertEqual(self.calls, [])

    def test_missing_serial_marker_times_out_without_known_hosts(self):
        self.serial = 'cloud-init still running\n'
        with self.assertRaisesRegex(access.AccessError, 'TIMEOUT'):
            self.enroll(GCP)
        self.assertEqual(self.clock, 30)
        self.assertFalse(self.output.exists())

    def test_existing_file_is_not_overwritten_and_invalid_key_is_rejected(self):
        self.output.write_text('trusted existing key')
        with self.assertRaisesRegex(access.AccessError, 'FILE_EXISTS'):
            self.enroll(GCP)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.output.read_text(), 'trusted existing key')
        for invalid in ('ssh-ed25519 AAAA', PUBLIC + '\n' + PUBLIC, PUBLIC.replace('ssh-ed25519', 'ssh-rsa'), 'secret'):
            with self.assertRaises(access.AccessError):
                access.key(invalid)

    def test_cli_errors_do_not_publish_provider_stdout_or_stderr(self):
        result = subprocess.CompletedProcess([], 1, 'private data', 'private account token')
        with patch.object(access.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(access.AccessError, '^PROVIDER_OBSERVATION_FAILED$'):
                access.command(['aws', 'sts', 'get-caller-identity'], 5)

    def test_descriptor_requires_private_regular_file(self):
        path = self.output.parent / 'descriptor.json'
        path.write_text(json.dumps(AWS)); path.chmod(0o600)
        self.assertEqual(access.private_descriptor(path), AWS)
        path.chmod(0o644)
        with self.assertRaises(access.AccessError):
            access.private_descriptor(path)


if __name__ == '__main__':
    unittest.main()
