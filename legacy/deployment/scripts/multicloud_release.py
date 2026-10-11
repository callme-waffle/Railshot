#!/usr/bin/env python3
"""One approved release, three registered runtimes, and one aggregate receipt."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'ci/scripts'))
from storage import durable_write
PROVIDERS = {'aws', 'gcp', 'openstack'}
OPERATOR_HOME = '/home/railshot-operator'
OPERATOR_STATE = OPERATOR_HOME + '/.local/share/railshot/'
COMPONENTS = {'dashboard', 'api', 'ci-runner'}
KUBE = ['/usr/local/bin/k3s', 'kubectl', '--request-timeout=30s']


def require(condition, code):
    if not condition:
        raise ValueError(code)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def private(path):
    path = Path(path)
    st = path.lstat()
    require(path.is_absolute() and path.resolve() == path and path.is_file()
            and st.st_uid == os.geteuid() and not st.st_mode & 0o077 and st.st_size <= 2_000_000,
            'PRIVATE_RELEASE_FILE_REQUIRED')
    return json.loads(path.read_bytes())


def save(path, value):
    durable_write(path, encoded(value) + b'\n')


def module(name):
    spec = importlib.util.spec_from_file_location('release_' + name, ROOT / 'deployment/scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def validate(manifest, config, scope='multicloud'):
    require(scope in {'ci-runtime', 'multicloud'}, 'RELEASE_SCOPE_INVALID')
    core = {'version', 'source_sha', 'platform_revision', 'images'}
    require(set(manifest) == core | (set() if scope == 'ci-runtime' else {'runtime_policy', 'provider_targets', 'edge_modules', 'edge_kinds'})
            and manifest['version'] == 1, 'RELEASE_MANIFEST_INVALID')
    for key in ('source_sha', 'platform_revision'):
        require(isinstance(manifest[key], str) and re.fullmatch('[a-f0-9]{40}', manifest[key]), 'RELEASE_SHA_INVALID')
    require(set(manifest['images']) == COMPONENTS, 'ALL_PLATFORM_IMAGES_REQUIRED')
    for name, image in manifest['images'].items():
        require(isinstance(image, str) and re.fullmatch(r'ghcr\.io/jasmin-softbank/railshot-' + name + r'@sha256:[a-f0-9]{64}', image),
                'IMMUTABLE_PLATFORM_IMAGES_REQUIRED')
    if scope == 'ci-runtime':
        require(config.get('version') == 1 and {'version', 'state_dir', 'workers', 'apps'} <= set(config), 'RELEASE_CONFIG_INVALID')
        return []  # CI promotion requires no provider inventory, edge state or node policy.
    require(set(manifest['edge_kinds']) == PROVIDERS, 'EDGE_KINDS_INVALID')
    require(set(manifest['edge_modules']) == PROVIDERS, 'EDGE_MODULE_PINS_REQUIRED')
    require(set(manifest['provider_targets']) == PROVIDERS and
            len(set(manifest['provider_targets'].values())) == 3 and
            all(isinstance(v, str) and re.fullmatch('[a-z][a-z0-9-]{0,62}', v) for v in manifest['provider_targets'].values()),
            'THREE_DISTINCT_PROVIDER_TARGETS_REQUIRED')
    require(config.get('version') == 1 and set(config) == {'version', 'state_dir', 'workers', 'apps', 'targets', 'operator_kubeconfig', 'gcp_credentials_file'}, 'RELEASE_CONFIG_INVALID')
    for key in ('operator_kubeconfig', 'gcp_credentials_file'):
        require(isinstance(config[key], str) and config[key].startswith(OPERATOR_STATE)
                and '..' not in Path(config[key]).parts, 'OPERATOR_CONFIG_PATH_REQUIRED')
    targets = config['targets']
    require(isinstance(targets, list) and len(targets) == 3 and {t['provider'] for t in targets} == PROVIDERS,
            'THREE_PROVIDER_INVENTORY_REQUIRED')
    common = {'provider', 'target_id', 'registry_file', 'config_file', 'registration_state', 'from_policy_file'}
    for target in targets:
        node_only = target.get('scope') == 'node-only'
        required = common | ({'scope'} if node_only else {'edge_config_file'})
        optional = {'upgrade'} | (set() if node_only else {'binding_file'})
        require(required <= set(target) <= required | optional, 'TARGET_CONFIG_INVALID')
        kind, pin = manifest['edge_kinds'][target['provider']], manifest['edge_modules'][target['provider']]
        if node_only:
            require(kind == 'none' and pin is None, 'NODE_ONLY_EDGE_BINDING_INVALID')
        else:
            require(kind in ({'native', 'aws-relay'} if target['provider'] == 'openstack' else {'native'}), 'EDGE_KINDS_INVALID')
            require(isinstance(pin, str) and re.fullmatch('[a-f0-9]{64}', pin), 'EDGE_MODULE_PINS_REQUIRED')
        require(manifest['provider_targets'][target['provider']] == target['target_id'], 'TARGET_BINDING_MISMATCH')
        for key in required - {'provider', 'target_id', 'scope'} | ({'binding_file'} if 'binding_file' in target else set()):
            value = target[key]
            require(isinstance(value, str) and value.startswith(OPERATOR_STATE) and '..' not in Path(value).parts,
                    'REGISTERED_RUNTIME_PATH_REQUIRED')
    require(len({target['from_policy_file'] for target in targets}) == 3
            and len({target['registration_state'] for target in targets}) == 3, 'TARGET_STATE_MUST_BE_DISTINCT')
    require(manifest['runtime_policy'] == json.loads((ROOT / 'deployment/airgap/versions.json').read_bytes()),
            'RUNTIME_POLICY_SOURCE_MISMATCH')
    return targets


def native(args, *, document=None, timeout=90):
    result = subprocess.run(args, input=None if document is None else encoded(document), capture_output=True, timeout=timeout)
    require(result.returncode == 0, 'RELEASE_COMMAND_FAILED')
    return json.loads(result.stdout) if result.stdout.strip() else None


# Exact checked-out source runs as the existing operator, with bounded native tool paths.

TARGET_PROGRAM = '''import hashlib,json,os,subprocess,sys
from pathlib import Path
payload=json.load(sys.stdin); target=payload['target']; release=payload['release']; source=Path(payload['source_root'])
sys.path.insert(0,str(source/'ci/scripts'))
from storage import durable_write
verify_only=payload.get('verify_only',False); reconcile=payload.get('reconcile',False)
assert not (verify_only and reconcile), 'one operation required'
root=Path('/home/railshot-operator/.local/share/railshot/multicloud-releases')/release['source_sha']/target['target_id']
def read(path):
 p=Path(path); s=p.lstat()
 assert p.resolve()==p and p.is_file() and s.st_uid==os.geteuid() and not s.st_mode&0o077 and s.st_size<2000000
 return json.loads(p.read_bytes())
def write(path,value):
 durable_write(path,json.dumps(value,sort_keys=True,separators=(',',':')).encode())
def hash(value): return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
runtime_config=read(target['config_file']); node_only=target.get('scope')=='node-only'
assert (runtime_config.get('version')==2 and runtime_config.get('scope')=='node-only')==node_only, 'runtime scope differs'
assert target.get('scope') in (None,'node-only'), 'target scope differs'
assert not reconcile or node_only, 'reconciliation requires node-only scope'
if node_only:
 assert 'edge_config_file' not in target and 'binding_file' not in target and release['edge_kinds'][target['provider']]=='none' and release['edge_modules'][target['provider']] is None, 'node-only edge binding differs'
if not (verify_only or reconcile): root.mkdir(parents=True,exist_ok=True,mode=0o700)
before=read(target['from_policy_file']); after=release['runtime_policy']
plan={'version':1,'source_sha':release['source_sha'],'from_policy':before,'to_policy':after,'from_policy_sha256':hash(before),'to_policy_sha256':hash(after)}
if 'upgrade' in target: plan['upgrade']=target['upgrade']
plan_path=root/'release.json'
if verify_only or reconcile:
 plan=read(plan_path)
 assert plan['source_sha']==release['source_sha'] and plan['to_policy']==after and before==after
elif plan_path.exists(): assert read(plan_path)==plan, 'release binding changed'
else: write(plan_path,plan)
command=[sys.executable,str(source/'deployment/scripts/runtime-update.py'),'--registry',target['registry_file'],'--target-id',target['target_id'],'--config',target['config_file'],'--registration-dir',target['registration_state'],'--state-dir',str(root/'runtime'),'--release',str(plan_path)]
if 'binding_file' in target: command+=['--binding',target['binding_file']]
if verify_only: command+=['--verify-only']
if reconcile: command+=['--reconcile']
if node_only: edge_proof={'status':'not_applicable','reason':'node-only'}
else:
 edge_config=read(target['edge_config_file'])
 assert edge_config.get('edge_kind','native')==release['edge_kinds'][target['provider']]
 edge=[sys.executable,str(source/'deployment/scripts/edge_update.py'),'--source-root',str(source),'--release-sha',release['source_sha'],'--config',target['edge_config_file'],'--module-sha256',release['edge_modules'][target['provider']]]
 if verify_only: edge+=['--verify-only']
 edge_run=subprocess.run(edge,capture_output=True,text=True,timeout=1800)
 try: edge_proof=json.loads(edge_run.stdout)
 except (ValueError,TypeError): edge_proof={'phase':'unknown'}
 if edge_run.returncode!=0 or edge_proof.get('phase')!='succeeded' or edge_proof.get('provider')!=target['provider'] or edge_proof.get('release_sha')!=release['source_sha']:
  print(json.dumps({'status':'failed','code':'EDGE_UPDATE_NOT_VERIFIED','provider':target['provider'],'target_id':target['target_id'],'source_sha':release['source_sha']})); sys.exit(1)
run=subprocess.run(command,capture_output=True,text=True,timeout=1800,env={**os.environ,'RAILSHOT_RELEASE_API_IMAGE':payload.get('api_image',release['images']['api'])})
try: proof=json.loads(run.stdout)
except (ValueError,TypeError): proof={'status':'failed','code':'RUNTIME_RECEIPT_UNAVAILABLE'}
if not isinstance(proof,dict) or any(proof.get(key)!=value for key,value in {'provider':target['provider'],'target_id':target['target_id'],'source_sha':release['source_sha'],'scope':target.get('scope')}.items()):
 proof={'status':'failed','code':'RUNTIME_RECEIPT_BINDING_MISMATCH','provider':target['provider'],'target_id':target['target_id'],'source_sha':release['source_sha']}
proof.update(release_sha=release['source_sha'],edge=edge_proof)
expected_status='reconciled' if reconcile else 'verified'
if run.returncode!=0 or proof.get('status')!=expected_status: proof['status']='failed'
if proof['status']=='verified' and not verify_only:
 current=read(target['from_policy_file'])
 assert current==before, 'runtime baseline changed during release'
 write(target['from_policy_file'],after)
print(json.dumps(proof)); sys.exit(0 if proof['status']==expected_status else 1)
'''


def update_target(target, manifest, config, verify_only=False, *, reconcile=False, api_image=None):
    require(not reconcile or (not verify_only and target.get('scope') == 'node-only' and isinstance(api_image, str)
            and re.fullmatch(r'ghcr\.io/jasmin-softbank/railshot-api@sha256:[a-f0-9]{64}', api_image)),
            'RECONCILIATION_API_BINDING_REQUIRED')
    # Operator-owned SSH, kubeconfig and WIF bindings stay on the existing control host.
    command = ['sudo', '-n', '-u', 'railshot-operator', '--', 'env', '-i',
        'HOME=' + OPERATOR_HOME, 'USER=railshot-operator', 'LOGNAME=railshot-operator',
        'PATH=/opt/railshot-release/bin:/usr/local/bin:/usr/bin:/bin',
        'KUBECONFIG=' + config['operator_kubeconfig'],
        'GOOGLE_APPLICATION_CREDENTIALS=' + config['gcp_credentials_file'],
        'CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE=' + config['gcp_credentials_file'],
        'AWS_REGION=ap-northeast-2', 'AWS_DEFAULT_REGION=ap-northeast-2',
        'AWS_PAGER=', 'PYTHONDONTWRITEBYTECODE=1', 'CHECKPOINT_DISABLE=1',
        'python3', '-c', TARGET_PROGRAM]
    result = subprocess.run(command, input=encoded({'target': target, 'release': manifest,
        'verify_only': verify_only, 'source_root': str(ROOT),
        **({'reconcile': True, 'api_image': api_image} if reconcile else {})}), capture_output=True, timeout=3700)

    try:
        proof = json.loads(result.stdout)
    except (ValueError, TypeError):
        proof = {'status': 'unknown', 'code': 'TARGET_RECEIPT_UNAVAILABLE'}
    require(isinstance(proof, dict) and proof.get('provider') == target['provider']
            and proof.get('target_id') == target['target_id']
            and proof.get('scope') == target.get('scope')
            and proof.get('source_sha') == manifest['source_sha'], 'TARGET_RECEIPT_BINDING_MISMATCH')
    if result.returncode and proof.get('status') in {'verified', 'reconciled'}:
        proof['status'] = 'unknown'
    return {**proof, 'provider': target['provider'], 'target_id': target['target_id'], 'source_sha': manifest['source_sha']}


def reconciled_targets(checked, previous):
    original = {(row.get('provider'), row.get('target_id')): row for row in previous.get('targets', [])}
    return (len(checked) == len(original) == 3 and {row.get('provider') for row in checked} == PROVIDERS
            and {(row.get('provider'), row.get('target_id')) for row in checked} == set(original)
            and all(row.get('status') == 'reconciled' and row.get('scope') == 'node-only'
                    and row.get('source_sha') == previous['source_sha']
                    and row.get('input_sha256') == original[row['provider'], row['target_id']]['input_sha256']
                    and row.get('to_policy_sha256') == original[row['provider'], row['target_id']]['to_policy_sha256']
                    and row.get('after', {}).get('node_uid') == original[row['provider'], row['target_id']]['runtime']['after']['node_uid']
                    for row in checked))


def reconciled_failure(state, previous):
    """Accept only the explicit acknowledgement bound to the unchanged failed receipt."""
    path = state / 'reconciliation.json'
    if not path.exists():
        return False
    proof = private(path)
    return (proof.get('status') == 'reconciled' and previous.get('status') == 'incomplete'
            and proof.get('source_sha') == previous.get('source_sha')
            and proof.get('input_sha256') == previous.get('input_sha256')
            and proof.get('failed_receipt_sha256') == hashlib.sha256((state / 'receipt.json').read_bytes()).hexdigest()
            and reconciled_targets(proof.get('targets', []), previous))


def reconcile(config, manifest, *, workers=None, target_reconciler=None, platform_verify=None):
    """Operator-only recovery acknowledgement; never retry or relabel a failed release."""
    targets = validate(manifest, config)
    # ponytail: only verified no-op node releases; changed runtime/edge recovery needs its own reviewed procedure.
    require(all(target.get('scope') == 'node-only' for target in targets), 'NODE_ONLY_RECONCILIATION_REQUIRED')
    home = Path(config['state_dir'])
    require(home.is_absolute() and home.resolve() == home and home.stat().st_uid == os.geteuid()
            and not home.stat().st_mode & 0o077, 'PRIVATE_RELEASE_STATE_REQUIRED')
    with os.fdopen(os.open(home / 'release.lock', os.O_RDONLY | os.O_NOFOLLOW), 'r') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = home / manifest['source_sha']; previous = private(state / 'receipt.json')
        identity = digest({'manifest': manifest, 'config': config})
        require(previous.get('status') == 'incomplete' and previous.get('stage') == 'targets'
                and previous.get('source_sha') == manifest['source_sha'] and previous.get('input_sha256') == identity
                and private(state / 'manifest.json') == manifest
                and private(home / 'last-attempt.json') == {'source_sha': manifest['source_sha'], 'input_sha256': identity},
                'FAILED_RELEASE_BINDING_DIFFERS')
        failed_hash = hashlib.sha256((state / 'receipt.json').read_bytes()).hexdigest()
        original = {row['target_id']: row for row in previous['targets']}
        require(len(original) == 3 and set(original) == {row['target_id'] for row in targets}
                and all(row.get('scope') == 'node-only' and row.get('runtime', {}).get('status') == 'verified'
                        and row['runtime'].get('changed') is False
                        and row.get('from_policy_sha256') == row.get('to_policy_sha256') == digest(manifest['runtime_policy'])
                        for row in original.values()), 'VERIFIED_NOOP_RUNTIME_REQUIRED')
        worker_module = workers or module('platform_workers')
        verification = platform_verify or module('verify-platform').local
        current = private(home / 'ci-runtime' / 'current.json')
        require(current.get('status') == 'verified' and re.fullmatch('[a-f0-9]{40}', current.get('source_sha', '')),
                'CURRENT_CI_RELEASE_REQUIRED')
        ci_state = home / 'ci-runtime' / current['source_sha']; ci_manifest = private(ci_state / 'manifest.json')
        validate(ci_manifest, config, 'ci-runtime')
        ci_config = {key: config[key] for key in ('version', 'state_dir', 'workers', 'apps')}
        ci_config['workers'] = worker_module.scoped_config(ci_config['workers'], 'ci')
        require(ci_manifest['source_sha'] == current['source_sha'] and private(ci_state / 'receipt.json') == current
                and current['input_sha256'] == digest({'manifest': ci_manifest, 'config': ci_config}), 'CURRENT_CI_RELEASE_DIFFERS')
        platform = verification(ci_manifest['platform_revision'], {k: ci_manifest['images'][k] for k in ('dashboard', 'api')})
        # The failed full release owns credential renewal; the current CI receipt owns build replenishment.
        require(private(ci_state / 'workers.json')['config'] == ci_config['workers'], 'CI_WORKER_BINDING_DIFFERS')
        require(private(state / 'workers.json')['config'] == worker_module.scoped_config(config['workers'], 'credentials'),
                'CREDENTIAL_WORKER_BINDING_DIFFERS')
        checked_workers = [worker_module.verify_workers(path) for path in (ci_state / 'workers.json', state / 'workers.json')]
        require(all(row.get('status') == 'verified' and row.get('executable_verification') is True
                    and row.get('source_sha') == expected['source_sha'] and row.get('images') == expected['images']
                    for row, expected in zip(checked_workers, (ci_manifest, manifest))),
                'WORKER_EXECUTION_NOT_VERIFIED')
        apps = worker_module.verify_apps(ci_state / 'apps.json')
        require(apps.get('status') == 'verified' and apps.get('source_sha') == apps.get('platform_ref') == current['source_sha'],
                'CURRENT_APPS_RELEASE_DIFFERS')
        if target_reconciler is None:
            target_reconciler = lambda target, release: update_target(target, release, config,
                reconcile=True, api_image=ci_manifest['images']['api'])
        checked = [target_reconciler(target, manifest) for target in targets]
        require(reconciled_targets(checked, previous), 'THREE_NODE_RECONCILIATION_NOT_VERIFIED')
        verification(ci_manifest['platform_revision'], {k: ci_manifest['images'][k] for k in ('dashboard', 'api')})
        require(private(home / 'ci-runtime' / 'current.json') == current
                and hashlib.sha256((state / 'receipt.json').read_bytes()).hexdigest() == failed_hash,
                'RECONCILIATION_CONCURRENT_CHANGE')
        proof = {'version': 1, 'status': 'reconciled', 'source_sha': manifest['source_sha'], 'input_sha256': identity,
                 'failed_receipt_sha256': failed_hash, 'current_ci_source_sha': current['source_sha'],
                 'platform': platform, 'workers': checked_workers, 'apps': apps, 'targets': checked,
                 'checked_at': datetime.now(timezone.utc).isoformat()}
        if (state / 'reconciliation.json').exists():
            require(reconciled_failure(state, previous), 'RECONCILIATION_RECORD_DIFFERS')
            return proof  # Rechecking is read-only; retain the original acknowledgement.
        save(state / 'reconciliation.json', proof)
        return proof


def execute(config, manifest, *, scope='multicloud', workers=None, target_runner=None, target_verifier=None, platform_verify=None, promote=None):
    targets = validate(manifest, config, scope)
    home = Path(config['state_dir'])
    require(home.is_absolute() and home.resolve() == home, 'RELEASE_STATE_PATH_INVALID')
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(home.stat().st_uid == os.geteuid() and not home.stat().st_mode & 0o077, 'PRIVATE_RELEASE_STATE_REQUIRED')
    with (home / 'release.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ci_state = home / 'ci-runtime' / manifest['source_sha']
        worker_module = workers or module('platform_workers')
        if scope == 'ci-runtime':
            home = home / 'ci-runtime'; home.mkdir(mode=0o700, exist_ok=True)
            # Node configuration changes must not invalidate a completed CI rollout.
            config = {key: config[key] for key in ('version', 'state_dir', 'workers', 'apps')}
            config['workers'] = worker_module.scoped_config(config['workers'], 'ci')
        state = home / manifest['source_sha']; state.mkdir(mode=0o700, exist_ok=True)
        receipt_path = state / 'receipt.json'
        identity = digest({'manifest': manifest, 'config': config})
        verification = platform_verify or module('verify-platform').local
        reuse_ci = scope == 'multicloud' and (ci_state / 'receipt.json').exists()
        if reuse_ci:
            ci_receipt = private(ci_state / 'receipt.json')
            ci_manifest = {key: manifest[key] for key in ('version', 'source_sha', 'platform_revision', 'images')}
            ci_config = {key: config[key] for key in ('version', 'state_dir', 'workers', 'apps')}
            ci_config['workers'] = worker_module.scoped_config(ci_config['workers'], 'ci')
            require(ci_receipt.get('status') == 'verified' and ci_receipt.get('input_sha256') ==
                    digest({'manifest': ci_manifest, 'config': ci_config}), 'CI_RELEASE_NOT_VERIFIED')
            ci_proof = worker_module.verify_workers(ci_state / 'workers.json')
            require(ci_proof.get('status') == 'verified' and ci_proof.get('executable_verification') is True,
                    'CI_RELEASE_NOT_VERIFIED')
        worker_state = state / 'workers.json'
        apps_state = (ci_state if reuse_ci else state) / 'apps.json'
        if receipt_path.exists():
            previous = private(receipt_path)
            require(previous['input_sha256'] == identity, 'RELEASE_INPUT_CHANGED')
            # Never replay an uncertain mutation after a host/workflow interruption.
            require(previous['status'] == 'verified', 'RELEASE_RECONCILIATION_REQUIRED')
            current = private(home / 'current.json')
            last_attempt = private(home / 'last-attempt.json')
            require(current['input_sha256'] == identity and last_attempt['input_sha256'] == identity,
                    'RELEASE_SUPERSEDED_RECONCILIATION_REQUIRED')
            verification(manifest['platform_revision'], {k: manifest['images'][k] for k in ('dashboard', 'api')})
            worker_proof = worker_module.verify_workers(worker_state)
            require(worker_proof.get('status') == 'verified' and worker_proof.get('executable_verification') is True, 'WORKER_EXECUTION_NOT_VERIFIED')
            if target_verifier is None:
                target_verifier = lambda target, release: update_target(target, release, config, True)
            with ThreadPoolExecutor(max_workers=3) as pool:
                checked = list(pool.map(lambda target: target_verifier(target, manifest), targets))
            require(all(proof.get('status') == 'verified' and proof.get('source_sha') == manifest['source_sha']
                        and proof.get('provider') == target['provider'] and proof.get('target_id') == target['target_id']
                        and proof.get('scope') == target.get('scope')
                        for target, proof in zip(targets, checked)), 'RELEASE_LIVE_READBACK_FAILED')
            worker_module.verify_apps(apps_state)
            return {**previous, 'targets': checked, 'reverified_at': datetime.now(timezone.utc).isoformat()}
        if (home / 'last-attempt.json').exists():
            last = private(home / 'last-attempt.json')
            require(isinstance(last.get('source_sha'), str) and re.fullmatch('[a-f0-9]{40}', last['source_sha']),
                    'PRIOR_RELEASE_RECONCILIATION_REQUIRED')
            prior = private(home / last['source_sha'] / 'receipt.json')
            require(prior.get('input_sha256') == last.get('input_sha256') and
                    ((prior.get('status') == 'verified' and private(home / 'current.json').get('input_sha256') == last.get('input_sha256'))
                     or (scope == 'multicloud' and reconciled_failure(home / last['source_sha'], prior))),
                    'PRIOR_RELEASE_RECONCILIATION_REQUIRED')
        receipt = {'version': 1, 'scope': scope, 'source_sha': manifest['source_sha'], 'input_sha256': identity,
                   'status': 'running', 'stage': 'preflight', 'targets': [], 'started_at': datetime.now(timezone.utc).isoformat()}
        save(state / 'manifest.json', manifest); save(receipt_path, receipt)
        save(home / 'last-attempt.json', {'source_sha': manifest['source_sha'], 'input_sha256': identity})
        try:
            receipt['platform'] = verification(manifest['platform_revision'], {k: manifest['images'][k] for k in ('dashboard', 'api')})
            receipt['stage'] = 'workers'; save(receipt_path, receipt)
            promotion = promote or worker_module.promote_apps
            def before_resume(prepared):
                receipt['stage'] = 'promotion'; save(receipt_path, receipt)
                receipt['apps'] = promotion(config['apps'], manifest['source_sha'],
                    {**receipt, 'status': 'prepared', 'workers': prepared}, apps_state)
                receipt['stage'] = 'worker-verification'; save(receipt_path, receipt)
            # CI owns only build replenishment; provider credential renewal remains in the full release.
            receipt['workers'] = worker_module.apply_workers(config['workers'], manifest['images'], manifest['source_sha'], worker_state,
                **({'before_resume': before_resume, 'scope': 'ci'} if scope == 'ci-runtime' else
                   {'scope': 'credentials'} if reuse_ci else {}))
            require(receipt['workers'].get('status') == 'verified' and receipt['workers'].get('executable_verification') is True, 'WORKER_EXECUTION_NOT_VERIFIED')
            receipt['stage'] = 'targets'; save(receipt_path, receipt)
            if target_runner is None:
                target_runner = lambda target, release: update_target(target, release, config)
            # ponytail: three providers in one bounded pool; use a queue only when the inventory grows beyond this contract.
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(target_runner, target, manifest): target for target in targets}
                for future in as_completed(futures):
                    target = futures[future]
                    try:
                        proof = future.result()
                        require(isinstance(proof, dict) and proof.get('provider') == target['provider'] and proof.get('target_id') == target['target_id']
                                and proof.get('scope') == target.get('scope')
                                and proof.get('source_sha') == manifest['source_sha'], 'TARGET_PROOF_MISMATCH')
                    except Exception:
                        proof = {'provider': target['provider'], 'target_id': target['target_id'], 'source_sha': manifest['source_sha'],
                                 'status': 'unknown', 'code': 'TARGET_READBACK_REQUIRED'}
                    receipt['targets'].append(proof); save(receipt_path, receipt)
            receipt['targets'].sort(key=lambda row: row['provider'])
            require(len(receipt['targets']) == len(targets) and all(t.get('status') == 'verified' for t in receipt['targets']), 'THREE_PROVIDER_VERIFICATION_FAILED')
            receipt['stage'] = 'promotion'; save(receipt_path, receipt)
            verified = {**receipt, 'status': 'verified'}
            if scope != 'ci-runtime':
                receipt['apps'] = (worker_module.verify_apps(apps_state) if reuse_ci else
                    promotion(config['apps'], manifest['source_sha'], verified, apps_state))
            receipt.update(status='verified', stage='complete', completed_at=datetime.now(timezone.utc).isoformat())
            save(receipt_path, receipt); save(home / 'current.json', receipt)
        except Exception as error:
            receipt.update(status='incomplete', code=str(error) if isinstance(error, ValueError) and re.fullmatch('[A-Z_]+', str(error)) else 'RELEASE_READBACK_REQUIRED')
            save(receipt_path, receipt)
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--reconcile', action='store_true', help='verify repaired no-op nodes and acknowledge the failed release without replaying it')
    args = parser.parse_args()
    try:
        result = (reconcile if args.reconcile else execute)(private(args.config), private(args.manifest))
    except Exception as error:
        result = {'status': 'blocked', 'code': str(error) if isinstance(error, ValueError) and re.fullmatch('[A-Z_]+', str(error)) else 'RELEASE_PREFLIGHT_FAILED'}
    print(json.dumps(result))
    return 0 if result['status'] == ('reconciled' if args.reconcile else 'verified') else 1


if __name__ == '__main__':
    sys.exit(main())
