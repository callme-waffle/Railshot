import importlib.util
import json
import os
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest


SPEC = importlib.util.spec_from_file_location('personal_e2e_mail', Path(__file__).resolve().parents[1] / 'personal-e2e-mail.py')
mail = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mail)


def test_authenticated_mail_is_private_and_never_echoed(tmp_path, capsys):
    token = 'test-mail-' + 'a' * 32
    token_file = tmp_path / 'token'
    token_file.write_text(token)
    token_file.chmod(0o600)
    assert mail.private_token(token_file) == token
    token_file.chmod(0o640)
    with pytest.raises(ValueError):
        mail.private_token(token_file)
    token_file.chmod(0o600)
    link = tmp_path / 'link'
    link.symlink_to(token_file)
    with pytest.raises(ValueError):
        mail.private_token(link)
    outbox = tmp_path / 'mail'
    outbox.mkdir(mode=0o700)
    body = {'to': 'e2e@example.test', 'purpose': 'verify', 'token': 'b' * 43}
    with mail.ThreadingHTTPServer(('127.0.0.1', 0), mail.Handler) as server:
        server.token, server.outbox = token, outbox
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/messages'
            for bearer, expected in [('wrong', 401), (token, 204)]:
                request = urllib.request.Request(url, data=json.dumps(body).encode(),
                    headers={'Authorization': f'Bearer {bearer}', 'Content-Type': 'application/json'})
                try:
                    with urllib.request.urlopen(request) as response:
                        assert response.status == expected
                        assert response.read() == b''
                except urllib.error.HTTPError as error:
                    assert error.code == expected
                    assert error.read() == b''
            files = list(outbox.glob('*.json'))
            assert len(files) == 1
            assert files[0].stat().st_mode & 0o777 == 0o600
            assert files[0].stat().st_uid == os.getuid()
            stored = json.loads(files[0].read_text())
            assert {key: stored[key] for key in body} == body
            assert 'e2e@example.test' not in files[0].name
        finally:
            server.shutdown()
            worker.join(timeout=5)
    captured = capsys.readouterr()
    assert token not in captured.out + captured.err
    assert body['token'] not in captured.out + captured.err
