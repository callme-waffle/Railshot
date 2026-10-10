import json
import os
from pathlib import Path
import tempfile
import subprocess
import unittest

from render import HERE, NAMESPACE, cluster, dashboard, prometheus, render, validate


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((HERE / 'target.example.json').read_text())

    def test_example(self):
        self.assertEqual(validate(self.config), self.config)

    def test_bad_network_inputs(self):
        for key, values in {
            'node_ip': ['127.0.0.1', '0.0.0.0', '169.254.169.254', 'host; command'],
            'observer_source_cidr': ['0.0.0.0/0', '10.0.0.0/24', '127.0.0.1/32'],
            'node_metrics_port': [True, 9100, 40000, self.config['cluster_metrics_port']],
            'name': ['../bad', 'name with spaces'],
        }.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    validate({**self.config, key: value})

    def test_probe_rejects_credentials_and_non_http(self):
        for url in ['file:///etc/passwd', 'https://a:b@example.com', 'https://example.com/?token=x',
                    'https://example.com/#secret', 'https://example.com/$TOKEN', 'http://bad host']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate({**self.config, 'probe_urls': [url]})

    def test_probe_list_bounds(self):
        for urls in [['https://example.com'] * 2, ['https://example.com/' + str(i) for i in range(11)]]:
            with self.assertRaises(ValueError):
                validate({**self.config, 'probe_urls': urls})

    def test_node_only_has_no_http_probe_job(self):
        config = {**self.config, 'probe_urls': []}
        validate(config)
        self.assertEqual([job['job_name'] for job in prometheus(config)['scrape_configs']], ['node', 'cluster'])

    def test_argo_optional_private_only(self):
        self.assertEqual(len(prometheus(self.config)['scrape_configs']), 3)
        good = {**self.config, 'argocd_metrics': '10.0.0.2:8082'}
        validate(good)
        self.assertEqual(prometheus(good)['scrape_configs'][-1]['job_name'], 'argocd')
        for bad in ['8.8.8.8:8082', 'http://10.0.0.1:8082', '127.0.0.1:8082', '10.0.0.1:99999']:
            with self.assertRaises(ValueError):
                validate({**self.config, 'argocd_metrics': bad})

    def test_no_custom_correlation_labels(self):
        text = json.dumps(prometheus(self.config))
        for name in ['run_id', 'tenant', 'commit', 'digest', 'external_labels']:
            self.assertNotIn(name, text)

    def test_rbac_is_read_only_and_no_secrets(self):
        role = next(o for o in cluster(self.config)['items'] if o['kind'] == 'ClusterRole')
        for rule in role['rules']:
            self.assertEqual(rule['verbs'], ['list', 'watch'])
            self.assertNotIn('secrets', rule['resources'])
            self.assertNotIn('*', rule['resources'])

    def test_ports_and_ingress_match_target(self):
        objects = cluster(self.config)['items']
        services = [o for o in objects if o['kind'] == 'Service']
        self.assertEqual({o['spec']['ports'][0]['nodePort'] for o in services}, {30081, 30910})
        self.assertTrue(all(o['spec']['externalTrafficPolicy'] == 'Local' for o in services))
        policy = next(o for o in objects if o['kind'] == 'NetworkPolicy')
        self.assertEqual(policy['spec']['ingress'][0]['from'], [{'ipBlock': {'cidr': '192.0.2.20/32'}}])

    def test_app_pods_use_only_existing_identity_labels(self):
        ksm = next(o for o in cluster(self.config)['items'] if o['kind'] == 'Deployment')
        args = ksm['spec']['template']['spec']['containers'][0]['args']
        self.assertIn('--metric-labels-allowlist=pods=[app.kubernetes.io/name,railshot.io/target]', args)
        self.assertIn('kube_pod_labels', next(a for a in args if a.startswith('--metric-allowlist=')))

    def test_host_metrics_deny_precedes_exporter_without_allowing_other_traffic(self):
        objects = cluster(self.config)['items']
        policy = objects[0]
        self.assertEqual(policy['kind'], 'CiliumClusterwideNetworkPolicy')
        spec = policy['spec']
        self.assertEqual(set(spec), {'endpointSelector', 'enableDefaultDeny', 'egressDeny', 'labels'})
        self.assertEqual(spec['endpointSelector'], {})
        self.assertEqual(spec['enableDefaultDeny'], {'ingress': False, 'egress': False})
        self.assertEqual(spec['egressDeny'], [{'toEntities': ['host', 'remote-node'], 'toPorts': [{'ports': [
            {'port': '9100', 'protocol': 'TCP'}, {'port': str(self.config['node_metrics_port']), 'protocol': 'TCP'}]}]}])

    def test_exporter_security(self):
        for obj in cluster(self.config)['items']:
            if obj['kind'] not in ('Deployment', 'DaemonSet'):
                continue
            pod = obj['spec']['template']['spec']
            self.assertEqual(pod.get('hostNetwork', False), obj['kind'] == 'DaemonSet')
            if obj['kind'] == 'DaemonSet':
                self.assertEqual(pod['dnsPolicy'], 'ClusterFirstWithHostNet')
                args = pod['containers'][0]['args']
                self.assertIn('--collector.netdev', args)
                self.assertIn('--web.listen-address=$(HOST_IP):9100', args)
                self.assertEqual(pod['containers'][0]['env'][0]['valueFrom']['fieldRef']['fieldPath'], 'status.hostIP')
            self.assertFalse(pod.get('hostPID', False))
            for container in pod['containers']:
                self.assertFalse(container['securityContext']['allowPrivilegeEscalation'])
                self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
                self.assertNotIn(':latest', container['image'])
                for mount in container.get('volumeMounts', []):
                    self.assertTrue(mount['readOnly'])

    def test_dashboard_is_small_and_missing_is_not_zero(self):
        doc = dashboard(self.config)
        self.assertEqual(len(doc['panels']), 8)
        for panel in doc['panels']:
            self.assertEqual(panel['fieldConfig']['defaults']['noValue'], 'Unknown / no data')
            for target in panel['targets']:
                self.assertNotIn('or vector(0)', target['expr'])
                if panel['title'] != 'Collector reachability':
                    self.assertIn('up{job=', target['expr'])

    def test_render_bundle_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / 'observer'
            render(self.config, out)
            self.assertTrue((out / 'compose.yaml').exists())
            self.assertTrue((out / 'runtime-ca').is_dir())
            self.assertIn('./runtime-ca:/etc/blackbox/runtime-ca:ro', (out / 'compose.yaml').read_text())
            password = (out / 'secrets/grafana_password').read_text()
            self.assertGreater(len(password.strip()), 30)
            for path in out.rglob('*.json'):
                self.assertNotIn(password.strip(), path.read_text())
                json.loads(path.read_text())
            if __import__('os').name != 'nt':
                self.assertEqual(out.stat().st_mode & 0o777, 0o700)
                self.assertEqual((out / 'secrets').stat().st_mode & 0o777, 0o700)
            with self.assertRaises(FileExistsError):
                render(self.config, out)
            self.assertEqual((out / 'secrets/grafana_password').read_text(), password)

    def test_http_tls_and_redirects(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / 'observer'
            render(self.config, out)
            module = json.loads((out / 'blackbox.json').read_text())['modules']['http_2xx']
            self.assertFalse(module['http']['follow_redirects'])
            self.assertNotIn('insecure_skip_verify', json.dumps(module))

    @unittest.skipUnless(os.environ.get('PROMTOOL'), 'PROMTOOL is not configured')
    def test_prometheus_native_validation(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / 'observer'
            config = {**self.config, 'argocd_metrics': '10.0.0.2:8082'}
            render(config, out)
            subprocess.run([os.environ['PROMTOOL'], 'check', 'config', str(out / 'prometheus.json')], check=True)
            rules = {'groups': [{'name': 'dashboard-validation', 'rules': [
                {'record': f'test:panel_{panel["id"]}_{i}', 'expr': target['expr']}
                for panel in dashboard(config)['panels'] for i, target in enumerate(panel['targets'])]}]}
            path = out / 'rules.json'
            path.write_text(json.dumps(rules))
            subprocess.run([os.environ['PROMTOOL'], 'check', 'rules', str(path)], check=True)




if __name__ == '__main__':
    unittest.main()
