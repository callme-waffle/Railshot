#!/usr/bin/env python3
"""Destructive acceptance suite, only on a disposable Linux VM. Restores its own nft table."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT/'scripts'))
from airgap.scripts.bundle import canonical, sha256, verify
from render import NAME


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--results', required=True)
    parser.add_argument('--runtime-timeout', type=int, default=180, help='Readiness timeout; 5..900 seconds, cold registry pulls may need longer')
    parser.add_argument('--disposable-node', required=True, action='store_true')
    args = parser.parse_args()
    if not 5 <= args.runtime_timeout <= 900:
        parser.error('--runtime-timeout must be 5..900 seconds')
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('root on a disposable Linux VM required')
    target = Path(args.results); target.mkdir(parents=True, exist_ok=False)
    data = json.loads((ROOT/'scripts/tests/fixtures/aws.json').read_text())
    data.update(schema_version='0.2', environment_id='railshot-airgap-test')
    data['workload']['namespace'] = 'railshot-airgap-test'
    data['runtime'].update(bundle_path=str(Path(args.bundle).resolve()), preflight_timeout_seconds=5,
                           timeout_seconds=args.runtime_timeout)
    manifest = verify(args.bundle)['manifest']
    data['runtime']['bundle_sha256'] = sha256(Path(args.bundle)/'bundle-manifest.json')
    (target/'bundle-manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    (target/'manifest.sha256').write_text(data['runtime']['bundle_sha256']+'\n')
    rows = []
    blocked = False
    current_case = None
    errors = []
    allowed_existing = Path('/etc/rancher/k3s/config.yaml')
    if Path('/usr/local/bin/k3s').exists() or allowed_existing.exists() or Path('/var/lib/rancher/k3s').exists():
        parser.error('Start with a new/fully cleaned dedicated node; refusing an existing cluster')
    if subprocess.run(['nft', 'list', 'table', 'inet', 'railshot_airgap_test'], capture_output=True).returncode == 0:
        parser.error('Test firewall table already exists; refusing to replace it')

    def command(argv, **kwargs):
        return subprocess.run(argv, capture_output=True, text=True, timeout=kwargs.pop('timeout', 60), **kwargs)

    def runtime(name, action, request, flags=(), success=True, code=None):
        (target/(name+'.input.json')).write_text(json.dumps(request, indent=2)+'\n')
        with (target/(name+'.log')).open('w') as log:
            p = subprocess.run(['bash', str(ROOT/'scripts'/f'{action}.sh'), *flags], input=json.dumps(request),
                               stdout=subprocess.PIPE, stderr=log, text=True, timeout=900)
        (target/(name+'.json')).write_text(p.stdout)
        result = json.loads(p.stdout)
        expected = 'cleaned' if action == 'cleanup' else 'ready'
        if success:
            assert p.returncode == 0 and result['status'] == expected and result['error'] is None, result
        else:
            assert p.returncode == 1 and result['status'] == 'failed' and result['error'], result
            if code:
                assert result['error']['code'] == code, result
        return result

    def case(name, operation):
        nonlocal current_case
        current_case = name
        started = time.monotonic()
        try:
            operation()
            rows.append({'test': name, 'status': 'PASS', 'duration_seconds': round(time.monotonic()-started, 3)})
            print('[PASS] '+name, file=sys.stderr, flush=True)
        except Exception as exc:
            rows.append({'test': name, 'status': 'FAIL', 'error': str(exc)[:4000]})
            print('[FAIL] '+name+': '+str(exc)[:1000], file=sys.stderr, flush=True)
            raise

    def uid():
        p=command(['/usr/local/bin/k3s','kubectl','-n',data['workload']['namespace'],'get','pods','-l',f'railshot.io/app={NAME}','-o','jsonpath={.items[0].metadata.uid}'])
        assert p.returncode==0, p.stderr
        return p.stdout

    def offline():
        request=copy.deepcopy(data); request['runtime']['mode']='offline'
        return request

    def online(bundle=True):
        request=copy.deepcopy(data); request['runtime']['mode']='online'
        if not bundle:
            request['runtime'].pop('bundle_path'); request['runtime'].pop('bundle_sha256')
        return request

    restricted=copy.deepcopy(data)
    restricted['runtime'].update(mode='auto', endpoint_overrides={'quay':'https://127.0.0.1:9/v2/','docker_hub':'https://127.0.0.1:9/v2/'})

    def assert_mode(result, mode):
        assert result['deployment_mode']==mode, result

    try:
        case('01-online-clean', lambda: assert_mode(runtime('01-online-clean','deploy',online(False)), 'online'))
        case('02-online-with-bundle', lambda: assert_mode(runtime('02-online-with-bundle','deploy',online()), 'online'))
        def fallback():
            r=runtime('03-registry-fallback','deploy',restricted)
            assert_mode(r,'airgap'); assert r['fallback_used'] and r['bundle_verified']
            assert r['network_capabilities']['quay'] is False and r['network_capabilities']['docker_hub'] is False
        case('03-registry-fallback',fallback)
        def missing():
            request=copy.deepcopy(restricted); request['runtime'].pop('bundle_path'); request['runtime'].pop('bundle_sha256')
            runtime('04-no-bundle','deploy',request,success=False,code='BUNDLE_REQUIRED')
        case('04-no-bundle',missing)
        # Remove the entire cluster/cache before blocking networking. The next install cannot reuse old containerd content.
        runtime('05-prior-cluster-remove','cleanup',data,('--all','--disposable-node'))
        assert not any(Path(p).exists() for p in ('/usr/local/bin/k3s','/var/lib/rancher/k3s','/etc/rancher/k3s/config.yaml'))
        (target/'offline-clean-node.txt').write_text('K3S_BINARY_DATA_CONFIG_ABSENT\n')
        # Dedicated test VM ONLY. Keep SSH replies; deny external DNS/TCP/UDP and forwarded public traffic.
        route=command(['ip','-j','route','show','default'])
        interface=json.loads(route.stdout)[0]['dev']
        assert interface.replace('-','').replace('_','').isalnum()
        rules=f'''table inet railshot_airgap_test {{
 chain output {{ type filter hook output priority -50; policy accept;
  oifname "{interface}" tcp sport 22 accept
  oifname "{interface}" counter reject
 }}
 chain forward {{ type filter hook forward priority -50; policy accept;
  oifname "{interface}" counter reject
 }}
}}'''
        (target/'offline-firewall.nft').write_text(rules)
        p=command(['nft','-f',str(target/'offline-firewall.nft')]); assert p.returncode==0,p.stderr
        blocked=True
        def complete_offline():
            probes=[]
            for url in ('https://registry-1.docker.io/v2/','https://1.1.1.1/'):
                p=command(['curl','--noproxy','*','--head','--connect-timeout','2','--max-time','3',url])
                assert p.returncode!=0, 'External traffic was not blocked'
                probes.append({'url':url,'exit_code':p.returncode,'stderr':p.stderr})
            (target/'external-block-proof.json').write_text(json.dumps(probes,indent=2)+'\n')
            r=runtime('05-offline-clean','deploy',offline())
            assert_mode(r,'airgap'); assert r['network_details']['external_checks_skipped']
            assert all(v is None for v in r['network_capabilities'].values())
            assert r['preload']['digest_verified']
            objects=command(['/usr/local/bin/k3s','kubectl','-n','kube-system','get','daemonset/cilium','daemonset/cilium-envoy','deployment/cilium-operator','-o','json'])
            assert objects.returncode==0,objects.stderr
            observed=json.loads(objects.stdout)
            for item in observed['items']:
                template = item['spec']['template']['spec']
                for container in template['containers'] + template.get('initContainers', []):
                    assert container['imagePullPolicy']=='Never',container
                    assert '@sha256:' in container['image'],container
            (target/'offline-cilium-images.json').write_text(objects.stdout)
            pod=command(['/usr/local/bin/k3s','kubectl','-n',data['workload']['namespace'],'get','deployment',NAME,'-o','json'])
            container=json.loads(pod.stdout)['spec']['template']['spec']['containers'][0]
            assert container['imagePullPolicy']=='Never' and '@sha256:' in container['image'],container
            (target/'offline-workload.json').write_text(pod.stdout)
            (target/'offline-firewall-state.json').write_text(command(['nft','-j','list','table','inet','railshot_airgap_test']).stdout)
        case('05-offline-clean',complete_offline)
        with tempfile.TemporaryDirectory(prefix='railshot-invalid-bundles-') as temp:
            def variant(label, mutate, expected):
                bad=Path(temp)/label
                # Hardlink large immutable payloads, but unlink before editing ANY linked file.
                shutil.copytree(args.bundle,bad,copy_function=os.link)
                def write(relative,content):
                    file=bad/relative; file.unlink(); file.write_text(content)
                mutate(bad,write)
                request=offline(); request['runtime']['bundle_path']=str(bad)
                request['runtime'].pop('bundle_sha256')
                runtime(label,'deploy',request,success=False,code=expected)
            def corrupt(root,write):
                path=manifest['files']['installer']['path']; write(path,(root/path).read_text()+'\nCORRUPT\n')
            case('06-corrupt-bundle',lambda:variant('06-corrupt-bundle',corrupt,'BUNDLE_CHECKSUM_MISMATCH'))
            case('07-wrong-checksum',lambda:variant('07-wrong-checksum',lambda root,write:write('manifest.sha256','0'*64+'\n'),'BUNDLE_CHECKSUM_MISMATCH'))
            def remove_image(root,write):
                content=copy.deepcopy(manifest)
                content['images']=[i for i in content['images'] if i['reference']!=canonical(data['workload']['image'])]
                text=json.dumps(content,indent=2)+'\n'
                write('bundle-manifest.json',text); write('manifest.sha256',hashlib.sha256(text.encode()).hexdigest()+'\n')
            case('08-missing-image',lambda:variant('08-missing-image',remove_image,'BUNDLE_IMAGE_MISSING'))
        def repeat():
            before=uid(); r=runtime('09-offline-repeat','deploy',offline())
            assert not r['preload']['imported_archives'],r['preload']
            assert before==uid(),'Repeated offline deployment replaced Pod'
        case('09-offline-repeat',repeat)
        def redeploy():
            runtime('10-workload-cleanup','cleanup',offline())
            r=runtime('10-offline-redeploy','deploy',offline()); assert_mode(r,'airgap')
        case('10-offline-cleanup-redeploy',redeploy)
        def update():
            request=offline();request['workload']['image']='nginx:1.28.1-alpine'
            runtime('11-offline-update','deploy',request)
        case('11-offline-update',update)
        p=command(['nft','delete','table','inet','railshot_airgap_test']); assert p.returncode==0,p.stderr
        blocked=False
        case('12-network-restored',lambda:assert_mode(runtime('12-network-restored','deploy',online()),'online'))
        def cloudflare():
            request=online(); request['exposure']['type']='cloudflare-tunnel'
            request['runtime']['endpoint_overrides']={'cloudflare_tunnel':'https://127.0.0.1:9/'}
            r=runtime('13-cloudflare-unavailable','deploy',request)
            assert r['exposure_status']['status']=='degraded' and r['network_capabilities']['cloudflare_tunnel'] is False
            assert r['endpoint_scope']=='node-local' and r['workload_status']=='ready'
        case('13-cloudflare-unavailable',cloudflare)
        def timeouts():
            stop=threading.Event()
            class Handler(socketserver.BaseRequestHandler):
                def handle(self): stop.wait(5)
            class Server(socketserver.ThreadingTCPServer):
                daemon_threads=True
            with Server(('127.0.0.1',0),Handler) as server:
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                request=online(False);request['runtime']['mode']='auto';request['runtime']['preflight_timeout_seconds']=2
                request['runtime']['endpoint_overrides']={k:f'https://127.0.0.1:{server.server_address[1]}/' for k in ('k3s_source','github','cilium_cli_source','cilium_chart','quay','registry_k8s','docker_hub','ghcr')}
                try:
                    started=time.monotonic();r=runtime('14-preflight-timeout','deploy',request,success=False,code='BUNDLE_REQUIRED')
                    elapsed=time.monotonic()-started
                    assert elapsed<8,(elapsed,r)
                    (target/'timeout-measurement.json').write_text(json.dumps({'elapsed_seconds':elapsed,'limit_seconds':8,'network_seconds':r['network_details']['duration_seconds']})+'\n')
                finally:
                    stop.set();server.shutdown()
        case('14-preflight-timeout',timeouts)
        def architecture_mismatch():
            with tempfile.TemporaryDirectory(prefix='railshot-wrong-arch-') as temp:
                bad=Path(temp)/'bundle'; shutil.copytree(args.bundle,bad,copy_function=os.link)
                content=copy.deepcopy(manifest)
                content['platform']='linux/amd64' if content['platform']=='linux/arm64' else 'linux/arm64'
                text=json.dumps(content,indent=2)+'\n'
                for name,value in [('bundle-manifest.json',text),('manifest.sha256',hashlib.sha256(text.encode()).hexdigest()+'\n')]:
                    path=bad/name;path.unlink();path.write_text(value)
                request=offline();request['runtime']['bundle_path']=str(bad);request['runtime'].pop('bundle_sha256')
                runtime('15-architecture-mismatch','deploy',request,success=False,code='BUNDLE_PLATFORM_MISMATCH')
        case('15-architecture-mismatch',architecture_mismatch)
        def no_duplicate_import():
            before=command(['/usr/local/bin/k3s','ctr','-n','k8s.io','images','list','-q'])
            assert before.returncode==0,before.stderr
            r=runtime('16-no-duplicate-import','deploy',offline())
            assert r['preload']['imported_archives']==[] and r['preload']['digest_verified'],r['preload']
            after=command(['/usr/local/bin/k3s','ctr','-n','k8s.io','images','list','-q'])
            assert after.returncode==0 and sorted(before.stdout.splitlines())==sorted(after.stdout.splitlines()),after.stderr
        case('16-no-duplicate-import',no_duplicate_import)
    except Exception as exc:
        errors.append({'stage':current_case,'message':str(exc)[:4000]})
    finally:
        if blocked:
            p=command(['nft','delete','table','inet','railshot_airgap_test'])
            if p.returncode: errors.append({'stage':'firewall-restore','message':p.stderr})
        marker=Path('/etc/rancher/k3s/config.yaml')
        if marker.exists() and '# Managed by Railshot deployment runtime.' in marker.read_text():
            try: runtime('final-cleanup','cleanup',data,('--all','--disposable-node'))
            except Exception as exc: errors.append({'stage':'final-cleanup','message':str(exc)[:2000]})
    done={r['test'] for r in rows}
    expected=['01-online-clean','02-online-with-bundle','03-registry-fallback','04-no-bundle','05-offline-clean','06-corrupt-bundle','07-wrong-checksum','08-missing-image','09-offline-repeat','10-offline-cleanup-redeploy','11-offline-update','12-network-restored','13-cloudflare-unavailable','14-preflight-timeout','15-architecture-mismatch','16-no-duplicate-import']
    rows.extend({'test':name,'status':'SKIP','reason':'prior scenario failed'} for name in expected if name not in done)
    result={'status':'passed' if not errors and len(done)==16 else 'failed','results':rows,'errors':errors,
            'counts':{s:sum(r['status']==s for r in rows) for s in ('PASS','FAIL','SKIP')},
            'scope':'Disposable Linux VM only; nft restored; no host/CSP firewall changes',
            'not_verified':['amd64','real AWS/GCP/OpenStack networks','authenticated WireGuard','QUIC/Cloudflare tunnel provisioning','private registry credentials']}
    (target/'summary.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
    return 0 if result['status']=='passed' else 1


if __name__=='__main__':sys.exit(main())
