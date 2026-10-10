"""Stable service hostnames; deployment revisions do not change the public URL."""
import hashlib
import json
import re
import unicodedata


def service_name(name, owner, environment, domain):
    for value in (name, owner, environment):
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError('nonempty registered service identity required')
    if not isinstance(domain, str) or domain not in ('railshot.io', 'railshot.com'):
        raise ValueError('operator-selected Railshot domain required')
    normalized = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower()
    slug = re.sub('[^a-z0-9]+', '-', normalized).strip('-') or 'app'
    identity = json.dumps([owner, name, environment], ensure_ascii=False, separators=(',', ':')).encode()
    suffix = hashlib.sha256(b'railshot-service-v1\0' + identity).hexdigest()[:12]
    label = slug[:50].rstrip('-') + '-' + suffix
    return {'policy': 'railshot-service-v1', 'label': label, 'hostname': label + '.' + domain}
