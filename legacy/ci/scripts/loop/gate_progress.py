"""Relay trusted gate journal rows; child stdout is never a public event producer."""
import json
import os
import time

from checks_progress import row


def observer(path, run_id, attempt_id, sink):
    offset, next_at = 0, 0

    def read(*, final=False):
        nonlocal offset, next_at
        if sink is None or time.monotonic() < next_at and not final:
            return
        next_at = time.monotonic() + 1
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
                if os.fstat(stream.fileno()).st_size > 2 * 1024 * 1024:
                    return
                stream.seek(offset)
                for line in stream:
                    if not line.endswith(b'\n'):
                        break
                    offset += len(line)
                    event = json.loads(line)
                    if (event.get('run_id') != run_id or event.get('attempt_id') != attempt_id
                            or not event.get('event_name', '').startswith('gate.layer.')):
                        continue
                    row(event)  # Enforce the same content-free projection before transport.
                    sink.emit(event)
        except (OSError, ValueError, TypeError, KeyError):
            return  # Remote telemetry is best effort; local gate verdict remains authoritative.
    return read
