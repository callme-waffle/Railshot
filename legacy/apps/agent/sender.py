#!/usr/bin/env python3
"""Server-side fixed-action SSH transport bound to the explicit management route."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import signal
import time
import subprocess
import sys
import tempfile
if __package__:
    from .protocol import COMMAND, MAX_RESPONSE_BYTES, ProtocolError, decode_request, sanitize_instances, _unique_object
else:
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    from protocol import COMMAND, MAX_RESPONSE_BYTES, ProtocolError, decode_request, sanitize_instances, _unique_object


def _private_input(value):
    path = Path(value).absolute()
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',str(path)):
        raise ValueError('Unsupported credential path')
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    from infrastructure.providers.openstack.access import _trusted_input_file
    return _trusted_input_file(path)


def _check_response(raw,request):
    if len(raw)>MAX_RESPONSE_BYTES:
        raise ProtocolError('response_too_large')
    try:
        response=json.loads(raw.decode('utf-8'),object_pairs_hook=_unique_object)
    except (ValueError,UnicodeError):
        raise ProtocolError('transport_failed') from None
    if not isinstance(response,dict) or set(response)!={'version','job_id','action','ok','result','error'}:
        raise ProtocolError('transport_failed')
    if type(response['version']) is not int or response['version']!=1 or response['job_id']!=request['job_id'] or response['action']!=request['action'] or type(response['ok']) is not bool:
        raise ProtocolError('transport_failed')
    if response['ok']:
        if response['error'] is not None:
            raise ProtocolError('transport_failed')
        response['result']=sanitize_instances(response['result'])
    else:
        from_code=response.get('error')
        if response['result'] is not None or not isinstance(from_code,dict) or set(from_code)!={'code'} or from_code['code'] not in {'invalid_request','request_too_large','command_rejected','credentials_unavailable','execution_failed','invalid_upstream_result','response_too_large','transport_failed'}:
            raise ProtocolError('transport_failed')
    return response


def _run_ssh_bounded(command, payload, timeout=45):
    """Drain stdout while running and kill the local transport at the byte/deadline limit."""
    with tempfile.TemporaryFile() as input_file:
        input_file.write(payload)
        input_file.seek(0)
        process = subprocess.Popen(command, stdin=input_file, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, shell=False, start_new_session=True)
        output = bytearray()
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ProtocolError('transport_failed')
                    if not selector.select(remaining):
                        raise ProtocolError('transport_failed')
                    chunk = os.read(process.stdout.fileno(), min(65536,MAX_RESPONSE_BYTES+1-len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > MAX_RESPONSE_BYTES:
                        raise ProtocolError('response_too_large')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError('transport_failed')
            returncode = process.wait(timeout=remaining)
            return returncode, bytes(output)
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            process.stdout.close()


def send_job(request,host,user,key_path,known_hosts,interface,runner=subprocess.run):
    request=decode_request(json.dumps(request).encode())
    host=str(ipaddress.ip_address(host))
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',user):
        raise ValueError('Invalid SSH account')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,15}',interface):
        raise ValueError('Invalid interface')
    key_path=_private_input(key_path)
    known_hosts=_private_input(known_hosts)
    try:
        route=runner(['ip','-j','route','get',host],capture_output=True,text=True,timeout=5,shell=False)
        routes=json.loads(route.stdout)
        if route.returncode or not isinstance(routes,list) or len(routes)!=1 or routes[0].get('dev')!=interface:
            raise ProtocolError('transport_failed')
        source=str(ipaddress.ip_address(routes[0]['prefsrc']))
        command=['ssh','-F','/dev/null','-T','-o','BatchMode=yes','-o','IdentitiesOnly=yes',
                 '-o','IdentityAgent=none','-o','StrictHostKeyChecking=yes','-o','GlobalKnownHostsFile=/dev/null',
                 '-o','UserKnownHostsFile='+str(known_hosts),'-o','ConnectTimeout=10',
                 '-o','ServerAliveInterval=5','-o','ServerAliveCountMax=2','-b',source,
                 '-i',str(key_path),user+'@'+host,COMMAND]
        payload=json.dumps(request).encode()
        if runner is subprocess.run:
            returncode,raw=_run_ssh_bounded(command,payload)
        else:
            # Explicit runner injection is a test seam; real SSH always uses the bounded reader.
            with tempfile.TemporaryFile() as output:
                completed=runner(command,input=payload,stdout=output,stderr=subprocess.DEVNULL,
                                 timeout=45,shell=False)
                output.seek(0)
                raw=output.read(MAX_RESPONSE_BYTES+1)
                returncode=completed.returncode
        if returncode not in (0,1):
            raise ProtocolError('transport_failed')
        response=_check_response(raw,request)
        if (returncode==0) != response['ok']:
            raise ProtocolError('transport_failed')
        return response
    except ProtocolError:
        raise
    except (OSError,subprocess.TimeoutExpired,ValueError,KeyError,TypeError):
        raise ProtocolError('transport_failed') from None


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',required=True)
    parser.add_argument('--user',default='root')
    parser.add_argument('--key',type=Path,required=True)
    parser.add_argument('--known-hosts',type=Path,required=True)
    parser.add_argument('--interface',required=True,help='Verified management route interface')
    parser.add_argument('--job-id',required=True)
    args=parser.parse_args()
    request={'version':1,'job_id':args.job_id,'action':'instance.list','params':{}}
    try:
        result=send_job(request,args.host,args.user,args.key,args.known_hosts,args.interface)
    except Exception:
        print(json.dumps({'version':1,'job_id':args.job_id if re.fullmatch(r'[A-Za-z0-9_-]{1,64}',args.job_id) else None,
                          'action':'instance.list','ok':False,'result':None,'error':{'code':'transport_failed'}}))
        return 1
    print(json.dumps(result))
    return 0 if result['ok'] else 1


if __name__=='__main__':
    raise SystemExit(main())
