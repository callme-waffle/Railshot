"""Ownership, failed-stage, cleanup and status tests without CSP resources."""
import json
import os
from pathlib import Path
import sys
import unittest
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from unittest.mock import MagicMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine import DeploymentEngine, DeploymentError, CommandRunner
from input_adapter import parse_input
from render import labels, render, NAME
from test_contract import fixture


class Runner:
    def __init__(self,spec,foreign=False,fail=None):
        self.spec,self.foreign,self.fail,self.calls=spec,foreign,fail,[]
    def run(self,args,**kwargs):
        self.calls.append((args,kwargs))
        if self.fail and self.fail in ' '.join(args):
            raise DeploymentError('COMMAND_FAILED','ImagePullBackOff: tag does not exist',1)
        if 'kubectl' not in args: return ''
        if 'get' in args and 'namespace' in args:
            return json.dumps({'metadata':{'labels':{} if self.foreign else labels(self.spec)}})
        if 'get' in args and 'deployment' in args:
            return json.dumps(render(self.spec)['items'][1])
        if 'get' in args and ('service' in args or 'configmap' in args):
            if 'service' in args: return json.dumps(render(self.spec)['items'][2])
            return ''
        if 'get' in args and 'pods' in args: return '{"items":[]}'
        if 'get' in args and 'nodes' in args:
            return '{"items":[{"status":{"addresses":[{"type":"InternalIP","address":"127.0.0.1"}]}}]}'
        return ''


class EngineTests(unittest.TestCase):
    def loopback_engine(self, delay=0, status=503):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(delay)
                self.send_response(status)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        data = fixture()
        data['schema_version'] = '0.2'
        data['exposure']['verification_url'] = f'http://127.0.0.1:{server.server_port}/health'
        return DeploymentEngine(parse_input(data).spec)

    def test_real_curl_http_503_is_external_degraded(self):
        engine = self.loopback_engine()
        engine.result.status = 'ready'
        engine.result.endpoint = 'http://192.0.2.1:30080'
        engine.additional_endpoint()
        self.assertEqual(engine.result.status, 'ready')
        self.assertEqual(engine.result.endpoint, 'http://192.0.2.1:30080')
        self.assertEqual(engine.result.exposure_status['additional_verification']['reason'], 'HTTP 503')

    def test_real_curl_external_timeout_is_bounded(self):
        engine = self.loopback_engine(delay=5)
        start = time.monotonic()
        engine.additional_endpoint()
        self.assertLess(time.monotonic() - start, 4.5)
        self.assertEqual(engine.result.exposure_status['status'], 'degraded')
        self.assertEqual(engine.result.exposure_status['additional_verification']['reason'], 'COMMAND_FAILED')

    def external_engine(self, response=None, failure=None):
        data = fixture()
        data['schema_version'] = '0.2'
        data['exposure']['verification_url'] = 'https://public.example/health'
        spec = parse_input(data).spec
        runner = Runner(spec)
        original = runner.run
        def run(args, **kwargs):
            if args[0] == 'curl':
                runner.calls.append((args, kwargs))
                if failure:
                    raise failure
                return response
            return original(args, **kwargs)
        runner.run = run
        return DeploymentEngine(spec, runner), runner

    def test_external_timeout_keeps_node_local_runtime_ready(self):
        engine, runner = self.external_engine(failure=DeploymentError('COMMAND_TIMEOUT', 'timeout'))
        response = MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b'Railshot Runtime OK'
        with patch.object(engine, 'network_check'), patch('engine.build_opener') as opener:
            opener.return_value.open.return_value = response
            result = engine.execute('verify')
        self.assertEqual(result.status, 'ready')
        self.assertIsNone(result.error)
        self.assertEqual(result.endpoint, 'http://127.0.0.1:30080')
        self.assertEqual(result.exposure_status['status'], 'degraded')
        self.assertEqual(result.exposure_status['additional_verification']['reason'], 'COMMAND_TIMEOUT')
        calls = [(args, kwargs) for args, kwargs in runner.calls if args[0] == 'curl']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]['timeout'], 4)
        self.assertIn('--max-time', calls[0][0])
        self.assertEqual(calls[0][0][-1], 'https://public.example/health')

    def test_additional_url_success_preserves_full_path_and_scope(self):
        engine, runner = self.external_engine(response='Railshot Runtime OK\n\n200')
        engine.result.endpoint_scope = 'node-local'
        engine.additional_endpoint()
        self.assertEqual(engine.result.exposure_status['additional_verification']['status'], 'ready')
        self.assertEqual(engine.result.endpoint_scope, 'node-local-with-additional-url')
        self.assertEqual(runner.calls[0][0][-1], 'https://public.example/health')

    def test_external_bad_status_or_body_is_degraded(self):
        for output in ('Railshot Runtime OK\n500', 'other application\n200', '\n302'):
            with self.subTest(output=output):
                engine, _ = self.external_engine(response=output)
                engine.result.exposure_status = {'status': 'ready'}
                engine.additional_endpoint()
                self.assertEqual(engine.result.exposure_status['status'], 'degraded')

    def test_offline_never_probes_additional_external_url(self):
        engine, runner = self.external_engine()
        engine.result.deployment_mode = 'airgap'
        engine.additional_endpoint()
        self.assertFalse(runner.calls)
        self.assertEqual(engine.result.exposure_status['additional_verification']['reason'], 'offline_mode')

    def test_node_local_failure_still_fails_runtime(self):
        engine, _ = self.external_engine(response='Railshot Runtime OK\n200')
        with patch.object(engine, 'network_check'), patch('engine.build_opener') as opener, \
             patch('engine.time.monotonic', side_effect=[0, 181]):
            opener.return_value.open.side_effect = OSError('local endpoint unavailable')
            result = engine.execute('verify')
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.error['code'], 'ENDPOINT_HTTP_FAILED')
        self.assertFalse(any(args[0] == 'curl' for args, _ in engine.runner.calls))

    def setUp(self):
        self.spec=parse_input(fixture()).spec
        self.open=patch('engine.open',return_value=MagicMock()); self.open.start()
        self.flock=patch('engine.fcntl.flock'); self.flock.start()
        self.preflight=patch('engine.check_network', return_value=({'docker_hub': True}, {'required_unavailable': []})); self.preflight.start()
    def tearDown(self):
        self.flock.stop(); self.open.stop()
        self.preflight.stop()

    def test_success_states_and_provider_independence(self):
        runner=Runner(self.spec); engine=DeploymentEngine(self.spec,runner)
        response=MagicMock(); response.__enter__.return_value.status=200
        response.__enter__.return_value.read.return_value=b'Railshot Runtime OK\n'
        with patch.object(engine,'network_check'),patch('engine.build_opener') as opener:
            opener.return_value.open.return_value=response
            result=engine.execute()
        self.assertEqual(result.status,'ready')
        self.assertEqual([e['state'] for e in result.states],['NODE_READY','K3S_INSTALLING','K3S_READY','CILIUM_INSTALLING','CILIUM_READY','WORKLOAD_DEPLOYING','WORKLOAD_READY','ENDPOINT_READY'])
        self.assertEqual(result.endpoint,'http://127.0.0.1:30080')
        self.assertNotIn('aws',' '.join(a for args,_ in runner.calls for a in args))
        self.assertTrue(any(k.get('input_text') for _,k in runner.calls))

    def test_foreign_namespace_is_never_overwritten(self):
        runner=Runner(self.spec,foreign=True)
        result=DeploymentEngine(self.spec,runner).execute()
        self.assertEqual(result.error['code'],'OWNERSHIP_CONFLICT')
        self.assertFalse(any('apply' in args for args,_ in runner.calls))

    def test_failed_rollout_reports_stage_and_diagnostic(self):
        result=DeploymentEngine(self.spec,Runner(self.spec,fail='rollout status')).execute()
        self.assertEqual(result.status,'failed')
        self.assertEqual(result.error['stage'],'WORKLOAD_DEPLOYING')
        self.assertEqual(result.error['exit_code'],1)
        self.assertIn('ImagePullBackOff',result.error['message'])
        self.assertEqual(result.states[-1]['state'],'FAILED')

    def test_full_cleanup_requires_explicit_flag_before_commands(self):
        runner=Runner(self.spec)
        result=DeploymentEngine(self.spec,runner).execute('cleanup',True,False)
        self.assertEqual(result.error['code'],'DESTRUCTIVE_ACTION_REQUIRES_FLAG')
        self.assertFalse(runner.calls)

    def test_cleanup_only_deletes_named_owned_resources(self):
        runner=Runner(self.spec)
        result=DeploymentEngine(self.spec,runner).execute('cleanup')
        self.assertEqual(result.status,'cleaned')
        deletes=[args for args,_ in runner.calls if 'delete' in args]
        self.assertEqual(len(deletes),1)
        self.assertIn('deployment,service,configmap',deletes[0])
        self.assertNotIn('namespace',deletes[0])

    def test_lock_contention_stops_before_mutation(self):
        runner=Runner(self.spec)
        with patch('engine.fcntl.flock',side_effect=BlockingIOError):
            result=DeploymentEngine(self.spec,runner).execute()
        self.assertEqual(result.error['code'],'RUNTIME_BUSY')
        self.assertEqual(len(runner.calls),1)

    def test_verify_does_not_install_or_apply(self):
        runner=Runner(self.spec); engine=DeploymentEngine(self.spec,runner)
        response=MagicMock(); response.__enter__.return_value.status=200
        response.__enter__.return_value.read.return_value=b'Railshot Runtime OK'
        with patch.object(engine,'network_check'),patch('engine.build_opener') as opener:
            opener.return_value.open.return_value=response
            result=engine.execute('verify')
        self.assertEqual(result.status,'ready')
        self.assertFalse(any('apply' in args or any('install' in s for s in args) for args,_ in runner.calls))

    def test_requested_image_drift_is_failure(self):
        runner=Runner(self.spec); engine=DeploymentEngine(self.spec,runner)
        self.spec.workload # immutable spec stays unchanged
        original=runner.run
        def drift(args,**kwargs):
            result=original(args,**kwargs)
            if 'get' in args and 'deployment' in args:
                data=json.loads(result); data['spec']['template']['spec']['containers'][0]['image']='nginx:other'
                return json.dumps(data)
            return result
        runner.run=drift
        result=engine.execute('verify')
        self.assertEqual(result.error['code'],'WORKLOAD_DRIFT')

    def test_timeout_stops_descendant_from_mutating_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            marker=Path(directory)/'orphan-write'
            child=f'import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).write_text("orphan")'
            parent=f'import subprocess,time,sys; subprocess.Popen([sys.executable,"-c",{child!r}]); time.sleep(30)'
            with self.assertRaises(DeploymentError) as error:
                CommandRunner().run([sys.executable,'-c',parent],env=os.environ.copy(),timeout=0.2)
            self.assertEqual(error.exception.code,'COMMAND_TIMEOUT')
            time.sleep(1.2)
            self.assertFalse(marker.exists(),'timed-out child kept mutating after the command failed')


if __name__=='__main__': unittest.main()
