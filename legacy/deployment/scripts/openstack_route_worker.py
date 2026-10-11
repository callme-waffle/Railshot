#!/usr/bin/env python3
"""Controller-only Octavia routes. Never provisions a listener, TLS, or DNS.

The root-owned config fixes one project/LB/listener/runtime/subnet/domain, the
runtime and amphora network identities, and a private state_dir. Keep that
directory across deployments: an intent without an
ID permits discovery only, never another create. JSON stdout contains no CLI
output. The existing cloud-cli.py owns authentication. Application identity is
immutable; a changed health_path recreates only the verified app-owned route.
"""

import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import uuid


CONFIG = Path("/opt/railshot/octavia/route-config.json")
ENVIRONMENTS = Path("/var/lib/railshot/octavia/environments")
CONTROLLER_STATE = Path("/var/lib/railshot/octavia")
CLI = "/opt/railshot/octavia/cloud-cli.py"
ROOT_UID = 0
KINDS = ("pool", "member", "monitor", "policy", "rule")


class RouteError(ValueError):
    def __init__(self, reason, status="blocked"):
        super().__init__(reason)
        self.status = status


def require(condition, reason):
    if not condition:
        raise RouteError(reason)


def is_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def is_project(value):
    return isinstance(value, str) and (is_uuid(value) or re.fullmatch(r"[0-9a-f]{32}", value))


def is_private_ipv4(value):
    try:
        ip = ipaddress.IPv4Address(value) if isinstance(value, str) else None
    except ipaddress.AddressValueError:
        return False
    return ip is not None and any(ip in ipaddress.ip_network(cidr) for cidr in
                                 ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def checked_request(config, request):
    """Validate the public adapter/worker contract without accessing credentials."""
    keys = {"application_id", "hostname", "private_address", "node_port", "health_path"}
    require(isinstance(request, dict) and set(request) == keys, "invalid_request_keys")
    app, host = request["application_id"], request["hostname"]
    require(isinstance(app, str) and re.fullmatch(r"app-[0-9a-f]{24}", app), "invalid_application_id")
    domain = config.get("base_domain", "")
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    require(isinstance(domain, str) and len(domain) <= 190 and
            re.fullmatch(rf"{label}(?:\.{label})+", domain), "invalid_base_domain")
    require(isinstance(host, str) and re.fullmatch(rf"{label}\.{re.escape(domain)}", host),
            "invalid_hostname")
    address = request["private_address"]
    require(is_private_ipv4(address) and
            address == config.get("runtime_private_address"), "invalid_private_address")
    port = request["node_port"]
    require(type(port) is int and 30000 <= port <= 32767, "invalid_node_port")
    path = request["health_path"]
    require(isinstance(path, str) and len(path) <= 1024 and
            re.fullmatch(r"/[A-Za-z0-9/_~.-]*", path) and "//" not in path and
            not {".", ".."}.intersection(path.split("/")), "invalid_health_path")
    return dict(request)


def checked_config(config):
    keys = {"version", "project_id", "loadbalancer_id", "listener_id", "member_subnet_id",
            "runtime_private_address", "base_domain", "state_dir", "network"}
    require(isinstance(config, dict) and set(config) == keys and
            type(config["version"]) is int and config["version"] == 1, "invalid_config")
    require(is_project(config["project_id"]), "invalid_config_identity")
    for key in ("loadbalancer_id", "listener_id", "member_subnet_id"):
        require(is_uuid(config[key]), "invalid_config_identity")
    network = config["network"]
    identities = {"runtime_server_id", "runtime_port_id", "runtime_security_group_id",
                  "amphora_port_id", "amphora_server_id"}
    require(isinstance(network, dict) and set(network) == identities | {
        "runtime_project_id", "amphora_private_address"}, "invalid_network_configuration")
    require(all(is_uuid(network[key]) for key in identities) and
            is_project(network["runtime_project_id"]) and is_private_ipv4(network["amphora_private_address"])
            and network["amphora_private_address"] != config["runtime_private_address"],
            "invalid_network_configuration")
    require(isinstance(config["state_dir"], str), "invalid_state_directory")
    path = Path(config["state_dir"])
    require(path.is_absolute() and path == path.resolve(), "invalid_state_directory")
    return config


def operator_base(config):
    """Separate the operator's fixed ingress authority from any old runtime."""
    keys = {'version', 'project_id', 'loadbalancer_id', 'listener_id', 'member_subnet_id', 'base_domain', 'network'}
    if isinstance(config, dict) and 'runtime_private_address' in config:
        checked_config(config)
        config = {key: config[key] for key in keys}
        config['network'] = {key: config['network'][key] for key in
                             ('amphora_port_id', 'amphora_server_id', 'amphora_private_address')}
    require(isinstance(config, dict) and set(config) == keys and type(config['version']) is int
            and config['version'] == 1 and is_project(config['project_id']), 'invalid_operator_base')
    require(all(is_uuid(config[key]) for key in ('loadbalancer_id', 'listener_id', 'member_subnet_id')),
            'invalid_operator_base')
    network = config['network']
    require(isinstance(network, dict) and set(network) == {
        'amphora_port_id', 'amphora_server_id', 'amphora_private_address'}
        and all(is_uuid(network[key]) for key in ('amphora_port_id', 'amphora_server_id'))
        and is_private_ipv4(network['amphora_private_address']), 'invalid_operator_base')
    label = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
    require(isinstance(config['base_domain'], str) and len(config['base_domain']) <= 190
            and re.fullmatch(rf'{label}(?:\.{label})+', config['base_domain']), 'invalid_operator_base')
    return json.loads(json.dumps(config))


def checked_worker_binding(binding):
    require(isinstance(binding, dict) and set(binding) == {
        'environment_id', 'generation', 'base_sha256', 'binding_sha256'}, 'invalid_worker_binding')
    ident = binding['environment_id']
    require(isinstance(ident, str) and ident.startswith('personal-') and is_uuid(ident[9:])
            and type(binding['generation']) is int and binding['generation'] > 0, 'invalid_worker_binding')
    require(all(isinstance(binding[key], str) and re.fullmatch(r'[a-f0-9]{64}', binding[key])
                for key in ('base_sha256', 'binding_sha256')), 'invalid_worker_binding')
    return dict(binding)


def registration_binding(base, environment_id, generation, runtime):
    base = operator_base(base)
    require(isinstance(runtime, dict) and set(runtime) == {
        'project_id', 'server_id', 'port_id', 'security_group_id', 'private_address'}, 'invalid_runtime_binding')
    require(is_project(runtime['project_id']) and all(is_uuid(runtime[key]) for key in
        ('server_id', 'port_id', 'security_group_id')) and is_private_ipv4(runtime['private_address'])
        and runtime['private_address'] != base['network']['amphora_private_address'], 'invalid_runtime_binding')
    binding = {'environment_id': environment_id, 'generation': generation, 'base_sha256': lifecycle_hash(base)}
    binding['binding_sha256'] = lifecycle_hash({**binding, 'runtime': runtime})
    return checked_worker_binding(binding)


def verify_runtime_registration(base, binding, runtime, call):
    """Read provider evidence again on the trusted controller; never modify it."""
    def native(arguments):
        return call([*arguments, '-f', 'json'], service=())
    project = native(['project', 'show', runtime['project_id']])
    require(project.get('id') == runtime['project_id'] and project.get('name') == 'railshot'
            and project.get('enabled') is True, 'runtime_project_unverified')
    raw_server = native(['server', 'show', runtime['server_id']])
    server = {key.lower().replace(' ', '_'): value for key, value in raw_server.items()}
    properties = server.get('properties', {})
    require(server.get('id') == runtime['server_id']
            and server.get('project_id', server.get('tenant_id')) == runtime['project_id']
            and server.get('status') == 'ACTIVE' and isinstance(properties, dict), 'runtime_server_unverified')
    if 'railshot.target' in properties or 'railshot.generation' in properties:
        require(properties.get('railshot.target') == binding['environment_id']
                and str(properties.get('railshot.generation')) == str(binding['generation']), 'runtime_server_owner_mismatch')
    ports = []
    for role, port_id, server_id, project_id, address in (
        ('runtime', runtime['port_id'], runtime['server_id'], runtime['project_id'], runtime['private_address']),
        ('amphora', base['network']['amphora_port_id'], base['network']['amphora_server_id'],
         base['project_id'], base['network']['amphora_private_address'])):
        port = native(['port', 'show', port_id])
        require(port.get('id') == port_id and port.get('device_id') == server_id
                and port.get('project_id') == project_id and port.get('device_owner') == 'compute:nova',
                'runtime_port_unverified')
        require(isinstance(port.get('fixed_ips'), list) and any(ip.get('ip_address') == address
                and ip.get('subnet_id') == base['member_subnet_id'] for ip in port['fixed_ips']
                if isinstance(ip, dict)), 'runtime_subnet_unverified')
        ports.append(port)
    require(is_uuid(ports[0].get('network_id')) and ports[0]['network_id'] == ports[1].get('network_id')
            and ports[0].get('security_group_ids') == [runtime['security_group_id']], 'runtime_network_unverified')
    group = native(['security', 'group', 'show', runtime['security_group_id']])
    require(group.get('id') == runtime['security_group_id'] and group.get('project_id') == runtime['project_id']
            and group.get('shared', False) is False,
            'runtime_security_group_unverified')
    # A NodePort rule applies to every port in the group. Refuse a group's reuse
    # before adding any application rule that could expose an unrelated VM.
    project_ports = native(['port', 'list', '--project', runtime['project_id'], '-c', 'ID'])
    require(isinstance(project_ports, list) and all(isinstance(row, dict) and is_uuid(row.get('ID'))
            for row in project_ports), 'runtime_group_membership_unverified')
    require(sum(row['ID'] == runtime['port_id'] for row in project_ports) == 1,
            'runtime_group_membership_unverified')
    for row in project_ports:
        if row['ID'] == runtime['port_id']:
            continue
        other = native(['port', 'show', row['ID']])
        require(other.get('id') == row['ID'] and other.get('project_id') == runtime['project_id']
                and isinstance(other.get('security_group_ids'), list)
                and runtime['security_group_id'] not in other['security_group_ids'], 'runtime_security_group_shared')
    lb = call(['show', base['loadbalancer_id'], '-f', 'json'])
    listener = call(['listener', 'show', base['listener_id'], '-f', 'json'])
    require(lb.get('id') == base['loadbalancer_id'] and lb.get('project_id') == base['project_id']
            and lb.get('admin_state_up') is True and lb.get('provisioning_status') == 'ACTIVE'
            and listener.get('id') == base['listener_id'] and listener.get('project_id') == base['project_id']
            and listener.get('admin_state_up') is True and listener.get('provisioning_status') == 'ACTIVE'
            and relation_ids(listener.get('loadbalancers')) == {base['loadbalancer_id']}
            and listener.get('protocol') == 'TERMINATED_HTTPS' and listener.get('protocol_port') == 443,
            'operator_ingress_unverified')


def environment_directory():
    require(ENVIRONMENTS.is_absolute() and ENVIRONMENTS.resolve() == ENVIRONMENTS, 'unsafe_environment_directory')
    ENVIRONMENTS.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_stat(ENVIRONMENTS.stat())
    return ENVIRONMENTS


def register_runtime(config, request, call=None):
    call = run_cli if call is None else call
    require(isinstance(request, dict) and set(request) == {'operation', 'binding', 'runtime'}
            and request['operation'] == 'register-runtime', 'invalid_registration_request')
    base = operator_base(config)
    binding = checked_worker_binding(request['binding'])
    runtime = request['runtime']
    require(binding == registration_binding(base, binding['environment_id'], binding['generation'], runtime),
            'operator_base_or_binding_changed')
    directory = environment_directory()
    fd = os.open(directory / 'registration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private_stat(os.fstat(fd))
        require(stat.S_ISREG(os.fstat(fd).st_mode), 'invalid_registration_lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        target = directory / binding['environment_id']
        path = target / 'binding.json'
        if path.exists() or path.is_symlink():
            prior = read_private(path)
            require(prior.get('binding') == binding and prior.get('runtime') == runtime
                    and prior.get('status') == 'active', 'environment_binding_immutable')
        else:
            # A global claim check also prevents two environment IDs from taking
            # the same VM, network port, or member address on the same ingress.
            for candidate in directory.glob('personal-*/binding.json'):
                other = read_private(candidate)
                if other.get('status') == 'released':
                    continue
                owned = other.get('runtime', {})
                require(not any(owned.get(key) == runtime[key] for key in
                        ('server_id', 'port_id', 'security_group_id', 'private_address')), 'runtime_already_registered')
        verify_runtime_registration(base, binding, runtime, call)
        target.mkdir(mode=0o700, exist_ok=True)
        require(target.resolve() == target, 'unsafe_environment_directory')
        private_stat(target.stat())
        routes = target / 'routes'
        routes.mkdir(mode=0o700, exist_ok=True)
        require(routes.resolve() == routes, 'unsafe_environment_directory')
        private_stat(routes.stat())
        full = {**base, 'runtime_private_address': runtime['private_address'], 'state_dir': str(routes),
                'network': {**base['network'], 'runtime_project_id': runtime['project_id'],
                            'runtime_server_id': runtime['server_id'], 'runtime_port_id': runtime['port_id'],
                            'runtime_security_group_id': runtime['security_group_id']}}
        checked_config(full)
        record = {'version': 1, 'status': 'active', 'binding': binding, 'runtime': runtime, 'config': full}
        if path.exists():
            require(read_private(path) == record, 'environment_binding_immutable')
        else:
            save(path, record)
        return {'status': 'registered', 'https_verified': False, 'binding': binding}
    finally:
        os.close(fd)


def environment_config(base_config, binding):
    binding = checked_worker_binding(binding)
    require(binding['base_sha256'] == lifecycle_hash(operator_base(base_config)), 'operator_base_changed')
    record = read_private(ENVIRONMENTS / binding['environment_id'] / 'binding.json')
    require(record.get('binding') == binding and record.get('version') == 1
            and record.get('status') == 'active', 'environment_binding_mismatch')
    config = checked_config(record['config'])
    require(config['state_dir'] == str(ENVIRONMENTS / binding['environment_id'] / 'routes')
            and operator_base(config) == operator_base(base_config)
            and registration_binding(operator_base(config), binding['environment_id'], binding['generation'],
                                     record['runtime']) == binding, 'environment_binding_mismatch')
    return config


def unregister_runtime(base_config, binding, call=None):
    call = run_cli if call is None else call
    binding = checked_worker_binding(binding)
    directory = environment_directory()
    fd = os.open(directory / 'registration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private_stat(os.fstat(fd))
        require(stat.S_ISREG(os.fstat(fd).st_mode), 'invalid_registration_lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        path = directory / binding['environment_id'] / 'binding.json'
        record = read_private(path)
        require(record.get('binding') == binding, 'environment_binding_mismatch')
        if record.get('status') != 'released':
            config = environment_config(base_config, binding)
            routes = Path(config['state_dir'])
            lifecycle_ready(routes)
            for app_path in routes.glob('app-*.json'):
                app = read_private(app_path)
                require(app.get('status') == 'deleted' and verified_deleted(config, app, routes),
                        'environment_routes_still_present')
                request = app['request']
                snapshot = lifecycle_observe(config, {key: request[key] for key in
                    ('application_id', 'hostname', 'node_port')}, routes, call)
                require(not snapshot['resources'], 'environment_routes_still_present')
            save(path, {**record, 'status': 'released'})
        return {'status': 'unregistered', 'https_verified': False, 'binding': binding}
    finally:
        os.close(fd)


def dispatch(config, request, call=None):
    call = run_cli if call is None else call
    operation = request.get('operation') if isinstance(request, dict) else None
    if operation == 'verify-runtime':
        require(set(request) == {'operation', 'binding', 'runtime'}, 'invalid_registration_request')
        base = operator_base(config)
        binding = checked_worker_binding(request['binding'])
        require(binding == registration_binding(base, binding['environment_id'], binding['generation'], request['runtime']),
                'operator_base_or_binding_changed')
        verify_runtime_registration(base, binding, request['runtime'], call)
        return {'status': 'verified', 'https_verified': False, 'binding': binding}
    if operation == 'register-runtime':
        return register_runtime(config, request, call)
    if operation in ('environment-route', 'environment-lifecycle', 'unregister-runtime'):
        expected = {'operation', 'binding'} | ({'request'} if operation != 'unregister-runtime' else set())
        require(set(request) == expected, 'invalid_environment_request')
        # Share the old ingress lock with legacy applications. Environment state
        # is separate, but every operation touches the same listener authority.
        if 'state_dir' in config:
            directory = Path(checked_config(config)['state_dir'])
        else:
            operator_base(config)
            directory = CONTROLLER_STATE
        require(directory.resolve() == directory, 'unsafe_state_directory')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_stat(directory.stat())
        fd = os.open(directory / (config['loadbalancer_id'] + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            private_stat(os.fstat(fd))
            require(stat.S_ISREG(os.fstat(fd).st_mode), 'invalid_lock_file')
            fcntl.flock(fd, fcntl.LOCK_EX)
            if operation == 'unregister-runtime':
                return unregister_runtime(config, request['binding'], call)
            selected = environment_config(config, request['binding'])
            if operation == 'environment-route':
                return configure(selected, request['request'], call)
            return lifecycle(selected, request['request'], call)
        finally:
            os.close(fd)
    return lifecycle(config, request, call) if operation else configure(config, request, call)


def private_stat(info):
    require(info.st_uid == ROOT_UID and not info.st_mode & 0o077, "insecure_private_file")


def read_private(path):
    require(path.is_absolute() and path == path.resolve(), "unsafe_private_path")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        private_stat(info)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 1048576,
                "invalid_private_file")
        return json.load(stream)


def save(path, record):
    fd, temporary = tempfile.mkstemp(prefix=".route-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_cli(arguments, service=("loadbalancer",)):
    try:
        result = subprocess.run(
            ["/usr/bin/python3", "-I", CLI, *service, *arguments],
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                 "HOME": "/root", "LANG": "C.UTF-8"},
            capture_output=True, text=True, timeout=180, check=True)
        return json.loads(result.stdout) if result.stdout.strip() else {}
    except (subprocess.SubprocessError, OSError, ValueError):
        # Provider output may contain authentication details. Never return it.
        raise RouteError("provider_response_unknown", "unknown") from None


def relation_ids(value):
    if isinstance(value, str):
        ids = value.splitlines()
        require(all(is_uuid(item) for item in ids), "invalid_provider_relation")
        return set(ids)
    require(isinstance(value, list) and all(isinstance(row, dict) and
            is_uuid(row.get("id")) for row in value), "invalid_provider_relation")
    return {row["id"] for row in value}


def verify_certificate(reference, hostname):
    require(is_uuid(reference), "local_listener_certificate_required")
    certificate = Path("/var/lib/octavia/certificates") / (reference + ".crt")
    ca = Path("/opt/railshot/octavia/origin-ca/ca.pem")
    try:
        octavia_uid = pwd.getpwnam("octavia").pw_uid
    except KeyError:
        octavia_uid = None
    try:
        for path in (certificate, ca):
            owners = {ROOT_UID, octavia_uid} if path == certificate else {ROOT_UID}
            parent = path.parent.lstat()
            require(stat.S_ISDIR(parent.st_mode) and parent.st_uid in owners and
                    not parent.st_mode & 0o022, "unsafe_certificate_directory")
            info = path.lstat()
            require(path.resolve() == path and stat.S_ISREG(info.st_mode) and
                    info.st_uid in owners and not info.st_mode & 0o022,
                    "unsafe_certificate_file")
        subprocess.run(["/usr/bin/openssl", "verify", "-CAfile", str(ca),
                        "-verify_hostname", hostname, "-purpose", "sslserver", str(certificate)],
                       capture_output=True, timeout=15, check=True)
    except (OSError, subprocess.SubprocessError):
        raise RouteError("hostname_certificate_not_ready") from None


def ensure_network_rule(config, request, record, path, call, observe_only=False):
    """One fixed amphora /32 -> one runtime TCP NodePort; no SG/port changes."""
    network = config["network"]
    group = network["runtime_security_group_id"]

    def native(arguments):
        return call([*arguments, "-f", "json"], service=())

    ports = []
    for role, project, address in (
            ("runtime", network["runtime_project_id"], request["private_address"]),
            ("amphora", config["project_id"], network["amphora_private_address"])):
        port = native(["port", "show", network[role + "_port_id"]])
        require(isinstance(port, dict) and port.get("id") == network[role + "_port_id"] and
                port.get("project_id") == project and port.get("device_id") == network[role + "_server_id"]
                and port.get("device_owner") == "compute:nova", "network_port_identity_mismatch")
        require(isinstance(port.get("fixed_ips"), list) and any(
            isinstance(ip, dict) and ip.get("ip_address") == address and
            ip.get("subnet_id") == config["member_subnet_id"] for ip in port["fixed_ips"]),
            "network_port_address_mismatch")
        ports.append(port)
    require(is_uuid(ports[0].get("network_id")) and ports[0]["network_id"] == ports[1].get("network_id")
            and isinstance(ports[0].get("security_group_ids"), list) and
            group in ports[0]["security_group_ids"], "runtime_security_group_attachment_mismatch")
    security_group = native(["security", "group", "show", group])
    require(isinstance(security_group, dict) and security_group.get("id") == group and
            security_group.get("project_id") == network["runtime_project_id"], "security_group_identity_mismatch")
    expected = {"security_group_id": group, "project_id": network["runtime_project_id"],
                "direction": "ingress", "protocol": "tcp", "port_range_min": request["node_port"],
                "port_range_max": request["node_port"],
                "remote_ip_prefix": network["amphora_private_address"] + "/32"}
    description = "railshot:" + request["application_id"]

    def exact(rule):
        return (isinstance(rule, dict) and all(rule.get(key) == value for key, value in expected.items())
                and rule.get("ether_type", rule.get("ethertype")) == "IPv4" and
                rule.get("remote_group_id") in (None, "") and rule.get("remote_address_group_id") in (None, ""))

    def find():
        rows = native(["security", "group", "rule", "list", group, "--protocol", "tcp",
                       "--ethertype", "IPv4", "--ingress", "-c", "ID"])
        require(isinstance(rows, list), "invalid_network_rule_list")
        found = []
        for row in rows:
            require(isinstance(row, dict) and is_uuid(row.get("ID")), "invalid_network_rule_id")
            rule = native(["security", "group", "rule", "show", row["ID"]])
            if exact(rule):
                require(rule.get("description") == description, "network_rule_owner_mismatch")
                found.append(row["ID"])
        require(len(found) <= 1, "ambiguous_network_rule")
        return found[0] if found else None

    step = record.get("network")
    rule_id = step.get("id") if step else None
    if not rule_id:
        require(not observe_only, "network_ownership_unrecorded")
        rule_id = find()
        if not rule_id:
            if step is not None:
                raise RouteError("network_creation_unresolved", "unknown")
            record["network"] = {"intent": True}
            save(path, record)
            try:
                response = native(["security", "group", "rule", "create", group, "--project", network["runtime_project_id"],
                                   "--ingress", "--ethertype", "IPv4", "--protocol", "tcp", "--remote-ip",
                                   expected["remote_ip_prefix"], "--dst-port", str(request["node_port"]),
                                   "--description", description])
                if isinstance(response, dict) and is_uuid(response.get("id")):
                    rule_id = response["id"]
                    record["network"]["id"] = rule_id
                    save(path, record)
            except RouteError:
                pass
            rule_id = rule_id or find()
            if not rule_id:
                raise RouteError("network_creation_unresolved", "unknown")
    require(is_uuid(rule_id), "invalid_network_rule_id")
    rule = native(["security", "group", "rule", "show", rule_id])
    require(rule.get("id") == rule_id and exact(rule) and rule.get("description") == description,
            "network_rule_readback_mismatch")
    record["network"] = {"intent": True, "id": rule_id}
    if not observe_only:
        save(path, record)
    return rule_id


def configure(config, request, call=run_cli):
    config = checked_config(config)
    request = checked_request(config, request)
    directory = Path(config["state_dir"])
    directory.mkdir(mode=0o700, exist_ok=True)
    private_stat(directory.stat())
    lock = os.open(directory / (config["loadbalancer_id"] + ".lock"),
                   os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private_stat(os.fstat(lock))
        require(stat.S_ISREG(os.fstat(lock).st_mode), "invalid_lock_file")
        fcntl.flock(lock, fcntl.LOCK_EX)
        lifecycle_ready(directory)
        return configure_locked(config, request, directory, call)
    finally:
        os.close(lock)


def configure_locked(config, request, directory, call, observe_only=False):
    app, lb, listener = request["application_id"], config["loadbalancer_id"], config["listener_id"]
    fingerprint = hashlib.sha256(json.dumps({"config": config, "request": request},
                                           sort_keys=True).encode()).hexdigest()
    tag = "railshot-" + fingerprint
    path = directory / (app + ".json")
    if path.exists():
        record = read_private(path)
        require(record.get("status") not in ("stopped", "deleted"), "application_lifecycle_action_required")
        if not observe_only and record.get("request") != request:
            return replace_health(config, request, record, fingerprint, directory, call)
        require(record.get("fingerprint") == fingerprint and record.get("request") == request,
                "application_intent_conflict")
    else:
        require(not observe_only, "application_ownership_unrecorded")
        record = {"fingerprint": fingerprint, "request": request, "steps": {}}
        for other in directory.glob("app-*.json"):
            previous_record = read_private(other)
            if verified_deleted(config, previous_record, directory):
                continue
            previous = previous_record["request"]
            require(previous["hostname"] != request["hostname"] and
                    previous["node_port"] != request["node_port"], "application_route_conflict")
        save(path, record)

    def show(kind, resource_id):
        command = {"monitor": "healthmonitor", "policy": "l7policy", "rule": "l7rule"}.get(kind, kind)
        parent = ([record["steps"]["pool"]["id"]] if kind == "member" else
                  [record["steps"]["policy"]["id"]] if kind == "rule" else [])
        return call([command, "show", *parent, resource_id, "-f", "json"])

    def matches(actual, expected):
        require(isinstance(actual, dict), "invalid_provider_response")
        for key, value in expected.items():
            found = actual.get(key)
            equal = (type(found) is bool and found == value) if type(value) is bool else str(found) == str(value)
            require(key in actual and equal, "resource_mismatch_" + key)

    tls_reference = None

    def preflight():
        matches(call(["show", lb, "-f", "json"]), {
            "id": lb, "project_id": config["project_id"],
            "admin_state_up": True, "provisioning_status": "ACTIVE"})
        existing = call(["listener", "show", listener, "-f", "json"])
        matches(existing, {"id": listener, "project_id": config["project_id"],
                           "protocol": "TERMINATED_HTTPS", "protocol_port": 443,
                           "admin_state_up": True, "provisioning_status": "ACTIVE"})
        require(relation_ids(existing.get("loadbalancers")) == {lb}, "listener_lb_mismatch")
        require("default_pool_id" in existing and existing["default_pool_id"] in (None, ""),
                "listener_has_default_pool")
        if tls_reference is not None:
            require(existing.get("default_tls_container_ref") == tls_reference,
                    "listener_certificate_changed")
        return existing

    def discover(kind):
        command = {"monitor": "healthmonitor", "policy": "l7policy", "rule": "l7rule"}.get(kind, kind)
        scope = {"pool": ["--loadbalancer", lb], "member": [record["steps"].get("pool", {}).get("id", "")],
                 "monitor": [], "policy": ["--listener", listener],
                 "rule": [record["steps"].get("policy", {}).get("id", "")]}
        rows = call([command, "list", *scope[kind], "--tags", tag, "-f", "json"])
        require(isinstance(rows, list) and len(rows) <= 1 and all(
            isinstance(row, dict) and isinstance(row.get("id"), str) for row in rows),
            "ambiguous_resource_discovery")
        return rows[0]["id"] if rows else None

    def verified(kind, resource_id):
        actual = show(kind, resource_id)
        expected = {"id": resource_id, "project_id": config["project_id"],
                    "provisioning_status": "ACTIVE"}
        if kind != "policy":
            expected["admin_state_up"] = True
        if kind != "rule":
            expected["name"] = app
        if kind == "pool":
            expected.update(protocol="HTTP", lb_algorithm="ROUND_ROBIN")
            require(relation_ids(actual.get("loadbalancers")) == {lb}, "pool_lb_mismatch")
        elif kind == "member":
            expected.update(address=request["private_address"], protocol_port=request["node_port"],
                            subnet_id=config["member_subnet_id"], weight=1, backup=False)
        elif kind == "monitor":
            expected.update(type="HTTP", delay=10, timeout=5, max_retries=3, http_method="GET",
                            http_version="1.1", domain_name=request["hostname"],
                            url_path=request["health_path"], expected_codes="200")
            require(relation_ids(actual.get("pools")) == {record["steps"]["pool"]["id"]},
                    "monitor_pool_mismatch")
        elif kind == "policy":
            expected.update(listener_id=listener, action="REDIRECT_TO_POOL",
                            redirect_pool_id=record["steps"]["pool"]["id"])
            require(type(actual.get("admin_state_up")) is bool, "invalid_policy_state")
            require(not actual["admin_state_up"] or record.get("activation_intent"),
                    "unexpected_enabled_policy")
        elif kind == "rule":
            expected.update(type="HOST_NAME", compare_type="EQUAL_TO", invert=False,
                            value=request["hostname"])
        matches(actual, expected)
        tags = actual.get("tags")
        tags = tags.splitlines() if isinstance(tags, str) else tags
        require(isinstance(tags, list) and tag in tags, "resource_owner_mismatch")
        return actual

    tls_reference = preflight().get("default_tls_container_ref")
    verify_certificate(tls_reference, request["hostname"])
    # Protect operator-created routes as well as applications in our journal.
    for existing in call(["l7policy", "list", "--listener", listener, "-f", "json"]):
        if existing["id"] == record["steps"].get("policy", {}).get("id") or existing.get("admin_state_up") is False:
            continue
        rules = call(["l7rule", "list", existing["id"], "-f", "json"])
        hosts = [rule for rule in rules if rule.get("type") == "HOST_NAME" and
                 rule.get("compare_type") == "EQUAL_TO" and rule.get("invert") is False]
        require(hosts and all(rule.get("value") != request["hostname"] for rule in hosts),
                "existing_listener_route_conflict")
    network_rule_id = ensure_network_rule(config, request, record, path, call, observe_only)
    for kind in KINDS:
        step = record["steps"].get(kind)
        if step is not None:
            resource_id = step.get("id") or discover(kind)
            if not resource_id:
                raise RouteError("creation_unresolved_" + kind, "unknown")
        else:
            require(not observe_only, "application_ownership_incomplete")
            require(not discover(kind), "unrecorded_resource_" + kind)
            preflight()
            pool = record["steps"].get("pool", {}).get("id", "")
            policy = record["steps"].get("policy", {}).get("id", "")
            commands = {
                "pool": ["pool", "create", "--name", app, "--protocol", "HTTP",
                         "--loadbalancer", lb, "--lb-algorithm", "ROUND_ROBIN", "--enable"],
                "member": ["member", "create", "--name", app, "--address", request["private_address"],
                           "--subnet-id", config["member_subnet_id"], "--protocol-port", str(request["node_port"]),
                           "--weight", "1", "--disable-backup", "--enable", pool],
                "monitor": ["healthmonitor", "create", "--name", app, "--type", "HTTP", "--delay", "10",
                            "--timeout", "5", "--max-retries", "3", "--http-method", "GET",
                            "--http-version", "1.1", "--domain-name", request["hostname"],
                            "--url-path", request["health_path"], "--expected-codes", "200", "--enable", pool],
                "policy": ["l7policy", "create", "--name", app, "--action", "REDIRECT_TO_POOL",
                           "--redirect-pool", pool, "--disable", listener],
                "rule": ["l7rule", "create", "--type", "HOST_NAME", "--compare-type", "EQUAL_TO",
                         "--value", request["hostname"], "--enable", policy],
            }
            record["steps"][kind] = {"intent": True}
            save(path, record)
            try:
                response = call([*commands[kind], "--tag", tag, "--wait", "-f", "json"])
                resource_id = response.get("id") if isinstance(response, dict) else None
            except RouteError:
                resource_id = None
            if not resource_id:
                resource_id = discover(kind)
                if not resource_id:
                    raise RouteError("creation_unresolved_" + kind, "unknown")
        require(is_uuid(resource_id), "invalid_resource_id")
        record["steps"][kind]["id"] = resource_id
        if not observe_only:
            save(path, record)
        verified(kind, resource_id)

    ids = {kind: record["steps"][kind]["id"] for kind in KINDS}
    pool, policy = verified("pool", ids["pool"]), verified("policy", ids["policy"])
    require(relation_ids(pool.get("members")) == {ids["member"]} and
            pool.get("healthmonitor_id") == ids["monitor"], "unexpected_pool_resources")
    require(relation_ids(policy.get("rules")) == {ids["rule"]}, "unexpected_policy_rules")
    if not policy["admin_state_up"]:
        require(not observe_only, "application_policy_disabled")
        if record.get("activation_intent"):
            raise RouteError("activation_unresolved", "unknown")
        preflight()
        record["activation_intent"] = True
        save(path, record)
        try:
            call(["l7policy", "set", "--enable", "--wait", ids["policy"]])
        except RouteError:
            pass
        if not verified("policy", ids["policy"])["admin_state_up"]:
            raise RouteError("activation_unresolved", "unknown")
    preflight()
    record["status"] = "configured"
    if not observe_only:
        save(path, record)
    return {**request, "status": "configured", "https_verified": False, "resources": ids,
            "network_rule_id": network_rule_id}


def lifecycle_ready(directory):
    for path in directory.glob("lifecycle-*.json"):
        require(read_private(path).get("phase") in ("planned", "succeeded"), "lifecycle_reconciliation_required")


def lifecycle_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verified_deleted(config, record, directory):
    """Release a reservation only with the successful absence-verification journal."""
    if record.get("status") != "deleted":
        return False
    request = {key: record["request"][key] for key in ("application_id", "hostname", "node_port")}
    for path in directory.glob("lifecycle-*.json"):
        journal = read_private(path)
        plan = journal.get("plan", {})
        snapshot = plan.get("snapshot", {})
        original = snapshot.get("record")
        if (journal.get("phase") == "succeeded" and is_uuid(journal.get("plan_id"))
                and path.name == "lifecycle-" + journal["plan_id"] + ".json"
                and journal.get("plan_sha256") == lifecycle_hash(plan)
                and plan.get("action") == "delete" and plan.get("request") == request
                and snapshot.get("config_sha256") == lifecycle_hash(config)
                and isinstance(original, dict) and record == {**original, "status": "deleted"}
                and journal.get("receipt") == {"status": "succeeded", "https_verified": False,
                    "action": "delete", "application_id": request["application_id"], "resources": {}}):
            return True
    return False


def lifecycle_observe(config, request, directory, call):
    """Reuse every existing project/tag/network/relationship check without saving."""
    app = request["application_id"]
    path = directory / (app + ".json")
    record = read_private(path) if path.exists() else None
    if record:
        require(all(record["request"].get(k) == v for k, v in request.items()), "lifecycle_binding_changed")
        checked_request(config, record["request"])
        require(record.get("status") in ("configured", "stopped", "deleted"), "application_creation_unresolved")
    if not record or record["status"] in ("stopped", "deleted"):
        for command, scope in (("pool", ["--loadbalancer", config["loadbalancer_id"]]),
                               ("l7policy", ["--listener", config["listener_id"]]), ("healthmonitor", [])):
            rows = call([command, "list", *scope, "-f", "json"])
            require(isinstance(rows, list) and not any(r.get("name") == app for r in rows), "unrecorded_application_resources")
        rules = call(["security", "group", "rule", "list", config["network"]["runtime_security_group_id"],
                      "-c", "ID", "-f", "json"], service=())
        require(isinstance(rules, list), "invalid_network_rule_list")
        for row in rules:
            require(is_uuid(row.get("ID")), "invalid_network_rule_id")
            rule = call(["security", "group", "rule", "show", row["ID"], "-f", "json"], service=())
            require(rule.get("description") != "railshot:" + app, "unrecorded_application_network_rule")
        return {"config_sha256": lifecycle_hash(config), "record": record, "resources": {}}
    observations = []
    def stable(value):
        if isinstance(value, dict):
            return {k: stable(v) for k, v in value.items() if k not in ("operating_status", "updated_at", "created_at")}
        if isinstance(value, list):
            return [stable(v) for v in value]
        return value
    def observe(arguments, service=("loadbalancer",)):
        result = call(arguments, service=service)
        observations.append({"arguments": arguments, "service": list(service), "value": stable(result)})
        return result
    result = configure_locked(config, record["request"], directory, observe, observe_only=True)
    return {"config_sha256": lifecycle_hash(config), "record": record, "resources": result["resources"],
            "network_rule_id": result["network_rule_id"], "observations": observations}


def lifecycle_apply(config, request, directory, snapshot, action, call):
    """Caller holds the LB lock and has saved an intent for this verified snapshot."""
    record = snapshot["record"]
    app_path = directory / (request["application_id"] + ".json")
    if action == "start":
        # Reset creation intents only after verified absence, never after an
        # uncertain creation. Both lifecycle and health replacement use this path.
        save(app_path, {k: v for k, v in record.items() if k in ("fingerprint", "request")} | {"steps": {}})
        configure_locked(config, record["request"], directory, call)
        result = lifecycle_observe(config, request, directory, call)
        require(len(result["resources"]) == 5 and is_uuid(result.get("network_rule_id")), "application_restore_unverified")
    elif snapshot["resources"]:
        ids = snapshot["resources"]
        for kind in ("rule", "policy", "monitor", "member", "pool"):
            command = {"monitor": "healthmonitor", "policy": "l7policy", "rule": "l7rule"}.get(kind, kind)
            parent = [ids["policy"]] if kind == "rule" else [ids["pool"]] if kind == "member" else []
            call([command, "delete", *parent, ids[kind], "--wait"])
            scope = parent or (["--listener", config["listener_id"]] if kind == "policy" else
                               ["--loadbalancer", config["loadbalancer_id"]] if kind == "pool" else [])
            rows = call([command, "list", *scope, "-f", "json"])
            require(isinstance(rows, list) and not any(row.get("id") == ids[kind] for row in rows), "application_delete_unverified")
        rule_id = snapshot["network_rule_id"]
        call(["security", "group", "rule", "delete", rule_id], service=())
        rows = call(["security", "group", "rule", "list", config["network"]["runtime_security_group_id"],
                     "-c", "ID", "-f", "json"], service=())
        require(isinstance(rows, list) and not any(row.get("ID") == rule_id for row in rows), "network_delete_unverified")
        save(app_path, {**record, "status": "stopped" if action == "stop" else "deleted"})
        result = lifecycle_observe(config, request, directory, call)
    else:
        if record and action == "delete":
            save(app_path, {**record, "status": "deleted"})
        result = lifecycle_observe(config, request, directory, call)
    return result


def replace_health(config, request, record, fingerprint, directory, call):
    """One health-only stop/start, with the old ownership and new request journaled."""
    # ponytail: recreation interrupts this app's route; use journaled monitor updates if continuity is required.
    require(record.get("status") == "configured" and
            {**record["request"], "health_path": request["health_path"]} == request, "application_intent_conflict")
    snapshot = lifecycle_observe(config, record["request"], directory, call)
    plan = {"action": "health-update", "request": request, "snapshot": snapshot}
    plan_id = str(uuid.uuid4())
    path = directory / ("lifecycle-" + plan_id + ".json")
    journal = {"phase": "applying", "plan_id": plan_id, "plan_sha256": lifecycle_hash(plan), "plan": plan}
    save(path, journal)
    try:
        stopped = lifecycle_apply(config, record["request"], directory, snapshot, "stop", call)
        # The old resources are now verified absent. The journal retains the old
        # request; only the replacement gets a new fingerprint and creation intents.
        stopped["record"] = {**stopped["record"], "request": request, "fingerprint": fingerprint}
        save(directory / (request["application_id"] + ".json"), stopped["record"])
        result = lifecycle_apply(config, request, directory, stopped, "start", call)
        receipt = {**request, "status": "configured", "https_verified": False,
                   "resources": result["resources"], "network_rule_id": result["network_rule_id"]}
        save(path, {**journal, "phase": "succeeded", "receipt": receipt})
        return receipt
    except BaseException:
        try:
            save(path, {**journal, "phase": "unknown"})
        finally:
            raise RouteError("health_update_outcome_unknown", "unknown") from None


def lifecycle(config, envelope, call=run_cli):
    config = checked_config(config)
    require(isinstance(envelope, dict) and set(envelope) <= {"operation", "action", "request", "expected"}
            and envelope.get("operation") in ("lifecycle-plan", "lifecycle-validate", "lifecycle-execute")
            and envelope.get("action") in ("start", "stop", "delete"), "invalid_lifecycle_request")
    request, action = envelope["request"], envelope["action"]
    require(isinstance(request, dict) and set(request) == {"application_id", "hostname", "node_port"}, "invalid_lifecycle_binding")
    checked_request(config, {**request, "private_address": config["runtime_private_address"], "health_path": "/"})
    directory = Path(config["state_dir"]); directory.mkdir(mode=0o700, exist_ok=True)
    private_stat(directory.stat())
    fd = os.open(directory / (config["loadbalancer_id"] + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private_stat(os.fstat(fd)); require(stat.S_ISREG(os.fstat(fd).st_mode), "invalid_lock_file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        if envelope["operation"] != "lifecycle-plan":
            expected = envelope.get("expected", {})
            require(isinstance(expected, dict) and set(expected) == {"plan_id", "plan_sha256"}
                    and is_uuid(expected["plan_id"]), "reviewed_lifecycle_plan_required")
            path = directory / ("lifecycle-" + expected["plan_id"] + ".json")
            journal = read_private(path)
            plan = journal.get("plan", {})
            require(all(journal.get(k) == v for k, v in expected.items())
                    and plan.get("action") == action and plan.get("request") == request
                    and plan.get("snapshot", {}).get("config_sha256") == lifecycle_hash(config)
                    and lifecycle_hash(plan) == expected["plan_sha256"], "lifecycle_plan_stale")
            if journal.get("phase") == "succeeded":
                receipt = journal.get("receipt", {})
                require(receipt.get("status") == "succeeded" and receipt.get("https_verified") is False
                        and receipt.get("action") == action and receipt.get("application_id") == request["application_id"]
                        and isinstance(receipt.get("resources"), dict), "lifecycle_receipt_invalid")
                # A historical result is not a new live observation or another mutation.
                return receipt
        lifecycle_ready(directory)
        snapshot = lifecycle_observe(config, request, directory, call)
        if action == "start":
            require(snapshot["record"] and snapshot["record"].get("status") == "stopped", "only_stopped_application_can_start")
        if envelope["operation"] == "lifecycle-plan":
            plan_id = str(uuid.uuid4())
            plan = {"action": action, "request": request, "snapshot": snapshot}
            expected = {"plan_id": plan_id, "plan_sha256": lifecycle_hash(plan)}
            save(directory / ("lifecycle-" + plan_id + ".json"), {"phase": "planned", "plan": plan, **expected})
            return {"status": "succeeded", "https_verified": False, "expected": expected,
                    "resources": snapshot["resources"], "network_rule_id": snapshot.get("network_rule_id"),
                    "health_path": snapshot["record"]["request"]["health_path"] if snapshot["record"] else None}
        require(journal["phase"] == "planned" and
                journal["plan"] == {"action": action, "request": request, "snapshot": snapshot}, "lifecycle_plan_stale")
        if envelope["operation"] == "lifecycle-validate":
            return {"status": "succeeded", "https_verified": False}
        save(path, {**journal, "phase": "applying"})
        try:
            result = lifecycle_apply(config, request, directory, snapshot, action, call)
            receipt = {"status": "succeeded", "https_verified": False, "action": action,
                       "application_id": request["application_id"], "resources": result["resources"]}
            save(path, {**journal, "phase": "succeeded", "receipt": receipt})
            return receipt
        except BaseException:
            save(path, {**journal, "phase": "unknown"})
            raise RouteError("lifecycle_outcome_unknown", "unknown") from None
    finally:
        os.close(fd)


def main():
    try:
        require(os.geteuid() == ROOT_UID, "root_required")
        require(len(sys.argv) == 1, "arguments_not_supported")
        config = read_private(CONFIG)
        raw = sys.stdin.read(16385)
        require(len(raw) <= 16384, "request_too_large")
        request = json.loads(raw)
        result = dispatch(config, request)
    except RouteError as error:
        result = {"status": error.status, "reason": str(error), "https_verified": False}
    except Exception:
        # Includes corrupt private state and native errors; no traceback/CLI data.
        result = {"status": "blocked", "reason": "invalid_local_state", "https_verified": False}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in ("configured", "succeeded", "verified", "registered", "unregistered") else 1


if __name__ == "__main__":
    sys.exit(main())
