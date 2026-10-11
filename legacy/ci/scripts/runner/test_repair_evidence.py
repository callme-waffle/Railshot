import copy
import json
import subprocess
import time
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runner import repair_evidence
from diagnostics import Diagnostics
from source_snapshot import capture, entries_digest
from execution import GATE_ORDER, RELEASE_ORDERS


def case_fixture(ws, run, layers=GATE_ORDER, scope='packaging'):
    run.mkdir(exist_ok=True)
    source = entries_digest(capture(ws))
    d = Diagnostics(ws, run, 'fixture-run', 'fixture-run:0', layers, scope, source)
    d.capture()
    d.finish({'source_sha256': source, 'status': 'FAIL', 'release_eligible': False,
              'layers': [{'layer': 'L2', 'outcome': 'FAIL'}],
              'failure': {'layer': 'L2', 'class': 'F1', 'signature': 'failure-fixture', 'excerpt': 'Dependency install failed'}})
    return repair_evidence.load(run)


def bind_proposal(run, output):
    data = repair_evidence.load(run); case = json.loads(data)
    return {**output, 'addresses_failure': case['failure']['fingerprint'],
            'evidence_binding': {'case_id': case['case_id'], 'case_sha256': repair_evidence.sha(data),
               'source_sha256': case['source']['tested_sha256'], 'policy_sha256': case['policy_sha256']},
            'evidence_refs': [{'kind': 'log', 'id': 'failure', 'sha256': repair_evidence.sha(case['failure']['excerpt'].encode())}]}


class RepairEvidenceTest(unittest.TestCase):
    def test_real_source_and_case_required_before_any_proposal_write(self):
        with tempfile.TemporaryDirectory() as directory:
            ws, run = Path(directory)/'work', Path(directory)/'run'; ws.mkdir()
            (ws/'app.py').write_text('print(1)\n')
            raw = case_fixture(ws, run)
            output = bind_proposal(run, {'files': [{'path':'app.py', 'content':'print(2)\n'}],
                'root_cause':'app.py:1 prints a constant', 'assumptions':[]})
            check = lambda proposal: repair_evidence.verify(run, ws, proposal, GATE_ORDER, 'packaging', raw)
            self.assertFalse(check(output)['causal_claim_verified'])
            for key, value in [('addresses_failure','unrelated-case-signature'), ('evidence_refs',[]),
                               ('evidence_binding', {**output['evidence_binding'], 'source_sha256':'0'*64}),
                               ('evidence_refs',[{'kind':'source','path':'app.py','line':1,'sha256':'0'*64}])]:
                with self.subTest(key=key), self.assertRaises(ValueError): check({**output,key:value})
            (ws/'app.py').write_text('print(3)\n')
            with self.assertRaises(ValueError): check(output)
            (ws/'app.py').write_text('print(1)\n')
            (run/'diagnostics/case.json').write_bytes(raw+b' ')
            with self.assertRaises(ValueError): check(output)
            self.assertEqual((ws/'app.py').read_text(),'print(1)\n')

    def test_prose_is_not_a_reference_but_typed_source_refs_are_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            ws, run = Path(directory)/'work', Path(directory)/'run'; ws.mkdir()
            (ws/'app.py').write_text('print(1)\n')
            raw = case_fixture(ws, run)
            output = bind_proposal(run, {'files': [{'path': 'app.py', 'content': 'print(2)\n'}],
                'root_cause': 'The listener defaults to 0.0.0.0:8080.',
                'assumptions': ['localhost.localdomain:8080 and https://example.com:443 are endpoints.',
                                'nonexistent.py:999 is an unverified model hypothesis.']})
            source = json.loads(raw)['source']['files']['app.py']
            ref = {'kind': 'source', 'path': 'app.py', 'line': 1, 'sha256': source['sha256']}
            output['evidence_refs'].append(ref)
            check = lambda value: repair_evidence.verify(run, ws, value, GATE_ORDER, 'packaging', raw)
            self.assertTrue(check(output)['reference_integrity_verified'])
            self.assertFalse(check(output)['causal_claim_verified'])
            for change in ({'path': 'nonexistent.py'}, {'line': 999}, {'sha256': '0'*64}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    check({**output, 'evidence_refs': [{**ref, **change}]})

    def test_initial_context_is_bounded_and_retains_original_reference_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            ws, run = Path(directory)/'work', Path(directory)/'run'; ws.mkdir()
            (ws/'app.py').write_text('print(1)\n')
            case = json.loads(case_fixture(ws, run))
            case['failure']['excerpt'] = '실패 TS2322 ' * 2000
            case['source']['files']['irrelevant.txt'] = {'sha256': 'a'*64, 'bytes': 999, 'lines': 5}
            raw = json.dumps(case).encode()
            context = repair_evidence.context(raw)
            self.assertLessEqual(len(context['failure']['excerpt'].encode()), repair_evidence.FAILURE_BYTES)
            self.assertNotIn('content', context['source_candidates'][0])
            self.assertEqual(context['failed_gate'], 'L2')
            self.assertEqual(context['stage'], 'image.build')
            self.assertIn('Dockerfile', context['investigation'][0])
            self.assertNotEqual(repair_evidence.context(raw, role='adapter')['investigation'], context['investigation'])
            self.assertNotIn('replay', context)
            self.assertEqual(context['evidence_binding']['case_sha256'], repair_evidence.sha(raw))
            self.assertEqual(context['failure_reference']['sha256'], repair_evidence.sha(case['failure']['excerpt'].encode()))
            self.assertGreater(context['failure']['excerpt_omitted_bytes'], 0)

    def test_context_prioritizes_observed_paths_without_reading_source_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            ws, run = Path(directory)/'work', Path(directory)/'run'; ws.mkdir()
            (ws/'package.json').write_text('{"scripts":{"start":"node app.js"}}')
            (ws/'app.js').write_text('private source body not injected\n')
            raw = case_fixture(ws, run)
            case = json.loads(raw)
            for i in range(100):
                case['source']['files'][f'asset-{i}.txt'] = {'sha256': 'a'*64, 'bytes': 1, 'lines': 1}
            case['failure']['locations'] = [{'path': 'app.js', 'line': 1, 'column': 1,
                'blob_sha256': case['source']['files']['app.js']['sha256'], 'mapping_status': 'exact'}]
            packet = repair_evidence.context(json.dumps(case).encode(), run, 'adapter')
            self.assertEqual(packet['task'], 'prepare_container')
            self.assertEqual([p['path'] for p in packet['source_candidates'][:2]], ['app.js', 'package.json'])
            self.assertEqual(len(packet['source_candidates']), repair_evidence.MAX_PATHS)
            self.assertEqual(packet['source_paths_omitted'], 78)
            self.assertNotIn('private source body', json.dumps(packet))

    def test_context_selects_failed_stage_logs_and_verifies_their_original_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            ws, run = Path(directory)/'work', Path(directory)/'run'; ws.mkdir(); run.mkdir()
            (ws/'app.py').write_text('print(1)\n')
            source = entries_digest(capture(ws))
            d = Diagnostics(ws, run, 'run', 'run:1', GATE_ORDER, 'packaging', source); d.capture()
            for layer, code, text in [('L2', 0, 'build passed'), ('L3', 0, 'server failed\n'*1000)]:
                d.layer = layer
                d.process(['docker', 'logs'], subprocess.CompletedProcess([], code, text, ''), time.monotonic())
            d.finish({'source_sha256': source, 'status': 'FAIL', 'release_eligible': False,
                'layers': [{'layer': 'L2', 'outcome': 'PASS'}, {'layer': 'L3', 'outcome': 'FAIL'}],
                'failure': {'layer': 'L3', 'class': 'F4', 'signature': 'runtime', 'excerpt': 'health failed'}})
            raw = repair_evidence.load(run)
            packet = repair_evidence.context(raw, run)
            self.assertEqual([p['id'] for p in packet['logs']], ['process-2'])
            log = packet['logs'][0]
            self.assertLessEqual(len(log['text'].encode()), repair_evidence.LOG_BYTES)
            self.assertGreater(log['omitted_bytes'], 0)
            self.assertEqual(log['reference']['sha256'], repair_evidence.sha((run/'diagnostics/process-2.log').read_bytes()))
            (run/'diagnostics/process-2.log').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'log changed'):
                repair_evidence.context(raw, run)

    def test_history_separates_model_hypotheses_from_host_outcomes(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            for i in range(1, 5):
                (run/f'fixer-{i}.json').write_text(json.dumps({
                    'written': ['Dockerfile'], 'output': {'root_cause': 'Maybe a port issue',
                    'assumptions': ['UNVERIFIED'], 'files': [{'content': 'do not replay'}]}}))
                gate = run/f'gate-{i}'; gate.mkdir()
                (gate/'verdict.json').write_text(json.dumps({'status': 'FAIL', 'failure': {'layer': 'L3'}}))
            history = repair_evidence.previous_attempts(run)
            self.assertEqual([row['attempt'] for row in history], [2, 3, 4])
            self.assertEqual(history[-1]['host_gate_result'], {'status': 'FAIL', 'failure_layer': 'L3'})
            self.assertEqual(history[-1]['model_hypothesis_unverified'], 'Maybe a port issue')
            self.assertNotIn('do not replay', json.dumps(history))
            (run/'fixer-4.json').unlink(); (run/'fixer-4.json').symlink_to(run/'fixer-3.json')
            with self.assertRaisesRegex(ValueError, 'history invalid'):
                repair_evidence.previous_attempts(run)

    def test_all_profiles_bind_exact_plan_and_policy(self):
        from runner.run_agent import record_plan, with_files
        for order in RELEASE_ORDERS:
            with self.subTest(order=order), tempfile.TemporaryDirectory() as directory:
                ws, run = Path(directory)/'work', Path(directory)/'run'; ws.mkdir(); (ws/'app.py').write_text('print(1)\n')
                raw = case_fixture(ws, run, order)
                output = bind_proposal(run, {'status':'proposed', 'root_cause':'app.py:1',
                    'files':[{'path':'app.py','content':'print(2)\n'}], 'files_changed':[{'path':'app.py','why':'fixture'}]})
                record_plan(run,'fixer',output,order,workspace=ws,expected=raw)
                receipt = json.loads((run/'fixer-plan.json').read_text())
                self.assertTrue(receipt['evidence']['reference_integrity_verified'])
                self.assertEqual(receipt['plan_owner'], 'host')
                self.assertEqual([step['gate'] for step in receipt['gate_plan']], list(order))
                self.assertFalse(receipt['execution_verified'])
                schema=with_files(json.loads((Path(__file__).resolve().parents[1]/'schemas/report.schema.json').read_text()), ['**'], order)
                self.assertNotIn('gate_plan', schema['properties'])
                with self.assertRaises(ValueError): repair_evidence.verify(run,ws,output,order,'source',raw)

if __name__ == '__main__': unittest.main()
