"""Local control-plane bootstrap contracts; no Docker, cluster or credential mutation."""
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


bootstrap = load('tested_control_bootstrap', ROOT / 'deployment/manifests/personal/control-bootstrap.py')
minter = load('tested_control_minter', ROOT / 'deployment/manifests/personal/control-token-minter.py')


def jwt(now=1_800_000_000):
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
    return encode({'alg': 'none'}) + '.' + encode({'iat': now, 'exp': now + 3600}) + '.signature'


def test_token_minter_writes_tokenfile_kubeconfig_and_no_inline_token():
    value = minter.kubeconfig()
    user = value['users'][0]['user']; cluster = value['clusters'][0]['cluster']
    assert user == {'tokenFile': '/run/railshot-kubernetes/token'}
    assert cluster == {'server': 'https://127.0.0.1:6443',
                       'certificate-authority': '/run/railshot-kubernetes/ca.crt'}
    assert minter.jwt_expiry(jwt()) == 1_800_003_600
    assert 'signature' not in json.dumps(value)


def test_token_minter_rejects_malformed_or_unbounded_tokens():
    for value in ('private', 'a.b.c.d', 'e30.broken.sig'):
        with pytest.raises(ValueError):
            minter.jwt_expiry(value)


def test_gateway_guard_uses_only_fixed_restore(monkeypatch, tmp_path):
    config = tmp_path / 'config.json'; config.write_text('{}'); config.chmod(0o600)
    calls = []
    class Result:
        returncode = 0
        stdout = '{"status":"succeeded","restored_peers":2}'
    monkeypatch.setattr(minter, 'private_file', lambda path, **kwargs: Path(path))
    monkeypatch.setattr(minter.subprocess, 'run', lambda args, **kwargs: calls.append(args) or Result())
    assert minter.restore_gateway(config) == 2
    assert calls == [['python3', '/opt/railshot/deployment/scripts/personal_wireguard.py',
                      '--config', str(config), '--restore']]


def test_can_i_accepts_only_kubectl_yes_and_denied_no(monkeypatch, tmp_path):
    class Result:
        def __init__(self, code, output):
            self.returncode, self.stdout = code, output
    results = iter((Result(0, 'yes\n'), Result(1, 'no\n'), Result(0, 'no\n')))
    monkeypatch.setattr(minter.subprocess, 'run', lambda *args, **kwargs: next(results))
    assert minter.can_i(['get', 'roles'], kubeconfig=tmp_path / 'config') is True
    assert minter.can_i(['get', 'secrets'], kubeconfig=tmp_path / 'config') is False
    with pytest.raises(ValueError):
        minter.can_i(['get', 'secrets'], kubeconfig=tmp_path / 'config')


def test_token_verification_checks_only_declared_bootstrap_permissions(monkeypatch, tmp_path):
    checked = []
    monkeypatch.setattr(minter, 'run', lambda *args, **kwargs: json.dumps({
        'status': {'userInfo': {'username': minter.USERNAME}}}))
    def allowed(args, **kwargs):
        checked.append(tuple(args))
        return tuple(args) in {
            ('get', 'roles/railshot-credentials', '-n', 'argocd'),
            ('get', 'roles/railshot-product-registrations', '-n', 'argocd'),
            ('create', 'secrets', '-n', 'argocd'),
        }
    monkeypatch.setattr(minter, 'can_i', allowed)
    results = minter.verify(tmp_path / 'config', 4_000, clock=lambda: 0)
    assert len(results) == 5
    assert not any('registration-bootstrap' in value for row in checked for value in row)
    assert ('create', 'clusterroles') in checked
    assert ('get', 'secrets', '-n', 'kube-system') in checked


def oci_archive(path, *, architecture='arm64'):
    config = json.dumps({'os': 'linux', 'architecture': architecture}).encode()
    config_digest = 'sha256:' + hashlib.sha256(config).hexdigest()
    manifest = json.dumps({'schemaVersion': 2, 'config': {'digest': config_digest}, 'layers': []}).encode()
    manifest_digest = 'sha256:' + hashlib.sha256(manifest).hexdigest()
    index = json.dumps({'schemaVersion': 2, 'manifests': [{'digest': manifest_digest}]}).encode()
    with tarfile.open(path, 'w') as archive:
        for name, data in [('index.json', index), ('blobs/sha256/' + config_digest[7:], config),
                           ('blobs/sha256/' + manifest_digest[7:], manifest)]:
            record = tarfile.TarInfo(name); record.size = len(data); record.mode = 0o644
            archive.addfile(record, io.BytesIO(data))
    return manifest_digest, config_digest


def test_oci_archive_binds_arm64_manifest_and_config(tmp_path):
    archive = tmp_path / 'api.oci.tar'
    expected = oci_archive(archive)
    assert bootstrap.oci_identity(archive) == expected
    bad = tmp_path / 'amd64.oci.tar'; oci_archive(bad, architecture='amd64')
    with pytest.raises(ValueError):
        bootstrap.oci_identity(bad)


def test_image_builder_uses_dedicated_local_renewal_target():
    raw = (ROOT / 'deployment/manifests/personal/control-bootstrap.py').read_text()
    assert "'--target=local-renewal'" in raw
    assert "'--target=api'" not in raw
    assert "'--provenance=false'" in raw
    dockerfile = (ROOT / 'apps/api/Dockerfile').read_text()
    target = dockerfile.split(' AS local-renewal', 1)[1].split('\nFROM ', 1)[0]
    assert 'test "$TARGETARCH" = arm64' in target
    assert 'COPY gitops/credentials.py gitops/argo.py gitops/handoff.py /app/gitops/' in target
    assert 'USER 1000:1000' in target
    assert 'ENTRYPOINT ["python3", "/app/gitops/credentials.py"]' in target
    assert "'/bin/ctr', '-n', 'k8s.io', 'images', 'import'" in raw
    assert "'/bin/crictl', 'images', '-o', 'json'" in raw
    assert "'/bin/mkdir', '-p', image_directory" in raw


def test_source_digest_includes_untracked_worktree_bytes(monkeypatch, tmp_path):
    first = tmp_path / 'tracked.py'; second = tmp_path / 'new.yaml'
    first.write_text('one'); second.write_text('two')
    monkeypatch.setattr(bootstrap, 'run', lambda *a, **kw: b'tracked.py\0new.yaml\0')
    before = bootstrap.source_digest(tmp_path)
    second.write_text('changed')
    assert bootstrap.source_digest(tmp_path) != before


def test_registration_role_allows_only_exact_dynamic_names():
    bootstrap.validate_registration_role({
        'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role', 'rules': None})
    bootstrap.validate_registration_role({
        'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role'})
    role = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role', 'rules': [{
        'apiGroups': ['argoproj.io'], 'resources': ['applications'],
        'resourceNames': ['personal-123'], 'verbs': ['get', 'patch']}]}
    bootstrap.validate_registration_role(role)
    role['rules'][0]['verbs'] = ['*']
    with pytest.raises(ValueError):
        bootstrap.validate_registration_role(role)
    role['rules'] = ''
    with pytest.raises(ValueError):
        bootstrap.validate_registration_role(role)


def test_credentials_renderer_import_resolves_sibling_argo_module():
    module = bootstrap.credentials_module()
    assert module.validate_policy({'version': 1, 'targets': []}) == {'version': 1, 'targets': []}


def test_job_verification_uses_declared_digest_and_image_id_not_normalized_status_tag(monkeypatch):
    digest = 'sha256:' + '5' * 64
    image = 'localhost/railshot-api@' + digest
    proof = {'manifest_digest': digest}
    pod = {'metadata': {'uid': 'pod-uid'}, 'spec': {'nodeName': 'node-1',
        'containers': [{'image': image}]}, 'status': {'containerStatuses': [{
            'image': 'localhost/railshot-api:local', 'imageID': image,
            'state': {'terminated': {'exitCode': 0}}}]}}
    job = {'metadata': {'uid': 'job-uid'}}
    node = {'metadata': {'uid': 'node-uid', 'labels': {
        'kubernetes.io/arch': 'arm64', 'railshot.io/node-role': 'platform'}}}
    monkeypatch.setattr(bootstrap, 'get', lambda *args: job)
    monkeypatch.setattr(bootstrap, 'run', lambda *args, **kwargs: '')
    monkeypatch.setattr(bootstrap, 'kube', lambda *args, **kwargs:
                        {'items': [pod]} if args[:3] == ('-n', 'argocd', 'get') else node)
    assert bootstrap.verify_local_job(proof, image)['pod_image_id'] == image
    pod['spec']['containers'][0]['image'] = 'localhost/railshot-api:local'
    with pytest.raises(ValueError):
        bootstrap.verify_local_job(proof, image)


def test_local_compose_is_pinned_native_and_never_publishes_k3s_api(monkeypatch):
    raw = (ROOT / 'deployment/manifests/personal/control.compose.yaml').read_text()
    assert 'rancher/k3s@sha256:5d52389a0f4fd7ebdb5a1fb2d7c67c35da966230782c4abb0667d86bcccea9c2' in raw
    assert 'platform: linux/arm64' in raw
    assert 'network_mode: "container:${RAILSHOT_PERSONAL_GATEWAY_CONTAINER:-railshot-personal-local}"' in raw
    assert '--cluster-cidr=10.52.0.0/16' in raw and '--service-cidr=10.53.0.0/16' in raw
    assert '6443:' not in raw and '/var/run/docker.sock' not in raw
    assert '--guard-interval' in raw and 'control-auth:/var/lib/railshot-control-auth' in raw
    assert 'test: [CMD, kubectl,' in raw
    assert 'railshot-personal-live-wireguard}' in raw and 'railshot-personal-live-api}' in raw


def test_bootstrap_requires_restore_and_forwarding_readback():
    raw = (ROOT / 'deployment/manifests/personal/control-bootstrap.py').read_text()
    assert "'--restore'" in raw and raw.count("'--verify-forwarding'") == 2
    assert 'LOCAL_CONTROL_GATEWAY_GUARD_FAILED' in raw
    dockerfile = (ROOT / 'deployment/manifests/personal/Dockerfile').read_text()
    assert 'control-bootstrap.py deployment/manifests/personal/control-token-minter.py' in dockerfile
    assert 'COPY gitops/argo/ gitops/argo/' in dockerfile
    assert 'COPY deployment/manifests/runtime-registration-access.yaml' in dockerfile


def test_compose_document_has_only_control_bootstrap_and_minter_services():
    document = yaml.safe_load((ROOT / 'deployment/manifests/personal/control.compose.yaml').read_text())
    assert set(document['services']) == {'control', 'bootstrap', 'token-minter'}
    assert document['services']['bootstrap']['restart'] == 'no'
    assert document['services']['token-minter']['restart'] == 'unless-stopped'
