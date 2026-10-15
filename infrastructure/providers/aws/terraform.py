"""One private Terraform writer per network/node/addon; saved plans only."""
from pathlib import Path
import hashlib
import json
import shutil
from .native import AwsError, command, digest, directory, private_json, save

ROOT = Path(__file__).resolve().parents[3]


class Terraform:
    def __init__(self, home, module, session, binding, *, runner=command, extra=None):
        self.home = directory(Path(home))
        self.session, self.runner = session, runner
        source = ROOT / 'infrastructure/terraform' / module
        files = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file() and (p.suffix in ('.tf', '.tftpl') or p.name == '.terraform.lock.hcl')}
        if not files: raise AwsError('AWS_MODULE_NOT_FOUND')
        for name, content in (extra or {}).items(): files[name] = content.encode()
        bound = {'binding': binding, 'source': {k: hashlib.sha256(v).hexdigest() for k, v in files.items()}}
        record = self.home / 'binding.json'
        if record.exists():
            if private_json(record) != bound: raise AwsError('AWS_STATE_BINDING_CHANGED')
        else:
            if any(self.home.iterdir()): raise AwsError('AWS_STATE_ADOPTION_FORBIDDEN')
            save(record, bound)
        for name, content in files.items():
            dest = self.home / name
            if dest.exists() and dest.read_bytes() != content: raise AwsError('AWS_MODULE_CHANGED')
            dest.write_bytes(content); dest.chmod(0o600)

    def call(self, *args, mutation=False):
        return self.runner(['terraform', *args], cwd=self.home, env=self.session.env(), timeout=1800, mutation=mutation)

    def output(self):
        return {key: row['value'] for key, row in json.loads(self.call('output', '-json')).items()}

    def plan(self, variables, *, destroy=False):
        marker = self.home / 'attempt.json'
        if marker.exists() and private_json(marker)['status'] == 'unknown':
            raise AwsError('AWS_TERRAFORM_OUTCOME_UNKNOWN', unknown=True)
        save(self.home / 'inputs.tfvars.json', variables)
        self.call('init', '-input=false', '-no-color', '-lockfile=readonly')
        args = ['plan', '-input=false', '-no-color', '-lock-timeout=30s', '-var-file=inputs.tfvars.json', '-out=reviewed.plan']
        if destroy: args += ['-destroy']
        self.call(*args)
        plan = json.loads(self.call('show', '-json', 'reviewed.plan'))
        changes = [{ 'address': r['address'], 'actions': r['change']['actions']} for r in plan.get('resource_changes', [])]
        allowed = {'read', 'no-op', 'delete'} if destroy else {'read', 'no-op', 'create', 'update'}
        if any(set(r['actions']) - allowed for r in changes): raise AwsError('AWS_DESTRUCTIVE_PLAN_REJECTED')
        receipt = {'input': digest(variables), 'plan': hashlib.sha256((self.home / 'reviewed.plan').read_bytes()).hexdigest(), 'changes': changes, 'destroy': destroy}
        save(self.home / 'plan.json', receipt)
        return receipt

    def apply(self, variables):
        receipt = self.plan(variables)
        self.apply_saved(receipt)
        return self.output()

    def apply_saved(self, receipt):
        if receipt != private_json(self.home / 'plan.json') or receipt['plan'] != hashlib.sha256((self.home / 'reviewed.plan').read_bytes()).hexdigest():
            raise AwsError('AWS_PLAN_CHANGED')
        save(self.home / 'attempt.json', {'status': 'unknown', 'plan': receipt})
        self.call('apply', '-input=false', '-no-color', '-lock-timeout=30s', 'reviewed.plan', mutation=True)
        save(self.home / 'attempt.json', {'status': 'succeeded', 'plan': receipt})

    def destroy(self, *, retain=None):
        """Retain exact approved data objects before destroying the remaining owned state."""
        variables = private_json(self.home / 'inputs.tfvars.json')
        if retain:
            if (self.home / 'retained.json').exists():
                if private_json(self.home / 'retained.json') != retain: raise AwsError('AWS_RETAIN_BINDING_CHANGED')
            else:
                save(self.home / 'retained.json', retain)
            present = self.call('state', 'list').splitlines()
            for address in retain:
                if address in present: self.call('state', 'rm', address)
        receipt = self.plan(variables, destroy=True)
        self.apply_saved(receipt)
        return retain or {}


def open_owned(home, session, binding, *, runner=command):
    """Reopen a stage for lifecycle work without silently rebinding source or inputs."""
    obj = Terraform.__new__(Terraform)
    obj.home, obj.session, obj.runner = directory(Path(home)), session, runner
    saved = private_json(obj.home / 'binding.json')
    if saved['binding'] != binding: raise AwsError('AWS_STATE_BINDING_CHANGED')
    for name, expected in saved['source'].items():
        if hashlib.sha256((obj.home / name).read_bytes()).hexdigest() != expected:
            raise AwsError('AWS_MODULE_CHANGED')
    return obj


def destroy_addon(home, session, binding, *, module=None, state=None, variables=None, retain=None, runner=command):
    """Take a one-time deletion lease on an existing addon state, keeping data IDs.

    The caller verifies the addon creation journal first. No resources are imported,
    adopted, or deleted by an alternate cloud SDK writer.
    """
    home = directory(Path(home)); cwd = module or home
    state = Path(state or home / 'terraform.tfstate')
    lease = home / 'aws-delete.json'
    if lease.exists():
        old = private_json(lease)
        if old['binding'] != binding: raise AwsError('AWS_DELETE_BINDING_CHANGED')
        if old['status'] == 'succeeded': return old.get('retained', {})
        raise AwsError('AWS_DELETE_OUTCOME_UNKNOWN', unknown=True)
    def call(*args, mutation=False):
        env = session.env(); env['TF_DATA_DIR'] = str(home / 'terraform-data')
        return runner(['terraform', *args], cwd=cwd, env=env, timeout=1800, mutation=mutation)
    if not state.exists(): return {}
    save(lease, {'binding': binding, 'status': 'unknown', 'retained': retain or {}})
    call('init', '-input=false', '-no-color', '-lockfile=readonly')
    present = call('state', 'list', '-state=' + str(state)).splitlines()
    for address in retain or {}:
        if address in present: call('state', 'rm', '-state=' + str(state), address)
    args = ['plan', '-destroy', '-input=false', '-no-color', '-lock-timeout=30s', '-state=' + str(state), '-out=' + str(home / 'delete.plan')]
    if variables: args += ['-var-file=' + str(variables)]
    call(*args)
    changes = json.loads(call('show', '-json', str(home / 'delete.plan'))).get('resource_changes', [])
    if any(set(r['change']['actions']) - {'delete', 'read', 'no-op'} for r in changes):
        raise AwsError('AWS_DELETE_PLAN_INVALID', unknown=True)
    call('apply', '-input=false', '-no-color', '-state=' + str(state), str(home / 'delete.plan'), mutation=True)
    remaining = call('state', 'list', '-state=' + str(state)).splitlines()
    if any(not r.startswith('data.') for r in remaining): raise AwsError('AWS_DELETE_UNVERIFIED', unknown=True)
    save(lease, {'binding': binding, 'status': 'succeeded', 'retained': retain or {}})
    return retain or {}
