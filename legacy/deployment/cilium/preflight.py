"""Read-only guard for customer and operations K3s Cilium profiles."""
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def check_host(role, root=Path('/')):
    if role not in ('customer', 'control'):
        raise ValueError('expected customer or control profile')
    pod_cidr, service_cidr = ('10.52.0.0/16', '10.53.0.0/16') if role == 'control' else ('10.42.0.0/16', '10.43.0.0/16')
    marker = root / 'etc/railshot/node-role'
    if (role == 'control' and (not marker.is_file() or marker.read_text().strip() != 'control')
            or role == 'customer' and marker.exists() and marker.read_text().strip() != 'customer'):
        raise ValueError('node role differs; customer and control installers cannot be mixed')
    config_dir = root / 'etc/rancher/k3s'
    if (config_dir / 'config.yaml.d').exists():
        raise ValueError('K3s config.yaml.d is not supported')
    config = (config_dir / 'config.yaml').read_text()
    # Only accept the literal scalars emitted by our shell/Ansible installers.
    # This is deliberately not a YAML parser; aliases/duplicate keys fail closed.
    for line in config.splitlines():
        if line.strip() and not line.lstrip().startswith('#'):
            if not re.fullmatch(r'(?:[a-z][a-z0-9-]*:.*|\s*- [^&*{}\[\]]+)', line) or re.search(r'[: ]+[&*]|^<<:', line):
                raise ValueError('unsupported K3s YAML; use the literal managed configuration')
    for key, expected in {'flannel-backend': 'none', 'disable-network-policy': 'true',
                          'cluster-cidr': pod_cidr, 'service-cidr': service_cidr,
                          'disable-kube-proxy': 'false', 'egress-selector-mode': 'agent'}.items():
        values = re.findall(r'^' + key + r':[ \t]*([^\n]*)$', config, re.MULTILINE)
        if not values and key in ('disable-kube-proxy', 'egress-selector-mode'):
            continue
        if len(values) != 1 or values[0].strip() not in (expected, f'"{expected}"', f"'{expected}'"):
            raise ValueError(f'K3s {key} differs; automatic CNI migration is refused')
    check_cni(root)
    return pod_cidr, service_cidr


def check_cni(root=Path('/')):
    # With Flannel disabled, K3s uses containerd's /etc/cni/net.d default.
    # Also reject remnants in the former Flannel directory before migration.
    for directory in ('etc/cni/net.d', 'var/lib/rancher/k3s/agent/etc/cni/net.d'):
        for path in (root / directory).glob('*'):
            if path.suffix not in ('.conf', '.conflist', '.json'):
                continue
            cni = json.loads(path.read_text())
            plugins = cni.get('plugins', [cni])
            if cni.get('name') != 'cilium' or not plugins or any(p.get('type') != 'cilium-cni' for p in plugins):
                raise ValueError(f'existing non-Cilium CNI: {path.name}; use the migration runbook')
    for interface in ('flannel.1', 'cni0'):
        if (root / 'sys/class/net' / interface).exists():
            raise ValueError(f'existing {interface}; use the migration runbook')


def kube(*args):
    raw = subprocess.check_output(['/usr/local/bin/k3s', 'kubectl', '--request-timeout=10s', *args], text=True)
    return json.loads(raw) if raw.strip() else None


def check_cluster(pod_cidr, service_cidr, wait_seconds=300, profile='customer', expected_agent=None):
    if profile not in ('customer', 'control'):
        raise ValueError('expected customer or control profile')
    pod_pool, service_pool = ipaddress.ip_network(pod_cidr), ipaddress.ip_network(service_cidr)
    if pod_pool.version != 4 or service_pool.version != 4 or pod_pool.overlaps(service_pool):
        raise ValueError('profile requires non-overlapping IPv4 Pod and Service CIDRs')
    # /readyz can precede kubelet registration, the K3s server-role label patch,
    # and controller CIDR allocation.
    # Wait for those API objects only; NodeReady itself needs Cilium.
    deadline = time.monotonic() + wait_seconds
    while True:
        nodes = kube('get', 'nodes', '-o', 'json')['items']
        if profile == 'customer' and len(nodes) > 1:
            raise ValueError('exactly one customer K3s node is supported')
        selected = nodes[0] if nodes else None
        if profile == 'control' and nodes:
            server_roles = {'node-role.kubernetes.io/control-plane', 'node-role.kubernetes.io/master'}
            servers = [node for node in nodes if server_roles & set(node.get('metadata', {}).get('labels', {}))]
            if len(nodes) > 2 or len(servers) > 1:
                raise ValueError('control requires one server and at most one approved build agent')
            # A newly registered server can have a PodCIDR before K3s patches
            # its role label. Missing identity is pending, never authorization.
            selected = servers[0] if servers else None
            if selected is not None:
                if (selected['metadata']['labels'].get('railshot.io/node-role') == 'build'
                        or any(t.get('key') == 'railshot.io/dedicated' and t.get('value') == 'build'
                               for t in selected.get('spec', {}).get('taints', []))):
                    raise ValueError('control-plane server cannot also be the dedicated build worker')
                for node in nodes:
                    if node is selected:
                        continue
                    labels = node.get('metadata', {}).get('labels', {})
                    dedicated = {'key': 'railshot.io/dedicated', 'value': 'build', 'effect': 'NoSchedule'}
                    taints = [t for t in node.get('spec', {}).get('taints', []) if t.get('key') == dedicated['key']]
                    if (labels.get('railshot.io/node-role') != 'build'
                            or 'node-role.kubernetes.io/etcd' in labels or taints != [dedicated]):
                        raise ValueError('additional control node must be the approved tainted build agent')
        if selected is not None and all(node.get('spec', {}).get('podCIDR') for node in nodes):
            break
        if time.monotonic() >= deadline:
            raise ValueError('timed out waiting for node registration/PodCIDR or confirmed server role')
        time.sleep(3)
    allocations = []
    for node in nodes:
        cidr = node['spec']['podCIDR']
        allocation = ipaddress.ip_network(cidr)
        if (allocation.version != 4 or not allocation.subnet_of(pod_pool)
                or node['spec'].get('podCIDRs', [cidr]) != [cidr]):
            raise ValueError('live node PodCIDR differs from selected IPv4 profile')
        if any(allocation.overlaps(other) for other in allocations):
            raise ValueError('live node PodCIDRs overlap')
        allocations.append(allocation)
    service = kube('-n', 'default', 'get', 'service', 'kubernetes', '-o', 'json')
    if ipaddress.ip_address(service['spec']['clusterIP']) not in service_pool:
        raise ValueError('live Kubernetes Service CIDR differs from selected profile')
    config = kube('-n', 'kube-system', 'get', 'configmap', 'cilium-config', '--ignore-not-found', '-o', 'json')
    if config:
        if config['metadata'].get('annotations', {}).get('meta.helm.sh/release-name') != 'cilium':
            raise ValueError('existing Cilium is not owned by the cilium Helm release')
        for key, value in {'cluster-pool-ipv4-cidr': pod_cidr, 'cluster-pool-ipv4-mask-size': '24',
                           'ipam': 'cluster-pool', 'kube-proxy-replacement': 'false',
                           'routing-mode': 'tunnel', 'tunnel-protocol': 'vxlan', 'enable-hubble': 'false'}.items():
            if config['data'].get(key) != value:
                raise ValueError(f'existing Cilium {key} differs; implicit migration/upgrade refused')
    daemon = kube('-n', 'kube-system', 'get', 'daemonset', 'cilium', '--ignore-not-found', '-o', 'json')
    if daemon:
        pinned = expected_agent or json.loads((Path(__file__).resolve().parents[1] / 'airgap/versions.json').read_text())['cilium_images']['agent']
        # This guard also runs standalone on the node, before any upgrade code is installed.
        images = [re.sub(r':[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}(?=@sha256:[a-f0-9]{64}$)', '', c['image'])
                  for c in daemon['spec']['template']['spec']['containers'] if c['name'] == 'cilium-agent']
        if not config or images != [pinned]:
            raise ValueError('existing Cilium agent differs from the pinned release; explicit upgrade required')
    # The operations smoke uses this validated server, not NodeList ordering.
    return selected


if __name__ == '__main__':
    try:
        versions = json.loads((Path(__file__).resolve().parents[1] / 'airgap/versions.json').read_text())['runtime']
        if any(os.environ.get(key.upper()) != value for key, value in versions.items()):
            raise ValueError('runtime versions differ from the pinned image policy')
        profile = sys.argv[1] if len(sys.argv) == 2 else ''
        pod, service = check_host(profile)
        timeout = os.environ.get('WAIT_TIMEOUT', '300s')
        if not re.fullmatch(r'[1-9][0-9]*s', timeout):
            raise ValueError('WAIT_TIMEOUT must be seconds, e.g. 300s')
        check_cluster(pod, service, int(timeout[:-1]), profile=profile)
        print(pod)
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        sys.exit(f'Cilium preflight refused: {exc}')
