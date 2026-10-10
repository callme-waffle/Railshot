#!/usr/bin/env python3
"""Apply an approved edge module to an operator-bound existing local Terraform state.

The private v1 config binds provider, state_file, variables_file, state_dir,
state_lineage, owned_resources (address -> ID), and previous_source_sha. No
creation, replacement, state migration, or uncertain-apply replay is automatic.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'gitops'), str(ROOT / 'ci/scripts')]
from edge import encoded, locked, read_private
from handoff import require
from storage import durable_write

MODULES = {provider: 'infrastructure/terraform/' + provider + '-edge'
           for provider in ('aws', 'gcp', 'openstack')}
RESOURCE_PREFIX = {'aws': 'aws_', 'gcp': 'google_', 'openstack': 'openstack_'}
SHA = re.compile(r'[0-9a-f]{40}')


def module_path(provider, edge_kind='native'):
    require(provider in MODULES and (edge_kind == 'native' or
            (provider == 'openstack' and edge_kind == 'aws-relay')), 'unsupported provider edge kind')
    return 'infrastructure/terraform/openstack-relay-edge' if edge_kind == 'aws-relay' else MODULES[provider]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def native(args, timeout=120):
    # Ambient Terraform flags/workspaces must not change the bound state or plan.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('TF_CLI_ARGS', 'TF_VAR_')) and key not in ('TF_WORKSPACE', 'TF_DATA_DIR')}
    env.update(TF_IN_AUTOMATION='1', AWS_PAGER='')
    result = subprocess.run(args, check=False, capture_output=True, timeout=timeout, env=env)
    require(result.returncode == 0, 'edge command failed; inspect private state before recovery')
    return result.stdout


def terraform(module, *args):
    return native(['terraform', '-chdir=' + str(module), *args], timeout=1800)


def configuration(path):
    config = read_private(path)
    fields = {'version', 'provider', 'state_file', 'variables_file', 'state_dir',
              'state_lineage', 'owned_resources', 'previous_source_sha'}
    require(isinstance(config, dict) and fields <= set(config) <= fields |
            {'edge_kind', 'backend_target', 'backend_ingress'} and config['version'] == 1,
            'exact operator edge config v1 required')
    require(isinstance(config['provider'], str) and config['provider'] in MODULES and
            isinstance(config['previous_source_sha'], str) and SHA.fullmatch(config['previous_source_sha']),
            'bound provider and previous source commit required')
    module_path(config['provider'], config.get('edge_kind', 'native'))
    for key in ('state_file', 'variables_file', 'state_dir'):
        require(isinstance(config[key], str) and Path(config[key]).is_absolute(), 'absolute operator paths required')
    require(isinstance(config['state_lineage'], str) and config['state_lineage'], 'existing state lineage required')
    require(isinstance(config['owned_resources'], dict) and config['owned_resources'] and
            all(isinstance(address, str) and isinstance(identity, str) and identity
                for address, identity in config['owned_resources'].items()), 'explicit existing ownership required')
    require(Path(config['state_file']) != Path(config['variables_file']), 'separate state and variables required')
    if config.get('edge_kind') == 'aws-relay':
        target, ingress = config.get('backend_target'), config.get('backend_ingress')
        require(isinstance(target, dict) and set(target) == {'id', 'port', 'target_group_arn'},
                'exact existing relay target binding required')
        require(isinstance(target['id'], str) and isinstance(target['target_group_arn'], str) and
                re.fullmatch(r'arn:aws:elasticloadbalancing:[a-z0-9-]+:[0-9]{12}:targetgroup/[A-Za-z0-9-]+/[a-f0-9]+',
                             target['target_group_arn']), 'bound AWS target group ARN required')
        address = ipaddress.ip_address(target['id'])
        require(address.version == 4 and any(address in ipaddress.ip_network(cidr) for cidr in
                ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')) and
                type(target['port']) is int and 1 <= target['port'] <= 65535, 'private relay IP and TCP port required')
        require(set(config['owned_resources']) == {'aws_lb_target_group.app', 'aws_lb_listener_rule.app', 'aws_route53_record.app'}
                and config['owned_resources']['aws_lb_target_group.app'] == target['target_group_arn'],
                'relay owns only its bound TG, listener rule and DNS; SG and target attachment stay external')
        require(isinstance(ingress, dict) and set(ingress) == {'rule_id', 'security_group_id', 'source_security_group_id'} and
                all(isinstance(value, str) and re.fullmatch(r'sgr-[a-f0-9]{17}' if key == 'rule_id' else r'sg-[a-f0-9]{8,17}', value)
                    for key, value in ingress.items()), 'exact external control ingress binding required')
    else:
        require('backend_target' not in config and 'backend_ingress' not in config,
                'external relay bindings are only valid for OpenStack aws-relay')
    return config


def bound_state(config):
    state = read_private(config['state_file'])
    require(state.get('version') == 4 and state.get('lineage') == config['state_lineage'] and
            type(state.get('serial')) is int, 'existing state lineage/serial mismatch')
    owned = {}
    for resource in state.get('resources', []):
        if resource.get('mode') == 'data':
            continue
        require(resource.get('mode') == 'managed' and resource['type'].startswith('aws_' if config.get('edge_kind') == 'aws-relay' else RESOURCE_PREFIX[config['provider']]),
                'state contains resources outside bound provider')
        prefix = resource.get('module', '')
        address = (prefix + '.' if prefix else '') + resource['type'] + '.' + resource['name']
        for instance in resource['instances']:
            require(not instance.get('deposed'), 'deposed state requires operator reconciliation')
            key = address + ('[' + json.dumps(instance['index_key'], separators=(',', ':')) + ']'
                             if 'index_key' in instance else '')
            require(key not in owned, 'duplicate state resource')
            owned[key] = instance.get('attributes', {}).get('id')
    require(owned == config['owned_resources'], 'state resource ownership mismatch')
    return state, sha256(Path(config['state_file']).read_bytes())


def read_module(directory):
    directory = Path(directory)
    require(not directory.is_symlink(), 'regular provider module directory required')
    files = {}
    for path in sorted(directory.iterdir()):
        if path.suffix == '.tf' or path.name == '.terraform.lock.hcl':
            require(path.is_file() and not path.is_symlink(), 'regular module file required')
            files[path.name] = path.read_bytes()
    return files


def module_files(source_root, provider, edge_kind='native'):
    return read_module(Path(source_root) / module_path(provider, edge_kind))


def files_digest(files):
    return sha256(encoded({name: sha256(body) for name, body in files.items()}))


def module_digest(source_root, provider, edge_kind='native'):
    """The release producer stores this digest beside its tested image digest."""
    return files_digest(module_files(source_root, provider, edge_kind))


def source_module(source_root, release_sha, config, destination, expected_digest=None):
    require(isinstance(release_sha, str) and SHA.fullmatch(release_sha), 'full immutable release commit required')
    require(Path(source_root).is_absolute(), 'absolute source checkout required')
    module = module_path(config['provider'], config.get('edge_kind', 'native'))
    proof = {'module': module}
    if expected_digest is not None:
        require(isinstance(expected_digest, str) and re.fullmatch(r'[0-9a-f]{64}', expected_digest),
                'trusted release module SHA256 required')
        files = module_files(source_root, config['provider'], config.get('edge_kind', 'native'))
        require(files_digest(files) == expected_digest, 'release module digest mismatch')
    else:
        def git(*args):
            return native(['git', '-C', str(source_root), *args])
        require(git('rev-parse', release_sha + '^{commit}').decode().strip() == release_sha,
                'release commit unavailable')
        proof['module_tree'] = git('rev-parse', release_sha + ':' + module).decode().strip()
        proof['previous_module_tree'] = git('rev-parse', config['previous_source_sha'] + ':' + module).decode().strip()
        files = {}
        for entry in git('ls-tree', '-rz', release_sha, '--', module).split(b'\0'):
            if not entry:
                continue
            meta, raw_name = entry.split(b'\t', 1)
            name = raw_name.decode()
            relative = Path(name).relative_to(module)
            if relative.suffix != '.tf' and relative.name != '.terraform.lock.hcl':
                continue
            require(meta.startswith(b'100644 blob ') or meta.startswith(b'100755 blob '), 'regular module source required')
            require(len(relative.parts) == 1, 'flat approved edge module required')
            files[relative.name] = git('show', release_sha + ':' + name)
    require('.terraform.lock.hcl' in files and any(name.endswith('.tf') for name in files),
            'locked edge module source required')
    require('railshot-backend.tf' not in files, 'reserved executor backend name')
    destination.mkdir(mode=0o700)
    for name, body in files.items():
        require(not re.search(rb'\bbackend\s+"', body), 'source backend migration is not automatic')
        durable_write(destination / name, body)
    durable_write(destination / 'railshot-backend.tf', b'terraform {\n  backend "local" {}\n}\n')
    return {**proof, 'module_sha256': files_digest(files)}


def changed_fields(before, after, prefix=''):
    """Record field paths without emitting provider values or credentials."""
    if isinstance(before, dict) and isinstance(after, dict):
        return [path for key in sorted(before.keys() | after.keys())
                for path in changed_fields(before.get(key), after.get(key), prefix + '.' + key if prefix else key)]
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [path for index, (left, right) in enumerate(zip(before, after))
                for path in changed_fields(left, right, prefix + '[' + str(index) + ']')]
    return [prefix] if before != after else []


def accepted_refresh(plan, config):
    """Accept observed refresh only when this exact saved plan preserves the owned object unchanged."""
    planned = {item['address']: item for item in plan.get('resource_changes', [])}
    accepted, seen = [], set()
    for drift in plan.get('resource_drift', []):
        address = drift['address']
        item = planned.get(address)
        identity = config['owned_resources'].get(address)
        require(identity is not None and address not in seen and item is not None and
                item['change']['actions'] == ['no-op'] and drift['change']['actions'] in (['update'], ['no-op']),
                'reconcile edge drift unless the owned resource has an exact no-op plan: ' + address)
        for observed in (drift, item):
            change = observed['change']
            require(not observed.get('deposed') and not observed.get('previous_address') and
                    not change.get('importing') and not change.get('replace_paths') and
                    isinstance(change.get('before'), dict) and isinstance(change.get('after'), dict) and
                    change['before'].get('id') == identity == change['after'].get('id') and
                    not change.get('after_unknown', {}).get('id'),
                    'drift identity change or ownership migration is not automatic: ' + address)
        seen.add(address)
        accepted.append({'address': address, 'observed_actions': drift['change']['actions'],
                         'planned_actions': ['no-op'], 'identity_preserved': True,
                         'changed_fields': changed_fields(drift['change']['before'], drift['change']['after'])})
    return accepted


def validate_plan(plan, config):
    require(not plan.get('errored') and plan.get('complete', True), 'complete successful edge plan required')
    accepted_refresh(plan, config)
    changes, seen = [], set()
    for item in plan.get('resource_changes', []):
        change, address = item['change'], item['address']
        actions = change['actions']
        if item.get('mode') == 'data' and actions in (['read'], ['no-op']):
            continue
        require(address in config['owned_resources'] and address not in seen and
                actions in (['no-op'], ['update']) and not item.get('previous_address') and not item.get('deposed') and
                not change.get('importing') and not change.get('replace_paths'), 'edge plan may only update bound existing resources: ' + address)
        identity = config['owned_resources'][address]
        require(isinstance(change.get('before'), dict) and isinstance(change.get('after'), dict) and
                change['before'].get('id') == identity == change['after'].get('id') and
                not change.get('after_unknown', {}).get('id'), 'resource identity change is not automatic')
        seen.add(address)
        if actions != ['no-op']:
            changes.append({'address': address, 'actions': actions})
    require(seen == set(config['owned_resources']), 'plan must retain every bound resource')
    # Output-only updates still need the saved plan applied to persist the new contract.
    for name, change in plan.get('output_changes', {}).items():
        actions = change['actions']
        require(actions in (['no-op'], ['create'], ['update'], ['delete']), 'unsupported output change')
        if actions != ['no-op']:
            changes.append({'address': 'output.' + name, 'actions': actions})
    return changes


def verify_relay(config):
    """Observe the external attachment and inline-owned control ingress without mutating either."""
    if config.get('edge_kind') != 'aws-relay':
        return {}
    target, binding = config['backend_target'], config['backend_ingress']
    region, account = target['target_group_arn'].split(':')[3:5]
    targets = json.loads(native(['aws', 'elbv2', 'describe-target-health', '--region', region,
        '--target-group-arn', target['target_group_arn'], '--output', 'json', '--no-cli-pager']))['TargetHealthDescriptions']
    require(len(targets) == 1 and targets[0].get('Target', {}).get('Id') == target['id'] and
            targets[0]['Target'].get('Port') == target['port'] and
            targets[0].get('TargetHealth', {}).get('State') == 'healthy', 'external relay target set or health mismatch')
    rules = json.loads(native(['aws', 'ec2', 'describe-security-group-rules', '--region', region,
        '--security-group-rule-ids', binding['rule_id'], '--output', 'json', '--no-cli-pager']))['SecurityGroupRules']
    require(len(rules) == 1, 'external control ingress rule missing')
    rule = rules[0]
    require(rule.get('SecurityGroupRuleId') == binding['rule_id'] and rule.get('GroupId') == binding['security_group_id'] and
            rule.get('GroupOwnerId') == account and rule.get('IsEgress') is False and rule.get('IpProtocol') == 'tcp' and
            rule.get('FromPort') == rule.get('ToPort') == target['port'] and
            rule.get('ReferencedGroupInfo', {}).get('GroupId') == binding['source_security_group_id'] and
            not any(rule.get(key) for key in ('CidrIpv4', 'CidrIpv6', 'PrefixListId')), 'external control ingress rule mismatch')
    return {'external_target_verified': True, 'external_ingress_verified': True}


def verify_existing(source_root, release_sha, config, receipt, root, module_sha256):
    """Refresh a succeeded release without applying or changing its durable receipt."""
    if module_sha256 is not None:
        require(module_digest(source_root, config['provider'], config.get('edge_kind', 'native')) == receipt['module_sha256'] == module_sha256,
                'release module digest mismatch')
    else:
        tree = native(['git', '-C', str(source_root), 'rev-parse',
                       release_sha + ':' + module_path(config['provider'], config.get('edge_kind', 'native'))]).decode().strip()
        require(tree == receipt.get('module_tree'), 'release module tree mismatch')
    directory = root / release_sha
    module = directory / 'module'
    files = read_module(module)
    require(files.pop('railshot-backend.tf', None) == b'terraform {\n  backend "local" {}\n}\n' and
            files_digest(files) == receipt['module_sha256'], 'materialized edge module changed')
    metadata = module / '.terraform/terraform.tfstate'
    info = metadata.lstat()
    # Terraform writes this local-backend metadata as 0644 inside our private module directory.
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
            not info.st_mode & 0o022 and info.st_size < 2_000_000, 'owned backend metadata required')
    backend = json.loads(metadata.read_bytes()).get('backend', {})
    require(backend.get('type') == 'local' and backend.get('config', {}).get('path') == config['state_file'],
            'materialized backend must retain the bound state')
    environment = module / '.terraform/environment'
    require(not environment.exists() or environment.read_text().strip() == 'default',
            'default bound Terraform workspace required')
    require(sha256(encoded(read_private(directory / 'variables.tfvars.json'))) == receipt['variables_sha256'],
            'materialized variables changed')
    verify_relay(config)
    with tempfile.TemporaryDirectory(prefix='.verify-' + release_sha + '-', dir=root) as temporary:
        saved = Path(temporary) / 'verified.tfplan'
        terraform(module, 'plan', '-input=false', '-lock-timeout=10s', '-no-color',
                  '-var-file=' + str(directory / 'variables.tfvars.json'), '-out=' + str(saved))
        os.chmod(saved, 0o600)
        fingerprint = sha256(saved.read_bytes())
        plan = json.loads(terraform(module, 'show', '-json', str(saved)))
        require(not validate_plan(plan, config), 'refreshed edge plan still has changes')
        require(sha256(saved.read_bytes()) == fingerprint and
                bound_state(config)[1] == receipt['state_after_sha256'], 'edge state changed during verification')
    return {**receipt, **verify_relay(config), 'reverified': True, 'verification_plan_sha256': fingerprint,
            'verification_accepted_noop_refresh': accepted_refresh(plan, config)}


def run(source_root, release_sha, config_path, module_sha256=None, verify_only=False):
    config = configuration(config_path)
    require(isinstance(release_sha, str) and SHA.fullmatch(release_sha), 'full immutable release commit required')
    binding = sha256(encoded(config))
    require(not verify_only or Path(config['state_dir']).is_dir(), 'succeeded edge attempt required for verification')
    with locked(config) as root:
        state, state_hash = bound_state(config)
        receipt_path = root / ('attempt-' + release_sha + '.json')
        for prior in root.glob('attempt-*.json'):
            require(read_private(prior)['phase'] not in ('applying', 'verifying', 'uncertain'),
                    'previous edge mutation requires observation; automatic replay forbidden')
        if receipt_path.exists():
            receipt = read_private(receipt_path)
            require(receipt['phase'] == 'succeeded' and receipt['binding_sha256'] == binding and
                    receipt['state_after_sha256'] == state_hash and
                    receipt['variables_sha256'] == sha256(encoded(read_private(config['variables_file']))) and
                    (module_sha256 is None or receipt['module_sha256'] == module_sha256), 'existing edge attempt requires observation')
            if verify_only:
                return verify_existing(source_root, release_sha, config, receipt, root, module_sha256)
            return {**receipt, **verify_relay(config), 'cached': True}
        require(not verify_only, 'succeeded edge attempt required for verification')
        directory = root / release_sha
        directory.mkdir(mode=0o700)
        module = directory / 'module'
        proof = source_module(source_root, release_sha, config, module, module_sha256)
        variables = read_private(config['variables_file'])
        require(isinstance(variables, dict), 'JSON Terraform variables required')
        variables_hash = sha256(encoded(variables))
        durable_write(directory / 'variables.tfvars.json', encoded(variables))
        durable_write(directory / 'state-before.json', Path(config['state_file']).read_bytes())
        receipt = {'version': 1, 'provider': config['provider'], 'edge_kind': config.get('edge_kind', 'native'), 'release_sha': release_sha,
                   'previous_source_sha': config['previous_source_sha'], **proof,
                   'binding_sha256': binding, 'variables_sha256': variables_hash,
                   'state_lineage': state['lineage'], 'state_serial_before': state['serial'],
                   'state_before_sha256': state_hash, 'phase': 'planning'}
        def save():
            durable_write(receipt_path, encoded(receipt))
        save()
        try:
            verify_relay(config)
            terraform(module, 'init', '-input=false', '-lockfile=readonly',
                      '-backend-config=path=' + config['state_file'])
            terraform(module, 'validate', '-no-color')
            saved = directory / 'approved.tfplan'
            def plan(path):
                terraform(module, 'plan', '-input=false', '-lock-timeout=10s', '-no-color',
                          '-var-file=' + str(directory / 'variables.tfvars.json'), '-out=' + str(path))
                os.chmod(path, 0o600)
                fingerprint = sha256(path.read_bytes())
                document = json.loads(terraform(module, 'show', '-json', str(path)))
                return fingerprint, validate_plan(document, config), accepted_refresh(document, config)
            fingerprint, changes, refresh = plan(saved)
            receipt.update(phase='planned', plan_sha256=fingerprint, changes=changes, accepted_noop_refresh=refresh)
            save()
            require(sha256(saved.read_bytes()) == fingerprint, 'saved plan changed')
            require(sha256(encoded(configuration(config_path))) == binding and
                    sha256(encoded(read_private(config['variables_file']))) == variables_hash,
                    'operator binding changed during planning')
            require(bound_state(config)[1] == state_hash, 'state changed during planning')
            if changes:
                receipt['phase'] = 'applying'
                save()  # Durable intent before mutation: failed/killed applies are never replayed.
                terraform(module, 'apply', '-input=false', '-lock-timeout=10s', '-no-color', str(saved))
            receipt['phase'] = 'verifying'
            save()
            _, remaining, verified_refresh = plan(directory / 'verified.tfplan')
            require(not remaining, 'refreshed edge plan still has changes')
            following, following_hash = bound_state(config)
            receipt.update(**verify_relay(config), phase='succeeded', state_serial_after=following['serial'],
                           state_after_sha256=following_hash, refreshed_plan_no_changes=True,
                           verification_accepted_noop_refresh=verified_refresh)
            save()
            return receipt
        except Exception:
            receipt['phase'] = 'uncertain' if receipt['phase'] in ('applying', 'verifying') else 'failed'
            save()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', required=True)
    parser.add_argument('--release-sha', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--module-sha256', help='trusted release module digest for image source without Git')
    parser.add_argument('--verify-only', action='store_true', help='fresh read-only plan for an existing succeeded release')
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.source_root, args.release_sha, args.config, args.module_sha256, args.verify_only), sort_keys=True))
    except (ValueError, OSError, subprocess.SubprocessError):
        print(json.dumps({'status': 'failed', 'error': 'edge update failed; inspect private attempt receipt before recovery'}))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
