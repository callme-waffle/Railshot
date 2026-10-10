"""Pure contracts and corrupted-archive checks; these do not claim Linux installation."""
from dataclasses import asdict
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch, MagicMock

SCRIPTS=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SCRIPTS));sys.path.insert(0,str(SCRIPTS.parent))
from input_adapter import InputError,parse_input
from engine import DeploymentEngine,DeploymentError
from network_preflight import check,endpoints,registry
from runtime import main
from models import DeploymentResult
from airgap.scripts import bundle
from test_contract import fixture
from test_engine import Runner


def extended():
    data=fixture();data['schema_version']='0.2';return data


class NetworkModeTests(unittest.TestCase):
    def test_short_image_is_docker_hub_not_tag_as_hostname(self):
        self.assertEqual(registry('nginx:1.28.0-alpine'),'docker.io')
        self.assertEqual(bundle.canonical('nginx:1.28.0-alpine'),'docker.io/library/nginx:1.28.0-alpine')
        self.assertEqual(bundle.canonical('curlimages/curl:8.12.1'),'docker.io/curlimages/curl:8.12.1')
        urls,required=endpoints(parse_input(fixture()).spec,'arm64')
        self.assertIn('docker_hub',required);self.assertNotIn('ghcr',required)
        self.assertNotIn('workload_registry',required)

    def test_custom_registry_required_but_optional_cloudflare_not_required(self):
        data=extended();data['workload'].update(image='ghcr.io/team/app:1',sample_content=False)
        data['exposure']['type']='cloudflare-tunnel'
        urls,required=endpoints(parse_input(data).spec,'arm64')
        self.assertIn('ghcr',required);self.assertNotIn('cloudflare_tunnel',required)

    def test_offline_probe_does_not_spawn_external_commands(self):
        with patch('network_preflight.subprocess.run') as run:
            caps,details=check(parse_input(fixture()).spec,'arm64',True)
        run.assert_not_called();self.assertTrue(details['external_checks_skipped']);self.assertTrue(all(v is None for v in caps.values()))
        self.assertNotIn('wireguard_udp_51820',caps)

    def test_registry_challenge_is_reachable_but_403_is_not(self):
        from network_preflight import probe
        for status,want in ((401,True),(403,False),(500,False)):
            with patch('network_preflight.subprocess.run',return_value=MagicMock(returncode=0,stdout=f'{status} 192.0.2.1',stderr='')):
                self.assertEqual(probe('quay','https://quay.io/v2/',1)['available'],want)

    def test_mode_selection_only_auto_falls_back_and_never_requires_optional_caps(self):
        for mode,blocked,path,want in (('auto',[],None,'online'),('auto',['ghcr'],'/unit/bundle','airgap'),('offline',[],'/unit/bundle','airgap')):
            data=extended();data['runtime'].update(mode=mode)
            if path:data['runtime']['bundle_path']=path
            engine=DeploymentEngine(parse_input(data).spec,network_checker=lambda *args:({'cloudflare_tunnel':False},{'required_unavailable':blocked}))
            verified={'root':'/unit/bundle','manifest_sha256':'a'*64,'manifest':{'bundle_version':'unit'}}
            with patch('engine.bundles.verify',return_value=verified):engine.select_mode()
            self.assertEqual(engine.result.deployment_mode,want)
            self.assertEqual(engine.result.fallback_used,mode=='auto' and bool(blocked))
        data=extended();data['runtime']['mode']='online'
        engine=DeploymentEngine(parse_input(data).spec,network_checker=lambda *args:({}, {'required_unavailable':['quay']}))
        with self.assertRaises(DeploymentError) as exc:engine.select_mode()
        self.assertEqual(exc.exception.code,'NETWORK_UNAVAILABLE')

    def test_unapproved_version_not_selected(self):
        data=extended();data['runtime']['k3s_version']='v1.99.0+k3s1'
        with self.assertRaises(bundle.BundleError) as exc:bundle.approved(parse_input(data).spec)
        self.assertEqual(exc.exception.code,'VERSION_NOT_APPROVED')

    def test_extended_options_require_version_02_and_valid_urls(self):
        for field,value in (('mode','wrong'),('bundle_path','../relative'),('bundle_sha256','bad'),('preflight_timeout_seconds',True),('endpoint_overrides',{'quay':'http://quay.io/'}),('endpoint_overrides',{'quay':'https://[bad'})):
            data=extended();data['runtime'][field]=value
            with self.subTest(field=field),self.assertRaises(InputError):parse_input(data)
        data=fixture();data['runtime']['mode']='offline'
        with self.assertRaises(InputError):parse_input(data)

    def test_legacy_output_shape_and_explicit_cli_negotiation(self):
        for args,version in ((['deploy'],'0.1'),(['deploy','--mode','auto'],'0.2')):
            stream=io.StringIO()
            with patch('sys.stdin',io.StringIO(json.dumps(fixture()))),patch('runtime.DeploymentEngine') as engine,contextlib.redirect_stdout(stream):
                engine.return_value.execute.return_value=DeploymentResult(status='ready')
                self.assertEqual(main(args),0)
            result=json.loads(stream.getvalue());self.assertEqual(result['schema_version'],version)
            self.assertEqual('network_capabilities' in result,version=='0.2')
            if importlib.util.find_spec('jsonschema'):
                import jsonschema
                name='output.schema.json' if version=='0.1' else 'output-v0.2.schema.json'
                jsonschema.validate(result,json.loads((SCRIPTS/'schemas'/name).read_text()))

    def test_late_fallback_does_not_hide_readiness_connection_errors(self):
        for stage,message,fallback in (('CILIUM_INSTALLING','curl: download connection refused',True),
                                        ('WORKLOAD_DEPLOYING','Readiness probe connection refused',False)):
            data=extended();data['runtime']['bundle_path']='/unit/bundle'
            spec=parse_input(data).spec
            engine=DeploymentEngine(spec,Runner(spec),network_checker=lambda *args:({}, {'required_unavailable':[]}))
            calls=[]
            def reconcile(action):
                calls.append(action)
                if len(calls)==1:
                    engine.stage=stage
                    raise DeploymentError('COMMAND_FAILED',message,1)
                engine.result.status='ready'
            verified={'root':'/unit/bundle','manifest_sha256':'a'*64,'manifest':{'bundle_version':'unit'}}
            with patch('engine.open',return_value=MagicMock()),patch('engine.fcntl.flock'), \
                 patch('engine.bundles.verify',return_value=verified),patch.object(engine,'reconcile',side_effect=reconcile):
                result=engine.execute()
            self.assertEqual(result.fallback_used,fallback)
            self.assertEqual(len(calls),2 if fallback else 1)
            self.assertEqual(result.status,'ready' if fallback else 'failed')

    def test_latest_detection_never_selects_or_writes_runtime_versions(self):
        from airgap.scripts.check_updates import detect,version_key
        before=(SCRIPTS.parent/'airgap/versions.json').read_bytes()
        with patch('airgap.scripts.check_updates.subprocess.run',return_value=MagicMock(returncode=0,stdout='{"tag_name":"v99.0.0+k3s1"}')):
            _,result=detect('k3s','k3s-io/k3s','v1.34.11+k3s1')
        self.assertTrue(result['update_available']);self.assertEqual(result['approved'],'v1.34.11+k3s1')
        self.assertEqual((SCRIPTS.parent/'airgap/versions.json').read_bytes(),before)
        self.assertLess(version_key('v1.34.11+k3s1'),version_key('v1.35.0+k3s1'))
        self.assertIsNone(version_key('v1.35.0-rc.1'))

    def test_local_containerd_timeout_returns_machine_readable_failure(self):
        data=extended();data['runtime']['bundle_path']='/unit/bundle'
        spec=parse_input(data).spec
        engine=DeploymentEngine(spec,Runner(spec),network_checker=lambda *args:({}, {'required_unavailable':[]}))
        verified={'root':'/unit/bundle','manifest_sha256':'a'*64,'manifest':{'bundle_version':'unit'}}
        with patch('engine.open',return_value=MagicMock()),patch('engine.fcntl.flock'), \
             patch('engine.bundles.verify',return_value=verified), \
             patch('airgap.scripts.bundle.subprocess.run',side_effect=subprocess.TimeoutExpired('ctr',300)):
            result=engine.execute()
        self.assertEqual(result.status,'failed')
        self.assertEqual(result.error['code'],'IMAGE_PRELOAD_TIMEOUT')
        self.assertEqual(result.error['stage'],'AIRGAP_PRELOADING')


class BundleIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        config_blob=b'{"architecture":"arm64","os":"linux"}'
        config_digest=hashlib.sha256(config_blob).hexdigest()
        manifest_blob=json.dumps({'schemaVersion':2,'mediaType':'application/vnd.oci.image.manifest.v1+json','config':{'mediaType':'application/vnd.oci.image.config.v1+json','digest':'sha256:'+config_digest,'size':len(config_blob)},'layers':[]}).encode()
        digest='sha256:'+hashlib.sha256(manifest_blob).hexdigest()
        self.policy=copy.deepcopy(bundle.policy())
        self.policy['cilium_images']={role:f'quay.io/unit/{role}@{digest}' for role in ('agent','operator','envoy')}
        refs=list(self.policy['cilium_images'].values())+[bundle.canonical(self.policy['health_image']),'docker.io/library/nginx:1.28.0-alpine']
        def add(tar,name,data):
            member=tarfile.TarInfo(name);member.size=len(data);tar.addfile(member,io.BytesIO(data))
        with tarfile.open(self.root/'images.tar','w') as tar:
            add(tar,'index.json',json.dumps({'schemaVersion':2,'manifests':[{'digest':digest,'annotations':{'io.containerd.image.name':ref}} for ref in refs]}).encode())
            add(tar,'blobs/sha256/'+digest.split(':')[1],manifest_blob)
            add(tar,'blobs/sha256/'+config_digest,config_blob)
        with tarfile.open(self.root/'chart.tgz','w:gz') as tar:add(tar,'cilium/Chart.yaml',b'version: 1.20.2\n')
        (self.root/'artifact').write_bytes(b'unit-only; not an executable Linux bundle')
        files={key:{'path':'artifact','sha256':bundle.sha256(self.root/'artifact'),'size':(self.root/'artifact').stat().st_size} for key in ('k3s','installer','k3s_images','cilium_cli')}
        for key,name in (('cilium_chart','chart.tgz'),('image','images.tar')):
            files[key]={'path':name,'sha256':bundle.sha256(self.root/name),'size':(self.root/name).stat().st_size}
        images=[{'reference':ref,'digest':digest,'archive':'image','role':'cilium_'+role} for role,ref in self.policy['cilium_images'].items()]
        images += [{'reference':ref,'digest':digest,'archive':'image','role':'health' if 'curl' in ref else 'workload'} for ref in refs[3:]]
        images += [{'reference':f'docker.io/rancher/{name}:1','digest':digest,'archive':'k3s_images','role':'k3s_system'} for name in ('mirrored-coredns-coredns','mirrored-pause')]
        self.data={'schema_version':'1','bundle_version':'unit','platform':'linux/'+bundle.architecture(),'runtime':self.policy['runtime'],'files':files,'images':images}
        self.patch=patch.object(bundle,'policy',return_value=self.policy);self.patch.start();self.write()
    def tearDown(self):self.patch.stop();self.temp.cleanup()
    def write(self):
        (self.root/'bundle-manifest.json').write_text(json.dumps(self.data))
        (self.root/'manifest.sha256').write_text(bundle.sha256(self.root/'bundle-manifest.json'))
    def test_valid_checksums_and_image_index(self):
        b=bundle.verify(self.root,parse_input(fixture()).spec)
        self.assertEqual(b['manifest']['bundle_version'],'unit')
    def test_corrupt_file_and_wrong_pinned_manifest_rejected(self):
        with self.assertRaises(bundle.BundleError) as exc:bundle.verify(self.root,expected_sha256='0'*64)
        self.assertEqual(exc.exception.code,'BUNDLE_CHECKSUM_MISMATCH')
        (self.root/'artifact').write_bytes(b'corrupt')
        with self.assertRaises(bundle.BundleError) as exc:bundle.verify(self.root)
        self.assertEqual(exc.exception.code,'BUNDLE_CHECKSUM_MISMATCH')
    def test_missing_workload_image_is_not_hidden_by_cached_image(self):
        self.data['images']=[i for i in self.data['images'] if i['reference']!='docker.io/library/nginx:1.28.0-alpine'];self.write()
        with self.assertRaises(bundle.BundleError) as exc:bundle.verify(self.root,parse_input(fixture()).spec)
        self.assertEqual(exc.exception.code,'BUNDLE_IMAGE_MISSING')
    def test_sample_image_required_even_for_a_different_workload(self):
        self.data['images']=[i for i in self.data['images'] if i['reference']!=bundle.canonical(self.policy['sample_image'])];self.write()
        with self.assertRaises(bundle.BundleError) as exc:bundle.verify(self.root)
        self.assertEqual(exc.exception.code,'BUNDLE_IMAGE_MISSING')
    def test_path_escape_and_symlink_rejected(self):
        self.data['files']['installer']['path']='../outside';self.write()
        with self.assertRaises(bundle.BundleError) as exc:bundle.verify(self.root)
        self.assertEqual(exc.exception.code,'BUNDLE_PATH_UNSAFE')
    def test_platform_mismatch(self):
        self.data['platform']='linux/not-the-node';self.write()
        with self.assertRaises(bundle.BundleError) as exc:bundle.verify(self.root)
        self.assertEqual(exc.exception.code,'BUNDLE_PLATFORM_MISMATCH')

    def test_bundle_inside_managed_cleanup_directory_is_rejected(self):
        with self.assertRaises(bundle.BundleError) as exc:bundle.verify('/var/lib/rancher/k3s/bundle')
        self.assertEqual(exc.exception.code,'BUNDLE_PATH_UNSAFE')

    def test_oci_blob_mismatch_rejected_even_if_outer_archive_checksum_is_rewritten(self):
        archive=self.root/'images.tar'
        with tarfile.open(archive) as tar:
            items=[(item.name,tar.extractfile(item).read()) for item in tar.getmembers()]
        with tarfile.open(archive,'w') as tar:
            for name,data in items:
                if name.startswith('blobs/sha256/'):data+=b'corrupted'
                member=tarfile.TarInfo(name);member.size=len(data);tar.addfile(member,io.BytesIO(data))
        self.data['files']['image'].update(sha256=bundle.sha256(archive),size=archive.stat().st_size);self.write()
        with self.assertRaises(bundle.BundleError) as exc:bundle.verify(self.root)
        self.assertEqual(exc.exception.code,'BUNDLE_DIGEST_MISMATCH')


if __name__=='__main__':unittest.main()
