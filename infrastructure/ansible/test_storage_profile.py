"""Offline contracts, backend admission/unknown outcomes, and actual-proof gates."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

import storage_profile as storage

SPEC = importlib.util.spec_from_file_location('shared_storage_install', Path(__file__).resolve().parents[2] / 'deployment/shared-storage/install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def request(provider='aws'):
    region = {'aws': 'ap-northeast-2', 'gcp': 'asia-northeast3', 'openstack': 'RegionOne'}[provider]
    account = '123456789012' if provider == 'aws' else 'railshot-project'
    workers = [{'id': 'compute-a', 'resource_id': 'i-0123456789abcdef0', 'private_ipv4': '10.0.1.10', 'availability_zone': region + 'a'},
               {'id': 'compute-b', 'resource_id': 'i-0123456789abcdef1', 'private_ipv4': '10.0.2.10', 'availability_zone': region + 'c'}]
    if provider == 'gcp':
        for node, zone in zip(workers, ('a', 'b')):
            node['availability_zone'] = region + '-' + zone
            node['resource_id'] = f'projects/{account}/zones/{node["availability_zone"]}/instances/{node["id"]}'
    cfg = {'backend': storage.BACKENDS[provider], 'mode': 'reuse',
           'binding': {'cluster_id': 'demo', 'provider': provider, 'region': region, 'account_id': account},
           'state_dir': '/tmp/operator-storage', 'credentials_file': '/tmp/operator-credentials'}
    if provider == 'aws':
        cfg['aws'] = {'vpc_id': 'vpc-0123456789abcdef0', 'worker_subnet_ids': {'compute-a': 'subnet-0123456789abcdef0', 'compute-b': 'subnet-0123456789abcdef1'},
                      'filesystem_id': 'fs-0123456789abcdef0', 'controller_credentials_secret': 'efs-controller'}
    elif provider == 'gcp':
        cfg['gcp'] = {'network': f'projects/{account}/global/networks/private', 'instance_name': 'demo-files', 'share_name': 'apps', 'capacity_gib': 1024}
    else:
        cfg.pop('credentials_file'); cfg.pop('state_dir')
        cfg['cephfs'] = {'cluster_id': 'd4df80c6-41a8-4f4a-bac1-20d1eb32d502', 'monitors': ['10.1.0.1:3300'],
                         'fs_name': 'apps', 'subvolume_group': 'railshot-demo', 'provisioner_secret': 'ceph-provisioner', 'node_secret': 'ceph-node'}
    return {'target': {'id': 'demo', 'provider': provider, 'placement': region, 'account_id': account}, 'inventory': {'workers': workers}, 'runtime': {'storage': cfg}}


class Cloud:
    def __init__(self, req):
        self.req = req
        self.argv = []
        self.open_ingress = False
        self.other_owner = False
        self.existing_token = False
        self.fail_apply = False
        self.filestore_protocol = 'NFS_V3'
        self.destructive = False
        self.region = req['target']['placement']
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        req, cfg = self.req, self.req['runtime']['storage']
        self.argv += argv
        if argv[0] == 'terraform':
            action = argv[2]
            if action == 'show':
                return json.dumps({'resource_changes': [{'change': {'actions': ['delete', 'create'] if self.destructive else ['create']}}]})
            if action == 'apply' and self.fail_apply:
                raise subprocess.TimeoutExpired(argv, 10)
            if action == 'output':
                return json.dumps({'backend_id': {'value': 'fs-0123456789abcdef0'}})
            return ''
        if argv[0] == 'gcloud':
            if argv[1] == 'filestore':
                return json.dumps({'name': f'projects/railshot-project/locations/{self.region}/instances/demo-files', 'tier': 'REGIONAL', 'state': 'READY', 'protocol': self.filestore_protocol,
                                   'labels': {'managed-by': 'railshot', 'railshot-cluster': 'other' if self.other_owner else 'demo'},
                                   'networks': [{'network': cfg['gcp']['network'], 'ipAddresses': ['10.0.8.2']}],
                                   'fileShares': [{'name': 'apps', 'capacityGb': 1024,
                                                   'nfsExportOptions': [{'ipRanges': [n['private_ipv4'] + '/32' for n in req['inventory']['workers']],
                                                                         'accessMode': 'READ_WRITE', 'squashMode': 'NO_ROOT_SQUASH'}]}]})
            name = argv[4]
            node = next(n for n in req['inventory']['workers'] if n['id'] == name)
            return json.dumps({'status': 'RUNNING', 'networkInterfaces': [{'network': cfg['gcp']['network'], 'networkIP': node['private_ipv4']}]})
        if 'get-caller-identity' in argv:
            return json.dumps({'Account': req['target']['account_id']})
        if 'describe-file-systems' in argv and '--creation-token' in argv:
            return json.dumps({'FileSystems': [{'FileSystemId': 'fs-unbound'}] if self.existing_token else []})
        if 'describe-file-systems' in argv:
            return json.dumps({'FileSystems': [{'FileSystemId': 'fs-0123456789abcdef0', 'OwnerId': req['target']['account_id'], 'Encrypted': True,
                                                'LifeCycleState': 'available', 'Tags': [{'Key': 'managed-by', 'Value': 'railshot'},
                                                                                      {'Key': 'railshot-cluster', 'Value': 'other' if self.other_owner else 'demo'}]}]})
        if 'describe-instances' in argv:
            return json.dumps({'Reservations': [{'Instances': [{'InstanceId': n['resource_id'], 'State': {'Name': 'running'}, 'PrivateIpAddress': n['private_ipv4'],
                                                                 'Placement': {'AvailabilityZone': n['availability_zone']}, 'VpcId': cfg['aws']['vpc_id'],
                                                                 'SubnetId': cfg['aws']['worker_subnet_ids'][n['id']], 'SecurityGroups': []}
                                                                for n in req['inventory']['workers']]}]})
        if 'describe-mount-targets' in argv:
            return json.dumps({'MountTargets': [{'MountTargetId': 'fsmt-' + n['id'], 'AvailabilityZoneName': n['availability_zone'], 'LifeCycleState': 'available', 'VpcId': cfg['aws']['vpc_id']}
                                               for n in req['inventory']['workers']]})
        if 'describe-mount-target-security-groups' in argv:
            return json.dumps({'SecurityGroups': ['sg-0123456789abcdef0']})
        if 'describe-security-groups' in argv:
            return json.dumps({'SecurityGroups': [{'VpcId': cfg['aws']['vpc_id'], 'IpPermissions': [{'IpProtocol': 'tcp', 'FromPort': 2049, 'ToPort': 2049,
                                                                                                 'IpRanges': [{'CidrIp': '0.0.0.0/0'}] if self.open_ingress else [{'CidrIp': n['private_ipv4'] + '/32'} for n in req['inventory']['workers']]}]}]})
        raise AssertionError(argv)


class ProfileTests(unittest.TestCase):
    def test_aws_instance_profile_is_control_only_and_has_no_static_secret(self):
        req = request()
        req['inventory']['control_plane'] = [{'id': 'control'}]
        aws = req['runtime']['storage']['aws']
        aws.pop('controller_credentials_secret')
        aws.update(identity_mode='instance_profile', controller_node='control')
        cfg = storage.variables(req)['railshot_storage']
        cfg['backend_descriptor'] = {**cfg['binding'], 'filesystem_id': aws['filesystem_id']}
        values, _ = installer.render(cfg)
        control = values['controller']
        self.assertTrue(control['hostNetwork'])
        self.assertEqual(control['nodeSelector'], {'kubernetes.io/hostname': 'control'})
        self.assertEqual(control['tags'], {'railshot-cluster': 'demo'})
        self.assertFalse(any('secretKeyRef' in str(row) for row in control['env']))
        self.assertEqual(control['replicaCount'], 1)
        class NeverSecrets:
            def get(self, *args): raise AssertionError('Static secret must not be read')
        installer.ensure_secrets(NeverSecrets(), cfg)
        aws['controller_node'] = 'compute-a'
        with self.assertRaises(storage.StorageError): storage.validate(req)

    def test_provider_contracts_and_private_variable_mapping(self):
        for provider in storage.BACKENDS:
            req = request(provider)
            self.assertIs(storage.validate(req), req)
            variables = storage.variables(req)['railshot_storage']
            self.assertEqual(variables['workers'], ['compute-a', 'compute-b'])
            self.assertNotIn('credentials_file', variables)
            self.assertNotIn('state_dir', variables)

    def test_binding_scope_region_and_backend_mismatch(self):
        for field in ('cluster_id', 'provider', 'region', 'account_id'):
            req = request()
            req['runtime']['storage']['binding'][field] = 'other'
            with self.assertRaises(storage.StorageError):
                storage.validate(req)
        req = request(); req['inventory']['workers'][1]['availability_zone'] = 'us-east-1a'
        with self.assertRaises(storage.StorageError): storage.validate(req)
        req = request('gcp'); req['inventory']['workers'][1]['resource_id'] = 'projects/other/zones/asia-northeast3-b/instances/compute-b'
        with self.assertRaises(storage.StorageError): storage.validate(req)

    def test_wrong_backend_unknown_fields_and_openstack_create_rejected(self):
        req = request(); req['runtime']['storage']['backend'] = 'ebs'
        with self.assertRaises(storage.StorageError): storage.validate(req)
        req = request(); req['runtime']['storage']['extra_secret_value'] = 'private'
        with self.assertRaises(storage.StorageError): storage.validate(req)
        req = request('openstack'); req['runtime']['storage']['mode'] = 'create'
        with self.assertRaises(storage.StorageError): storage.validate(req)

    def test_regional_range_and_capacity_validation(self):
        req = request('gcp'); cfg = req['runtime']['storage']; cfg['mode'] = 'create'
        cfg['gcp']['reserved_ip_range'] = '10.0.8.0/26'
        storage.validate(req)
        for value in ('10.0.8.0/24', '0.0.0.0/26', '10.0.1.0/26'):
            cfg['gcp']['reserved_ip_range'] = value
            with self.assertRaises(storage.StorageError): storage.validate(req)
        cfg['gcp']['reserved_ip_range'] = '10.0.8.0/26'; cfg['gcp']['capacity_gib'] = 100
        with self.assertRaises(storage.StorageError): storage.validate(req)

    def test_ceph_refs_only_no_inline_keys_or_public_mons(self):
        req = request('openstack'); cfg = req['runtime']['storage']['cephfs']
        cfg['userKey'] = 'secret'
        with self.assertRaises(storage.StorageError): storage.validate(req)
        del cfg['userKey']; cfg['monitors'] = ['8.8.8.8:3300']
        with self.assertRaises(storage.StorageError): storage.validate(req)
        cfg['monitors'] = ['10.1.0.1:3300']; cfg['secret_files'] = {'unknown': '/root/key'}
        with self.assertRaises(storage.StorageError): storage.validate(req)

    def run_prepare(self, req, directory, cloud):
        cfg = req['runtime']['storage']
        cfg['state_dir'] = str(Path(directory) / 'state')
        cred = Path(directory) / 'credentials.json'
        if req['target']['provider'] == 'aws':
            cred.write_text(json.dumps({'AWS_ACCESS_KEY_ID': 'access-private', 'AWS_SECRET_ACCESS_KEY': 'secret-private'}))
        else:
            cred.write_text(json.dumps({'type': 'service_account', 'project_id': 'railshot-project'}))
        cred.chmod(0o600); cfg['credentials_file'] = str(cred)
        return storage.prepare(req, work=directory, env={'PATH': '/usr/bin', 'UNSAFE_CREDENTIAL': 'drop'}, deadline=time.monotonic() + 300, runner=cloud)

    def test_observed_reuse_and_secret_environment_not_argv(self):
        for provider in ('aws', 'gcp'):
            with tempfile.TemporaryDirectory() as directory:
                req = request(provider); cloud = Cloud(req)
                result = self.run_prepare(req, directory, cloud)
                self.assertTrue(result['storage_backend_ready'])
                self.assertFalse(result['storage_pvc_ready'])
                self.assertNotIn('secret-private', ' '.join(cloud.argv))
                self.assertNotIn('UNSAFE_CREDENTIAL', cloud.calls[0][1]['env'])
                self.assertFalse(any(argv[0] == 'terraform' for argv, _ in cloud.calls))

    def test_reject_unowned_backend_and_broad_nfs_access(self):
        for invalid in ('other_owner', 'open_ingress'):
            with tempfile.TemporaryDirectory() as directory:
                req = request(); cloud = Cloud(req); setattr(cloud, invalid, True)
                with self.assertRaises(storage.StorageError) as caught: self.run_prepare(req, directory, cloud)
                self.assertFalse(caught.exception.unknown)

    def test_reuse_filestore_protocol_mismatch_stops_before_csi(self):
        with tempfile.TemporaryDirectory() as directory:
            req = request('gcp'); cloud = Cloud(req); cloud.filestore_protocol = 'NFS_V4_1'
            with self.assertRaises(storage.StorageError) as caught:
                self.run_prepare(req, directory, cloud)
            self.assertFalse(caught.exception.unknown)

    def test_create_apply_unknown_is_not_retried_and_destructive_plan_never_applied(self):
        for flag in ('fail_apply', 'destructive'):
            with tempfile.TemporaryDirectory() as directory:
                req = request(); cfg = req['runtime']['storage']; cfg['mode'] = 'create'; del cfg['aws']['filesystem_id']
                cloud = Cloud(req); setattr(cloud, flag, True)
                with self.assertRaises((storage.StorageError, subprocess.TimeoutExpired)) as caught:
                    self.run_prepare(req, directory, cloud)
                self.assertEqual(caught.exception.unknown, flag == 'fail_apply')
                if flag == 'fail_apply':
                    count = len(cloud.calls)
                    with self.assertRaisesRegex(storage.StorageError, 'unresolved'):
                        self.run_prepare(req, directory, cloud)
                    self.assertEqual(count, len(cloud.calls))
                else:
                    self.assertFalse(any(argv[0] == 'terraform' and argv[2] == 'apply' for argv, _ in cloud.calls))

    def test_successful_create_uses_saved_plan_and_isolated_state(self):
        with tempfile.TemporaryDirectory() as directory:
            req = request(); cfg = req['runtime']['storage']; cfg['mode'] = 'create'; del cfg['aws']['filesystem_id']
            cloud = Cloud(req)
            result = self.run_prepare(req, directory, cloud)
            self.assertTrue(result['storage_backend_ready'])
            argv = next(argv for argv, _ in cloud.calls if argv[0] == 'terraform' and argv[2] == 'apply')
            self.assertTrue(argv[-1].endswith('backend.plan'))
            self.assertTrue(any(item.endswith('/state/backend.tfstate') for item in argv))
            self.assertFalse((Path(directory) / 'state/apply-unknown.json').exists())

    def test_unbound_existing_creation_token_stops_without_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            req = request(); cfg = req['runtime']['storage']; cfg['mode'] = 'create'; del cfg['aws']['filesystem_id']
            cloud = Cloud(req); cloud.existing_token = True
            with self.assertRaisesRegex(storage.StorageError, 'without owned state') as caught:
                self.run_prepare(req, directory, cloud)
            self.assertFalse(caught.exception.unknown)
            self.assertFalse(any(argv[0] == 'terraform' for argv, _ in cloud.calls))

    def test_state_symlink_and_public_credentials_fail_before_backend_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            req = request(); cloud = Cloud(req)
            parent = Path(directory); real = parent / 'real'; real.mkdir(mode=0o700)
            (parent / 'state').symlink_to(real, target_is_directory=True)
            with self.assertRaises(storage.StorageError): self.run_prepare(req, directory, cloud)
            self.assertFalse(cloud.calls)
        with tempfile.TemporaryDirectory() as directory:
            req = request(); cloud = Cloud(req)
            self.run_prepare(req, directory, cloud)
            Path(req['runtime']['storage']['credentials_file']).chmod(0o644)
            calls = len(cloud.calls)
            with self.assertRaises(storage.StorageError):
                storage.prepare(req, work=directory, env={'PATH': '/usr/bin'}, deadline=time.monotonic() + 300, runner=cloud)
            self.assertEqual(len(cloud.calls), calls)

    def test_bound_state_rejects_input_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            req = request(); cloud = Cloud(req)
            self.run_prepare(req, directory, cloud)
            req['runtime']['storage']['aws']['controller_credentials_secret'] = 'different'
            with self.assertRaisesRegex(storage.StorageError, 'different inputs'):
                self.run_prepare(req, directory, cloud)

    def test_ceph_configuration_cannot_claim_backend_readiness(self):
        result = storage.prepare(request('openstack'), work='/tmp', env={}, deadline=time.monotonic())
        self.assertFalse(result['storage_backend_ready'])
        self.assertFalse(result['storage_pvc_ready'])


def rendered_config(provider):
    req = request(provider)
    config = storage.variables(req)['railshot_storage']
    descriptor = {**config['binding'], 'backend': config['backend']}
    if provider == 'aws': descriptor['filesystem_id'] = 'fs-0123456789abcdef0'
    if provider == 'gcp': descriptor.update(server='10.0.8.2', share='/apps')
    config['backend_descriptor'] = descriptor
    return config


class Kube:
    def __init__(self):
        self.objects = {}
        self.applied = []
        self.commands = []

    def get(self, kind, name=None, namespace=None):
        if kind == 'storageclasses': return {'items': []}
        if kind == 'deployment,daemonset':
            return {'items': [{'kind': kind, 'metadata': {'name': name, 'annotations': {'meta.helm.sh/release-name': installer.RELEASE}}}
                              for kind, name in [('Deployment', 'controller'), ('DaemonSet', 'node')]]}
        return self.objects.get((kind, name, namespace))

    def apply(self, doc):
        self.applied.append(copy.deepcopy(doc))
        self.objects[(doc['kind'].lower(), doc['metadata']['name'], doc['metadata'].get('namespace'))] = copy.deepcopy(doc)
        return doc

    def run(self, *args): self.commands.append(args)


class ProofKube(Kube):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.operations = []
        self.wrong_uid = False
        self.wrong_nonce = False
        self.failed_pod = False

    def get(self, kind, name=None, namespace=None):
        if kind == 'pods':
            return {'items': [value for (k, _, ns), value in self.objects.items() if k == 'pod' and ns == namespace]}
        return super().get(kind, name, namespace)

    def apply(self, doc):
        self.operations.append(('apply', doc['kind'], doc['metadata']['name']))
        doc = copy.deepcopy(doc)
        if doc['kind'] == 'PersistentVolumeClaim':
            doc['metadata']['uid'] = 'proof-pvc-uid'
            doc['spec']['volumeName'] = 'proof-pv'
            doc['status'] = {'phase': 'Bound'}
            descriptor = self.config['backend_descriptor']
            attrs = {'server': descriptor['server'], 'share': descriptor['share']}
            self.objects[('pv', 'proof-pv', None)] = {
                'metadata': {'name': 'proof-pv', 'uid': 'proof-pv-uid'},
                'spec': {'claimRef': {'namespace': doc['metadata']['namespace'], 'name': doc['metadata']['name'], 'uid': 'proof-pvc-uid'},
                         'storageClassName': installer.CLASS, 'persistentVolumeReclaimPolicy': 'Retain',
                         'csi': {'driver': 'nfs.csi.k8s.io', 'volumeAttributes': attrs, 'volumeHandle': 'server#share#directory'}}}
        if doc['kind'] == 'Pod':
            doc['metadata']['uid'] = 'pod-' + doc['metadata']['name']
            doc['spec']['nodeName'] = doc['spec']['nodeSelector']['kubernetes.io/hostname']
            if self.wrong_nonce: doc['metadata']['annotations']['railshot.io/nonce'] = 'other'
            doc['status'] = {'phase': 'Failed' if self.failed_pod else 'Succeeded', 'containerStatuses': [{'state': {'terminated': {'exitCode': 1 if self.failed_pod else 0}}}]}
        result = super().apply(doc)
        if doc['kind'] == 'PersistentVolumeClaim':
            self.objects[('pvc', doc['metadata']['name'], doc['metadata']['namespace'])] = copy.deepcopy(doc)
        return result

    def run(self, *args):
        super().run(*args)
        if 'delete' in args:
            self.operations.append(('delete', args[3], args[4]))
            self.objects.pop((args[3], args[4], args[1]), None)


class InstallTests(unittest.TestCase):
    def test_class_contract_is_retained_singlewriter_compatible_and_not_default(self):
        for provider in storage.BACKENDS:
            config = rendered_config(provider)
            values, cls = installer.render(config)
            self.assertEqual(cls['metadata']['name'], 'railshot-persistent')
            self.assertEqual(cls['reclaimPolicy'], 'Retain')
            self.assertEqual(cls['metadata']['annotations']['storageclass.kubernetes.io/is-default-class'], 'false')
            self.assertNotIn('secret-private', json.dumps(values))
            self.assertIn('affinity', values['controller'] if provider != 'openstack' else values['provisioner'])
        values, cls = installer.render(rendered_config('gcp'))
        self.assertEqual(cls['provisioner'], 'nfs.csi.k8s.io')
        self.assertEqual(cls['parameters']['onDelete'], 'retain')
        values, cls = installer.render(rendered_config('openstack'))
        self.assertFalse(values['secret']['create'])
        self.assertEqual(cls['parameters']['csi.storage.k8s.io/node-stage-secret-namespace'], installer.NAMESPACE)

    def test_unowned_class_driver_and_immutable_drift_stop(self):
        config = rendered_config('gcp'); _, cls = installer.render(config)
        for change in ('unowned', 'drift', 'driver'):
            kube = Kube(); current = copy.deepcopy(cls)
            if change == 'unowned': current['metadata']['labels'].pop(installer.OWNER)
            if change == 'drift': current['parameters']['server'] = '10.1.2.3'
            if change == 'driver': kube.objects[('csidriver', 'nfs.csi.k8s.io', None)] = {'metadata': {}}
            kube.objects[('storageclass', installer.CLASS, None)] = current
            with self.assertRaises(installer.InstallError): installer.preflight(kube, config, cls)
            self.assertFalse(kube.applied)

    def test_ceph_missing_unowned_or_incomplete_secret_never_installs(self):
        config = rendered_config('openstack')
        for current in (None, {'metadata': {'labels': {}}, 'data': {}}, {'metadata': {'labels': {installer.OWNER: 'demo'}}, 'data': {}}):
            kube = Kube()
            if current: kube.objects[('secret', 'ceph-node', installer.NAMESPACE)] = current
            with self.assertRaises(installer.InstallError): installer.ensure_secrets(kube, config)

    def test_success_requires_driver_then_actual_mobility(self):
        config = rendered_config('gcp'); kube = Kube(); phases = []
        def mobility(*args):
            phases.append('proof'); self.assertTrue(any(d['kind'] == 'StorageClass' for d in kube.applied))
            return {'retained_test_pv': 'pv-test'}
        result = installer.install(config, request_id='request-1', nonce='a' * 32, kube=kube,
                                   downloader=lambda *args: None, helm_runner=lambda *args: phases.append('helm'), mobility=mobility)
        self.assertEqual(phases, ['helm', 'proof'])
        self.assertTrue(result['storage_pvc_ready'])
        self.assertTrue(result['storage_ready'])
        self.assertEqual(sum('rollout' in c for c in kube.commands), 2)

    def test_failed_helm_or_mobility_never_returns_readiness(self):
        for stage in ('helm', 'mobility'):
            def failure(*args): raise installer.InstallError('failure')
            args = {'downloader': lambda *args: None, 'helm_runner': lambda *args: None, 'mobility': lambda *args: {}}
            args['helm_runner' if stage == 'helm' else 'mobility'] = failure
            with self.assertRaises(installer.InstallError):
                installer.install(rendered_config('gcp'), request_id='request-1', nonce='a' * 32, kube=Kube(), **args)

    def test_actual_proof_deletes_writer_before_reader_and_reuses_one_volume(self):
        config = rendered_config('gcp'); kube = ProofKube(config)
        first = installer.prove_mobility(kube, config, 'request-1', 'a' * 32, 300)
        second = installer.prove_mobility(kube, config, 'request-2', 'b' * 32, 300)
        self.assertEqual(first['proof_pv'], second['proof_pv'])
        self.assertEqual(sum(doc['kind'] == 'PersistentVolumeClaim' for doc in kube.applied), 1)
        writes = [i for i, event in enumerate(kube.operations) if event[0] == 'delete' and event[2].endswith('-write')]
        reads = [i for i, event in enumerate(kube.operations) if event[0] == 'apply' and event[1] == 'Pod' and event[2].endswith('-read')]
        self.assertTrue(all(write < read for write, read in zip(writes, reads)))
        self.assertFalse(kube.get('pods', namespace='railshot-storage-proof')['items'])

    def test_proof_unknown_uid_nonce_and_failed_pod_block_readiness(self):
        config = rendered_config('gcp')
        for failure in ('wrong_nonce', 'failed_pod'):
            kube = ProofKube(config); setattr(kube, failure, True)
            with self.assertRaises(installer.InstallError): installer.prove_mobility(kube, config, 'r1', 'a' * 32, 300)
            with self.assertRaisesRegex(installer.InstallError, 'Prior proof Pods'):
                installer.prove_mobility(kube, config, 'r2', 'b' * 32, 300)
        kube = ProofKube(config)
        installer.prove_mobility(kube, config, 'r1', 'a' * 32, 300)
        kube.objects[('pv', 'proof-pv', None)]['metadata']['uid'] = 'other-pv-uid'
        with self.assertRaisesRegex(installer.InstallError, 'identity changed'):
            installer.prove_mobility(kube, config, 'r2', 'b' * 32, 300)

    def test_proof_same_pvc_two_workers_and_safe_nonce(self):
        ns, pvc, pods = installer._proof_objects(rendered_config('gcp'), 'request-1', 'a' * 32)
        self.assertEqual(pods[0]['spec']['volumes'], pods[1]['spec']['volumes'])
        self.assertEqual(pvc['spec']['accessModes'], ['ReadWriteOnce'])
        self.assertEqual([p['spec']['nodeSelector']['kubernetes.io/hostname'] for p in pods], ['compute-a', 'compute-b'])
        with self.assertRaises(installer.InstallError):
            installer.install(rendered_config('gcp'), request_id='request-1', nonce='injection;command', kube=Kube())


if __name__ == '__main__':
    unittest.main()
