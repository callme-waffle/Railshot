#!/usr/bin/env python3
"""Verify personal production inputs, local gateway, and immutable downloads."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import urllib.parse
import urllib.request


TOKEN = re.compile(r'[A-Za-z0-9._~-]{32,512}')
SHA256 = re.compile(r'[a-f0-9]{64}')


def require(value, code):
    if not value:
        raise ValueError(code)


def private_file(path, maximum):
    path = Path(path)
    info = path.lstat()
    require(path.is_absolute() and stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
            and not info.st_mode & 0o077 and info.st_size <= maximum, 'PRIVATE_FILE_INVALID')
    return path


def checked_url(value, allow_http):
    parsed = urllib.parse.urlsplit(value)
    require(parsed.scheme in (('https', 'http') if allow_http else ('https',)) and parsed.hostname
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            and not any(character.isspace() for character in value), 'PUBLIC_URL_INVALID')
    return parsed


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError('PUBLIC_REDIRECT_REJECTED')


def download(url, maximum, expected=None):
    digest, size = hashlib.sha256(), 0
    request = urllib.request.Request(url, headers={'Accept': 'application/octet-stream'})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        require(response.status == 200, 'PUBLIC_FILE_UNAVAILABLE')
        while True:
            chunk = response.read(min(1024 * 1024, maximum - size + 1))
            if not chunk:
                break
            size += len(chunk)
            require(size <= maximum, 'PUBLIC_FILE_TOO_LARGE')
            digest.update(chunk)
    observed = digest.hexdigest()
    if expected is not None:
        require(observed == expected, 'PUBLIC_FILE_HASH_MISMATCH')
    return observed, size


def gateway_health(socket_path, token):
    require(Path(socket_path).is_absolute(), 'GATEWAY_SOCKET_INVALID')
    message = (f'GET /healthz HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {token}'
               '\r\nConnection: close\r\n\r\n').encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(socket_path)
        client.sendall(message)
        response = b''
        while len(response) <= 16 * 1024:
            chunk = client.recv(4096)
            if not chunk:
                break
            response += chunk
    require(len(response) <= 16 * 1024 and b'\r\n\r\n' in response, 'GATEWAY_RESPONSE_INVALID')
    head, body = response.split(b'\r\n\r\n', 1)
    require(head.startswith((b'HTTP/1.0 200 ', b'HTTP/1.1 200 ')), 'GATEWAY_NOT_READY')
    require(json.loads(body) == {'status': 'ready'}, 'GATEWAY_RESPONSE_INVALID')


def check(configuration, allow_http=False):
    path = private_file(configuration, 1024 * 1024)
    config = json.loads(path.read_text())
    require(isinstance(config, dict) and config.get('version') == 1, 'PERSONAL_CONFIG_INVALID')
    required = {'public_url', 'installer_url', 'artifact_url', 'artifact_sha256', 'gateway'}
    require(required <= set(config), 'PERSONAL_CONFIG_INVALID')
    urls = {name: checked_url(config[name], allow_http) for name in
            ('public_url', 'installer_url', 'artifact_url')}
    digest = config['artifact_sha256']
    require(isinstance(digest, str) and SHA256.fullmatch(digest), 'ARTIFACT_HASH_INVALID')
    gateway = config['gateway']
    require(isinstance(gateway, dict) and isinstance(gateway.get('socket_path'), str)
            and isinstance(gateway.get('token_file'), str), 'GATEWAY_CONFIG_INVALID')
    token = private_file(gateway['token_file'], 512).read_text().strip()
    require(TOKEN.fullmatch(token), 'GATEWAY_TOKEN_INVALID')
    gateway_health(gateway['socket_path'], token)

    suffix = f'/personal/{digest}'
    require(urls['installer_url'].path == suffix + '/install.sh'
            and urls['artifact_url'].path == suffix + '/personal-client.tgz'
            and (urls['installer_url'].scheme, urls['installer_url'].netloc)
            == (urls['artifact_url'].scheme, urls['artifact_url'].netloc), 'PERSONAL_RELEASE_PATH_INVALID')
    manifest_url = urllib.parse.urlunsplit((urls['installer_url'].scheme, urls['installer_url'].netloc,
                                           '/personal/manifest.json', '', ''))
    manifest_bytes = bytearray()
    request = urllib.request.Request(manifest_url, headers={'Accept': 'application/json'})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        require(response.status == 200, 'RELEASE_MANIFEST_UNAVAILABLE')
        manifest_bytes.extend(response.read(1024 * 1024 + 1))
    require(len(manifest_bytes) <= 1024 * 1024, 'RELEASE_MANIFEST_INVALID')
    manifest = json.loads(manifest_bytes)
    require(isinstance(manifest, dict) and manifest.get('version') == 1
            and manifest.get('release') == digest and manifest.get('artifact_sha256') == digest
            and manifest.get('installer_path') == urls['installer_url'].path
            and manifest.get('artifact_path') == urls['artifact_url'].path
            and SHA256.fullmatch(manifest.get('installer_sha256', '')), 'RELEASE_MANIFEST_INVALID')
    installer_hash, installer_size = download(config['installer_url'], 4 * 1024 * 1024,
                                                manifest['installer_sha256'])
    artifact_hash, artifact_size = download(config['artifact_url'], 512 * 1024 * 1024, digest)
    require(installer_size == manifest.get('installer_size') and artifact_size == manifest.get('artifact_size'),
            'PUBLIC_FILE_SIZE_MISMATCH')
    return {'ready': True, 'scope': 'personal-production', 'gateway': 'ready',
            'release': digest, 'installer_sha256': installer_hash, 'artifact_sha256': artifact_hash}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--test-allow-http', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(check(args.config, args.test_allow_http), sort_keys=True))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch(r'[A-Z][A-Z0-9_]{2,63}', str(error)) else 'PERSONAL_PREFLIGHT_FAILED'
        parser.exit(2, f'BLOCKED: {code}\n')


if __name__ == '__main__':
    raise SystemExit(main())
