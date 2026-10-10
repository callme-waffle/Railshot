#!/usr/bin/env python3
"""Observe a prepared single-node runtime. No install, apply, exec, delete or DNS changes."""
import argparse
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import time

from input_adapter import parse_input
from render import NAME
from runtime import load


class ObservationError(RuntimeError):
    pass


class Observer:
    def __init__(self, timeout=25):
        self.deadline = time.monotonic() + timeout

    def run(self, argv):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ObservationError('preflight deadline exceeded')
        try:
            result = subprocess.run(argv, capture_output=True, text=True,
                                    timeout=min(4, remaining), check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ObservationError(f'{argv[0]} unavailable or timed out') from exc
        if result.returncode:
            raise ObservationError(f'{argv[0]} exited {result.returncode}')
        return result.stdout

    def kube(self, *args):
        return json.loads(self.run(['/usr/local/bin/k3s', 'kubectl', '--request-timeout=3s',
                                   'get', *args, '-o', 'json']))


def ready_condition(resource):
    return any(c.get('type') == 'Ready' and c.get('status') == 'True'
               for c in resource.get('status', {}).get('conditions', []))


def inspect(spec, observer, workload=NAME, service=None, expected_image_id=None, health_path=None):
    """All observations share one deadline. Missing/failed evidence is never a PASS."""
    started = time.monotonic()
    checks = []
    endpoint = None

    def record(name, condition, detail):
        checks.append({'check': name, 'status': 'pass' if condition else 'fail', 'detail': detail})
        print(f'[{"PASS" if condition else "FAIL"}] {name}: {detail}', file=sys.stderr)

    try:
        record('K3s service', observer.run(['systemctl', 'is-active', 'k3s']).strip() == 'active',
               'systemctl is-active k3s')
        nodes = observer.kube('nodes')['items']
        record('node Ready', len(nodes) == 1 and ready_condition(nodes[0]), 'single-node Ready condition')
        if len(nodes) != 1:
            raise ObservationError('a single-node cluster is required')
        node_ip = next(a['address'] for a in nodes[0]['status']['addresses'] if a['type'] == 'InternalIP')
        ipaddress.IPv4Address(node_ip)
        ds = observer.kube('daemonset', 'cilium', '-n', 'kube-system')
        status = ds.get('status', {})
        desired = status.get('desiredNumberScheduled', 0)
        record('Cilium daemonset Ready', desired > 0 and status.get('numberReady') == desired
               and status.get('updatedNumberScheduled') == desired
               and status.get('observedGeneration', 0) >= ds['metadata'].get('generation', 1),
               f'{status.get("numberReady", 0)}/{desired} Ready')
        agents = observer.kube('pods', '-n', 'kube-system', '-l', 'k8s-app=cilium')['items']
        record('Cilium Running', bool(agents) and all(p['status'].get('phase') == 'Running'
               and ready_condition(p) and not p['metadata'].get('deletionTimestamp') for p in agents),
               'all Cilium agents Running/Ready')
        ns = spec.workload.namespace
        namespace = observer.kube('namespace', ns)
        record('namespace', namespace.get('status', {}).get('phase') == 'Active', ns)
        resources = observer.kube('deployment,service,pods,endpointslices', '-n', ns)['items']
        deployment = next(r for r in resources if r['kind'] == 'Deployment' and r['metadata']['name'] == workload)
        svc_name = service or workload
        svc = next(r for r in resources if r['kind'] == 'Service' and r['metadata']['name'] == svc_name)
        status = deployment.get('status', {})
        desired = deployment['spec'].get('replicas', 1)
        record('workload Deployment', desired == spec.workload.replicas
               and status.get('readyReplicas', 0) == desired
               and status.get('updatedReplicas', 0) == desired
               and status.get('availableReplicas', 0) == desired
               and status.get('observedGeneration', 0) >= deployment['metadata'].get('generation', 1),
               f'{workload}: {status.get("readyReplicas", 0)}/{desired} Ready, current generation')
        # Restrict observation to the Deployment's selector, not unrelated healthy Pods.
        selector = deployment['spec']['selector'].get('matchLabels', {})
        pod_labels = deployment['spec']['template']['metadata'].get('labels', {})
        pods = [r for r in resources if r['kind'] == 'Pod' and selector
                and all(r['metadata'].get('labels', {}).get(k) == v for k, v in selector.items())
                and not r['metadata'].get('deletionTimestamp')]
        record('workload Pod', len(pods) == desired and all(p['status'].get('phase') == 'Running'
               and ready_condition(p) for p in pods), f'{len(pods)} matching Pods Running/Ready')
        if expected_image_id:
            record('verified image digest', bool(pods) and all(any(
                c.get('imageID', '').removeprefix('docker-pullable://') == expected_image_id
                for c in p['status'].get('containerStatuses', [])) for p in pods), expected_image_id)
        service_selector = svc['spec'].get('selector', {})
        port = next((p for p in svc['spec']['ports'] if p.get('nodePort') == spec.exposure.node_port), None)
        container_ports = [p for c in deployment['spec']['template']['spec']['containers'] for p in c.get('ports', [])]
        target = port.get('targetPort', port['port']) if port else None
        port_matches = any(p.get('containerPort') == spec.workload.container_port
                           and (p.get('name') == target if isinstance(target, str) else p['containerPort'] == target)
                           for p in container_ports)
        record('Service / NodePort', svc['spec']['type'] == 'NodePort' and bool(port)
               and port.get('protocol', 'TCP') == 'TCP' and port_matches
               and bool(service_selector) and all(pod_labels.get(k) == v for k, v in service_selector.items()),
               f'{svc_name}: NodePort {spec.exposure.node_port}, target port and selector')
        slices = [r for r in resources if r['kind'] == 'EndpointSlice'
                  and r['metadata'].get('labels', {}).get('kubernetes.io/service-name') == svc_name]
        ready_uids = {p['metadata']['uid'] for p in pods if ready_condition(p)}
        record('Service backend', any(e.get('conditions', {}).get('ready') is True
               and e.get('targetRef', {}).get('uid') in ready_uids
               for s in slices for e in s.get('endpoints', [])), 'ready EndpointSlice belongs to workload Pod')
        endpoint = f'http://{node_ip}:{spec.exposure.node_port}{health_path or spec.workload.health_path}'
        output = observer.run(['curl', '--noproxy', '*', '--silent', '--show-error',
            '--connect-timeout', '2', '--max-time', '3', '--max-filesize', '4096',
            '--write-out', '\n%{http_code}', endpoint])
        body, _, code = output.rpartition('\n')
        record('node-local health', code == '200' and
               (not spec.workload.sample_content or body.strip() == 'Railshot Runtime OK'), f'HTTP {code}')
    except (ObservationError, KeyError, StopIteration, ValueError, TypeError) as exc:
        # API failure/permission/timeout means unknown, not proof of a Runtime implementation bug.
        checks.append({'check': 'observation', 'status': 'unknown', 'detail': str(exc) or 'required resource missing'})
        print('[UNKNOWN] runtime observation incomplete', file=sys.stderr)
    failed = any(c['status'] == 'fail' for c in checks)
    unknown = any(c['status'] == 'unknown' for c in checks)
    return {'runtime_status': 'not_ready' if failed else 'unknown' if unknown else 'ready',
            'endpoint': endpoint, 'checks': checks, 'elapsed_seconds': round(time.monotonic() - started, 3),
            'read_only': True, 'public_exposure': 'not_checked'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, help='existing Runtime JSON input; no deploy is performed')
    parser.add_argument('--workload', default=NAME)
    parser.add_argument('--service', help='default: workload name')
    parser.add_argument('--expected-image-id', help='observed full imageID, not a multiarch index digest')
    parser.add_argument('--health-path', help='safe diagnostic override; normally use the input health_path')
    parser.add_argument('--timeout-seconds', type=float, default=25)
    args = parser.parse_args(argv)
    if not 0 < args.timeout_seconds <= 30:
        parser.error('timeout-seconds must be > 0 and <= 30')
    if args.health_path and (not args.health_path.startswith('/') or '?' in args.health_path or '#' in args.health_path):
        parser.error('health-path must be an absolute path without query or fragment')
    try:
        spec = parse_input(load(Path(args.input).read_text())).spec
        result = inspect(spec, Observer(args.timeout_seconds), args.workload, args.service,
                         args.expected_image_id, args.health_path)
    except (OSError, ValueError) as exc:
        result = {'runtime_status': 'unknown', 'error': str(exc), 'read_only': True}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['runtime_status'] == 'ready' else 1


if __name__ == '__main__':
    sys.exit(main())
