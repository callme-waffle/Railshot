#!/usr/bin/env python3
"""Administrator CLI for a registered trusted publisher, not a product Allow service.

start persists intent before S3/CodeBuild effects. reconcile only reads the recorded
build; ambiguous dispatch is never submitted again, including after token expiry.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import subprocess
import uuid
import zipfile

PLATFORM = Path(__file__).resolve().parent
sys.path[:0] = [str(PLATFORM), str(PLATFORM / 'gate')]
import bundle
from observability import OperationError
from process import run_bounded
from runner.runtime_boundary import private_directory
from storage import durable_write


def fail(phase, *, unknown=False, cause=None):
    return OperationError('STEP_OUTPUT_INVALID' if unknown else 'CD_CONFIG_INVALID',
                          component='codebuild.dispatch', phase=phase, outcome='UNKNOWN' if unknown else 'BLOCKED',
                          retry_policy='after_reconcile' if unknown else 'after_configuration',
                          side_effect='unknown' if unknown else 'none', cause=cause)


def save(path, value):
    durable_write(path, json.dumps(value, sort_keys=True).encode())


def aws(config, *args):
    result = run_bounded(['aws', *args, '--region', config['region'], '--output', 'json', '--no-cli-pager'],
                         timeout=300, check=True, env={**os.environ, 'AWS_MAX_ATTEMPTS': '1'})
    return json.loads(result.stdout or '{}')


def checked_config(value):
    patterns = {'project_name': r'railshot-[a-z0-9-]{3,30}', 'region': r'[a-z]{2}-[a-z]+-[0-9]',
                'account_id': r'[0-9]{12}', 'platform_ref': r'[a-f0-9]{40}',
                'artifact_bucket': r'[a-z0-9][a-z0-9.-]{2,62}',
                'registry_prefix': r'[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/railshot-[a-z0-9-]+',
                'role_arn': r'arn:aws:iam::[0-9]{12}:role/railshot-[a-z0-9-]+'}
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or set(value) - (patterns.keys() | {'schema_version', 'scope'})
            or any(not isinstance(value.get(k), str) or not re.fullmatch(p, value[k]) for k,p in patterns.items())
            or not value['registry_prefix'].startswith(f"{value['account_id']}.dkr.ecr.{value['region']}.")
            or not value['role_arn'].startswith(f"arn:aws:iam::{value['account_id']}:role/")):
        raise fail('configure')
    return value


def preflight(config):
    if aws(config, 'sts', 'get-caller-identity')['Account'] != config['account_id']:
        raise fail('account')
    projects = aws(config, 'codebuild', 'batch-get-projects', '--names', config['project_name']).get('projects', [])
    if len(projects) != 1: raise fail('project')
    p = projects[0]; env = p['environment']; source = p['source']
    values = {v['name']: v['value'] for v in env['environmentVariables'] if v['type'] == 'PLAINTEXT'}
    expected = {'RAILSHOT_SOURCE_REF': config['platform_ref'], 'RAILSHOT_ARTIFACT_BUCKET': config['artifact_bucket'],
                'RAILSHOT_REGISTRY_PREFIX': config['registry_prefix']}
    if (p['sourceVersion'] != config['platform_ref'] or p['serviceRole'] != config['role_arn']
            or p['timeoutInMinutes'] != 15 or p['concurrentBuildLimit'] != 1
            or p.get('autoRetryLimit', 0) != 0 or p.get('projectVisibility') != 'PRIVATE'
            or p.get('webhook') or p.get('secondarySources') or p.get('vpcConfig', {}).get('vpcId')
            or env['privilegedMode'] or env['image'] != 'aws/codebuild/standard:7.0'
            or env['computeType'] != 'BUILD_GENERAL1_SMALL' or values != expected
            or len(env['environmentVariables']) != 3 or source['type'] != 'GITHUB'
            or source['location'] != 'https://github.com/Jasmin-Softbank/Jasmin.git'
            or source['buildspec'] != 'ci/workflows/codebuild-release.yml'):
        raise fail('project.binding')


def start(config, source, job, job_id, approved):
    manifest = bundle.verify(source)
    manifest_sha = bundle.file_hash(source / 'manifest.json')
    if approved != manifest_sha or set(manifest['images']) != {'web'}: raise fail('approve.manifest')
    if source.resolve() == job.resolve() or source.resolve() in job.resolve().parents: raise fail('root')
    preflight(config)
    archive = job / 'bundle.zip'
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_STORED) as output:
        os.chmod(archive, 0o600)
        for name in sorted(set(manifest['files']) | {'manifest.json'}): output.write(source / name, name)
    if bundle.verify(source) != manifest: raise fail('source.binding')
    archive_sha = bundle.file_hash(archive)
    state = {'schema_version': 1, 'job_id': job_id, 'config': config, 'manifest_sha256': manifest_sha,
             'archive_sha256': archive_sha, 'source_sha256': manifest['source_sha256'], 'phase': 'upload_intent'}
    save(job / 'state.json', state)
    key = 'bundles/' + archive_sha + '.zip'
    try:
        aws(config, 's3api', 'put-object', '--bucket', config['artifact_bucket'], '--key', key,
            '--body', str(archive), '--if-none-match', '*')
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # Reconcile only this immutable upload; never overwrite it or re-submit a build.
        existing = job / 'existing-bundle.zip'
        aws(config, 's3api', 'get-object', '--bucket', config['artifact_bucket'], '--key', key, str(existing))
        existing.chmod(0o600)
        if bundle.file_hash(existing) != archive_sha: raise fail('upload.binding', unknown=True)
    payload = {'projectName': config['project_name'], 'sourceVersion': config['platform_ref'],
               'idempotencyToken': job_id, 'environmentVariablesOverride': [
                   {'name': 'RAILSHOT_' + key, 'value': value, 'type': 'PLAINTEXT'} for key, value in
                   [('JOB_ID', job_id), ('MANIFEST_SHA256', manifest_sha), ('ARCHIVE_SHA256', archive_sha)]]}
    save(job / 'start-build.json', payload)
    state['phase'] = 'dispatch_intent'; save(job / 'state.json', state)
    result = aws(config, 'codebuild', 'start-build', '--cli-input-json', 'file://' + str(job / 'start-build.json'))
    build_id = result['build']['id']
    if not re.fullmatch(re.escape(config['project_name']) + r':[a-f0-9-]{36}', build_id): raise fail('build.id', unknown=True)
    state.update(phase='started', build_id=build_id); save(job / 'state.json', state)
    return {'outcome': 'RUNNING', 'build_id': build_id, 'job_id': job_id}


def reconcile(config, job):
    state = json.loads((job / 'state.json').read_bytes())
    if state['config'] != config: raise fail('reconcile.binding')
    if state.get('phase') == 'completed': return json.loads((job / 'result.json').read_bytes())
    if not state.get('build_id'): raise fail('dispatch.uncertain', unknown=True)
    builds = aws(config, 'codebuild', 'batch-get-builds', '--ids', state['build_id']).get('builds', [])
    if len(builds) != 1 or builds[0]['id'] != state['build_id']: raise fail('build.receipt', unknown=True)
    build = builds[0]; status = build['buildStatus']
    if status == 'IN_PROGRESS': return {'outcome': 'RUNNING', 'build_id': state['build_id'], 'phase': build.get('currentPhase')}
    if status != 'SUCCEEDED' or build.get('resolvedSourceVersion') != config['platform_ref']:
        raise fail('publish.uncertain', unknown=True)
    receipt_path = job / 'remote-receipt.json'
    aws(config, 's3api', 'get-object', '--bucket', config['artifact_bucket'],
        '--key', 'receipts/' + state['job_id'] + '.json', str(receipt_path))
    receipt_path.chmod(0o600)
    result = json.loads(receipt_path.read_bytes())
    expected = {'outcome': 'PASS', 'operation': 'publish_only', 'job_id': state['job_id'], 'build_id': state['build_id'],
                'platform_ref': config['platform_ref'], 'manifest_sha256': state['manifest_sha256'],
                'source_sha256': state['source_sha256'], 'deployment_status': 'NOT_RUN'}
    if (any(result.get(k) != v for k,v in expected.items()) or set(result.get('images', {})) != {'web'}
            or not re.fullmatch(re.escape(config['registry_prefix'] + '-web@sha256:') + r'[a-f0-9]{64}', result['images']['web'])):
        raise fail('publish.receipt', unknown=True)
    save(job / 'result.json', result); state['phase'] = 'completed'; save(job / 'state.json', state)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('start', 'reconcile'))
    parser.add_argument('--config', type=Path, required=True); parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--job-id', required=True); parser.add_argument('--bundle', type=Path)
    parser.add_argument('--approve-manifest-sha256')
    args = parser.parse_args(); job = None
    try:
        config = checked_config(json.loads(args.config.read_bytes()))
        if str(uuid.UUID(args.job_id)) != args.job_id: raise fail('job')
        root = private_directory(args.root); job = root / args.job_id
        if args.command == 'start':
            if job.exists() or not args.bundle: raise fail('duplicate.dispatch', unknown=job.exists())
            job.mkdir(mode=0o700)
        if not job.is_dir() or job.is_symlink(): raise fail('job')
        with (job / '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = (start(config, args.bundle.resolve(), job, args.job_id, args.approve_manifest_sha256)
                      if args.command == 'start' else reconcile(config, job))
        print(json.dumps(result)); return 0
    except Exception as exc:
        failure = exc if isinstance(exc, OperationError) else fail('dispatch', unknown=bool(job and (job / 'state.json').exists()), cause=exc)
        print(json.dumps({'outcome': failure.outcome, 'error': failure.as_dict()})); return 1


if __name__ == '__main__': raise SystemExit(main())
