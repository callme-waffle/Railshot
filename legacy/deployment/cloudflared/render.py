#!/usr/bin/env python3
"""Render a locally managed Named Tunnel connector. No API calls or secret input."""
import argparse
import hashlib
import ipaddress
import json
import re
import sys
import uuid

# Official 2026.9.3 multiarch index, verified against Docker Hub on 2026-10-02.
IMAGE = 'docker.io/cloudflare/cloudflared:2026.9.3@sha256:072c067d25ccbe61d46e18f0d0723255f2bb5304f7317caa95b27031520ff92c'
BINARY = '/usr/local/bin/cloudflared'
CONFIG_PATH = '/etc/cloudflared/config/config.json'
CREDENTIALS_PATH = '/etc/cloudflared/credentials/credentials.json'
CA_PATH = '/etc/cloudflared/ca/ca.pem'
METRICS = '127.0.0.1:2000'
LABEL = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def dns_label(value, field):
    if not isinstance(value, str) or not LABEL.fullmatch(value):
        raise ValueError(f'{field} must be a lowercase DNS label (1-63 characters)')


def render(*, namespace, name, tunnel_id, credentials_secret, hostnames, origin_vip, ca_configmap=None):
    for field, value in [('namespace', namespace), ('name', name), ('credentials-secret', credentials_secret)]:
        dns_label(value, field)
    if len(name) > 56:
        raise ValueError('name must be at most 56 characters to leave room for the ConfigMap suffix')
    if ca_configmap is not None:
        dns_label(ca_configmap, 'ca-configmap')
    try:
        if str(uuid.UUID(tunnel_id)) != tunnel_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError('tunnel-id must be a canonical lowercase UUID') from None
    try:
        address = ipaddress.IPv4Address(origin_vip)
        if not any(address in network for network in PRIVATE_NETWORKS):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('origin-vip must be an RFC1918 IPv4 address; URLs, public, metadata and CGNAT addresses are forbidden') from None
    if not isinstance(hostnames, list) or not 0 <= len(hostnames) <= 50:
        raise ValueError('provide 0-50 distinct hostnames')
    for host in hostnames:
        if (not isinstance(host, str) or len(host) > 253 or '.' not in host
                or not all(LABEL.fullmatch(part) for part in host.split('.'))):
            raise ValueError('hostname must be an exact lowercase DNS name without wildcard, URL, port or trailing dot')
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError('hostname must be a DNS name, not an IP address')
    if len(set(hostnames)) != len(hostnames):
        raise ValueError('duplicate hostnames are forbidden')

    ingress = []
    for host in sorted(hostnames):
        origin = {'originServerName': host, 'httpHostHeader': host, 'noTLSVerify': False}
        if ca_configmap:
            origin['caPool'] = CA_PATH
        ingress.append({'hostname': host, 'service': f'https://{address}:443', 'originRequest': origin})
    ingress.append({'service': 'http_status:404'})
    config = {'tunnel': tunnel_id, 'credentials-file': CREDENTIALS_PATH, 'ingress': ingress}
    # JSON is valid YAML and is accepted by cloudflared's native config parser.
    config_text = json.dumps(config, indent=2, sort_keys=True) + '\n'
    labels = {'app.kubernetes.io/name': 'cloudflared', 'app.kubernetes.io/instance': name}

    def metadata(resource_name):
        return {'name': resource_name, 'namespace': namespace, 'labels': dict(labels)}

    volumes = [
        {'name': 'config', 'configMap': {'name': f'{name}-config', 'defaultMode': 0o444}},
        {'name': 'credentials', 'secret': {'secretName': credentials_secret, 'defaultMode': 0o440,
                                         'items': [{'key': 'credentials.json', 'path': 'credentials.json'}]}},
    ]
    mounts = [
        {'name': 'config', 'mountPath': '/etc/cloudflared/config', 'readOnly': True},
        {'name': 'credentials', 'mountPath': '/etc/cloudflared/credentials', 'readOnly': True},
    ]
    if ca_configmap:
        volumes.append({'name': 'ca', 'configMap': {'name': ca_configmap, 'defaultMode': 0o444,
                                                  'items': [{'key': 'ca.pem', 'path': 'ca.pem'}]}})
        mounts.append({'name': 'ca', 'mountPath': '/etc/cloudflared/ca', 'readOnly': True})
    container = {
        'name': 'cloudflared', 'image': IMAGE, 'imagePullPolicy': 'IfNotPresent',
        'command': [BINARY, '--no-autoupdate'],
        'args': ['tunnel', '--config', CONFIG_PATH, '--metrics', METRICS, 'run'],
        'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                            'capabilities': {'drop': ['ALL']}},
        'resources': {'requests': {'cpu': '100m', 'memory': '128Mi'},
                      'limits': {'cpu': '500m', 'memory': '256Mi'}},
        'volumeMounts': mounts,
        'readinessProbe': {'exec': {'command': [BINARY, 'tunnel', '--metrics', METRICS, 'ready']},
                           'initialDelaySeconds': 5, 'periodSeconds': 10, 'timeoutSeconds': 2,
                           'failureThreshold': 3},
    }
    return {'apiVersion': 'v1', 'kind': 'List', 'items': [
        {'apiVersion': 'v1', 'kind': 'ServiceAccount', 'metadata': metadata(name),
         'automountServiceAccountToken': False},
        {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': metadata(f'{name}-config'),
         'data': {'config.json': config_text}},
        {'apiVersion': 'apps/v1', 'kind': 'Deployment', 'metadata': metadata(name), 'spec': {
            # ponytail: one replica and Recreate intentionally bound this PoC;
            # do not imply HA or keep old ingress rules alive during a rollout.
            'replicas': 1, 'strategy': {'type': 'Recreate'}, 'revisionHistoryLimit': 2,
            'selector': {'matchLabels': labels},
            'template': {'metadata': {'labels': labels, 'annotations': {
                'railshot.io/config-sha256': hashlib.sha256(config_text.encode()).hexdigest(),
            }}, 'spec': {
                'serviceAccountName': name, 'automountServiceAccountToken': False,
                'hostNetwork': False, 'hostPID': False, 'hostIPC': False,
                'securityContext': {'runAsNonRoot': True, 'runAsUser': 65532, 'runAsGroup': 65532,
                                    'fsGroup': 65532, 'seccompProfile': {'type': 'RuntimeDefault'}},
                'terminationGracePeriodSeconds': 60, 'containers': [container], 'volumes': volumes,
            }},
        }},
    ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--name', default='railshot-tunnel')
    parser.add_argument('--tunnel-id', required=True)
    parser.add_argument('--credentials-secret', required=True)
    parser.add_argument('--hostname', action='append', dest='hostnames', required=True)
    parser.add_argument('--origin-vip', required=True, help='RFC1918 IPv4 only; HTTPS port 443 is fixed')
    parser.add_argument('--ca-configmap', help='Existing same-namespace ConfigMap with ca.pem for a private origin CA')
    args = parser.parse_args()
    try:
        result = render(**vars(args))
    except ValueError as exc:
        parser.error(str(exc))
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
