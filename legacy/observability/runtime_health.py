"""Configure native runtime health through its existing registered SSH identity."""
import ipaddress
import json
from pathlib import Path
import shlex
import time

SCRIPT = Path(__file__).resolve().parents[1] / 'deployment/bootstrap/runtime-healthz.py'


def configure_runtime_healthz(request, descriptor, ansible, native):
    node = request['inventory']['control_plane'][0]
    private = descriptor['addresses']['private']
    if node['private_ipv4'] != private or descriptor['target_id'] != request['target']['id']:
        raise ValueError('Registered runtime identity differs')
    ipaddress.IPv4Address(private)
    endpoint = descriptor.get('management_endpoint', 'https://' + private + ':6443')
    with ansible.forwarded_port(node['ssh'].get('transport_ref'), time.monotonic() + 480) as port:
        host = next(iter(ansible.build_inventory(request, port)['all']['children']['k3s_server']['hosts'].values()))
        prefix = ['ssh', *shlex.split(host['ansible_ssh_common_args']), '-i', host['ansible_ssh_private_key_file'],
                  '-p', str(host['ansible_port']), '-o', 'ConnectTimeout=15',
                  host['ansible_user'] + '@' + host['ansible_host']]
        output = native([*prefix, shlex.join(['sudo', '-n', 'python3', '-c', SCRIPT.read_text()])],
                        document={'node_ip': private}, timeout=420)
    result = json.loads(output)
    if (result.get('checks') != {'/healthz': 200, '/version': 401, '/api/v1/secrets': 401}
            or not result.get('workloads_preserved')):
        raise ValueError('Native runtime health verification failed')
    return {'healthz_url': endpoint + '/healthz', 'server_name': private, 'ca_pem': result['ca_pem']}
