#!/usr/bin/env python3
"""Connect a verified app publication to its registered native edge and authoritative DNS."""
import argparse
import fcntl
import stat
import hashlib
import importlib.util
import json
import os
from pathlib import Path

import application_release
import applications
import environment as runtime
import dns
import edge
import openstack_routes

_tunnel_spec = importlib.util.spec_from_file_location('railshot_tunnel_registration', runtime.ROOT / 'deployment/cloudflared/register.py')
tunnel = importlib.util.module_from_spec(_tunnel_spec)
_tunnel_spec.loader.exec_module(tunnel)


def ensure(config_path, publication_request):
    prepared = application_release.finalize(config_path, publication_request)
    config = applications.load_config(config_path)
    root = applications.private_directory(config['state_dir'])
    with os.fdopen(os.open(root / 'registration.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as lock:
        info = os.fstat(lock.fileno())
        applications.require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                             'APPLICATION_STORAGE_INVALID')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise applications.RegistrationError('REGISTRATION_BUSY') from None
        applications.assert_deployable(root / prepared['application_id'])
        return _ensure(config, publication_request, prepared)


def _ensure(config, publication_request, prepared):
    directory = Path(config['state_dir']) / prepared['application_id'] / 'deployments' / publication_request['deployment_id']
    # finalize has checked both hashes against release.json and the current registration.
    request = runtime.read_private(directory / 'route-request.json')
    ingress = request['ingress']
    applications.require(request['provider'] in ('aws', 'gcp', 'openstack'), 'APPLICATION_ROUTE_PROVIDER_UNAVAILABLE')
    paths = ('edge_config_file', 'dns_config_file') + (('tunnel_config_file',) if request['provider'] == 'openstack' else ())
    applications.require(all(isinstance(ingress.get(key), str) and Path(ingress[key]).is_absolute()
                             for key in paths), 'APPLICATION_ROUTE_NOT_CONFIGURED')
    dns_config = dns.config_at(ingress['dns_config_file'])
    applications.require(dns_config['base_domain'] == ingress['base_domain'], 'APPLICATION_DNS_ZONE_MISMATCH')
    app_id = prepared['application_id']
    if request['provider'] == 'aws':
        registry = runtime.read_private(config['registry_file'])
        _, descriptor = runtime.registered_node(registry['targets'][request['environment_id']],
                                                request['environment_id'], 'route.' + app_id)
        security_group = runtime.aws_security_group(descriptor, ingress.get('target_security_group_id'))
        allocation = edge.prepare(ingress['edge_config_file'], {
            'target_id': app_id, 'tenant': request['tenant'], 'app': request['app'],
            'environment_id': request['environment_id'], 'provider_kind': 'aws',
            'target_private_ip': request['resource']['private_address'], 'namespace': request['namespace'],
            'health_path': request['health_path'], 'expected_status': 200, 'node_port': request['node_port'],
            'manage_dns': False, 'target_security_group_id': security_group, 'expires_at': ingress.get('expires_at'),
        })
        applications.require(all(allocation[key] == request[key] for key in ('hostname', 'node_port')),
                             'APPLICATION_ROUTE_BINDING_MISMATCH')
        edge.ensure(allocation['reference'])
        native_config, applied = edge.load(allocation['reference'])
        if applied['phase'] != 'applied':
            raise applications.RegistrationError('APPLICATION_ROUTE_RECONCILE_REQUIRED', unknown=applied['phase'] == 'applying')
        outputs = json.loads(edge.terraform(native_config, 'output', '-json'))
        target = outputs['alb_dns_name']['value'].rstrip('.')
        applications.require(target.endswith('.elb.amazonaws.com'), 'APPLICATION_ROUTE_BINDING_MISMATCH')
        dns_request = {'application_id': app_id, 'hostname': request['hostname'], 'type': 'CNAME', 'content': target}
        edge_receipt = {'provider': 'aws', 'plan_sha256': applied['plan_sha256'], 'route_key': applied['route_key']}
    elif request['provider'] == 'gcp':
        import gcp_routes
        try:
            native = gcp_routes.ensure(ingress['edge_config_file'], {
                'application_id': app_id, 'hostname': request['hostname'], 'node_port': request['node_port'],
                'health_path': request['health_path'],
            }, dns_config_path=ingress['dns_config_file'])
        except gcp_routes.RouteError as error:
            raise applications.RegistrationError(error.code, unknown=error.unknown) from error
        applications.require(native['hostname'] == request['hostname'], 'APPLICATION_ROUTE_BINDING_MISMATCH')
        auth = native['dns_authorization_record']
        if auth is not None:
            applications.require(auth['type'] == 'CNAME' and auth['data'].rstrip('.').endswith('.authorize.certificatemanager.goog'),
                                 'APPLICATION_CERTIFICATE_DNS_INVALID')
            dns.ensure(ingress['dns_config_file'], {'application_id': app_id, 'purpose': 'certificate',
                'application_hostname': request['hostname'], 'hostname': auth['name'].rstrip('.'),
                'type': 'CNAME', 'content': auth['data'].rstrip('.')})
        dns_request = {'application_id': app_id, 'hostname': request['hostname'], 'type': 'A', 'content': native['frontend_ip']}
        edge_receipt = {'provider': 'gcp', 'backend_service': native['backend_service']}
    else:
        tunnel_request = {key: request[key] for key in ('environment_id', 'application_id', 'app', 'tenant', 'hostname')}
        # Check the existing runtime/Tunnel binding before the Octavia worker can write.
        try:
            tunnel_config, _ = tunnel.inputs(ingress['tunnel_config_file'], tunnel_request)
        except (OSError, ValueError, KeyError, TypeError):
            raise applications.RegistrationError('APPLICATION_TUNNEL_CONFIGURATION_INVALID') from None
        resource = request['resource']
        applications.require(tunnel_config['registry_file'] == config['registry_file'] == resource['registry_file']
                             and tunnel_config['environment_id'] == request['environment_id'] == resource['registry_target_id']
                             and tunnel_config['resource_id'] == resource['resource_id']
                             and tunnel_config['runtime_private_address'] == resource['private_address']
                             and tunnel_config['base_domain'] == ingress['base_domain'], 'APPLICATION_ROUTE_BINDING_MISMATCH')
        try:
            edge_config = runtime.read_private(ingress['edge_config_file'])
        except (OSError, ValueError):
            raise applications.RegistrationError('APPLICATION_OPENSTACK_ROUTE_CONFIGURATION_INVALID') from None
        applications.require(isinstance(edge_config, dict) and edge_config.get('provider') == 'openstack'
                             and edge_config.get('base_domain') == ingress['base_domain']
                             and edge_config.get('runtime_private_address') == resource['private_address'],
                             'APPLICATION_ROUTE_BINDING_MISMATCH')
        backend_request = {key: request[key] for key in ('application_id', 'hostname', 'node_port', 'health_path')}
        backend_request['private_address'] = resource['private_address']
        try:
            openstack_routes.worker.checked_request(edge_config, backend_request)
        except ValueError:
            raise applications.RegistrationError('APPLICATION_OPENSTACK_ROUTE_INVALID') from None
        native = openstack_routes.ensure(ingress['edge_config_file'], backend_request)
        if not (isinstance(native, dict) and native.get('status') == 'configured' and native.get('https_verified') is False
                and all(native.get(key) == value for key, value in backend_request.items())):
            raise applications.RegistrationError('APPLICATION_ROUTE_BINDING_MISMATCH', unknown=True)
        connector = tunnel.ensure(ingress['tunnel_config_file'], tunnel_request)
        if not isinstance(connector, dict) or connector.get('status') != 'succeeded':
            blocked = (isinstance(connector, dict) and connector.get('status') == 'blocked'
                       and connector.get('error', {}).get('outcome_unknown') is False)
            raise applications.RegistrationError('APPLICATION_TUNNEL_BLOCKED' if blocked else 'APPLICATION_TUNNEL_OUTCOME_UNKNOWN',
                                                 unknown=not blocked)
        expected_dns = {'type': 'CNAME', 'content': tunnel_config['tunnel_id'] + '.cfargotunnel.com', 'proxied': True}
        if not (connector.get('phase') == 'tunnel_configured' and connector.get('https_verified') is False
                and connector.get('hostname') == request['hostname'] and connector.get('tunnel_id') == tunnel_config['tunnel_id']
                and connector.get('dns') == expected_dns and connector['dns'].get('proxied') is True):
            raise applications.RegistrationError('APPLICATION_TUNNEL_READBACK_MISMATCH', unknown=True)
        dns_request = {'application_id': app_id, 'hostname': request['hostname'], **connector['dns']}
        edge_receipt = {'provider': 'openstack', 'resources': native['resources'],
                        'network_rule_id': native['network_rule_id'], 'tunnel_id': connector['tunnel_id']}
    proof = dns.ensure(ingress['dns_config_file'], dns_request)
    result = {**prepared, 'phase': 'route_configured', 'public_route_state': 'configured',
              'https_verified': False, 'dns_record_id': proof['record_id'], 'edge': edge_receipt,
              'route_request_sha256': hashlib.sha256(runtime.read_private(directory / 'route-request.json', raw=True)).hexdigest()}
    if request['provider'] == 'openstack':
        result['backend_verified'] = False
    runtime.save(directory / 'route-result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        result = ensure(args.config, application_release.request_file(args.request))
    except Exception as error:
        # A native operation may have written before returning an error. Never replay it blindly.
        known = isinstance(error, (applications.RegistrationError, dns.DNSError, openstack_routes.RouteError))
        unknown = not known or getattr(error, 'unknown', False) or getattr(error, 'outcome', None) == 'UNKNOWN'
        result = {'status': 'unknown' if unknown else 'blocked', 'public_route_state': 'unverified',
                  'error': {'code': error.code if known else 'APPLICATION_ROUTE_RECONCILE_REQUIRED',
                            'retryable': False, 'outcome_unknown': unknown}}
    print(json.dumps(result))
    return 0 if result['status'] == 'succeeded' else 3


if __name__ == '__main__':
    raise SystemExit(main())
