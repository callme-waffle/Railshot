"""Render real templates with Ansible, using only synthetic localhost fixtures.

Run with: python -m pytest ci/tests/test_rendering.py
Requires ansible-playbook, pytest and PyYAML on the controller. No remote hosts
are contacted and no system services or production paths are changed.
"""

from __future__ import annotations

import base64
import configparser
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
ROLES = ROOT / "infrastructure/ansible/roles"
PASSWORD = 'synthetic:secret "quoted" # value'


def render(tmp_path, role, filename, overrides=None, omit_private_ip=False):
    executable = shutil.which("ansible-playbook")
    if executable is None:
        pytest.fail("ansible-playbook is required for executable rendering checks")
    variables = {}
    for name in ("deployment_defaults", role):
        path = ROLES / name / "defaults/main.yml"
        if path.exists():
            variables.update(yaml.safe_load(path.read_text()) or {})
    variables.update(
        cluster_name="test-cluster",
        site="onprem",
        vault_patroni_api_password=PASSWORD,
        vault_postgres_password=PASSWORD,
        vault_replication_password=PASSWORD,
        client_cidrs=["10.70.1.0/24"],
    )
    variables.update(overrides or {})
    host = {
        "ansible_connection": "local",
        "ansible_python_interpreter": sys.executable,
    }
    if not omit_private_ip:
        host["private_ip"] = "10.70.0.11"
    inventory = {
        "all": {
            "children": {
                "db_nodes": {"hosts": {"db01": host, "db02": {"private_ip": "10.70.0.12"}}},
                "etcd_nodes": {
                    "hosts": {
                        "etcd01": {"private_ip": "10.71.0.11"},
                        "etcd02": {"private_ip": "10.71.0.12"},
                        "etcd03": {"private_ip": "10.71.0.13"},
                    }
                },
            }
        }
    }
    output = tmp_path / "rendered.conf"
    inventory_path = tmp_path / "inventory.yml"
    playbook = tmp_path / "render.yml"
    inventory_path.write_text(yaml.safe_dump(inventory))
    playbook.write_text(yaml.safe_dump([{
        "name": "Render synthetic configuration",
        "hosts": "db01",
        "gather_facts": False,
        "vars": variables,
        "tasks": [{
            "name": "Render fixture",
            "ansible.builtin.template": {
                "src": str(ROLES / role / "templates" / filename),
                "dest": str(output),
                "mode": "0600",
            },
            "no_log": True,
        }],
    }]))
    environment = os.environ.copy()
    environment.update(
        ANSIBLE_HOME=str(tmp_path / "ansible-home"),
        ANSIBLE_LOCAL_TEMP=str(tmp_path / "ansible-local"),
        ANSIBLE_REMOTE_TEMP=str(tmp_path / "ansible-remote"),
        ANSIBLE_CONFIG=str(tmp_path / "ansible.cfg"),
        ANSIBLE_NOCOLOR="1",
    )
    (tmp_path / "ansible.cfg").write_text("[defaults]\nretry_files_enabled = False\n")
    result = subprocess.run(
        [executable, "-i", str(inventory_path), str(playbook)],
        capture_output=True, text=True, env=environment, timeout=60, check=False,
    )
    return result, output


def parsed_render(tmp_path, role, filename, overrides=None):
    result, output = render(tmp_path, role, filename, overrides)
    assert result.returncode == 0, result.stdout + result.stderr
    return yaml.safe_load(output.read_text())


def test_etcd_topology_and_mutual_tls(tmp_path):
    config = parsed_render(tmp_path, "etcd", "etcd.yml.j2")
    peers = dict(pair.split("=", 1) for pair in config["initial-cluster"].split(","))
    assert peers == {
        "etcd01": "https://10.71.0.11:2380",
        "etcd02": "https://10.71.0.12:2380",
        "etcd03": "https://10.71.0.13:2380",
    }
    assert config["initial-cluster-token"] == "test-cluster-etcd"
    assert config["strict-reconfig-check"] is True
    assert config["enable-grpc-gateway"] is True
    for key in ("client-transport-security", "peer-transport-security"):
        assert config[key]["client-cert-auth"] is True
        assert config[key]["trusted-ca-file"] == "/etc/etcd/tls/ca.crt"
        assert config[key]["key-file"] == "/etc/etcd/tls/server.key"


@pytest.mark.parametrize('legacy,label', [(False, 'Railshot'), (True, 'Jasmin')])
def test_etcd_service_display_preserves_existing_description(tmp_path, legacy, label):
    result, output = render(tmp_path, 'etcd', 'etcd.service.j2', {'etcd_legacy_service_description': legacy})
    assert result.returncode == 0, result.stdout + result.stderr
    service = output.read_text()
    assert f'Description={label} managed etcd\n' in service
    assert 'ExecStart=/usr/bin/etcd --config-file=/etc/etcd/jasmin.yml\n' in service
    assert 'ReadWritePaths=/var/lib/etcd/jasmin\n' in service


@pytest.mark.parametrize('status,payload,ready', [
    (404, '404 page not found\n', False),
    (200, {'members': []}, False),
    (200, {'members': [{'name': name} for name in ('etcd01', 'etcd02', 'etcd03')]}, True),
])
def test_etcd_gateway_rejects_missing_or_incomplete_json_api(tmp_path, status, payload, ready):
    """Run the actual URI readiness task against a loopback-only response fixture."""
    task = yaml.safe_load((ROLES / 'etcd/tasks/verify.yml').read_text())[-1]
    uri = task['ansible.builtin.uri']
    assert uri['url'] == 'https://{{ private_ip }}:2379/v3/cluster/member/list'
    assert uri['validate_certs'] is True and uri['use_proxy'] is False
    assert all(uri[key].startswith('/etc/etcd/tls/') for key in ('ca_path', 'client_cert', 'client_key'))
    requests = []

    class Fixture(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append((self.path, self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(status)
            self.send_header('Content-Type', 'application/json' if isinstance(payload, dict) else 'text/plain')
            self.end_headers()
            self.wfile.write((json.dumps(payload) if isinstance(payload, dict) else payload).encode())

        def log_message(self, *_):
            pass

    server = HTTPServer(('127.0.0.1', 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        uri['url'] = f'http://127.0.0.1:{server.server_port}/v3/cluster/member/list'
        for key in ('ca_path', 'client_cert', 'client_key'):
            del uri[key]  # Synthetic loopback transport; production task TLS is asserted above.
        inventory = tmp_path / 'inventory.json'
        inventory.write_text(json.dumps({'all': {'hosts': {'localhost': {}}, 'children': {
            'etcd_nodes': {'hosts': {name: {} for name in ('etcd01', 'etcd02', 'etcd03')}}}}}))
        playbook = tmp_path / 'verify.yml'
        playbook.write_text(yaml.safe_dump([{'name': 'Check gateway fixture', 'hosts': 'localhost',
            'gather_facts': False, 'vars': {'etcd_health_retries': 1, 'etcd_health_delay': 0}, 'tasks': [task]}]))
        result = subprocess.run(['ansible-playbook', '-i', str(inventory), '-c', 'local', str(playbook),
            '-e', 'ansible_python_interpreter=' + sys.executable], capture_output=True, text=True,
            env={**os.environ, 'ANSIBLE_CONFIG': str(tmp_path / 'absent.cfg')}, timeout=20)
        assert (result.returncode == 0) is ready, result.stdout + result.stderr
        assert requests and requests[0] == ('/v3/cluster/member/list', b'{}')
    finally:
        server.shutdown(); server.server_close(); thread.join(2)


def test_haproxy_check_channel_is_distinct_from_database_channel(tmp_path):
    result, output = render(tmp_path, "haproxy", "haproxy.cfg.j2")
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [shlex.split(line) for line in output.read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    servers = [line for line in lines if line[0] == "server"]
    assert len(servers) == 2
    for server in servers:
        assert server[2].endswith(":5432")
        assert "ssl" not in server  # No extra TLS layer on PostgreSQL protocol.
        assert "check-ssl" in server
        assert server[server.index("port") + 1] == "8008"
        assert server[server.index("verify") + 1] == "required"
        assert server[server.index("verifyhost") + 1] == server[1]
    request = next(line for line in lines if line[:2] == ["http-check", "send"])
    assert request[request.index("uri") + 1] == "/primary"
    authorization = request[request.index("Authorization") + 1]
    assert base64.b64decode(authorization.split()[1]).decode() == "patroni:" + PASSWORD
    assert ["http-check", "expect", "status", "200"] in lines


@pytest.mark.parametrize("mode,expected", [(None, True), ("asynchronous", False)])
def test_patroni_replication_default_and_override(tmp_path, mode, expected):
    overrides = {} if mode is None else {"replication_mode": mode}
    config = parsed_render(tmp_path, "patroni", "patroni.yml.j2", overrides)
    assert config["bootstrap"]["dcs"]["synchronous_mode"] is expected
    assert config["restapi"]["authentication"]["password"] == PASSWORD
    assert config["etcd3"]["protocol"] == "https"
    assert len(config["etcd3"]["hosts"]) == 3
    assert config["postgresql"]["authentication"]["replication"]["sslmode"] == "verify-full"
    assert all("host " not in rule for rule in config["postgresql"]["pg_hba"])


@pytest.mark.parametrize("role,filename", [
    ("etcd", "etcd.yml.j2"),
    ("haproxy", "haproxy.cfg.j2"),
    ("patroni", "patroni.yml.j2"),
])
def test_missing_address_rejects_render_without_partial_configuration(tmp_path, role, filename):
    result, output = render(tmp_path, role, filename, omit_private_ip=True)
    assert result.returncode != 0
    assert not output.exists()


def test_backup_retention_override_and_database_location(tmp_path):
    result, output = render(tmp_path, "backup", "pgbackrest.conf.j2", {
        "pgbackrest_repo_path": "/mnt/dedicated-backup",
        "retention_full": 3,
        "postgres_data_dir": "/srv/postgres/data",
    })
    assert result.returncode == 0, result.stdout + result.stderr
    config = configparser.ConfigParser()
    config.read(output)
    assert config.getint("global", "repo1-retention-full") == 3
    assert config.get("global", "repo1-path") == "/mnt/dedicated-backup"
    assert config.get("test-cluster", "pg1-path") == "/srv/postgres/data"
    assert config.getint("test-cluster", "pg1-port") == 5432


def test_backup_wrapper_has_valid_posix_shell_syntax(tmp_path):
    result, output = render(tmp_path, "backup", "jasmin-backup.sh.j2")
    assert result.returncode == 0, result.stdout + result.stderr
    syntax = subprocess.run(
        ["/bin/sh", "-n", str(output)], capture_output=True, text=True,
        check=False, timeout=10,
    )
    assert syntax.returncode == 0, syntax.stderr
