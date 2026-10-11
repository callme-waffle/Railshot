#!/usr/bin/env python3
"""Root-owned WireGuard gateway adapter. JSON stdin/stdout; never log key material."""
import argparse
import base64
from contextlib import contextmanager
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time

MARKER = '# Managed by RailShot personal gateway v1\n'
ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
WIREGUARD_DIRECTORY = Path('/etc/wireguard')


def require(value, code='GATEWAY_INVALID'):
    if not value:
        raise ValueError(code)


def key(value):
    try:
        require(isinstance(value, str) and len(base64.b64decode(value, validate=True)) == 32)
    except Exception:
        raise ValueError('GATEWAY_KEY_INVALID') from None
    return value


def private(path, owner=None, max_bytes=1048576):
    path = Path(path)
    require(path.is_absolute() and not any(p.is_symlink() for p in (path, *path.parents)), 'GATEWAY_PATH_UNSAFE')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'r') as stream:
        metadata = os.fstat(stream.fileno())
        require(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == (os.geteuid() if owner is None else owner)
                and not metadata.st_mode & 0o077 and metadata.st_nlink == 1, 'GATEWAY_FILE_UNSAFE')
        result = stream.read(max_bytes + 1)
        require(len(result) <= max_bytes, 'GATEWAY_FILE_TOO_LARGE')
        return result


def save(path, value):
    path = Path(path)
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'GATEWAY_PATH_UNSAFE')
    if path.exists():
        private(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(path.parent.stat().st_uid == os.geteuid() and not path.parent.stat().st_mode & 0o022)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run(args, *, input=None):
    result = subprocess.run(args, input=input, capture_output=True, text=True, timeout=30, check=False)
    require(result.returncode == 0, 'GATEWAY_COMMAND_FAILED')
    return result.stdout.strip()


def validate(config):
    require(config.get('version') == 1)
    require(re.fullmatch(r'railshot[a-z0-9]{0,7}', config['interface']))
    pool = ipaddress.ip_network(config['address_pool'])
    address = ipaddress.ip_interface(config['server_address'])
    require(pool.version == 4 and 24 <= pool.prefixlen <= 28 and address.ip in pool and address.network.prefixlen == 32)
    require(re.fullmatch(r'[A-Za-z0-9.-]+:[0-9]{1,5}', config['endpoint']))
    require(1 <= int(config['endpoint'].rsplit(':', 1)[1]) <= 65535)
    require(type(config['listen_port']) is int and 1 <= config['listen_port'] <= 65535)
    require(config.get('lifecycle', 'systemd') in ('systemd', 'direct'), 'GATEWAY_LIFECYCLE_INVALID')
    return pool, address.ip


def server_public_key(config, runner):
    return key(runner(['wg', 'pubkey'], input=private(config['private_key_file']).strip() + '\n'))


def existing_interface(config, runner):
    links = json.loads(runner(['ip', '-j', '-d', 'link', 'show']))
    require(isinstance(links, list), 'GATEWAY_LINK_UNVERIFIED')
    matches = [link for link in links if link.get('ifname') == config['interface']]
    require(len(matches) <= 1, 'GATEWAY_LINK_UNVERIFIED')
    if not matches:
        return False
    path = WIREGUARD_DIRECTORY / (config['interface'] + '.conf')
    require(path.exists() and private(path).startswith(MARKER), 'GATEWAY_INTERFACE_NOT_OWNED')
    require(matches[0].get('linkinfo', {}).get('info_kind') == 'wireguard', 'GATEWAY_INTERFACE_NOT_OWNED')
    require(runner(['wg', 'show', config['interface'], 'public-key']) == server_public_key(config, runner),
            'GATEWAY_INTERFACE_IDENTITY_CHANGED')
    return True


def forwarding_rules(config):
    return [f'-A FORWARD {direction} {config["interface"]} -j DROP' for direction in ('-i', '-o')]


def verify_forwarding(config, runner):
    verify_forwarding_guard(config, runner)
    rules = [line for line in runner(['iptables', '-w', '-S', 'FORWARD']).splitlines() if line.startswith('-A ')]
    # A DROP behind an ACCEPT would not isolate customers. Require both rules
    # at the beginning, ahead of any existing forwarding policy.
    require(set(rules[:2]) == set(forwarding_rules(config)), 'GATEWAY_FORWARDING_UNVERIFIED')


def verify_forwarding_guard(config, runner):
    # Netfilter's mangle FORWARD hook precedes filter FORWARD. A later
    # kube-proxy filter ACCEPT cannot resurrect packets dropped at this hook.
    # Only forwarded packets match: gateway-local INPUT/OUTPUT is unaffected.
    rules = [line for line in runner(['iptables', '-w', '-t', 'mangle', '-S', 'FORWARD']).splitlines()
             if line.startswith('-A ')]
    require(set(rules[:2]) == set(forwarding_rules(config)), 'GATEWAY_FORWARD_GUARD_UNVERIFIED')


def ensure_forwarding(config, runner):
    try:
        verify_forwarding_guard(config, runner)
    except ValueError:
        for direction in ('-i', '-o'):
            runner(['iptables', '-w', '-t', 'mangle', '-I', 'FORWARD', '1', direction, config['interface'], '-j', 'DROP'])
    verify_forwarding_guard(config, runner)
    try:
        verify_forwarding(config, runner)
    except ValueError:
        for direction in ('-i', '-o'):
            runner(['iptables', '-w', '-I', 'FORWARD', '1', direction, config['interface'], '-j', 'DROP'])
    verify_forwarding(config, runner)


def verify_applied(config, ledger, runner):
    require(existing_interface(config, runner), 'GATEWAY_INTERFACE_MISSING')
    interface = config['interface']
    require(runner(['wg', 'show', interface, 'listen-port']) == str(config['listen_port']), 'GATEWAY_PORT_UNVERIFIED')
    addresses = json.loads(runner(['ip', '-j', '-4', 'address', 'show', 'dev', interface]))
    observed = [f'{item["local"]}/{item["prefixlen"]}' for row in addresses for item in row.get('addr_info', [])
                if item.get('family') == 'inet']
    require(observed == [config['server_address']], 'GATEWAY_ADDRESS_UNVERIFIED')
    peers = {}
    for row in runner(['wg', 'show', interface, 'allowed-ips']).splitlines():
        parts = row.split()
        require(len(parts) == 2 and parts[0] not in peers, 'GATEWAY_PEERS_UNVERIFIED')
        peers[parts[0]] = parts[1]
    expected = {peer['public_key']: peer['address'] for peer in ledger.values() if not peer['removed']}
    require(peers == expected, 'GATEWAY_PEERS_UNVERIFIED')
    routes = json.loads(runner(['ip', '-j', '-4', 'route', 'show', 'dev', interface]))
    destinations = {str(ipaddress.ip_network(route['dst'])) for route in routes if route.get('dst') != 'default' and 'dst' in route}
    active_addresses = set(expected.values())
    retired_addresses = {peer['address'] for peer in ledger.values() if peer['removed']} - active_addresses
    require(active_addresses <= destinations and not retired_addresses.intersection(destinations),
            'GATEWAY_ROUTES_UNVERIFIED')
    verify_forwarding(config, runner)


def apply(config, ledger, runner):
    interface = config['interface']
    path = WIREGUARD_DIRECTORY / (interface + '.conf')
    exists = existing_interface(config, runner)  # Check ownership before writing the marker/config.
    secret = key(private(config['private_key_file']).strip())
    lines = [MARKER, '[Interface]\n', 'PrivateKey = ' + secret + '\n',
             'Address = ' + config['server_address'] + '\n', 'ListenPort = ' + str(config['listen_port']) + '\n']
    # systemd starts wg-quick independently on host boot: retain its automatic
    # AllowedIPs routes. Only the direct entrypoint always invokes --restore.
    if config.get('lifecycle', 'systemd') == 'direct':
        lines.append('Table = off\n')
    # Independent wg-quick/systemd starts also establish the earlier guard
    # before loading peers or bringing the link up, including after a cold boot.
    # Inserting exact DROP rules preserves other owners' chains and rules.
    lines.append('PreUp = iptables -w -t mangle -I FORWARD 1 -i %i -j DROP; '
                 'iptables -w -t mangle -I FORWARD 1 -o %i -j DROP\n')
    lines.append('PostUp = iptables -w -I FORWARD -i %i -j DROP; iptables -w -I FORWARD -o %i -j DROP\n')
    for peer in ledger.values():
        if not peer.get('removed'):
            lines.extend(['\n[Peer]\n', 'PublicKey = ' + key(peer['public_key']) + '\n', 'AllowedIPs = ' + peer['address'] + '\n'])
    if path.exists():
        require(private(path).startswith(MARKER), 'GATEWAY_CONFIG_NOT_OWNED')
    save(path, ''.join(lines))
    # Block forwarding in both directions before enabling peers: customers receive
    # only a server /32 route and cannot use this gateway as a network transit.
    ensure_forwarding(config, runner)
    if config.get('lifecycle', 'systemd') == 'systemd':
        runner(['systemctl', 'enable', '--now', 'wg-quick@' + interface])
    elif not exists:
        runner(['wg-quick', 'up', str(path)])
    stripped = runner(['wg-quick', 'strip', str(path)])
    fd, tmp = tempfile.mkstemp(prefix='railshot-wg-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(stripped)
        runner(['wg', 'syncconf', interface, tmp])
        active_addresses = {peer['address'] for peer in ledger.values() if not peer['removed']}
        retired_routes = set()
        for peer in ledger.values():
            route = ['ip', '-4', 'route']
            if peer.get('removed'):
                # A historical removal must not remove the new owner's route.
                if peer['address'] in active_addresses or peer['address'] in retired_routes:
                    continue
                retired_routes.add(peer['address'])
                existing = runner([*route, 'show', peer['address'], 'dev', interface])
                if existing:
                    runner([*route, 'del', peer['address'], 'dev', interface])
            else:
                other = json.loads(runner(['ip', '-j', '-4', 'route', 'show', 'exact', peer['address']]))
                require(all(row.get('dev') == interface for row in other), 'GATEWAY_ROUTE_ALREADY_OWNED')
                runner([*route, 'replace', peer['address'], 'dev', interface])
    finally:
        os.unlink(tmp)
    verify_applied(config, ledger, runner)


def validate_ledger(config, ledger):
    pool, server_ip = validate(config)
    require(isinstance(ledger, dict), 'GATEWAY_LEDGER_INVALID')
    addresses, keys = set(), set()
    for ident, peer in ledger.items():
        require(isinstance(ident, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}:[1-9][0-9]*', ident), 'GATEWAY_LEDGER_INVALID')
        require(isinstance(peer, dict) and set(peer) == {'public_key', 'address', 'removed'} and type(peer['removed']) is bool,
                'GATEWAY_LEDGER_INVALID')
        address = ipaddress.ip_interface(peer['address'])
        require(address.version == 4 and address.network.prefixlen == 32 and address.ip in pool
                and address.ip not in (server_ip, pool.network_address, pool.broadcast_address)
                and str(address) == peer['address']
                and (peer['removed'] or peer['address'] not in addresses), 'GATEWAY_LEDGER_INVALID')
        require(key(peer['public_key']) not in keys, 'GATEWAY_LEDGER_INVALID')
        if not peer['removed']:
            addresses.add(peer['address'])
        keys.add(peer['public_key'])
    return ledger


@contextmanager
def locked_ledger(config):
    path = Path(config['ledger_path'])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not any(p.is_symlink() for p in (path.parent, *path.parent.parents)))
    require(path.parent.stat().st_uid == os.geteuid() and not path.parent.stat().st_mode & 0o022, 'GATEWAY_LEDGER_UNSAFE')
    lock = path.with_suffix('.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        private(lock)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield validate_ledger(config, json.loads(private(path)) if path.exists() else {})
    finally:
        os.close(fd)


def restore(config, runner=run):
    """Root-only startup, never exposed through the API's sudo wrapper."""
    validate(config)
    with locked_ledger(config) as ledger:
        apply(config, ledger, runner)
        return {'status': 'succeeded', 'restored_peers': sum(not peer['removed'] for peer in ledger.values())}


def execute(config, request, runner=run, apply_fn=apply, now=time.time):
    pool, server_ip = validate(config)
    require(set(request) == {'action', 'target_id', 'generation', 'public_key'})
    require(request['action'] in ('register', 'remove', 'verify') and ID.fullmatch(request['target_id']))
    require(type(request['generation']) is int and request['generation'] > 0)
    public_key = key(request['public_key'])
    ledger_path = Path(config['ledger_path'])
    with locked_ledger(config) as ledger:
        ident = request['target_id'] + ':' + str(request['generation'])
        peer = ledger.get(ident)
        if peer:
            require(peer['public_key'] == public_key, 'GATEWAY_PEER_CONFLICT')
        action = request['action']
        if action == 'register':
            require(not peer or not peer.get('removed'), 'GATEWAY_PEER_REVOKED')
            require(not any(k != ident and p['public_key'] == public_key for k, p in ledger.items()), 'GATEWAY_KEY_REUSED')
            if not peer:
                used = {p['address'] for p in ledger.values() if not p['removed']}
                address = next((str(ip) + '/32' for ip in pool.hosts() if ip != server_ip and str(ip) + '/32' not in used), None)
                require(address, 'GATEWAY_POOL_EXHAUSTED')
                if any(p['address'] == address for p in ledger.values()):
                    # Removed is a durable intent, not proof of peer removal.
                    # Reclaim only after native readback confirms old peers and
                    # unused routes are absent, while holding the ledger lock.
                    verify_applied(config, ledger, runner)
                peer = {'public_key': public_key, 'address': address, 'removed': False}
                ledger[ident] = peer
                save(ledger_path, json.dumps(ledger, sort_keys=True))
            apply_fn(config, ledger, runner)
        elif action == 'remove':
            require(peer, 'GATEWAY_PEER_UNKNOWN')
            peer['removed'] = True
            save(ledger_path, json.dumps(ledger, sort_keys=True))
            apply_fn(config, ledger, runner)
        else:
            require(peer and not peer['removed'], 'GATEWAY_PEER_UNKNOWN')
            if apply_fn is apply:
                verify_applied(config, ledger, runner)
        reachable = False
        if not peer['removed']:
            for row in runner(['wg', 'show', config['interface'], 'latest-handshakes']).splitlines():
                fields = row.split()
                if len(fields) == 2 and fields[0] == public_key:
                    reachable = 0 < int(fields[1]) <= now() and now() - int(fields[1]) <= 180
        public = server_public_key(config, runner)
        return {'status': 'succeeded', 'reachable': reachable, 'tunnel': {
            'server_public_key': public, 'endpoint': config['endpoint'], 'address': peer['address'],
            'allowed_ips': str(server_ip) + '/32'}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--request', help='Private JSON request file; otherwise read stdin')
    parser.add_argument('--restore', action='store_true', help='Restore persisted peers during root-owned service startup')
    parser.add_argument('--verify-forwarding', action='store_true', help='Read-only verification of both forwarding barriers')
    args = parser.parse_args()
    try:
        require(os.geteuid() == 0, 'GATEWAY_ROOT_REQUIRED')
        config = json.loads(private(args.config))
        if args.verify_forwarding:
            require(not args.restore and not args.request, 'GATEWAY_REQUEST_INVALID')
            validate(config)
            verify_forwarding(config, run)
            print(json.dumps({'status': 'succeeded', 'forwarding_isolated': True}))
            return 0
        if args.restore:
            require(not args.request, 'GATEWAY_REQUEST_INVALID')
            print(json.dumps(restore(config)))
            return 0
        if args.request:
            request_path = Path(args.request)
            request_root = Path(config.get('request_root', '/var/lib/railshot/personal/gateway'))
            require(request_path.is_absolute() and request_path.parent == request_root
                    and '..' not in request_path.parts, 'GATEWAY_REQUEST_PATH_INVALID')
            raw = private(request_path, owner=config.get('request_owner_uid', os.geteuid()), max_bytes=16384).encode()
        else:
            raw = sys.stdin.buffer.read(16385)
        require(len(raw) <= 16384)
        result = execute(config, json.loads(raw))
    except Exception:
        print(json.dumps({'status': 'failed', 'error': 'GATEWAY_OPERATION_FAILED'}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
