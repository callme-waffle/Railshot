"""Local Linux execution engine. No AWS/GCP/OpenStack or transport schemas."""
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import sys
import signal
import time
from datetime import datetime, timezone
from urllib.request import Request, ProxyHandler, build_opener
from uuid import uuid4
from models import DeploymentResult
from render import NAME, ROOT, labels, render
sys.path.insert(0, str(ROOT))
from airgap.scripts import bundle as bundles
from network_preflight import check as check_network
from exposure import apply as apply_exposure


class DeploymentError(RuntimeError):
    def __init__(self, code, message, exit_code=None):
        super().__init__(message)
        self.code, self.exit_code = code, exit_code


class CommandRunner:
    def run(self, argv, *, env, timeout, input_text=None):
        try:
            process = subprocess.Popen(argv, env=env, text=True, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                stdout, stderr = process.communicate(input_text, timeout=timeout)
            except subprocess.TimeoutExpired:
                # Stop descendants too: an orphan installer must not keep mutating a node after failure.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate()
                raise
            result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired as exc:
            raise DeploymentError('COMMAND_TIMEOUT', f'{Path(argv[0]).name}: timed out after {timeout}s') from exc
        except OSError as exc:
            raise DeploymentError('COMMAND_UNAVAILABLE', f'{Path(argv[0]).name}: {exc.strerror}') from exc
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        if result.returncode:
            # Exclude arbitrary output bodies; managed shell diagnostics contain only readiness/state/events.
            detail = (result.stderr or result.stdout).strip()[-5000:]
            raise DeploymentError('COMMAND_FAILED', detail or f'{Path(argv[0]).name} failed', result.returncode)
        return result.stdout


class DeploymentEngine:
    def __init__(self, spec, runner=None, network_checker=None):
        self.spec, self.runner = spec, runner or CommandRunner()
        self.result = DeploymentResult()
        self.stage = 'NODE_READY'
        self.bundle = None
        self.network_checker = network_checker or check_network
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('K3S_', 'INSTALL_K3S_', 'RAILSHOT_BUNDLE', 'RAILSHOT_OFFLINE', 'CILIUM_CHART', 'CILIUM_VALUES'))}
        self.env.update({'K3S_VERSION': spec.runtime.k3s_version, 'CILIUM_VERSION': spec.runtime.cilium_version,
                         'CILIUM_CLI_VERSION': spec.runtime.cilium_cli_version,
                         'WAIT_TIMEOUT': f'{spec.runtime.timeout_seconds}s',
                         'WORKLOAD_NAMESPACE': spec.workload.namespace,
                         'KUBECONFIG': '/etc/rancher/k3s/k3s.yaml', 'NODE_IP': spec.runtime.node_ip or ''})

    def command(self, args, input_text=None, timeout=None):
        return self.runner.run(args, env=self.env, timeout=timeout or self.spec.runtime.timeout_seconds + 30,
                               input_text=input_text)

    def shell(self, relative):
        return self.command(['bash', str(ROOT/relative)], timeout=self.spec.runtime.timeout_seconds + 600)

    def kube(self, *args, input_text=None):
        return self.command(['/usr/local/bin/k3s', 'kubectl', '--request-timeout=10s', *args], input_text)

    def state(self, state):
        self.stage = state
        self.result.states.append({'state': state, 'timestamp': datetime.now(timezone.utc).isoformat()})
        print(f'[{state}]', file=sys.stderr)

    def network_state(self, state):
        # Preserve the legacy 0.1 states enum/order. Extended phases have their own 0.2 field.
        self.stage = state
        self.result.network_states.append({'state': state, 'timestamp': datetime.now(timezone.utc).isoformat()})
        print(f'[{state}]', file=sys.stderr)

    def select_mode(self):
        bundles.approved(self.spec)
        self.network_state('NETWORK_CHECKING')
        caps, details = self.network_checker(self.spec, bundles.architecture(), self.spec.runtime.mode == 'offline')
        self.result.network_capabilities, self.result.network_details = caps, details
        if self.spec.runtime.bundle_path:
            self.network_state('BUNDLE_VERIFYING')
            self.bundle = bundles.verify(self.spec.runtime.bundle_path, self.spec, self.spec.runtime.bundle_sha256)
            self.result.bundle_verified = True
            self.result.bundle_version = self.bundle['manifest']['bundle_version']
            self.env['RAILSHOT_BUNDLE'] = self.bundle['root']
            self.env['RAILSHOT_BUNDLE_SHA256'] = self.bundle['manifest_sha256']
        blocked = details.get('required_unavailable', [])
        mode = self.spec.runtime.mode
        if mode == 'online' and blocked:
            raise DeploymentError('NETWORK_UNAVAILABLE', 'Online mode requires: ' + ', '.join(blocked))
        if mode == 'offline' or (mode == 'auto' and blocked):
            if not self.bundle:
                raise DeploymentError('BUNDLE_REQUIRED', 'Offline/fallback needs a valid local bundle; unreachable: ' + ', '.join(blocked))
            self.enable_airgap('registry_unreachable' if set(blocked) & {'quay', 'docker_hub', 'ghcr', 'registry_k8s', 'workload_registry'} else 'artifact_source_unreachable', fallback=mode == 'auto')
        else:
            self.result.deployment_mode = 'online'
            self.env['RAILSHOT_OFFLINE'] = 'false'

    def enable_airgap(self, reason, fallback=True):
        self.result.deployment_mode = 'airgap'
        self.result.fallback_used = fallback
        self.result.fallback_reason = reason if fallback else None
        self.env['RAILSHOT_OFFLINE'] = 'true'

    def effective_image(self, image):
        if self.bundle:
            ref = bundles.canonical(image)
            for record in self.bundle['manifest']['images']:
                if record['reference'] == ref:
                    return bundles.pinned(ref, record['digest'])
            raise bundles.BundleError('BUNDLE_IMAGE_MISSING', ref)
        return image

    def preload(self):
        if self.bundle:
            self.network_state('AIRGAP_PRELOADING')
            self.result.preload = bundles.preload(self.bundle)

    def owned(self, kind, name, namespace=None):
        args = ['get', kind, name, '--ignore-not-found', '-o', 'json']
        if namespace:
            args += ['-n', namespace]
        raw = self.kube(*args)
        if not raw.strip():
            return False
        actual = json.loads(raw)['metadata'].get('labels', {})
        if any(actual.get(k) != v for k, v in labels(self.spec).items()):
            raise DeploymentError('OWNERSHIP_CONFLICT', f'{kind}/{name}: not owned by this Railshot environment')
        return True

    def ownership(self, required=False):
        ns = self.spec.workload.namespace
        exists = self.owned('namespace', ns)
        if required and not exists:
            raise DeploymentError('WORKLOAD_MISSING', f'namespace/{ns}: absent')
        if exists:
            for kind in ('deployment', 'service', 'configmap'):
                self.owned(kind, NAME, ns)

    def diagnose(self):
        try:
            raw = self.kube('get', 'pods', '-n', self.spec.workload.namespace, '-o', 'json')
            items = json.loads(raw)['items']
            waiting = [f"{p['metadata']['name']}: {c['state']['waiting'].get('reason', '')} {c['state']['waiting'].get('message', '')}"
                       for p in items for c in p.get('status', {}).get('containerStatuses', []) if 'waiting' in c.get('state', {})]
            events = self.kube('get', 'events', '-n', self.spec.workload.namespace, '--sort-by=.lastTimestamp')
            return '\n'.join(waiting + events.splitlines()[-12:])[-5000:]
        except (DeploymentError, ValueError, KeyError):
            return 'Additional Kubernetes diagnostics unavailable'

    def workload_ready(self):
        ns = self.spec.workload.namespace
        self.kube('-n', ns, 'rollout', 'status', f'deployment/{NAME}', f'--timeout={self.spec.runtime.timeout_seconds}s')
        deployment = json.loads(self.kube('-n', ns, 'get', 'deployment', NAME, '-o', 'json'))
        live = deployment['spec']
        container = live['template']['spec']['containers'][0]
        if (live['replicas'] != self.spec.workload.replicas or container['image'] != self.effective_image(self.spec.workload.image)
                or container['ports'][0]['containerPort'] != self.spec.workload.container_port
                or container['readinessProbe']['httpGet']['path'] != self.spec.workload.health_path):
            raise DeploymentError('WORKLOAD_DRIFT', 'Live workload does not match the requested image/replicas/port/health path')
        self.result.workload_status = 'ready'
        self.state('WORKLOAD_READY')

    def network_check(self):
        """Generic image does not need curl/wget. Use a short-lived owned diagnostic Pod."""
        ns, name = self.spec.workload.namespace, f'railshot-check-{uuid4().hex[:8]}'
        url = f'http://{NAME}.{ns}.svc.cluster.local{self.spec.workload.health_path}'
        pod = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': ns, 'labels': labels(self.spec)},
               'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                        'containers': [{'name': 'check', 'image': self.effective_image(bundles.policy()['health_image']),
                                        'imagePullPolicy': 'Never' if self.result.deployment_mode == 'airgap' else 'IfNotPresent',
                                        'command': ['curl', '--silent', '--show-error', '--connect-timeout', '3',
                                                    '--max-time', '10', '--output', '/dev/null', '--write-out', '%{http_code}', url]}]}}
        try:
            self.kube('create', '-f', '-', input_text=json.dumps(pod))
            deadline = time.monotonic() + self.spec.runtime.timeout_seconds
            while True:
                observed = json.loads(self.kube('-n', ns, 'get', 'pod', name, '-o', 'json'))
                phase = observed.get('status', {}).get('phase')
                if phase in ('Succeeded', 'Failed'):
                    status = self.kube('-n', ns, 'logs', name).strip()
                    if phase != 'Succeeded' or status != '200':
                        raise DeploymentError('SERVICE_HTTP_FAILED', f'Pod DNS/Service health check failed (phase={phase}, HTTP={status[-64:]})')
                    break
                if time.monotonic() >= deadline:
                    raise DeploymentError('SERVICE_HTTP_TIMEOUT', 'Diagnostic Pod did not complete; check image access, DNS, Service and NetworkPolicy')
                time.sleep(1)
        finally:
            try:
                self.kube('-n', ns, 'delete', 'pod', name, '--ignore-not-found', '--wait=false')
            except DeploymentError:
                print(f'[WARN] diagnostic Pod cleanup requires retry: {name}', file=sys.stderr)

    def endpoints(self):
        self.stage = 'ENDPOINT_READY'
        ns = self.spec.workload.namespace
        service = json.loads(self.kube('-n', ns, 'get', 'service', NAME, '-o', 'json'))
        port = service['spec']['ports'][0]
        if (service['spec']['type'] != 'NodePort' or port['nodePort'] != self.spec.exposure.node_port
                or port['port'] != 80 or port['targetPort'] != 'http'
                or service['spec']['selector'] != {'railshot.io/app': NAME}):
            raise DeploymentError('SERVICE_DRIFT', 'Live Service does not match the requested NodePort/target/selector')
        self.network_check()
        nodes = json.loads(self.kube('get', 'nodes', '-o', 'json'))['items']
        if len(nodes) != 1:
            raise DeploymentError('UNSUPPORTED_CLUSTER', 'Single-node K3s only')
        addresses = [a['address'] for a in nodes[0]['status']['addresses'] if a['type'] == 'InternalIP']
        if not addresses:
            raise DeploymentError('NODE_IP_MISSING', 'Node InternalIP is missing')
        try:
            ipaddress.IPv4Address(addresses[0])
        except ValueError:
            raise DeploymentError('NODE_IP_INVALID', 'Node InternalIP must be IPv4') from None
        base = f'http://{addresses[0]}:{self.spec.exposure.node_port}'
        urls = [base + self.spec.workload.health_path]
        opener = build_opener(ProxyHandler({}))
        for url in urls:
            deadline, last = time.monotonic() + self.spec.runtime.timeout_seconds, ''
            while True:
                try:
                    with opener.open(Request(url), timeout=10) as response:
                        body = response.read(1024) if self.spec.workload.sample_content else b''
                        if response.status != 200:
                            raise ValueError(f'HTTP {response.status}')
                        if self.spec.workload.sample_content and body.strip() != b'Railshot Runtime OK':
                            raise ValueError('Unexpected sample response body')
                    break
                except (OSError, ValueError) as exc:
                    last = str(exc)
                    if time.monotonic() >= deadline:
                        raise DeploymentError('ENDPOINT_HTTP_FAILED', f'{url}: {last}') from exc
                    time.sleep(1)
        self.result.endpoint = base
        self.result.endpoint_scope = 'node-local'
        self.state('ENDPOINT_READY')

    def additional_endpoint(self):
        """Bounded observation of an upstream exposure; never invalidate local readiness."""
        url = self.spec.exposure.verification_url
        if not url:
            return
        diagnostic = {'url': url, 'status': 'unavailable', 'reason': 'offline_mode'}
        if self.result.deployment_mode != 'airgap':
            try:
                output = self.command(['curl', '--noproxy', '*', '--silent', '--show-error',
                    '--connect-timeout', '2', '--max-time', '3', '--max-filesize', '4096',
                    '--write-out', '\n%{http_code}', url], timeout=4)
                body, _, status = output.rpartition('\n')
                if status != '200':
                    raise ValueError(f'HTTP {status or "unknown"}')
                if self.spec.workload.sample_content and body.strip() != 'Railshot Runtime OK':
                    raise ValueError('Unexpected sample response body')
                diagnostic.update(status='ready', reason=None)
                if self.result.endpoint_scope == 'node-local':
                    self.result.endpoint_scope = 'node-local-with-additional-url'
            except (DeploymentError, ValueError) as exc:
                # No raw response bodies or credentials in a diagnostic.
                diagnostic['reason'] = exc.code if isinstance(exc, DeploymentError) else str(exc)
        self.result.exposure_status['additional_verification'] = diagnostic
        if diagnostic['status'] != 'ready':
            self.result.exposure_status.update(status='degraded', reason='additional_endpoint_unavailable')
            print(f'[EXTERNAL_DEPENDENCY] additional endpoint {diagnostic["reason"]}; node-local runtime remains ready', file=sys.stderr)

    def reconcile(self, action):
        if action == 'deploy':
            self.state('K3S_INSTALLING')
            self.shell('bootstrap/install-k3s.sh')
        else:
            self.stage = 'K3S_READY'
            self.shell('bootstrap/health.sh')
        self.result.cluster_status = 'api_ready'
        self.state('K3S_READY')
        if action == 'cleanup':
            self.cleanup_workload()
            self.result.status = 'cleaned'
            return
        if action == 'deploy':
            self.preload()
            self.state('CILIUM_INSTALLING')
            self.shell('cilium/install.sh')
        else:
            self.stage = 'CILIUM_READY'
        self.shell('cilium/health.sh')
        self.result.cilium_status, self.result.cluster_status = 'healthy', 'ready'
        self.state('CILIUM_READY')
        self.stage = 'WORKLOAD_DEPLOYING'
        self.ownership(required=action == 'verify')
        if action == 'deploy':
            self.state('WORKLOAD_DEPLOYING')
            self.kube('apply', '-f', '-', input_text=json.dumps(render(self.spec,
                self.effective_image(self.spec.workload.image), 'Never' if self.result.deployment_mode == 'airgap' else 'IfNotPresent')))
        self.workload_ready()
        self.endpoints()
        apply_exposure(self.spec, self.result, self.result.deployment_mode == 'airgap')
        self.additional_endpoint()
        self.result.status = 'ready'

    def cleanup_workload(self):
        self.stage = 'WORKLOAD_DEPLOYING'
        if not self.owned('namespace', self.spec.workload.namespace):
            self.result.workload_status = 'absent'
            return
        self.ownership()
        self.kube('-n', self.spec.workload.namespace, 'delete', 'deployment,service,configmap', NAME,
                  '--ignore-not-found', '--wait=true', f'--timeout={self.spec.runtime.timeout_seconds}s')
        self.result.workload_status = 'absent'
        # Retain Namespace and unrelated resources. No implicit broad deletion.

    def execute(self, action='deploy', full_cleanup=False, disposable=False):
        lock = None
        try:
            if action not in ('deploy', 'verify', 'cleanup'):
                raise DeploymentError('INVALID_ACTION', action)
            if full_cleanup and not disposable:
                raise DeploymentError('DESTRUCTIVE_ACTION_REQUIRES_FLAG', 'Full cleanup requires --disposable-node')
            self.shell('bootstrap/preflight.sh')
            lock = open('/run/railshot-deployment.lock', 'a')
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DeploymentError('RUNTIME_BUSY', 'Another Railshot deployment/verify/cleanup is running') from None
            self.state('NODE_READY')
            if action == 'cleanup' and full_cleanup:
                self.stage = 'CLEANUP'
                self.command(['bash', str(ROOT/'bootstrap/cleanup.sh'), '--disposable-node'], timeout=self.spec.runtime.timeout_seconds + 600)
                self.result.cluster_status = self.result.cilium_status = self.result.workload_status = 'absent'
                self.result.status = 'cleaned'
                return self.result
            if action == 'deploy':
                self.select_mode()
            elif action == 'verify':
                self.result.deployment_mode = 'airgap' if self.spec.runtime.mode == 'offline' else 'online'
                if self.spec.runtime.bundle_path:
                    self.bundle = bundles.verify(self.spec.runtime.bundle_path, self.spec, self.spec.runtime.bundle_sha256)
                    self.result.bundle_verified = True
                    self.result.bundle_version = self.bundle['manifest']['bundle_version']
            try:
                self.reconcile(action)
            except DeploymentError as exc:
                message = str(exc).lower()
                image_failure = any(word in message for word in ('imagepullbackoff', 'errimagepull'))
                artifact_stage = self.stage in ('K3S_INSTALLING', 'CILIUM_INSTALLING')
                artifact_failure = artifact_stage and (any(word in message for word in ('curl:', 'failed to download'))
                    or ('https://' in message and any(word in message for word in ('no such host', 'connection refused'))))
                network_failure = image_failure or artifact_failure
                if (action == 'deploy' and self.spec.runtime.mode == 'auto' and self.bundle
                        and self.result.deployment_mode == 'online' and network_failure):
                    print('[FALLBACK] Online artifact/image access failed; retry with verified local bundle', file=sys.stderr)
                    self.enable_airgap('online_artifact_or_image_failure')
                    self.reconcile(action)
                else:
                    raise
        except (DeploymentError, OSError, ValueError, KeyError) as exc:
            stage = self.stage
            if stage.startswith('K3S'):
                self.result.cluster_status = 'failed'
            elif stage.startswith('CILIUM'):
                self.result.cilium_status = 'failed'
            elif stage.startswith('WORKLOAD'):
                self.result.workload_status = 'failed'
            detail = self.diagnose() if stage.startswith(('WORKLOAD', 'ENDPOINT')) else ''
            self.result.endpoint = None
            self.result.error = {'code': getattr(exc, 'code', 'RUNTIME_ERROR'), 'stage': stage,
                                 'message': str(exc), 'exit_code': getattr(exc, 'exit_code', None),
                                 'diagnostics': detail or None}
            self.state('FAILED')
        finally:
            if lock:
                lock.close()
        return self.result
