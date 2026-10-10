"""Versioned local observation contract; stdlib only, no exporter or workflow engine.

Public errors contain registered summaries and safe causal locations, never exception
messages, locals, command arguments, source lines or SDK/tool output. See contract/observability.md.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import traceback
import uuid

CONTRACT = json.loads((Path(__file__).parent / 'contract/telemetry.json').read_text())
SCHEMA_VERSION = CONTRACT['version']
OUTCOMES = set(CONTRACT['outcomes'])
RETRY_POLICIES = {'never', 'after_reconcile', 'after_configuration', 'safe'}
SIDE_EFFECTS = {'none', 'possible', 'completed', 'unknown'}
# Codes are the machine contract. Summaries are safe operator-facing text, not predicates.
ERRORS = {
    'INTAKE_REJECTED': 'Upload did not satisfy the intake policy.',
    'STATE_USAGE_INVALID': 'Existing state and requested initialization/resume mode do not agree.',
    'STATE_BINDING_MISMATCH': 'Source, configuration, request or harness differs from the recorded run.',
    'STATE_INFLIGHT_UNCERTAIN': 'A previously started step has no durable completion; reconcile before retry.',
    'STATE_EVIDENCE_MISMATCH': 'A recorded workspace, checkpoint or evidence artifact is missing or changed.',
    'STATE_WRITER_CONFLICT': 'Another process owns this run; do not start a second writer.',
    'STATE_STORAGE_FAILED': 'Durable state could not be read or committed; stop side effects.',
    'STEP_TIMEOUT': 'The step timed out; its external outcome has not been reconciled.',
    'STEP_START_FAILED': 'The step process could not be started.',
    'STEP_OUTPUT_INVALID': 'The step did not return a valid result contract.',
    'SDK_CONFIG_INVALID': 'SDK version or authentication configuration is unsupported or incomplete.',
    'SDK_POLICY_DENIED': 'SDK access was rejected by the platform boundary.',
    'SDK_SANDBOX_UNAVAILABLE': 'The runner sandbox cannot enforce the required access boundary; repair its configuration before retrying.',
    'SDK_RESUME_UNSUPPORTED': 'Native conversation resume is not supported by this adapter.',
    'SDK_OUTCOME_UNKNOWN': 'The model operation outcome is unknown; do not automatically invoke it again.',
    'SDK_EXECUTION_FAILED': 'The SDK reported an unsuccessful model operation.',
    'SDK_OUTPUT_INVALID': 'The completed model response does not satisfy the required output schema.',
    'SDK_PATCH_REJECTED': 'The proposed patch violates the current write policy.',
    'OBSERVATION_WRITE_FAILED': 'Required execution evidence could not be stored; stop progression.',
    'GATE_CONFIG_INVALID': 'The requested gate configuration is invalid.',
    'GATE_EXECUTION_FAILED': 'A required check could not complete; it is not a check pass.',
    'GATE_CHECK_FAILED': 'An executed deterministic check reported failure.',
    'GATE_EVIDENCE_MISMATCH': 'Source or image identity differs from the gate evidence.',
    'GATE_ENVIRONMENT_UNAVAILABLE': 'Required tools, isolation, credentials or environment are unavailable.',
    'CD_CONFIG_INVALID': 'Trusted deployment bindings or image artifact are invalid.',
    'PUBLISH_OUTCOME_UNKNOWN': 'Image publication is incomplete or uncertain; reconcile the durable per-image receipt before retry.',
    'INFRA_CONFIG_INVALID': 'Administrator infrastructure inputs do not satisfy the registered contract.',
    'INFRA_CAPABILITY_UNSUPPORTED': 'The registered target does not support this requested operation.',
    'INFRA_BUDGET_BLOCKED': 'A current cost reservation could not be obtained; no infrastructure dispatch is allowed.',
    'INFRA_MAINTENANCE_REQUIRED': 'A current drain and data preservation observation is required for this operation.',
    'INFRA_EXECUTION_FAILED': 'Infrastructure command failed; reconcile its private evidence and actual resources.',
    'INTERNAL_ERROR': 'An unexpected platform error occurred; use causal locations and the run identity to investigate.',
}


def causal_label(value, limit=128):
    """Bounded diagnostic identifiers, shared by producers and receipt validation."""
    return re.sub(r'[^A-Za-z0-9_.<>-]', '_', value)[:limit]


def cause_details(exc):
    """Preserve causal type/location without serializing potentially secret exception text."""
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen and len(chain) < 4:
        seen.add(id(exc))
        item = {'type': causal_label(type(exc).__module__ + '.' + type(exc).__qualname__, 180)}
        for key in ('errno', 'sqlite_errorcode', 'returncode'):
            value = getattr(exc, key, None)
            if isinstance(value, int):
                item[key] = value
        if exc.__traceback__:
            # No absolute user path, source line or local variable is included.
            item['frames'] = [{'file': causal_label(f.filename.replace('\\', '/').rsplit('/', 1)[-1]),
                               'function': causal_label(f.name), 'line': f.lineno}
                              for f in traceback.extract_tb(exc.__traceback__)[-6:]]
        chain.append(item)
        if isinstance(exc, OperationError) and exc.__cause__ is None and exc._reported_causes:
            chain.extend(exc._reported_causes[:4 - len(chain)])
            break
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return chain


class OperationError(RuntimeError):
    """A classified failure with an explicit recovery and side-effect boundary."""
    def __init__(self, code, *, component, phase, outcome='BLOCKED', retry_policy='never',
                 side_effect='none', cause=None):
        if code not in ERRORS or outcome not in {'FAIL', 'BLOCKED', 'UNKNOWN'} or retry_policy not in RETRY_POLICIES or side_effect not in SIDE_EFFECTS:
            raise ValueError('unregistered observation error contract')
        self.code, self.component, self.phase = code, component, phase
        self.outcome, self.retry_policy, self.side_effect = outcome, retry_policy, side_effect
        self._reported_causes = []
        self.__cause__ = cause
        super().__init__(ERRORS[code])

    def as_dict(self):
        return {'code': self.code, 'summary': ERRORS[self.code], 'component': self.component,
                'phase': self.phase, 'outcome': self.outcome, 'retry_policy': self.retry_policy,
                'side_effect': self.side_effect, 'causes': cause_details(self.__cause__) or self._reported_causes}

    @classmethod
    def from_dict(cls, value):
        """Rehydrate a trusted subprocess error without dynamic exception loading or free text."""
        result = cls(value['code'], component=value['component'], phase=value['phase'],
                     outcome=value['outcome'], retry_policy=value['retry_policy'], side_effect=value['side_effect'])
        for field in (result.component, result.phase):
            if not isinstance(field, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}', field):
                raise ValueError('invalid error scope')
        chain = value.get('causes', [])
        if not isinstance(chain, list) or len(chain) > 4:
            raise ValueError('invalid error cause chain')
        for item in chain:
            if not isinstance(item['type'], str) or not item['type'] or item['type'] != causal_label(item['type'], 180):
                raise ValueError('invalid cause type')
            safe = {'type': item['type']}
            for key in ('errno', 'sqlite_errorcode', 'returncode'):
                if key in item:
                    if type(item[key]) is not int:
                        raise ValueError('invalid cause numeric field')
                    safe[key] = item[key]
            frames = item.get('frames', [])
            if not isinstance(frames, list) or len(frames) > 6:
                raise ValueError('invalid causal locations')
            if frames:
                safe['frames'] = []
                for frame in frames:
                    if (any(not isinstance(frame[key], str) or not frame[key]
                            or frame[key] != causal_label(frame[key]) for key in ('file', 'function'))
                            or type(frame['line']) is not int or frame['line'] < 1):
                        raise ValueError('invalid causal location')
                    safe['frames'].append({key: frame[key] for key in ('file', 'function', 'line')})
            result._reported_causes.append(safe)
        return result


def event_record(name, *, component, phase, outcome, run_id=None, attempt_id=None, error=None, attributes=None):
    """Build an honest local observation. run_id is not a fabricated OTel TraceId."""
    if not re.fullmatch(r'[a-z][a-z0-9_.]*', name) or outcome not in OUTCOMES:
        raise ValueError('invalid event name or outcome')
    detail = error.as_dict() if isinstance(error, OperationError) else OperationError.from_dict(error).as_dict() if error else None
    if detail and detail.get('outcome') != outcome:
        raise ValueError('event and error outcomes disagree')
    now = datetime.now(timezone.utc).isoformat()
    severity = 'ERROR' if outcome in ('FAIL', 'UNKNOWN') else 'WARN' if outcome == 'BLOCKED' else 'INFO'
    return {'schema_version': SCHEMA_VERSION, 'event_id': str(uuid.uuid4()), 'event_name': name,
            'occurred_at': now, 'observed_at': now, 'component': component, 'phase': phase,
            'outcome': outcome, 'severity': severity, 'severity_number': CONTRACT['severity_numbers'][severity],
            'run_id': run_id, 'attempt_id': attempt_id, 'error': detail, 'attributes': attributes or {}}
