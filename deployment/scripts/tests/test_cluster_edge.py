"""Native cluster edge contracts with a stateful transport, without cloud writes."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import cluster_edge as edge


class Runner:
    def __init__(self, test):
        self.test, self.calls, self.dns_calls = test, [], []
        self.applied, self.fail, self.change, self.bad_output = False, False, None, False

    def __call__(self, args, *, cwd, timeout=300):
        self.calls.append(args)
        operation = args[1]
        if operation == 'plan':
            name = args[-1].split('=', 1)[1]
            edge.durable_write(cwd / name, b'exact saved plan')
        if operation == 'show':
            resources = self.test.resources
            changes = [{'address': kind + '.' + name + ('[' + json.dumps(key) + ']' if key else ''),
                        'mode': 'managed', 'type': kind, 'change': {
                            'actions': ['no-op'] if self.applied else ['create'],
                            'before': {'id': identity} if self.applied else None,
                            'after': {'id': identity}, 'after_unknown': {}}}
                       for kind, name, key, identity in resources]
            if self.change:
                changes = self.change(changes)
            return json.dumps({'resource_changes': changes}).encode()
        if operation == 'apply':
            self.test.assertEqual((cwd / args[-1]).read_bytes(), b'exact saved plan')
            self.applied = True
            grouped = {}
            for kind, name, key, identity in self.test.resources:
                resource = grouped.setdefault((kind, name), {'mode': 'managed', 'type': kind, 'name': name, 'instances': []})
                resource['instances'].append({'attributes': {'id': identity}, **({'index_key': key} if key else {})})
            edge.durable_write(cwd / 'terraform.tfstate', edge.encoded({
                'version': 4, 'serial': 1, 'lineage': 'cluster-only', 'resources': list(grouped.values())}))
            if self.fail:
                raise OSError('provider secret text must not escape')
        if operation == 'output':
            values = {'alb_dns_name': 'lb.ap-northeast-2.elb.amazonaws.com',
                      'target_group_arns': {'app-a': 'arn:tg/app'},
                      'certificate_arn': 'arn:aws:acm:ap-northeast-2:123456789012:certificate/abcd'}
            if self.bad_output:
                values['alb_dns_name'] = 'evil.example.com'
            return edge.encoded({name: {'value': value} for name, value in values.items()})
        return b''

    def dns(self, config, request, *, observe=False):
        self.dns_calls.append(request)
        return {'status': 'verified', **request}


class ClusterEdgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_file = self.root / 'edge.json'
        self.variables_file = self.root / 'variables.json'
        self.request = {'runtime': {'profile': 'kubeadm-1c2w', 'edge': str(self.config_file)},
                        'target': {'id': 'cluster-a', 'provider': 'aws', 'account_id': '123456789012', 'placement': 'ap-northeast-2'},
                        'inventory': {'control_plane': [self.node('control', 1)],
                                      'workers': [self.node('compute-a', 2), self.node('compute-b', 3)]}}
        self.config = {'version': 1, 'cluster_id': 'cluster-a', 'provider': 'aws', 'account_id': '123456789012',
                       'region': 'ap-northeast-2', 'variables_file': str(self.variables_file), 'credentials_file': str(self.root / 'cloud-credentials.json'),
                       'worker_bindings': {node['resource_id']: {'security_group_id': 'sg-' + str(index) * 17}
                                           for index, node in enumerate(self.request['inventory']['workers'], 2)}}
        self.values = {'name': 'cluster-a', 'vpc_id': 'vpc-0123456789abcdef0',
                       'public_subnet_ids': ['subnet-0123456789abcdef0', 'subnet-0123456789abcdef1'],
                       'base_domain': 'example.com', 'zone_id': 'ZEXAMPLE', 'routes': {'app-a': {
                           'host': 'app.example.com', 'node_port': 30080, 'health_path': '/health', 'priority': 10}}}
        edge.durable_write(self.root / 'cloud-credentials.json', edge.encoded({'AWS_ACCESS_KEY_ID': 'key-fixture', 'AWS_SECRET_ACCESS_KEY': 'private-fixture'}))
        self.resources = [('aws_lb', 'app', None, 'lb-owned'),
                          ('aws_lb_target_group_attachment', 'app', 'app-a', 'worker-a-attachment'),
                          ('aws_lb_target_group_attachment', 'compute', 'app-a:compute-b', 'worker-b-attachment')]
        self.write()
        self.runner = Runner(self)

    def test_sts_rotation_preserves_explicit_role_binding(self):
        self.config['credential_binding'] = {'account_id': '123456789012', 'role_arn': 'arn:aws:iam::123456789012:role/railshot', 'credential_ref': 'customer-a', 'policy_revision': 'v1'}
        credentials = self.root / 'cloud-credentials.json'
        edge.durable_write(credentials, edge.encoded({'AWS_ACCESS_KEY_ID': 'session-a', 'AWS_SECRET_ACCESS_KEY': 'private-a', 'AWS_SESSION_TOKEN': 'token-a'}))
        self.write()
        first = edge.validate(self.request)['binding_sha256']
        edge.durable_write(credentials, edge.encoded({'AWS_ACCESS_KEY_ID': 'session-b', 'AWS_SECRET_ACCESS_KEY': 'private-b', 'AWS_SESSION_TOKEN': 'token-b'}))
        self.assertEqual(first, edge.validate(self.request)['binding_sha256'])
        self.config['credential_binding']['role_arn'] = 'arn:aws:iam::123456789012:role/other'
        self.write()
        self.assertNotEqual(first, edge.validate(self.request)['binding_sha256'])

    @staticmethod
    def node(role, index):
        return {'id': role, 'resource_id': 'arn:aws:ec2:ap-northeast-2:123456789012:instance/i-' + str(index) * 17,
                'private_ipv4': '10.10.0.' + str(index), 'availability_zone': 'ap-northeast-2' + chr(96 + index)}

    def write(self):
        edge.durable_write(self.config_file, edge.encoded(self.config))
        edge.durable_write(self.variables_file, edge.encoded(self.values))

    def run_edge(self):
        return edge.provision(self.request, state_dir=self.root, runner=self.runner)

    def test_worker_binding_renders_only_two_compute_and_exact_health(self):
        values = edge.validate(self.request)['values']
        self.assertEqual(set(values['compute_nodes']), {'compute-a', 'compute-b'})
        self.assertNotIn('control', values['compute_nodes'])
        self.assertEqual(values['routes']['app-a']['target_private_ip'], '10.10.0.2')
        self.assertEqual(values['routes']['app-a']['expected_status'], 200)

    def test_control_target_and_control_worker_binding_are_rejected(self):
        self.values['routes']['app-a']['target_private_ip'] = '10.10.0.1'
        self.write()
        with self.assertRaisesRegex(edge.EdgeError, 'PRIMARY_COMPUTE'):
            edge.validate(self.request)
        self.values['routes']['app-a'].pop('target_private_ip')
        self.config['worker_bindings'][self.request['inventory']['control_plane'][0]['resource_id']] = {'security_group_id': 'sg-' + '1' * 17}
        self.write()
        with self.assertRaisesRegex(edge.EdgeError, 'COMPUTE_BINDING'):
            edge.validate(self.request)

    def test_wrong_provider_account_region_duplicate_and_cross_az_admission(self):
        for field, value in (('provider', 'gcp'), ('account_id', '111111111111'), ('region', 'eu-west-1')):
            previous = self.config[field]
            self.config[field] = value
            self.write()
            with self.subTest(field=field), self.assertRaisesRegex(edge.EdgeError, 'SCOPE_MISMATCH'):
                edge.validate(self.request)
            self.config[field] = previous
        self.write()
        # Different AZs inside one region are accepted.
        self.assertEqual(len(edge.validate(self.request)['workers']), 2)
        self.request['inventory']['workers'][1]['availability_zone'] = 'eu-west-1a'
        with self.assertRaisesRegex(edge.EdgeError, 'NODE_SCOPE'):
            edge.validate(self.request)

    def test_mixed_provider_and_duplicate_resource_node_are_rejected(self):
        self.request['inventory']['workers'][1]['provider'] = 'gcp'
        with self.assertRaisesRegex(edge.EdgeError, 'NODE_SCOPE'):
            edge.validate(self.request)
        self.request['inventory']['workers'][1].pop('provider')
        self.request['inventory']['workers'][1]['resource_id'] = self.request['inventory']['control_plane'][0]['resource_id']
        with self.assertRaisesRegex(edge.EdgeError, 'DUPLICATE_NODE'):
            edge.validate(self.request)

    def test_certificate_and_dns_authority_cannot_cross_scope(self):
        for field, value, code in (('certificate_arn', 'arn:aws:acm:eu-west-1:123456789012:certificate/abcd', 'CERTIFICATE_SCOPE'),
                                   ('base_domain', 'elsewhere.com', 'DNS_SCOPE')):
            self.values[field] = value
            self.write()
            with self.subTest(field=field), self.assertRaisesRegex(edge.EdgeError, code):
                edge.validate(self.request)
            self.values.pop(field)
            self.values['base_domain'] = 'example.com'
        self.values['routes']['app-a']['manage_dns'] = False
        self.write()
        with self.assertRaisesRegex(edge.EdgeError, 'ROUTE_SCOPE'):
            edge.validate(self.request)

    def test_private_files_and_duplicate_json_are_required(self):
        self.config_file.chmod(0o644)
        with self.assertRaisesRegex(edge.EdgeError, 'PRIVATE_FILE'):
            edge.validate(self.request)
        self.config_file.chmod(0o600)
        edge.durable_write(self.config_file, b'{"version":1,"version":1}')
        with self.assertRaisesRegex(edge.EdgeError, 'DUPLICATE_JSON'):
            edge.validate(self.request)

    def test_configured_stage_replays_without_writes_or_false_live_success(self):
        result = self.run_edge()
        self.assertTrue(result['edge_ready'])
        self.assertTrue(result['configured'])
        for field in ('lb_healthy', 'dns_resolved', 'tls_verified', 'public_http_verified'):
            self.assertIs(result[field], False)
        calls = len(self.runner.calls)
        self.assertEqual(self.run_edge(), result)
        self.assertEqual([call[1] for call in self.runner.calls[calls:]], ['init', 'validate', 'plan', 'show', 'output'])
        self.assertEqual(sum(call[1] == 'apply' for call in self.runner.calls), 1)

    def test_partial_apply_is_durable_unknown_and_never_replayed(self):
        self.runner.fail = True
        with self.assertRaises(edge.EdgeError) as failure:
            self.run_edge()
        self.assertTrue(failure.exception.unknown)
        self.assertNotIn('secret', str(failure.exception))
        calls = len(self.runner.calls)
        with self.assertRaises(edge.EdgeError) as failure:
            self.run_edge()
        self.assertTrue(failure.exception.unknown)
        self.assertEqual(len(self.runner.calls), calls)

    def test_readback_failure_after_successful_write_requires_reconciliation(self):
        self.runner.bad_output = True
        with self.assertRaisesRegex(edge.EdgeError, 'OUTCOME_UNKNOWN'):
            self.run_edge()
        journal = edge.private_json(self.root / 'cluster-a/journal.json')
        self.assertEqual(journal['phase'], 'configuring')
        with self.assertRaisesRegex(edge.EdgeError, 'OUTCOME_UNKNOWN'):
            self.run_edge()

    def test_foreign_state_never_adopted(self):
        root = edge.private_directory(self.root / 'cluster-a')
        edge.durable_write(root / 'terraform.tfstate', b'{}')
        with self.assertRaisesRegex(edge.EdgeError, 'ADOPTION_FORBIDDEN'):
            self.run_edge()
        self.assertEqual(self.runner.calls, [])

    def test_changed_binding_or_state_after_success_are_rejected(self):
        self.run_edge()
        self.values['routes']['app-a']['node_port'] = 30081
        self.write()
        with self.assertRaisesRegex(edge.EdgeError, 'SCOPE_CHANGED'):
            self.run_edge()
        self.values['routes']['app-a']['node_port'] = 30080
        self.write()
        path = self.root / 'cluster-a/terraform.tfstate'
        state = edge.private_json(path)
        state['lineage'] = 'someone-elses-lineage'
        edge.durable_write(path, edge.encoded(state))
        with self.assertRaisesRegex(edge.EdgeError, 'STATE_CHANGED'):
            self.run_edge()

    def test_plan_deletion_replacement_import_and_foreign_resource_rejected_before_apply(self):
        original = {'address': 'aws_lb.app', 'mode': 'managed', 'type': 'aws_lb',
                    'change': {'actions': ['create'], 'before': None, 'after': {'id': 'new'}}}
        for actions in (['delete'], ['delete', 'create'], ['update']):
            item = copy.deepcopy(original)
            item['change']['actions'] = actions
            with self.subTest(actions=actions), self.assertRaises(edge.EdgeError):
                edge.validate_plan({'resource_changes': [item]}, 'aws', {})
        item = copy.deepcopy(original)
        item['change']['importing'] = {'id': 'foreign'}
        with self.assertRaisesRegex(edge.EdgeError, 'PLAN_FOREIGN'):
            edge.validate_plan({'resource_changes': [item]}, 'aws', {})
        item = copy.deepcopy(original)
        item.update(type='aws_instance', address='aws_instance.illegal')
        with self.assertRaisesRegex(edge.EdgeError, 'NON_EDGE_RESOURCE'):
            edge.validate_plan({'resource_changes': [item]}, 'aws', {})

    def test_expected_health_status_and_path_are_not_generic_success(self):
        for field, value in (('expected_status', 204), ('health_path', '/health?x=1'), ('path_prefix', '/api.*')):
            self.values['routes']['app-a'][field] = value
            self.write()
            with self.subTest(field=field), self.assertRaises(edge.EdgeError):
                edge.validate(self.request)
            self.values['routes']['app-a'].pop(field)
            self.values['routes']['app-a']['health_path'] = '/health'

    def test_private_aws_auth_overrides_ambient_auth_and_works_with_child_env(self):
        prepared = edge.validate(self.request)
        original = {'PATH': '/usr/bin', 'HOME': str(self.root), 'AWS_SECRET_ACCESS_KEY': 'foreign-ambient',
                    'TF_VAR_account_id': 'foreign', 'TF_WORKSPACE': 'foreign'}
        runner = edge.NativeRunner(prepared, time.monotonic() + 60, env=original)
        self.assertEqual(runner.env['AWS_SECRET_ACCESS_KEY'], 'private-fixture')
        self.assertEqual(runner.env['AWS_CONFIG_FILE'], '/dev/null')
        self.assertEqual(runner.env['AWS_EC2_METADATA_DISABLED'], 'true')
        self.assertNotIn('TF_VAR_account_id', runner.env)
        self.assertNotIn('TF_WORKSPACE', runner.env)
        self.assertNotIn('private-fixture', json.dumps(prepared))
        old = prepared['binding_sha256']
        edge.durable_write(self.root / 'cloud-credentials.json', edge.encoded({'AWS_ACCESS_KEY_ID': 'key-new', 'AWS_SECRET_ACCESS_KEY': 'changed-secret'}))
        self.assertEqual(edge.validate(self.request)['binding_sha256'], old)
        self.assertEqual(edge.NativeRunner(edge.validate(self.request), time.monotonic() + 60, env=original).env['AWS_SECRET_ACCESS_KEY'], 'changed-secret')

    def test_gcp_explicit_adc_and_openstack_application_auth_scope(self):
        config = dict(self.config, provider='gcp', account_id='railshot-project', region='asia-northeast3')
        edge.durable_write(self.root / 'cloud-credentials.json', edge.encoded({'type': 'service_account', 'project_id': 'railshot-project',
            'client_email': 'fixture@railshot-project.iam.gserviceaccount.com', 'private_key': '-----BEGIN PRIVATE KEY-----fixture-----END PRIVATE KEY-----',
            'token_uri': 'https://oauth2.googleapis.com/token'}))
        env = edge.credential_env(config, {'PATH': '/usr/bin', 'GOOGLE_APPLICATION_CREDENTIALS': '/foreign/file'})
        self.assertEqual(env['GOOGLE_APPLICATION_CREDENTIALS'], self.config['credentials_file'])
        self.assertEqual(env['CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE'], self.config['credentials_file'])
        config.update(provider='openstack', account_id='44444444-4444-4444-4444-444444444444', region='RegionOne')
        credentials = {'OS_AUTH_URL': 'https://identity.example.com/v3', 'OS_PROJECT_ID': config['account_id'],
                       'OS_APPLICATION_CREDENTIAL_ID': '44444444-4444-4444-4444-444444444444', 'OS_APPLICATION_CREDENTIAL_SECRET': 'private-secret'}
        edge.durable_write(self.root / 'cloud-credentials.json', edge.encoded(credentials))
        env = edge.credential_env(config, {'PATH': '/usr/bin', 'OS_CLOUD': 'foreign', 'OS_AUTH_URL': 'https://foreign.example.com/v3'})
        self.assertEqual(env['OS_AUTH_URL'], credentials['OS_AUTH_URL'])
        self.assertEqual(env['OS_REGION_NAME'], 'RegionOne')
        self.assertEqual(env['OS_AUTH_TYPE'], 'v3applicationcredential')
        self.assertNotIn('OS_CLOUD', env)
        credentials['OS_PROJECT_ID'] = 'foreign-project'
        edge.durable_write(self.root / 'cloud-credentials.json', edge.encoded(credentials))
        with self.assertRaisesRegex(edge.EdgeError, 'CREDENTIAL_INVALID'):
            edge.credential_env(config, {'PATH': '/usr/bin'})

    def test_bound_config_changed_during_prior_stages_blocks_before_cloud_writes(self):
        binding = edge.validate(self.request)['binding_sha256']
        self.values['routes']['app-a']['node_port'] = 30081
        self.write()
        with self.assertRaisesRegex(edge.EdgeError, 'SCOPE_CHANGED'):
            edge.provision(self.request, state_dir=self.root, runner=self.runner, expected_binding_sha256=binding)
        self.assertEqual(self.runner.calls, [])
        self.assertFalse((self.root / 'cluster-a').exists())

    def test_expired_deadline_prevents_any_stage_creation_or_command(self):
        with self.assertRaisesRegex(edge.EdgeError, 'DEADLINE_EXCEEDED'):
            edge.provision(self.request, state_dir=self.root, runner=self.runner, deadline=time.monotonic() - 1)
        self.assertFalse((self.root / 'cluster-a').exists())
        self.assertEqual(self.runner.calls, [])

    def test_same_scope_replay_observes_provider_drift_without_apply(self):
        self.run_edge()
        self.runner.change = lambda changes: [{**row, 'change': {**row['change'], 'actions': ['create'], 'before': None}} for row in changes]
        with self.assertRaisesRegex(edge.EdgeError, 'EXISTING_CHANGE'):
            self.run_edge()
        self.assertEqual(sum(call[1] == 'apply' for call in self.runner.calls), 1)

    def test_distinct_cluster_ids_get_distinct_stage_states(self):
        self.run_edge()
        self.request['target']['id'] = 'cluster-b'
        self.config['cluster_id'] = 'cluster-b'
        self.write()
        self.runner.applied = False
        self.run_edge()
        self.assertTrue((self.root / 'cluster-a/terraform.tfstate').exists())
        self.assertTrue((self.root / 'cluster-b/terraform.tfstate').exists())
        self.assertEqual(sum(call[1] == 'apply' for call in self.runner.calls), 2)

    def test_gcp_projects_zones_and_dns_authorization_hook_input(self):
        target = self.request['target']
        target.update(provider='gcp', account_id='railshot-project', placement='asia-northeast3')
        for index, node in enumerate(self.request['inventory']['control_plane'] + self.request['inventory']['workers']):
            node['availability_zone'] = 'asia-northeast3-' + chr(97 + index)
            node['resource_id'] = f"projects/railshot-project/zones/{node['availability_zone']}/instances/{node['id']}"
        self.config.update(provider='gcp', account_id=target['account_id'], region=target['placement'],
                           dns_config_file=str(self.root / 'dns.json'),
                           worker_bindings={node['resource_id']: {} for node in self.request['inventory']['workers']})
        edge.durable_write(self.root / 'dns.json', edge.encoded({'version': 1, 'zone_id': 'a' * 32, 'base_domain': 'example.com',
            'token_file': str(self.root / 'token'), 'state_dir': str(self.root / 'dns-state')}))
        edge.durable_write(self.root / 'cloud-credentials.json', edge.encoded({'type': 'service_account', 'project_id': 'railshot-project', 'client_email': 'fixture@railshot-project.iam.gserviceaccount.com', 'private_key': '-----BEGIN PRIVATE KEY-----fixture-----END PRIVATE KEY-----', 'token_uri': 'https://oauth2.googleapis.com/token'}))
        self.values = {'hostname': 'app.example.com', 'node_port': 30080, 'health_path': '/health'}
        self.write()
        prepared = edge.validate(self.request)
        self.assertEqual(prepared['values']['certificate_dns_owner_id'], 'cluster-a')
        self.assertEqual({row['availability_zone'] for row in prepared['values']['compute_nodes'].values()},
                         {'asia-northeast3-b', 'asia-northeast3-c'})
        before = prepared['binding_sha256']
        credentials = self.root / 'cloud-credentials.json'
        adc = edge.private_json(credentials)
        adc['private_key'] = '-----BEGIN PRIVATE KEY-----rotated-fixture-----END PRIVATE KEY-----'
        edge.durable_write(credentials, edge.encoded(adc))
        self.assertEqual(edge.validate(self.request)['binding_sha256'], before)
        adc['client_email'] = 'another@railshot-project.iam.gserviceaccount.com'
        edge.durable_write(credentials, edge.encoded(adc))
        self.assertNotEqual(edge.validate(self.request)['binding_sha256'], before)
        self.config['credential_binding'] = {'account_id': '123456789012', 'role_arn': 'arn:aws:iam::123456789012:role/railshot', 'credential_ref': 'customer-a', 'policy_revision': 'v1'}
        self.write()
        with self.assertRaisesRegex(edge.EdgeError, 'CREDENTIAL_BINDING_INVALID'):
            edge.validate(self.request)
        self.config.pop('credential_binding'); self.write()
        before = edge.validate(self.request)['binding_sha256']
        dns_authority = edge.private_json(self.root / 'dns.json')
        dns_authority['zone_id'] = 'b' * 32
        edge.durable_write(self.root / 'dns.json', edge.encoded(dns_authority))
        self.assertNotEqual(edge.validate(self.request)['binding_sha256'], before)
        self.request['inventory']['workers'][0]['resource_id'] = 'projects/foreign-project/zones/asia-northeast3-b/instances/compute-a'
        self.config['worker_bindings'] = {node['resource_id']: {} for node in self.request['inventory']['workers']}
        self.write()
        with self.assertRaisesRegex(edge.EdgeError, 'NODE_SCOPE'):
            edge.validate(self.request)

class TunnelBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tunnel_id = '11111111-1111-1111-1111-111111111111'
        credentials = {'AccountTag': 'a' * 32, 'TunnelID': self.tunnel_id,
                       'TunnelSecret': 'MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE='}
        edge.durable_write(self.root / 'credentials.json', edge.encoded(credentials))
        self.config = {'version': 1, 'profile': 'kubeadm-1c2w', 'cluster_id': 'cluster-a', 'namespace': 'cluster-public',
                       'name': 'cluster-tunnel', 'tunnel_id': self.tunnel_id, 'credentials_secret': 'tunnel-credentials',
                       'credentials_file': str(self.root / 'credentials.json'), 'origin_vip': '10.10.0.10'}
        self.request = {'environment_id': 'cluster-a', 'application_id': 'app-aaaaaaaaaaaaaaaaaaaaaaaa',
                        'app': 'web', 'tenant': 'demo', 'hostname': 'app.example.com'}
        self.objects, self.mutations = {}, []

    def kube(self, ns, *args, document=None):
        if args[0] == 'get':
            return copy.deepcopy(self.objects.get((ns, args[1], args[2])))
        self.assertEqual(args[0], 'create')
        self.mutations.append(document['kind'])
        observed = copy.deepcopy(document)
        observed['metadata']['uid'] = 'uid-' + str(len(self.objects))
        if observed['kind'] == 'Deployment':
            observed['status'] = {'availableReplicas': 1}
        key = (ns, observed['kind'], observed['metadata']['name'])
        self.assertNotIn(key, self.objects)
        self.objects[key] = observed
        return observed

    def test_new_connector_creates_private_secret_and_excludes_control_then_readonly_replay(self):
        receipt = edge.cluster_tunnel(self.config, self.request, self.kube, self.root)
        self.assertFalse(receipt['https_verified'])
        self.assertEqual(receipt['tunnel_id'], self.tunnel_id)
        self.assertTrue(receipt['connector_available'])
        deployment = self.objects[('cluster-public', 'Deployment', 'cluster-tunnel')]
        pod = deployment['spec']['template']['spec']
        self.assertEqual(pod['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms'][0]['matchExpressions'],
                         [{'key': 'node-role.kubernetes.io/control-plane', 'operator': 'DoesNotExist'}])
        self.assertNotIn('tolerations', pod)
        self.assertNotIn('TunnelSecret', json.dumps(receipt))
        count = len(self.mutations)
        self.assertEqual(edge.cluster_tunnel(self.config, self.request, self.kube, self.root, observe=True), receipt)
        self.assertEqual(len(self.mutations), count)

    def test_unowned_connector_object_and_changed_uid_cannot_be_adopted(self):
        self.objects[('', 'Namespace', 'cluster-public')] = {'kind': 'Namespace', 'metadata': {'name': 'cluster-public', 'uid': 'foreign'}}
        with self.assertRaisesRegex(edge.EdgeError, 'OBJECT_CONFLICT'):
            edge.cluster_tunnel(self.config, self.request, self.kube, self.root)
        self.assertEqual(self.mutations, [])
        self.objects.clear()
        edge.cluster_tunnel(self.config, self.request, self.kube, self.root)
        self.objects[('cluster-public', 'Deployment', 'cluster-tunnel')]['metadata']['uid'] = 'new-unowned'
        with self.assertRaisesRegex(edge.EdgeError, 'OBJECT_CONFLICT'):
            edge.cluster_tunnel(self.config, self.request, self.kube, self.root, observe=True)

    def test_missing_owned_resource_on_readback_does_not_recreate(self):
        edge.cluster_tunnel(self.config, self.request, self.kube, self.root)
        self.objects.pop(('cluster-public', 'Secret', 'tunnel-credentials'))
        count = len(self.mutations)
        with self.assertRaisesRegex(edge.EdgeError, 'OBJECT_MISSING'):
            edge.cluster_tunnel(self.config, self.request, self.kube, self.root, observe=True)
        self.assertEqual(len(self.mutations), count)


if __name__ == '__main__':
    unittest.main()
