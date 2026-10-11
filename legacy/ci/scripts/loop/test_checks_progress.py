"""Checks progress contract tests; no GitHub, SDK or model calls."""
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import loop
import checks_progress as progress
from observability import event_record

ENV = {'GITHUB_REPOSITORY': 'Jasmin-Softbank/railshot-apps', 'GITHUB_RUN_ID': '1234', 'GITHUB_RUN_ATTEMPT': '1',
       'GITHUB_SHA': 'a' * 40, 'SOURCE_COMMIT': 'a' * 40, 'APP': 'calculator', 'TENANT': 'demo', 'TARGET_ID': 'test-target'}


def event(name='agent.heartbeat', **attributes):
    if name.startswith('loop.'):
        return event_record(name, component='loop', phase='loop', outcome='PASS' if name.endswith('completed') else 'RUNNING',
                            run_id='native-run', attributes={'sdk_invocations': 0 if name.endswith('completed') else None, **attributes})
    return event_record(name, component='loop', phase='agent', outcome='RUNNING', run_id='native-run',
                        attempt_id='native-run:1', attributes={'role': 'fixer', 'provider': 'codex', 'elapsed_ms': 25000,
                        'process_running': name == 'agent.heartbeat', 'snapshot_state': 'current',
                        'sdk_activity_since_previous': True, 'last_sdk_event_age_ms': 5,
                        'progress': {'elapsed_ms': 24995, 'sdk_event_count': 3, 'last_sdk_event_at_ms': 100000,
                                     'item_counts': {'reasoning': 1, 'commandExecution': 1},
                                     'last_item': {'kind': 'commandExecution', 'status': 'completed'},
                                     'token_usage': {'input_tokens': 1000, 'output_tokens': 200}}, **attributes})


class FakeChecks(progress.ChecksProgress):
    def __init__(self):
        super().__init__('sentinel-publisher-secret', ENV, 'calculator')
        self.calls, self.remote, self.lose_create, self.omit_remote = [], None, False, False

    def request(self, method, path, body=None):
        self.calls.append((method, path, deepcopy(body)))
        if method == 'GET':
            checks = [deepcopy(self.remote)] if self.remote and not self.omit_remote else []
            return {'total_count': len(checks), 'check_runs': checks}
        if method == 'POST':
            self.remote = {**body, 'id': 55, 'app': {'id': 15368, 'slug': 'github-actions'}}
            self.remote['details_url'] = f'https://github.com/{self.repository}/runs/55'
            if self.lose_create:
                raise OSError('sentinel-private-network-error')
        else:
            self.remote.update(body)
        return deepcopy(self.remote)


class ChecksProgressTest(unittest.TestCase):
    def test_repair_receipt_requires_host_release_verdict_and_redacts_descriptions(self):
        record = {'exit_code': 0, 'written': ['Dockerfile'], 'output': {
            'status': 'proposed', 'files_changed': [{'path': 'Dockerfile', 'why': 'token=secret-canary'}]}}
        record['repair_activity'] = loop.repair_changes(record)
        self.assertEqual(record['repair_activity']['changes'][0]['summary'], '[REDACTED]')
        sink = FakeChecks()
        with patch.dict(os.environ, {'RAILSHOT_RUN_ID': 'native-run', 'RAILSHOT_ATTEMPT_ID': 'native-run:1'}):
            loop.publish_repair(sink, 'fixer', record, 'L2')
            loop.publish_repair(sink, 'fixer', record, 'L2', {'ok': True, 'release_eligible': False})
            loop.publish_repair(sink, 'fixer', record, 'L2', {'ok': True, 'release_eligible': True})
        rows = json.loads(sink.payload()['text'])['items']
        self.assertEqual([r['repair']['state'] for r in rows], ['verifying', 'failed', 'succeeded'])
        for r in rows:
            self.assertEqual(progress.restored_row(r), r)
        self.assertNotIn('secret-canary', json.dumps(rows))
        invalid = event('agent.repair', repair={**rows[0]['repair'], 'raw_prompt': 'forbidden'})
        with self.assertRaises(ValueError):
            progress.row(invalid)

    def test_loop_budget_is_optional_bounded_and_survives_check_restore(self):
        self.assertNotIn('agent_budget', progress.row(event('loop.started')))
        for limit in (0, 1, 2, 3, 4, 10, 16):
            budget = {'enabled': limit > 0, 'max_invocations': limit}
            for name in ('loop.started', 'loop.completed'):
                with self.subTest(limit=limit, event=name):
                    row = progress.row(event(name, agent_budget=budget, sdk_invocations=None))
                    self.assertEqual(row['agent_budget'], budget)
                    self.assertIsNone(row['sdk_invocations'])
                    row['sequence'] = 1
                    self.assertEqual(progress.restored_row(row), row)
        for budget in (None, {}, {'enabled': True, 'max_invocations': 17},
                       {'enabled': True, 'max_invocations': 0}, {'enabled': False, 'max_invocations': 2},
                       {'enabled': 1, 'max_invocations': 1}, {'enabled': True, 'max_invocations': True},
                       {'enabled': True, 'max_invocations': 2, 'token': 'sentinel'}):
            with self.subTest(invalid=budget), self.assertRaises(ValueError):
                progress.row(event('loop.started', agent_budget=budget))

    def test_real_loop_emits_declared_budget_and_confirmed_zero_without_sdk(self):
        for repair, packaging, limit in ((0, 2, 0), (1, 0, 1), (1, 1, 2), (2, 1, 3), (1, 2, 3), (2, 2, 4)):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); upload = root / 'upload'; run = root / 'run'
                upload.mkdir(); (upload / 'app.py').write_text('print(1)')
                sink = FakeChecks()
                def gate(ws, run, attempt, *args, **kwargs):
                    path = run / f'gate-{attempt}'; path.mkdir()
                    verdict = {'ok': True, 'release_eligible': True, 'status': 'PASS'}
                    (path / 'verdict.json').write_text(json.dumps(verdict))
                    return verdict
                with patch.object(sys, 'argv', ['loop', str(upload), str(run), '--max-attempts', str(repair),
                                              '--max-packaging-attempts', str(packaging)]), \
                        patch.object(loop, 'progress_from_environment', return_value=sink), \
                        patch.object(loop, 'gate', side_effect=gate), patch.object(loop, 'agent') as agent:
                    self.assertEqual(loop.main(), 0)
                    agent.assert_not_called()
                events = json.loads(sink.remote['output']['text'])['items']
                self.assertEqual([row['event_name'] for row in events], ['loop.started', 'loop.completed'])
                self.assertTrue(all(row['agent_budget'] == {'enabled': limit > 0, 'max_invocations': limit} for row in events))
                self.assertIsNone(events[0]['sdk_invocations'])
                self.assertEqual(events[-1]['sdk_invocations'], 0)

    def test_live_safe_projection_rate_bound_and_neutral_zero_call_completion(self):
        sink = FakeChecks()
        with patch.object(progress.time, 'monotonic', return_value=0) as clock:
            sink.emit(event('loop.started'))
            sink.emit(event(command='sentinel-private-command', reasoning='sentinel-private-reasoning', thread_id='private-thread'))
            self.assertEqual([call[0] for call in sink.calls], ['GET', 'POST'])
            clock.return_value = progress.PUBLISH_INTERVAL_SECONDS
            sink.emit(event())
            self.assertEqual(sink.calls[-1][0], 'PATCH')
            sink.emit(event('loop.completed'), final=True)
        document = json.loads(sink.remote['output']['text'])
        self.assertEqual(document['status'], 'completed')
        self.assertEqual(document['items'][-1]['sdk_invocations'], 0)
        self.assertEqual(sink.remote['conclusion'], 'neutral')
        self.assertEqual([row['sequence'] for row in document['items']], [1, 2, 3, 4])
        self.assertTrue(all(row['occurred_at'].endswith('Z') for row in document['items']))
        self.assertNotIn('sentinel', json.dumps(sink.calls))
        self.assertNotIn('private-thread', json.dumps(sink.calls))
        self.assertEqual(document['items'][1]['progress']['item_counts']['reasoning'], 1)

    def test_nested_arbitrary_text_is_rejected_without_remote_call(self):
        sink, output = FakeChecks(), io.StringIO()
        bad = event(); bad['attributes']['progress']['reasoning'] = 'sentinel-private-text'
        with patch.object(sys, 'stderr', output):
            sink.emit(bad)
        self.assertEqual(sink.calls, [])
        self.assertIn('PROGRESS_UNAVAILABLE', output.getvalue())
        self.assertNotIn('sentinel', output.getvalue())

    def test_unknown_create_reconciles_same_id_without_second_post(self):
        sink, output = FakeChecks(), io.StringIO()
        sink.lose_create = True
        with patch.object(sys, 'stderr', output), patch.object(progress.time, 'monotonic', return_value=0) as clock:
            sink.emit(event('loop.started'))
            sink.omit_remote = True
            clock.return_value = progress.PUBLISH_INTERVAL_SECONDS; sink.emit(event())
            sink.omit_remote = False
            clock.return_value = 2 * progress.PUBLISH_INTERVAL_SECONDS; sink.emit(event())
        self.assertEqual(sum(method == 'POST' for method, _, _ in sink.calls), 1)
        self.assertEqual(sink.check_id, 55)
        self.assertEqual(sink.calls[-1][0], 'PATCH')
        self.assertEqual(len(json.loads(sink.remote['output']['text'])['items']), 3)
        self.assertNotIn('sentinel', output.getvalue())

    def test_same_attempt_resume_reuses_check_and_rejects_mismatched_binding(self):
        original = FakeChecks(); original.emit(event('loop.started'))
        sink = FakeChecks(); sink.remote = deepcopy(original.remote)
        sink.emit(event('loop.started'))
        self.assertEqual([call[0] for call in sink.calls], ['GET', 'PATCH'])
        self.assertEqual(len(json.loads(sink.remote['output']['text'])['items']), 1)
        original.remote['details_url'] = original.details_url
        self.assertTrue(original.matches(original.remote), 'the requested workflow link remains valid')
        for field, value in [('head_sha', 'b' * 40), ('details_url', 'https://untrusted.example'),
                             ('details_url', f'https://github.com/{original.repository}/runs/56'),
                             ('details_url', 'https://github.com/other/repo/runs/55'),
                             ('app', {'id': 123, 'slug': 'github-actions'})]:
            sink = FakeChecks(); sink.remote = deepcopy(original.remote); sink.remote[field] = value
            with patch.object(sys, 'stderr', io.StringIO()):
                sink.emit(event('loop.started'))
            self.assertEqual([call[0] for call in sink.calls], ['GET'])

    def test_duplicate_checks_or_incomplete_scan_never_creates(self):
        original = FakeChecks(); original.emit(event('loop.started'))
        for rows in ([original.remote, original.remote], [original.remote] * 20):
            sink = FakeChecks()
            with patch.object(sink, 'request', return_value={'total_count': 100, 'check_runs': rows}) as request, patch.object(sys, 'stderr', io.StringIO()):
                sink.emit(event('loop.started'))
            self.assertTrue(all(call.args[0] == 'GET' for call in request.call_args_list))
            self.assertLessEqual(request.call_count, 5)

    def test_history_is_bounded_by_count_and_bytes_with_explicit_truncation(self):
        from runner.run_agent import PROGRESS_ITEMS, PROGRESS_TOKENS
        sink = FakeChecks()
        with patch.object(progress.time, 'monotonic', return_value=0) as clock:
            sink.emit(event('loop.started'))
            for index in range(100):
                item = event()
                item['attributes']['progress']['item_counts'] = {key: 2 ** 53 - 1 for key in PROGRESS_ITEMS}
                item['attributes']['progress']['token_usage'] = {key: 2 ** 53 - 1 for key in PROGRESS_TOKENS}
                clock.return_value = (index + 1) * progress.PUBLISH_INTERVAL_SECONDS
                sink.emit(item)
        text = sink.remote['output']['text']; document = json.loads(text)
        self.assertLess(len(text.encode()), 60000)
        self.assertLessEqual(len(document['items']), 60)
        self.assertTrue(document['truncated'])
        self.assertGreater(document['items'][0]['sequence'], 1)

    def test_token_is_popped_before_even_self_test_or_invalid_cli(self):
        for arguments in (['loop', '--self-test'], ['loop']):
            with patch.dict(os.environ, {'RAILSHOT_PROGRESS_TOKEN': 'sentinel-publisher-secret'}), \
                    patch.object(sys, 'argv', arguments), patch.object(sys, 'stdout', io.StringIO()), \
                    patch.object(loop, 'progress_from_environment', return_value=None) as create:
                loop.main()
                self.assertNotIn('RAILSHOT_PROGRESS_TOKEN', os.environ)
                if create.called:
                    self.assertEqual(create.call_args.args[0], 'sentinel-publisher-secret')

    def test_real_child_and_run_binding_never_receive_token_and_transport_failure_preserves_result(self):
        for result in (0, 1):
            sink = FakeChecks()
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory); upload = root / 'upload'; run = root / 'run'
                upload.mkdir(); (upload / 'app.py').write_text('print(1)')
                def execute(arguments, run, state, progress_sink):
                    self.assertIs(progress_sink, sink)
                    self.assertNotIn('sentinel-publisher-secret', json.dumps(state.data))
                    child = subprocess.run([sys.executable, '-c',
                        'import os; assert "RAILSHOT_PROGRESS_TOKEN" not in os.environ'], capture_output=True)
                    self.assertEqual(child.returncode, 0)
                    (run / 'evidence.json').write_text(json.dumps({'status': 'PASS' if result == 0 else 'FAIL', 'sdk_invocations': 0}))
                    return result
                with patch.dict(os.environ, {**ENV, 'RAILSHOT_PROGRESS_TOKEN': 'sentinel-publisher-secret'}), \
                        patch.object(sys, 'argv', ['loop', str(upload), str(run), '--app-id', 'calculator']), \
                        patch.object(loop, 'progress_from_environment', return_value=sink), \
                        patch.object(loop, 'execute', side_effect=execute), \
                        patch.object(sink, 'request', side_effect=OSError('sentinel-private-network-error')), \
                        patch.object(sys, 'stderr', io.StringIO()) as output:
                    self.assertEqual(loop.main(), result)
                    self.assertNotIn('sentinel', output.getvalue())

    def test_http_transport_does_not_follow_redirect_or_log_response_and_respects_backoff(self):
        from unittest.mock import MagicMock
        sink, connection, output = FakeChecks(), MagicMock(), io.StringIO()
        response = connection.getresponse.return_value
        response.status = 302
        with patch.object(progress.http.client, 'HTTPSConnection', return_value=connection) as client:
            with self.assertRaises(ValueError):
                progress.ChecksProgress.request(sink, 'GET', '/only-fixed-api-path')
            client.assert_called_once_with('api.github.com', timeout=2)
            response.read.assert_not_called()
        sink = FakeChecks()
        response.status = 429
        response.getheader.side_effect = lambda name, default='': {'Retry-After': '120'}.get(name, default)
        with patch.object(progress.http.client, 'HTTPSConnection', return_value=connection), \
                patch.object(progress.time, 'monotonic', return_value=0) as clock, patch.object(sys, 'stderr', output), \
                patch.object(sink, 'request', wraps=lambda *args: progress.ChecksProgress.request(sink, *args)) as request:
            sink.emit(event('loop.started'))
            clock.return_value = 20
            sink.emit(event('loop.completed'), final=True)
            self.assertEqual(request.call_count, 1)
        self.assertNotIn('sentinel', output.getvalue())


if __name__ == '__main__':
    unittest.main()
