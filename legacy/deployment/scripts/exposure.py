"""Optional existing public URL hook, isolated from workload readiness."""
from urllib.request import ProxyHandler, Request, build_opener


def apply(spec, result, offline=False):
    result.exposure_status = {'requested': spec.exposure.type, 'status': 'ready', 'public_endpoint': None, 'reason': None}
    if spec.exposure.type == 'nodeport':
        return
    reason = 'offline_mode' if offline else 'connector_not_configured'
    if not offline and spec.exposure.public_url:
        try:
            base = spec.exposure.public_url.rstrip('/')
            with build_opener(ProxyHandler({})).open(Request(base + spec.workload.health_path), timeout=3) as response:
                if response.status != 200:
                    raise ValueError(f'HTTP {response.status}')
                if spec.workload.sample_content and response.read(1024).strip() != b'Railshot Runtime OK':
                    raise ValueError('Unexpected sample public response')
            result.exposure_status.update(status='ready', public_endpoint=base, reason=None)
            result.endpoint, result.endpoint_scope = base, 'public-url-verified'
            return
        except (OSError, ValueError) as exc:
            reason = 'public_url_unavailable: ' + str(exc)[:256]
    result.exposure_status.update(status='degraded', reason=reason)
