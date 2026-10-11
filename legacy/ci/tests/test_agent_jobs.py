"""Independent adversarial checks; fake CLI responses are not live cloud evidence."""
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from apps.agent.protocol import ProtocolError, decode_request, sanitize_instances
from apps.agent.runner import execute_request, run_stream


def valid_request(**updates):
    value = {"version": 1, "job_id": "job-01", "action": "instance.list", "params": {}}
    value.update(updates)
    return json.dumps(value).encode()


@pytest.mark.parametrize("raw", [
    b"", b"{", b"null", b"[]", b"1", b'"hello"', b"\xff",
    valid_request(action="instance.delete"), valid_request(action="instance.list\nwhoami"),
    valid_request(extra="ignored?"), valid_request(version=True), valid_request(version="1"),
    valid_request(version=2), valid_request(job_id=""), valid_request(job_id="x" * 65),
    valid_request(job_id="id\nnext"), valid_request(job_id="id with spaces"),
    valid_request(job_id=[]), valid_request(params=[]), valid_request(params=None),
    valid_request(params={"command": "env"}),
    b'{"version":1,"version":1,"job_id":"x","action":"instance.list","params":{}}',
    valid_request() + b"\n{}", b" " * 16385,
])
def test_rejects_ambiguous_or_unapproved_request(raw):
    with pytest.raises(ProtocolError):
        decode_request(raw)


@pytest.mark.parametrize("command", ["", "bash", "jasmin-job-v1; id", "jasmin-job-v1\n", " jasmin-job-v1", "jasmin-job-v1 --config /etc/shadow"])
def test_forced_command_rejects_before_credentials(command):
    def forbidden(*unused):
        pytest.fail("Rejected remote command touched credentials or CLI")
    response = execute_request(valid_request(), command, forbidden, forbidden)
    assert response["ok"] is False
    assert response["result"] is None
    assert set(response["error"]) == {"code"}


def test_invalid_json_rejects_before_credentials():
    def forbidden(*unused):
        pytest.fail("Invalid request touched credentials")
    assert execute_request(b"{}", "jasmin-job-v1", forbidden, forbidden)["ok"] is False


@pytest.mark.parametrize("failure", [RuntimeError("RAW-UPSTREAM-SECRET"), subprocess.TimeoutExpired("RAW-UPSTREAM-SECRET", 1)])
def test_provider_failure_does_not_reflect_raw_error(failure):
    class BrokenCLI:
        def __init__(self, auth):
            pass
        def run(self, args):
            assert args == ["server", "list"]
            raise failure
    result = execute_request(valid_request(), "jasmin-job-v1", lambda: {"application_credential_secret": "LOCAL-SECRET"}, BrokenCLI)
    assert result["ok"] is False
    assert "RAW-UPSTREAM-SECRET" not in json.dumps(result)
    assert "LOCAL-SECRET" not in json.dumps(result)


def test_secret_fields_and_secret_values_are_never_returned():
    secret = "LOCAL-SECRET"
    class CLI:
        def __init__(self, auth):
            assert auth["application_credential_secret"] == secret
        def run(self, args):
            assert args == ["server", "list"]
            return [{"ID": "vm-id", "Name": "vm", "Status": "ACTIVE", "token": secret, "metadata": {"secret": secret}, "Networks": "private=192.0.2.1"}]
    result = execute_request(valid_request(), "jasmin-job-v1", lambda: {"application_credential_secret": secret}, CLI)
    assert result["ok"] is True
    assert set(result["result"][0]) == {"id", "name", "status"}
    assert secret not in json.dumps(result)
    assert "192.0.2.1" not in json.dumps(result)
    try:
        clean = sanitize_instances([{"ID": "id", "Name": "prefix-" + secret, "Status": "ACTIVE"}], secrets=(secret,))
    except ProtocolError:
        pass
    else:
        assert secret not in json.dumps(clean)


def test_stream_emits_exactly_one_json_document():
    class CLI:
        def __init__(self, auth):
            pass
        def run(self, args):
            return []
    output = io.StringIO()
    run_stream(io.BytesIO(valid_request()), output, "jasmin-job-v1", lambda: {}, CLI)
    value = json.loads(output.getvalue())
    assert value["ok"] is True
    assert value["result"] == []
    assert len(output.getvalue().splitlines()) == 1


def test_stream_bounded_read():
    class GuardedInput:
        def read(self, count=-1):
            assert 0 < count <= 16385
            return b"x" * count
    def forbidden(*unused):
        pytest.fail("Oversized request touched credentials")
    output = io.StringIO()
    run_stream(GuardedInput(), output, "jasmin-job-v1", forbidden, forbidden)
    assert json.loads(output.getvalue())["ok"] is False


def test_replay_is_read_only_reexecution_not_exactly_once():
    calls = []
    class CLI:
        def __init__(self, auth):
            pass
        def run(self, args):
            calls.append(args)
            return []
    first = execute_request(valid_request(), "jasmin-job-v1", lambda: {}, CLI)
    second = execute_request(valid_request(), "jasmin-job-v1", lambda: {}, CLI)
    assert first["ok"] and second["ok"]
    assert calls == [["server", "list"], ["server", "list"]]


def test_input_deadline_rejects_without_credentials(monkeypatch):
    from apps.agent import runner
    import os
    read_fd, write_fd = os.pipe()
    try:
        monkeypatch.setattr(runner.select, "select", lambda *args: ([], [], []))
        def forbidden(*unused):
            pytest.fail("Timed out input touched credentials")
        output = io.StringIO()
        with os.fdopen(read_fd, "rb") as stream:
            run_stream(stream, output, "jasmin-job-v1", forbidden, forbidden)
        assert json.loads(output.getvalue())["ok"] is False
    finally:
        os.close(write_fd)


def test_response_limit_and_invalid_upstream_types():
    from apps.agent.protocol import success_response
    request = decode_request(valid_request())
    with pytest.raises(ProtocolError):
        success_response(request, [{"id": "x", "name": "x" * 1048576, "status": "ACTIVE"}])
    for rows in (None, {}, [None], [{"ID": "id", "Name": "bad\nname", "Status": "ACTIVE"}]):
        with pytest.raises(ProtocolError):
            sanitize_instances(rows)


def test_isolated_runner_entrypoint_rejects_shell_without_traceback():
    import os
    env = dict(os.environ, SSH_ORIGINAL_COMMAND="/bin/sh -c env")
    result = subprocess.run([sys.executable, "-I", str(ROOT / "apps/agent/runner.py"), "--repo", str(ROOT)], input=b"", capture_output=True, env=env, timeout=5)
    assert result.returncode == 1
    assert not result.stderr
    assert json.loads(result.stdout)["error"]["code"] == "command_rejected"


def test_sender_refuses_unapproved_management_route_before_ssh(monkeypatch):
    from apps.agent import sender
    monkeypatch.setattr(sender, "_private_input", lambda value: Path(value))
    calls = []
    def fake(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, '[{"dev":"eth0","prefsrc":"192.0.2.1"}]', "")
    with pytest.raises(ProtocolError):
        sender.send_job(json.loads(valid_request()), "192.0.2.2", "root", "/private/key", "/private/hosts", interface="ens3", runner=fake)
    assert len(calls) == 1
    assert calls[0][0] == "ip"


def test_sender_fixed_ssh_and_response_correlation(monkeypatch):
    from apps.agent import sender
    monkeypatch.setattr(sender, "_private_input", lambda value: Path(value))
    def fake(command, **kwargs):
        if command[0] == "ip":
            return subprocess.CompletedProcess(command, 0, '[{"dev":"ens3","prefsrc":"10.200.0.1"}]', "")
        assert command[-1] == "jasmin-job-v1"
        assert "StrictHostKeyChecking=yes" in command
        assert "IdentityAgent=none" in command
        assert "-T" in command
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 45
        assert kwargs["stderr"] == subprocess.DEVNULL
        kwargs["stdout"].write(json.dumps({"version":1,"job_id":"DIFFERENT-JOB","action":"instance.list","ok":True,"result":[],"error":None}).encode())
        return subprocess.CompletedProcess(command, 0)
    with pytest.raises(ProtocolError):
        sender.send_job(json.loads(valid_request()), "192.0.2.2", "root", "/private/key", "/private/hosts", interface="ens3", runner=fake)


def test_restricted_key_renderer_rejects_options_and_path_injection():
    from apps.agent.install_forced_command import authorized_key_line
    import base64
    blob = b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20' + b'x' * 32
    public = "ssh-ed25519 " + base64.b64encode(blob).decode()
    result = authorized_key_line(public, "10.200.0.2", "/usr/bin/python3", "/opt/jasmin", "/etc/jasmin")
    assert result.startswith('restrict,from="10.200.0.2/32",command="')
    assert " -I " in result
    for malicious in ('command="sh" ' + public, public + "\n" + public):
        with pytest.raises(ValueError):
            authorized_key_line(malicious, "10.200.0.2", "/usr/bin/python3", "/opt/jasmin", "/etc/jasmin")
    for path in ("/usr/bin/python3;id", "/opt/../bin/python3", "relative"):
        with pytest.raises(ValueError):
            authorized_key_line(public, "10.200.0.2", path, "/opt/jasmin", "/etc/jasmin")
