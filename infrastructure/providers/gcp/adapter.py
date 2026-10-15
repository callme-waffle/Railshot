"""Seoul kubeadm environment using owned Terraform and authenticated IAP."""
import importlib
import ipaddress
import json
from pathlib import Path
import re
import sys
import time
from .native import GcpError, ROOT, command, digest, directory, private_json, save
from .auth import Session
from .config import require_config, budget
from .terraform import Terraform, owned


class Adapter:
    def __init__(self, config):
        self.config, self.runner = config, command

    def _home(self, context): return directory(Path(context['state_dir']) / 'gcp')
    def _binding(self, context): return {k: context[k] for k in ('environment_id', 'owner_id', 'account_id', 'credential_ref', 'policy_revision')}
    def _session(self, context): return Session(self.config['auth'], self.config['project_id'], self._home(context) / 'auth', runner=self.runner)
    def _name(self, context): return 'rs-' + digest(context['environment_id'])[:15]
    def _tf(self, context, name, module, session, extra=None):
        return Terraform(self._home(context) / name, module, session, self._binding(context), runner=self.runner, extra=extra)

    def _spec(self, spec, context):
        require_config(self.config)
        if spec['provider'] != 'gcp' or spec['region'] != 'asia-northeast3' or spec['runtime'] != {'kind': 'kubeadm', 'control_planes': 1, 'workers': 2}:
            raise GcpError('GCP_TOPOLOGY_UNSUPPORTED')
        if not re.fullmatch(r'env-[a-f0-9]{24,28}', context['environment_id']): raise GcpError('GCP_ENVIRONMENT_ID_INVALID')
        if spec['database']['mode'] == 'standalone':
            sys.path.insert(0, str(ROOT / 'infrastructure/ansible'))
            import standalone
            if not self.config.get('database_config_file') or not standalone.validate_config(private_json(self.config['database_config_file']))['valid']:
                raise GcpError('GCP_DATABASE_CONFIGURATION_REQUIRED')
            backup = private_json(self.config['database_config_file'])['backup']
            if backup['type'] != 'gcs': raise GcpError('GCP_DATABASE_GCS_REQUIRED')

    def connection(self, context):
        project = self._session(context).call('projects', 'describe', self.config['project_id'])
        if project.get('projectId') != self.config['project_id'] or str(project.get('projectNumber')) != self.config['project_number'] or project.get('lifecycleState') != 'ACTIVE':
            raise GcpError('GCP_PROJECT_IDENTITY_MISMATCH')
        return {'status': 'succeeded', 'provider': 'gcp', 'authenticated': True, 'region': 'asia-northeast3'}

    def catalog(self, context):
        self.connection(context)
        zones = self._session(context).call('compute', 'zones', 'list', '--filter=region:asia-northeast3')
        return {'regions': ['asia-northeast3'], 'zones': [z['name'] for z in zones if z.get('status') == 'UP'],
            'runtime_profiles': ['kubeadm-1c2w'], 'machine_types': ['e2-standard-2', 'e2-standard-4', 'e2-standard-8'],
            'capabilities': ['network.create', 'compute.create', 'compute.start', 'compute.stop', 'runtime.kubeadm', 'storage.filestore', 'edge.https', 'database.standalone'], 'managed_kubernetes': False}

    def plan(self, spec, context):
        self._spec(spec, context); approved = budget(self.config, spec); self.connection(context)
        count = 4 if spec['database']['mode'] == 'standalone' else 3
        return {'status': 'succeeded', 'provider': 'gcp', 'region': 'asia-northeast3', 'topology': spec['runtime'], 'budget': approved,
            'changes': [{'kind': k, 'action': 'create', 'count': n} for k, n in (('network', 1), ('compute', count), ('static_public_ip', count), ('filestore', 1), ('https_load_balancer', 1))],
            'database_private_ip': self._database_ip() if count == 4 else None, 'data_policy': 'retain-filestore-network-disks',
            'plan_kind': 'environment intent; each dependency uses a checked native saved plan', 'input_sha256': digest({'spec': spec, 'binding': self._binding(context)})}

    def _database_ip(self): return str(ipaddress.ip_network(self.config.get('subnet_cidr', '10.76.0.0/24'))[14])

    def _network(self, context, session):
        template = private_json(self.config['application_template_file'])
        observer = private_json(template['profile']['ingress']['observability_config_file'])
        variables = {'project_id': self.config['project_id'], 'name': self._name(context), 'subnet_cidr': self.config.get('subnet_cidr', '10.76.0.0/24'),
            'filestore_cidr': self.config.get('filestore_cidr', '10.77.0.0/26'), 'observer_source_cidrs': self.config['observer_source_cidrs'],
            'metrics_ports': [observer['node_metrics_port'], observer['cluster_metrics_port']]}
        return self._tf(context, 'network', 'gcp-network', session).apply(variables)['network']

    def _nodes(self, spec, context, session, network):
        zones = self.config.get('zones', ['asia-northeast3-a', 'asia-northeast3-b', 'asia-northeast3-c'])
        roles = [('control', zones[0], 'cp'), ('compute-a', zones[1], 'wa'), ('compute-b', zones[2], 'wb')]
        if spec['database']['mode'] == 'standalone': roles.append(('database', zones[0], 'db'))
        nodes = {}; name = self._name(context)
        for role, zone, suffix in roles:
            target = name + '-' + suffix
            instance = {'tags': [name + '-database'] if role == 'database' else [name + '-cluster'] + ([] if role == 'control' else [name + '-worker'])}
            extra = {'locals': {'labels': {'project': 'railshot', 'environment': context['environment_id'], 'owner': digest(context['owner_id'])[:32], 'managed_by': 'terraform'}},
                'resource': {'google_compute_instance': {'node': instance}}}
            if role == 'database':
                # Repeat the complete network block: override blocks replace nested blocks.
                instance['network_interface'] = [{'subnetwork': '${local.subnetwork_ref}', 'network_ip': self._database_ip(), 'access_config': [{'nat_ip': '${google_compute_address.node.address}'}]}]
                backup = private_json(self.config['database_config_file'])['backup']
                if backup['identity'] == 'instance':
                    instance['service_account'] = [{'email': '${google_service_account.node.email}', 'scopes': ['https://www.googleapis.com/auth/cloud-platform']}]
                    # A dedicated bucket is required. Object listing is bucket scoped;
                    # workers/control have neither OAuth scope nor this IAM binding.
                    extra['resource']['google_storage_bucket_iam_member'] = {'database_backup': {'bucket': backup['bucket'], 'role': 'roles/storage.objectAdmin', 'member': 'serviceAccount:${google_service_account.node.email}'}}
            tf = self._tf(context, 'nodes/' + role, 'gcp', session, {'environment_override.tf.json': json.dumps(extra)})
            variables = {'project_id': self.config['project_id'], 'region': 'asia-northeast3', 'zone': zone, 'name': target, 'target_id': target, 'node_name': target,
                'owner_ref': 'terraform:local:' + str(tf.home / 'terraform.tfstate'), 'boot_image': self.config['boot_image'], 'machine_type': self.config.get('machine_type', 'e2-standard-4'),
                'data_disk_gib': self.config.get('data_disk_gib', 100), 'initialize_empty_data_disk': True, 'allow_iap_ssh': True, 'operator_ssh_public_key': self.config['ssh_public_key'],
                'existing_network_name': network['name'], 'existing_subnetwork_name': network['subnet_name'], 'purpose': 'database' if role == 'database' else 'runtime',
                'k3s_control_source_cidrs': self.config['management_source_cidrs'] if role == 'control' else []}
            nodes[role] = tf.apply(variables)['node_descriptor']
            if role == 'database' and nodes[role]['addresses']['private'] != self._database_ip(): raise GcpError('GCP_DATABASE_ADDRESS_MISMATCH', unknown=True)
            save(self._home(context) / (role + '.json'), nodes[role])
        save(self._home(context) / 'nodes.json', nodes)
        return nodes

    def _observe(self, context, session, nodes):
        resources = []
        for role, node in nodes.items():
            name, zone = node['target_id'], node['location']['zone']
            expected = 'projects/' + self.config['project_id'] + '/zones/' + zone + '/instances/' + name
            if node['resource_id'] != expected: raise GcpError('GCP_RESOURCE_SCOPE_MISMATCH')
            row = session.call('compute', 'instances', 'describe', name, '--zone=' + zone)
            addresses = row.get('networkInterfaces', []); labels = row.get('labels', {})
            if str(row.get('id')) != str(node['instance_id']) or labels.get('environment') != context['environment_id'] or labels.get('owner') != digest(context['owner_id'])[:32] or len(addresses) != 1:
                raise GcpError('GCP_RESOURCE_OWNERSHIP_MISMATCH')
            public = addresses[0].get('accessConfigs', [{}])[0].get('natIP')
            if addresses[0].get('networkIP') != node['addresses']['private'] or public != node['addresses']['public'] or not ipaddress.ip_address(public).is_global:
                raise GcpError('GCP_NATIVE_ADDRESS_MISMATCH')
            if role == 'database' and addresses[0]['networkIP'] != self._database_ip(): raise GcpError('GCP_DATABASE_ADDRESS_MISMATCH')
            resources.append({'kind': 'compute', 'role': role, 'id': expected, 'state': row['status'].lower(), 'private_ip': addresses[0]['networkIP'], 'public_ip': public})
        return resources

    def _runtime(self, context, session, network, nodes):
        home = self._home(context); members = []
        for role, node in nodes.items():
            hostkeys = home / (role + '.known_hosts')
            if not hostkeys.exists():
                output = self.runner([sys.executable, str(ROOT / 'infrastructure/providers/terraform_tools/access.py'), '--descriptor', str(home / (role + '.json')), '--output', str(hostkeys)], env=session.env(), timeout=650, mutation=True)
                if json.loads(output).get('status') != 'succeeded': raise GcpError('GCP_HOST_KEY_ENROLLMENT_FAILED', unknown=True)
            if role != 'database': members.append({'id': node['target_id'], 'role': 'control_plane' if role == 'control' else 'workers', 'availability_zone': node['location']['zone'],
                'descriptor_file': str(home / (role + '.json')), 'ssh': {'user': 'railshot-operator', 'port': 22, 'identity_file': self.config['ssh_identity_file'], 'known_hosts_file': str(hostkeys)}})
        save(home / 'authority.json', {'config': {k: self.config[k] for k in ('auth', 'project_id', 'project_number')}, 'context': self._binding(context),
            'state_dir': str(home / 'auth'), 'credentials_file': self.config['auth']['credentials_file']})
        edgevars = private_json(self.config['edge_variables_file']); edgevars.update(project_id=self.config['project_id'], name=self._name(context))
        save(home / 'edge-vars.json', edgevars)
        edge = {'version': 1, 'cluster_id': context['environment_id'], 'provider': 'gcp', 'account_id': self.config['project_id'], 'region': 'asia-northeast3',
            'variables_file': str(home / 'edge-vars.json'), 'credentials_file': self.config['auth']['credentials_file'], 'gcp_authority_file': str(home / 'authority.json'),
            'dns_config_file': self.config['dns_config_file'], 'worker_bindings': {nodes[r]['resource_id']: {} for r in ('compute-a', 'compute-b')}}
        save(home / 'edge.json', edge)
        storage = {'backend': 'filestore', 'mode': 'create', 'binding': {k: edge[k] for k in ('cluster_id', 'provider', 'account_id', 'region')},
            'state_dir': str(directory(home / 'storage')), 'credentials_file': self.config['auth']['credentials_file'],
            'gcp': {'network': network['resource_id'], 'instance_name': self._name(context), 'share_name': 'railshot', 'capacity_gib': self.config.get('filestore_capacity_gib', 1024), 'reserved_ip_range': self.config.get('filestore_cidr', '10.77.0.0/26')}}
        registry = {'version': 1, 'cluster_id': context['environment_id'], 'provider': 'gcp', 'account_id': self.config['project_id'], 'region': 'asia-northeast3', 'members': members, 'runtime': {'storage': storage, 'edge': str(home / 'edge.json')}}
        save(home / 'cluster.json', registry)
        result = json.loads(self.runner([sys.executable, str(ROOT / 'infrastructure/ansible/run.py'), '--cluster-file', str(home / 'cluster.json'), '--request-id', context['environment_id'] + '.runtime', '--state-dir', str(home / 'runtime')], env=session.env(), timeout=1900, mutation=True))
        save(home / 'runtime-result.json', result)
        if result.get('status') != 'succeeded': raise GcpError('GCP_RUNTIME_NOT_READY', unknown=bool(result.get('error', {}).get('outcome_unknown')))
        return result

    def _database(self, operation, context, session):
        home = self._home(context); config = private_json(self.config['database_config_file'])
        config['ssh'] = {'user': 'railshot-operator', 'identity_file': self.config['ssh_identity_file'], 'known_hosts_file': str(home / 'database.known_hosts'), 'port': 22}
        save(home / 'database-config.json', config)
        save(home / 'database-context.json', {**context, 'operation_id': (context.get('operation_id') or 'observe') + '.database'})
        save(home / 'database-request.json', {'action': operation, 'descriptor_file': str(home / 'database.json'), 'config_file': str(home / 'database-config.json'), 'context_file': str(home / 'database-context.json')})
        result = json.loads(self.runner([sys.executable, str(ROOT / 'infrastructure/providers/gcp/database.py'), str(home / 'database-request.json')], env=session.env(), timeout=1850, mutation=operation['action'] not in ('status', 'verify')))
        save(home / 'database-result.json', result)
        if result.get('status') != 'succeeded' or result.get('ready') is not True: raise GcpError('GCP_DATABASE_UNVERIFIED', unknown=result.get('status') == 'unknown')
        return result

    def _application_binding(self, context, nodes):
        home = self._home(context); eid = context['environment_id']; template = private_json(self.config['application_template_file'])
        profile = template['profile']; profile['ingress'].update(gcp_authority_file=str(home / 'authority.json'), cluster_file=str(home / 'cluster.json'),
            cluster_edge_config_file=str(home / 'edge.json'), cluster_edge_state_dir=str(home / 'runtime/cluster-edge' / eid), dns_config_file=self.config['dns_config_file'])
        if (home / 'database-result.json').exists(): profile['ingress']['standalone_database'] = private_json(home / 'database-result.json')['application_bundle_file']
        target = {'profile': 'kubeadm-1c2w', 'purpose': 'runtime', 'cluster_file': str(home / 'cluster.json'),
            'management_endpoint': 'https://' + nodes['control']['addresses']['public'] + ':6443',
            'metrics_endpoints': {n['target_id']: n['addresses']['public'] for role, n in nodes.items() if role != 'database'}}
        save(home / 'targets.json', {'version': 1, 'targets': {eid: target}})
        save(home / 'application-config.json', {'version': 1, 'state_dir': str(directory(home / 'applications')), 'registry_file': str(home / 'targets.json'), 'cd': template['cd'], 'environments': {eid: profile}})

    def _registration(self, action, context, session):
        home = self._home(context); save(home / 'registration-context.json', context)
        result = json.loads(self.runner([sys.executable, str(ROOT / 'infrastructure/providers/gcp/runtime.py'), action, str(home / 'application-config.json'), str(home / 'registration-context.json')], env=session.env(), timeout=1900, mutation=True))
        if result.get('status') != 'succeeded': raise GcpError('GCP_APPLICATION_REGISTRATION_UNVERIFIED', unknown=True)
        save(home / ('application-binding-result.json' if action == 'bootstrap' else 'application-teardown-result.json'), result)
        return result

    def apply(self, spec, context):
        self._spec(spec, context); budget(self.config, spec, context); self.connection(context)
        home = self._home(context); session = self._session(context); save(home / 'spec.json', spec)
        network = self._network(context, session); save(home / 'network.json', network)
        nodes = self._nodes(spec, context, session, network); resources = self._observe(context, session, nodes)
        if any(r['state'] != 'running' for r in resources): return {'status': 'blocked', 'readiness': {}, 'resources': resources, 'blockers': ['GCP_INSTANCES_NOT_RUNNING']}
        runtime = self._runtime(context, session, network, nodes)
        if spec['database']['mode'] == 'standalone': self._database({'action': 'install'}, context, session)
        self._application_binding(context, nodes); self._registration('bootstrap', context, session)
        return self._result(context, resources, runtime)

    def _result(self, context, resources, runtime):
        home = self._home(context)
        readiness = {'infrastructure': bool(resources) and all(r['state'] == 'running' for r in resources), 'cluster': runtime.get('runtime_ready') is True,
            'storage': runtime.get('storage_ready') is True, 'public_access': runtime.get('edge_ready') is True, 'application_http': runtime.get('public_http_verified') is True,
            'deployment_binding': False, 'database': False}
        uncertain = any(private_json(p).get('status') == 'unknown' for p in home.glob('**/journal.json') if (p.parent / 'binding.json').exists())
        result = {'status': 'succeeded', 'readiness': readiness, 'resources': resources, 'ownership_verified': True, 'mutation_in_progress': uncertain,
            'cluster_file': str(home / 'cluster.json'), 'runtime_receipt_path': str(home / 'runtime-result.json')}
        if (home / 'application-config.json').exists(): result['application_config_path'] = str(home / 'application-config.json')
        if (home / 'application-binding-result.json').exists(): result['application_binding_receipt_path'] = private_json(home / 'application-binding-result.json')['application_binding_receipt_path']
        if runtime and (home / 'database-result.json').exists(): readiness['database'] = private_json(home / 'database-result.json').get('ready') is True
        return result

    def describe(self, context):
        home = self._home(context); session = self._session(context)
        if (home / 'deleted.json').exists(): return private_json(home / 'deleted.json')
        if not (home / 'nodes.json').exists(): return {'status': 'blocked', 'readiness': {}, 'resources': [], 'blockers': ['GCP_NOT_PROVISIONED']}
        resources = self._observe(context, session, private_json(home / 'nodes.json')); result = self._result(context, resources, {})
        if not result['readiness']['infrastructure']:
            result.update(status='blocked', blockers=['GCP_INSTANCES_NOT_RUNNING'])
            if all(r['state'] == 'terminated' for r in resources): result.update(status='succeeded', state='stopped', stopped_verified=True)
            return result
        if not (home / 'cluster.json').exists(): return {**result, 'status': 'blocked', 'blockers': ['GCP_RUNTIME_NOT_PROVISIONED']}
        sys.path[:0] = [str(ROOT / 'infrastructure/ansible'), str(ROOT / 'deployment/scripts')]
        kubeadm, storage, edge = (importlib.import_module(n) for n in ('kubeadm', 'storage_profile', 'cluster_edge'))
        request = kubeadm.from_registry(private_json(home / 'cluster.json'), request_id=context['environment_id'] + '.observe')
        backend = private_json(home / 'storage/observed-backend.json')
        storage._observe(request, lambda argv: json.loads(self.runner(argv, env=session.env())), backend['resource_id'].rsplit('/', 1)[1])
        argv = [sys.executable, str(ROOT / 'infrastructure/providers/gcp/probe.py'), str(home / 'cluster.json')]
        prior = home / 'current-observation.json'
        if prior.exists(): argv.append(private_json(prior)['cluster_uid'])
        observed = json.loads(self.runner(argv, env=session.env(), timeout=210)); save(prior, observed)
        result['readiness'].update(cluster=observed['cluster'], storage=observed['storage'])
        edge_home = home / 'runtime/cluster-edge' / context['environment_id']
        if (edge_home / 'journal.json').exists() and private_json(edge_home / 'journal.json').get('phase') == 'configured':
            proof = edge.provision(request, state_dir=home / 'runtime/cluster-edge', deadline=time.monotonic() + 180, env=session.env())
            result['readiness']['public_access'] = proof.get('edge_ready') is True
        if private_json(home / 'spec.json')['database']['mode'] == 'standalone':
            database = self._database({'action': 'status'}, context, session); result['readiness']['database'] = database.get('ready') is True
            result['resources'] += [{'kind': 'database_backup', 'id': label, 'status': 'verified'} for label in database.get('backups', [])]
        if not all(result['readiness'][k] for k in ('infrastructure', 'cluster', 'storage', 'public_access')): result['status'] = 'blocked'
        return result

    def operate(self, operation, context):
        if not isinstance(operation, dict) or not isinstance(operation.get('action'), str): raise GcpError('GCP_OPERATION_INVALID')
        action = operation['action']; home = self._home(context); session = self._session(context)
        if action in ('verify', 'reconcile'): return self.describe(context)
        nodes = private_json(home / 'nodes.json'); self._observe(context, session, nodes)
        if action in ('backup', 'restore'):
            if private_json(home / 'spec.json')['database']['mode'] != 'standalone': raise GcpError('GCP_DATABASE_NOT_CONFIGURED')
            self._database(operation, context, session); return self.describe(context)
        if action not in ('start', 'stop'): raise GcpError('GCP_OPERATION_UNSUPPORTED')
        marker = home / ('operation-' + digest(context['operation_id']) + '.json')
        identity = {'operation_id': context['operation_id'], 'action': action, 'nodes': [n['resource_id'] for n in nodes.values()]}
        if marker.exists():
            old = private_json(marker)
            if old['identity'] != identity: raise GcpError('GCP_OPERATION_CONFLICT')
            if old['status'] == 'succeeded': return old['result']
            result = self.describe(context)
            expected = 'terminated' if action == 'stop' else 'running'
            if all(r['state'] == expected for r in result['resources'] if r['kind'] == 'compute'): save(marker, {'identity': identity, 'status': 'succeeded', 'result': result})
            return result
        save(marker, {'identity': identity, 'status': 'unknown'})
        for node in nodes.values(): session.call('compute', 'instances', action, node['target_id'], '--zone=' + node['location']['zone'], mutation=True)
        result = self.describe(context); save(marker, {'identity': identity, 'status': 'succeeded', 'result': result}); return result

    def delete(self, context):
        home = self._home(context); session = self._session(context); binding = self._binding(context)
        if (home / 'deleted.json').exists(): return private_json(home / 'deleted.json')
        nodes = private_json(home / 'nodes.json'); marker = home / 'deleting.json'
        if not marker.exists(): self._observe(context, session, nodes); save(marker, {'binding': binding})
        elif private_json(marker)['binding'] != binding: raise GcpError('GCP_DELETE_BINDING_CHANGED')
        if (home / 'application-config.json').exists(): self._registration('teardown', context, session)
        elif (home / 'runtime/cluster-edge' / context['environment_id'] / 'terraform.tfstate').exists():
            # Incomplete bootstrap has no verified common teardown authority.
            raise GcpError('GCP_PARTIAL_EDGE_CLEANUP_RECONCILIATION_REQUIRED', unknown=True)
        retained = []
        for role, node in nodes.items():
            owned(home / 'nodes' / role, session, binding, runner=self.runner).destroy(retain={'google_compute_disk.data': node['data_disk']['resource_id']})
            retained.append({'kind': 'disk', 'id': node['data_disk']['resource_id'], 'status': 'retained'})
        network = private_json(home / 'network.json')
        if (home / 'storage/observed-backend.json').exists():
            backend = private_json(home / 'storage/observed-backend.json')
            if backend['account_id'] != self.config['project_id'] or backend['cluster_id'] != context['environment_id']: raise GcpError('GCP_FILESTORE_OWNERSHIP_UNPROVEN')
            retained += [{'kind': 'filestore', 'id': backend['resource_id'], 'status': 'retained'}, {'kind': 'network', 'id': network['resource_id'], 'status': 'retained'}, {'kind': 'subnet', 'id': network['subnet_id'], 'status': 'retained'}]
            # Filestore DIRECT_PEERING requires its VPC/subnet. Preserve the owned
            # dependency set explicitly. Remove only rules targeting deleted VMs.
            owned(home / 'network', session, binding, runner=self.runner).destroy(retain={
                'google_compute_network.environment': network['resource_id'], 'google_compute_subnetwork.environment': network['subnet_id']})
        else: owned(home / 'network', session, binding, runner=self.runner).destroy()
        result = {'status': 'succeeded', 'deleted': True, 'residuals': [], 'retained': retained, 'retained_resources': retained, 'resources': [], 'readiness': {k: False for k in ('infrastructure', 'cluster', 'storage', 'public_access', 'database', 'deployment_binding')}}
        save(home / 'deleted.json', result); return result
