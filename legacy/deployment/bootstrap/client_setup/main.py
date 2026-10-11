"""Explicit operator commands; never provision at import time."""
from __future__ import annotations
from contextlib import contextmanager
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import sys

from .state import StateStore, atomic_private_write, private_directory, read_private
from .credentials import CredentialStore
from .report import installation_report
from .preflight import RetiredEnrollment, load_config, parse_args

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


@contextmanager
def installation_lock(directory):
    private_directory(directory)
    path = directory / "install.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        read_private(path)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def initialize(args):
    from .preflight import run_preflight
    from infrastructure.providers.openstack.cli import OpenStackCLI
    from infrastructure.providers.openstack.identity import configure_identity
    from infrastructure.providers.openstack.discovery import discover_capabilities
    from infrastructure.providers.openstack.access import prepare_vm_access

    config = load_config(args.config)
    state = StateStore(args.state_dir / "bootstrap-state.json")
    state.start("configuration")
    binding = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    if state.data.get("config_binding", binding) != binding:
        raise ValueError("Installation configuration changed; explicit migration required")
    state.data["config_binding"] = binding
    state.complete("configuration", {"bound": True})
    state.start("preflight")
    state.complete("preflight", run_preflight())
    state.start("identity")
    vault = CredentialStore(args.config_dir)
    os_config = dict(config["openstack"])
    os_config["existing_resources"] = state.data["resources"]
    if vault.path.exists():
        auth = vault.load()
    else:
        if not os_config.get("service_username"):
            os_config["service_username"] = input("OpenStack 전용 계정 이름: ").strip()
        if not os_config.get("admin_username"):
            os_config["admin_username"] = input("OpenStack 관리자 ID: ").strip()
        password = getpass.getpass("OpenStack 관리자 암호: ")
        auth = configure_identity(os_config, password, on_resource=state.record_resource)
        del password
        vault.save(auth)
    cli = OpenStackCLI(auth)
    cli.run(["token", "issue"])
    state.complete("identity", {"verified": True})
    state.start("discovery")
    capabilities = discover_capabilities(cli)
    state.complete("discovery", capabilities)
    access_config = dict(config["vm_access"])
    access_config["existing_resources"] = state.data["resources"]
    state.start("vm_access_preparation")
    access = prepare_vm_access(cli, access_config, args.state_dir, on_resource=state.record_resource)
    atomic_private_write(args.state_dir / "vm-access.json", json.dumps(access, indent=2).encode())
    state.complete("vm_access_preparation", {"prepared": True, "connectivity_verified": False})
    report = installation_report(state.data, capabilities, access)
    atomic_private_write(args.state_dir / "report.json", json.dumps(report, indent=2).encode())
    print(json.dumps(report, indent=2, ensure_ascii=False))


def diagnose(args):
    from infrastructure.providers.openstack.cli import OpenStackCLI
    from infrastructure.providers.openstack.discovery import discover_capabilities
    print(json.dumps(discover_capabilities(OpenStackCLI(CredentialStore(args.config_dir).load())), indent=2))


def uninstall(args):
    from .wireguard import uninstall_wireguard
    installation_path = args.config_dir / "installation.json"
    installation = json.loads(read_private(installation_path)) if installation_path.exists() else {}
    result = (uninstall_wireguard(Path(installation["wireguard_config_path"]))
              if "wireguard_config_path" in installation else {"removed": False})
    # Keep cloud resource ownership and credentials for explicit operator revocation.
    print(json.dumps({"wireguard": result, "retained": [str(args.config_dir), str(args.state_dir)],
                      "next_action": "Revoke any previous server registration and OpenStack credential, then explicitly remove retained local files."}))


def require_root():
    if os.geteuid() != 0:
        raise PermissionError("root execution required")


def main(argv=None):
    args = parse_args(argv)
    os.environ.pop("JASMIN_ENROLLMENT_TOKEN", None)
    os.environ.pop("RAILSHOT_ENROLLMENT_TOKEN", None)
    failure_stage = "configuration"
    try:
        require_root()
        private_directory(args.config_dir)
        with installation_lock(args.state_dir):
            if args.command == "init":
                try:
                    initialize(args)
                except Exception as exc:
                    try:
                        failed_state = StateStore(args.state_dir / "bootstrap-state.json")
                        failure_stage = failed_state.fail_current(type(exc).__name__)
                    except Exception:
                        pass
                    raise
            elif args.command == "diagnose":
                diagnose(args)
            elif args.command == "uninstall":
                uninstall(args)
            else:
                from infrastructure.providers.openstack.access import verify_vm_access
                print(json.dumps(verify_vm_access(json.loads(read_private(args.profile)), args.known_hosts), indent=2))
    except Exception as exc:
        if isinstance(exc, RetiredEnrollment):
            print(str(exc), file=sys.stderr)
        # Exception text may contain HTTP responses, command output, or credentials.
        print(f"실패 단계={failure_stage}, 오류 유형={type(exc).__name__}. docs/architecture/client-bootstrap.md의 단계별 복구 안내를 확인하십시오. 원본 오류는 비밀정보 보호를 위해 숨깁니다.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
