"""Private gate-supervisor observations. Program stdout never produces these events."""
import json
import os
import time

from observability import OperationError, event_record
from storage import durable_write


class Progress:
    def __init__(self, run, run_id, attempt_id, layers):
        self.path = run / 'progress.jsonl'
        self.run_id, self.attempt_id, self.layers = run_id, attempt_id, list(layers)
        self.sequence, self.completed, self.started = 0, 0, {}
        self.active, self.next_tick = None, 0
        try:
            if self.path.exists() or self.path.is_symlink():
                raise FileExistsError('gate progress already exists')
            durable_write(self.path, b'')
        except OSError as exc:
            raise self.failure(exc) from exc

    @staticmethod
    def failure(exc):
        return OperationError('OBSERVATION_WRITE_FAILED', component='gate', phase='progress',
                              outcome='UNKNOWN', retry_policy='after_reconcile', side_effect='possible', cause=exc)

    def append(self, event):
        self.sequence += 1
        event['attributes'].update(sequence=self.sequence, completed_steps=self.completed,
                                   total_steps=len(self.layers), observation_source='gate-supervisor')
        try:
            with os.fdopen(os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW), 'ab') as stream:
                stream.write(json.dumps(event, sort_keys=True).encode() + b'\n')
                stream.flush(); os.fsync(stream.fileno())
        except OSError as exc:
            raise self.failure(exc) from exc

    def start(self, layer):
        event = event_record('gate.layer.started', component='gate', phase=layer, outcome='RUNNING',
                             run_id=self.run_id, attempt_id=self.attempt_id)
        self.active, self.next_tick = layer, time.monotonic() + 20
        self.started[layer] = (event['occurred_at'], time.monotonic())
        event['attributes']['started_at'] = event['occurred_at']
        self.append(event)

    def tick(self):
        if self.active is None or time.monotonic() < self.next_tick:
            return
        self.next_tick = time.monotonic() + 20
        self.append(event_record('gate.layer.heartbeat', component='gate', phase=self.active, outcome='RUNNING',
                                run_id=self.run_id, attempt_id=self.attempt_id,
                                attributes={'duration_s': round(time.monotonic() - self.started[self.active][1], 3)}))

    def complete(self, result):
        started_at, tick = self.started[result['layer']]
        result.update(started_at=started_at, duration_s=round(time.monotonic() - tick, 3))
        result['event']['attributes'].update(started_at=started_at, duration_s=result['duration_s'])
        self.active = None
        self.completed += 1
        self.append(result['event'])
