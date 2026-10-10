"""Private, bounded diagnostic evidence. Never changes gate verdicts or invokes a model."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import time

from storage import durable_write

VERSION = 1
MAX_LOG_BYTES = 64 * 1024
MAX_PROCESSES = 64
SECRET = re.compile(
    r'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z]+ )?PRIVATE KEY-----|$)'
    r'|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16}'
    r'|sk-[A-Za-z0-9_-]{16,}|apikey_[A-Za-z0-9_-]{16,}|xox[bpas]-[A-Za-z0-9-]{10,})\b'
    r'|(?i:(?:authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s,;]+)'
    r'|(?i:(?:password|passwd|api[_-]?key|access[_-]?token|secret|token)\s*[=:]\s*["\x27]?[^\s,"\x27;]+)'
    r'|(?i:https?://[^\s/@]+:[^\s/@]+@[^\s]+)'
    r'|(?i:https?://[^\s?]+\?[^\s]+)')
ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|$))')
ERROR_CODE = re.compile(r'\b(?:EAI_AGAIN|ENOTFOUND|ECONNREFUSED|ECONNRESET|ETIMEDOUT|ERESOLVE|ELOCKVERIFY|ENOENT|TS\d{3,6})\b')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def redact(value):
    value = ANSI.sub('', str(value))
    return SECRET.sub('[REDACTED]', re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value))


def bounded(value, limit=MAX_LOG_BYTES):
    data = redact(value).encode()
    if len(data) <= limit:
        return data.decode(), 0
    # Preserve both the initial diagnostic and terminal context, after redaction.
    marker = '\n[OUTPUT OMITTED]\n'
    half = (limit - len(marker.encode())) // 2
    return (data[:half].decode(errors='ignore') + marker
            + data[-half:].decode(errors='ignore')), len(data) - 2 * half


def fingerprint(layer, cls, text):
    lines = [line.strip() for line in redact(text).splitlines() if line.strip()]
    causal = [line for line in lines if not re.fullmatch(r'[^:]+: build failed', line, re.I)]
    first = next((line for line in causal if re.search(r'error|failed|not |invalid|denied|missing|must', line, re.I)),
                 causal[0] if causal else '')
    # Keep compiler codes, versions and source paths. Only ephemeral IDs/timing change.
    normalized = re.sub(r'\b[0-9a-f]{32,}\b|\b\d+(?:\.\d+)?(?:ms|s)\b', '#', first.lower())
    return f'v2:{layer}:{cls}:' + sha(normalized.encode())[:24]


def locations(text, files):
    result = []
    for match in re.finditer(r'(?P<path>[A-Za-z0-9_@./-]+\.(?:tsx?|jsx?|mjs|cjs|py|go|java|rs))(?::|\()(?P<line>\d+)(?:[:,](?P<column>\d+))?', text):
        path = match['path'].removeprefix('./')
        # Never guess an absolute/container/bundled path's source mapping.
        if path not in files or path.startswith('/'):
            continue
        row = {'path': path, 'line': int(match['line']), 'column': int(match['column'] or 1),
               'blob_sha256': files[path]['sha256'], 'mapping_status': 'exact'}
        if 0 < row['line'] <= files[path]['lines'] and row not in result:
            result.append(row)
        if len(result) == 12:
            break
    return result


class Diagnostics:
    def __init__(self, workspace, run, run_id, attempt_id, layers, repair_scope, source_sha256):
        self.workspace, self.run = Path(workspace), Path(run)
        self.run_id, self.attempt_id = run_id, attempt_id
        self.layers, self.repair_scope, self.before = list(layers), repair_scope, source_sha256
        self.processes, self.files, self.missing, self.layer = [], {}, [], None
        self.log_bytes = 0
        self.snapshot = None
        self.binding = None
        self.directory = self.run / 'diagnostics'
        self.directory.mkdir(mode=0o700, exist_ok=True)

    def capture(self):
        from source_snapshot import capture, entries_digest, identity, save
        try:
            entries = capture(self.workspace)
            if entries_digest(entries) != self.before:
                raise ValueError('source changed before diagnostics')
            try:
                self.binding = identity(os.environ)
            except (KeyError, ValueError):
                self.missing.append('github_binding_unavailable')
            value = {'version': 1, **(self.binding or {}), 'source_sha256': self.before, 'entries': entries}
            path = self.run / 'diagnostic-source.json'
            save(value, path)
            self.snapshot = {'path': 'snapshot.json', 'sha256': sha(path.read_bytes()), 'source_sha256': self.before,
                             'purpose': 'diagnostic', 'state': 'available'}
            for entry in entries:
                if entry['type'] == 'f':
                    data = base64.b64decode(entry['content'])
                    self.files[entry['path']] = {'sha256': sha(data), 'bytes': len(data), 'lines': len(data.splitlines())}
        except (OSError, ValueError, TypeError):
            self.missing.append('source_snapshot_unavailable')

    def process(self, cmd, result, started, error=None):
        if len(self.processes) >= MAX_PROCESSES:
            if 'process_limit' not in self.missing:
                self.missing.append('process_limit')
            return
        # Commands, argv, environment and git output may contain private source/secrets.
        command = ('native.dependencies' if cmd == ['native.dependencies'] else
                   'docker.build' if cmd[:3] == ['docker', 'buildx', 'build'] else
                   'docker.logs' if cmd[:2] == ['docker', 'logs'] else
                   'docker.inspect' if cmd[:3] == ['docker', 'inspect', '--format'] else 'process')
        row = {'id': f'process-{len(self.processes) + 1}', 'layer': self.layer, 'command_kind': command,
               'duration_ms': max(0, int((time.monotonic() - started) * 1000)),
               'exit_code': result.returncode if result is not None else None,
               'outcome': 'UNKNOWN' if error else 'PASS' if result.returncode == 0 else 'FAIL',
               'stdout_bytes': len(result.stdout.encode() if isinstance(result.stdout, str) else result.stdout) if result else None,
               'stderr_bytes': len(result.stderr.encode() if isinstance(result.stderr, str) else result.stderr) if result else None}
        if error:
            row['error_code'] = 'PROCESS_TIMEOUT' if error in {'TimeoutExpired', 'TimeoutError'} else 'PROCESS_START_FAILED' if error == 'FileNotFoundError' else 'PROCESS_FAILED'
        if result is not None and command in {'docker.build', 'docker.logs', 'native.dependencies'} and self.log_bytes < 512 * 1024:
            stdout = str(result.stdout)
            if command == 'native.dependencies':
                stdout = '\n'.join(line for line in stdout.splitlines() if not line.startswith('RAILSHOT_NATIVE_LOCK='))
            output, omitted = bounded(stdout + '\n' + str(result.stderr), min(MAX_LOG_BYTES, 512 * 1024 - self.log_bytes))
            self.log_bytes += len(output.encode())
            path = self.directory / (row['id'] + '.log')
            durable_write(path, output.encode())
            row['log'] = {'path': path.name, 'sha256': sha(output.encode()), 'redaction_version': VERSION,
                          'capture_complete': omitted == 0, 'omitted_bytes': omitted}
        self.processes.append(row)

    def finish(self, verdict):
        failure = verdict.get('failure') or {}
        excerpt, omitted = bounded(failure.get('excerpt', ''), 16000)
        case_id = sha(json.dumps([self.run_id, self.attempt_id, self.before], separators=(',', ':')).encode())
        checks = [{'check_id': row['layer'], 'outcome': row['outcome'], 'required': not row.get('advisory', False),
                   'duration_ms': round(row.get('duration_s', 0) * 1000)} for row in verdict['layers']]
        for layer in self.layers:
            if layer not in {row['check_id'] for row in checks}:
                checks.append({'check_id': layer, 'outcome': 'NOT_RUN', 'required': True, 'reason': 'prior_check_stopped'})
        for layer in ('Q', 'L4'):
            if layer not in self.layers:
                checks.append({'check_id': layer, 'outcome': 'NOT_RUN', 'required': False, 'reason': 'profile_disabled'})
        if self.before != verdict.get('source_sha256'):
            self.missing.append('source_changed_during_checks')
        code = ERROR_CODE.search(excerpt)
        authority = 'platform' if verdict.get('status') in {'BLOCKED', 'UNKNOWN'} else 'application'
        if code and code[0] in {'EAI_AGAIN', 'ENOTFOUND', 'ECONNRESET', 'ETIMEDOUT'}:
            authority = 'environment'
        policy = {'gate_order': self.layers, 'repair_scope': self.repair_scope,
                  'max_files': 8, 'max_full_content_bytes': 20000, 'allow_publish': False,
                  'protected_policy_sha256': sha((Path(__file__).parent / 'contract/paths.yaml').read_bytes())}
        detail = verdict.get('error')
        value = {'version': VERSION, 'purpose': 'diagnostic', 'case_id': case_id, 'binding': self.binding,
                 'error': {k: v for k, v in detail.items() if k != 'summary'} if detail else None,
                 'native_run_id': self.run_id, 'attempt_id': self.attempt_id,
                 'source': {'tested_sha256': self.before, 'after_sha256': verdict.get('source_sha256'),
                            'snapshot': self.snapshot, 'files': self.files},
                 'failure': {'layer': failure.get('layer'), 'class': failure.get('class'),
                             'fingerprint': failure.get('signature'), 'code': code[0] if code else None,
                             'excerpt': excerpt, 'excerpt_omitted_bytes': omitted,
                             'locations': locations(excerpt, self.files)},
                 'checks': checks, 'processes': self.processes, 'missing_evidence': sorted(set(self.missing)),
                 'policy': policy, 'policy_sha256': sha(json.dumps(policy, sort_keys=True, separators=(',', ':')).encode()),
                 'classification': {'source': 'rules', 'revision': 1, 'responsibility': authority,
                                    'is_hypothesis': True, 'grants_write_authority': False},
                 'verification': {'release_eligible': verdict['release_eligible'],
                                  'gate_outcome': verdict['status'], 'target_check_passed': 'NOT_RUN',
                                  'regression_verified': 'NOT_RUN', 'runtime_recovered': 'NOT_RUN'}}
        durable_write(self.directory / 'case.json', json.dumps(value, ensure_ascii=True, separators=(',', ':')).encode())
        return {'case_id': case_id, 'state': 'available', 'missing_evidence': value['missing_evidence']}
