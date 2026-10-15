"""Single Terraform writer with bound plans, unknown fencing and data retention."""
import hashlib
import json
from .native import GcpError, ROOT, command, digest, directory, private_json, save


class Terraform:
    def __init__(self, home, module, session, binding, *, runner=command, extra=None):
        if module not in ('gcp', 'gcp-network', 'gcp-edge', 'gcp-storage'): raise GcpError('GCP_TERRAFORM_MODULE_INVALID')
        self.home, self.session, self.binding, self.runner = directory(home), session, binding, runner
        source = ROOT / 'infrastructure/terraform' / module
        files = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file() and (p.suffix in ('.tf', '.tftpl') or p.name == '.terraform.lock.hcl')}
        for name, value in (extra or {}).items():
            if not name.endswith('_override.tf.json') or '/' in name: raise GcpError('GCP_OVERRIDE_INVALID')
            files[name] = value.encode()
        expected = {'binding': binding, 'module': module, 'files': {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}}
        marker = self.home / 'binding.json'
        if marker.exists():
            if private_json(marker) != expected: raise GcpError('GCP_TERRAFORM_BINDING_CHANGED')
            for name, sha in expected['files'].items():
                if hashlib.sha256((self.home / name).read_bytes()).hexdigest() != sha: raise GcpError('GCP_TERRAFORM_SOURCE_CHANGED')
        else:
            if (self.home / 'terraform.tfstate').exists(): raise GcpError('GCP_STATE_ADOPTION_REFUSED')
            for name, value in files.items():
                (self.home / name).write_bytes(value); (self.home / name).chmod(0o600)
            save(marker, expected)
    def call(self, *args, mutation=False):
        return self.runner(['terraform', *args], cwd=self.home, env=self.session.env(), mutation=mutation)
    def apply(self, variables):
        journal = self.home / 'journal.json'
        if journal.exists():
            old = private_json(journal)
            if old['variables_sha256'] != digest(variables): raise GcpError('GCP_TERRAFORM_INPUT_CHANGED')
            if old['status'] == 'unknown': raise GcpError('GCP_TERRAFORM_RECONCILIATION_REQUIRED', unknown=True)
            if old['status'] == 'succeeded': return self.output()
        save(self.home / 'variables.json', variables)
        self.call('init', '-input=false', '-lockfile=readonly')
        plan = self.home / 'reviewed.plan'
        self.call('plan', '-input=false', '-lock-timeout=5s', '-var-file=variables.json', '-out=' + str(plan))
        changes = json.loads(self.call('show', '-json', str(plan)))
        if any(row['change']['actions'] not in (['no-op'], ['read'], ['create'], ['update']) for row in changes.get('resource_changes', [])):
            raise GcpError('GCP_DESTRUCTIVE_PLAN_REJECTED')
        receipt = {'status': 'planned', 'variables_sha256': digest(variables), 'plan_sha256': hashlib.sha256(plan.read_bytes()).hexdigest()}
        save(journal, receipt)
        if hashlib.sha256(plan.read_bytes()).hexdigest() != receipt['plan_sha256']: raise GcpError('GCP_PLAN_CHANGED')
        save(journal, {**receipt, 'status': 'unknown'})
        self.call('apply', '-input=false', '-auto-approve', str(plan), mutation=True)
        result = self.output()
        save(journal, {**receipt, 'status': 'succeeded'})
        return result
    def output(self):
        return {key: value['value'] for key, value in json.loads(self.call('output', '-json')).items()}
    def destroy(self, retain=None):
        marker = self.home / 'delete.json'; retain = retain or {}
        if marker.exists():
            old = private_json(marker)
            if old['retained'] != retain: raise GcpError('GCP_RETENTION_CHANGED')
            if old['status'] != 'succeeded': raise GcpError('GCP_DELETE_RECONCILIATION_REQUIRED', unknown=True)
            return old
        self.call('init', '-input=false', '-lockfile=readonly')
        addresses = self.call('state', 'list').splitlines()
        for address, identity in retain.items():
            if address not in addresses: raise GcpError('GCP_RETAINED_RESOURCE_UNPROVEN')
            state = json.loads(self.call('show', '-json'))
            resources = state.get('values', {}).get('root_module', {}).get('resources', [])
            if not any(r['address'] == address and r['values'].get('id') == identity for r in resources): raise GcpError('GCP_RETAINED_IDENTITY_MISMATCH')
        save(marker, {'status': 'unknown', 'retained': retain})
        for address in retain: self.call('state', 'rm', address, mutation=True)
        plan = self.home / 'destroy.plan'
        self.call('plan', '-destroy', '-input=false', '-lock-timeout=5s', '-var-file=variables.json', '-out=' + str(plan))
        changes = json.loads(self.call('show', '-json', str(plan)))
        if any(r['change']['actions'] not in (['delete'], ['read'], ['no-op']) for r in changes.get('resource_changes', [])):
            raise GcpError('GCP_DELETE_PLAN_INVALID', unknown=True)
        self.call('apply', '-input=false', '-auto-approve', str(plan), mutation=True)
        if self.call('state', 'list').strip(): raise GcpError('GCP_DELETE_RESIDUALS', unknown=True)
        result = {'status': 'succeeded', 'retained': retain}; save(marker, result); return result


def owned(home, session, binding, *, runner=command):
    """Reopen the exact source files recorded by the environment writer."""
    home = directory(home); identity = private_json(home / 'binding.json')
    if identity['binding'] != binding: raise GcpError('GCP_TERRAFORM_BINDING_CHANGED')
    for name, expected in identity['files'].items():
        if hashlib.sha256((home / name).read_bytes()).hexdigest() != expected: raise GcpError('GCP_TERRAFORM_SOURCE_CHANGED')
    obj = object.__new__(Terraform); obj.home, obj.session, obj.binding, obj.runner = home, session, binding, runner
    return obj
