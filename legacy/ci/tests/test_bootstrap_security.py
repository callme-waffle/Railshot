import json
import os
from pathlib import Path
import sys
import pytest
from cryptography.fernet import InvalidToken
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deployment/bootstrap"))
from client_setup.credentials import CredentialStore
from client_setup.state import StateStore, atomic_private_write, read_private
from client_setup.report import sanitize


def test_ciphertext_roundtrip_permissions_and_tamper(tmp_path):
    directory = tmp_path / "private"
    vault = CredentialStore(directory)
    vault.save({"application_credential_secret": "unique-secret", "project_id": "project"})
    assert b"unique-secret" not in vault.path.read_bytes()
    assert vault.path.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    assert vault.load()["application_credential_secret"] == "unique-secret"
    vault.path.write_bytes(vault.path.read_bytes()[:-3] + b"bad")
    with pytest.raises(InvalidToken):
        vault.load()


def test_symlinks_and_unsafe_permissions_rejected(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    target = tmp_path / "victim"
    target.write_text("original")
    path = directory / "credentials.enc"
    path.symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        atomic_private_write(path, b"replacement")
    assert target.read_text() == "original"
    path.unlink()
    path.write_text("value")
    path.chmod(0o644)
    with pytest.raises(ValueError):
        read_private(path)


def test_secret_fields_do_not_enter_progress(tmp_path):
    state = StateStore(tmp_path / "private" / "state.json")
    state.complete("identity", {"password": "needle", "nested": [{"token": "needle"}]})
    assert "needle" not in state.path.read_text()
    assert StateStore(state.path).data["stages"]["identity"]["status"] == "complete"


def test_missing_key_never_silently_overwrites_ciphertext(tmp_path):
    vault = CredentialStore(tmp_path / "private")
    vault.save({"secret": "old"})
    original = vault.path.read_bytes()
    vault.key_path.unlink()
    with pytest.raises(ValueError):
        vault.save({"secret": "new"})
    assert vault.path.read_bytes() == original


def test_parent_symlink_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(actual)
    with pytest.raises(ValueError):
        atomic_private_write(alias / "secret", b"secret")


def mock_install(monkeypatch, tmp_path):
    from client_setup import main, preflight
    from infrastructure.providers.openstack import identity, discovery, access, cli
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    # Unit tests retain actual temporary file ownership checks.
    from client_setup import state
    uid = tmp_path.stat().st_uid
    monkeypatch.setattr(state.os, "geteuid", lambda: uid)
    monkeypatch.setattr(main.os, "geteuid", lambda: uid)
    monkeypatch.setattr(preflight, "run_preflight", lambda: {"ok": True})
    calls = {"identity": 0, "password": 0}
    def auth(*args, **kwargs):
        calls["identity"] += 1
        return {"application_credential_secret": "private-auth"}
    monkeypatch.setattr(identity, "configure_identity", auth)
    monkeypatch.setattr(cli.OpenStackCLI, "run", lambda *args: {"token": "never-display"})
    monkeypatch.setattr(discovery, "discover_capabilities", lambda *args: {"compute": "available"})
    monkeypatch.setattr(access, "prepare_vm_access", lambda *args, **kwargs: {"prepared": True})
    def password(*args):
        calls["password"] += 1
        return "private-admin-password"
    monkeypatch.setattr(main.getpass, "getpass", password)
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o700)
    config = config_dir / "input.json"
    config.write_text(json.dumps({"openstack": {"admin_username": "admin", "service_username": "svc", "project_id": "one", "role_id": "role", "auth_url": "https://identity.example.org/v3", "user_domain_name": "Default"}, "vm_access": {"network_id": "network", "ssh_username": "ubuntu", "ssh_source_cidr": "192.0.2.10/32"}}))
    config.chmod(0o600)
    import argparse
    args = argparse.Namespace(config=config, config_dir=config_dir, state_dir=tmp_path / "state")
    return main, args, calls


def test_init_resume_and_configuration_binding(monkeypatch, tmp_path, capsys):
    main, args, calls = mock_install(monkeypatch, tmp_path)
    main.initialize(args)
    main.initialize(args)
    assert calls == {"identity": 1, "password": 1}
    assert not (args.config_dir / "registration.json").exists()
    assert not (args.config_dir / "wireguard").exists()
    stages = StateStore(args.state_dir / "bootstrap-state.json").data["stages"]
    assert not any(stage.startswith(("wireguard", "registration")) for stage in stages)
    output = capsys.readouterr().out
    assert not any(secret in output for secret in ("private-auth", "private-admin-password", "never-display"))
    config = json.loads(args.config.read_text())
    config["openstack"]["project_id"] = "other"
    args.config.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="configuration changed"):
        main.initialize(args)
    assert calls["identity"] == 1


def test_stage_failure_overwrites_previous_success(tmp_path):
    state = StateStore(tmp_path / "state" / "progress.json")
    state.complete("discovery", {"ok": True})
    state.start("discovery")
    assert state.data["stages"]["discovery"]["status"] == "running"
    state.fail_current("TimeoutError")
    reloaded = StateStore(state.path)
    assert reloaded.data["stages"]["discovery"] == {"status": "failed", "error_type": "TimeoutError"}


@pytest.mark.parametrize('canonical', [None, 'new-one-use-token'])
def test_main_discards_retired_token_environment_before_execution(monkeypatch, tmp_path, canonical):
    from client_setup import main
    monkeypatch.setattr(main.os, "geteuid", lambda: 0)
    monkeypatch.setattr(main, "private_directory", lambda path: path)
    from contextlib import nullcontext
    monkeypatch.setattr(main, "installation_lock", lambda path: nullcontext())
    monkeypatch.setenv("JASMIN_ENROLLMENT_TOKEN", "one-use-token")
    if canonical:
        monkeypatch.setenv("RAILSHOT_ENROLLMENT_TOKEN", canonical)
    else:
        monkeypatch.delenv("RAILSHOT_ENROLLMENT_TOKEN", raising=False)
    def execute(args):
        assert not hasattr(args, "enrollment_token")
        assert "JASMIN_ENROLLMENT_TOKEN" not in os.environ
        assert "RAILSHOT_ENROLLMENT_TOKEN" not in os.environ
    monkeypatch.setattr(main, "initialize", execute)
    assert main.main(["init", "--config", "/unused"]) == 0


def test_failure_persists_failed_stage_without_raw_error(monkeypatch, tmp_path, capsys):
    main, args, _ = mock_install(monkeypatch, tmp_path)
    from infrastructure.providers.openstack import discovery
    def failure(*unused):
        raise RuntimeError("secret-password-in-upstream-error")
    monkeypatch.setattr(discovery, "discover_capabilities", failure)
    monkeypatch.setattr(main, "require_root", lambda: None)
    monkeypatch.setenv("JASMIN_ENROLLMENT_TOKEN", "one-time-key")
    result = main.main(["--config-dir", str(args.config_dir), "--state-dir", str(args.state_dir), "init", "--config", str(args.config)])
    assert result == 1
    assert StateStore(args.state_dir / "bootstrap-state.json").data["stages"]["discovery"]["status"] == "failed"
    output = capsys.readouterr().err
    assert "discovery" in output
    assert "secret-password" not in output


def test_invalid_config_has_no_external_side_effect(monkeypatch, tmp_path):
    main, args, calls = mock_install(monkeypatch, tmp_path)
    config = json.loads(args.config.read_text())
    del config["vm_access"]["network_id"]
    args.config.write_text(json.dumps(config))
    with pytest.raises(ValueError):
        main.initialize(args)
    assert calls["identity"] == 0


def test_fifo_rejected_without_waiting(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "pipe"
    os.mkfifo(path, mode=0o600)
    with pytest.raises(ValueError):
        read_private(path)


@pytest.mark.parametrize("field", ["service_url", "wireguard_config_path", "wireguard_peer"])
def test_retired_registration_fails_before_local_or_external_setup(monkeypatch, tmp_path, capsys, field):
    main, args, calls = mock_install(monkeypatch, tmp_path)
    config = json.loads(args.config.read_text())
    config[field] = "legacy-value"
    args.config.write_text(json.dumps(config))
    with pytest.raises(main.RetiredEnrollment):
        main.initialize(args)
    assert calls == {"identity": 0, "password": 0}
    assert not args.state_dir.exists()
    monkeypatch.setattr(main, "require_root", lambda: None)
    assert main.main(["--config-dir", str(args.config_dir), "--state-dir", str(args.state_dir),
                      "init", "--config", str(args.config)]) == 1
    assert "WireGuard 등록은 지원하지 않습니다" in capsys.readouterr().err
    assert calls == {"identity": 0, "password": 0}


def test_removed_enrollment_option_rejected_before_setup(monkeypatch):
    from client_setup import main
    monkeypatch.setattr(main, "require_root", lambda: pytest.fail("must reject before setup"))
    with pytest.raises(SystemExit) as error:
        main.main(["init", "--config", "/unused", "--enrollment-token-file", "/unused"])
    assert error.value.code == 2


def test_uninstall_local_only_setup_preserves_credentials(monkeypatch, tmp_path, capsys):
    main, args, _ = mock_install(monkeypatch, tmp_path)
    main.initialize(args)
    original = CredentialStore(args.config_dir).path.read_bytes()
    main.uninstall(args)
    assert CredentialStore(args.config_dir).path.read_bytes() == original
    assert '"removed": false' in capsys.readouterr().out


@pytest.mark.parametrize('arguments,expected', [
    (['init', '--config', 'CONFIG'], 1),
    (['init', '--config', 'CONFIG', '--enrollment-token-file', '/unused'], 2),
    (['init', '--help'], 0),
])
def test_installer_rejects_retired_inputs_before_package_or_payload_changes(tmp_path, arguments, expected):
    import shlex
    import subprocess
    config_dir = tmp_path / 'config'
    config_dir.mkdir(mode=0o700)
    config = config_dir / 'legacy.json'
    config.write_text(json.dumps({'service_url': 'https://retired.example'}))
    config.chmod(0o600)
    commands = tmp_path / 'commands'
    commands.mkdir()
    marker = tmp_path / 'mutation-attempt'
    # Only the stdlib input check may execute; intercept all setup commands safely.
    shim = commands / 'python3'
    shim.write_text('#!/bin/sh\nif [ "$1" = "-m" ] && [ "$2" = "client_setup.preflight" ]; then\n'
                    f'  exec {shlex.quote(sys.executable)} "$@"\nfi\n'
                    f'touch {shlex.quote(str(marker))}\nexit 99\n')
    shim.chmod(0o755)
    apt = commands / 'apt-get'
    apt.write_text(f'#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 99\n')
    apt.chmod(0o755)
    args = [str(config) if value == 'CONFIG' else value for value in arguments]
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(['bash', str(root / 'deployment/bootstrap/install.sh'),
                             '--install-dependencies', *args], capture_output=True, text=True,
                            env={**os.environ, 'PATH': str(commands) + ':/usr/bin:/bin'}, timeout=5)
    assert result.returncode == expected, result.stdout + result.stderr
    assert not marker.exists()
    if expected == 1:
        assert 'WireGuard 등록은 지원하지 않습니다' in result.stderr
