import base64
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from apps.agent.runner import execute_request,run_stream
from apps.agent.protocol import COMMAND,ProtocolError
from apps.agent.sender import send_job
from apps.agent.install_forced_command import authorized_key_line
import pytest

REQUEST={'version':1,'job_id':'job-1','action':'instance.list','params':{}}


def test_execute_local_credentials_fixed_action():
    class CLI:
        def __init__(self,credentials):
            assert credentials=={'application_credential_secret':'secret'}
        def run(self,args):
            assert args==['server','list']
            return [{'ID':'id','Name':'vm','Status':'ACTIVE','Networks':'hidden'}]
    output=execute_request(json.dumps(REQUEST).encode(),COMMAND,lambda:{'application_credential_secret':'secret'},CLI)
    assert output['result']==[{'id':'id','name':'vm','status':'ACTIVE'}]
    assert output['ok']


def test_missing_vault_is_honest_error():
    def load():
        raise FileNotFoundError('/secret/path')
    response=execute_request(json.dumps(REQUEST).encode(),COMMAND,load,None)
    assert response['error']=={'code':'credentials_unavailable'}
    assert '/secret' not in json.dumps(response)


def test_sender_route_and_strict_transport(tmp_path):
    key=tmp_path/'key';known=tmp_path/'known'
    for p in (key,known):
        p.write_text('test');p.chmod(0o600)
    def run(args,**kwargs):
        if args[0]=='ip':
            return SimpleNamespace(returncode=0,stdout='[{"dev":"ens3","prefsrc":"10.44.0.1"}]')
        assert args[-1]==COMMAND
        assert 'StrictHostKeyChecking=yes' in args
        assert '-T' in args
        assert args[args.index('-b')+1]=='10.44.0.1'
        assert kwargs['shell'] is False
        assert json.loads(kwargs['input'])==REQUEST
        kwargs['stdout'].write(json.dumps({'version':1,'job_id':'job-1','action':'instance.list','ok':True,'result':[],'error':None}).encode())
        return SimpleNamespace(returncode=0)
    assert send_job(REQUEST,'10.44.0.2','root',key,known,interface='ens3',runner=run)['ok']


def test_sender_refuses_wrong_route(tmp_path):
    key=tmp_path/'key';known=tmp_path/'known'
    for p in (key,known):
        p.write_text('test');p.chmod(0o600)
    def run(args,**kwargs):
        assert args[0]=='ip'
        return SimpleNamespace(returncode=0,stdout='[{"dev":"eth0","prefsrc":"10.44.0.1"}]')
    with pytest.raises(ProtocolError):
        send_job(REQUEST,'10.44.0.2','root',key,known,interface='ens3',runner=run)


def test_forced_key_line_and_injection_rejection():
    blob=b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20'+b'a'*32
    pub='ssh-ed25519 '+base64.b64encode(blob).decode()
    line=authorized_key_line(pub,'10.44.0.1','/opt/jasmin/python','/opt/jasmin/source','/etc/jasmin')
    assert line.startswith('restrict,from="10.44.0.1/32",command=')
    assert ' -I ' in line
    with pytest.raises(ValueError):
        authorized_key_line(pub,'10.44.0.1','/bin/python;bad','/opt/source','/etc/jasmin')


def test_direct_isolated_runner_rejects_command_without_vault():
    result=subprocess.run([sys.executable,'-I',str(ROOT/'apps/agent/runner.py'),'--repo',str(ROOT),'--config-dir','/no/vault'],
                          input='',text=True,capture_output=True)
    assert result.returncode==1
    assert json.loads(result.stdout)['error']=={'code':'command_rejected'}
    assert result.stderr==''


def test_duplicate_response_keys_rejected():
    from apps.agent.sender import _check_response
    with pytest.raises(ProtocolError):
        _check_response(b'{"version":1,"version":1,"job_id":"job-1","action":"instance.list","ok":true,"result":[],"error":null}',REQUEST)


def test_real_transport_output_limit():
    from apps.agent.sender import _run_ssh_bounded
    with pytest.raises(ProtocolError,match='response_too_large'):
        _run_ssh_bounded([sys.executable,'-c','import os; os.write(1,b"x"*2000000)'],b'',timeout=3)


def test_real_transport_deadline():
    from apps.agent.sender import _run_ssh_bounded
    with pytest.raises(ProtocolError,match='transport_failed'):
        _run_ssh_bounded([sys.executable,'-c','import time; time.sleep(10)'],b'',timeout=0.05)


def test_real_transport_success_bytes():
    from apps.agent.sender import _run_ssh_bounded
    code,raw=_run_ssh_bounded([sys.executable,'-c','import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'],b'hello',timeout=3)
    assert (code,raw)==(0,b'hello')


def test_sender_requires_explicit_management_interface():
    result=subprocess.run([sys.executable,str(ROOT/'apps/agent/sender.py'),
                           '--host','10.44.0.2','--key','/unused','--known-hosts','/unused',
                           '--job-id','job-1'],text=True,capture_output=True,timeout=5)
    assert result.returncode==2
    assert '--interface' in result.stderr
    assert not result.stdout
