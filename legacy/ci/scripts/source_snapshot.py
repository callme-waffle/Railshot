#!/usr/bin/env python3
"""Preserve the exact gate-tested source separately from the immutable image bundle."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'gate'))
from bundle import contract, file_hash, require, source_digest, spec_name
from storage import durable_write

MAX_BYTES = 100 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 140 * 1024 * 1024
MAX_FILES, MAX_ENTRIES = 2000, 6000
SECRET = re.compile(rb'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----|\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|(?:AKIA|ASIA)[A-Z0-9]{16}|apikey_[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9_-]{16,})\b')


def safe_path(name):
    require(isinstance(name, str) and 0 < len(name) <= 1024 and len(name.split('/')) <= 32
            and not name.startswith('/') and not re.search(r'[\x00-\x1f\x7f\\]', name)
            and all(part not in ('', '.', '..') for part in name.split('/')), 'unsafe source path')
    for part in name.split('/'):
        require(part not in {'.git', 'node_modules', '__MACOSX', '.DS_Store', '.ssh', '.aws', '.kube', '.codex',
                            '.npmrc', '.pypirc', '.netrc'} and not re.search(
            r'^\.env(?:\.|$)|\.(?:pem|key|p12|pfx)$|^id_(?:rsa|ed25519|ecdsa)$', part, re.I), 'private or excluded source path')


def identity(env):
    value = {key: env[key.upper()] for key in ('source_commit', 'app', 'tenant', 'target_id')}
    for key, pattern in {'source_commit': r'[a-f0-9]{40}', 'app': r'[a-z][a-z0-9-]{1,28}[a-z0-9]',
                         'tenant': r'[a-z0-9]{1,20}', 'target_id': r'[a-z][a-z0-9-]{0,62}'}.items():
        require(re.fullmatch(pattern, value[key]), 'invalid source identity')
    require(env['GITHUB_SHA'] == value['source_commit'], 'source commit differs')
    for field, variable in [('run_id', 'GITHUB_RUN_ID'), ('producer_attempt', 'GITHUB_RUN_ATTEMPT')]:
        require(re.fullmatch(r'[1-9][0-9]*', env[variable]), 'invalid producer identity')
        value[field] = int(env[variable])
        require(value[field] <= (100 if field == 'producer_attempt' else 2**53 - 1), 'invalid producer identity')
    return value


def bundle_source(bundle):
    bundle = Path(bundle)
    require(bundle.is_dir() and not bundle.is_symlink(), 'invalid bundle directory')
    name = spec_name(path.name for path in bundle.iterdir())
    for filename in (name, 'manifest.json', 'verdict.json'):
        require((bundle / filename).lstat().st_size <= 2 * 1024 * 1024, 'bundle metadata too large')
        file_hash(bundle / filename)
    manifest = json.loads((bundle / 'manifest.json').read_bytes())
    verdict = contract((bundle / name).read_bytes(), (bundle / 'verdict.json').read_bytes())
    require(manifest.get('version') == 1 and manifest.get('trust') == 'trusted-ci-artifact-not-a-signature'
            and manifest.get('source_sha256') == verdict['source_sha256']
            and all(manifest.get('files', {}).get(filename) == file_hash(bundle / filename)
                    for filename in (name, 'verdict.json')), 'bundle source binding differs')
    return verdict['source_sha256']


def entries_digest(entries):
    require(isinstance(entries, list) and 0 < len(entries) <= MAX_ENTRIES, 'invalid source entries')
    digest = hashlib.sha256(b'railshot-source-v1\0')
    paths, directories = set(), {''}
    total, count = 0, 0
    children = {}
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) == {'path', 'type', 'mode', 'size', 'content'}, 'invalid source entry')
        path, kind, mode, size = (entry[key] for key in ('path', 'type', 'mode', 'size'))
        safe_path(path)
        require(path not in paths and kind in {'f', 'd'} and type(mode) is int and 0 <= mode <= 0o777
                and type(size) is int and 0 <= size <= MAX_BYTES, 'invalid source metadata')
        paths.add(path)
        parent = str(PurePosixPath(path).parent)
        children.setdefault('' if parent == '.' else parent, []).append(entry)
        if kind == 'd':
            require(size == 0 and entry['content'] is None, 'invalid directory metadata')
            directories.add(path)
        else:
            count += 1; total += size
            require(count <= MAX_FILES and total <= MAX_BYTES and isinstance(entry['content'], str)
                    and len(entry['content']) == 4 * ((size + 2) // 3), 'source exceeds limit')
    require(count > 0 and set(children) <= directories, 'source parent directory missing')
    visited = []
    def visit(parent):
        for entry in sorted(children.get(parent, []), key=lambda row: PurePosixPath(row['path']).name):
            visited.append(entry['path'])
            header = json.dumps([entry['path'], entry['type'], entry['mode'], entry['size']], separators=(',', ':')).encode()
            digest.update(len(header).to_bytes(8, 'big') + header)
            if entry['type'] == 'f':
                content = base64.b64decode(entry['content'], validate=True)
                require(len(content) == entry['size'] and base64.b64encode(content).decode() == entry['content']
                        and not SECRET.search(content), 'invalid or private source content')
                digest.update(content)
            else:
                visit(entry['path'])
    visit('')
    require(visited == [entry['path'] for entry in entries], 'source order differs')
    return digest.hexdigest()


def save(value, output):
    content = json.dumps(value, separators=(',', ':')).encode()
    require(len(content) <= MAX_SNAPSHOT_BYTES, 'source snapshot too large')
    output = Path(output)
    require(not output.exists() and not output.is_symlink(), 'source snapshot already exists')
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    durable_write(output, content)


def export(workspace, bundle, output, env):
    workspace = Path(workspace)
    require(not Path(output).resolve().is_relative_to(workspace.resolve()), 'snapshot must be outside source')
    source = bundle_source(bundle)
    require(source_digest(workspace) == source, 'source changed since gate')
    entries = capture(workspace)
    require(entries_digest(entries) == source_digest(workspace) == source, 'snapshot differs from gate source')
    save({'version': 1, **identity(env), 'source_sha256': source, 'entries': entries}, output)


def capture(workspace):
    """Read bounded source bytes; callers separately establish purpose and producer identity."""
    workspace = Path(workspace)
    entries = []
    total = 0
    def visit(directory):
        nonlocal total
        for path in sorted(directory.iterdir(), key=lambda p: p.name):
            if path.name == '.git':
                continue  # The gate digest explicitly excludes .git at every depth.
            name, info = path.relative_to(workspace).as_posix(), path.lstat()
            safe_path(name)
            require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode), 'source links and special files forbidden')
            require(stat.S_IMODE(info.st_mode) <= 0o777 and len(entries) < MAX_ENTRIES, 'invalid source mode or count')
            content = None
            if stat.S_ISREG(info.st_mode):
                require(info.st_nlink == 1, 'source hard links forbidden')
                total += info.st_size
                require(total <= MAX_BYTES, 'source too large')
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
                    require(os.fstat(stream.fileno()) == info, 'source changed during export')
                    content = base64.b64encode(stream.read(MAX_BYTES + 1)).decode()
            entries.append({'path': name, 'type': 'd' if content is None else 'f', 'mode': stat.S_IMODE(info.st_mode),
                            'size': 0 if content is None else info.st_size, 'content': content})
            if content is None:
                visit(path)
    visit(workspace)
    require(entries_digest(entries) == source_digest(workspace), 'source changed during capture')
    return entries


def bind(snapshot, bundle, output, env):
    snapshot = Path(snapshot)
    require(snapshot.is_file() and not snapshot.is_symlink() and snapshot.stat().st_size <= MAX_SNAPSHOT_BYTES, 'invalid snapshot file')
    value, bound = json.loads(snapshot.read_bytes()), identity(env)
    require(isinstance(value, dict) and set(value) == {'version', *bound, 'source_sha256', 'entries'}
            and value['version'] == 1 and type(value['producer_attempt']) is int
            and 1 <= value['producer_attempt'] <= bound['producer_attempt']
            and all(value[key] == expected for key, expected in bound.items() if key != 'producer_attempt'), 'source producer binding differs')
    require(entries_digest(value['entries']) == value['source_sha256'] == bundle_source(bundle), 'source digest differs')
    save({**value, **bound}, output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['export', 'bind'])
    parser.add_argument('source'); parser.add_argument('bundle'); parser.add_argument('output')
    args = parser.parse_args()
    try:
        (export if args.action == 'export' else bind)(args.source, args.bundle, args.output, os.environ)
    except (ValueError, OSError, KeyError, TypeError):
        print('SOURCE_SNAPSHOT_INVALID: final source could not be verified', file=sys.stderr)
        sys.exit(1)
