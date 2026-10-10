"""Model-free checks of the exact Codex permission profile in the calling process context."""
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time

from observability import OperationError

# Adapted from OpenAI Codex (Apache-2.0); source and scope in UPSTREAM.md.
NAMESPACE_FAILURES = (
    "loopback: Failed RTM_NEWADDR", "loopback: Failed RTM_NEWLINK",
    "setting up uid map: Permission denied", "No permissions to create a new namespace",
    "bwrap: Creating new namespace failed", "bwrap: setting up uid map: Operation not permitted",
)
SUCCESS = "railshot-native-preflight-ok"


def sandbox_failure(output):
    return any(signature in (output or "") for signature in NAMESPACE_FAILURES)


def _execute(command, *, cwd, env, timeout=10):
    """A timeout must stop the native sandbox's children as well as the CLI."""
    with subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def check(binary, overrides, workspace, run, auth_home):
    """Fail closed before a model call; never change privileges or retry a broken sandbox."""
    workspace, run = Path(workspace).resolve(), Path(run).resolve()
    receipt = {"schema_version": 1, "status": "BLOCKED", "model_calls": 0,
               "hostname": socket.gethostname(), "uid": os.geteuid(), "binary": str(binary),
               "permissions_sha256": hashlib.sha256("\n".join(overrides).encode()).hexdigest(),
               "checks": ["allowed_read", "write_denied", "outside_read_denied", "network_denied"],
               "started_at": time.time(), "phase": "prepare"}
    # No auth value or user environment is passed to the probe command.
    env = {key: value for key, value in os.environ.items() if key in ("PATH", "TMPDIR", "LANG")}
    env.update(CODEX_HOME=str(auth_home), OPENAI_API_KEY="", CODEX_API_KEY="")
    try:
        with tempfile.TemporaryDirectory(prefix="railshot-denied-probe-") as temporary, \
                tempfile.TemporaryDirectory(prefix="native-probe-", dir=run) as allowed, \
                socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            denied = Path(temporary).resolve() / "unreadable"
            # A broad caller-provided root must not accidentally cover the negative control.
            if any(denied.is_relative_to(root) for root in (workspace, run)):
                raise ValueError("outside probe is covered by a read root")
            denied.write_text("outside-control")
            marker = Path(allowed) / "readable"
            marker.write_text("allowed-control")
            write_target = Path(allowed) / "must-not-exist"
            listener.bind(("127.0.0.1", 0)); listener.listen(2)
            listener.settimeout(2)
            port = str(listener.getsockname()[1])
            # Prove this bash supports /dev/tcp and the endpoint works outside the sandbox.
            receipt["phase"] = "network_control"
            control = _execute(["/bin/bash", "--noprofile", "--norc", "-c",
                                'exec 3<>/dev/tcp/127.0.0.1/"$1"', "probe", port], cwd=workspace, env=env)
            if control.returncode:
                raise subprocess.CalledProcessError(control.returncode, "network-control")
            connection, _ = listener.accept(); connection.close()
            command = [str(binary), "sandbox", "-P", "railshot_read", "-C", str(workspace)]
            for override in overrides:
                command.extend(("-c", override))
            # Each negative check starts only after a successful allowed read in this sandbox.
            script = '''set -eu
[ "$(cat "$1")" = allowed-control ] || exit 71
ls "$2" >/dev/null || exit 71
cat "$3" "$4" >/dev/null || exit 71
if (printf forbidden >"$5") 2>/dev/null; then exit 72; fi
if cat "$6" >/dev/null 2>&1; then exit 73; fi
if (exec 3<>/dev/tcp/127.0.0.1/"$7") 2>/dev/null; then exit 74; fi
printf railshot-native-preflight-ok
'''
            platform = Path(__file__).resolve().parents[1]
            command.extend(("--", "/bin/bash", "--noprofile", "--norc", "-c", script, "probe",
                            str(marker), str(workspace), str(platform / "contract/paths.yaml"),
                            str(platform / "schemas/report.schema.json"), str(write_target), str(denied), port))
            receipt["phase"] = "native_sandbox"
            result = _execute(command, cwd=workspace, env=env)
            receipt.update(exit_code=result.returncode,
                           output_sha256=hashlib.sha256((result.stdout + result.stderr).encode()).hexdigest())
            if sandbox_failure(result.stdout + result.stderr):
                receipt["category"] = "namespace_unavailable"
            if result.returncode or result.stdout != SUCCESS or write_target.exists():
                receipt["phase"] = {71: "allowed_read", 72: "write_denied", 73: "outside_read_denied",
                                    74: "network_denied"}.get(result.returncode, "native_sandbox")
                raise subprocess.CalledProcessError(result.returncode or 1, "native-sandbox")
            receipt.update(status="PASS", phase="complete")
            return receipt
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        receipt["cause_type"] = type(exc).__name__
        raise OperationError("SDK_SANDBOX_UNAVAILABLE", component="runner", phase="sandbox.preflight",
                             retry_policy="after_configuration", cause=exc) from exc
    finally:
        receipt["finished_at"] = time.time()
        try:
            (run / "codex-sandbox-preflight.json").write_text(json.dumps(receipt, sort_keys=True))
        except OSError as exc:
            raise OperationError("OBSERVATION_WRITE_FAILED", component="runner", phase="sandbox.preflight",
                                 retry_policy="after_reconcile", cause=exc) from exc
