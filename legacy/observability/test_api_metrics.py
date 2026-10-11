"""Evaluate the API's generated PromQL with the pinned Prometheus engine."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ApiMetricsTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('PROMTOOL'), 'PROMTOOL is not configured')
    def test_timestamp_sources_do_not_collide_or_hide_older_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'observer.json'
            config.write_text(json.dumps({'version': 1, 'targets': [{
                'target_id': 'demo', 'app': 'demo-app', 'namespace': 'tenant-demo-app',
                'prometheus_url': 'http://unused.invalid', 'probe_url': 'http://unused.invalid/health',
                'node_instance': 'node:9100', 'cluster_instance': 'node:8080',
            }]}))
            config.chmod(0o600)
            # Capture the production query; all PromQL computation below runs in promtool.
            program = """
import { createMetricsObserver } from './apps/api/src/metrics.js';
await createMetricsObserver({configPath:process.argv[1], fetchImpl:async (url) => {
  if (url.searchParams.get('query').includes('job="node"')) console.log(url.searchParams.get('query'));
  return Response.json({status:'success',data:{resultType:'vector',result:[]}});
}})({target_id:'demo',app:'demo-app'});
"""
            query = subprocess.check_output(['node', '--input-type=module', '-e', program, str(config)], cwd=ROOT, text=True).strip()
            self.assertTrue(query)
            series = [('up', '1 1 1 1 1'), ('node_memory_MemAvailable_bytes', '40 40 40 40 40'),
                      ('node_memory_MemTotal_bytes', '100 _ _ _ _'),
                      ('node_filesystem_avail_bytes', '20 20 20 20 20'),
                      ('node_filesystem_size_bytes', '100 _ _ _ _')]
            inputs = [{'series': name + '{job="node",instance="node:9100"' +
                       (',mountpoint="/",device="/dev/root",fstype="ext4"' if 'filesystem' in name else '') + '}',
                       'values': values} for name, values in series]
            checks = []
            for name in ['memory_percent_observed', 'disk_percent_observed']:
                checks.append({'expr': f'({query}) and on(railshot_metric) label_replace(vector(1), "railshot_metric", "{name}", "", "")',
                               'eval_time': '2m', 'exp_samples': [{'labels': '{railshot_metric="' + name + '"}', 'value': 0}]})
            spec = Path(directory) / 'query-test.json'
            spec.write_text(json.dumps({'rule_files': [], 'evaluation_interval': '30s', 'tests': [
                {'interval': '30s', 'input_series': inputs, 'promql_expr_test': checks}]}))
            subprocess.run([os.environ['PROMTOOL'], 'test', 'rules', str(spec)], check=True)


if __name__ == '__main__':
    unittest.main()
