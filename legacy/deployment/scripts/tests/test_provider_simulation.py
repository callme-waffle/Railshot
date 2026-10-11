"""Harness boundary tests; real VM outcomes are stored separately."""
import io
import json
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from provider_simulation import FIXTURES, make_input, verify_external, extract_results, main
from input_adapter import parse_input
from simulated_node import core_hashes


class SimulationHarnessTests(unittest.TestCase):
    def test_profiles_change_environment_only_and_keep_provider_out_of_core(self):
        profiles=json.loads((FIXTURES/'provider-simulation.json').read_text())
        specs=[]
        for provider,profile in profiles.items():
            request=make_input(provider,profile,'20261002080000')
            specs.append(parse_input(request).spec)
            self.assertEqual(request['provider'],provider)
            self.assertEqual(request['runtime']['node_ip'],profile['node_ip'])
            self.assertNotIn('provider',parse_input(request).spec.__dict__)
            self.assertTrue(request['workload']['sample_content'])
        self.assertEqual(len({s.runtime.node_ip for s in specs}),3)
        self.assertEqual(len({s.environment_id for s in specs}),3)
        self.assertEqual(len({s.workload.image for s in specs}),1)

    def test_core_hashes_cover_engine_and_bootstrap_without_test_files(self):
        hashes=core_hashes()
        self.assertIn('scripts/engine.py',hashes)
        self.assertIn('bootstrap/install-k3s.sh',hashes)
        self.assertIn('cilium/install.sh',hashes)
        self.assertTrue(all(len(h)==64 for h in hashes.values()))
        self.assertFalse(any('tests/' in p for p in hashes))

    def test_host_http_200_with_wrong_body_is_rejected(self):
        response=MagicMock(); response.__enter__.return_value.status=200
        response.__enter__.return_value.read.return_value=b'other service'
        with patch('provider_simulation.build_opener') as opener:
            opener.return_value.open.return_value=response
            with self.assertRaises(RuntimeError): verify_external('http://127.0.0.1:30084/')

    def test_artifact_extraction_rejects_paths_and_links(self):
        for name,kind in (('../escape',tarfile.REGTYPE),('/tmp/escape',tarfile.REGTYPE),('link',tarfile.SYMTYPE)):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as directory:
                blob=io.BytesIO()
                with tarfile.open(fileobj=blob,mode='w') as archive:
                    item=tarfile.TarInfo(name); item.type=kind
                    if kind==tarfile.SYMTYPE: item.linkname='../escape'
                    archive.addfile(item)
                with self.assertRaises(RuntimeError): extract_results(blob.getvalue(),Path(directory))

    def test_failed_vm_stop_returns_json_and_does_not_start_the_next_vm(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[1] == 'start':
                raise subprocess.TimeoutExpired(argv, 300)
            return SimpleNamespace(returncode=1 if argv[1] == 'stop' else 0)

        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch('sys.argv', ['provider_simulation.py', '--disposable-vms', '--results', directory]), \
                 patch('provider_simulation.socket.socket'), \
                 patch('provider_simulation.subprocess.run', side_effect=fake_run), \
                 redirect_stdout(output), redirect_stderr(io.StringIO()):
                self.assertEqual(main(), 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result['aws']['status'], 'failed')
            self.assertIn('stop_error', result['aws'])
            self.assertEqual(result['gcp']['status'], 'not_run')
            self.assertEqual(result['openstack']['status'], 'not_run')
            self.assertEqual([call[1] for call in calls], ['create', 'start', 'stop'])


if __name__=='__main__': unittest.main()
