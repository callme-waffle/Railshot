"""JSON boundary only. No provider vocabulary is passed to the engine."""
import ipaddress
import re
from urllib.parse import urlsplit
from models import AdaptedRequest, DeploymentSpec, ExposureSpec, RequestContext, RuntimeSpec, WorkloadSpec


class InputError(ValueError):
    pass


def obj(value, path, allowed, required=()):
    if not isinstance(value, dict):
        raise InputError(f'{path}: expected object')
    extra = value.keys() - set(allowed)
    missing = set(required) - value.keys()
    if extra or missing:
        raise InputError(f'{path}: unknown={sorted(extra)}, missing={sorted(missing)}')
    return value


def string(value, path, pattern=None, maximum=253):
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise InputError(f'{path}: expected nonempty string of at most {maximum} characters')
    if pattern and not re.fullmatch(pattern, value):
        raise InputError(f'{path}: invalid format')
    return value


def integer(value, path, low, high):
    if type(value) is not int or not low <= value <= high:
        raise InputError(f'{path}: expected integer {low}..{high}')
    return value


LABEL = r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?'
IMAGE = r'[a-z0-9][a-z0-9./:_-]*(?:@sha256:[a-f0-9]{64})?'


def parse_input(data):
    obj(data, 'input', ('schema_version', 'provider', 'environment_id', 'node', 'workload', 'exposure', 'runtime'),
        ('provider', 'environment_id', 'node', 'workload'))
    schema_version = data.get('schema_version', '0.1')
    if schema_version not in ('0.1', '0.2'):
        raise InputError('schema_version: expected 0.1 or 0.2 (provisional contract)')
    provider = data['provider']
    if provider not in ('aws', 'gcp', 'openstack'):
        raise InputError('provider: expected aws, gcp or openstack')
    environment = string(data['environment_id'], 'environment_id', LABEL, 63)
    node = obj(data['node'], 'node', ('host', 'ssh_user'), ('host',))
    host = string(node['host'], 'node.host', r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?')
    user = node.get('ssh_user')
    if user is not None:
        user = string(user, 'node.ssh_user', r'[a-z_][a-z0-9_-]*', 32)
    work = obj(data['workload'], 'workload', ('image', 'namespace', 'replicas', 'container_port', 'health_path', 'sample_content'),
               ('image', 'namespace'))
    image = string(work['image'], 'workload.image', IMAGE, 512)
    # Require an explicit tag or digest. Reject implicit latest before touching Linux.
    if '@sha256:' not in image and ':' not in image.rsplit('/', 1)[-1]:
        raise InputError('workload.image: explicit tag or sha256 digest is required')
    namespace = string(work['namespace'], 'workload.namespace', LABEL, 63)
    if namespace in ('default', 'kube-system', 'kube-public', 'kube-node-lease'):
        raise InputError('workload.namespace: reserved namespace')
    replicas = integer(work.get('replicas', 1), 'workload.replicas', 1, 10)
    port = integer(work.get('container_port', 80), 'workload.container_port', 1, 65535)
    health = string(work.get('health_path', '/'), 'workload.health_path', r'/[^\s?#]*', 256)
    sample = work.get('sample_content', False)
    if type(sample) is not bool:
        raise InputError('workload.sample_content: expected boolean')
    if sample and (port != 80 or health != '/' or not re.fullmatch(r'(?:docker.io/library/)?nginx(?::[A-Za-z0-9_.-]+)?(?:@sha256:[a-f0-9]{64})?', image)):
        raise InputError('sample_content is only for explicitly tagged or digest-pinned nginx on port 80 with health_path /')
    exposure = obj(data.get('exposure', {}), 'exposure', ('type', 'node_port', 'verification_url', 'public_url'))
    exposure_type = exposure.get('type', 'nodeport')
    if exposure_type not in ('nodeport', 'cloudflare-tunnel'):
        raise InputError('exposure.type: expected nodeport or cloudflare-tunnel (existing URL hook only)')
    if schema_version == '0.1' and (exposure_type != 'nodeport' or 'public_url' in exposure):
        raise InputError('extended exposure requires schema_version 0.2')
    node_port = integer(exposure.get('node_port', 30080), 'exposure.node_port', 30000, 32767)
    url = exposure.get('verification_url')
    if url is not None:
        string(url, 'exposure.verification_url', maximum=1024)
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username
                     and not parsed.password and not parsed.query and not parsed.fragment and parsed.port != 0)
        except ValueError:
            valid = False
        if not valid or any(c.isspace() for c in url):
            raise InputError('exposure.verification_url: expected HTTP(S) URL without credentials/query/fragment')
    public_url = exposure.get('public_url')
    if public_url is not None:
        string(public_url, 'exposure.public_url', maximum=1024)
        try:
            parsed = urlsplit(public_url)
            parsed.port
        except ValueError:
            raise InputError('exposure.public_url: invalid URL/port') from None
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ('', '/') or any(c.isspace() for c in public_url)):
            raise InputError('exposure.public_url: expected HTTPS base URL without credentials/query/path')
    legacy_runtime = ('k3s_version', 'cilium_version', 'cilium_cli_version', 'timeout_seconds', 'node_ip')
    new_runtime = ('mode', 'bundle_path', 'bundle_sha256', 'preflight_timeout_seconds', 'endpoint_overrides')
    runtime = obj(data.get('runtime', {}), 'runtime', legacy_runtime + new_runtime)
    if schema_version == '0.1' and any(k in runtime for k in new_runtime):
        raise InputError('extended runtime options require schema_version 0.2 or CLI --mode/--bundle')
    defaults = RuntimeSpec()
    versions = [string(runtime.get(k, getattr(defaults, k)), f'runtime.{k}', pattern, 64) for k, pattern in (
        ('k3s_version', r'v[0-9]+\.[0-9]+\.[0-9]+\+k3s[0-9]+'),
        ('cilium_version', r'[0-9]+\.[0-9]+\.[0-9]+'),
        ('cilium_cli_version', r'v[0-9]+\.[0-9]+\.[0-9]+'))]
    timeout = integer(runtime.get('timeout_seconds', 180), 'runtime.timeout_seconds', 5, 900)
    node_ip = runtime.get('node_ip')
    if node_ip is not None:
        try:
            if not isinstance(node_ip, str):
                raise ValueError()
            ipaddress.IPv4Address(node_ip)
        except ValueError:
            raise InputError('runtime.node_ip: expected IPv4 address') from None
    mode = runtime.get('mode', 'auto')
    if mode not in ('auto', 'online', 'offline'):
        raise InputError('runtime.mode: expected auto, online or offline')
    bundle = runtime.get('bundle_path')
    if bundle is not None:
        string(bundle, 'runtime.bundle_path', maximum=4096)
        if not bundle.startswith('/') or '\x00' in bundle:
            raise InputError('runtime.bundle_path: expected absolute local directory')
    checksum = runtime.get('bundle_sha256')
    if checksum is not None:
        string(checksum, 'runtime.bundle_sha256', r'[a-f0-9]{64}', 64)
        if not bundle:
            raise InputError('runtime.bundle_sha256 requires bundle_path')
    probe_timeout = integer(runtime.get('preflight_timeout_seconds', 3), 'runtime.preflight_timeout_seconds', 1, 10)
    overrides = runtime.get('endpoint_overrides', {})
    allowed = ('k3s_source', 'github', 'cilium_cli_source', 'cilium_chart', 'quay', 'registry_k8s', 'docker_hub', 'ghcr', 'workload_registry', 'cloudflare_tunnel')
    obj(overrides, 'runtime.endpoint_overrides', allowed)
    for key, value in overrides.items():
        string(value, 'runtime.endpoint_overrides.' + key, maximum=1024)
        try:
            parsed = urlsplit(value)
            parsed.port
        except ValueError:
            raise InputError('endpoint override: invalid URL/port') from None
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query
                or parsed.fragment or any(c.isspace() for c in value)):
            raise InputError('endpoint overrides must be HTTPS URLs without credentials/query/fragment')
        try:
            parsed.port
        except ValueError:
            raise InputError('endpoint override: invalid port') from None
    return AdaptedRequest(DeploymentSpec(environment, WorkloadSpec(image, namespace, replicas, port, health, sample),
                                        RuntimeSpec(*versions, timeout, node_ip, mode, bundle, checksum, probe_timeout, overrides),
                                        ExposureSpec(node_port, url, exposure_type, public_url)),
                          RequestContext(provider, host, user, schema_version))
