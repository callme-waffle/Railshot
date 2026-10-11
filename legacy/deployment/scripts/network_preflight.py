"""Bounded real endpoint/TLS probes. Never changes routes/firewalls or calls providers."""
from concurrent.futures import ThreadPoolExecutor
import subprocess
import time
from urllib.parse import urlsplit, quote

KEYS = ('dns', 'https_443', 'k3s_source', 'github', 'cilium_cli_source', 'cilium_chart',
        'quay', 'registry_k8s', 'docker_hub', 'ghcr', 'workload_registry',
        'cloudflare_tunnel')


def registry(image):
    if '/' not in image:
        return 'docker.io'
    component = image.split('/')[0]
    return component if '.' in component or ':' in component or component == 'localhost' else 'docker.io'


def endpoints(spec, arch):
    version = quote(spec.runtime.k3s_version, safe='')
    binary = 'k3s-arm64' if arch == 'arm64' else 'k3s'
    urls = {
        'k3s_source': f'https://raw.githubusercontent.com/k3s-io/k3s/{version}/install.sh',
        'github': f'https://github.com/k3s-io/k3s/releases/download/{version}/{binary}',
        'cilium_cli_source': f'https://github.com/cilium/cilium-cli/releases/download/{spec.runtime.cilium_cli_version}/cilium-linux-{arch}.tar.gz',
        'cilium_chart': f'https://helm.cilium.io/cilium-{spec.runtime.cilium_version}.tgz',
        'quay': 'https://quay.io/v2/',
        'registry_k8s': 'https://registry.k8s.io/v2/',
        'docker_hub': 'https://registry-1.docker.io/v2/',
        'ghcr': 'https://ghcr.io/v2/',
    }
    host = registry(spec.workload.image)
    key = {'docker.io': 'docker_hub', 'ghcr.io': 'ghcr', 'quay.io': 'quay', 'registry.k8s.io': 'registry_k8s'}.get(host, 'workload_registry')
    if key == 'workload_registry':
        urls[key] = f'https://{host}/v2/'
    required = {'k3s_source', 'github', 'cilium_cli_source', 'cilium_chart', 'quay', 'registry_k8s', 'docker_hub', key}
    if spec.exposure.type == 'cloudflare-tunnel':
        # TCP/TLS 7844 only; not a QUIC test or authenticated connector check.
        urls['cloudflare_tunnel'] = 'https://region1.v2.argotunnel.com:7844/'
    urls.update(spec.runtime.endpoint_overrides)
    return urls, required


def probe(key, url, timeout):
    started = time.monotonic()
    try:
        method = [] if key in ('quay', 'registry_k8s', 'docker_hub', 'ghcr', 'workload_registry') else ['--head']
        result = subprocess.run(['curl', '--silent', '--show-error', *method, '--location',
                                 '--connect-timeout', str(timeout), '--max-time', str(timeout),
                                 '--output', '/dev/null', '--write-out', '%{http_code} %{remote_ip}', url],
                                capture_output=True, text=True, timeout=timeout + 1)
        parts = result.stdout.split()
        status = int(parts[0]) if parts and parts[0].isdigit() else 0
        # Registry 401 is a reachable authentication challenge, NOT image/credential authorization.
        reachable = result.returncode == 0 and (status in (200, 401) or (key == 'cloudflare_tunnel' and status > 0))
        return {'available': reachable, 'url': url, 'protocol': 'TLS/TCP', 'http_status': status or None,
                'resolved_ip': parts[1] if len(parts) > 1 else None, 'curl_exit_code': result.returncode,
                'error': result.stderr.strip()[-512:] or None, 'duration_seconds': round(time.monotonic()-started, 3)}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'available': False, 'url': url, 'protocol': 'TLS/TCP', 'error': str(exc)[:512],
                'duration_seconds': round(time.monotonic()-started, 3)}


def check(spec, arch, offline=False):
    capabilities = dict.fromkeys(KEYS)
    if offline:
        return capabilities, {'external_checks_skipped': True, 'reason': 'explicit offline mode', 'required_unavailable': []}
    urls, required = endpoints(spec, arch)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        futures = {key: pool.submit(probe, key, url, spec.runtime.preflight_timeout_seconds) for key, url in urls.items()}
        details = {key: future.result() for key, future in futures.items()}
    capabilities.update({key: value['available'] for key, value in details.items()})
    capabilities['dns'] = (False if any(d.get('curl_exit_code') == 6 for k, d in details.items() if k in required)
                           else True if any(d.get('resolved_ip') for d in details.values()) else None)
    capabilities['https_443'] = any(d['available'] for d in details.values() if (urlsplit(d['url']).port or 443) == 443)
    details.update(required=sorted(required), required_unavailable=sorted(key for key in required if not capabilities[key]),
                   duration_seconds=round(time.monotonic()-started, 3),
                   note='TLS reachability/registry challenge only; image authorization, QUIC and tunnel credentials are not proven')
    return capabilities, details
