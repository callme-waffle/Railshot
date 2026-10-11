#!/usr/bin/env python3
"""Trusted CodeBuild publisher. Reads an approved bundle; never builds/runs user code."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import uuid
import zipfile

PLATFORM = Path(__file__).resolve().parent
sys.path[:0] = [str(PLATFORM), str(PLATFORM / 'gate')]
import bundle
from observability import OperationError
from process import run_bounded
from storage import durable_write

MAX_ARCHIVE_BYTES = 4 * 1024 ** 3


def checked_request(env):
    values = {key: env.get('RAILSHOT_' + key, '') for key in
              ('SOURCE_REF', 'ARTIFACT_BUCKET', 'REGISTRY_PREFIX', 'JOB_ID', 'MANIFEST_SHA256', 'ARCHIVE_SHA256')}
    if (not re.fullmatch(r'[a-f0-9]{40}', values['SOURCE_REF'])
            or env.get('CODEBUILD_RESOLVED_SOURCE_VERSION') != values['SOURCE_REF']
            or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{2,62}', values['ARTIFACT_BUCKET'])
            or not re.fullmatch(r'[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/railshot-[a-z0-9-]+', values['REGISTRY_PREFIX'])
            or any(not re.fullmatch(r'[a-f0-9]{64}', values[key]) for key in ('MANIFEST_SHA256', 'ARCHIVE_SHA256'))
            or str(uuid.UUID(values['JOB_ID'])) != values['JOB_ID']):
        raise ValueError('invalid trusted publisher binding')
    return values


def unpack(path, destination):
    """Only the four bundle files; no archive path, link or oversized expansion."""
    if path.stat().st_size > MAX_ARCHIVE_BYTES: raise ValueError('archive too large')
    with zipfile.ZipFile(path) as archive:
        items = archive.infolist()
        names = {i.filename for i in items}
        spec = bundle.spec_name(names)
        if (len(items) != 4 or names != {spec, 'verdict.json', 'images.tar', 'manifest.json'}
                or sum(i.file_size for i in items) > MAX_ARCHIVE_BYTES
                or any(i.flag_bits & 1 or stat.S_ISLNK(i.external_attr >> 16) for i in items)):
            raise ValueError('invalid bundle archive')
        destination.mkdir(mode=0o700)
        for item in items:
            target = destination / item.filename
            with archive.open(item) as source, target.open('xb') as output:
                os.fchmod(output.fileno(), 0o600)
                while chunk := source.read(1024 * 1024): output.write(chunk)


def aws(*args):
    return run_bounded(['aws', *args, '--no-cli-pager'], timeout=300, check=True)


def publish(env):
    try:
        request = checked_request(env)
    except (ValueError, TypeError, KeyError) as exc:
        raise OperationError('CD_CONFIG_INVALID', component='codebuild.release', phase='configure',
                              retry_policy='after_configuration', cause=exc) from exc
    with tempfile.TemporaryDirectory(prefix='railshot-release-') as temporary:
        root = Path(temporary); archive = root / 'bundle.zip'; source = root / 'bundle'
        key = 'bundles/' + request['ARCHIVE_SHA256'] + '.zip'
        aws('s3api', 'get-object', '--bucket', request['ARTIFACT_BUCKET'], '--key', key, str(archive))
        if bundle.file_hash(archive) != request['ARCHIVE_SHA256']: raise ValueError('archive changed')
        unpack(archive, source)
        if bundle.file_hash(source / 'manifest.json') != request['MANIFEST_SHA256']: raise ValueError('manifest changed')
        manifest = bundle.verify(source)
        if set(manifest['images']) != {'web'}: raise ValueError('publisher supports only the registered web repository')
        registry = request['REGISTRY_PREFIX'].split('/')[0]
        region = registry.split('.')[3]
        # Temporary role credentials remain inside trusted CodeBuild. Never log the password.
        password = aws('ecr', 'get-login-password', '--region', region).stdout.strip()
        if not password: raise ValueError('ECR authentication unavailable')
        auth = root / 'registry-auth.json'; durable_write(auth, b'{"auths":{}}')
        run_bounded(['skopeo', 'login', '--authfile', str(auth), '--username', 'AWS', '--password-stdin', registry],
                    input=(password + '\n').encode(), timeout=30, check=True)
        journal = root / 'publish-journal'
        try:
            images = bundle.publish(source, request['REGISTRY_PREFIX'], 'run-' + request['JOB_ID'],
                                    journal_dir=journal, backend='skopeo', authfile=auth)
        except Exception:
            # Keep bounded native diagnostics in the private artifact channel, never stdout.
            failure = {'schema_version': 1, 'job_id': request['JOB_ID'], 'build_id': env['CODEBUILD_BUILD_ID'],
                       'platform_ref': request['SOURCE_REF'], 'manifest_sha256': request['MANIFEST_SHA256'],
                       'diagnostics': [json.loads(p.read_bytes()) for p in sorted(journal.glob('native-failure-*.json'))[:4]],
                       'journal': json.loads((journal / 'publish.json').read_bytes()) if (journal / 'publish.json').is_file() else None}
            private = root / 'failure.json'; durable_write(private, json.dumps(failure, sort_keys=True).encode())
            try:
                aws('s3api', 'put-object', '--bucket', request['ARTIFACT_BUCKET'],
                    '--key', 'receipts/' + request['JOB_ID'] + '.failure.json', '--body', str(private), '--if-none-match', '*')
            except Exception as exc:
                raise OperationError('OBSERVATION_WRITE_FAILED', component='codebuild.release',
                                     phase='publish.diagnostic', outcome='UNKNOWN',
                                     retry_policy='after_reconcile', side_effect='possible', cause=exc) from exc
            raise
        receipt = {'schema_version': 1, 'outcome': 'PASS', 'operation': 'publish_only',
                   'job_id': request['JOB_ID'], 'build_id': env['CODEBUILD_BUILD_ID'],
                   'platform_ref': request['SOURCE_REF'], 'manifest_sha256': request['MANIFEST_SHA256'],
                   'source_sha256': manifest['source_sha256'], 'images': images,
                   'deployment_status': 'NOT_RUN'}
        output = root / 'receipt.json'; durable_write(output, json.dumps(receipt, sort_keys=True).encode())
        aws('s3api', 'put-object', '--bucket', request['ARTIFACT_BUCKET'],
            '--key', 'receipts/' + request['JOB_ID'] + '.json', '--body', str(output), '--if-none-match', '*')
        return receipt


if __name__ == '__main__':
    try:
        print(json.dumps(publish(os.environ)))
    except Exception as exc:
        error = exc if isinstance(exc, OperationError) else OperationError(
            'STEP_OUTPUT_INVALID', component='codebuild.release', phase='publish', outcome='UNKNOWN',
            retry_policy='after_reconcile', side_effect='unknown', cause=exc)
        print(json.dumps({'outcome': error.outcome, 'error': error.as_dict()}))
        raise SystemExit(1)
