#!/usr/bin/env python3
"""Build the immutable personal installer tree embedded in the dashboard image."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile


def load_packager(source):
    path = Path(source) / 'deployment/scripts/package-personal-client.py'
    spec = importlib.util.spec_from_file_location('railshot_personal_packager', path)
    if not spec or not spec.loader:
        raise ValueError('Personal packager is unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or output.is_symlink():
        raise ValueError('Release output already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.personal-release-', dir=output.parent))
    try:
        artifact = staging / 'personal-client.tgz'
        result = load_packager(source).package(source, artifact)
        digest = result['artifact_sha256']
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError('Invalid artifact digest')
        release = staging / digest
        release.mkdir(mode=0o755)
        artifact.rename(release / artifact.name)
        installer = source / 'deployment/bootstrap/install.sh'
        if not installer.is_file() or installer.is_symlink():
            raise ValueError('Canonical installer missing or unsafe')
        shutil.copyfile(installer, release / 'install.sh')
        (release / 'install.sh').chmod(0o555)
        manifest = {
            'version': 1,
            'release': digest,
            'artifact_sha256': digest,
            'artifact_path': f'/personal/{digest}/personal-client.tgz',
            'artifact_size': (release / 'personal-client.tgz').stat().st_size,
            'installer_path': f'/personal/{digest}/install.sh',
            'installer_sha256': hashlib.sha256((release / 'install.sh').read_bytes()).hexdigest(),
            'installer_size': (release / 'install.sh').stat().st_size,
        }
        (staging / 'manifest.json').write_text(
            json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
        staging.rename(output)
        return manifest
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output), sort_keys=True))


if __name__ == '__main__':
    main()
