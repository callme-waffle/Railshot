#!/usr/bin/env python3
"""Live cross-worker DNS, Pod/Service traffic and NetworkPolicy acceptance probe."""
import json
import secrets
import subprocess
import sys
import time

IMAGE = 'docker.io/library/busybox:1.37.0'


def verify(workers, *, runner=None, timeout=300):
    require = lambda value: value or (_ for _ in ()).throw(ValueError('cluster network acceptance failed'))
    require(len(workers) == 2 and len(set(workers)) == 2)
    prefix = ['/usr/bin/kubectl', '--kubeconfig=/etc/kubernetes/admin.conf', '--request-timeout=20s']
    deadline = time.monotonic() + timeout
    def call(*args, document=None, allow_failure=False):
        remaining = deadline - time.monotonic()
        require(remaining > 0)
        execute = runner or subprocess.run
        reply = execute([*prefix, *args], input=None if document is None else json.dumps(document),
                        text=True, capture_output=True, timeout=remaining if 'wait' in args else min(35, remaining), check=False)
        require(len(reply.stdout.encode()) <= 1048576)
        if not allow_failure: require(reply.returncode == 0)
        return reply
    namespace = 'railshot-net-' + secrets.token_hex(8)
    ownership = secrets.token_hex(16)
    created = None
    labels = {'railshot.io/network-probe': ownership}
    def apply(kind, name, spec=None, api='v1', extra=None):
        doc = {'apiVersion': api, 'kind': kind, 'metadata': {'name': name, 'namespace': namespace, 'labels': labels}}
        if spec is not None: doc['spec'] = spec
        doc.update(extra or {})
        return call('-n', namespace, 'create', '-f', '-', '-o', 'json', document=doc)
    try:
        created = json.loads(call('create', '-f', '-', '-o', 'json', document={
            'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace, 'labels': labels}}).stdout)
        def pod(name, worker, role, command):
            apply('Pod', name, {'nodeSelector': {'kubernetes.io/hostname': worker}, 'restartPolicy': 'Never',
                  'containers': [{'name': 'probe', 'image': IMAGE, 'command': command,
                                  'resources': {'requests': {'cpu': '10m', 'memory': '16Mi'},
                                                'limits': {'cpu': '100m', 'memory': '64Mi'}}}]},
                  extra={'metadata': {'name': name, 'namespace': namespace, 'labels': {**labels, 'role': role}}})
        pod('server', workers[0], 'server', ['sh', '-ec', 'mkdir -p /www; echo railshot-network-ready > /www/index.html; exec httpd -f -p 8080 -h /www'])
        pod('allowed', workers[1], 'allowed', ['sleep', str(timeout)])
        pod('denied', workers[1], 'denied', ['sleep', str(timeout)])
        apply('Service', 'probe', {'selector': {**labels, 'role': 'server'}, 'ports': [{'port': 8080, 'targetPort': 8080}]})
        apply('NetworkPolicy', 'probe-ingress', {'podSelector': {'matchLabels': {**labels, 'role': 'server'}},
              'policyTypes': ['Ingress'], 'ingress': [{'from': [{'podSelector': {'matchLabels': {**labels, 'role': 'allowed'}}}],
                                                     'ports': [{'protocol': 'TCP', 'port': 8080}]}]}, 'networking.k8s.io/v1')
        for name in ('server', 'allowed', 'denied'):
            wait = max(1, min(120, int(deadline - time.monotonic()) - 2))
            call('-n', namespace, 'wait', '--for=condition=Ready', 'pod/' + name, f'--timeout={wait}s')
        server = json.loads(call('-n', namespace, 'get', 'pod', 'server', '-o', 'json').stdout)
        allowed = json.loads(call('-n', namespace, 'get', 'pod', 'allowed', '-o', 'json').stdout)
        denied = json.loads(call('-n', namespace, 'get', 'pod', 'denied', '-o', 'json').stdout)
        require(server['spec']['nodeName'] == workers[0] and allowed['spec']['nodeName'] == workers[1]
                and denied['spec']['nodeName'] == workers[1])
        call('-n', namespace, 'exec', 'allowed', '--', 'nslookup', 'kubernetes.default.svc.cluster.local')
        for address in (server['status']['podIP'], f'probe.{namespace}.svc.cluster.local'):
            ok = call('-n', namespace, 'exec', 'allowed', '--', 'wget', '-qO-', '-T', '5', f'http://{address}:8080/')
            require(ok.stdout.strip() == 'railshot-network-ready')
        blocked = False
        for _ in range(10):
            # Exec itself must succeed. Only wget's exit status proves policy denial.
            command = 'if wget -qO /dev/null -T 3 http://' + server['status']['podIP'] + ':8080/; then printf ALLOWED; else printf BLOCKED; fi'
            reply = call('-n', namespace, 'exec', 'denied', '--', 'sh', '-ec', command)
            require(reply.stdout.strip() in ('ALLOWED', 'BLOCKED'))
            if reply.stdout.strip() == 'BLOCKED':
                blocked = True; break
            time.sleep(1)
        require(blocked)
        # Recheck the allowed path after denial so a failed server cannot prove isolation.
        ok = call('-n', namespace, 'exec', 'allowed', '--', 'wget', '-qO-', '-T', '5', f'http://{server["status"]["podIP"]}:8080/')
        require(ok.stdout.strip() == 'railshot-network-ready')
        return {'dns_ready': True, 'pod_network_ready': True, 'service_network_ready': True, 'network_policy_verified': True}
    finally:
        if created:
            # UID preconditions prevent deleting a replacement namespace after a race.
            call('delete', '--raw', '/api/v1/namespaces/' + namespace, '-f', '-', document={
                'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': created['metadata']['uid']}})


if __name__ == '__main__':
    print(json.dumps(verify(json.loads(sys.argv[1]), timeout=int(sys.argv[2]))))
