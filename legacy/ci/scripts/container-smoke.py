#!/usr/bin/env python3
"""Exercise real container entrypoints against local fixtures; never dispatch CI or touch a cluster."""
import argparse
from contextlib import contextmanager
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import secrets
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def docker(*args, timeout=90, **kwargs):
    return subprocess.run(["docker", *args], check=True, text=True, capture_output=True, timeout=timeout, **kwargs).stdout.strip()


def remove_container(name):
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        path = values.get("src") if tag == "script" else values.get("href") if tag == "link" and values.get("rel") == "stylesheet" else None
        if path and path.startswith("/"):
            self.paths.append(path)


def http(url, headers=None, data=None):
    try:
        response = urlopen(Request(url, headers=headers or {}, data=data), timeout=5)
    except HTTPError as error:
        response = error
    with response:
        return response.status, dict(response.headers), response.read()


@contextmanager
def mock_api(token):
    """A host fixture reachable through Docker's host gateway. No credentials or cloud clients."""
    calls = []
    marker = secrets.token_hex(16)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            calls.append({"path": self.path, "method": self.command, "host": self.headers.get('Host'),
                          "origin": self.headers.get('Origin'), "body": body,
                          "authorization": self.headers.get('Authorization'),
                          "authenticated": self.headers.get('Authorization') == 'Bearer ' + token,
                          "idempotency_key": self.headers.get('Idempotency-Key')})
            code = 200 if calls[-1]['authenticated'] else 401
            content = json.dumps({"proxy_smoke": marker}).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        do_POST = do_GET

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('0.0.0.0', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.server_port, calls, marker
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def verify_dashboard(endpoint, token, calls, marker):
    status, _, document = http(endpoint)
    assert status == 200 and token.encode() not in document, 'Dashboard HTML must not expose the server token'
    assets = Assets()
    assets.feed(document.decode())
    assert assets.paths, 'Vite output assets missing'
    for path in assets.paths:
        status, _, content = http(endpoint + path)
        assert status == 200 and content and token.encode() not in content, 'Asset missing or contains server credentials'
    # This request has no browser Authorization. The actual Nginx entrypoint injects its mounted token.
    headers = {'Host': 'console.example.test', 'Origin': 'https://console.example.test'}
    status, _, content = http(endpoint + '/api/v1/targets', headers)
    assert status == 200 and json.loads(content) == {'proxy_smoke': marker}, 'Same-origin API proxy did not reach fixture'
    assert token.encode() not in content, 'Proxy response exposed its internal token'
    assert calls[-1]['authenticated'], 'Nginx did not supply the internal bearer'
    assert calls[-1]['host'] == headers['Host'] and calls[-1]['origin'] == headers['Origin'], 'Proxy lost Host/Origin'
    # Client credentials cannot override the server-owned credential, and request bodies/keys reach the backend.
    status, _, _ = http(endpoint + '/api/v1/deployments',
                        {**headers, 'Authorization': 'Bearer browser-value-must-be-replaced',
                         'Idempotency-Key': 'proxy-smoke', 'Content-Type': 'application/octet-stream'}, b'local-source-fixture')
    assert status == 200 and calls[-1]['authenticated'], 'Browser supplied authorization reached the private API'
    assert calls[-1]['path'] == '/api/v1/deployments' and calls[-1]['method'] == 'POST'
    assert calls[-1]['body'] == b'local-source-fixture' and calls[-1]['idempotency_key'] == 'proxy-smoke'
    client_token = 'client.' + secrets.token_urlsafe(32)
    status, _, _ = http(endpoint + '/api/v1/enrollments/enrollment_1/claims',
                        {**headers, 'Authorization': 'Bearer ' + client_token,
                         'Content-Type': 'application/json'}, b'{}')
    assert status == 401 and calls[-1]['authorization'] == 'Bearer ' + client_token, \
        'Personal client bearer was replaced by the dashboard credential'
    status, _, manifest_raw = http(endpoint + '/personal/manifest.json')
    manifest = json.loads(manifest_raw)
    assert status == 200 and manifest['version'] == 1 and manifest['release'] == manifest['artifact_sha256']
    for path, digest, size in ((manifest['installer_path'], manifest['installer_sha256'], manifest['installer_size']),
                               (manifest['artifact_path'], manifest['artifact_sha256'], manifest['artifact_size'])):
        status, _, content = http(endpoint + path)
        assert status == 200 and len(content) == size and hashlib.sha256(content).hexdigest() == digest, \
            'Dashboard immutable personal release is incomplete'
    for path in ['/mcp', '/mcp/', '/.well-known/oauth-protected-resource']:
        status, _, _ = http(endpoint + path, headers)
        assert status == 401 and calls[-1]['path'] == path, 'MCP proxy did not reach fixture'
        assert not calls[-1]['authenticated'], 'MCP proxy injected the internal API token'
        assert calls[-1]['host'] == headers['Host'], 'MCP proxy lost Host'
    for path in ['/railshot-proxy.conf', '/start.sh', '/run/secrets/api-token', '/.env', '/.git/config']:
        status, _, content = http(endpoint + path)
        assert status == 404 and token.encode() not in content, 'Private configuration is reachable as a static asset'


def native_api_smoke(image):
    name = 'railshot-native-smoke-' + secrets.token_hex(6)
    try:
        print(docker('run', '--rm', '--name', name, '--network', 'none', '--read-only', '--tmpfs', '/tmp',
                     '--user', '1000:1000', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                     '-e', 'RAILSHOT_STATE_DIR=/tmp/railshot-state', '--entrypoint', 'python3',
                     image, '/app/apps/api/runtime-smoke.py', timeout=180))
    finally:
        remove_container(name)


def state_init_smoke(image):
    # Exercise restart on the actual PVC layout without network or real secrets.
    script = '''set -eu
mkdir -p /var/lib/railshot/repository
git -C /var/lib/railshot/repository init -b deployment/apps >/dev/null
git -C /var/lib/railshot/repository remote add origin https://github.com/Jasmin-Softbank/Railshot.git
printf '{"fixture":true}' > /run/config/cd.json
printf '{"legacy":true}' > /run/config/observer.json
node /app/apps/api/prepare-state.js
node /app/apps/api/prepare-state.js
node -e "const fs=require('node:fs');const p='/var/lib/railshot/config';if((fs.statSync(p).mode&511)!==448||(fs.statSync(p+'/cd.json').mode&511)!==384||!JSON.parse(fs.readFileSync(p+'/cd.json')).fixture)process.exit(1)"
mkdir -p /var/lib/railshot/state/observer
printf '{"registered":"kept"}' > /var/lib/railshot/state/observer/product.json
printf '{"migrated":true}' > /var/lib/railshot/config/observer.json
export RAILSHOT_OBSERVER_PRODUCT_FILE=/var/lib/railshot/state/observer/product.json
node /app/apps/api/prepare-state.js
node /app/apps/api/prepare-state.js
node -e "const fs=require('node:fs');if(!JSON.parse(fs.readFileSync('/var/lib/railshot/config/observer.json')).migrated||JSON.parse(fs.readFileSync(process.env.RAILSHOT_OBSERVER_PRODUCT_FILE)).registered!=='kept')process.exit(1)"
git -C /var/lib/railshot/repository symbolic-ref HEAD refs/heads/unregistered
if node /app/apps/api/prepare-state.js >/dev/null 2>&1; then exit 1; fi
'''
    name = 'railshot-init-smoke-' + secrets.token_hex(6)
    try:
        docker('run', '--rm', '--name', name, '--network', 'none', '--read-only', '--tmpfs', '/tmp',
               '--tmpfs', '/var/lib/railshot:uid=1000,gid=1000', '--tmpfs', '/run/config:uid=1000,gid=1000',
               '--user', '1000:1000', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--entrypoint', 'sh', image, '-c', script)
    finally:
        remove_container(name)


def web_smoke(component, image, token_file, token, upstream=None):
    port = '8080' if component == 'dashboard' else '4173'
    options = ['--read-only', '--tmpfs', '/tmp', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '-p', f'127.0.0.1::{port}', '-e', 'RAILSHOT_API_TOKEN_FILE=/run/secrets/api-token',
               '-v', f'{token_file}:/run/secrets/api-token:ro']
    if component == 'dashboard':
        options += ['--add-host', 'host.docker.internal:host-gateway',
                    '-e', f'RAILSHOT_API_UPSTREAM=host.docker.internal:{upstream[0]}',
                    '-e', f'RAILSHOT_MCP_UPSTREAM=host.docker.internal:{upstream[0]}']
    else:
        options += ['-e', 'RAILSHOT_BIND_HOST=0.0.0.0', '-e', 'RAILSHOT_ALLOWED_HOSTS=localhost,127.0.0.1',
                    '-e', 'RAILSHOT_STATE_DIR=/tmp/railshot-state', '-e', 'RAILSHOT_TARGET_ID=container-smoke',
                    '-e', 'GITHUB_TOKEN=synthetic-container-fixture-no-network-use']
    name = 'railshot-' + component + '-smoke-' + secrets.token_hex(6)
    try:
        container = docker('run', '-d', '--name', name, *options, image)
        endpoint = 'http://' + docker('port', container, port).splitlines()[0]
        for attempt in range(50):
            try:
                if http(endpoint + '/healthz')[0] != 200:
                    raise ConnectionError('Health check not ready')
                break
            except (URLError, ConnectionError):
                if attempt == 49:
                    raise
                time.sleep(0.2)
        if component == 'dashboard':
            verify_dashboard(endpoint, token, upstream[1], upstream[2])
        else:
            # The synthetic GitHub value initializes durable state but no dispatch/foreign lookup is allowed.
            for headers, expected in [({}, 401), ({'Authorization': 'Bearer ' + token}, 404), ({'Host': 'untrusted.invalid'}, 403)]:
                assert http(endpoint + '/api/runs/1', headers)[0] == expected, 'API access or run-binding boundary failed'
            status, _, content = http(endpoint + '/api/v1/targets', {'Authorization': 'Bearer ' + token})
            assert status == 200 and json.loads(content)['items'][0]['id'] == 'container-smoke', 'Product state did not initialize'
            # Build the real installer from image files; source-checkout tests cannot catch missing COPY inputs.
            docker('exec', container, 'node', '--input-type=module', '-e',
                   "import assert from 'node:assert/strict'; import {buildOpenStackInstaller} from '/app/apps/api/src/openstack/installer.js'; "
                   "const {archive,tokenClient}=await buildOpenStackInstaller(); assert(archive.length>0); assert(tokenClient.length>0);")
            docker('exec', container, 'node', '-e', "const fs=require('node:fs');if(process.getuid()!==1000||(fs.statSync('/tmp/railshot-state').mode&0o777)!==0o700)process.exit(1)")
    finally:
        remove_container(name)


def mcp_http_smoke(image):
    name = 'railshot-mcp-http-smoke-' + secrets.token_hex(6)
    try:
        container = docker('run', '-d', '--name', name, '--read-only', '--cap-drop', 'ALL',
                           '--security-opt', 'no-new-privileges', '-p', '127.0.0.1::4185',
                           '-e', 'RAILSHOT_MCP_PORT=tcp://10.52.0.1:4185',
                           image, 'node', 'src/remote-mcp.js')
        endpoint = 'http://' + docker('port', container, '4185').splitlines()[0]
        for attempt in range(50):
            try:
                status, _, body = http(endpoint + '/healthz')
                assert status == 200 and body == b'ok\n'
                break
            except (URLError, ConnectionError, TimeoutError, AssertionError):
                if attempt == 49:
                    raise AssertionError('Remote MCP did not start with Kubernetes Service environment')
                time.sleep(0.2)
        status, _, body = http(endpoint + '/.well-known/oauth-protected-resource/mcp')
        assert status == 200 and json.loads(body)['resource'] == 'https://railshot.io/mcp'
    finally:
        remove_container(name)


def smoke(component, image):
    if component == 'personal-gateway':
        # Native WireGuard needs a Linux kernel and NET_ADMIN. Image admission
        # still proves that only the fixed helper and required tools are present.
        for command in (['python3', '/opt/railshot/deployment/scripts/personal_gateway_service.py', '--help'],
                        ['python3', '/opt/railshot/deployment/scripts/personal_wireguard.py', '--help'],
                        ['wg', '--version'], ['ip', '-Version'], ['iptables', '--version']):
            docker('run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                   '--entrypoint', command[0], image, *command[1:])
        return
    if component == 'ci-runner':
        # Registration and host firewall integration need the dedicated CI VM.
        print(docker('run', '--rm', '--network', 'none', image, '--check-image'))
        return
    if component == 'mcp':
        # stdin stays open until the initialize response arrives; stderr is not MCP data.
        name = 'railshot-mcp-smoke-' + secrets.token_hex(6)
        process = subprocess.Popen(['docker', 'run', '--rm', '-i', '--name', name, '--network', 'none', image],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        import selectors
        try:
            request = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'container-smoke', 'version': '1'}}}
            process.stdin.write(json.dumps(request) + '\n')
            process.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(30), 'MCP initialize timed out'
                response = json.loads(process.stdout.readline())
            assert response.get('id') == 1 and response.get('result', {}).get('serverInfo'), response
            process.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')
            process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}}) + '\n')
            process.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                assert selector.select(30), 'MCP tools/list timed out'
                tools = json.loads(process.stdout.readline())
            names = {item['name'] for item in tools.get('result', {}).get('tools', [])}
            assert tools.get('id') == 2 and {'deploy_repository', 'get_deployment'} <= names, tools
        finally:
            remove_container(name)
            process.communicate(timeout=15)
        mcp_http_smoke(image)
        return
    if component == 'api':
        native_api_smoke(image)
        state_init_smoke(image)
    token = secrets.token_hex(32)
    with tempfile.TemporaryDirectory(prefix='railshot-container-smoke-') as directory:
        token_file = Path(directory) / 'token'
        token_file.write_text(token)
        # Random test token only; private parent directory, readable nonroot bind mount.
        token_file.chmod(0o444)
        if component == 'dashboard':
            with mock_api(token) as upstream:
                web_smoke(component, image, token_file, token, upstream)
        else:
            web_smoke(component, image, token_file, token)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('component', choices=('dashboard', 'api', 'mcp', 'personal-gateway', 'ci-runner'))
    parser.add_argument('image')
    args = parser.parse_args()
    smoke(args.component, args.image)
    print(f'{args.component}: container smoke passed')
