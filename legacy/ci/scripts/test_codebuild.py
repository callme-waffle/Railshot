"""Publisher/dispatch failure boundaries. AWS, registry and model calls are mocked."""
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zipfile

import codebuild
import codebuild_release as publisher


class CodeBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root / 'bundle'; self.source.mkdir()
        self.config = {'schema_version': 1, 'project_name': 'railshot-test-release', 'region': 'ap-northeast-2',
                       'account_id': '123456789012', 'platform_ref': 'a' * 40,
                       'artifact_bucket': 'railshot-test-artifacts',
                       'registry_prefix': '123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/railshot-test',
                       'role_arn': 'arn:aws:iam::123456789012:role/railshot-test-release'}
        self.job_id = str(uuid.uuid4()); self.job = self.root / self.job_id; self.job.mkdir()
        image = 'sha256:' + 'b' * 64; ref = 'railshot-gate/demo-web:test'
        spec = b'apiVersion: railshot/v0\napp: demo\nservices:\n- name: web\n  build: {dockerfile: Dockerfile}\n  port: 3000\n  route: /\n'
        verdict = {'ok': True, 'release_eligible': True, 'status': 'PASS', 'source_sha256': 'c' * 64,
                   'layers': [{'layer': x, 'ok': True} for x in codebuild.bundle.LAYERS],
                   'images': {'web': ref}, 'image_ids': {'web': image}}
        for name, body in {'railshot.yaml': spec, 'verdict.json': json.dumps(verdict).encode(), 'images.tar': b'not a live image'}.items():
            (self.source / name).write_bytes(body)
        manifest = {'version': 1, 'trust': codebuild.bundle.TRUST, 'source_sha256': 'c' * 64,
                    'images': {'web': {'local_ref': ref, 'id': image}},
                    'files': {n: codebuild.bundle.file_hash(self.source / n) for n in codebuild.bundle.FILES}}
        (self.source / 'manifest.json').write_text(json.dumps(manifest))
        self.approved = codebuild.bundle.file_hash(self.source / 'manifest.json')
        self.build_id = self.config['project_name'] + ':' + str(uuid.uuid4())

    def test_mismatched_approval_has_zero_cloud_calls(self):
        with patch.object(codebuild, 'aws') as aws, self.assertRaises(codebuild.OperationError):
            codebuild.start(self.config, self.source, self.job, self.job_id, 'd' * 64)
        aws.assert_not_called()

    def test_dispatch_persists_intent_and_binds_only_allowed_overrides(self):
        def aws(config, *args):
            state = json.loads((self.job / 'state.json').read_text())
            if args[:2] == ('codebuild', 'start-build'):
                self.assertEqual(state['phase'], 'dispatch_intent')
                payload = json.loads((self.job / 'start-build.json').read_text())
                self.assertEqual(set(payload), {'projectName','sourceVersion','idempotencyToken','environmentVariablesOverride'})
                self.assertEqual(payload['sourceVersion'], self.config['platform_ref'])
                self.assertEqual({v['name'] for v in payload['environmentVariablesOverride']},
                                 {'RAILSHOT_JOB_ID','RAILSHOT_MANIFEST_SHA256','RAILSHOT_ARCHIVE_SHA256'})
                return {'build': {'id': self.build_id}}
            self.assertEqual(state['phase'], 'upload_intent'); return {}
        with patch.object(codebuild, 'preflight'), patch.object(codebuild, 'aws', side_effect=aws):
            result = codebuild.start(self.config, self.source, self.job, self.job_id, self.approved)
        self.assertEqual(result['outcome'], 'RUNNING')
        self.assertEqual(json.loads((self.job / 'state.json').read_text())['build_id'], self.build_id)
        self.assertEqual((self.job / 'state.json').stat().st_mode & 0o777, 0o600)

    def test_uncertain_dispatch_reconciliation_never_starts_another_build(self):
        def aws(config, *args):
            if args[0] == 'codebuild': raise subprocess.TimeoutExpired('aws', 1)
            return {}
        with patch.object(codebuild, 'preflight'), patch.object(codebuild, 'aws', side_effect=aws), self.assertRaises(subprocess.TimeoutExpired):
            codebuild.start(self.config, self.source, self.job, self.job_id, self.approved)
        with patch.object(codebuild, 'aws') as client, self.assertRaises(codebuild.OperationError) as caught:
            codebuild.reconcile(self.config, self.job)
        client.assert_not_called(); self.assertEqual(caught.exception.outcome, 'UNKNOWN')

    def test_existing_upload_requires_exact_bytes_before_new_dispatch(self):
        def aws(config, *args):
            if args[:2] == ('s3api', 'put-object'): raise subprocess.CalledProcessError(1, 'aws')
            if args[:2] == ('s3api', 'get-object'):
                shutil.copyfile(self.job / 'bundle.zip', args[-1]); return {}
            return {'build': {'id': self.build_id}}
        with patch.object(codebuild, 'preflight'), patch.object(codebuild, 'aws', side_effect=aws):
            result = codebuild.start(self.config, self.source, self.job, self.job_id, self.approved)
        self.assertEqual(result['outcome'], 'RUNNING')

    def test_existing_upload_mismatch_never_dispatches(self):
        def aws(config, *args):
            if args[:2] == ('s3api', 'put-object'): raise subprocess.CalledProcessError(1, 'aws')
            if args[:2] == ('s3api', 'get-object'): Path(args[-1]).write_bytes(b'tampered'); return {}
            self.fail('changed upload must never dispatch')
        with patch.object(codebuild, 'preflight'), patch.object(codebuild, 'aws', side_effect=aws), self.assertRaises(codebuild.OperationError):
            codebuild.start(self.config, self.source, self.job, self.job_id, self.approved)

    def test_provider_success_without_bound_receipt_cannot_be_pass(self):
        state = {'config': self.config, 'phase': 'started', 'build_id': self.build_id, 'job_id': self.job_id,
                 'manifest_sha256': self.approved, 'source_sha256': 'c' * 64}
        codebuild.save(self.job / 'state.json', state)
        def aws(config, *args):
            if args[0] == 'codebuild': return {'builds': [{'id': self.build_id, 'buildStatus': 'SUCCEEDED', 'resolvedSourceVersion': 'b' * 40}]}
            self.fail('wrong source must fail before receipt download')
        with patch.object(codebuild, 'aws', side_effect=aws), self.assertRaises(codebuild.OperationError) as caught:
            codebuild.reconcile(self.config, self.job)
        self.assertEqual(caught.exception.outcome, 'UNKNOWN')

    def test_archive_path_duplicate_and_symlink_are_rejected(self):
        for names in (['../manifest.json','images.tar','railshot.yaml','verdict.json'],
                      ['manifest.json','manifest.json','images.tar','railshot.yaml']):
            archive = self.root / (str(uuid.uuid4()) + '.zip')
            with zipfile.ZipFile(archive, 'w') as output:
                for name in names: output.writestr(name, b'fixture')
            with self.assertRaises(ValueError): publisher.unpack(archive, self.root / str(uuid.uuid4()))
        archive = self.root / 'symlink.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            for name in ('manifest.json','images.tar','railshot.yaml','verdict.json'):
                info = zipfile.ZipInfo(name); info.external_attr = (stat.S_IFLNK | 0o777) << 16
                output.writestr(info, '/private/credential')
        with self.assertRaises(ValueError): publisher.unpack(archive, self.root / 'links')

    def test_historical_bundle_dispatch_and_unpack_preserve_filename_and_hash(self):
        (self.source / 'railshot.yaml').rename(self.source / 'jasmin.yaml')
        manifest = json.loads((self.source / 'manifest.json').read_bytes())
        manifest['files']['jasmin.yaml'] = manifest['files'].pop('railshot.yaml')
        (self.source / 'manifest.json').write_text(json.dumps(manifest))
        approved = codebuild.bundle.file_hash(self.source / 'manifest.json')
        with patch.object(codebuild, 'preflight'), patch.object(codebuild, 'aws', return_value={'build': {'id': self.build_id}}):
            codebuild.start(self.config, self.source, self.job, self.job_id, approved)
        destination = self.root / 'unpacked'
        publisher.unpack(self.job / 'bundle.zip', destination)
        self.assertEqual(codebuild.bundle.verify(destination), manifest)
        self.assertEqual(codebuild.bundle.file_hash(destination / 'manifest.json'), approved)
        self.assertEqual((destination / 'jasmin.yaml').read_bytes(), (self.source / 'jasmin.yaml').read_bytes())

    def test_project_drift_is_blocked_before_upload(self):
        project = {'sourceVersion': self.config['platform_ref'], 'serviceRole': self.config['role_arn'],
                   'timeoutInMinutes':15, 'concurrentBuildLimit':1, 'projectVisibility':'PRIVATE',
                   'source':{'type':'GITHUB','location':'https://github.com/Jasmin-Softbank/Jasmin.git','buildspec':'ci/workflows/codebuild-release.yml'},
                   'environment':{'privilegedMode':False,'image':'aws/codebuild/standard:7.0','computeType':'BUILD_GENERAL1_SMALL',
                     'environmentVariables':[{'type':'PLAINTEXT','name':'RAILSHOT_'+k,'value':v} for k,v in
                       [('SOURCE_REF',self.config['platform_ref']),('ARTIFACT_BUCKET',self.config['artifact_bucket']),('REGISTRY_PREFIX',self.config['registry_prefix'])]]}}
        for key,value in [('autoRetryLimit',1),('webhook',{'url':'unreviewed'}),('projectVisibility','PUBLIC_READ')]:
            with patch.object(codebuild,'aws',side_effect=[{'Account':self.config['account_id']},{'projects':[{**project,key:value}]}]), self.assertRaises(codebuild.OperationError):
                codebuild.preflight(self.config)

    def test_publisher_never_executes_archive_and_keeps_registry_password_off_argv(self):
        archive = self.root / 'input.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            for path in self.source.iterdir(): output.write(path, path.name)
        env = {'RAILSHOT_' + key: value for key,value in {
            'SOURCE_REF': self.config['platform_ref'], 'ARTIFACT_BUCKET': self.config['artifact_bucket'],
            'REGISTRY_PREFIX': self.config['registry_prefix'], 'JOB_ID': self.job_id,
            'MANIFEST_SHA256': self.approved, 'ARCHIVE_SHA256': codebuild.bundle.file_hash(archive)}.items()}
        env.update(CODEBUILD_RESOLVED_SOURCE_VERSION=self.config['platform_ref'], CODEBUILD_BUILD_ID=self.build_id)
        def aws(*args):
            if args[:2] == ('s3api','get-object'): shutil.copyfile(archive, args[-1])
            return subprocess.CompletedProcess([], 0, 'synthetic-password' if args[0]=='ecr' else '{}', '')
        images = {'web': self.config['registry_prefix'] + '-web@sha256:' + 'd' * 64}
        with patch.object(publisher, 'aws', side_effect=aws), patch.object(publisher.bundle, 'publish', return_value=images), \
                patch.object(publisher, 'run_bounded', return_value=subprocess.CompletedProcess([],0,'','')) as login:
            result = publisher.publish(env)
        self.assertEqual(result['deployment_status'], 'NOT_RUN')
        self.assertNotIn('synthetic-password', repr(login.call_args.args))
        self.assertEqual(login.call_args.kwargs['input'], b'synthetic-password\n')
        self.assertNotIn('synthetic-password', json.dumps(result))

    def test_failure_diagnostics_use_private_channel_and_keep_unknown(self):
        archive = self.root / 'failure-input.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            for path in self.source.iterdir(): output.write(path, path.name)
        env = {'RAILSHOT_' + key: value for key,value in {
            'SOURCE_REF': self.config['platform_ref'], 'ARTIFACT_BUCKET': self.config['artifact_bucket'],
            'REGISTRY_PREFIX': self.config['registry_prefix'], 'JOB_ID': self.job_id,
            'MANIFEST_SHA256': self.approved, 'ARCHIVE_SHA256': codebuild.bundle.file_hash(archive)}.items()}
        env.update(CODEBUILD_RESOLVED_SOURCE_VERSION=self.config['platform_ref'], CODEBUILD_BUILD_ID=self.build_id)
        captured = []
        def aws(*args):
            if args[:2] == ('s3api', 'get-object'): shutil.copyfile(archive, args[-1])
            if args[:2] == ('s3api', 'put-object'):
                self.assertEqual(args[args.index('--key')+1], 'receipts/' + self.job_id + '.failure.json')
                captured.append(json.loads(Path(args[args.index('--body')+1]).read_bytes()))
            return subprocess.CompletedProcess([], 0, 'synthetic-password' if args[0]=='ecr' else '{}', '')
        def failure(*args, **kwargs):
            journal = kwargs['journal_dir']; journal.mkdir(mode=0o700)
            publisher.durable_write(journal / 'native-failure-test.json', b'{"command":"skopeo.copy","exit_code":17}')
            raise codebuild.OperationError('PUBLISH_OUTCOME_UNKNOWN', component='publish', phase='image', outcome='UNKNOWN')
        with patch.object(publisher, 'aws', side_effect=aws), patch.object(publisher.bundle, 'publish', side_effect=failure), \
                patch.object(publisher, 'run_bounded'), self.assertRaises(codebuild.OperationError) as error:
            publisher.publish(env)
        self.assertEqual(error.exception.outcome, 'UNKNOWN')
        self.assertEqual(captured[0]['diagnostics'][0]['exit_code'], 17)
        self.assertNotIn('synthetic-password', json.dumps(captured))
        def unavailable_receipt(*args):
            if args[:2] == ('s3api', 'put-object'):
                raise subprocess.CalledProcessError(1, 'aws')
            return aws(*args)
        with patch.object(publisher, 'aws', side_effect=unavailable_receipt), patch.object(publisher.bundle, 'publish', side_effect=failure), \
                patch.object(publisher, 'run_bounded'), self.assertRaises(codebuild.OperationError) as error:
            publisher.publish(env)
        self.assertEqual(error.exception.code, 'OBSERVATION_WRITE_FAILED')
        self.assertEqual(error.exception.outcome, 'UNKNOWN')


if __name__ == '__main__': unittest.main()
