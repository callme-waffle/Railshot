#!/usr/bin/env python3
"""Remove only this attempt's matching bootstrap token using a UID precondition."""
import base64
import json
import re
import subprocess
import sys


def cleanup(token):
    if re.fullmatch(r'[a-f0-9]{6}\.[a-f0-9]{16}', token) is None:
        raise ValueError('invalid bootstrap token')
    identity, secret = token.split('.')
    prefix = ['/usr/bin/kubectl', '--kubeconfig=/etc/kubernetes/admin.conf', '--request-timeout=10s', '-n', 'kube-system']
    result = subprocess.run([*prefix, 'get', 'secret', 'bootstrap-token-' + identity,
                             '--ignore-not-found', '-o', 'json'], capture_output=True, check=True, timeout=15)
    if not result.stdout.strip():
        return
    observed = json.loads(result.stdout)
    data = observed.get('data', {})
    if (base64.b64decode(data.get('token-id', ''), validate=True).decode() != identity
            or base64.b64decode(data.get('token-secret', ''), validate=True).decode() != secret):
        raise ValueError('bootstrap token ownership differs; no deletion performed')
    options = {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': observed['metadata']['uid']}}
    subprocess.run([*prefix, 'delete', '--raw', '/api/v1/namespaces/kube-system/secrets/bootstrap-token-' + identity,
                    '-f', '-'], input=json.dumps(options).encode(), capture_output=True, check=True, timeout=15)


if __name__ == '__main__':
    cleanup(sys.argv[1])
