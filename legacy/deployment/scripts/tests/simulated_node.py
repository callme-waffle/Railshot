#!/usr/bin/env python3
"""Provider-independent acceptance workflow run inside each new Linux VM."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone

SCRIPTS = Path(__file__).resolve().parents[1]
ROOT = SCRIPTS.parent


def core_hashes(root=ROOT):
    paths = [root/'scripts'/name for name in ('engine.py', 'models.py', 'runtime.py', 'input_adapter.py', 'render.py', 'common.sh')]
    paths += list((root/'bootstrap').glob('*.sh')) + list((root/'cilium').glob('*.sh')) + list((root/'cilium').glob('*.py'))
    paths += list((root/'manifests').glob('*.template'))
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def main():
    parser = argparse.ArgumentParser(description='Fresh disposable-node simulation; removes cluster at the end')
    parser.add_argument('--input', required=True)
    parser.add_argument('--results', required=True)
    parser.add_argument('--disposable-node', required=True, action='store_true')
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('Requires root on a new dedicated Linux VM')
    data = json.loads(Path(args.input).read_text())
    results = Path(args.results); results.mkdir(parents=True, exist_ok=True)
    rows, stage = [], 'clean-state'
    summary = {'status': 'failed', 'provider': data['provider'], 'results': rows, 'error': None,
               'core_hashes': core_hashes(), 'environment': {
                   'hostname': platform.node(), 'architecture': platform.machine(), 'kernel': platform.release(),
                   'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                   'os_release': Path('/etc/os-release').read_text(),
                   'addresses': json.loads(subprocess.check_output(['ip', '-j', '-4', 'addr', 'show'], text=True))}}

    def kube(*argv):
        return subprocess.check_output(['/usr/local/bin/k3s', 'kubectl', '--request-timeout=10s', *argv], text=True,
                                       stderr=subprocess.STDOUT, timeout=60)

    def runtime(name, action, request, flags=()):
        nonlocal stage
        stage = name
        started = time.monotonic()
        with (results/f'{name}.log').open('w') as log:
            process = subprocess.run([sys.executable, str(SCRIPTS/'runtime.py'), action, *flags],
                                     input=json.dumps(request), text=True, stdout=subprocess.PIPE, stderr=log, timeout=1200)
        (results/f'{name}.json').write_text(process.stdout)
        output = json.loads(process.stdout)
        expected = 'cleaned' if action == 'cleanup' else 'ready'
        if process.returncode or output['status'] != expected or output['error'] is not None:
            raise RuntimeError(f'{name}: exit={process.returncode}; {output.get("error")}')
        if output['provider'] != request['provider'] or output['environment_id'] != request['environment_id']:
            raise RuntimeError(f'{name}: JSON request context mismatch')
        if expected == 'ready':
            states = {item['state'] for item in output['states']}
            if not {'K3S_READY', 'CILIUM_READY', 'WORKLOAD_READY', 'ENDPOINT_READY'}.issubset(states):
                raise RuntimeError(f'{name}: missing readiness transitions')
            if output['endpoint'] != f'http://{request["runtime"]["node_ip"]}:{request["exposure"]["node_port"]}':
                raise RuntimeError(f'{name}: observed endpoint did not use the configured NIC address')
        rows.append({'test': name, 'status': 'passed', 'duration_seconds': round(time.monotonic()-started, 3)})
        print(f'[PASS] {data["provider"]}: {name}', file=sys.stderr, flush=True)
        return output

    try:
        absent = [not Path(p).exists() for p in ('/usr/local/bin/k3s', '/var/lib/rancher/k3s', '/etc/rancher/k3s/config.yaml')]
        if not all(absent):
            raise RuntimeError('Fresh install requires K3s binary, data and config to be absent; refusing existing node')
        (results/'clean-state.txt').write_text('CLEAN_NODE_CONFIRMED\n')
        ready = runtime('clean-install', 'deploy', data)
        ns = data['workload']['namespace']
        uid = kube('-n', ns, 'get', 'deployment', 'railshot-workload', '-o', 'jsonpath={.metadata.uid}')
        pod_uid = kube('-n', ns, 'get', 'pods', '-l', 'railshot.io/app=railshot-workload', '-o', 'jsonpath={.items[0].metadata.uid}')
        runtime('bootstrap-repeat', 'deploy', data)
        if uid != kube('-n', ns, 'get', 'deployment', 'railshot-workload', '-o', 'jsonpath={.metadata.uid}'):
            raise RuntimeError('Bootstrap repeat replaced Deployment UID')
        if pod_uid != kube('-n', ns, 'get', 'pods', '-l', 'railshot.io/app=railshot-workload', '-o', 'jsonpath={.items[0].metadata.uid}'):
            raise RuntimeError('Bootstrap repeat replaced Pod UID')
        runtime('health-endpoint', 'verify', data)
        updated = copy.deepcopy(data); updated['workload']['image'] = 'nginx:1.28.1-alpine'
        runtime('workload-update', 'deploy', updated)
        runtime('update-health', 'verify', updated)
        (results/'node-state.log').write_text(kube('get', 'nodes', '-o', 'wide') + kube('-n', ns, 'get', 'deployment,pods,service,endpointslices', '-o', 'wide'))
        runtime('workload-cleanup', 'cleanup', updated)
        if kube('-n', ns, 'get', 'deployment,service', 'railshot-workload', '--ignore-not-found', '-o', 'name').strip():
            raise RuntimeError('Workload cleanup left managed Deployment/Service')
        runtime('workload-redeploy', 'deploy', data)
        summary.update(status='passed', endpoint=ready['endpoint'])
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        summary['error'] = {'stage': stage, 'message': str(exc)}
        print(f'[FAIL] {data["provider"]}: {stage}: {exc}', file=sys.stderr, flush=True)
    finally:
        # Only remove a cluster with the runtime ownership marker. Never delete foreign K3s.
        marker = Path('/etc/rancher/k3s/config.yaml')
        owned = marker.is_file() and '# Managed by Railshot deployment runtime.' in marker.read_text()
        if owned:
            try:
                runtime('full-cleanup', 'cleanup', data, ('--all', '--disposable-node'))
                if any(Path(p).exists() for p in ('/usr/local/bin/k3s', '/var/lib/rancher/k3s', '/etc/rancher/k3s/config.yaml')):
                    raise RuntimeError('Full cleanup left core K3s files')
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                summary['status'] = 'failed'
                summary['cleanup_error'] = str(exc)
        summary['timestamp'] = datetime.now(timezone.utc).isoformat()
        (results/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary))
    return 0 if summary['status'] == 'passed' else 1


if __name__ == '__main__':
    sys.exit(main())
