#!/usr/bin/env python3
"""Bind a release to the current trusted branch and its successful full CI gate."""
import argparse
from datetime import datetime
import hashlib
import io
import json
import os
import re
import stat
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import zipfile

REPOSITORY = 'Jasmin-Softbank/Railshot'
REPOSITORY_ID = 1400202256
REFS = {'refs/heads/main', 'refs/heads/integration/team-assembly-20261002'}
COMPONENTS = ('dashboard', 'api', 'ci-runner')
ARCHIVE_LIMIT = 1_000_000


def require(condition, code):
    if not condition:
        raise ValueError(code)


def headers():
    result = {'Accept': 'application/vnd.github+json', 'User-Agent': 'railshot-release',
              'X-GitHub-Api-Version': '2022-11-28'}
    if os.environ.get('GITHUB_TOKEN'):
        result['Authorization'] = 'Bearer ' + os.environ['GITHUB_TOKEN']
    return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ValueError('GITHUB_API_REDIRECT_REJECTED')


class ArtifactRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, response_headers, newurl):
        destination = urlsplit(newurl)
        host = destination.hostname or ''
        require(destination.scheme == 'https' and destination.port in (None, 443)
                and not destination.username and not destination.password and not destination.fragment
                and any(host.endswith(suffix) and host != suffix[1:] for suffix in
                        ('.blob.core.windows.net', '.actions.githubusercontent.com', '.githubusercontent.com')),
                'ARTIFACT_REDIRECT_REJECTED')
        # Never forward the repository token to the signed archive URL, even on later hops.
        redirected = super().redirect_request(req, fp, code, msg, response_headers, newurl)
        require(redirected is not None, 'ARTIFACT_REDIRECT_REJECTED')
        for key, _ in list(redirected.header_items()):
            if key.lower() not in ('accept', 'user-agent'):
                redirected.remove_header(key)
        return redirected


def github(path):
    with build_opener(ProxyHandler({}), NoRedirect()).open(
            Request('https://api.github.com/repos/' + REPOSITORY + '/' + path, headers=headers()), timeout=20) as response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError('GITHUB_RESPONSE_TOO_LARGE')
    return json.loads(data)


def download_artifact(artifact_id):
    require(type(artifact_id) is int and artifact_id > 0, 'PUBLICATION_ARTIFACT_INVALID')
    url = f'https://api.github.com/repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip'
    with build_opener(ProxyHandler({}), ArtifactRedirect()).open(Request(url, headers=headers()), timeout=30) as response:
        body = response.read(ARCHIVE_LIMIT + 1)
    require(0 < len(body) <= ARCHIVE_LIMIT, 'PUBLICATION_ARCHIVE_TOO_LARGE')
    return body


def timestamp(value):
    require(isinstance(value, str), 'PUBLICATION_TIMESTAMP_INVALID')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('PUBLICATION_TIMESTAMP_INVALID') from None
    require(result.tzinfo is not None, 'PUBLICATION_TIMESTAMP_INVALID')
    return result


def publication_run(run, source_sha, ref, run_id, attempt):
    require(isinstance(run, dict) and run.get('id') == run_id and run.get('run_attempt') == attempt
            and run.get('head_sha') == source_sha and run.get('head_branch') == ref.removeprefix('refs/heads/')
            and all(run.get(key, {}).get('full_name') == REPOSITORY
                    and run.get(key, {}).get('id') == REPOSITORY_ID for key in ('repository', 'head_repository'))
            and run.get('path', '').split('@')[0] in
                ('.github/workflows/platform-publish.yml', '.github/workflows/railshot-ci.yml')
            and run.get('event') in ('push', 'workflow_dispatch') and run.get('conclusion') in (None, 'success'),
            'PUBLICATION_RUN_BINDING_MISMATCH')


def archive_image(body, artifact, component, image):
    require(isinstance(body, bytes) and 0 < len(body) <= ARCHIVE_LIMIT, 'PUBLICATION_ARCHIVE_TOO_LARGE')
    checksum = hashlib.sha256(body).hexdigest()
    if artifact.get('digest') is not None:
        require(artifact['digest'] == 'sha256:' + checksum, 'PUBLICATION_ARCHIVE_DIGEST_MISMATCH')
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            entries = archive.infolist()
            require(len(entries) == 1 and entries[0].filename == component + '.json', 'PUBLICATION_ARCHIVE_CONTENT_INVALID')
            entry = entries[0]
            mode = entry.external_attr >> 16
            require(0 < entry.file_size <= 4096 and not entry.flag_bits & 1
                    and stat.S_IFMT(mode) in (0, stat.S_IFREG), 'PUBLICATION_ARCHIVE_CONTENT_INVALID')
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    require(key not in result, 'PUBLICATION_ARCHIVE_CONTENT_INVALID')
                    result[key] = value
                return result
            value = json.loads(archive.read(entry), object_pairs_hook=unique)
    except (zipfile.BadZipFile, RuntimeError, UnicodeError, json.JSONDecodeError):
        raise ValueError('PUBLICATION_ARCHIVE_CONTENT_INVALID') from None
    require(value == {component: image}, 'PUBLICATION_IMAGE_PIN_MISMATCH')
    return checksum


def publication(source_sha, ref, run_id, run_attempt, images, *, read=github, download=download_artifact):
    """Bind three pins to completed publishers and archives from one exact current attempt."""
    require(ref in REFS and isinstance(source_sha, str) and re.fullmatch('[a-f0-9]{40}', source_sha),
            'TRUSTED_RELEASE_SOURCE_REQUIRED')
    require(all(re.fullmatch('[1-9][0-9]{0,19}', str(value)) for value in (run_id, run_attempt)),
            'PUBLICATION_RUN_ID_INVALID')
    run_id, attempt = int(run_id), int(run_attempt)
    require(set(images) == set(COMPONENTS) and all(isinstance(images[name], str) and re.fullmatch(
            r'ghcr\.io/jasmin-softbank/railshot-' + name + r'@sha256:[a-f0-9]{64}', images[name])
            for name in COMPONENTS), 'PUBLICATION_IMAGE_PIN_INVALID')
    run_path = 'actions/runs/' + str(run_id)
    publication_run(read(run_path), source_sha, ref, run_id, attempt)
    jobs = read(f'{run_path}/attempts/{attempt}/jobs?per_page=100')
    artifacts = read(f'{run_path}/artifacts?per_page=100')
    for value, key in ((jobs, 'jobs'), (artifacts, 'artifacts')):
        require(isinstance(value.get(key), list) and type(value.get('total_count')) is int
                and len(value[key]) == value['total_count'] <= 100, 'PUBLICATION_LIST_INCOMPLETE')
    proof = {}
    for component in COMPONENTS:
        publishers = [job for job in jobs['jobs'] if re.search(r'(?:^| / )publish \(' + re.escape(component) + r'\)$', job.get('name', ''))]
        require(len(publishers) == 1, 'PUBLICATION_PUBLISHER_NOT_VERIFIED')
        publisher = publishers[0]
        require(publisher.get('run_id') == run_id and publisher.get('head_sha') == source_sha
                and publisher.get('status') == 'completed' and publisher.get('conclusion') == 'success',
                'PUBLICATION_PUBLISHER_NOT_VERIFIED')
        matches = [artifact for artifact in artifacts['artifacts']
                   if artifact.get('name') == f'platform-image-{component}-{source_sha}']
        require(len(matches) == 1, 'PUBLICATION_ARTIFACT_NOT_UNIQUE')
        artifact = matches[0]; producer = artifact.get('workflow_run', {})
        require(type(artifact.get('id')) is int and artifact['id'] > 0 and artifact.get('expired') is False
                and type(artifact.get('size_in_bytes')) is int and 0 < artifact['size_in_bytes'] <= ARCHIVE_LIMIT
                and producer.get('id') == run_id and producer.get('head_sha') == source_sha
                and producer.get('head_branch') == ref.removeprefix('refs/heads/')
                and producer.get('repository_id') == producer.get('head_repository_id') == REPOSITORY_ID,
                'PUBLICATION_ARTIFACT_BINDING_MISMATCH')
        # Run-level artifact listings include older attempts. Bind creation to this publisher.
        require(timestamp(publisher.get('started_at')) <= timestamp(artifact.get('created_at'))
                <= timestamp(publisher.get('completed_at')), 'PUBLICATION_ARTIFACT_ATTEMPT_MISMATCH')
        checksum = archive_image(download(artifact['id']), artifact, component, images[component])
        proof[component] = {'id': artifact['id'], 'sha256': checksum}
    publication_run(read(run_path), source_sha, ref, run_id, attempt)
    return {'status': 'verified', 'source_sha': source_sha, 'run_id': run_id, 'run_attempt': attempt, 'artifacts': proof}


def admit(source_sha, ref, run_id=None, *, read=github):
    if ref not in REFS or not re.fullmatch('[a-f0-9]{40}', source_sha):
        raise ValueError('TRUSTED_RELEASE_SOURCE_REQUIRED')
    branch = ref.removeprefix('refs/heads/')
    if read('git/ref/heads/' + branch)['object']['sha'] != source_sha:
        raise ValueError('SUPERSEDED_RELEASE_SOURCE')
    if run_id:
        if not re.fullmatch('[1-9][0-9]{0,19}', str(run_id)):
            raise ValueError('CI_RUN_ID_INVALID')
        runs = [read('actions/runs/' + str(run_id))]
    else:
        runs = read('actions/workflows/railshot-ci.yml/runs?head_sha=' + source_sha + '&per_page=30')['workflow_runs']
    for run in runs:
        if (run.get('head_sha') != source_sha or run.get('head_branch') != branch
                or run.get('head_repository', {}).get('full_name') != REPOSITORY
                or run.get('path', '').split('@')[0] != '.github/workflows/railshot-ci.yml'
                or run.get('event') not in ('push', 'workflow_dispatch')
                or run.get('conclusion') not in (None, 'success')):
            continue
        attempt = run.get('run_attempt', 1)
        jobs = read(f"actions/runs/{run['id']}/attempts/{attempt}/jobs?per_page=100")
        if jobs.get('total_count', 101) > 100:
            raise ValueError('CI_JOB_LIST_INCOMPLETE')
        gates = [job for job in jobs['jobs'] if job['name'] == 'Railshot CI gate']
        if len(gates) == 1 and gates[0]['status'] == 'completed' and gates[0]['conclusion'] == 'success':
            return {'status': 'admitted', 'source_sha': source_sha, 'ref': ref, 'ci_run_id': run['id'], 'ci_run_attempt': attempt}
    raise ValueError('EXACT_SOURCE_CI_GATE_REQUIRED')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--ref', required=True)
    parser.add_argument('--ci-run-id', default='')
    parser.add_argument('--skip-superseded', action='store_true',
                        help='Report superseded sources as a non-mutating Actions skip; requires GITHUB_OUTPUT')
    args = parser.parse_args(argv)
    if args.skip_superseded and not os.environ.get('GITHUB_OUTPUT'):
        parser.error('--skip-superseded requires GITHUB_OUTPUT')
    exit_code = 0
    try:
        result = admit(args.source_sha, args.ref, args.ci_run_id)
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) else 'CI_ADMISSION_UNAVAILABLE'
        superseded = args.skip_superseded and code == 'SUPERSEDED_RELEASE_SOURCE'
        result = {'status': 'superseded' if superseded else 'blocked', 'code': code}
        exit_code = 0 if superseded else 1
    if args.skip_superseded:
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('admitted=' + str(result['status'] == 'admitted').lower() + '\n')
        if result['status'] == 'superseded' and os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
                summary.write('### Release superseded\nA newer source replaced this release. '
                              'No mutation follows this check; this is not deployment verification.\n')
    print(json.dumps(result))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
