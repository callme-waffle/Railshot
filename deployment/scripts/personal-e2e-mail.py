#!/usr/bin/env python3
"""Authenticated, private-file mail receiver for isolated connection E2E only."""
import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time


def private_token(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_size > 513:
        raise ValueError('Private mail token required')
    value = path.read_text().strip()
    if not re.fullmatch(r'[A-Za-z0-9._~-]{32,512}', value):
        raise ValueError('Invalid mail token')
    return value


def save(outbox, message):
    digest = hashlib.sha256(message['to'].encode()).hexdigest()
    destination = outbox / f"{digest}-{message['purpose']}.json"
    fd, temporary = tempfile.mkstemp(prefix='.mail-', dir=outbox)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({**message, 'received_at': int(time.time())}, stream)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def answer(self, status):
        self.send_response(status)
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()

    def do_GET(self):
        self.answer(200 if self.path == '/healthz' else 404)

    def do_POST(self):
        if self.path != '/messages':
            self.answer(404)
            return
        if not hmac.compare_digest(self.headers.get('Authorization', ''), f'Bearer {self.server.token}'):
            self.answer(401)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 4096 or self.headers.get('Transfer-Encoding'):
                raise ValueError()
            self.connection.settimeout(5)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or set(body) != {'to', 'purpose', 'token'} \
                    or not isinstance(body['to'], str) or len(body['to']) > 254 \
                    or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', body['to']) \
                    or body['purpose'] not in {'verify', 'reset'} \
                    or not isinstance(body['token'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', body['token']):
                raise ValueError()
            save(self.server.outbox, body)
        except Exception:
            self.answer(422)
            return
        self.answer(204)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--outbox', type=Path, required=True)
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=4186)
    args = parser.parse_args()
    token = private_token(args.token_file)
    info = args.outbox.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Private outbox required')
    with ThreadingHTTPServer((args.bind, args.port), Handler) as server:
        server.token, server.outbox = token, args.outbox
        server.serve_forever()


if __name__ == '__main__':
    main()
