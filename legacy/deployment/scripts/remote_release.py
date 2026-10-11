#!/usr/bin/env python3
"""Invoke only the pinned multicloud SSM document, once, then verify its aggregate proof."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys
import time

spec = importlib.util.spec_from_file_location('platform_verifier', Path(__file__).with_name('verify-platform.py'))
verifier = importlib.util.module_from_spec(spec); spec.loader.exec_module(verifier)
DOCUMENT = 'Railshot-MulticloudRelease'


def release(source_sha, revision, images, version, document_hash, publication_run_id, publication_run_attempt, *, scope='multicloud', call=verifier.aws, sleep=time.sleep, clock=time.monotonic):
    if scope not in {'ci-runtime', 'multicloud'}: raise ValueError('RELEASE_SCOPE_INVALID')
    for value in (source_sha, revision):
        if not re.fullmatch('[a-f0-9]{40}', value): raise ValueError('RELEASE_SHA_INVALID')
    if set(images) != {'dashboard', 'api', 'ci-runner'}: raise ValueError('RELEASE_IMAGES_MISSING')
    parameters = {'SourceSha': [source_sha], 'Revision': [revision]}
    if scope == 'ci-runtime': parameters['Scope'] = [scope]
    for name, parameter in (('dashboard', 'DashboardDigest'), ('api', 'ApiDigest'), ('ci-runner', 'RunnerDigest')):
        prefix = 'ghcr.io/jasmin-softbank/railshot-' + name + '@sha256:'
        image = images[name]
        if not isinstance(image, str) or not re.fullmatch(re.escape(prefix) + '[a-f0-9]{64}', image):
            raise ValueError('RELEASE_IMAGE_INVALID')
        parameters[parameter] = [image[len(prefix):]]
    if not re.fullmatch('[1-9][0-9]*', version) or not re.fullmatch('[a-f0-9]{64}', document_hash):
        raise ValueError('RELEASE_DOCUMENT_PIN_REQUIRED')
    for name, value in [('PublicationRunId', publication_run_id), ('PublicationRunAttempt', publication_run_attempt)]:
        if not re.fullmatch('[1-9][0-9]{0,19}', str(value)):
            raise ValueError('PUBLICATION_RUN_REQUIRED')
        parameters[name] = [str(value)]
    sent = call('send-command', '--document-name', DOCUMENT, '--document-version', version,
        '--document-hash', document_hash, '--document-hash-type', 'Sha256', '--instance-ids', verifier.INSTANCE,
        '--timeout-seconds', '60', '--parameters', json.dumps(parameters))
    command_id = sent['Command']['CommandId']
    if not re.fullmatch('[a-f0-9-]{36}', command_id): raise ValueError('RELEASE_COMMAND_ID_INVALID')
    deadline = clock() + 4800
    while clock() < deadline:
        try:
            observed = call('get-command-invocation', '--command-id', command_id, '--instance-id', verifier.INSTANCE)
        except Exception:
            return {'status': 'unknown', 'source_sha': source_sha, 'command_id': command_id, 'code': 'RELEASE_READBACK_REQUIRED'}
        if observed.get('Status') in ('Pending', 'InProgress', 'Delayed'):
            sleep(10); continue
        try: proof = json.loads(observed.get('StandardOutputContent', ''))
        except (ValueError, TypeError): proof = {'status': 'unknown', 'code': 'REMOTE_RECEIPT_UNAVAILABLE'}
        if not isinstance(proof, dict):
            proof = {'status': 'unknown', 'code': 'REMOTE_RECEIPT_UNAVAILABLE'}
        targets = proof.get('targets', [])
        if not isinstance(targets, list) or any(not isinstance(target, dict) for target in targets):
            targets = []
        bound = (proof.get('source_sha') == source_sha and len(targets) == 3
                 and {t.get('provider') for t in targets} == {'aws', 'gcp', 'openstack'}
                 and all(isinstance(t.get('target_id'), str) and re.fullmatch('[a-z][a-z0-9-]{0,62}', t['target_id']) for t in targets)
                 and len({t['target_id'] for t in targets}) == 3
                 and all(t.get('status') == 'verified' and t.get('source_sha') == source_sha for t in targets))
        if scope == 'ci-runtime':
            worker, apps = proof.get('workers'), proof.get('apps')
            bound = (proof.get('scope') == scope and proof.get('source_sha') == source_sha
                     and isinstance(worker, dict) and worker.get('status') == 'verified'
                     and worker.get('source_sha') == source_sha and worker.get('images') == images
                     and worker.get('executable_verification') is True and worker.get('runner_jobs') == 'preserved'
                     and isinstance(apps, dict) and apps.get('status') == 'verified'
                     and apps.get('source_sha') == source_sha and apps.get('platform_ref') == source_sha)
        if not (observed.get('Status') == 'Success' and observed.get('ResponseCode') == 0
                and proof.get('status') == 'verified' and bound):
            proof['status'] = 'incomplete'
        return {**proof, 'command_id': command_id, 'requested_source_sha': source_sha,
                'ssm_status': observed.get('Status'), 'ssm_response_code': observed.get('ResponseCode')}
    return {'status': 'unknown', 'source_sha': source_sha, 'command_id': command_id, 'code': 'RELEASE_TIMEOUT_READBACK_REQUIRED'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-sha', required=True); parser.add_argument('--revision', required=True)
    parser.add_argument('--scope', choices=('ci-runtime', 'multicloud'), default='multicloud')
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--document-version', required=True); parser.add_argument('--document-hash', required=True)
    parser.add_argument('--publication-run-id', required=True); parser.add_argument('--publication-run-attempt', required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    try:
        images = {}
        for name in ('dashboard', 'api', 'ci-runner'):
            item = json.loads((args.artifacts / (name + '.json')).read_text())
            if set(item) != {name}: raise ValueError('IMAGE_ARTIFACT_INVALID')
            images.update(item)
        result = release(args.source_sha, args.revision, images, args.document_version, args.document_hash, args.publication_run_id, args.publication_run_attempt, scope=args.scope)
    except Exception as error:
        result = {'status': 'unknown', 'source_sha': args.source_sha,
                  'code': str(error) if isinstance(error, ValueError) and re.fullmatch('[A-Z_]+', str(error)) else 'RELEASE_READBACK_REQUIRED'}
    args.receipt.write_text(json.dumps(result) + '\n')
    print(json.dumps(result))
    sys.exit(0 if result['status'] == 'verified' else 1)
