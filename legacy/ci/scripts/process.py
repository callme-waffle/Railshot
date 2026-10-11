"""Bounded subprocess capture and process-group cleanup for Linux/macOS workers."""
import os
import selectors
import signal
import subprocess
import time


class OutputLimitError(RuntimeError):
    """Execution exceeded the capture budget; raw output is intentionally omitted."""
    def __init__(self, limit):
        self.limit = limit
        super().__init__("subprocess output limit exceeded")


class ProcessCleanupError(OSError):
    """The child started but its process group could not be confirmed stopped."""


def stop_group(proc):
    # Reap the direct child, and also kill descendants that hold inherited pipes.
    proc.poll()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            # macOS can report EPERM when the only group member is our exited,
            # unreaped child. Reap it, then independently prove group absence.
            # A live child, surviving descendants or an inaccessible group is
            # still an uncertain cleanup failure, never an ignored permission.
            if proc.poll() is None:
                raise
            try:
                os.killpg(proc.pid, 0)
            except ProcessLookupError:
                break
            raise
        if sig == signal.SIGTERM:
            time.sleep(0.1)
    proc.wait(timeout=5)


def run_bounded(cmd, cwd=None, timeout=900, check=False, raw=False,
                max_output_bytes=8 * 1024 * 1024, *, env=None, input=None,
                max_input_bytes=72 * 1024 * 1024, on_output=None, on_tick=None):
    if timeout <= 0 or max_output_bytes <= 0:
        raise ValueError("positive subprocess limits required")
    if input is not None and (not isinstance(input, bytes) or len(input) > max_input_bytes):
        raise ValueError("bounded bytes input required")
    deadline = time.monotonic() + timeout
    buffers = [bytearray(), bytearray()]
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, env=env)
    try:
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((proc.stdout, proc.stderr)):
                selector.register(stream, selectors.EVENT_READ, index)
            pending = memoryview(input or b'')
            if proc.stdin is not None:
                if pending:
                    os.set_blocking(proc.stdin.fileno(), False)
                    selector.register(proc.stdin, selectors.EVENT_WRITE, 'stdin')
                else:
                    proc.stdin.close()
            try:
                total = 0
                while selector.get_map():
                    if on_tick is not None:
                        on_tick()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(cmd, timeout)
                    for key, _ in selector.select(min(remaining, 0.1)):
                        if key.data == 'stdin':
                            try:
                                pending = pending[os.write(key.fd, pending[:65536]):]
                            except BrokenPipeError:
                                pending = pending[:0]
                            if not pending:
                                selector.unregister(key.fileobj)
                                proc.stdin.close()
                            continue
                        chunk = os.read(key.fd, min(65536, max_output_bytes - total + 1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > max_output_bytes:
                            raise OutputLimitError(max_output_bytes)
                        buffers[key.data].extend(chunk)
                        if on_output is not None:
                            on_output('stdout' if key.data == 0 else 'stderr', chunk)
                proc.wait(timeout=max(0.001, deadline - time.monotonic()))
            except BaseException:
                try:
                    stop_group(proc)
                except (OSError, subprocess.SubprocessError) as exc:
                    # A post-start EPERM is not a safely retryable spawn error.
                    raise ProcessCleanupError("subprocess cleanup could not be confirmed") from exc
                raise
        result = subprocess.CompletedProcess(cmd, proc.returncode,
            *[bytes(b) if raw else b.decode("utf-8", errors="replace") for b in buffers])
    finally:
        # Popen.__exit__ performs an unbounded wait. If group cleanup itself
        # fails, return that uncertainty to the supervisor without waiting for
        # an uncontrolled child forever. Successful paths already reap above.
        proc.stdout.close()
        proc.stderr.close()
        if proc.stdin is not None:
            proc.stdin.close()
    if check and result.returncode:
        # Commands may print secrets; callers receive only return code and identity.
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return result
