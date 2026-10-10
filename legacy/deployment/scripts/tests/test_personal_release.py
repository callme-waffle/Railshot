import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location('personal_release', ROOT / 'deployment/scripts/package-personal-release.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def test_release_is_content_addressed_and_reproducible():
    with tempfile.TemporaryDirectory() as temporary:
        first, second = Path(temporary) / 'first', Path(temporary) / 'second'
        one, two = release.build(ROOT, first), release.build(ROOT, second)
        assert one == two
        digest = one['release']
        artifact, installer = first / digest / 'personal-client.tgz', first / digest / 'install.sh'
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == digest == one['artifact_sha256']
        assert hashlib.sha256(installer.read_bytes()).hexdigest() == one['installer_sha256']
        assert json.loads((first / 'manifest.json').read_text()) == one
        assert one['artifact_path'] == f'/personal/{digest}/personal-client.tgz'
        assert one['installer_path'] == f'/personal/{digest}/install.sh'


def test_release_never_replaces_existing_output():
    with tempfile.TemporaryDirectory() as temporary:
        output = Path(temporary) / 'release'
        release.build(ROOT, output)
        try:
            release.build(ROOT, output)
        except ValueError as error:
            assert str(error) == 'Release output already exists'
        else:
            raise AssertionError('existing immutable release was replaced')
