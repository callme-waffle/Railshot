"""CNI refusal/profile regressions without root, downloads or a Kubernetes cluster."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('cilium_preflight', ROOT / 'deployment/cilium/preflight.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
CONTROL = (ROOT / 'infrastructure/ansible/control.sh').read_text()
CONFIG = CONTROL.split('<<\'YAML\'\n', 1)[1].split('\nYAML', 1)[0] + '\n'


def server_node():
    return {'metadata': {'name': 'platform-01', 'labels': {'node-role.kubernetes.io/control-plane': 'true'}},
            'spec': {'podCIDR': '10.52.0.0/24'}}


def build_node():
    return {'metadata': {'name': 'build-01', 'labels': {'railshot.io/node-role': 'build'}},
            'spec': {'podCIDR': '10.52.1.0/24', 'podCIDRs': ['10.52.1.0/24'],
                     'taints': [{'key': 'railshot.io/dedicated', 'value': 'build', 'effect': 'NoSchedule'}]}}


class ControlCiliumTests(unittest.TestCase):
    def host(self, root, config=CONFIG, role='control'):
        for name, content in [('etc/rancher/k3s/config.yaml', config), ('etc/railshot/node-role', role)]:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def test_profiles_and_foreign_cni_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.host(root)
            podman = root / 'etc/cni/net.d/87-podman-bridge.conflist'
            podman.parent.mkdir(parents=True)
            podman.write_text('{"name":"podman", "plugins":[{"type":"bridge"}]}')
            with self.assertRaisesRegex(ValueError, 'non-Cilium'):
                guard.check_host('control', root)
            podman.unlink()
            self.assertEqual(guard.check_host('control', root), ('10.52.0.0/16', '10.53.0.0/16'))
            with self.assertRaisesRegex(ValueError, 'role differs'):
                guard.check_host('customer', root)
            for bad in (CONFIG.replace('flannel-backend: none\n', ''),
                        CONFIG.replace('10.52.', '10.42.'), CONFIG + 'cluster-cidr: 10.42.0.0/16\n',
                        CONFIG + 'disable-kube-proxy: true\n', CONFIG + '"cluster-cidr": 10.42.0.0/16\n',
                        CONFIG + '<<: *alternate\n'):
                self.host(root, bad)
                with self.assertRaises(ValueError):
                    guard.check_host('control', root)
            self.host(root)
            cni = root / 'var/lib/rancher/k3s/agent/etc/cni/net.d/10-cni.conflist'
            cni.parent.mkdir(parents=True)
            cni.write_text(json.dumps({'name': 'cbr0', 'plugins': [{'type': 'flannel'}]}))
            with self.assertRaisesRegex(ValueError, 'non-Cilium'):
                guard.check_host('control', root)
            cni.write_text(json.dumps({'name': 'cilium', 'plugins': [{'type': 'cilium-cni'}]}))
            guard.check_host('control', root)
            link = root / 'sys/class/net/flannel.1'
            link.mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, 'flannel.1'):
                guard.check_host('control', root)
            link.rmdir()
            self.host(root, CONFIG.replace('10.52.', '10.42.').replace('10.53.', '10.43.'), 'customer')
            self.assertEqual(guard.check_host('customer', root), ('10.42.0.0/16', '10.43.0.0/16'))

    def test_cluster_profile_and_api_errors_fail_closed(self):
        nodes = {'items': [server_node()]}
        service = {'spec': {'clusterIP': '10.53.0.1'}}
        config = {'metadata': {'annotations': {'meta.helm.sh/release-name': 'cilium'}}, 'data': {
            'cluster-pool-ipv4-cidr': '10.52.0.0/16', 'cluster-pool-ipv4-mask-size': '24',
            'ipam': 'cluster-pool', 'kube-proxy-replacement': 'false',
            'routing-mode': 'tunnel', 'tunnel-protocol': 'vxlan', 'enable-hubble': 'false'}}
        pinned = json.loads((ROOT / 'deployment/airgap/versions.json').read_text())['cilium_images']['agent']
        daemon = {'spec': {'template': {'spec': {'containers': [{'name': 'cilium-agent', 'image': pinned}]}}}}
        for existing in (False, True):
            with patch.object(guard, 'kube', side_effect=[nodes, service, config if existing else None, daemon if existing else None]):
                guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', profile='control')
        daemon['spec']['template']['spec']['containers'][0]['image'] = pinned.replace('@', ':v1.20.2@')
        with patch.object(guard, 'kube', side_effect=[nodes, service, config, daemon]):
            guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', profile='control')
        daemon['spec']['template']['spec']['containers'][0]['image'] = pinned.replace('@', ':v1.20.2@').replace('quay.io/cilium/cilium', 'quay.io/cilium/other')
        with patch.object(guard, 'kube', side_effect=[nodes, service, config, daemon]):
            with self.assertRaisesRegex(ValueError, 'explicit upgrade'):
                guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', profile='control')
        daemon['spec']['template']['spec']['containers'][0]['image'] = pinned
        with patch.object(guard, 'kube', side_effect=[{'items': []}]):
            with self.assertRaisesRegex(ValueError, 'registration/PodCIDR'):
                guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', wait_seconds=0, profile='control')
        with patch.object(guard, 'kube', side_effect=[{'items': nodes['items'] * 2}]):
            with self.assertRaisesRegex(ValueError, 'one server'):
                guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', profile='control')
        with patch.object(guard, 'kube', side_effect=[{'items': []}, {'items': [{**server_node(), 'spec': {}}]}, nodes, service, None, None]), patch.object(guard.time, 'sleep') as sleep:
            guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', profile='control')
            self.assertEqual(sleep.call_count, 2)
        with patch.object(guard, 'kube', side_effect=[nodes, service]):
            with self.assertRaisesRegex(ValueError, 'Service CIDR'):
                guard.check_cluster('10.52.0.0/16', '10.43.0.0/16', profile='control')
        config['data']['cluster-pool-ipv4-cidr'] = '10.42.0.0/16'
        with patch.object(guard, 'kube', side_effect=[nodes, service, config]):
            with self.assertRaisesRegex(ValueError, 'implicit migration'):
                guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', profile='control')
        config['data']['cluster-pool-ipv4-cidr'] = '10.52.0.0/16'
        daemon['spec']['template']['spec']['containers'][0]['image'] = 'quay.io/cilium/cilium:other'
        with patch.object(guard, 'kube', side_effect=[nodes, service, config, daemon]):
            with self.assertRaisesRegex(ValueError, 'explicit upgrade'):
                guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', profile='control')
        with patch.object(guard, 'kube', side_effect=subprocess.CalledProcessError(1, ['kubectl'])):
            with self.assertRaises(subprocess.CalledProcessError):
                guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', profile='control')

    def test_control_accepts_only_server_and_approved_build_agent(self):
        server, worker = server_node(), build_node()
        service = {'spec': {'clusterIP': '10.53.0.1'}}
        for nodes in ([server], [worker, server], [server, worker]):
            with self.subTest(nodes=nodes), patch.object(guard, 'kube', side_effect=[{'items': nodes}, service, None, None]):
                self.assertEqual(guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', profile='control'), server)
        invalid = [[worker], [server, worker, copy.deepcopy(worker)]]
        for labels, taints in (({}, worker['spec']['taints']), ({'railshot.io/node-role': 'platform'}, worker['spec']['taints']),
                               (worker['metadata']['labels'], []),
                               (worker['metadata']['labels'], [{'key': 'railshot.io/dedicated', 'value': 'build', 'effect': 'PreferNoSchedule'}])):
            wrong = copy.deepcopy(worker)
            wrong['metadata']['labels'], wrong['spec']['taints'] = labels, taints
            invalid.append([server, wrong])
        for role in ('control-plane', 'master', 'etcd'):
            wrong = copy.deepcopy(worker)
            wrong['metadata']['labels']['node-role.kubernetes.io/' + role] = 'true'
            invalid.append([server, wrong])
        for field in ('label', 'taint'):
            wrong = copy.deepcopy(server)
            if field == 'label':
                wrong['metadata']['labels']['railshot.io/node-role'] = 'build'
            else:
                wrong['spec']['taints'] = worker['spec']['taints']
            invalid.append([wrong])
        for nodes in invalid:
            with self.subTest(nodes=nodes), patch.object(guard, 'kube', return_value={'items': nodes}):
                with self.assertRaises(ValueError):
                    guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', wait_seconds=0, profile='control')

    def test_control_waits_for_server_role_patch_but_never_accepts_missing_identity(self):
        server = server_node()
        pending = copy.deepcopy(server)
        pending['metadata']['labels'] = {}
        with patch.object(guard, 'kube', side_effect=[{'items': [pending]}, {'items': [server]},
                {'spec': {'clusterIP': '10.53.0.1'}}, None, None]), patch.object(guard.time, 'sleep') as sleep:
            self.assertEqual(guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', profile='control'), server)
            sleep.assert_called_once_with(3)
        with patch.object(guard, 'kube', return_value={'items': [pending]}) as kube, \
                patch.object(guard.time, 'sleep') as sleep:
            with self.assertRaisesRegex(ValueError, 'confirmed server role'):
                guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', wait_seconds=0, profile='control')
            kube.assert_called_once_with('get', 'nodes', '-o', 'json')
            sleep.assert_not_called()
        for nodes in ([server, server], [pending, pending, pending],
                      [server, {**build_node(), 'spec': {'podCIDR': '10.52.1.0/24'}}]):
            with self.subTest(nodes=nodes), patch.object(guard, 'kube', return_value={'items': nodes}) as kube, \
                    patch.object(guard.time, 'sleep') as sleep:
                with self.assertRaises(ValueError):
                    guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', profile='control')
                kube.assert_called_once_with('get', 'nodes', '-o', 'json')
                sleep.assert_not_called()

    def test_customer_remains_single_node_and_control_waits_for_all_allocations(self):
        customer = {'spec': {'podCIDR': '10.42.0.0/24'}}
        with patch.object(guard, 'kube', side_effect=[{'items': [customer]}, {'spec': {'clusterIP': '10.43.0.1'}}, None, None]):
            self.assertEqual(guard.check_cluster('10.42.0.0/16', '10.43.0.0/16'), customer)
        with patch.object(guard, 'kube', return_value={'items': [customer, customer]}):
            with self.assertRaisesRegex(ValueError, 'exactly one customer'):
                guard.check_cluster('10.42.0.0/16', '10.43.0.0/16')
        server, worker = server_node(), build_node()
        pending = copy.deepcopy(worker)
        pending['spec'].pop('podCIDR')
        with patch.object(guard, 'kube', side_effect=[{'items': [pending, server]}, {'items': [worker, server]},
                {'spec': {'clusterIP': '10.53.0.1'}}, None, None]), patch.object(guard.time, 'sleep') as sleep:
            self.assertEqual(guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', profile='control'), server)
            sleep.assert_called_once_with(3)
        with patch.object(guard, 'kube', return_value={'items': [server, pending]}):
            with self.assertRaisesRegex(ValueError, 'registration/PodCIDR'):
                guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', wait_seconds=0, profile='control')

    def test_node_cidrs_must_be_ipv4_nonoverlapping_and_in_profile(self):
        for cidr in ('10.52.0.0/24', '10.52.0.0/25', '10.42.1.0/24', '10.52.1.1/24', 'fd00::/64'):
            worker = build_node()
            worker['spec']['podCIDR'] = cidr
            worker['spec']['podCIDRs'] = [cidr]
            with self.subTest(cidr=cidr), patch.object(guard, 'kube', return_value={'items': [server_node(), worker]}):
                with self.assertRaises(ValueError):
                    guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', profile='control')
        worker = build_node()
        worker['spec']['podCIDRs'].append('fd00::/64')
        with patch.object(guard, 'kube', return_value={'items': [server_node(), worker]}):
            with self.assertRaisesRegex(ValueError, 'IPv4 profile'):
                guard.check_cluster('10.52.0.0/16', '10.53.0.0/16', profile='control')
        with self.assertRaisesRegex(ValueError, 'non-overlapping'):
            guard.check_cluster('10.52.0.0/16', '10.52.0.0/24', profile='control')

    def test_platform_smoke_pins_all_pods_to_selected_server(self):
        source = (ROOT / 'infrastructure/ansible/test-control-cilium.sh').read_text()
        marker = '"$node" <<\'PY\' | kubectl -n "$namespace" apply -f -\n'
        generator = source.split(marker, 1)[1].split('\nPY', 1)[0]
        result = subprocess.run(['python3', '-', str(ROOT / 'deployment/airgap/versions.json'), 'smoke', 'platform-01'],
                                input=generator, text=True, capture_output=True, check=True)
        pods = [item for item in json.loads(result.stdout)['items'] if item['kind'] == 'Pod']
        self.assertEqual({pod['metadata']['name'] for pod in pods}, {'web', 'allowed', 'denied'})
        for pod in pods:
            self.assertEqual(pod['spec']['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution'],
                             {'nodeSelectorTerms': [{'matchFields': [{'key': 'metadata.name', 'operator': 'In', 'values': ['platform-01']}]}]})

    def test_flannel_control_refuses_before_any_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = CONFIG.replace('flannel-backend: none\n', '').replace('disable-network-policy: true\n', '')
            self.host(root, legacy)
            (root / 'run').mkdir()
            binaries = root / 'bin'
            binaries.mkdir()
            for name, body in {'id': 'echo 0', 'uname': 'if [ "$1" = -s ]; then echo Linux; else echo x86_64; fi',
                               'flock': 'exit 0', 'install': 'exit 99', 'curl': 'exit 99', 'systemctl': 'exit 99'}.items():
                path = binaries / name
                path.write_text('#!/bin/sh\n' + body + '\n')
                path.chmod(0o755)
            script = CONTROL
            for path in ('/etc/', '/var/lib/', '/run/'):
                script = script.replace(path, str(root) + path)
            entry = root / 'control.sh'
            entry.write_text(script)
            env = {k: v for k, v in os.environ.items() if not k.startswith(('K3S_', 'INSTALL_K3S_', 'CILIUM_', 'RAILSHOT_', 'NODE_IP'))}
            env['PATH'] = str(binaries) + ':' + env['PATH']
            result = subprocess.run(['bash', str(entry)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('Automatic CNI migration is refused', result.stderr)
            self.assertEqual((root / 'etc/rancher/k3s/config.yaml').read_text(), legacy)
            self.assertEqual((root / 'etc/railshot/node-role').read_text(), 'control')


if __name__ == '__main__':
    unittest.main()
