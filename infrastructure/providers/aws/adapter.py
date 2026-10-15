"""AWS Seoul adapter. Native evidence is required for every readiness claim."""
from __future__ import annotations
import importlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from .native import AwsError, Session, command, digest, directory, private_json, save
from .terraform import ROOT, Terraform, open_owned, destroy_addon
from .config import require_config, budget, reserve_budget


class Adapter:
    def __init__(self, config):
        self.config = config
        self.runner = command

    def _scope(self, context):
        account = context['account_id']
        # account_id in the common registry can be a connection alias; AWS scope is explicit.
        account = self.config.get('account_id', account)
        if not re.fullmatch(r'\d{12}', account): raise AwsError('AWS_ACCOUNT_INVALID')
        return account

    def _home(self, context):
        return directory(Path(context['state_dir']) / 'aws')

    def _session(self, context):
        return Session(self.config['auth'], self._scope(context), runner=self.runner)

    def _binding(self, context):
        return {k: context[k] for k in ('environment_id', 'owner_id', 'account_id', 'credential_ref', 'policy_revision')}

    def _tf(self, context, name, module, session, extra=None):
        return Terraform(self._home(context) / name, module, session, self._binding(context), runner=self.runner, extra=extra)

    def _spec(self, spec, context):
        require_config(self.config)
        if spec['region'] != 'ap-northeast-2' or spec['runtime'] != {'kind': 'kubeadm', 'control_planes': 1, 'workers': 2}:
            raise AwsError('AWS_TOPOLOGY_UNSUPPORTED')
        eid = context['environment_id']
        if not re.fullmatch(r'[a-z][a-z0-9-]{2,39}', eid): raise AwsError('AWS_ENVIRONMENT_ID_INVALID')
        required = ('ami_id', 'ssh_identity_file', 'ssh_public_key', 'edge_variables_file')
        if any(not self.config.get(k) for k in required): raise AwsError('AWS_OPERATOR_PROFILE_INCOMPLETE')
        if not re.fullmatch(r'ami-[0-9a-f]{17}', self.config['ami_id']): raise AwsError('AWS_AMI_INVALID')
        zones = self.config.get('zones', ['ap-northeast-2a', 'ap-northeast-2c'])
        if len(zones) != 2 or len(set(zones)) != 2 or any(not re.fullmatch(r'ap-northeast-2[a-d]', z) for z in zones):
            raise AwsError('AWS_ZONES_INVALID')
        edge = private_json(self.config['edge_variables_file'])
        if not edge.get('zone_id') or not edge.get('base_domain') or not edge.get('routes'):
            raise AwsError('AWS_DNS_PROFILE_REQUIRED')
        # An exact local SSH key is never accepted from an HTTP request.
        p = Path(self.config['ssh_identity_file']); info = p.lstat()
        if not p.is_absolute() or p.is_symlink() or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise AwsError('AWS_SSH_IDENTITY_INVALID')
        if spec['database']['mode'] == 'standalone':
            sys.path.insert(0, str(ROOT / 'infrastructure/ansible'))
            standalone = importlib.import_module('standalone')
            if not self.config.get('database_config_file') or not standalone.validate_config(private_json(self.config['database_config_file']))['valid']:
                raise AwsError('AWS_DATABASE_CONFIGURATION_REQUIRED')
        return zones

    def connection(self, context):
        session = self._session(context)
        identity = session.call('sts', 'get-caller-identity')
        return {'status': 'succeeded', 'provider': 'aws', 'account_id': self._scope(context), 'region': 'ap-northeast-2', 'authenticated': identity.get('Account') == self._scope(context)}

    def catalog(self, context):
        session = self._session(context)
        zones = session.call('ec2', 'describe-availability-zones', '--filters', 'Name=state,Values=available')['AvailabilityZones']
        return {'regions': ['ap-northeast-2'], 'zones': [r['ZoneName'] for r in zones], 'runtime_profiles': ['kubeadm-1c2w'],
            'instance_types': ['t3.large', 't3.xlarge', 't3.2xlarge'], 'capabilities': ['network.create', 'compute.create', 'compute.start', 'compute.stop', 'compute.reboot', 'runtime.kubeadm', 'storage.efs', 'edge.alb'], 'managed_kubernetes': False}

    def plan(self, spec, context):
        zones = self._spec(spec, context)
        approved = budget(self.config, spec)
        self.connection(context)
        return {'status': 'succeeded', 'provider': 'aws', 'region': spec['region'], 'topology': spec['runtime'],
            'nodes': 4 if spec['database']['mode'] == 'standalone' else 3, 'zones': zones,
            'changes': [{'kind': 'vpc', 'action': 'create', 'count': 1}, {'kind': 'subnet', 'action': 'create', 'count': 2}, {'kind': 'compute', 'action': 'create', 'count': 4 if spec['database']['mode'] == 'standalone' else 3}, {'kind': 'efs', 'action': 'create', 'count': 1}, {'kind': 'alb', 'action': 'create', 'count': 1}],
            'static_public_ip_count': 3, 'database_private_ip': self._database_ip() if spec['database']['mode'] == 'standalone' else None,
            'steps': ['network', 'compute', 'ssm', 'efs', 'csi_identity', 'kubeadm', 'edge', 'deployment_binding'],
            'data_deletion': False, 'network_egress': 'public-ip; control API and metrics restricted to operator /32s',
            'plan_kind': 'environment-intent; native saved plans are checked at each dependency stage',
            'budget': approved, 'input_sha256': digest({'spec': spec, 'binding': self._binding(context)})}

    def _network(self, spec, context, session):
        variables = {'account_id': self._scope(context), 'environment_id': context['environment_id'], 'owner_id': context['owner_id'],
            'cidr': self.config.get('vpc_cidr', '10.72.0.0/16'), 'zones': self._spec(spec, context)}
        template = private_json(self.config['application_template_file'])
        observer = private_json(template['profile']['ingress']['observability_config_file'])
        variables.update(management_source_cidrs=self.config['management_source_cidrs'], observer_source_cidrs=self.config['observer_source_cidrs'],
            metrics_ports=[observer['node_metrics_port'], observer['cluster_metrics_port']])
        variables['database_enabled'] = spec['database']['mode'] == 'standalone'
        variables['database_backup'] = None
        if variables['database_enabled']:
            backup = private_json(self.config['database_config_file'])['backup']
            if backup['identity'] == 'instance':
                if backup['type'] != 's3' or backup['region'] != 'ap-northeast-2' or backup['endpoint'] not in ('s3.ap-northeast-2.amazonaws.com', 's3.amazonaws.com'):
                    raise AwsError('AWS_DATABASE_BACKUP_SCOPE_INVALID')
                variables['database_backup'] = {'bucket': backup['bucket'], 'prefix': backup['prefix'].strip('/') + '/' + context['environment_id']}
        return self._tf(context, 'network', 'aws-network', session).apply(variables)['network']

    def _nodes(self, spec, context, session, network):
        zones = self.config.get('zones', ['ap-northeast-2a', 'ap-northeast-2c'])
        roles = [('control', zones[0]), ('compute-a', zones[0]), ('compute-b', zones[1])]
        if spec['database']['mode'] == 'standalone': roles.append(('database', zones[0]))
        descriptors = {}
        for role, zone in roles:
            node_id = 'rs-' + digest(context['environment_id'])[:20] + '-' + role
            tags = {'Project': 'railshot', 'ManagedBy': 'terraform', 'ProjectOwner': 'railshot-product', 'Target': node_id,
                'Environment': context['environment_id'], 'Owner': context['owner_id']}
            override = {'provider': {'aws': {'default_tags': [{'tags': tags}]}}}
            if role == 'database': override['resource'] = {'aws_instance': {'node': {'private_ip': self._database_ip()}}}
            extra = {'environment_override.tf.json': json.dumps(override)}
            tf = self._tf(context, 'nodes/' + role, 'aws', session, extra)
            variables = {'region': 'ap-northeast-2', 'account_id': self._scope(context), 'name': node_id, 'target_id': node_id,
                'owner_ref': 'terraform:local:' + str(tf.home / 'terraform.tfstate'), 'product_environment': True,
                'ami_id': self.config['ami_id'], 'instance_type': self.config.get('instance_type', 't3.large'),
                'vpc_id': network['vpc_id'], 'subnet_id': network['subnets'][zone], 'allocate_eip': role != 'database',
                'create_ci_plan_role': False, 'http_enabled': False, 'https_enabled': False,
                'existing_instance_profile': network['profiles']['database' if role == 'database' else 'control' if role == 'control' else 'worker'],
                'operator_ssh_public_key': self.config['ssh_public_key'], 'node_name': node_id,
                'purpose': 'database' if role == 'database' else 'runtime', 'initialize_empty_data_disk': True,
                'data_disk_gib': self.config.get('data_disk_gib', 100),
                'additional_security_group_ids': [network['database_sg']] if role == 'database' else [network['cluster_sg'], network['metrics_sg']] + ([network['target_sg']] if role != 'control' else [network['control_api_sg']])}
            descriptors[role] = tf.apply(variables)['node_descriptor']
            if role == 'database' and descriptors[role]['addresses']['private'] != self._database_ip(): raise AwsError('AWS_DATABASE_ADDRESS_MISMATCH', unknown=True)
            save(self._home(context) / (role + '.json'), descriptors[role])
        save(self._home(context) / 'nodes.json', descriptors)
        return descriptors

    def _database_ip(self): return str(ipaddress.ip_network(self.config.get('vpc_cidr', '10.72.0.0/16'))[14])

    def _observe(self, context, session, descriptors):
        ids = [d['instance_id'] for d in descriptors.values()]
        data = session.call('ec2', 'describe-instances', '--instance-ids', *ids)
        rows = {i['InstanceId']: i for r in data.get('Reservations', []) for i in r['Instances']}
        if set(rows) != set(ids): raise AwsError('AWS_RESOURCE_OBSERVATION_INCOMPLETE')
        resources = []
        for role, desc in descriptors.items():
            if desc['resource_id'] != 'arn:aws:ec2:ap-northeast-2:' + self._scope(context) + ':instance/' + desc['instance_id']:
                raise AwsError('AWS_RESOURCE_OWNERSHIP_MISMATCH')
            row = rows[desc['instance_id']]; tags = {t['Key']: t['Value'] for t in row.get('Tags', [])}
            if tags.get('Environment') != context['environment_id'] or tags.get('Owner') != context['owner_id'] or row.get('PrivateIpAddress') != desc['addresses']['private']:
                raise AwsError('AWS_RESOURCE_OWNERSHIP_MISMATCH')
            if role == 'database' and row['PrivateIpAddress'] != self._database_ip(): raise AwsError('AWS_DATABASE_ADDRESS_MISMATCH')
            # Stop does not release a runtime EIP; the instance response may omit
            # PublicIpAddress while stopped, so verify its allocation independently.
            if role != 'database':
                public = desc['addresses']['public']
                allocation = session.call('ec2', 'describe-addresses', '--public-ips', public).get('Addresses', [])
                if not ipaddress.ip_address(public).is_global or len(allocation) != 1 or allocation[0].get('InstanceId') != desc['instance_id'] or allocation[0].get('PrivateIpAddress') != desc['addresses']['private']:
                    raise AwsError('AWS_MANAGEMENT_ADDRESS_MISMATCH')
            resources.append({'kind': 'compute', 'role': role, 'id': desc['resource_id'], 'state': row['State']['Name'],
                'private_ip': row['PrivateIpAddress'], 'zone': row['Placement']['AvailabilityZone'],
                'root_volume_id': next((b['Ebs']['VolumeId'] for b in row.get('BlockDeviceMappings', []) if b.get('DeviceName') == row.get('RootDeviceName') and 'Ebs' in b), None)})
        return resources

    def _runtime(self, spec, context, session, network, descriptors):
        home = self._home(context)
        session.materialize(home / 'session.json')
        # Enrollment uses the authenticated AWS API, never ssh-keyscan.
        members = []
        for role, desc in descriptors.items():
            hostkeys = home / (role + '.known_hosts')
            if not hostkeys.exists():
                raw = self.runner([sys.executable, str(ROOT / 'infrastructure/providers/terraform_tools/access.py'), '--descriptor', str(home / (role + '.json')), '--output', str(hostkeys)], env=session.env(), timeout=650, mutation=True)
                if json.loads(raw).get('status') != 'succeeded': raise AwsError('AWS_HOST_KEY_ENROLLMENT_FAILED', unknown=True)
            if role == 'database': continue
            members.append({'id': desc['target_id'], 'role': 'control_plane' if role == 'control' else 'workers',
                'availability_zone': desc['location']['zone'], 'descriptor_file': str(home / (role + '.json')),
                'ssh': {'user': 'railshot-operator', 'port': 22, 'identity_file': self.config['ssh_identity_file'], 'known_hosts_file': str(hostkeys)}})
        edgevars = private_json(self.config['edge_variables_file'])
        edgevars.update(name='rs-' + digest(context['environment_id'])[:20], vpc_id=network['vpc_id'], public_subnet_ids=list(network['subnets'].values()))
        save(home / 'edge-vars.json', edgevars)
        save(home / 'authority.json', {'config': {'auth': self.config['auth'], 'account_id': self._scope(context)},
            'context': self._binding(context), 'credentials_file': str(home / 'session.json')})
        edge = {'version': 1, 'cluster_id': context['environment_id'], 'provider': 'aws', 'account_id': self._scope(context), 'region': 'ap-northeast-2',
            'variables_file': str(home / 'edge-vars.json'), 'credentials_file': str(home / 'session.json'), 'aws_authority_file': str(home / 'authority.json'),
            'credential_binding': {'account_id': self._scope(context), 'role_arn': self.config['auth']['role_arn'], 'credential_ref': context['credential_ref'], 'policy_revision': context['policy_revision']},
            'worker_bindings': {descriptors[r]['resource_id']: {'security_group_id': network['target_sg']} for r in ('compute-a', 'compute-b')}}
        save(home / 'edge.json', edge)
        binding = {k: edge[k] for k in ('cluster_id', 'provider', 'account_id', 'region')}
        storage = {'backend': 'efs', 'mode': 'create', 'binding': binding, 'state_dir': str(directory(home / 'storage')), 'credentials_file': str(home / 'session.json'),
            'aws': {'vpc_id': network['vpc_id'], 'worker_subnet_ids': {descriptors[r]['target_id']: network['subnets'][descriptors[r]['location']['zone']] for r in ('compute-a', 'compute-b')},
                'identity_mode': 'instance_profile', 'controller_node': descriptors['control']['target_id']}}
        registry = {'version': 1, 'cluster_id': context['environment_id'], 'provider': 'aws', 'account_id': self._scope(context), 'region': 'ap-northeast-2',
            'members': members, 'runtime': {'storage': storage, 'edge': str(home / 'edge.json')}}
        save(home / 'cluster.json', registry)
        sys.path.insert(0, str(ROOT / 'infrastructure/ansible'))
        kubeadm = importlib.import_module('kubeadm'); storage_module = importlib.import_module('storage_profile')
        request = kubeadm.from_registry(registry, request_id=context['environment_id'] + '.runtime')
        with tempfile.TemporaryDirectory() as work:
            prepared = storage_module.prepare(request, work=Path(work), env=session.env(), deadline=time.monotonic() + 1200)
        filesystem = prepared['variables']['railshot_storage']['backend_descriptor']['filesystem_id']
        self._tf(context, 'csi-role', 'aws-csi-role', session).apply({'account_id': self._scope(context), 'role_name': network['control_role'], 'filesystem_id': filesystem, 'cluster_id': context['environment_id']})
        session.materialize(home / 'session.json')
        raw = self.runner([sys.executable, str(ROOT / 'infrastructure/ansible/run.py'), '--cluster-file', str(home / 'cluster.json'),
            '--request-id', context['environment_id'] + '.runtime', '--state-dir', str(home / 'runtime')], env=session.env(), timeout=1900, mutation=True)
        result = json.loads(raw)
        save(home / 'runtime-result.json', result)
        if result.get('status') != 'succeeded': raise AwsError('AWS_RUNTIME_NOT_READY', unknown=bool(result.get('error', {}).get('outcome_unknown')))
        return result

    def apply(self, spec, context):
        self._spec(spec, context)
        reserve_budget(self.config, spec, context)
        home = self._home(context); session = self._session(context)
        save(home / 'spec.json', spec)
        network = self._network(spec, context, session); save(home / 'network.json', network)
        descriptors = self._nodes(spec, context, session, network)
        resources = self._observe(context, session, descriptors)
        if any(r['state'] != 'running' for r in resources):
            return {'status': 'blocked', 'resources': resources, 'blockers': ['AWS_INSTANCES_NOT_RUNNING'], 'readiness': {}}
        result = self._runtime(spec, context, session, network, descriptors)
        if spec['database']['mode'] == 'standalone': self._database({'action': 'install'}, context, session)
        self._application_binding(context, network)
        save(home / 'registration-context.json', context)
        session.materialize(home / 'session.json')
        registered = json.loads(self.runner([sys.executable, str(ROOT / 'infrastructure/providers/aws/register.py'),
            str(home / 'application-config.json'), str(home / 'registration-context.json')], env=session.env(), timeout=1800, mutation=True))
        if registered.get('status') != 'succeeded' or not registered.get('application_binding_receipt_path'):
            raise AwsError('AWS_APPLICATION_REGISTRATION_UNVERIFIED', unknown=True)
        save(home / 'application-binding-result.json', registered)
        return self._result(context, resources, result)

    def _database(self, operation, context, session):
        home = self._home(context)
        config = private_json(self.config['database_config_file'])
        config['ssh'] = {'user': 'railshot-operator', 'identity_file': self.config['ssh_identity_file'], 'known_hosts_file': str(home / 'database.known_hosts'), 'port': 22}
        save(home / 'database-config.json', config)
        database_context = {**context, 'operation_id': (context.get('operation_id') or 'observe') + '.database'}
        save(home / 'database-context.json', database_context)
        request = {'action': operation, 'descriptor_file': str(home / 'database.json'), 'config_file': str(home / 'database-config.json'), 'context_file': str(home / 'database-context.json')}
        save(home / 'database-request.json', request)
        result = json.loads(self.runner([sys.executable, str(ROOT / 'infrastructure/providers/aws/database.py'), str(home / 'database-request.json')], env=session.env(), timeout=1850, mutation=operation['action'] not in ('status', 'verify')))
        save(home / 'database-result.json', result)
        if result.get('status') != 'succeeded' or result.get('ready') is not True:
            raise AwsError('AWS_DATABASE_UNVERIFIED', unknown=result.get('status') == 'unknown')
        return result

    def _application_binding(self, context, network):
        home = self._home(context); eid = context['environment_id']
        template = private_json(self.config['application_template_file'])
        if set(template) != {'version', 'cd', 'profile'} or template['version'] != 1 or template['profile'].get('provider') != 'aws':
            raise AwsError('AWS_APPLICATION_TEMPLATE_INVALID')
        profile = template['profile']
        profile['ingress'].update(aws_authority_file=str(home / 'authority.json'), cluster_file=str(home / 'cluster.json'), cluster_edge_config_file=str(home / 'edge.json'),
            cluster_edge_state_dir=str(home / 'runtime/cluster-edge' / eid), target_security_group_id=network['target_sg'])
        if (home / 'database-result.json').exists():
            profile['ingress']['standalone_database'] = private_json(home / 'database-result.json')['application_bundle_file']
        nodes = private_json(home / 'nodes.json')
        save(home / 'targets.json', {'version': 1, 'targets': {eid: {'profile': 'kubeadm-1c2w', 'cluster_file': str(home / 'cluster.json'), 'purpose': 'runtime',
            'management_endpoint': 'https://' + nodes['control']['addresses']['public'] + ':6443',
            'metrics_endpoints': {n['target_id']: n['addresses']['public'] for role, n in nodes.items() if role != 'database'}}}})
        save(home / 'application-config.json', {'version': 1, 'state_dir': str(directory(home / 'applications')),
            'registry_file': str(home / 'targets.json'), 'cd': template['cd'], 'environments': {eid: profile}})

    def _result(self, context, resources, runtime):
        ready = {'infrastructure': all(r['state'] == 'running' for r in resources), 'cluster': runtime.get('runtime_ready') is True,
            'storage': runtime.get('storage_ready') is True, 'public_access': runtime.get('edge_ready') is True,
            'application_http': runtime.get('public_http_verified') is True, 'database': False, 'deployment_binding': False}
        if (self._home(context) / 'database-result.json').exists():
            database = private_json(self._home(context) / 'database-result.json')
            ready['database'] = ready['infrastructure'] and database.get('ready') is True
        result = {'status': 'succeeded', 'resources': resources, 'readiness': ready,
            'ownership_verified': True, 'mutation_in_progress': any(private_json(p).get('status') == 'unknown' for p in self._home(context).glob('**/attempt.json') if (p.parent / 'binding.json').exists()),
            'runtime_receipt_path': str(self._home(context) / 'runtime-result.json'), 'cluster_file': str(self._home(context) / 'cluster.json')}
        if (self._home(context) / 'application-config.json').exists(): result['application_config_path'] = str(self._home(context) / 'application-config.json')
        if (self._home(context) / 'application-binding-result.json').exists():
            result['application_binding_receipt_path'] = private_json(self._home(context) / 'application-binding-result.json')['application_binding_receipt_path']
        if (self._home(context) / 'database.json').exists(): result['database_descriptor_path'] = str(self._home(context) / 'database.json')
        return result

    def describe(self, context):
        home = self._home(context)
        if not (home / 'nodes.json').exists(): return {'status': 'blocked', 'resources': [], 'readiness': {}, 'blockers': ['AWS_NOT_PROVISIONED']}
        resources = self._observe(context, self._session(context), private_json(home / 'nodes.json'))
        result = self._result(context, resources, {})
        if any(r['state'] != 'running' for r in resources):
            result.update(status='blocked', blockers=['AWS_INSTANCES_NOT_RUNNING'])
            if all(r['state'] == 'stopped' for r in resources): result.update(status='succeeded', state='stopped', stopped_verified=True)
            return result
        if not (home / 'cluster.json').exists():
            result.update(status='blocked', blockers=['AWS_RUNTIME_NOT_PROVISIONED'])
            return result
        session = self._session(context)
        session.materialize(home / 'session.json')
        prior = private_json(home / 'current-observation.json') if (home / 'current-observation.json').exists() else {}
        argv = [sys.executable, str(ROOT / 'infrastructure/providers/aws/probe.py'), str(home / 'cluster.json')]
        if prior.get('cluster_uid'): argv += [prior['cluster_uid']]
        sys.path.insert(0, str(ROOT / 'infrastructure/ansible'))
        kubeadm = importlib.import_module('kubeadm'); storage_module = importlib.import_module('storage_profile')
        registry = private_json(home / 'cluster.json')
        request = kubeadm.from_registry(registry, request_id=context['environment_id'] + '.observe')
        backend = private_json(home / 'storage/observed-backend.json')
        def cloud(argv): return json.loads(self.runner(argv, env=session.env()))
        storage_module._observe(request, cloud, backend['filesystem_id'])
        observed = json.loads(self.runner(argv, env=session.env(), timeout=210))
        save(home / 'current-observation.json', observed)
        result['readiness'].update(cluster=observed['cluster'], storage=observed['storage'])
        # Reconcile edge only through its configured read-only replay path.
        edge_dir = home / 'runtime/cluster-edge' / context['environment_id']
        if (edge_dir / 'journal.json').exists() and private_json(edge_dir / 'journal.json')['phase'] == 'configured':
            sys.path[:0] = [str(ROOT / 'infrastructure/ansible'), str(ROOT / 'deployment/scripts')]
            kubeadm = importlib.import_module('kubeadm'); edge = importlib.import_module('cluster_edge')
            request = kubeadm.from_registry(private_json(home / 'cluster.json'), request_id=context['environment_id'] + '.observe')
            proof = edge.provision(request, state_dir=home / 'runtime/cluster-edge', deadline=time.monotonic() + 180, env=session.env())
            result['readiness']['public_access'] = proof.get('edge_ready') is True
        spec = private_json(home / 'spec.json')
        if spec['database']['mode'] == 'standalone':
            database = self._database({'action': 'status'}, context, session)
            result['readiness']['database'] = database.get('ready') is True
            result['resources'] += [{'kind': 'database_backup', 'id': label, 'status': 'verified'} for label in database.get('backups', [])]
        result['status'] = 'succeeded' if all(result['readiness'][k] for k in ('infrastructure', 'cluster', 'storage')) else 'blocked'
        return result

    def operate(self, operation, context):
        if not isinstance(operation, dict) or not isinstance(operation.get('action'), str): raise AwsError('AWS_OPERATION_INVALID')
        if operation['action'] in ('backup', 'restore'):
            home = self._home(context); session = self._session(context)
            if private_json(home / 'spec.json')['database']['mode'] != 'standalone': raise AwsError('AWS_DATABASE_NOT_CONFIGURED')
            self._observe(context, session, private_json(home / 'nodes.json'))
            self._database(operation, context, session)
            return self.describe(context)
        operation = operation['action']
        if operation in ('reconcile', 'verify'): return self.describe(context)
        if operation not in ('start', 'resume', 'stop', 'reboot'): raise AwsError('AWS_OPERATION_UNSUPPORTED')
        home = self._home(context); session = self._session(context); descriptors = private_json(home / 'nodes.json')
        self._observe(context, session, descriptors)
        marker = home / ('operation-' + digest(context['operation_id']) + '.json')
        identity = {'id': context['operation_id'], 'operation': operation, 'nodes': [d['instance_id'] for d in descriptors.values()]}
        if marker.exists():
            old = private_json(marker)
            if old['identity'] != identity: raise AwsError('AWS_OPERATION_CONFLICT')
            if old['status'] == 'unknown': return self.describe(context)
            return old['result']
        save(marker, {'identity': identity, 'status': 'unknown'})
        action = {'start': 'start-instances', 'resume': 'start-instances', 'stop': 'stop-instances', 'reboot': 'reboot-instances'}[operation]
        session.call('ec2', action, '--instance-ids', *identity['nodes'], mutation=True)
        # AWS waiters are read-only and bounded by the CLI's built-in attempt limit.
        waiter = 'instance-stopped' if operation == 'stop' else 'instance-running'
        self.runner(['aws', 'ec2', 'wait', waiter, '--instance-ids', *identity['nodes'], '--region', 'ap-northeast-2'], env=session.env(), timeout=650)
        result = self.describe(context)
        if operation == 'stop' and result['resources'] and all(r['state'] == 'stopped' for r in result['resources']): result.update(status='succeeded', state='stopped', stopped_verified=True)
        save(marker, {'identity': identity, 'status': 'succeeded', 'result': result})
        return result

    def delete(self, context):
        home = self._home(context); session = self._session(context); binding = self._binding(context)
        marker = home / 'deleted.json'
        if marker.exists(): return private_json(marker)
        descriptors = private_json(home / 'nodes.json')
        # Preflight all ownership before any deletion; common control has already
        # rejected deletion while applications remain attached.
        deletion = home / 'deleting.json'
        if not deletion.exists():
            observed = self._observe(context, session, descriptors)
            save(deletion, {'binding': binding, 'root_volumes': {r['role']: r['root_volume_id'] for r in observed if r.get('root_volume_id')}})
        elif private_json(deletion)['binding'] != binding:
            raise AwsError('AWS_DELETE_BINDING_CHANGED')
        retained = {'root_volumes': private_json(deletion).get('root_volumes', {})}
        registered = home / 'application-binding-result.json'
        if registered.exists():
            save(home / 'registration-context.json', context)
            session.materialize(home / 'session.json')
            result = json.loads(self.runner([sys.executable, str(ROOT / 'infrastructure/providers/aws/register.py'),
                str(home / 'application-config.json'), str(home / 'registration-context.json'), 'teardown'], env=session.env(), timeout=1900, mutation=True))
            if result.get('status') != 'succeeded': raise AwsError('AWS_APPLICATION_TEARDOWN_UNVERIFIED', unknown=True)
            save(home / 'application-teardown-result.json', result)
        elif (Path(context['state_dir']) / 'bootstrap' / context['environment_id']).exists():
            raise AwsError('AWS_PARTIAL_BOOTSTRAP_RECONCILIATION_REQUIRED', unknown=True)
        edge_home = home / 'runtime/cluster-edge' / context['environment_id']
        if (edge_home / 'terraform.tfstate').exists() and not registered.exists():
            if not (edge_home / 'aws-delete.json').exists():
                journal = private_json(edge_home / 'journal.json')
                sys.path.insert(0, str(ROOT / 'deployment/scripts'))
                edge = importlib.import_module('cluster_edge')
                if journal['phase'] != 'configured' or edge.state_identity(edge_home / 'terraform.tfstate', 'aws') != journal['state']:
                    raise AwsError('AWS_EDGE_DELETE_OWNERSHIP_UNPROVEN')
            destroy_addon(edge_home, session, binding, runner=self.runner)
        if (home / 'csi-role/binding.json').exists():
            open_owned(home / 'csi-role', session, binding, runner=self.runner).destroy()
        storage = home / 'storage'
        if (storage / 'backend.tfstate').exists():
            fs = private_json(storage / 'observed-backend.json')
            if fs['account_id'] != self._scope(context) or fs['cluster_id'] != context['environment_id']:
                raise AwsError('AWS_EFS_DELETE_OWNERSHIP_UNPROVEN')
            retained.update(destroy_addon(storage, session, binding, module=ROOT / 'infrastructure/terraform/aws-storage',
                state=storage / 'backend.tfstate', variables=storage / 'backend.tfvars.json',
                retain={'aws_efs_file_system.cluster': fs['filesystem_id']}, runner=self.runner))
        for role, desc in descriptors.items():
            tf = open_owned(home / 'nodes' / role, session, binding, runner=self.runner)
            retained[role] = tf.destroy(retain={'aws_ebs_volume.data': desc['data_disk']['resource_id']})
        open_owned(home / 'network', session, binding, runner=self.runner).destroy()
        retained_rows = []
        def collect(value):
            if isinstance(value, dict):
                for item in value.values(): collect(item)
            elif isinstance(value, str) and value.startswith(('vol-', 'fs-')):
                retained_rows.append({'kind': 'efs' if value.startswith('fs-') else 'ebs', 'id': value, 'status': 'retained'})
        collect(retained)
        result = {'status': 'succeeded', 'deleted': True, 'retained_resources': retained, 'retained': retained_rows, 'resources': [], 'residuals': [],
            'readiness': {k: False for k in ('infrastructure', 'cluster', 'storage', 'database', 'deployment_binding', 'public_access')}}
        save(marker, result)
        return result
