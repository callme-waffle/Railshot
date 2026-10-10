"""지원 호스트의 읽기 전용 사전 점검."""
import argparse
import ipaddress
import json
import os
import re
import sys
from urllib.parse import urlsplit
from pathlib import Path
import shutil
import subprocess

from .state import read_private


def run_preflight(*, os_release=Path('/etc/os-release'), geteuid=os.geteuid,
                  which=shutil.which, runner=subprocess.run):
    if geteuid() != 0:
        raise RuntimeError('설치는 root 권한이 필요합니다.')
    values = {}
    for line in Path(os_release).read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            values[key] = value.strip('"')
    if values.get('ID') != 'ubuntu' or values.get('VERSION_ID') != '24.04':
        raise RuntimeError('현재 지원 운영체제는 Ubuntu 24.04입니다.')
    required = ('ip', 'systemctl', 'openstack', 'ssh', 'ssh-keygen')
    missing = [name for name in required if not which(name)]
    if missing:
        raise RuntimeError('필수 명령이 없습니다: ' + ', '.join(missing))
    result = runner(['systemctl', 'is-system-running'], capture_output=True,
                    text=True, timeout=15, check=False)
    if result.stdout.strip() not in ('running', 'degraded'):
        raise RuntimeError('systemd 서비스 관리 환경을 확인하지 못했습니다.')
    return {'os': 'ubuntu', 'version': '24.04', 'commands': list(required)}


def load_config(path):
    # Configuration contains no passwords; nevertheless require root ownership/private mode.
    config = json.loads(read_private(path))
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    validate_config(config)
    return config


class RetiredEnrollment(ValueError):
    """Legacy network registration must never silently become local setup."""


def validate_config(config):
    if "service_url" in config or any(key.startswith("wireguard") for key in config):
        raise RetiredEnrollment("WireGuard 등록은 지원하지 않습니다. 기존 설치는 diagnose/uninstall로 관리하고, 별도 관리 경로를 확보한 뒤 service_url과 wireguard 설정을 뺀 새 로컬 설치 설정을 사용하십시오. Cloudflare 앱 Tunnel은 SSH 관리 경로를 제공하지 않습니다.")
    identity = config.get("openstack")
    access = config.get("vm_access")
    if not isinstance(identity, dict) or not isinstance(access, dict):
        raise ValueError("OpenStack and vm_access configurations are required")
    for name in ("auth_url", "user_domain_name", "project_id", "role_id"):
        if not isinstance(identity.get(name), str) or not identity[name].strip():
            raise ValueError("Missing identity configuration")
    endpoint = urlsplit(identity["auth_url"])
    if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.query or endpoint.fragment or not endpoint.path.rstrip("/").endswith("/v3"):
        raise ValueError("Invalid Keystone endpoint")
    for name in ("network_id", "ssh_username"):
        if not isinstance(access.get(name), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", access[name]):
            raise ValueError("Missing VM access configuration")
    if access["ssh_username"] == "root" or ipaddress.ip_network(access.get("ssh_source_cidr", ""), strict=True).prefixlen == 0:
        raise ValueError("Restricted non-root VM access required")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Railshot 고객 OpenStack 설치·진단")
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/jasmin/bootstrap"))
    parser.add_argument("--config-dir", type=Path, default=Path("/etc/jasmin"))
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--config", type=Path, required=True)
    commands.add_parser("diagnose")
    commands.add_parser("uninstall")
    verify = commands.add_parser("verify-vm")
    verify.add_argument("--profile", type=Path, required=True)
    verify.add_argument("--known-hosts", type=Path, required=True)
    return parser.parse_args(argv)


if __name__ == '__main__':
    args = parse_args()
    if args.command == 'init':
        try:
            load_config(args.config)
        except RetiredEnrollment as exc:
            raise SystemExit(str(exc)) from None
        except Exception as exc:
            raise SystemExit(f'설정 점검 실패: {type(exc).__name__}. docs/architecture/client-bootstrap.md를 확인하십시오.') from None
