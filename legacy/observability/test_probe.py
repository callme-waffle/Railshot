"""Optional real Blackbox process tests against a loopback HTTP fixture, not a cloud test."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import urlencode
from urllib.request import build_opener, ProxyHandler

from render import HERE, render


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        code = {'/ok': 200, '/failed': 503, '/redirect': 302}.get(self.path, 404)
        self.send_response(code)
        if code == 302:
            self.send_header('Location', '/ok')
        self.end_headers()
        self.wfile.write(b'fixture')

    def log_message(self, *args):
        pass


@unittest.skipUnless(os.environ.get('BLACKBOX'), 'BLACKBOX is not configured')
class RealProbeTests(unittest.TestCase):
    def test_success_failure_and_redirect(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / 'observer'
            render(json.loads((HERE / 'target.example.json').read_text()), out)
            subprocess.run([os.environ['BLACKBOX'], '--config.check',
                            '--config.file=' + str(out / 'blackbox.json')], check=True)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            fixture = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
            thread = threading.Thread(target=fixture.serve_forever, daemon=True)
            thread.start()
            process = None
            opener = build_opener(ProxyHandler({}))
            try:
                with tempfile.TemporaryFile() as log:
                    process = subprocess.Popen([
                        os.environ['BLACKBOX'], '--config.file=' + str(out / 'blackbox.json'),
                        f'--web.listen-address=127.0.0.1:{port}'], stdout=log, stderr=log)
                    for attempt in range(50):
                        try:
                            with opener.open(f'http://127.0.0.1:{port}/-/healthy', timeout=1):
                                break
                        except OSError:
                            if process.poll() is not None:
                                self.fail('Blackbox exited before becoming ready')
                            time.sleep(0.1)
                    else:
                        self.fail('Blackbox startup timeout')
                    for path, expected in [('/ok', 1), ('/failed', 0), ('/redirect', 0)]:
                        with self.subTest(path=path):
                            query = urlencode({'module': 'http_2xx',
                                               'target': f'http://127.0.0.1:{fixture.server_port}{path}'})
                            with opener.open(f'http://127.0.0.1:{port}/probe?{query}', timeout=10) as response:
                                lines = response.read().decode().splitlines()
                            result = next(line for line in lines if line.startswith('probe_success '))
                            self.assertEqual(float(result.split()[1]), expected)
            finally:
                if process is not None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                fixture.shutdown()
                fixture.server_close()
                thread.join(timeout=5)
