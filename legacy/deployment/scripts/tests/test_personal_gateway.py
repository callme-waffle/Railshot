"""Gateway reconciliation using native-command-shaped fixtures, without real links."""
import base64
import importlib.util
import json
from pathlib import Path
import time

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('personal_gateway_direct_tests', ROOT / 'deployment/scripts/personal_wireguard.py')
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)
SERVER = base64.b64encode(bytes(range(32))).decode()
CLIENT = base64.b64encode(bytes(range(1, 33))).decode()
OTHER = base64.b64encode(bytes(range(2, 34))).decode()


class Native:
    def __init__(self, config):
        self.config, self.calls = config, []
        self.exists, self.kind, self.public = False, 'wireguard', SERVER
        self.peers, self.routes, self.rules = {}, {}, []
        self.guard_rules = []
        self.address, self.port = config['server_address'], config['listen_port']

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        interface = self.config['interface']
        if args == ['ip', '-j', '-d', 'link', 'show']:
            return json.dumps([{'ifname': interface, 'linkinfo': {'info_kind': self.kind}}] if self.exists else [])
        if args == ['wg', 'pubkey']:
            assert kwargs['input'].strip() == SERVER
            return SERVER
        if args[:3] == ['wg', 'show', interface]:
            assert self.exists
            return {'public-key': self.public, 'listen-port': str(self.port),
                'allowed-ips': '\n'.join(public + '\t' + address for public, address in self.peers.items()),
                'latest-handshakes': '\n'.join(public + '\t' + str(int(time.time())) for public in self.peers)}[args[3]]
        if args == ['ip', '-j', '-4', 'address', 'show', 'dev', interface]:
            address, prefix = self.address.split('/')
            return json.dumps([{'addr_info': [{'family': 'inet', 'local': address, 'prefixlen': int(prefix)}]}])
        if args == ['iptables', '-w', '-S', 'FORWARD']:
            return '\n'.join(['-P FORWARD ACCEPT', *self.rules])
        if args[:5] == ['iptables', '-w', '-I', 'FORWARD', '1']:
            self.rules.insert(0, '-A FORWARD ' + ' '.join(args[5:])); return ''
        if args == ['iptables', '-w', '-t', 'mangle', '-S', 'FORWARD']:
            return '\n'.join(['-P FORWARD ACCEPT', *self.guard_rules])
        if args[:7] == ['iptables', '-w', '-t', 'mangle', '-I', 'FORWARD', '1']:
            self.guard_rules.insert(0, '-A FORWARD ' + ' '.join(args[7:])); return ''
        if args[:2] == ['wg-quick', 'up'] or args[:3] == ['systemctl', 'enable', '--now']:
            if args[0] == 'wg-quick':
                assert not self.exists
            self.exists = True
            return ''
        if args[:2] == ['wg-quick', 'strip']:
            return '\n'.join(line for line in Path(args[2]).read_text().splitlines()
                             if not line.startswith(('Address', 'Table', 'PreUp', 'PostUp')))
        if args[:3] == ['wg', 'syncconf', interface]:
            peers, public = {}, None
            for line in Path(args[3]).read_text().splitlines():
                if line.startswith('PublicKey = '):
                    public = line.split(' = ')[1]
                elif line.startswith('AllowedIPs = '):
                    peers[public] = line.split(' = ')[1]
            self.peers = peers
            return ''
        if args[:6] == ['ip', '-j', '-4', 'route', 'show', 'exact']:
            return json.dumps([{'dst': args[6], 'dev': self.routes[args[6]]}] if args[6] in self.routes else [])
        if args == ['ip', '-j', '-4', 'route', 'show', 'dev', interface]:
            return json.dumps([{'dst': address.removesuffix('/32'), 'dev': device} for address, device in self.routes.items() if device == interface])
        if args[:4] == ['ip', '-4', 'route', 'replace']:
            self.routes[args[4]] = args[6]; return ''
        if args[:4] == ['ip', '-4', 'route', 'show']:
            return 'owned route' if self.routes.get(args[4]) == args[6] else ''
        if args[:4] == ['ip', '-4', 'route', 'del']:
            assert self.routes[args[4]] == args[6]
            del self.routes[args[4]]; return ''
        raise AssertionError(args)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    private = tmp_path / 'private.key'
    private.write_text(SERVER); private.chmod(0o600)
    wg = tmp_path / 'wireguard'; wg.mkdir(mode=0o700)
    monkeypatch.setattr(gateway, 'WIREGUARD_DIRECTORY', wg)
    config = {'version': 1, 'lifecycle': 'direct', 'interface': 'railshotwg', 'address_pool': '10.253.240.0/24',
        'server_address': '10.253.240.1/32', 'endpoint': '10.113.0.5:51820', 'listen_port': 51820,
        'private_key_file': str(private), 'ledger_path': str(tmp_path / 'ledger.json')}
    return config, Native(config), wg


def request(action='register', target='personal-one', public=CLIENT):
    return {'action': action, 'target_id': target, 'generation': 1, 'public_key': public}


def writes(native):
    return [args for args in native.calls if args[0] == 'systemctl' or args[:2] in
            (['wg-quick', 'up'], ['wg', 'syncconf']) or '-I' in args or 'replace' in args or 'del' in args]


def test_direct_register_repeat_verify_and_remove_read_back_native_state(prepared):
    config, native, wg = prepared
    first = gateway.execute(config, request(), native)
    second = gateway.execute(config, request(), native)
    assert first == second and first['reachable'] is True
    assert sum(args[:2] == ['wg-quick', 'up'] for args in native.calls) == 1
    assert not any(args[0] == 'systemctl' for args in native.calls)
    native.calls.clear()
    assert gateway.execute(config, request('verify'), native)['reachable'] is True
    assert not writes(native)
    assert len(native.rules) == 2
    assert len(native.guard_rules) == 2
    assert 'Table = off' in (wg / 'railshotwg.conf').read_text()
    gateway.execute(config, request('remove'), native)
    assert native.peers == {} and native.routes == {}
    assert json.loads(Path(config['ledger_path']).read_text())['personal-one:1']['removed'] is True


def test_restore_reconstructs_live_peers_routes_and_forwarding_from_persistent_ledger(prepared):
    config, native, _ = prepared
    gateway.execute(config, request(), native)
    gateway.execute(config, request(target='personal-two', public=OTHER), native)
    gateway.execute(config, request('remove'), native)
    restarted = Native(config)
    assert gateway.restore(config, restarted) == {'status': 'succeeded', 'restored_peers': 1}
    assert restarted.peers == {OTHER: '10.253.240.3/32'}
    assert restarted.routes == {'10.253.240.3/32': 'railshotwg'}
    assert set(restarted.rules) == set(gateway.forwarding_rules(config))
    assert set(restarted.guard_rules) == set(gateway.forwarding_rules(config))
    gateway.restore(config, restarted)
    assert sum(args[:2] == ['wg-quick', 'up'] for args in restarted.calls) == 1


@pytest.mark.parametrize('conflict', ['no-marker', 'different-key', 'not-wireguard'])
def test_existing_interface_never_adopted_without_exact_ownership(prepared, conflict):
    config, native, wg = prepared
    native.exists = True
    if conflict != 'no-marker':
        gateway.save(wg / 'railshotwg.conf', gateway.MARKER)
    if conflict == 'different-key':
        native.public = OTHER
    if conflict == 'not-wireguard':
        native.kind = 'dummy'
    with pytest.raises(ValueError):
        gateway.restore(config, native)
    assert not writes(native)


def test_default_systemd_lifecycle_is_preserved(prepared):
    config, native, _ = prepared
    config.pop('lifecycle')
    gateway.execute(config, request(), native)
    assert ['systemctl', 'enable', '--now', 'wg-quick@railshotwg'] in native.calls
    assert not any(args[:2] == ['wg-quick', 'up'] for args in native.calls)


@pytest.mark.parametrize('mode', [None, 'systemd'])
def test_systemd_cold_boot_restores_peer_routes_without_gateway_helper(prepared, mode):
    config, native, wg = prepared
    if mode is None:
        config.pop('lifecycle')
    else:
        config['lifecycle'] = mode
    gateway.execute(config, request(), native)
    gateway.execute(config, request(target='personal-two', public=OTHER), native)
    gateway.execute(config, request('remove'), native)
    stored = (wg / 'railshotwg.conf').read_text()
    assert 'Table =' not in stored
    # Model a cold boot's wg-quick configuration handling independently of
    # gateway.apply/restore: absent Table=off installs every AllowedIPs route.
    rebooted = Native(config)
    rebooted.exists = True
    public = None
    for line in stored.splitlines():
        if line.startswith('PublicKey = '):
            public = line.split(' = ')[1]
        elif line.startswith('AllowedIPs = '):
            address = line.split(' = ')[1]
            rebooted.peers[public] = address
            if 'Table = off' not in stored:
                rebooted.routes[address] = config['interface']
    assert 'PostUp = iptables' in stored
    assert 'PreUp = iptables -w -t mangle -I FORWARD 1 -i %i -j DROP' in stored
    rebooted.rules = gateway.forwarding_rules(config)
    rebooted.guard_rules = gateway.forwarding_rules(config)
    assert rebooted.peers == {OTHER: '10.253.240.3/32'}
    assert rebooted.routes == {'10.253.240.3/32': 'railshotwg'}
    assert gateway.execute(config, request('verify', target='personal-two', public=OTHER), rebooted)['reachable'] is True
    assert not writes(rebooted)


@pytest.mark.parametrize('drift', ['peer', 'route', 'port', 'address', 'firewall', 'early_guard'])
def test_verification_refuses_drift_without_mutating_it(prepared, drift):
    config, native, _ = prepared
    gateway.execute(config, request(), native)
    if drift == 'peer': native.peers[OTHER] = '10.253.240.99/32'
    if drift == 'route': native.routes.clear()
    if drift == 'port': native.port += 1
    if drift == 'address': native.address = '10.253.240.1/24'
    if drift == 'firewall': native.rules.insert(0, '-A FORWARD -j ACCEPT')
    if drift == 'early_guard': native.guard_rules.insert(0, '-A FORWARD -j ACCEPT')
    native.calls.clear()
    with pytest.raises(ValueError):
        gateway.execute(config, request('verify'), native)
    assert not writes(native)


def test_restore_places_drop_ahead_of_existing_accept_and_keeps_other_rules(prepared):
    config, native, _ = prepared
    native.rules = ['-A FORWARD -j ACCEPT']
    gateway.restore(config, native)
    assert native.rules[-1] == '-A FORWARD -j ACCEPT'
    assert set(native.rules[:2]) == set(gateway.forwarding_rules(config))


def test_early_guard_precedes_peer_activation_and_preserves_other_rules(prepared):
    config, native, _ = prepared
    native.guard_rules = ['-A FORWARD -i customer0 -j ACCEPT']
    gateway.execute(config, request(), native)
    activation = next(i for i, args in enumerate(native.calls) if args[:2] == ['wg-quick', 'up'])
    writes_before = [args for args in native.calls[:activation] if '-I' in args]
    assert writes_before[:2] == [
        ['iptables', '-w', '-t', 'mangle', '-I', 'FORWARD', '1', direction, config['interface'], '-j', 'DROP']
        for direction in ('-i', '-o')]
    assert native.guard_rules[-1] == '-A FORWARD -i customer0 -j ACCEPT'
    assert not any(chain in args for args in native.calls if args[0] == 'iptables'
                   for chain in ('INPUT', 'OUTPUT', 'PREROUTING', 'POSTROUTING'))


def test_kube_proxy_filter_prepend_does_not_displace_early_barrier(prepared):
    config, native, _ = prepared
    gateway.execute(config, request(), native)
    guards = list(native.guard_rules)
    native.rules[:0] = ['-A FORWARD -j KUBE-PROXY-FIREWALL', '-A FORWARD -j KUBE-FORWARD']
    # The early barrier remains intact while the strict filter diagnostic still
    # detects order drift. Repairing filter order never opens either direction.
    gateway.verify_forwarding_guard(config, native)
    with pytest.raises(ValueError, match='GATEWAY_FORWARDING_UNVERIFIED'):
        gateway.verify_forwarding(config, native)
    gateway.ensure_forwarding(config, native)
    assert native.guard_rules == guards
    assert native.rules[2:4] == ['-A FORWARD -j KUBE-PROXY-FIREWALL', '-A FORWARD -j KUBE-FORWARD']


def test_unsupported_mangle_hook_blocks_peer_activation(prepared):
    config, native, _ = prepared
    def unsupported(args, **kwargs):
        if 'mangle' in args:
            raise ValueError('GATEWAY_COMMAND_FAILED')
        return native(args, **kwargs)
    with pytest.raises(ValueError, match='GATEWAY_COMMAND_FAILED'):
        gateway.execute(config, request(), unsupported)
    assert not native.exists and not native.peers
    assert not any(args[:2] in (['wg-quick', 'up'], ['wg', 'syncconf']) for args in native.calls)


def test_route_on_another_interface_is_not_stolen(prepared):
    config, native, _ = prepared
    native.routes['10.253.240.2/32'] = 'customer0'
    with pytest.raises(ValueError, match='GATEWAY_ROUTE_ALREADY_OWNED'):
        gateway.execute(config, request(), native)
    assert native.routes == {'10.253.240.2/32': 'customer0'}


def test_corrupt_ledger_blocks_restore_before_native_changes(prepared):
    config, native, _ = prepared
    gateway.save(config['ledger_path'], json.dumps({'personal-one:1': {
        'public_key': CLIENT, 'address': '10.0.0.1/32', 'removed': False}}))
    with pytest.raises(ValueError, match='GATEWAY_LEDGER_INVALID'):
        gateway.restore(config, native)
    assert not native.calls


def test_verified_tombstone_address_reuse_preserves_late_request_and_restore_isolation(prepared):
    config, native, _ = prepared
    old = gateway.execute(config, request(), native)
    gateway.execute(config, request('remove'), native)
    new = gateway.execute(config, request(target='personal-two', public=OTHER), native)
    assert new['tunnel']['address'] == old['tunnel']['address']
    before = dict(native.peers), dict(native.routes)
    # A late old remove rebuilds the ledger, but cannot remove the reused route.
    gateway.execute(config, request('remove'), native)
    assert (native.peers, native.routes) == before
    for stale in (request(), request('verify')):
        with pytest.raises(ValueError, match='GATEWAY_PEER_REVOKED|GATEWAY_PEER_UNKNOWN'):
            gateway.execute(config, stale, native)
    with pytest.raises(ValueError, match='GATEWAY_PEER_CONFLICT'):
        gateway.execute(config, request('remove', public=OTHER), native)
    with pytest.raises(ValueError, match='GATEWAY_KEY_REUSED'):
        gateway.execute(config, request(target='personal-three'), native)
    ledger = json.loads(Path(config['ledger_path']).read_text())
    assert ledger['personal-one:1']['removed'] is True
    assert ledger['personal-two:1']['removed'] is False
    # Sorted ledger order cannot make a tombstone delete an active route.
    gateway.save(config['ledger_path'], json.dumps(dict(reversed(list(ledger.items())))))
    restarted = Native(config)
    gateway.restore(config, restarted)
    assert (restarted.peers, restarted.routes) == before


@pytest.mark.parametrize('residual', ['peer', 'route'])
def test_unverified_removal_never_reassigns_an_address(prepared, residual):
    config, native, _ = prepared
    original = gateway.execute(config, request(), native)
    def interrupted(*args):
        if residual == 'route':
            native.peers.clear()
        raise ValueError('LOST_REMOVAL_RESPONSE')
    with pytest.raises(ValueError, match='LOST_REMOVAL_RESPONSE'):
        gateway.execute(config, request('remove'), native, interrupted)
    before = Path(config['ledger_path']).read_bytes(), dict(native.peers), dict(native.routes)
    native.calls.clear()
    with pytest.raises(ValueError, match='GATEWAY_PEERS_UNVERIFIED|GATEWAY_ROUTES_UNVERIFIED'):
        gateway.execute(config, request(target='personal-two', public=OTHER), native)
    assert not writes(native)
    assert (Path(config['ledger_path']).read_bytes(), native.peers, native.routes) == before
    # Explicit removal readback succeeds before the old address becomes free.
    gateway.execute(config, request('remove'), native)
    assert gateway.execute(config, request(target='personal-two', public=OTHER), native)['tunnel']['address'] == original['tunnel']['address']


@pytest.mark.parametrize('prefix, cycles', [(28, 30), (24, 260)])
def test_normal_registration_removal_cycles_exceed_pool_capacity_without_erasing_history(prepared, prefix, cycles):
    config, native, _ = prepared
    config['address_pool'] = f'10.253.240.0/{prefix}'
    for index in range(cycles):
        public = base64.b64encode((index + 1000).to_bytes(32, 'big')).decode()
        body = request(target='personal-cycle', public=public)
        body['generation'] = index + 1
        result = gateway.execute(config, body, native)
        assert result['tunnel']['address'] == '10.253.240.2/32'
        gateway.execute(config, {**body, 'action': 'remove'}, native)
    ledger = json.loads(Path(config['ledger_path']).read_text())
    assert len(ledger) == cycles and all(peer['removed'] for peer in ledger.values())
    assert native.peers == {} and native.routes == {}


def test_ledger_still_rejects_two_active_owners_for_one_address(prepared):
    config, _, _ = prepared
    ledger = {'personal-one:1': {'public_key': CLIENT, 'address': '10.253.240.2/32', 'removed': False},
              'personal-two:1': {'public_key': OTHER, 'address': '10.253.240.2/32', 'removed': False}}
    with pytest.raises(ValueError, match='GATEWAY_LEDGER_INVALID'):
        gateway.validate_ledger(config, ledger)
