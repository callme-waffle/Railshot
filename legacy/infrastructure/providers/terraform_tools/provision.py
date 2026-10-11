#!/usr/bin/env python3
"""Administrator Terraform plan/apply CLI. No cloud SDK, auto-apply, or product Allow service."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from contextlib import closing
from datetime import datetime, timezone

import costs
from capabilities import capabilities, validate_profile, validate_maintenance, timestamp

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / 'ci/scripts'))
from observability import OperationError, event_record
from storage import durable_write

MODULES = {name: REPO / 'infrastructure' / 'terraform' / name for name in ('aws', 'gcp', 'azure')}


def digest(value):
    return hashlib.sha256(value).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def write_private(path, value):
    durable_write(path, value)


def sources(module):
    files = {p.name: p.read_bytes() for p in module.iterdir()
             if p.is_file() and (p.suffix in {'.tf', '.tftpl'} or p.name == '.terraform.lock.hcl')}
    if '.terraform.lock.hcl' not in files or not any(name.endswith('.tf') for name in files):
        raise ValueError('module requires Terraform sources and a reviewed provider lock')
    return files


def source_hash(files):
    return digest(encoded({name: digest(content) for name, content in files.items()}))


def executor_hash():
    paths = [Path(__file__), Path(__file__).with_name('capabilities.py'), Path(__file__).with_name('costs.py'),
             REPO / 'ci/scripts/storage.py', REPO / 'ci/scripts/observability.py']
    return source_hash({str(p.relative_to(REPO)): p.read_bytes() for p in paths})


def command(args, work, log):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('TF_CLI_ARGS', 'TF_VAR_')) and k not in {'TF_WORKSPACE', 'TF_DATA_DIR'}}
    env.update(TF_IN_AUTOMATION='1', TF_INPUT='0', TF_DATA_DIR=str(work.parent / '.terraform'))
    try:
        result = subprocess.run(['terraform', *args], cwd=work, env=env, capture_output=True, text=True)
    except OSError as exc:
        raise OperationError('STEP_START_FAILED', component='infra', phase='terraform.' + args[0],
                             retry_policy='after_configuration', cause=exc) from exc
    try:
        with log.open('a') as stream:
            stream.write(result.stdout + result.stderr)
        log.chmod(0o600)
    except OSError as exc:
        apply_started = args[0] == 'apply'
        raise OperationError('OBSERVATION_WRITE_FAILED', component='infra', phase='terraform.' + args[0] + '.evidence',
                             outcome='UNKNOWN' if apply_started else 'BLOCKED',
                             side_effect='possible' if apply_started else 'none',
                             retry_policy='after_reconcile' if apply_started else 'after_configuration', cause=exc) from exc
    if result.returncode:
        cause = subprocess.CalledProcessError(result.returncode, 'terraform')
        raise OperationError('INFRA_EXECUTION_FAILED', component='infra', phase='terraform.' + args[0],
                             outcome='FAIL', retry_policy='after_reconcile',
                             side_effect='possible' if args[0] == 'apply' else 'none', cause=cause) from cause
    return result.stdout


def review(plan):
    changes = []
    for resource in plan.get('resource_changes', []):
        actions = resource['change']['actions']
        if actions == ['no-op']:
            continue
        risk = ('replace' if 'delete' in actions and 'create' in actions else
                'delete' if 'delete' in actions else 'forget' if 'forget' in actions else 'change')
        change = resource['change']
        before, after = change.get('before') or {}, change.get('after') or {}
        resized = any(key in before and key in after and before[key] != after[key]
                      for key in ('instance_type', 'machine_type', 'size', 'disk_size_gb'))
        changes.append({'address': resource['address'], 'actions': actions, 'risk': risk, 'resize': resized})
    return {'changes': changes, 'destructive': any(c['risk'] in {'delete', 'replace'} for c in changes),
            'maintenance_required': any(c['resize'] or c['risk'] in {'delete', 'replace', 'forget'} for c in changes),
            'product_allow': 'not_implemented', 'readiness': 'unverified'}


def reserve_budget(target, receipt):
    """Reserve before apply; missing/stale billing never authorizes a cloud call."""
    budget = target.get('budget')
    required = {'ledger_path', 'scope', 'currency', 'incremental_cost', 'limit', 'unreported_cost', 'quoted_at', 'expires_at'}
    try:
        if not isinstance(budget, dict) or set(budget) != required:
            raise ValueError('registered budget and reviewed quote required')
        now = datetime.now(timezone.utc)
        if not timestamp(budget['quoted_at']) <= now < timestamp(budget['expires_at']):
            raise ValueError('budget quote missing or expired')
        path = Path(budget['ledger_path'])
        if not path.is_absolute() or not path.is_file() or REPO == path.resolve() or REPO in path.resolve().parents:
            raise ValueError('existing private billing ledger required')
        provider = target['provider_kind']
        scope_field = {'aws': 'account_id', 'gcp': 'project_id', 'azure': 'subscription_id'}.get(provider)
        if scope_field is None:
            raise ValueError('billing provider unsupported')
        with closing(costs.ledger(path)) as db:
            report = costs.report(db, budget['scope'], now=now)
            source = target['variables'].get(scope_field)
            if (not source or report.get('provider') != provider
                    or str(report.get('source_scope', '')).casefold() != source.casefold()):
                raise ValueError('billing scope does not match registered provider account')
            return costs.reserve(db, scope=budget['scope'], operation_id=receipt['operation_id'],
                                 **{k: budget[k] for k in ('currency', 'incremental_cost', 'limit', 'unreported_cost')}, now=now)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as exc:
        raise OperationError('INFRA_BUDGET_BLOCKED', component='infra', phase='budget.reserve',
                             retry_policy='after_configuration', cause=exc) from exc


def execute(action, target_path, state_root, plan_sha256=None, *, maintenance=None):
    target = json.loads(Path(target_path).read_text())
    allowed = {'schema_version', 'provider_kind', 'target_id', 'execution_driver', 'owner_ref', 'variables', 'profile', 'budget'}
    if not isinstance(target, dict) or set(target) - allowed:
        raise ValueError('only trusted target identity and provider variables are accepted')
    provider, target_id = target.get('provider_kind'), target.get('target_id')
    if provider not in MODULES or not isinstance(target_id, str) or not re.fullmatch('[a-z][a-z0-9-]{2,39}', target_id):
        raise ValueError('unsupported provider or invalid registered target alias')
    if target.get('schema_version') != 'v1' or target.get('execution_driver', 'terraform') != 'terraform':
        raise ValueError('only v1 Terraform targets are supported')
    variables = target.get('variables')
    if not isinstance(variables, dict) or variables.get('target_id') != target_id:
        raise ValueError('provider variables required; target_id must match the target binding')
    if any(key.startswith('wireguard_') for key in variables):
        raise ValueError('WireGuard provisioning is retired; remove legacy variables and prepare provider-local ingress plus a verified management route')
    try:
        supported = validate_profile(target)
    except ValueError as exc:
        raise OperationError('INFRA_CAPABILITY_UNSUPPORTED', component='infra', phase='profile',
                             retry_policy='after_configuration', cause=exc) from exc
    root = Path(state_root).expanduser()
    if not root.is_absolute():
        raise ValueError('state root must be an explicit absolute path')
    root = root.resolve()
    if root == REPO or REPO in root.parents:
        raise ValueError('state root must be outside the repository')
    home = root / target_id
    if home.is_symlink():
        raise ValueError('target state directory cannot be a symlink')
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    home.chmod(0o700)
    state, work = home / 'terraform.tfstate', home / 'module'
    owner = 'terraform:local:' + str(state)
    if target.get('owner_ref', owner) != owner:
        raise ValueError('owner_ref must identify this exact state path')
    binding = {'provider_kind': provider, 'target_id': target_id, 'execution_driver': 'terraform', 'owner_ref': owner}
    if variables.get('owner_ref', owner) != owner:
        raise ValueError('provider owner_ref must match the exact executor state owner')
    variables = {**variables, 'owner_ref': owner}
    target = {**target, **binding, 'variables': variables}
    with (home / 'executor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        binding_file = home / 'binding.json'
        if binding_file.exists() and json.loads(binding_file.read_text()) != binding:
            raise ValueError('target state already belongs to a different binding')
        write_private(binding_file, encoded(binding))
        files = sources(MODULES[provider])
        plan, manifest, log = home / 'reviewed.tfplan', home / 'plan-manifest.json', home / 'terraform.log'
        if action == 'plan':
            if manifest.exists():
                prior = json.loads(manifest.read_text())
                if prior.get('apply_attempted') and prior.get('apply_status') != 'completed':
                    raise OperationError('STATE_INFLIGHT_UNCERTAIN', component='infra', phase='plan',
                                         outcome='UNKNOWN', side_effect='unknown', retry_policy='after_reconcile')
            # Dedicated disposable source copy; persistent state and receipts are siblings.
            if work.exists():
                if work.is_symlink():
                    raise ValueError('module workspace cannot be a symlink')
                shutil.rmtree(work)
            work.mkdir(mode=0o700)
            for name, content in files.items():
                write_private(work / name, content)
            write_private(work / 'inputs.tfvars.json', encoded(variables))
            # Explicit local backend keeps state outside checkout and separate per target.
            write_private(work / 'executor_backend.tf', b'terraform {\n  backend "local" {}\n}\n')
            manifest.unlink(missing_ok=True)
            command(['init', '-input=false', '-lockfile=readonly', '-reconfigure', '-backend-config=path=' + str(state)], work, log)
            version = json.loads(command(['version', '-json'], work, log))['terraform_version']
            command(['plan', '-input=false', '-lock-timeout=30s', '-var-file=inputs.tfvars.json', '-out=' + str(plan)], work, log)
            plan.chmod(0o600)
            raw = command(['show', '-json', str(plan)], work, log)
            write_private(home / 'plan.raw.json', raw.encode())
            result = review(json.loads(raw))
            receipt = {**binding, 'target_sha256': digest(encoded(target)), 'source_sha256': source_hash(files),
                       'workspace_sha256': source_hash(sources(work)), 'variables_sha256': digest(encoded(variables)),
                       'plan_sha256': digest(plan.read_bytes()), 'terraform_version': version, 'apply_attempted': False,
                       'operation_id': str(uuid.uuid4()), 'maintenance_required': result['maintenance_required'],
                       'executor_sha256': executor_hash(), 'python_version': sys.version}
            write_private(manifest, encoded(receipt))
            result.update(plan_sha256=receipt['plan_sha256'], owner_ref=owner, target_id=target_id,
                          operation_id=receipt['operation_id'], capabilities=supported,
                          budget='required_before_apply')
            write_private(home / 'review.json', encoded(result))
            return result
        if action != 'apply' or not plan_sha256 or not re.fullmatch('[0-9a-f]{64}', plan_sha256):
            raise ValueError('apply requires the reviewed --plan-sha256')
        receipt = json.loads(manifest.read_text())
        checks = {**binding, 'target_sha256': digest(encoded(target)), 'source_sha256': source_hash(files),
                  'workspace_sha256': source_hash(sources(work)), 'variables_sha256': digest((work / 'inputs.tfvars.json').read_bytes()),
                  'plan_sha256': digest(plan.read_bytes()), 'executor_sha256': executor_hash(), 'python_version': sys.version}
        if any(receipt.get(key) != value for key, value in checks.items()) or receipt['plan_sha256'] != plan_sha256:
            raise ValueError('plan, target binding, variables, or module source changed; produce and review a new plan')
        if receipt['apply_attempted']:
            raise ValueError('apply already attempted; reconcile state and review a new plan')
        version = json.loads(command(['version', '-json'], work, log))['terraform_version']
        if version != receipt['terraform_version']:
            raise ValueError('Terraform CLI version changed; produce a new plan')
        if receipt['maintenance_required']:
            try:
                validate_maintenance(maintenance, receipt)
            except (ValueError, TypeError, KeyError) as exc:
                raise OperationError('INFRA_MAINTENANCE_REQUIRED', component='infra', phase='maintenance',
                                     retry_policy='after_configuration', cause=exc) from exc
        receipt['budget_reservation'] = reserve_budget(target, receipt)
        receipt['apply_attempted'] = True
        receipt['apply_status'] = 'outcome_unknown'
        try:
            write_private(manifest, encoded(receipt))
        except OSError as exc:
            raise OperationError('STATE_STORAGE_FAILED', component='infra', phase='dispatch.marker',
                                 retry_policy='after_reconcile', cause=exc) from exc
        command(['apply', '-input=false', '-lock-timeout=30s', str(plan)], work, log)
        try:
            descriptor = json.loads(command(['output', '-json', 'node_descriptor'], work, log))
            if not isinstance(descriptor, dict) or descriptor.get('schema_version') != 'v1':
                raise ValueError('invalid provider descriptor schema')
            if any(descriptor.get(key) != binding[key] for key in ('provider_kind', 'target_id', 'owner_ref', 'execution_driver')):
                raise ValueError('provider output does not match authoritative target/state binding')
            purpose = variables.get('purpose', 'runtime')
            disk, runtime = descriptor.get('data_disk'), descriptor.get('runtime')
            if descriptor.get('purpose', 'runtime') != purpose or (purpose == 'database' and
                    (not isinstance(disk, dict) or disk.get('mount_path') != '/var/lib/postgresql' or
                     not isinstance(runtime, dict) or runtime.get('readiness') != 'not_configured')):
                raise ValueError('provider output does not match the approved node purpose')
            write_private(home / 'node-descriptor.json', encoded(descriptor))
        except (ValueError, TypeError, OSError, OperationError) as exc:
            raise OperationError('OBSERVATION_WRITE_FAILED', component='infra', phase='terraform.output',
                                 outcome='UNKNOWN', side_effect='completed', retry_policy='after_reconcile', cause=exc) from exc
        receipt['apply_status'] = 'completed'
        try:
            write_private(manifest, encoded(receipt))
        except OSError as exc:
            raise OperationError('OBSERVATION_WRITE_FAILED', component='infra', phase='terraform.apply.evidence',
                                 outcome='UNKNOWN', side_effect='completed', retry_policy='after_reconcile', cause=exc) from exc
        return {**binding, 'plan_sha256': plan_sha256, 'operation_id': receipt['operation_id'],
                'apply_status': 'completed', 'readiness': 'unverified', 'node_descriptor': descriptor,
                'budget_reservation': receipt['budget_reservation']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['plan', 'apply'])
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--state-root', type=Path, required=True)
    parser.add_argument('--plan-sha256')
    parser.add_argument('--maintenance', type=Path, help='Fresh trusted drain observation JSON, bound to this plan')
    args = parser.parse_args()
    old_umask = os.umask(0o077)
    try:
        maintenance = json.loads(args.maintenance.read_text()) if args.maintenance else None
        result = execute(args.action, args.target, args.state_root, args.plan_sha256, maintenance=maintenance)
        result['observation'] = event_record('infra.command.completed', component='infra', phase='terraform.' + args.action,
                                             outcome='PASS', attributes={'target_id': result.get('target_id'),
                                             'plan_sha256': result.get('plan_sha256'), 'readiness': 'unverified'})
        print(json.dumps(result, indent=2))
    except (OperationError, ValueError, KeyError, OSError) as exc:
        if isinstance(exc, OperationError):
            error = exc
        else:
            # Apply/evidence boundaries above classify their own failures; configuration errors precede mutation.
            error = OperationError('INFRA_CONFIG_INVALID', component='infra', phase='terraform.configure',
                                   retry_policy='after_configuration', cause=exc)
        print(json.dumps(event_record('infra.command.blocked', component='infra', phase=error.phase,
                                      outcome=error.outcome, error=error)), file=sys.stderr)
        return 2
    finally:
        os.umask(old_umask)
    return 0


if __name__ == '__main__':
    sys.exit(main())
