"""Pure boundary tests. Provider equivalence is not live CSP validation."""
from dataclasses import asdict
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(SCRIPTS))
from input_adapter import InputError, parse_input
from render import render
from runtime import load, main
from models import DeploymentResult


def fixture(provider='aws'):
    return json.loads((SCRIPTS/'tests/fixtures'/f'{provider}.json').read_text())


class ContractTests(unittest.TestCase):
    def test_sample_supports_observed_nginx_digest_without_provider_branching(self):
        data = fixture()
        data['workload']['image'] = 'docker.io/library/nginx@sha256:' + 'a' * 64
        specs = []
        for provider in ('aws', 'gcp', 'openstack'):
            data['provider'] = provider
            specs.append(parse_input(data).spec)
        self.assertEqual(specs[0], specs[1])
        self.assertEqual(specs[1], specs[2])
        self.assertEqual(render(specs[0])['items'][1]['spec']['template']['spec']['containers'][0]['image'], data['workload']['image'])

    def test_sample_digest_does_not_allow_unrelated_image_or_probe(self):
        for image, port, path in [('other/nginx@sha256:'+'a'*64, 80, '/'),
                                 ('nginx@sha256:bad', 80, '/'),
                                 ('nginx@sha256:'+'a'*64, 8080, '/'),
                                 ('nginx@sha256:'+'a'*64, 80, '/health')]:
            with self.subTest(image=image, port=port, path=path):
                data = fixture()
                data['workload'].update(image=image, container_port=port, health_path=path)
                with self.assertRaises(InputError):
                    parse_input(data)

    def test_provider_normalization_has_identical_core_spec_and_manifests(self):
        requests=[parse_input(fixture(provider)) for provider in ('aws','gcp','openstack')]
        self.assertEqual(len({repr(r.spec) for r in requests}),1)
        self.assertEqual(len({json.dumps(render(r.spec),sort_keys=True) for r in requests}),1)
        self.assertEqual([r.context.provider for r in requests],['aws','gcp','openstack'])
        self.assertNotIn('provider',asdict(requests[0].spec))
        self.assertNotIn('ssh_user',asdict(requests[0].spec))

    def test_generic_image_preserves_custom_port_path_replicas(self):
        data=fixture(); data['workload']={'image':'ghcr.io/railshot/demo@sha256:'+'a'*64,'namespace':'demo','replicas':2,'container_port':8080,'health_path':'/healthz'}
        objects=render(parse_input(data).spec)['items']
        self.assertEqual(len(objects),3)
        deployment=objects[1]; container=deployment['spec']['template']['spec']['containers'][0]
        self.assertEqual(deployment['spec']['replicas'],2)
        self.assertEqual(container['ports'][0]['containerPort'],8080)
        self.assertEqual(container['readinessProbe']['httpGet']['path'],'/healthz')
        self.assertNotIn('volumeMounts',container)
        self.assertEqual(objects[2]['spec']['ports'][0]['targetPort'],'http')

    def test_invalid_inputs_rejected_before_engine(self):
        changes=[('provider',[]),('provider','proxmox'),('environment_id','../bad'),('unknown',True)]
        for key,value in changes:
            with self.subTest(key=key,value=value):
                data=fixture(); data[key]=value
                with self.assertRaises(InputError): parse_input(data)
        for key,value in [('replicas',True),('replicas',0),('image','nginx'),('image','nginx:bad;touch /tmp/pwn'),
                          ('namespace','kube-system'),('health_path','/healthz?token=bad'),('container_port',70000)]:
            with self.subTest(key=key,value=value):
                data=fixture(); data['workload'][key]=value
                with self.assertRaises(InputError): parse_input(data)
        for url in ('http://u:p@example.com/','file:///tmp/a','http://example.com/?token=x','http://example.com:99999/'):
            data=fixture(); data['exposure']['verification_url']=url
            with self.assertRaises(InputError): parse_input(data)

    def test_malformed_duplicate_and_nonfinite_json(self):
        for text in ('{','{"provider":"aws","provider":"gcp"}','{"replicas":NaN}'):
            with self.assertRaises((InputError,json.JSONDecodeError)): load(text)

    def test_cli_invalid_input_and_flags_always_return_one_json(self):
        for args,text in [(['deploy'],'{}'),(['deploy'],'{'),(['wat'],'{}'),(['deploy','--all'],json.dumps(fixture()))]:
            result=subprocess.run([sys.executable,str(SCRIPTS/'runtime.py'),*args],input=text,text=True,capture_output=True)
            self.assertEqual(result.returncode,1)
            output=json.loads(result.stdout)
            self.assertEqual(output['error']['code'],'INVALID_INPUT')
            self.assertEqual(output['states'][-1]['state'],'FAILED')

    def test_result_context_is_added_only_at_boundary(self):
        import contextlib,io
        for provider in ('aws','gcp','openstack'):
            with self.subTest(provider=provider), patch('sys.stdin',io.StringIO(json.dumps(fixture(provider)))), \
                 patch('runtime.DeploymentEngine') as factory, contextlib.redirect_stdout(io.StringIO()) as output:
                factory.return_value.execute.return_value=DeploymentResult(status='ready')
                self.assertEqual(main(['deploy']),0)
                payload=json.loads(output.getvalue())
                self.assertEqual(payload['provider'],provider)
                self.assertEqual(factory.call_args.args[0],parse_input(fixture(provider)).spec)

    @unittest.skipUnless(importlib.util.find_spec('jsonschema'),'Optional schema validation requires jsonschema')
    def test_fixtures_and_cli_failure_against_published_schemas(self):
        import jsonschema
        input_schema=json.loads((SCRIPTS/'schemas/input.schema.json').read_text())
        output_schema=json.loads((SCRIPTS/'schemas/output.schema.json').read_text())
        for provider in ('aws','gcp','openstack'):
            jsonschema.Draft202012Validator(input_schema,format_checker=jsonschema.FormatChecker()).validate(fixture(provider))
        result=subprocess.run([sys.executable,str(SCRIPTS/'runtime.py'),'deploy'],input='{}',text=True,capture_output=True)
        jsonschema.Draft202012Validator(output_schema,format_checker=jsonschema.FormatChecker()).validate(json.loads(result.stdout))


if __name__=='__main__': unittest.main()
