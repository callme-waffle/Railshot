"""Local single-writer checkpoints; SQLite state/events commit together, no remote scheduler."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from observability import OperationError as StateError, event_record
from runner.runtime_boundary import private_directory


def state_error(code, phase, **kwargs):
    return StateError(code, component='loop.state', phase=phase, **kwargs)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tree_digest(root, exclude_git=True):
    """Fingerprint bytes, modes and symlink targets without following uploaded symlinks."""
    root = Path(root)
    if not root.exists():
        return None
    h = hashlib.sha256()
    for p in sorted(root.rglob('*')):
        if (exclude_git and '.git' in p.relative_to(root).parts) or '__pycache__' in p.parts:
            continue
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            value = ['link', rel, os.readlink(p)]
        elif p.is_file():
            value = ['file', rel, p.stat().st_mode & 0o777, digest(p)]
        else:
            continue
        h.update(json.dumps(value, separators=(',', ':')).encode())
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.tmp')
    with tmp.open('w') as f:
        os.chmod(tmp, 0o600)
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class RunState:
    def __init__(self, run, binding, resume=False):
        self.run, self.db, self.lock = Path(run), None, None
        try:
            self.run = private_directory(self.run)
            self.lock = (self.run / '.writer.lock').open('a')
            os.chmod(self.run / '.writer.lock', 0o600)
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise state_error('STATE_WRITER_CONFLICT', 'lock', retry_policy='safe', cause=exc) from exc
            path = self.run / 'state.sqlite3'
            exists = path.exists()
            if exists != resume or (not exists and set(p.name for p in self.run.iterdir()) != {'.writer.lock'}):
                raise state_error('STATE_USAGE_INVALID', 'open', retry_policy='after_configuration')
            self.db = sqlite3.connect(path)
            os.chmod(path, 0o600)
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
            self.db.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL)')
            for action in ('UPDATE', 'DELETE'):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS events_no_{action.lower()} BEFORE {action} ON events BEGIN SELECT RAISE(ABORT, 'append-only events'); END")
            if exists:
                row = self.db.execute('SELECT body FROM state WHERE id=1').fetchone()
                if row is None:
                    raise state_error('STATE_STORAGE_FAILED', 'resume', retry_policy='after_reconcile')
                self.data = json.loads(row[0])
                if self.data['binding'] != binding:
                    raise state_error('STATE_BINDING_MISMATCH', 'resume', retry_policy='after_configuration')
                if self.data.get('inflight'):
                    error = state_error('STATE_INFLIGHT_UNCERTAIN', self.data['inflight'], outcome='UNKNOWN',
                                        retry_policy='after_reconcile', side_effect='unknown')
                    self.save('run.resume_blocked', self.data['inflight'], outcome=error.outcome, error=error)
                    raise error
                if self.data.get('workspace_sha256') != tree_digest(self.run / 'work', exclude_git=False):
                    raise state_error('STATE_EVIDENCE_MISMATCH', 'resume.workspace', retry_policy='after_reconcile')
                lessons = self.run / 'lessons.md'
                if self.data.get('lessons_sha256') != (digest(lessons) if lessons.exists() else None):
                    raise state_error('STATE_EVIDENCE_MISMATCH', 'resume.lessons', retry_policy='after_reconcile')
                failure = self.run / 'failure.txt'
                if self.data.get('failure_sha256') != (digest(failure) if failure.exists() else None):
                    raise state_error('STATE_EVIDENCE_MISMATCH', 'resume.failure', retry_policy='after_reconcile')
                for entry in self.data['steps'].values():
                    if digest(self.run / entry['ref']) != entry['sha256']:
                        raise state_error('STATE_EVIDENCE_MISMATCH', 'resume.checkpoint', retry_policy='after_reconcile')
                    for ref, sha in entry.get('artifacts', {}).items():
                        if digest(self.run / ref) != sha:
                            raise state_error('STATE_EVIDENCE_MISMATCH', 'resume.artifact', retry_policy='after_reconcile')
                self.save('run.resumed')
            else:
                self.data = {'version': 1, 'run_id': str(uuid.uuid4()), 'started': time.time(),
                             'binding': binding, 'steps': {}, 'inflight': None,
                             'workspace_sha256': None, 'lessons_sha256': None}
                self.save('run.started')
        except (sqlite3.Error, OSError) as exc:
            self.close()
            code = 'STATE_EVIDENCE_MISMATCH' if isinstance(exc, FileNotFoundError) else 'STATE_STORAGE_FAILED'
            raise state_error(code, 'open', retry_policy='after_reconcile', cause=exc) from exc
        except (ValueError, KeyError, TypeError) as exc:
            self.close()
            raise state_error('STATE_EVIDENCE_MISMATCH', 'resume', retry_policy='after_reconcile', cause=exc) from exc
        except BaseException:
            self.close()
            raise

    def save(self, kind, step=None, *, outcome='RUNNING', error=None, **fields):
        attempt_id = self.data['run_id'] + ':' + step.rsplit(':', 1)[-1] if step and ':' in step else None
        event = event_record(kind, component='loop.state', phase=step or 'run', outcome=outcome,
                             run_id=self.data['run_id'], attempt_id=attempt_id, error=error, attributes=fields)
        try:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO state VALUES (1, ?)', (json.dumps(self.data),))
                self.db.execute('INSERT INTO events(body) VALUES (?)', (json.dumps(event),))
        except sqlite3.Error as exc:
            raise state_error('STATE_STORAGE_FAILED', 'commit', outcome='UNKNOWN', retry_policy='after_reconcile',
                              side_effect='unknown', cause=exc) from exc

    def step(self, name, function, artifacts=(), optional_artifacts=()):
        """Only durable completion is reusable; uncertain effects retain the in-flight marker."""
        if name in self.data['steps']:
            try:
                return json.loads((self.run / self.data['steps'][name]['ref']).read_text())
            except (OSError, ValueError) as exc:
                raise state_error('STATE_EVIDENCE_MISMATCH', name, retry_policy='after_reconcile', cause=exc) from exc
        self.data['inflight'] = name
        self.save('step.started', name)
        try:
            result = function()
        except StateError as exc:
            if exc.side_effect == 'none' and exc.retry_policy == 'safe':
                self.data['inflight'] = None
            self.save('step.interrupted', name, outcome=exc.outcome, error=exc)
            raise
        except Exception as exc:
            error = state_error('INTERNAL_ERROR', name, outcome='UNKNOWN', retry_policy='after_reconcile',
                                side_effect='unknown', cause=exc)
            self.save('step.interrupted', name, outcome='UNKNOWN', error=error)
            raise error from exc
        try:
            required = artifacts(result) if callable(artifacts) else artifacts
            # A successful producer must leave every required artifact. Optional diagnostics
            # can legitimately be absent, e.g. failure.txt after a successful gate.
            refs = {a: digest(self.run / a) for a in required}
            refs.update({a: digest(self.run / a) for a in optional_artifacts if (self.run / a).is_file()})
            ref = 'checkpoints/' + name.replace(':', '-') + '.json'
            atomic_json(self.run / ref, result)
            self.data['steps'][name] = {'ref': ref, 'sha256': digest(self.run / ref),
                                       'artifacts': refs}
            self.data['workspace_sha256'] = tree_digest(self.run / 'work', exclude_git=False)
            lessons = self.run / 'lessons.md'
            self.data['lessons_sha256'] = digest(lessons) if lessons.exists() else None
            failure = self.run / 'failure.txt'
            self.data['failure_sha256'] = digest(failure) if failure.exists() else None
            self.data['inflight'] = None
            # Persistence completion is not a gate pass; only final gate verdicts can promote.
            self.save('step.checkpointed', name, artifact=ref, sha256=self.data['steps'][name]['sha256'])
        except (OSError, TypeError, ValueError) as exc:
            raise state_error('STATE_STORAGE_FAILED', name, outcome='UNKNOWN', retry_policy='after_reconcile',
                              side_effect='possible', cause=exc) from exc
        return result

    def complete(self, evidence):
        ref = 'checkpoints/final.json'
        try:
            atomic_json(self.run / ref, evidence)
            sha = digest(self.run / ref)
        except (OSError, TypeError, ValueError) as exc:
            raise state_error('STATE_STORAGE_FAILED', 'complete', retry_policy='after_reconcile',
                              side_effect='completed', cause=exc) from exc
        self.data['steps']['final'] = {'ref': ref, 'sha256': sha}
        self.data['final'] = ref
        self.save('run.completed', outcome=evidence.get('status', 'PASS' if evidence['passed'] else 'FAIL'),
                  error=evidence.get('error'), passed=evidence['passed'])

    def close(self):
        if self.db is not None:
            self.db.close()
        if self.lock is not None:
            self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
