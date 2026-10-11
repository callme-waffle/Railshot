#!/usr/bin/env python3
"""Disruptive acceptance tests for a dedicated Linux node; no CSP provisioning."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone
from input_adapter import parse_input
from render import render_policy

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description='Dedicated-node tests; includes full K3s deletion')
    parser.add_argument('--disposable-node',action='store_true',required=True)
    parser.add_argument('--results',default='/var/tmp/railshot-runtime-tests')
    args=parser.parse_args()
    if sys.platform!='linux' or os.geteuid()!=0:
        parser.error('Run as root on a dedicated Linux node')
    results=Path(args.results); results.mkdir(parents=True,exist_ok=True)
    rows=[]
    baseline=json.loads((ROOT/'scripts/tests/fixtures/openstack.json').read_text())
    active=baseline
    current='initialization'

    def kube(*argv):
        return subprocess.check_output(['/usr/local/bin/k3s','kubectl','--request-timeout=10s',*argv],text=True,stderr=subprocess.STDOUT)

    def runtime(name,action,data,expected='ready',flags=()):
        started=time.monotonic()
        with (results/f'{name}.log').open('w') as log:
            process=subprocess.run([sys.executable,str(ROOT/'scripts/runtime.py'),action,*flags],input=json.dumps(data),
                                   text=True,stdout=subprocess.PIPE,stderr=log,timeout=1200)
        (results/f'{name}.json').write_text(process.stdout)
        output=json.loads(process.stdout)
        assert output['status']==expected,output
        assert process.returncode==(1 if expected=='failed' else 0),output
        assert output['provider']==data['provider'] and output['environment_id']==data['environment_id']
        assert (output['error'] is not None)==(expected=='failed')
        if expected=='failed': assert output['states'][-1]['state']=='FAILED'
        row={'test':name,'status':'PASS','duration_seconds':round(time.monotonic()-started,3)}
        rows.append(row); print(f'[PASS] {name}',file=sys.stderr)
        return output

    def mutated(**workload):
        data=copy.deepcopy(baseline); data['workload'].update(workload); return data

    try:
        current='bootstrap'
        if not Path('/usr/local/bin/k3s').exists() and not Path('/var/lib/rancher/k3s').exists():
            (results/'clean-state.txt').write_text('CLEAN_NODE_CONFIRMED\n')
            runtime('clean-install','deploy',baseline)
        else:
            rows.append({'test':'clean-install','status':'SKIP','reason':'already installed; full cleanup/reinstall tested later'})
            runtime('existing-bootstrap','deploy',baseline)
        ns=baseline['workload']['namespace']
        uid=kube('-n',ns,'get','deployment','railshot-workload','-o','jsonpath={.metadata.uid}')
        pod_uid=kube('-n',ns,'get','pods','-l','railshot.io/app=railshot-workload','-o','jsonpath={.items[0].metadata.uid}')
        for provider in ('aws','gcp','openstack'):
            current=f'repeat-{provider}'
            data=copy.deepcopy(baseline); data['provider']=provider
            runtime(current,'deploy',data)
            assert kube('-n',ns,'get','deployment','railshot-workload','-o','jsonpath={.metadata.uid}')==uid
            assert kube('-n',ns,'get','pods','-l','railshot.io/app=railshot-workload','-o','jsonpath={.items[0].metadata.uid}')==pod_uid
        current='health-and-endpoint'
        runtime(current,'verify',baseline)
        current='workload-update'
        updated=mutated(image='nginx:1.28.1-alpine')
        runtime(current,'deploy',updated)
        runtime('update-restore','deploy',baseline)
        current='generic-workload'
        generic=copy.deepcopy(baseline)
        generic['workload']={'image':'nginxinc/nginx-unprivileged:1.28.0-alpine','namespace':'railshot-generic','replicas':2,
                             'container_port':8080,'health_path':'/index.html'}
        generic['exposure']['node_port']=30083
        runtime(current,'deploy',generic)
        runtime('generic-verify','verify',generic)
        current='missing-image'
        bad=mutated(image='nginx:railshot-tag-does-not-exist'); bad['runtime']['timeout_seconds']=15
        failure=runtime(current,'deploy',bad,'failed')
        assert failure['error']['stage']=='WORKLOAD_DEPLOYING'
        assert any(word in str(failure['error']) for word in ('ImagePull','pull image','not found','BackOff'))
        runtime('missing-image-restore','deploy',baseline)
        current='health-failure'
        bad=mutated(health_path='/missing-healthz',sample_content=False); bad['runtime']['timeout_seconds']=15
        failure=runtime(current,'deploy',bad,'failed')
        assert '404' in str(failure['error']),failure
        runtime('health-restore','deploy',baseline)
        current='service-drift'
        kube('-n',ns,'patch','service','railshot-workload','--type=merge','-p',
             '{"spec":{"ports":[{"name":"http","port":80,"targetPort":81,"nodePort":30080}]}}')
        failure=runtime(current,'verify',baseline,'failed')
        assert failure['error']['code']=='SERVICE_DRIFT'
        runtime('service-restore','deploy',baseline)
        current='cilium-unhealthy'
        original=kube('-n','kube-system','get','ds','cilium','-o','jsonpath={.spec.template.spec.containers[?(@.name=="cilium-agent")].image}')
        try:
            kube('-n','kube-system','set','image','ds/cilium','cilium-agent=invalid@@@')
            short=copy.deepcopy(baseline); short['runtime']['timeout_seconds']=15
            failure=runtime(current,'verify',short,'failed')
            assert failure['error']['stage']=='CILIUM_READY'
            assert failure['cilium_status']=='failed'
        finally:
            kube('-n','kube-system','set','image','ds/cilium',f'cilium-agent={original}')
            kube('-n','kube-system','rollout','status','ds/cilium','--timeout=180s')
        runtime('cilium-recovered','verify',baseline)
        current='ownership-guard'
        foreign=copy.deepcopy(baseline); foreign['workload']['namespace']='railshot-foreign'
        kube('create','namespace','railshot-foreign')
        try:
            failure=runtime(current,'verify',foreign,'failed')
            assert failure['error']['code']=='OWNERSHIP_CONFLICT'
            failure=runtime('foreign-cleanup-guard','cleanup',foreign,'failed')
            assert failure['error']['code']=='OWNERSHIP_CONFLICT'
        finally:
            kube('delete','namespace','railshot-foreign','--wait=true','--timeout=180s')
        current='network-policy'
        names=('railshot-allowed','railshot-denied')
        try:
            for name,label in zip(names,('allowed','denied')):
                kube('-n',ns,'run',name,'--image=curlimages/curl:8.12.1',f'--labels=railshot.io/client={label}',
                     '--restart=Never','--command','--','sleep','600')
            kube('-n',ns,'wait','--for=condition=Ready',*[f'pod/{name}' for name in names],'--timeout=180s')
            url=f'http://railshot-workload.{ns}.svc.cluster.local/'
            curl=['curl','--silent','--show-error','--connect-timeout','3','--max-time','5','--output','/dev/null','--write-out','%{http_code}',url]
            for name in names:
                assert kube('-n',ns,'exec',name,'--',*curl).strip()=='200'
            subprocess.run(['/usr/local/bin/k3s','kubectl','apply','-f','-'],input=json.dumps(render_policy(parse_input(baseline).spec)),
                           text=True,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            time.sleep(5)
            assert kube('-n',ns,'exec',names[0],'--',*curl).strip()=='200'
            denied=subprocess.run(['/usr/local/bin/k3s','kubectl','-n',ns,'exec',names[1],'--',*curl],text=True,capture_output=True,timeout=15)
            assert denied.returncode!=0 and 'timed out' in denied.stderr.lower(),denied
            (results/'policy-denied.log').write_text(denied.stdout+denied.stderr)
            rows.append({'test':current,'status':'PASS'})
            print(f'[PASS] {current}',file=sys.stderr)
        finally:
            kube('-n',ns,'delete','networkpolicy','railshot-workload-ingress','--ignore-not-found')
            kube('-n',ns,'delete','pod',*names,'--ignore-not-found','--wait=true','--timeout=180s')
        runtime('network-policy-restored','verify',baseline)
        current='cleanup-guard'
        failure=runtime(current,'cleanup',baseline,'failed',('--all',))
        assert failure['error']['code']=='DESTRUCTIVE_ACTION_REQUIRES_FLAG'
        current='workload-cleanup'
        runtime(current,'cleanup',baseline,'cleaned')
        assert not kube('-n',ns,'get','deployment','railshot-workload','--ignore-not-found','-o','name').strip()
        assert kube('-n','railshot-generic','get','deployment','railshot-workload','-o','name').strip()
        runtime('workload-cleanup-repeat','cleanup',baseline,'cleaned')
        runtime('workload-redeploy','deploy',baseline)
        current='full-cleanup'
        runtime(current,'cleanup',baseline,'cleaned',('--all','--disposable-node'))
        assert not Path('/usr/local/bin/k3s').exists() and not Path('/var/lib/rancher/k3s').exists()
        runtime('full-cleanup-repeat','cleanup',baseline,'cleaned',('--all','--disposable-node'))
        runtime('reinstall','deploy',baseline)
        runtime('final-verify','verify',baseline)
        status='passed'
    except (AssertionError,OSError,ValueError,subprocess.SubprocessError) as exc:
        print(f'[FAIL] {current}: {exc}',file=sys.stderr)
        rows.append({'test':current,'status':'FAIL','error':str(exc)})
        status='failed'
        try:
            runtime('emergency-restore','deploy',active)
        except Exception as restore_error:
            rows.append({'test':'emergency-restore','status':'FAIL','error':str(restore_error)})
    summary={'status':status,'timestamp':datetime.now(timezone.utc).isoformat(),'results':rows,
             'note':'Provider labels use the same local node. This is not live AWS/GCP/OpenStack verification.'}
    (results/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary))
    return 0 if status=='passed' else 1


if __name__=='__main__': sys.exit(main())
