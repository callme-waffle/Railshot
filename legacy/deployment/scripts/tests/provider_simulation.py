#!/usr/bin/env python3
"""Lima-only host harness: sequential fresh VMs, same core, no cloud APIs."""
import argparse
from datetime import datetime, timezone, timedelta
import io
import json
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tarfile
import time
from urllib.request import ProxyHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from input_adapter import parse_input
from simulated_node import core_hashes

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent/'fixtures'
REAL_CLOUD = ['AWS IAM', 'Security Group', 'Elastic IP', 'VPC routing', 'GCP IAM', 'VPC Firewall', 'External IP', 'actual cloud metadata semantics']


def make_input(provider, profile, run_id):
    data = json.loads((FIXTURES/profile['fixture']).read_text())
    data.update(provider=provider, environment_id=f'railshot-sim-{provider}-{run_id}')
    data['node']['host'] = profile['node_ip']
    data['workload']['namespace'] = f'railshot-sim-{provider}'
    data['runtime']['node_ip'] = profile['node_ip']
    parse_input(data)
    return data


def verify_external(url):
    with build_opener(ProxyHandler({})).open(url, timeout=10) as response:
        body = response.read(1024)
        if response.status != 200 or body.strip() != b'Railshot Runtime OK':
            raise RuntimeError(f'Unexpected external response at {url}: HTTP {response.status}')
        return {'status': 'passed', 'http_status': response.status, 'body': body.decode().strip(),
                'url': url, 'scope': 'Mac host -> Lima SSH forwarding -> VM NodePort (not cloud ingress)'}


def extract_results(blob, destination):
    # Tar is produced by a controlled VM. Still reject path traversal/links before extracting.
    with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
        for member in archive.getmembers():
            if member.name.startswith('/') or '..' in Path(member.name).parts or not (member.isfile() or member.isdir()):
                raise RuntimeError(f'Unsafe artifact member: {member.name}')
        archive.extractall(destination, filter='data')


def main():
    parser = argparse.ArgumentParser(description='Fresh sequential Ubuntu arm64 VM simulations; no cloud APIs')
    parser.add_argument('--disposable-vms', required=True, action='store_true')
    parser.add_argument('--results', default=str(ROOT/'scripts/tests/results/provider-simulation'))
    args = parser.parse_args()
    profiles = json.loads((FIXTURES/'provider-simulation.json').read_text())
    run_id = datetime.now(timezone(timedelta(hours=9))).strftime('%Y%m%d%H%M%S')
    output_root = Path(args.results)/run_id; output_root.mkdir(parents=True, exist_ok=False)
    reference = core_hashes(ROOT)
    results = {}

    def command(argv, log_path, timeout=180):
        with log_path.open('ab') as log:
            result = subprocess.run(argv, stdout=log, stderr=log, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f'{Path(argv[0]).name} exited {result.returncode}; see {log_path.name}')

    for provider, profile in profiles.items():
        name = f'railshot-sim-{provider}-{run_id}'
        folder = output_root/provider; folder.mkdir()
        result = {'status': 'failed', 'vm': name, 'architecture': 'aarch64', 'error': None,
                  'metadata_mock': 'not implemented; core does not read provider metadata',
                  'requires real cloud smoke test': REAL_CLOUD}
        results[provider] = result
        created = False
        started = time.monotonic()
        try:
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', profile['host_port']))
            data = make_input(provider, profile, run_id)
            fixture_path = folder/'input.json'; fixture_path.write_text(json.dumps(data, indent=2)+'\n')
            print(f'[RUN] {provider}: {name}', file=sys.stderr, flush=True)
            command(['limactl', 'create', f'--name={name}', '--cpus=2', '--memory=4', '--disk=20', '--arch=aarch64',
                     '--containerd=none', '--set='+'.portForwards = '+json.dumps([
                         {'guestPort': data['exposure']['node_port'], 'hostPort': profile['host_port'], 'static': True}]),
                     '--tty=false', 'template:ubuntu-24.04'], folder/'vm-create.log')
            created = True
            command(['limactl', 'start', name, '--tty=false'], folder/'vm-start.log', timeout=300)
            setup = ('set -e; '
                     f'sudo hostnamectl set-hostname {shlex.quote(profile["hostname"])}; '
                     f'sudo ip link add {shlex.quote(profile["nic"])} type dummy; '
                     f'sudo ip addr add {shlex.quote(profile["node_ip"]+"/24")} dev {shlex.quote(profile["nic"])}; '
                     f'sudo ip link set {shlex.quote(profile["nic"])} up; '
                     'mkdir -p "$HOME/railshot-simulation"; '
                     f'cp -R {shlex.quote(str(ROOT))} "$HOME/railshot-simulation/deployment"; '
                     f'cp {shlex.quote(str(fixture_path.resolve()))} "$HOME/railshot-simulation/input.json"')
            command(['limactl', 'shell', name, 'bash', '-c', setup], folder/'vm-setup.log')
            guest_cmd = ('sudo python3 "$HOME/railshot-simulation/deployment/scripts/tests/simulated_node.py" '
                         '--input "$HOME/railshot-simulation/input.json" --results /var/tmp/provider-simulation --disposable-node')
            # Guest restores and cleans the cluster before it returns. External probing happens while it is running.
            with (folder/'guest-summary.json').open('w') as stdout, (folder/'guest.log').open('w') as stderr:
                process = subprocess.Popen(['limactl', 'shell', name, 'bash', '-c', guest_cmd], stdout=stdout, stderr=stderr)
                external = None
                deadline = time.monotonic() + 1200
                while process.poll() is None:
                    if external is None:
                        try:
                            external = verify_external(f'http://127.0.0.1:{profile["host_port"]}/')
                        except (OSError, ValueError, RuntimeError):
                            pass
                    if time.monotonic() > deadline:
                        process.terminate()
                        try:
                            process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            process.kill(); process.wait()
                        raise RuntimeError('Guest workflow timed out; VM will be force-stopped')
                    time.sleep(2)
            # Fetch artifacts even on guest failure; failure JSON must not be relabeled as a pass.
            artifacts = subprocess.run(['limactl', 'shell', name, 'sudo', 'tar', '-C', '/var/tmp/provider-simulation', '-cf', '-', '.'],
                                       capture_output=True, timeout=60, check=True)
            extract_results(artifacts.stdout, folder/'guest')
            summary = json.loads((folder/'guest-summary.json').read_text())
            result.update(guest=summary, external_endpoint=external)
            if process.returncode or summary['status'] != 'passed':
                raise RuntimeError(f'Guest workflow failed: {summary.get("error") or summary.get("cleanup_error")}')
            if summary['core_hashes'] != reference:
                raise RuntimeError('VM core source hashes differ from the host reference')
            if summary['environment']['hostname'] != profile['hostname'] or summary['environment']['architecture'] != 'aarch64':
                raise RuntimeError('VM hostname or architecture does not match the profile')
            if external is None:
                raise RuntimeError('Host-side NodePort HTTP/body check never succeeded')
            result['status'] = 'passed'
            print(f'[PASS] {provider}: clean/bootstrap/Cilium/workload/health/update/cleanup + host HTTP', file=sys.stderr, flush=True)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            result['error'] = str(exc)
            print(f'[FAIL] {provider}: {exc}', file=sys.stderr, flush=True)
        finally:
            if created:
                try:
                    # Only stop the uniquely named VM created by this harness; preserve its disk and evidence.
                    command(['limactl', 'stop', '--force', name], folder/'vm-stop.log', timeout=60)
                except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                    result.update(status='failed', stop_error=str(exc))
            result['duration_seconds'] = round(time.monotonic()-started, 3)
            (folder/'result.json').write_text(json.dumps(result, indent=2)+'\n')
            (output_root/'summary.json').write_text(json.dumps(results, indent=2)+'\n')
        # Do not start another 4 GiB VM when stopping the preceding VM failed.
        if result.get('stop_error'):
            for remaining in profiles.keys()-results.keys():
                results[remaining] = {'status': 'not_run', 'reason': 'prior VM stop failed'}
            break
    (output_root/'summary.json').write_text(json.dumps(results, indent=2)+'\n')
    print(json.dumps(results))
    return 0 if all(r['status'] == 'passed' for r in results.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
