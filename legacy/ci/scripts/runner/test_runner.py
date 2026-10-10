import asyncio
import contextlib
import errno
import io
import json
import os
import subprocess
from importlib.metadata import version
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import run_agent
from runner import native_preflight


def native_turn(status='completed', response='{"status":"proposed"}', identity='turn-test', message='synthetic error', before=None, command=None):
    from openai_codex.models import Notification, ItemCompletedNotification, TurnCompletedNotification
    def stream():
        if before: before()
        if command is not None:
            yield Notification('item/completed', ItemCompletedNotification.model_validate({
                'threadId':'thread-test','turnId':identity,'completedAtMs':1,
                'item':{'id':'command-test','type':'commandExecution','status':'completed',
                        'command':'cat contract','commandActions':[],'cwd':'/workspace',
                        'aggregatedOutput':command[0],'exitCode':command[1]}}))
        if response:
            yield Notification('item/completed', ItemCompletedNotification.model_validate({
                'threadId':'thread-test','turnId':identity,'completedAtMs':1,
                'item':{'id':'item-test','type':'agentMessage','phase':'final_answer','text':response}}))
        yield Notification('turn/completed', TurnCompletedNotification.model_validate({
            'threadId':'thread-test','turn':{'id':identity,'status':status,'items':[],
                'error':{'message':message,'codexErrorInfo':'other'} if status=='failed' else None}}))
    return SimpleNamespace(id=identity,stream=stream)


class RunnerTest(unittest.TestCase):
    def test_path_policy_supports_recursive_globs_without_python313(self):
        for rel, pattern, expected in [('app.py', '**/*.py', True), ('src/lib/app.py', '**/*.py', True),
                                       ('src/lib/app.py', '*.py', False), ('x/tests/a.py', '**/tests/**', True),
                                       ('tests/a.py', '**/tests/**', True), ('a/test.js', 'a/test.[jt]s', True)]:
            self.assertEqual(run_agent.path_ok(rel, [pattern], []), expected)
        self.assertFalse(run_agent.path_ok('src/tests/a.py', ['**/*.py'], ['**/tests/**']))

    def test_forbidden_proposal_reports_rule_location_without_source(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            files = [{"path": "first.txt", "content": "safe"},
                     {"path": "Dockerfile", "content": "FROM scratch\nRUN private-canary || true\n"}]
            with self.assertRaises(run_agent.ProposalPolicyError) as caught:
                run_agent.apply_files(workspace, files, ["*"], [])
            receipt = run_agent.proposal_rejection(caught.exception)
            self.assertEqual("files[1].content", receipt["field"])
            self.assertEqual(2, receipt["line"])
            self.assertEqual("BYPASS_FORBIDDEN", receipt["reason"])
            self.assertEqual(64, len(receipt["rule_sha256"]))
            self.assertNotIn("private-canary", json.dumps(receipt))
            self.assertEqual([], list(workspace.iterdir()))

    def test_standard_docker_healthcheck_and_build_root_are_not_proposal_bypasses(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            content = 'FROM node:22-slim AS build\nUSER root\nFROM node:22-slim\nUSER 65532\nHEALTHCHECK CMD curl -f http://localhost:8080/ || exit 1\n'
            self.assertEqual(['Dockerfile'], run_agent.apply_files(workspace,
                [{'path': 'Dockerfile', 'content': content}], ['Dockerfile'], []))

    def test_patch_is_validated_before_any_write(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / 'work'; ws.mkdir()
            allow, deny = ['**'], []
            (ws / 'escape').symlink_to(Path(d), target_is_directory=True)
            with self.assertRaises(ValueError):
                run_agent.apply_files(ws, [{'path':'Dockerfile','content':'ok'},
                    {'path':'escape/Dockerfile','content':'bad'}], allow, deny)
            self.assertFalse((ws/'Dockerfile').exists())
            self.assertEqual(run_agent.apply_files(ws, [{'path':'.railshot/test','content':'x'}], allow, deny), ['.railshot/test'])

    def test_delete_is_explicit_scoped_and_validated_before_mutation(self):
        for scope in ('packaging', 'source'):
            with self.subTest(scope=scope), tempfile.TemporaryDirectory() as d:
                ws = Path(d)
                (ws / 'Dockerfile').write_text('old')
                (ws / 'app.py').write_text('print("app")')
                (ws / 'app_test.py').write_text('assert True')
                (ws / 'package.json').write_text('{}')
                allow, protect = run_agent.writable_rules('contract/paths.yaml', scope)
                for rejected in ('package.json', 'app_test.py', 'missing.Dockerfile'):
                    with self.assertRaises(ValueError):
                        run_agent.apply_files(ws, [
                            {'path': 'Dockerfile', 'content': 'new'},
                            {'path': rejected, 'action': 'delete', 'content': ''}],
                            allow, protect, repair_scope=scope)
                    self.assertEqual((ws / 'Dockerfile').read_text(), 'old')
                patch_files = [{'path': 'app.py', 'action': 'delete', 'content': ''}]
                if scope == 'packaging':
                    with self.assertRaises(ValueError):
                        run_agent.apply_files(ws, patch_files, allow, protect, repair_scope=scope)
                else:
                    run_agent.apply_files(ws, patch_files, allow, protect, repair_scope=scope)
                    self.assertFalse((ws / 'app.py').exists())
                run_agent.apply_files(ws, [{'path': 'Dockerfile', 'action': 'delete', 'content': ''}], allow, protect)
                self.assertFalse((ws / 'Dockerfile').exists())

    def test_deletion_preserves_symlink_binary_and_size_boundaries(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / 'real.Dockerfile').write_text('old')
            (ws / 'link.Dockerfile').symlink_to(ws / 'real.Dockerfile')
            (ws / 'binary.Dockerfile').write_bytes(b'abc\0def')
            (ws / 'large.Dockerfile').write_text('x' * 20001)
            allow, protect = run_agent.writable_rules('contract/paths.yaml')
            for name in ('link.Dockerfile', 'binary.Dockerfile', 'large.Dockerfile'):
                with self.assertRaises(ValueError):
                    run_agent.apply_files(ws, [{'path': name, 'action': 'delete', 'content': ''}], allow, protect)
                self.assertTrue((ws / name).exists())

    def test_sdk_contract(self):
        import openai_codex
        profile = run_agent.load_yaml(run_agent.PLATFORM / 'runner/profiles.yaml')
        cfg = profile['providers']['codex']
        self.assertEqual(cfg['model'], 'gpt-6.1-sol')
        self.assertEqual(cfg['reasoning_effort'], 'medium')
        result = SimpleNamespace(status='completed', final_response='{"status":"proposed"}', id='turn-test')
        observed = []
        def complete():
            self.assertEqual(observed[-1], ('turn.started', {'turn_id': 'turn-test'}))
            return result
        thread = SimpleNamespace(id='thread-test', turn=Mock(return_value=native_turn(before=complete)))
        with tempfile.TemporaryDirectory() as d, patch.object(openai_codex, 'Codex') as sdk, \
                patch.object(run_agent, 'codex_preflight', return_value={'status':'PASS','model_calls':0}) as preflight, \
                patch.dict('os.environ', {'RAILSHOT_AUTH_MODE':'subscription', 'RAILSHOT_CODEX_HOME':'/tmp/operator-auth', 'CODEX_API_KEY':'', 'OPENAI_API_KEY':''}):
            sdk.return_value.__enter__.return_value.thread_start.return_value = thread
            out, meta = run_agent.run_codex(cfg, 'policy', 'task', {'type':'object','properties':{}}, Path(d), Path(d),
                                          emit=lambda kind, **fields: observed.append((kind, fields)))
            self.assertEqual(out['status'], 'proposed')
            self.assertEqual(meta['runtime'], 'openai-codex')
            args = sdk.return_value.__enter__.return_value.thread_start.call_args.kwargs
            self.assertNotIn('sandbox', args)  # legacy sandbox would override the read profile
            config = sdk.call_args.args[0]
            self.assertEqual(preflight.call_args.args[1], config.config_overrides)
            self.assertIn('default_permissions="railshot_read"', config.config_overrides)
            encoded = next(item.split('=', 1)[1] for item in config.config_overrides if item.startswith('permissions.railshot_read.filesystem='))
            rules = tomllib.loads('rules=' + encoded)['rules']
            self.assertEqual(rules[':root'], 'deny')
            self.assertEqual(rules[str(Path('/tmp/operator-auth').resolve())], 'deny')
            self.assertEqual(rules[str(Path(d).resolve() / '**/.env*')], 'deny')
            self.assertEqual(rules['glob_scan_max_depth'], 32)
            self.assertEqual(args['approval_mode'], openai_codex.ApprovalMode.deny_all)
            self.assertTrue(args['ephemeral'])
            self.assertEqual(args['model'], 'gpt-6.1-sol')
            thread.turn.assert_called_once()
            self.assertEqual(thread.turn.call_args.kwargs['effort'], 'medium')
            self.assertEqual(meta['requested_model'], 'gpt-6.1-sol')
            self.assertEqual(meta['requested_reasoning_effort'], 'medium')
            self.assertEqual(meta['session_id'], 'thread-test')
            self.assertEqual(meta['turn_id'], 'turn-test')
            self.assertEqual([event for event, _ in observed if event != 'turn.progress'],
                             ['sandbox.checked', 'session.starting', 'session.started', 'turn.started', 'session.finished'])
            self.assertTrue(any(event == 'turn.progress' for event, _ in observed))

    def test_claude_sdk_offline_contract_and_read_guards(self):
        import claude_agent_sdk
        self.assertEqual(version('claude-agent-sdk'), '0.2.158')
        profile = run_agent.load_yaml(run_agent.PLATFORM / 'runner/profiles.yaml')
        cfg = profile['providers']['claude']
        schema = {'type': 'object', 'properties': {'status': {'type': 'string'}}}
        captured = {}

        async def query(*, prompt, options):
            # Actual SDK options/result objects; only the provider boundary is mocked.
            captured['options'] = options
            captured['prompt'] = [message async for message in prompt]
            yield claude_agent_sdk.SystemMessage(subtype='init', data={'session_id': 'offline-contract'})
            yield claude_agent_sdk.ResultMessage(
                subtype='success', duration_ms=1, duration_api_ms=0,
                is_error=False, num_turns=1, session_id='offline-contract',
                total_cost_usd=None, structured_output={'status': 'proposed'},
                permission_denials=[])

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            workspace = base / 'workspace'
            workspace.mkdir()
            (workspace / 'app.py').write_text('value = 1\n')
            outside = base / 'outside.py'
            outside.write_text('outside = True\n')
            observed = []
            with patch.object(claude_agent_sdk, 'query', side_effect=query) as mocked:
                output, meta = asyncio.run(run_agent.run_claude(
                    cfg, 'trusted policy', 'offline task', schema, workspace,
                    [workspace], profile['read_deny'], lambda kind, **fields: observed.append((kind, fields))))
            mocked.assert_called_once()
            opts = captured['options']
            self.assertIsInstance(opts, claude_agent_sdk.ClaudeAgentOptions)
            self.assertEqual(opts.tools, ['Read', 'Glob', 'Grep'])
            self.assertEqual(opts.allowed_tools, ['Read', 'Glob', 'Grep'])
            self.assertEqual(opts.setting_sources, [])
            self.assertTrue(opts.strict_mcp_config)
            self.assertEqual(opts.mcp_servers, {})
            self.assertEqual(opts.model, cfg['model'])
            self.assertEqual(opts.max_turns, cfg['max_turns'])
            self.assertEqual(opts.max_budget_usd, cfg['max_budget_usd'])
            self.assertEqual(opts.output_format, {'type': 'json_schema', 'schema': schema})
            self.assertEqual(captured['prompt'], [{'type': 'user', 'message': {'role': 'user', 'content': 'offline task'}}])
            self.assertEqual(output, {'status': 'proposed'})
            self.assertIsNone(meta['cost_usd'])  # Unknown mock cost is not a free hosted call.
            self.assertEqual(meta['session_id'], 'offline-contract')
            self.assertIsNone(meta['turn_id'])
            self.assertEqual([event for event, _ in observed], ['session.starting', 'session.started', 'session.finished'])
            # Files added after preflight must also be denied by tool-time checks.
            (workspace / '.envrc').write_text('EXAMPLE=fixture-only\n')
            (workspace / 'nested').mkdir()
            (workspace / 'nested/.env.local').write_text('EXAMPLE=fixture-only\n')
            (workspace / 'escape.py').symlink_to(outside)
            (workspace / '.env').symlink_to(workspace / 'app.py')
            matcher = opts.hooks['PreToolUse'][0]
            self.assertIsInstance(matcher, claude_agent_sdk.HookMatcher)
            self.assertEqual(matcher.matcher, 'Read|Glob|Grep')
            guard = matcher.hooks[0]
            cases = [
                ('Read', {'file_path': 'app.py'}, False),
                ('Read', {'file_path': '.env'}, True),
                ('Read', {'file_path': '.envrc'}, True),
                ('Read', {'file_path': 'nested/.env.local'}, True),
                ('Grep', {'path': str(workspace), 'pattern': 'EXAMPLE'}, True),
                ('Glob', {'pattern': '**/*'}, True),
                ('Grep', {'path': str(workspace / '.env'), 'pattern': 'EXAMPLE'}, True),
                ('Glob', {'pattern': str(workspace / '.env')}, True),
                ('Read', {'file_path': str(outside)}, True),
                ('Read', {'file_path': '../outside.py'}, True),
                ('Glob', {'pattern': str(base / '*.py')}, True),
                ('Read', {'file_path': 'escape.py'}, True),
            ]
            for tool, tool_input, denied in cases:
                with self.subTest(tool=tool, tool_input=tool_input):
                    decision = asyncio.run(guard({'tool_name': tool, 'tool_input': tool_input}, 'offline-tool', {}))
                    if denied:
                        self.assertEqual(decision['hookSpecificOutput']['permissionDecision'], 'deny')
                    else:
                        self.assertEqual(decision, {})

    def test_claude_sdk_missing_or_failed_structured_result_stops(self):
        import claude_agent_sdk
        profile = run_agent.load_yaml(run_agent.PLATFORM / 'runner/profiles.yaml')
        for is_error, structured in [(True, {'status': 'proposed'}), (False, None)]:
            async def query(**kwargs):
                yield claude_agent_sdk.ResultMessage(
                    subtype='error' if is_error else 'success', duration_ms=1,
                    duration_api_ms=0, is_error=is_error, num_turns=1,
                    session_id='offline-failure', structured_output=structured)

            with self.subTest(is_error=is_error), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory).resolve()
                with patch.object(claude_agent_sdk, 'query', side_effect=query), self.assertRaises(run_agent.OperationError) as error:
                    asyncio.run(run_agent.run_claude(profile['providers']['claude'], 'policy', 'task',
                        {'type': 'object'}, workspace, [workspace], profile['read_deny']))
                self.assertEqual(error.exception.code, 'SDK_EXECUTION_FAILED' if is_error else 'SDK_OUTPUT_INVALID')
                self.assertEqual(error.exception.outcome, 'FAIL')
                self.assertEqual(error.exception.side_effect, 'completed')

    def test_denied_read_tree_stops_before_either_sdk_launches(self):
        import claude_agent_sdk
        import openai_codex
        profile = run_agent.load_yaml(run_agent.PLATFORM / 'runner/profiles.yaml')
        for name in ['.envrc', 'nested/.env.production', 'nested/id_ecdsa', 'private.key', 'escape']:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory).resolve()
                unsafe = workspace / name
                unsafe.parent.mkdir(parents=True, exist_ok=True)
                if name == 'escape':
                    unsafe.symlink_to(workspace, target_is_directory=True)
                else:
                    unsafe.write_text('sentinel-must-not-be-read')
                with patch.object(openai_codex, 'Codex') as codex, self.assertRaises(run_agent.OperationError) as error:
                    run_agent.run_codex(profile['providers']['codex'], 'policy', 'task', {'type':'object'},
                                        workspace, workspace, profile['read_deny'])
                self.assertEqual(error.exception.code, 'SDK_POLICY_DENIED')
                self.assertEqual(error.exception.outcome, 'BLOCKED')
                self.assertEqual(error.exception.side_effect, 'none')
                codex.assert_not_called()
                with patch.object(claude_agent_sdk, 'query') as claude, self.assertRaises(run_agent.OperationError):
                    asyncio.run(run_agent.run_claude(profile['providers']['claude'], 'policy', 'task',
                                {'type':'object'}, workspace, [workspace], profile['read_deny']))
                claude.assert_not_called()

    def test_lifecycle_private_ordered_receipts_and_replay_rejection(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'RAILSHOT_RUN_ID': 'run-fixture', 'RAILSHOT_ATTEMPT_ID': 'run-fixture:2'}):
            run = Path(directory)
            state, emit = run_agent.lifecycle(run, 'fixer', 'codex', 'gpt-6.1-sol')
            emit('agent.started', status='running')
            emit('session.started', session_id='thread-fixture', thread_id='thread-fixture', sdk_status='running')
            emit('turn.started', turn_id='turn-fixture')
            emit('session.finished', sdk_status='completed')
            emit('agent.completed', status='completed')
            events = [json.loads(line) for line in (run/'fixer-events.jsonl').read_text().splitlines()]
            self.assertEqual([event['sequence'] for event in events], list(range(1, 6)))
            self.assertTrue(all(event['attempt_id'] == 'run-fixture:2' for event in events))
            self.assertEqual(json.loads((run/'fixer-session.json').read_text()), events[-1])
            self.assertEqual(events[3]['event_name'], 'session.finished')
            self.assertEqual(events[3]['phase'], 'invoke')
            self.assertEqual(events[3]['outcome'], 'PASS')
            self.assertEqual(events[3]['status'], 'running')  # SDK completion is not agent/patch completion.
            self.assertEqual(events[-1]['phase'], 'agent')
            self.assertEqual(events[-1]['outcome'], 'PASS')
            self.assertTrue(all(event['component'] == 'runner' and event['event_id'] for event in events))
            self.assertEqual(set(events[-1]['attributes']), {'role', 'provider', 'model', 'status', 'sdk_status',
                             'session_id', 'thread_id', 'turn_id', 'conversation_resume', 'resume_reason'})
            self.assertEqual(state['session_id'], 'thread-fixture')
            self.assertEqual(state['conversation_resume'], 'unsupported')
            for name in ['fixer-events.jsonl', 'fixer-session.json']:
                self.assertEqual((run/name).stat().st_mode & 0o777, 0o600)
            with self.assertRaises(run_agent.OperationError):
                run_agent.lifecycle(run, 'fixer', 'codex', 'gpt-6.1-sol')

    def test_main_uncertain_sdk_failure_keeps_ids_without_raw_exception_or_retry(self):
        def fail(cfg, system, task, schema, workspace, run, deny, emit):
            emit('session.starting', sdk_status='running')
            emit('session.started', session_id='thread-fixture', thread_id='thread-fixture')
            emit('turn.started', turn_id='turn-fixture')
            try:
                raise OSError(errno.ECONNRESET, 'sentinel-private-connection')
            except OSError as exc:
                raise RuntimeError('sentinel-private-provider-error') from exc
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace, run, task = base/'workspace', base/'run', base/'task.txt'
            workspace.mkdir(); task.write_text('sentinel-private-prompt')
            argv = ['runner', 'fixer', '--workspace', str(workspace), '--run', str(run), '--task', str(task)]
            with patch('sys.argv', argv), patch.object(run_agent, 'run_codex', side_effect=fail) as sdk, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_agent.main(), 1)
                self.assertEqual(run_agent.main(), 1)
            sdk.assert_called_once()
            record = json.loads((run/'fixer.json').read_text())
            self.assertEqual(record['meta']['status'], 'unknown')
            self.assertEqual(record['meta']['turn_id'], 'turn-fixture')
            self.assertEqual(record['error']['code'], 'SDK_OUTCOME_UNKNOWN')
            self.assertEqual(record['error']['outcome'], 'UNKNOWN')
            self.assertEqual(record['error']['side_effect'], 'unknown')
            self.assertEqual(record['error']['retry_policy'], 'after_reconcile')
            self.assertEqual(record['error']['causes'][0]['type'], 'builtins.RuntimeError')
            self.assertEqual(record['error']['causes'][0]['frames'][-1]['function'], 'fail')
            self.assertEqual(record['error']['causes'][1]['errno'], errno.ECONNRESET)
            snapshot = json.loads((run/'fixer-session.json').read_text())
            self.assertEqual(snapshot['event'], 'agent.unknown')
            self.assertEqual(snapshot['error']['code'], 'SDK_OUTCOME_UNKNOWN')
            for name in ['fixer.json', 'fixer-session.json', 'fixer-events.jsonl']:
                text = (run/name).read_text()
                self.assertNotIn('sentinel-private', text)
                self.assertEqual((run/name).stat().st_mode & 0o777, 0o600)

    def test_native_resume_is_explicitly_unsupported_without_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace, run, task = base/'workspace', base/'run', base/'task.txt'
            workspace.mkdir(); task.write_text('offline task')
            argv = ['runner', 'fixer', '--workspace', str(workspace), '--run', str(run), '--task', str(task),
                    '--resume-session-id', 'prior-thread']
            with patch('sys.argv', argv), patch.object(run_agent, 'run_codex') as sdk, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_agent.main(), 1)
            sdk.assert_not_called()
            meta = json.loads((run/'fixer.json').read_text())['meta']
            self.assertEqual(meta['conversation_resume'], 'unsupported')
            self.assertEqual(meta['resume_reason'], 'ephemeral_thread')
            self.assertEqual(meta['sdk_status'], 'not_started')
            self.assertEqual(meta['status'], 'failed')
            error = json.loads((run/'fixer.json').read_text())['error']
            self.assertEqual(error['code'], 'SDK_RESUME_UNSUPPORTED')
            self.assertEqual(error['outcome'], 'BLOCKED')
            self.assertEqual(error['side_effect'], 'none')

    def test_invalid_structured_result_still_writes_failed_receipt(self):
        def invalid(cfg, system, task, schema, workspace, run, deny, emit):
            emit('session.finished', sdk_status='completed', session_id='thread-fixture')
            return ['invalid-output'], {}
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace, run, task = base/'workspace', base/'run', base/'task.txt'
            workspace.mkdir(); task.write_text('offline task')
            argv = ['runner', 'fixer', '--workspace', str(workspace), '--run', str(run), '--task', str(task)]
            with patch('sys.argv', argv), patch.object(run_agent, 'run_codex', side_effect=invalid), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_agent.main(), 1)
            record = json.loads((run/'fixer.json').read_text())
            self.assertEqual(record['meta']['status'], 'failed')
            self.assertEqual(record['output'], {})
            self.assertEqual(record['error']['code'], 'SDK_OUTPUT_INVALID')
            self.assertEqual(record['error']['outcome'], 'FAIL')
            self.assertEqual(record['error']['side_effect'], 'none')
            self.assertEqual(record['meta']['sdk_status'], 'completed')
            self.assertTrue(record['proposal_rejection']['safe_to_replan'])
            self.assertEqual(record['error']['causes'][0]['type'], 'jsonschema.exceptions.ValidationError')

    def test_lifecycle_rejects_unapproved_native_attributes(self):
        with tempfile.TemporaryDirectory() as directory:
            state, emit = run_agent.lifecycle(Path(directory), 'fixer', 'codex', 'gpt-6.1-sol')
            with self.assertRaises(run_agent.OperationError) as error:
                emit('turn.started', prompt='sentinel-private-prompt')
            self.assertEqual(error.exception.code, 'INTERNAL_ERROR')
            self.assertNotIn('sentinel-private-prompt', (Path(directory)/'fixer-events.jsonl').read_text())

    def test_event_storage_failure_stops_before_provider_and_keeps_safe_os_cause(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace, run, task = base/'workspace', base/'run', base/'task.txt'
            workspace.mkdir(); task.write_text('offline task')
            argv = ['runner', 'fixer', '--workspace', str(workspace), '--run', str(run), '--task', str(task)]
            stdout = io.StringIO()
            with patch('sys.argv', argv), patch.object(run_agent, 'run_codex') as sdk, \
                    patch.object(run_agent.os, 'fsync', side_effect=OSError(errno.ENOSPC, 'sentinel-private-filesystem')), \
                    contextlib.redirect_stdout(stdout):
                self.assertEqual(run_agent.main(), 1)
            sdk.assert_not_called()
            event = json.loads(stdout.getvalue())
            self.assertEqual(event['error']['code'], 'OBSERVATION_WRITE_FAILED')
            self.assertEqual(event['error']['side_effect'], 'none')
            self.assertEqual(event['error']['causes'][0]['errno'], errno.ENOSPC)
            self.assertTrue(event['error']['causes'][0]['frames'])
            self.assertNotIn('sentinel-private', stdout.getvalue())

    def test_completed_proposal_write_rejection_has_separate_phase(self):
        def propose(cfg, system, task, schema, workspace, run, deny, emit):
            emit('session.finished', sdk_status='completed', session_id='thread-fixture')
            return {'status':'proposed', 'summary':'offline', 'files_changed':[], 'assumptions':[], 'confidence':'high'}, {}
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace, run, task = base/'workspace', base/'run', base/'task.txt'
            workspace.mkdir(); task.write_text('offline task')
            argv = ['runner', 'fixer', '--workspace', str(workspace), '--run', str(run), '--task', str(task)]
            with patch('sys.argv', argv), patch.object(run_agent, 'run_codex', side_effect=propose), \
                    patch.object(run_agent, 'apply_files', side_effect=ValueError('sentinel-private-patch')), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_agent.main(), 1)
            error = json.loads((run/'fixer.json').read_text())['error']
            self.assertEqual((error['code'], error['phase'], error['outcome'], error['side_effect']),
                             ('SDK_PATCH_REJECTED', 'patch', 'FAIL', 'none'))
            self.assertEqual(error['causes'][0]['type'], 'builtins.ValueError')
            self.assertNotIn('sentinel-private', (run/'fixer-events.jsonl').read_text())

    def test_error_event_storage_failure_after_call_cannot_continue_or_retry(self):
        failed_storage = False
        original_open = Path.open
        def provider(cfg, system, task, schema, workspace, run, deny, emit):
            nonlocal failed_storage
            emit('session.starting', sdk_status='running')
            emit('session.started', session_id='thread-fixture', thread_id='thread-fixture')
            failed_storage = True
            raise TimeoutError('sentinel-private-timeout')
        def open_file(path, *args, **kwargs):
            if failed_storage and path.name == 'fixer-events.jsonl':
                raise OSError(errno.ENOSPC, 'sentinel-private-disk')
            return original_open(path, *args, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace, run, task = base/'workspace', base/'run', base/'task.txt'
            workspace.mkdir(); task.write_text('offline task')
            argv = ['runner', 'fixer', '--workspace', str(workspace), '--run', str(run), '--task', str(task)]
            stdout = io.StringIO()
            with patch('sys.argv', argv), patch.object(run_agent, 'run_codex', side_effect=provider) as sdk, \
                    patch.object(run_agent, 'apply_files') as writer, patch.object(Path, 'open', open_file), \
                    contextlib.redirect_stdout(stdout):
                self.assertEqual(run_agent.main(), 1)
            sdk.assert_called_once(); writer.assert_not_called()
            event = json.loads(stdout.getvalue())
            self.assertEqual(event['error']['code'], 'OBSERVATION_WRITE_FAILED')
            self.assertEqual(event['outcome'], 'UNKNOWN')
            self.assertEqual(event['error']['side_effect'], 'unknown')
            self.assertEqual(event['error']['causes'][0]['errno'], errno.ENOSPC)
            record = json.loads((run/'fixer.json').read_text())
            self.assertEqual(record['error']['code'], 'SDK_OUTCOME_UNKNOWN')
            self.assertEqual(record['error']['causes'][0]['type'], 'builtins.TimeoutError')
            self.assertEqual(record['meta']['session_id'], 'thread-fixture')
            self.assertNotIn('sentinel-private', stdout.getvalue())

    def test_codex_terminal_failure_and_invalid_json_are_distinct(self):
        import openai_codex
        cfg = run_agent.load_yaml(run_agent.PLATFORM/'runner/profiles.yaml')['providers']['codex']
        for status, response, code in [('failed', '', 'SDK_EXECUTION_FAILED'),
                                       ('completed', 'sentinel-invalid-json', 'SDK_OUTPUT_INVALID')]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                result = SimpleNamespace(id='turn-fixture', status=status, final_response=response)
                thread = SimpleNamespace(id='thread-fixture', turn=lambda *a, **kw: native_turn(status,response,result.id))
                with patch.object(openai_codex, 'Codex') as sdk, \
                        patch.object(run_agent, 'codex_preflight', return_value={'status':'PASS','model_calls':0}), patch.dict(os.environ, {
                        'RAILSHOT_AUTH_MODE':'subscription', 'RAILSHOT_CODEX_HOME':'/tmp/operator-auth',
                        'CODEX_API_KEY':'', 'OPENAI_API_KEY':''}), self.assertRaises(run_agent.OperationError) as error:
                    sdk.return_value.__enter__.return_value.thread_start.return_value = thread
                    run_agent.run_codex(cfg, 'policy', 'task', {'type':'object','properties':{}}, Path(directory), Path(directory))
                self.assertEqual((error.exception.code, error.exception.outcome, error.exception.side_effect),
                                 (code, 'FAIL', 'completed'))
                self.assertNotIn('sentinel-invalid-json', json.dumps(error.exception.as_dict()))

    def test_completed_turn_with_native_sandbox_failure_cannot_apply_proposal(self):
        import openai_codex
        profile = run_agent.load_yaml(run_agent.PLATFORM / 'runner/profiles.yaml')
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory); workspace = base/'workspace'; workspace.mkdir()
            run = base/'run'; run.mkdir(); task = base/'task'; task.write_text('package source')
            response = json.dumps({'status':'proposed','files':[{'path':'Dockerfile','content':'FROM scratch'}]})
            thread = SimpleNamespace(id='thread-test', turn=Mock(return_value=native_turn(
                response=response, command=('bwrap: No permissions to create a new namespace\nsentinel-private', 1))))
            args = SimpleNamespace(role='adapter', provider='codex', workspace=str(workspace), run=str(run),
                                   task=str(task), repair_scope='packaging', resume_session_id=None)
            with patch.object(openai_codex, 'Codex') as sdk, \
                    patch.object(run_agent, 'codex_preflight', return_value={'status':'PASS','model_calls':0}), \
                    patch.dict(os.environ, {'RAILSHOT_AUTH_MODE':'subscription','RAILSHOT_CODEX_HOME':'/tmp/operator-auth',
                                             'CODEX_API_KEY':'','OPENAI_API_KEY':''}), \
                    contextlib.redirect_stdout(io.StringIO()):
                sdk.return_value.__enter__.return_value.thread_start.return_value = thread
                self.assertEqual(run_agent.execute(args), 1)
            record = json.loads((run/'adapter.json').read_text())
            self.assertEqual((record['error']['code'], record['error']['outcome'], record['error']['retry_policy']),
                             ('SDK_SANDBOX_UNAVAILABLE', 'BLOCKED', 'after_configuration'))
            self.assertFalse((workspace/'Dockerfile').exists())
            self.assertEqual(record['written'], [])
            self.assertEqual(record['meta']['sandbox_preflight']['status'], 'PASS')
            self.assertNotIn('sentinel-private', json.dumps(record))
            self.assertEqual(record['instructions_sha256'], __import__('hashlib').sha256(
                sdk.return_value.__enter__.return_value.thread_start.call_args.kwargs['developer_instructions'].encode()).hexdigest()[:12])
            self.assertIn((run_agent.PLATFORM/'agents/DONT.md').read_text(),
                          sdk.return_value.__enter__.return_value.thread_start.call_args.kwargs['developer_instructions'])
        success = run_agent.collect_codex_turn(native_turn(command=('No permissions to create a new namespace', 0)))
        self.assertFalse(run_agent.codex_sandbox_failure(success))  # Merely reading this text is not a failed tool.

    def test_preflight_failure_prevents_model_start(self):
        import openai_codex
        cfg = run_agent.load_yaml(run_agent.PLATFORM/'runner/profiles.yaml')['providers']['codex']
        failure = run_agent.OperationError('SDK_SANDBOX_UNAVAILABLE', component='runner', phase='sandbox.preflight',
                                           retry_policy='after_configuration')
        with tempfile.TemporaryDirectory() as directory, patch.object(openai_codex, 'Codex') as sdk, \
                patch.object(run_agent, 'codex_preflight', side_effect=failure), \
                patch.dict(os.environ, {'RAILSHOT_AUTH_MODE':'subscription','RAILSHOT_CODEX_HOME':'/tmp/operator-auth',
                                       'CODEX_API_KEY':'','OPENAI_API_KEY':''}), self.assertRaises(run_agent.OperationError):
            run_agent.run_codex(cfg, 'policy', 'task', {'type':'object'}, Path(directory), Path(directory))
        sdk.assert_not_called()

    def test_preflight_rejects_disabled_sandbox_and_timeout_without_exposing_output(self):
        execute = native_preflight._execute
        def without_sandbox(command, **kwargs):
            if command[0] == '/probe-codex':
                command = command[command.index('--') + 1:]
            return execute(command, **kwargs)
        for effect in (without_sandbox, subprocess.TimeoutExpired('native-sandbox', 10, stderr='sentinel-private')):
            with self.subTest(effect=type(effect).__name__), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve(); workspace=base/'workspace'; workspace.mkdir()
                run=base/'run'; run.mkdir()
                with patch.object(native_preflight, '_execute', side_effect=effect), self.assertRaises(run_agent.OperationError) as caught:
                    native_preflight.check('/probe-codex', (), workspace, run, base/'auth')
                receipt=json.loads((run/'codex-sandbox-preflight.json').read_text())
                self.assertEqual(receipt['status'], 'BLOCKED')
                self.assertEqual(caught.exception.code, 'SDK_SANDBOX_UNAVAILABLE')
                self.assertNotIn('sentinel-private', json.dumps(receipt)+json.dumps(caught.exception.as_dict()))
                self.assertEqual(list(run.iterdir()), [run/'codex-sandbox-preflight.json'])

    def test_native_failed_run_raises_but_public_stream_preserves_terminal_evidence(self):
        from openai_codex import TurnHandle
        turn=native_turn('failed','',message='Invalid schema: uniqueItems; sentinel-private-value')
        with self.assertRaises(RuntimeError): TurnHandle.run(turn)
        result=run_agent.collect_codex_turn(turn)
        self.assertEqual(result.status.value,'failed')
        detail=run_agent.codex_failure_diagnostic(result.error)
        self.assertEqual(detail['category'],'invalid_output_schema')
        self.assertEqual(len(detail['message_sha256']),64)
        self.assertNotIn('sentinel-private-value',json.dumps(detail))
        missing=SimpleNamespace(id='turn-test',stream=lambda:(x for x in []))
        with self.assertRaises(run_agent.OperationError) as caught:run_agent.collect_codex_turn(missing)
        self.assertEqual((caught.exception.code,caught.exception.outcome),('SDK_OUTCOME_UNKNOWN','UNKNOWN'))

    def test_wire_schema_removes_unsupported_constraints_without_weakening_canonical(self):
        schema={'type':'object','properties':{'version':{'const':1},'kind':{'enum':['draft']},
            'items':{'type':'array','uniqueItems':True,'items':{'type':'string'}}},'required':['version']}
        projected=run_agent.strict_variant(schema)
        self.assertTrue(schema['properties']['items']['uniqueItems'])
        self.assertEqual(schema['properties']['version'],{'const':1})
        self.assertEqual(projected['properties']['version'],{'enum':[1],'type':'integer'})
        self.assertNotIn('uniqueItems',json.dumps(projected))

    def test_validated_main_success_is_later_than_sdk_completion(self):
        def provider(cfg, system, task, schema, workspace, run, deny, emit):
            emit('session.finished', sdk_status='completed', session_id='thread-fixture')
            return {'status':'proposed', 'summary':'offline', 'files_changed':[], 'assumptions':[], 'confidence':'high'}, {}
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace, run, task = base/'workspace', base/'run', base/'task.txt'
            workspace.mkdir(); task.write_text('offline task')
            argv = ['runner', 'fixer', '--workspace', str(workspace), '--run', str(run), '--task', str(task)]
            with patch('sys.argv', argv), patch.object(run_agent, 'run_codex', side_effect=provider), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_agent.main(), 0)
            events = [json.loads(line) for line in (run/'fixer-events.jsonl').read_text().splitlines()]
            self.assertEqual((events[-2]['event_name'], events[-2]['phase'], events[-2]['status']),
                             ('session.finished', 'invoke', 'running'))
            self.assertEqual((events[-1]['event_name'], events[-1]['phase'], events[-1]['outcome']),
                             ('agent.completed', 'agent', 'PASS'))
            self.assertIsNone(events[-1]['error'])
            self.assertEqual(run.stat().st_mode & 0o777, 0o700)

    def test_patch_staging_failure_preserves_every_original(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            for name in ('Dockerfile', '.dockerignore'):
                (workspace/name).write_text('before')
            original_open = Path.open
            def fail_stage(path, mode='r', *args, **kwargs):
                if path.parent.name.startswith('.railshot-patch-') and path.name == '1' and mode == 'x':
                    raise OSError(errno.ENOSPC, 'private sentinel')
                return original_open(path, mode, *args, **kwargs)
            applied = []
            with patch.object(Path, 'open', fail_stage), self.assertRaises(OSError):
                run_agent.apply_files(workspace, [{'path':name,'content':'after'} for name in ('Dockerfile','.dockerignore')],
                                      ['**'], [], applied=applied)
            self.assertEqual(applied, [])
            self.assertEqual([(workspace/name).read_text() for name in ('Dockerfile','.dockerignore')], ['before','before'])
            self.assertFalse(list(workspace.glob('.railshot-patch-*')))

    def test_partial_atomic_patch_is_preserved_in_failure_receipt(self):
        def provider(cfg, system, task, schema, workspace, run, deny, emit):
            emit('session.finished', sdk_status='completed', session_id='offline-fixture')
            from runner.test_repair_evidence import bind_proposal
            return bind_proposal(run, {'status':'proposed', 'summary':'offline', 'root_cause':'Dockerfile fixture requires repair',
                    'files_changed':[{'path':name,'why':'fixture repair'} for name in ('Dockerfile','.dockerignore')], 'assumptions':[], 'confidence':'high',
                    'files':[{'path':name,'content':'after'} for name in ('Dockerfile','.dockerignore')]}), {}
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve(); workspace=base/'workspace'; workspace.mkdir()
            run, task = base/'run', base/'task.md'; task.write_text('offline task')
            for name in ('Dockerfile','.dockerignore'): (workspace/name).write_text('before')
            (workspace/'Dockerfile').chmod(0o755)
            from runner.test_repair_evidence import case_fixture
            case_fixture(workspace, run)
            original_replace = os.replace
            def fail_second(source, target):
                if Path(target) == workspace/'.dockerignore':
                    raise OSError(errno.ENOSPC, 'private sentinel')
                return original_replace(source, target)
            argv=['runner','fixer','--workspace',str(workspace),'--run',str(run),'--task',str(task)]
            with patch.object(os, 'replace', side_effect=fail_second), patch.object(run_agent, 'run_codex', side_effect=provider), \
                    patch('sys.argv', argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_agent.main(), 1)
            receipt=json.loads((run/'fixer.json').read_text())
            self.assertEqual(receipt['written'], ['Dockerfile'])
            self.assertEqual((workspace/'Dockerfile').read_text(), 'after')
            self.assertEqual((workspace/'.dockerignore').read_text(), 'before')
            self.assertEqual((workspace/'Dockerfile').stat().st_mode & 0o777, 0o755)
            self.assertEqual((receipt['error']['phase'], receipt['error']['retry_policy']), ('patch','after_reconcile'))
            self.assertNotIn('private sentinel', json.dumps(receipt))
            self.assertNotIn('proposal_rejection', receipt)

    def test_validation_only_rejection_has_safe_replan_guidance_without_source_writes(self):
        def provider(cfg, system, task, schema, workspace, run, deny, emit):
            emit('session.finished', sdk_status='completed', session_id='offline-fixture')
            from runner.test_repair_evidence import bind_proposal
            return bind_proposal(run, {'status':'proposed', 'summary':'offline', 'root_cause':'fixture needs repair',
                    'files_changed':[{'path':'../private-canary.py','why':'invalid fixture'}], 'assumptions':[], 'confidence':'high',
                    'files':[{'path':'../private-canary.py','content':'wrong'}]}), {}
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); workspace=base/'work'; workspace.mkdir()
            run, task=base/'run', base/'task.md'; task.write_text('fixture')
            from runner.test_repair_evidence import case_fixture
            (workspace/'app.py').write_text('print(1)\n')
            case_fixture(workspace,run)
            argv=['runner','fixer','--workspace',str(workspace),'--run',str(run),'--task',str(task)]
            with patch.object(run_agent, 'run_codex', side_effect=provider), patch('sys.argv', argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_agent.main(), 1)
            receipt=json.loads((run/'fixer.json').read_text())
            self.assertEqual([], receipt['written'])
            self.assertEqual('none', receipt['error']['side_effect'])
            self.assertEqual('PATH_SCOPE', receipt['proposal_rejection']['reason'])
            self.assertTrue(receipt['proposal_rejection']['safe_to_replan'])
            self.assertNotIn('private-canary', json.dumps(receipt['proposal_rejection']))
            self.assertFalse((base/'private-canary.py').exists())

    def test_structured_evidence_feedback_and_host_drift_never_write_invalid_proposals(self):
        from runner.test_repair_evidence import bind_proposal, case_fixture
        cases = [
            ('valid', {}, None, None),
            ('path', {'path': 'private-canary.py'}, 'EVIDENCE_SOURCE_NOT_FOUND', 'evidence_refs[0].path'),
            ('line', {'line': 999}, 'EVIDENCE_LINE_OUT_OF_RANGE', 'evidence_refs[0].line'),
            ('hash', {'sha256': '0' * 64}, 'EVIDENCE_HASH_MISMATCH', 'evidence_refs[0].sha256'),
            ('binding', {}, 'EVIDENCE_BINDING_MISMATCH', 'addresses_failure'),
            ('log-id', {}, 'EVIDENCE_LOG_NOT_FOUND', 'evidence_refs[0].id'),
            ('log-hash', {}, 'EVIDENCE_HASH_MISMATCH', 'evidence_refs[0].sha256'),
            ('host-drift', {}, None, None),
            ('model-plan', {}, 'PROPOSAL_CONTRACT', None),
        ]
        for name, change, reason, field in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                base = Path(directory).resolve(); workspace = base/'workspace'; workspace.mkdir()
                run, task = base/'run', base/'task.md'; task.write_text('prepare container')
                source = workspace/'Dockerfile'; source.write_text('FROM node:22-alpine\n')
                raw = case_fixture(workspace, run)
                digest = json.loads(raw)['source']['files']['Dockerfile']['sha256']

                def provider(cfg, system, task, schema, workspace, run, deny, emit):
                    emit('session.finished', sdk_status='completed', session_id='offline-fixture')
                    proposal = bind_proposal(run, {
                        'status': 'proposed', 'summary': 'offline', 'confidence': 'high',
                        'root_cause': 'The listener defaults to 0.0.0.0:8080.',
                        'assumptions': ['An endpoint such as https://example.com:443 is prose, not a citation.'],
                        'files_changed': [{'path': 'Dockerfile', 'why': 'fixture repair'}],
                        'files': [{'path': 'Dockerfile', 'content': 'FROM node:22-alpine\nEXPOSE 8080\n'}]})
                    proposal['evidence_refs'] = [{
                        'kind': 'source', 'path': 'Dockerfile', 'line': 1, 'sha256': digest, **change}]
                    if name == 'binding':
                        proposal['addresses_failure'] = 'private-canary'
                    if name in ('log-id', 'log-hash'):
                        proposal['evidence_refs'] = [{'kind': 'log', 'sha256': '0' * 64,
                                                     'id': 'process-999' if name == 'log-id' else 'failure'}]
                    if name == 'host-drift':
                        source.write_text('FROM node:24-alpine\n')
                    if name == 'model-plan':
                        proposal['gate_plan'] = [{'gate': 'L2', 'action': 'skip validation'}]
                    return proposal, {}

                argv = ['runner', 'adapter', '--workspace', str(workspace), '--run', str(run), '--task', str(task)]
                with patch.object(run_agent, 'run_codex', side_effect=provider), patch('sys.argv', argv), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(run_agent.main(), 0 if name == 'valid' else 1)
                receipt = json.loads((run/'adapter.json').read_text())
                if name == 'valid':
                    self.assertEqual(receipt['written'], ['Dockerfile'])
                    plan = json.loads((run/'adapter-plan.json').read_text())
                    self.assertEqual(plan['plan_owner'], 'host')
                    self.assertFalse(plan['execution_verified'])
                    self.assertFalse(plan['evidence']['causal_claim_verified'])
                else:
                    self.assertEqual(receipt['written'], [])
                    self.assertNotIn('EXPOSE', source.read_text())
                    self.assertFalse((run/'adapter-plan.json').exists())
                    if name == 'host-drift':
                        self.assertNotIn('proposal_rejection', receipt)
                    else:
                        rejection = receipt['proposal_rejection']
                        self.assertEqual(rejection['reason'], reason)
                        self.assertEqual(rejection.get('field'), field)
                        self.assertTrue(rejection['safe_to_replan'])
                        self.assertNotIn('private-canary', json.dumps(rejection))

    def test_private_directory_refuses_foreign_owner_and_auth_route_has_no_secret(self):
        from runtime_boundary import private_directory, effective_auth_route
        with tempfile.TemporaryDirectory() as directory, patch.object(os, 'fstat', return_value=SimpleNamespace(
                st_mode=0o40755, st_uid=os.geteuid()+1)), self.assertRaises(PermissionError):
            private_directory(directory)
        route = effective_auth_route('codex', {'CODEX_HOME':'/offline/account',
                    'CODEX_API_KEY':'synthetic-never-log', 'OPENAI_API_KEY':'synthetic-never-log'})
        self.assertEqual(route['mode'], 'subscription')
        self.assertEqual(route['credential_home'], '/offline/account')
        self.assertNotIn('synthetic-never-log', json.dumps(route))


class SDKProgressTest(unittest.TestCase):
    def test_native_stream_progress_is_bounded_typed_and_content_free(self):
        from openai_codex.models import Notification, ThreadTokenUsageUpdatedNotification
        from openai_codex.generated.v2_all import ReasoningTextDeltaNotification
        def stream():
            for index in range(40):
                yield Notification('item/reasoning/textDelta', ReasoningTextDeltaNotification.model_validate({
                    'threadId': 'thread-test', 'turnId': 'turn-test', 'itemId': 'reasoning-test',
                    'contentIndex': 0, 'delta': 'sentinel-private-reasoning'}))
            tokens = {'inputTokens': 100, 'cachedInputTokens': 40, 'outputTokens': 20,
                      'reasoningOutputTokens': 10, 'totalTokens': 120}
            yield Notification('thread/tokenUsage/updated', ThreadTokenUsageUpdatedNotification.model_validate({
                'threadId': 'thread-test', 'turnId': 'turn-test', 'tokenUsage': {'total': tokens, 'last': tokens}}))
            yield Notification('item/reasoning/textDelta', ReasoningTextDeltaNotification.model_validate({
                'threadId': 'other-thread', 'turnId': 'other-turn', 'itemId': 'other-item',
                'contentIndex': 0, 'delta': 'sentinel-private-unrelated'}))
            yield from native_turn(command=('sentinel-private-command-output', 0),
                                   response='sentinel-private-response').stream()
        with tempfile.TemporaryDirectory() as directory, patch.object(run_agent.time, 'monotonic', return_value=10):
            run = Path(directory)
            state, emit = run_agent.lifecycle(run, 'fixer', 'codex', 'gpt-6.1-sol')
            emit('turn.started', sdk_status='running', turn_id='turn-test')
            result = run_agent.collect_codex_turn(SimpleNamespace(id='turn-test', stream=stream), emit=emit)
            self.assertEqual(result.status.value, 'completed')
            events = [json.loads(line) for line in (run/'fixer-events.jsonl').read_text().splitlines()]
            progress_events = [event for event in events if event['event_name'] == 'turn.progress']
            self.assertEqual(2, len(progress_events))  # Burst is coalesced; terminal metadata is flushed.
            progress = state['progress']
            self.assertEqual(44, progress['sdk_event_count'])
            self.assertEqual({'commandExecution': 1, 'agentMessage': 1}, progress['item_counts'])
            self.assertEqual({'kind': 'agentMessage', 'status': 'completed'}, progress['last_item'])
            self.assertEqual(120, progress['token_usage']['total_tokens'])
            self.assertTrue(all(event['outcome'] == 'RUNNING' for event in progress_events))
            self.assertNotIn('sentinel-private', (run/'fixer-events.jsonl').read_text())
            self.assertNotIn('sentinel-private', (run/'fixer-session.json').read_text())

    def test_progress_rejects_unknown_fields_and_invalid_counters(self):
        valid = {'elapsed_ms': 100, 'sdk_event_count': 1, 'last_sdk_event_at_ms': 1000, 'item_counts': {}}
        for mutation in ({'command': 'sentinel-private'}, {'sdk_event_count': True},
                         {'item_counts': {'sentinel-private': 1}}, {'token_usage': {'prompt': 'sentinel-private'}},
                         {'last_item': {'kind': 'reasoning', 'status': 'completed', 'text': 'sentinel-private'}},
                         {'token_usage': {'total_tokens': -1}}):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                run_agent.validated_progress({**valid, **mutation})
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            _, emit = run_agent.lifecycle(run, 'fixer', 'codex', 'gpt-6.1-sol')
            with self.assertRaises(ValueError):
                emit('turn.progress', progress={**valid, 'raw': 'sentinel-private'})
            self.assertEqual('', (run/'fixer-events.jsonl').read_text())


if __name__ == '__main__':
    unittest.main()
