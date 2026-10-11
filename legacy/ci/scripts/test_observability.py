"""Contract tests: evidence semantics and secret-safe causal diagnosis."""
import json
import unittest

from observability import ERRORS, OperationError, event_record


class ObservationTest(unittest.TestCase):
    def test_cause_has_type_and_location_but_no_secret_or_command(self):
        try:
            raise OSError(13, 'secret-token-user-content')
        except OSError as original:
            error = OperationError('STATE_STORAGE_FAILED', component='loop', phase='commit', cause=original)
        record = event_record('run.blocked', component='loop', phase='commit', outcome='BLOCKED', error=error)
        text = json.dumps(record)
        self.assertNotIn('secret-token', text)
        self.assertEqual(record['error']['causes'][0]['errno'], 13)
        self.assertTrue(record['error']['causes'][0]['frames'])
        self.assertEqual(record['error']['retry_policy'], 'never')
        self.assertIsNone(record['run_id'])
        self.assertNotIn('trace_id', record)

    def test_unknown_is_not_failure_or_permission_to_retry(self):
        error = OperationError('STATE_INFLIGHT_UNCERTAIN', component='loop', phase='agent', outcome='UNKNOWN',
                               retry_policy='after_reconcile', side_effect='unknown')
        record = event_record('run.blocked', component='loop', phase='agent', outcome='UNKNOWN', error=error)
        self.assertEqual(record['error']['side_effect'], 'unknown')
        with self.assertRaises(ValueError):
            event_record('run.completed', component='loop', phase='agent', outcome='PASS', error=error)
        with self.assertRaises(ValueError):
            OperationError('MYSTERY', component='loop', phase='agent')

    def test_every_code_has_a_safe_unique_operator_summary(self):
        self.assertEqual(len(ERRORS), len(set(ERRORS.values())))
        for summary in ERRORS.values():
            self.assertNotIn('\n', summary)

    def test_subprocess_error_preserves_cause_without_trusting_free_text(self):
        try:
            raise OSError(28, 'secret-token')
        except OSError as exc:
            value = OperationError('OBSERVATION_WRITE_FAILED', component='runner', phase='observation',
                                   outcome='UNKNOWN', side_effect='unknown', retry_policy='after_reconcile', cause=exc).as_dict()
        value['summary'] = 'secret-token'
        value['causes'][0]['message'] = 'secret-token'
        restored = OperationError.from_dict(value).as_dict()
        self.assertEqual(restored['causes'][0]['errno'], 28)
        self.assertNotIn('secret-token', json.dumps(restored))
        value['outcome'] = 'PASS'
        with self.assertRaises(ValueError):
            event_record('agent.completed', component='runner', phase='agent', outcome='PASS', error=value)

    def test_synthetic_and_non_ascii_locations_survive_subprocess_roundtrip(self):
        for filename in ('<stdin>', '<string>', '<frozen importlib._bootstrap>', '/private/user/앱 이름.py'):
            with self.subTest(filename=filename):
                try:
                    exec(compile("raise OSError(28, 'secret-token')", filename, 'exec'))
                except OSError as exc:
                    value = OperationError('OBSERVATION_WRITE_FAILED', component='runner', phase='observation',
                                           outcome='UNKNOWN', retry_policy='after_reconcile', cause=exc).as_dict()
                self.assertEqual(OperationError.from_dict(value).as_dict(), value)
                self.assertEqual(value['causes'][0]['errno'], 28)
                self.assertNotIn('/private/user', json.dumps(value))
                self.assertNotIn('secret-token', json.dumps(value))


if __name__ == '__main__':
    unittest.main()
