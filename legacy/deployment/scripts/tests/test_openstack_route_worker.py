import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid


SPEC = importlib.util.spec_from_file_location(
    "openstack_route_worker", Path(__file__).resolve().parents[1] / "openstack_route_worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class OctaviaCLI:
    """Only the worker's CLI subset, with observed OSC show-field formatting.

    Controller readbacks encode relations/tags as newline strings, project IDs
    as 32 hex, booleans as JSON booleans and absent pool references as null/"".
    Operating status deliberately stays OFFLINE: deployments follow routing.
    """

    def __init__(self, config):
        self.config = config
        self.objects = {kind: {} for kind in worker.KINDS}
        self.calls = []
        self.fail_create = None
        self.apply_failed_create = True
        self.fail_enable = False
        self.network_calls, self.network_rules = [], {}
        self.fail_network_create = False
        self.apply_network_create = True
        network = config["network"]
        self.group = {"id": network["runtime_security_group_id"], "project_id": network["runtime_project_id"]}
        self.ports = {}
        for role in ("runtime", "amphora"):
            self.ports[network[role + "_port_id"]] = {
                "id": network[role + "_port_id"], "device_id": network[role + "_server_id"],
                "project_id": network["runtime_project_id"] if role == "runtime" else config["project_id"],
                "device_owner": "compute:nova", "network_id": str(uuid.UUID(int=30)),
                "fixed_ips": [{"subnet_id": config["member_subnet_id"], "ip_address":
                    config["runtime_private_address"] if role == "runtime" else network["amphora_private_address"]}],
                "security_group_ids": [network["runtime_security_group_id"]] if role == "runtime" else []}
        self.lb = {"id": config["loadbalancer_id"], "project_id": config["project_id"],
                   "admin_state_up": True, "provisioning_status": "ACTIVE"}
        self.listener = {**self.lb, "id": config["listener_id"], "default_pool_id": None,
                         "protocol": "TERMINATED_HTTPS", "protocol_port": 443,
                         "loadbalancers": config["loadbalancer_id"],
                         "default_tls_container_ref": str(uuid.UUID(int=9))}

    def __call__(self, arguments, service=("loadbalancer",)):
        if not service:
            return self.network(arguments)
        assert service == ("loadbalancer",)
        self.calls.append(list(arguments))
        if arguments[0] == "show":
            return copy.deepcopy(self.lb)
        command, action, *tail = arguments
        if command == "listener":
            return copy.deepcopy(self.listener)
        kind = {"healthmonitor": "monitor", "l7policy": "policy", "l7rule": "rule"}.get(command, command)
        options, positionals = {}, []
        iterator = iter(tail)
        for token in iterator:
            if token in ("--wait", "--enable", "--disable", "--disable-backup"):
                options[token] = True
            elif token.startswith("-"):
                options[token] = next(iterator)
            else:
                positionals.append(token)
        objects = self.objects[kind]
        if action == 'delete':
            del objects[positionals[-1]]
            return {}
        if action == "list":
            rows = [value for value in objects.values()
                    if ("--tags" not in options or options["--tags"] in value["tags"].splitlines()) and
                    (kind not in ("member", "rule") or value["parent"] == positionals[0])]
            return copy.deepcopy(rows)
        if action == "show":
            value = copy.deepcopy(objects[positionals[-1]])
            if kind in ("member", "rule"):
                assert value["parent"] == positionals[0]
            if kind == "pool":
                value["members"] = "\n".join(item["id"] for item in self.objects["member"].values()
                                              if item["parent"] == value["id"])
                value["healthmonitor_id"] = next((item["id"] for item in self.objects["monitor"].values()
                                                 if item["pools"] == value["id"]), "")
            if kind == "policy":
                value["rules"] = "\n".join(item["id"] for item in self.objects["rule"].values()
                                            if item["parent"] == value["id"])
            return value
        if action == "set":
            assert kind == "policy" and set(options) == {"--enable", "--wait"}
            if self.fail_enable:
                raise worker.RouteError("provider_response_unknown", "unknown")
            objects[positionals[0]]["admin_state_up"] = True
            return {}
        assert action == "create"
        value = {"id": str(uuid.uuid4()), "project_id": self.config["project_id"],
                 "provisioning_status": "ACTIVE", "operating_status": "OFFLINE",
                 "admin_state_up": "--enable" in options, "tags": options["--tag"]}
        for flag in ("name", "protocol", "address", "subnet-id", "http-method", "http-version",
                     "domain-name", "url-path", "expected-codes", "type", "compare-type", "value",
                     "action", "redirect-pool", "lb-algorithm"):
            if "--" + flag in options:
                key = "redirect_pool_id" if flag == "redirect-pool" else flag.replace("-", "_")
                value[key] = options["--" + flag]
        for flag in ("delay", "timeout", "max-retries", "protocol-port", "weight"):
            if "--" + flag in options:
                value[flag.replace("-", "_")] = int(options["--" + flag])
        if kind == "pool":
            assert "--listener" not in options
            assert options["--loadbalancer"] == self.config["loadbalancer_id"]
            value["loadbalancers"] = options["--loadbalancer"]
        if kind in ("member", "rule"):
            value["parent"] = positionals[0]
            value["backup" if kind == "member" else "invert"] = False
        if kind == "monitor":
            value["pools"] = positionals[0]
        if kind == "policy":
            assert options["--disable"]
            value["listener_id"] = positionals[0]
        fails = self.fail_create == kind
        if not fails or self.apply_failed_create:
            objects[value["id"]] = value
        if fails:
            self.fail_create = None
            raise worker.RouteError("provider_response_unknown", "unknown")
        return copy.deepcopy(value)

    def mutations(self):
        return [args for args in self.calls if len(args) > 1 and args[1] in ("create", "set", "delete")]

    def network(self, arguments):
        self.network_calls.append(list(arguments))
        if arguments[:2] == ["port", "show"]:
            return copy.deepcopy(self.ports[arguments[2]])
        if arguments[:3] == ["security", "group", "show"]:
            return copy.deepcopy(self.group)
        assert arguments[:3] == ["security", "group", "rule"]
        action = arguments[3]
        if action == 'delete':
            del self.network_rules[arguments[4]]
            return {}
        if action == "list":
            return [{"ID": key} for key in self.network_rules]
        if action == "show":
            return copy.deepcopy(self.network_rules[arguments[4]])
        assert action == "create"
        assert arguments[4] == self.group["id"]
        options = {}
        iterator = iter(arguments[5:])
        for token in iterator:
            options[token] = True if token == "--ingress" else next(iterator)
        rule = {"id": str(uuid.uuid4()), "security_group_id": arguments[4],
                "project_id": options["--project"], "direction": "ingress", "protocol": options["--protocol"],
                "ether_type": options["--ethertype"], "remote_ip_prefix": options["--remote-ip"],
                "port_range_min": int(options["--dst-port"]), "port_range_max": int(options["--dst-port"]),
                "remote_group_id": None, "remote_address_group_id": None, "description": options["--description"]}
        if not self.fail_network_create or self.apply_network_create:
            self.network_rules[rule["id"]] = rule
        if self.fail_network_create:
            self.fail_network_create = False
            raise worker.RouteError("provider_response_unknown", "unknown")
        return copy.deepcopy(rule)

    def network_mutations(self):
        return [args for args in self.network_calls if args[:3] == ["security", "group", "rule"]
                and args[3] in ("create", "delete")]


class RouteWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = {"version": 1, "project_id": "2a48957bf4b140fcb832de30baf156e8",
                       "loadbalancer_id": str(uuid.UUID(int=1)), "listener_id": str(uuid.UUID(int=2)),
                       "member_subnet_id": str(uuid.UUID(int=3)), "runtime_private_address": "10.0.0.17",
                       "base_domain": "railshot.io", "state_dir": str(Path(self.temporary.name).resolve())}
        self.config["network"] = {"runtime_project_id": "8de0a5b10e974e71b575cd16ff55d6df",
            "runtime_server_id": str(uuid.UUID(int=21)), "runtime_port_id": str(uuid.UUID(int=22)),
            "runtime_security_group_id": str(uuid.UUID(int=23)), "amphora_port_id": str(uuid.UUID(int=24)),
            "amphora_private_address": "10.0.0.40", "amphora_server_id": str(uuid.UUID(int=25))}
        self.request = {"application_id": "app-" + "a" * 24, "hostname": "first.railshot.io",
                        "private_address": "10.0.0.17", "node_port": 30123, "health_path": "/healthz"}
        self.cli = OctaviaCLI(self.config)
        for target, value in (("ROOT_UID", os.getuid()), ("verify_certificate", lambda *_: None)):
            mock = patch.object(worker, target, value)
            mock.start()
            self.addCleanup(mock.stop)

    def configure(self, request=None):
        return worker.configure(self.config, request or self.request, self.cli)

    def lifecycle(self, action, operation='lifecycle-plan', expected=None):
        envelope = {'operation': operation, 'action': action, 'request': {
            k: self.request[k] for k in ('application_id', 'hostname', 'node_port')}}
        if expected is not None:
            envelope['expected'] = expected
        return worker.lifecycle(self.config, envelope, self.cli)

    def test_lifecycle_stop_start_delete_preserves_other_app_and_shared_listener(self):
        first = self.configure()
        other_request = {**self.request, 'application_id': 'app-' + 'b' * 24,
                         'hostname': 'other.railshot.io', 'node_port': 31002}
        other = self.configure(other_request)
        shared = copy.deepcopy((self.cli.lb, self.cli.listener, self.cli.ports, self.cli.group))
        app_path = Path(self.config['state_dir']) / (self.request['application_id'] + '.json')
        raw = app_path.read_bytes(); mutations = len(self.cli.mutations())
        plan = self.lifecycle('stop')
        self.lifecycle('stop', 'lifecycle-validate', plan['expected'])
        self.assertEqual(app_path.read_bytes(), raw)
        self.assertEqual(len(self.cli.mutations()), mutations)
        receipt = self.lifecycle('stop', 'lifecycle-execute', plan['expected'])
        self.assertEqual(receipt['resources'], {})
        for kind, resource_id in other['resources'].items():
            self.assertEqual(set(self.cli.objects[kind]), {resource_id})
        self.assertEqual(set(self.cli.network_rules), {other['network_rule_id']})
        plan = self.lifecycle('start')
        restored = self.lifecycle('start', 'lifecycle-execute', plan['expected'])
        self.assertEqual(set(restored['resources']), set(worker.KINDS))
        self.assertTrue(all(restored['resources'][kind] != first['resources'][kind] for kind in worker.KINDS))
        plan = self.lifecycle('delete')
        self.lifecycle('delete', 'lifecycle-execute', plan['expected'])
        self.assertEqual((self.cli.lb, self.cli.listener, self.cli.ports, self.cli.group), shared)
        with self.assertRaises(worker.RouteError):
            self.lifecycle('start')

    def test_lifecycle_foreign_tag_and_partial_delete_fail_closed_without_replay(self):
        result = self.configure()
        plan = self.lifecycle('delete')
        monitor = self.cli.objects['monitor'][result['resources']['monitor']]
        original = monitor['tags']; monitor['tags'] = 'foreign-owner'
        mutations = len(self.cli.mutations())
        with self.assertRaises(worker.RouteError):
            self.lifecycle('delete', 'lifecycle-execute', plan['expected'])
        self.assertEqual(len(self.cli.mutations()), mutations)
        monitor['tags'] = original
        raw_call = self.cli
        def fail(arguments, service=('loadbalancer',)):
            if arguments[:2] == ['l7policy', 'delete']:
                raise worker.RouteError('uncertain delete', 'unknown')
            return raw_call(arguments, service=service)
        envelope = {'operation': 'lifecycle-execute', 'action': 'delete', 'expected': plan['expected'],
                    'request': {k: self.request[k] for k in ('application_id', 'hostname', 'node_port')}}
        with self.assertRaises(worker.RouteError) as caught:
            worker.lifecycle(self.config, envelope, fail)
        self.assertEqual(caught.exception.status, 'unknown')
        mutations = len(self.cli.mutations())
        with self.assertRaises(worker.RouteError):
            self.lifecycle('delete')
        with self.assertRaises(worker.RouteError):
            self.configure()
        self.assertEqual(len(self.cli.mutations()), mutations)

    def test_lifecycle_before_first_deployment_is_readonly_noop(self):
        plan = self.lifecycle('delete')
        self.assertEqual(plan['resources'], {})
        self.lifecycle('delete', 'lifecycle-execute', plan['expected'])
        self.assertFalse(self.cli.mutations())
        self.assertFalse(self.cli.network_mutations())

    def test_verified_deleted_app_releases_hostname_and_nodeport_to_another_app(self):
        first = self.configure()
        plan = self.lifecycle('delete')
        self.lifecycle('delete', 'lifecycle-execute', plan['expected'])
        old = Path(self.config['state_dir']) / (self.request['application_id'] + '.json')
        tombstone = old.read_bytes()
        second = self.configure({**self.request, 'application_id': 'app-' + 'b' * 24})
        self.assertEqual(second['status'], 'configured')
        self.assertEqual(second['hostname'], first['hostname'])
        self.assertEqual(second['node_port'], first['node_port'])
        self.assertEqual(old.read_bytes(), tombstone)
        self.assertTrue(all(first['resources'][kind] != second['resources'][kind] for kind in worker.KINDS))

    def test_stopped_unknown_and_unanchored_deleted_records_retain_reservations(self):
        self.configure()
        path = Path(self.config['state_dir']) / (self.request['application_id'] + '.json')
        original = worker.read_private(path)
        for status in ('stopped', 'unknown', 'deleted'):
            with self.subTest(status=status):
                worker.save(path, {**original, 'status': status})
                before = copy.deepcopy((self.cli.calls, self.cli.network_calls))
                with self.assertRaisesRegex(worker.RouteError, '^application_route_conflict$'):
                    self.configure({**self.request, 'application_id': 'app-' + 'b' * 24})
                self.assertEqual((self.cli.calls, self.cli.network_calls), before)
                self.assertFalse((path.parent / ('app-' + 'b' * 24 + '.json')).exists())

    def test_deleted_reservation_requires_exact_successful_plan_and_receipt_anchor(self):
        self.configure()
        plan = self.lifecycle('delete')
        self.lifecycle('delete', 'lifecycle-execute', plan['expected'])
        path = Path(self.config['state_dir']) / ('lifecycle-' + plan['expected']['plan_id'] + '.json')
        original = worker.read_private(path)
        for change in ('receipt', 'hash', 'snapshot', 'config'):
            with self.subTest(change=change):
                value = copy.deepcopy(original)
                if change == 'receipt':
                    value['receipt']['application_id'] = 'app-' + 'c' * 24
                elif change == 'hash':
                    value['plan_sha256'] = '0' * 64
                elif change == 'snapshot':
                    value['plan']['snapshot']['record']['fingerprint'] = '0' * 64
                    value['plan_sha256'] = worker.lifecycle_hash(value['plan'])
                else:
                    value['plan']['snapshot']['config_sha256'] = '0' * 64
                    value['plan_sha256'] = worker.lifecycle_hash(value['plan'])
                worker.save(path, value)
                before = copy.deepcopy((self.cli.calls, self.cli.network_calls))
                with self.assertRaisesRegex(worker.RouteError, '^application_route_conflict$'):
                    self.configure({**self.request, 'application_id': 'app-' + 'b' * 24})
                self.assertEqual((self.cli.calls, self.cli.network_calls), before)
        worker.save(path, original)

    def test_successful_lifecycle_replay_returns_exact_receipt_without_provider_or_state_writes(self):
        self.configure()
        directory = Path(self.config['state_dir'])
        previous = []
        def no_provider(*_args, **_kwargs):
            self.fail('A successful replay must not make another provider call')
        for action in ('stop', 'start', 'delete'):
            plan = self.lifecycle(action)
            result = self.lifecycle(action, 'lifecycle-execute', plan['expected'])
            envelope = {'operation': 'lifecycle-execute', 'action': action, 'expected': plan['expected'],
                        'request': {key: self.request[key] for key in ('application_id', 'hostname', 'node_port')}}
            previous.append((envelope, result))
            before = {p.name: p.read_bytes() for p in directory.glob('*.json')}
            for saved, receipt in previous:
                self.assertEqual(worker.lifecycle(self.config, saved, no_provider), receipt)
            self.assertEqual({p.name: p.read_bytes() for p in directory.glob('*.json')}, before)
        for change in ('action', 'hostname', 'node_port', 'hash', 'config'):
            with self.subTest(change=change):
                altered, config = copy.deepcopy(envelope), copy.deepcopy(self.config)
                if change == 'action':
                    altered['action'] = 'stop'
                elif change == 'hash':
                    altered['expected']['plan_sha256'] = '0' * 64
                elif change == 'config':
                    config['listener_id'] = str(uuid.UUID(int=44))
                else:
                    altered['request'][change] = 'other.railshot.io' if change == 'hostname' else 31001
                with self.assertRaisesRegex(worker.RouteError, '^lifecycle_plan_stale$'):
                    worker.lifecycle(config, altered, no_provider)
                self.assertEqual({p.name: p.read_bytes() for p in directory.glob('*.json')}, before)

    def test_input_validation_before_provider_calls(self):
        for key, value in (("application_id", "../app"), ("hostname", "first.evil.test"),
                           ("hostname", "a.b.railshot.io"), ("private_address", "10.0.0.18"),
                           ("private_address", "127.0.0.1"), ("node_port", True), ("node_port", 29999),
                           ("health_path", "/../secret"), ("health_path", "/ok\ncommand")):
            with self.subTest(key=key, value=value), self.assertRaises(worker.RouteError):
                self.configure({**self.request, key: value})
        self.assertEqual(self.cli.calls, [])

    def test_two_apps_and_completed_replay_preserve_existing_resources(self):
        first = self.configure()
        old = copy.deepcopy(self.cli.objects)
        old_network = copy.deepcopy(self.cli.network_rules)
        second = self.configure({**self.request, "application_id": "app-" + "b" * 24,
                                 "hostname": "second.railshot.io", "node_port": 30124})
        for kind in worker.KINDS:
            self.assertEqual(self.cli.objects[kind][first["resources"][kind]], old[kind][first["resources"][kind]])
            self.assertNotEqual(first["resources"][kind], second["resources"][kind])
        count = len(self.cli.mutations())
        self.assertEqual(self.configure(), first)
        self.assertEqual(len(self.cli.mutations()), count)
        self.assertEqual(first["status"], "configured")
        self.assertIs(first["https_verified"], False)
        self.assertEqual(len(self.cli.objects["pool"]), 2)
        self.assertEqual(self.cli.network_rules[first["network_rule_id"]], old_network[first["network_rule_id"]])
        self.assertEqual(len(self.cli.network_mutations()), 2)

    def test_health_change_recreates_only_owned_route_and_replay_is_readonly(self):
        first = self.configure()
        other = self.configure({**self.request, "application_id": "app-" + "b" * 24,
                                "hostname": "other.railshot.io", "node_port": 30124})
        preserved = copy.deepcopy(({kind: self.cli.objects[kind][other["resources"][kind]] for kind in worker.KINDS},
                                   self.cli.network_rules[other["network_rule_id"]],
                                   self.cli.lb, self.cli.listener, self.cli.ports, self.cli.group))
        updated = {**self.request, "health_path": "/ready"}
        directory = Path(self.config["state_dir"])
        def checked_intent(arguments, service=("loadbalancer",)):
            journals = list(directory.glob("lifecycle-*.json"))
            if arguments[1] == "delete" or arguments[:4] == ["security", "group", "rule", "delete"]:
                self.assertEqual(len(journals), 1)
                journal = worker.read_private(journals[0])
                self.assertEqual(journal["phase"], "applying")
                self.assertEqual(journal["plan"]["request"], updated)
                self.assertEqual(journal["plan"]["snapshot"]["record"]["request"], self.request)
                self.assertEqual(journal["plan"]["snapshot"]["resources"], first["resources"])
                self.assertEqual(journal["plan_sha256"], worker.lifecycle_hash(journal["plan"]))
            return self.cli(arguments, service=service)
        result = worker.configure(self.config, updated, checked_intent)
        self.assertEqual(result["status"], "configured")
        self.assertIs(result["https_verified"], False)
        self.assertEqual(result["health_path"], "/ready")
        for kind in worker.KINDS:
            self.assertNotEqual(first["resources"][kind], result["resources"][kind])
            self.assertEqual(set(self.cli.objects[kind]), {result["resources"][kind], other["resources"][kind]})
        self.assertEqual(set(self.cli.network_rules), {result["network_rule_id"], other["network_rule_id"]})
        self.assertEqual(self.cli.objects["monitor"][result["resources"]["monitor"]]["url_path"], "/ready")
        self.assertEqual(({kind: self.cli.objects[kind][other["resources"][kind]] for kind in worker.KINDS},
                          self.cli.network_rules[other["network_rule_id"]],
                          self.cli.lb, self.cli.listener, self.cli.ports, self.cli.group), preserved)
        journal = worker.read_private(next(directory.glob("lifecycle-*.json")))
        self.assertEqual((journal["phase"], journal["receipt"]), ("succeeded", result))
        mutations = self.cli.mutations() + self.cli.network_mutations()
        self.assertEqual(self.configure(updated), result)
        self.assertEqual(self.cli.mutations() + self.cli.network_mutations(), mutations)
        self.assertEqual(len(list(directory.glob("lifecycle-*.json"))), 1)
        # Common lifecycle must still operate on the replacement's new ownership.
        for action in ("stop", "start", "delete"):
            plan = self.lifecycle(action)
            self.assertEqual(plan["health_path"], "/ready")
            self.assertEqual(self.lifecycle(action, "lifecycle-execute", plan["expected"])["status"], "succeeded")

    def test_health_change_verifies_old_owner_network_and_identity_before_mutation(self):
        first = self.configure()
        updated = {**self.request, "health_path": "/ready"}
        directory = Path(self.config["state_dir"])
        for target, key, value in (
                (self.cli.objects["monitor"][first["resources"]["monitor"]], "tags", "foreign-owner"),
                (self.cli.objects["member"][first["resources"]["member"]], "protocol_port", 30125),
                (self.cli.network_rules[first["network_rule_id"]], "remote_ip_prefix", "0.0.0.0/0"),
                (self.cli.listener, "default_pool_id", str(uuid.uuid4()))):
            with self.subTest(field=key):
                original = target[key]
                target[key] = value
                before = {p.name: p.read_bytes() for p in directory.glob("*.json")}
                mutations = self.cli.mutations() + self.cli.network_mutations()
                with self.assertRaises(worker.RouteError):
                    self.configure(updated)
                self.assertEqual(self.cli.mutations() + self.cli.network_mutations(), mutations)
                self.assertEqual({p.name: p.read_bytes() for p in directory.glob("*.json")}, before)
                target[key] = original
        for key, value in (("hostname", "changed.railshot.io"), ("node_port", 30125)):
            with self.subTest(identity=key), self.assertRaisesRegex(worker.RouteError, "application_intent_conflict"):
                self.configure({**updated, key: value})
        with patch.object(worker, "verify_certificate", side_effect=worker.RouteError("hostname_certificate_not_ready")):
            with self.assertRaisesRegex(worker.RouteError, "hostname_certificate_not_ready"):
                self.configure(updated)
        self.assertEqual(self.cli.mutations() + self.cli.network_mutations(), mutations)
        self.assertFalse(list(directory.glob("lifecycle-*.json")))

    def test_health_change_lost_create_response_discovers_without_duplicate(self):
        self.configure()
        self.cli.fail_create = "monitor"
        updated = {**self.request, "health_path": "/ready"}
        result = self.configure(updated)
        mutations = self.cli.mutations() + self.cli.network_mutations()
        self.assertEqual(result, self.configure(updated))
        self.assertEqual(self.cli.mutations() + self.cli.network_mutations(), mutations)
        self.assertEqual(len([args for args in self.cli.mutations() if args[1] == "create"]), 10)
        self.assertTrue(all(len(objects) == 1 for objects in self.cli.objects.values()))
        self.assertEqual(len(self.cli.network_rules), 1)

    def test_health_change_partial_delete_is_unknown_and_retry_never_mutates(self):
        self.configure()
        updated = {**self.request, "health_path": "/ready"}
        directory = Path(self.config["state_dir"])
        def lost_delete(arguments, service=("loadbalancer",)):
            result = self.cli(arguments, service=service)
            if arguments[:2] == ["l7rule", "delete"]:
                raise worker.RouteError("provider_response_unknown", "unknown")
            return result
        with self.assertRaisesRegex(worker.RouteError, "health_update_outcome_unknown") as error:
            worker.configure(self.config, updated, lost_delete)
        self.assertEqual(error.exception.status, "unknown")
        journal = worker.read_private(next(directory.glob("lifecycle-*.json")))
        self.assertEqual(journal["phase"], "unknown")
        self.assertEqual(worker.read_private(directory / (self.request["application_id"] + ".json"))["request"], self.request)
        before = {p.name: p.read_bytes() for p in directory.glob("*.json")}
        calls = copy.deepcopy((self.cli.calls, self.cli.network_calls))
        for request in (updated, self.request):
            with self.assertRaisesRegex(worker.RouteError, "lifecycle_reconciliation_required"):
                self.configure(request)
        self.assertEqual((self.cli.calls, self.cli.network_calls), calls)
        self.assertEqual({p.name: p.read_bytes() for p in directory.glob("*.json")}, before)

    def test_health_change_uncertain_recreation_never_resets_creation_intent(self):
        self.configure()
        self.cli.fail_create, self.cli.apply_failed_create = "member", False
        updated = {**self.request, "health_path": "/ready"}
        with self.assertRaisesRegex(worker.RouteError, "health_update_outcome_unknown") as error:
            self.configure(updated)
        self.assertEqual(error.exception.status, "unknown")
        directory = Path(self.config["state_dir"])
        record = worker.read_private(directory / (self.request["application_id"] + ".json"))
        self.assertEqual(record["request"], updated)
        self.assertEqual(record["steps"]["member"], {"intent": True})
        before = {p.name: p.read_bytes() for p in directory.glob("*.json")}
        calls = copy.deepcopy((self.cli.calls, self.cli.network_calls))
        with self.assertRaisesRegex(worker.RouteError, "lifecycle_reconciliation_required"):
            self.configure(updated)
        self.assertEqual((self.cli.calls, self.cli.network_calls), calls)
        self.assertEqual({p.name: p.read_bytes() for p in directory.glob("*.json")}, before)

    def test_health_change_never_revives_stopped_or_deleted_app(self):
        self.configure()
        updated = {**self.request, "health_path": "/ready"}
        for action in ("stop", "delete"):
            plan = self.lifecycle(action)
            self.lifecycle(action, "lifecycle-execute", plan["expected"])
            before = {p.name: p.read_bytes() for p in Path(self.config["state_dir"]).glob("*.json")}
            calls = copy.deepcopy((self.cli.calls, self.cli.network_calls))
            with self.assertRaisesRegex(worker.RouteError, "application_lifecycle_action_required"):
                self.configure(updated)
            self.assertEqual((self.cli.calls, self.cli.network_calls), calls)
            self.assertEqual({p.name: p.read_bytes() for p in Path(self.config["state_dir"]).glob("*.json")}, before)

    def test_health_change_does_not_reset_uncertain_initial_creation(self):
        self.cli.fail_create, self.cli.apply_failed_create = "pool", False
        with self.assertRaisesRegex(worker.RouteError, "creation_unresolved_pool"):
            self.configure()
        directory = Path(self.config["state_dir"])
        before = {p.name: p.read_bytes() for p in directory.glob("*.json")}
        calls = copy.deepcopy((self.cli.calls, self.cli.network_calls))
        with self.assertRaisesRegex(worker.RouteError, "application_intent_conflict"):
            self.configure({**self.request, "health_path": "/ready"})
        self.assertEqual((self.cli.calls, self.cli.network_calls), calls)
        self.assertEqual({p.name: p.read_bytes() for p in directory.glob("*.json")}, before)

    def test_lost_create_response_recovers_by_reading_each_resource(self):
        for kind in worker.KINDS:
            with self.subTest(kind=kind):
                self.cli.fail_create = kind
                request = {**self.request, "application_id": "app-" + format(worker.KINDS.index(kind), "024x"),
                           "hostname": kind + ".railshot.io", "node_port": 30200 + worker.KINDS.index(kind)}
                self.assertEqual(self.configure(request)["status"], "configured")
        self.assertEqual(len([args for args in self.cli.mutations() if args[1] == "create"]), 25)

    def test_absent_uncertain_creation_is_never_repeated(self):
        self.cli.fail_create, self.cli.apply_failed_create = "pool", False
        for _ in range(2):
            with self.assertRaisesRegex(worker.RouteError, "creation_unresolved_pool") as error:
                self.configure()
            self.assertEqual(error.exception.status, "unknown")
        self.assertEqual(len(self.cli.mutations()), 1)
        state = json.loads((Path(self.config["state_dir"]) / (self.request["application_id"] + ".json")).read_text())
        self.assertEqual(state["steps"], {"pool": {"intent": True}})

    def test_lost_activation_is_not_repeated(self):
        self.cli.fail_enable = True
        for _ in range(2):
            with self.assertRaisesRegex(worker.RouteError, "activation_unresolved"):
                self.configure()
        self.assertEqual(len(self.cli.mutations()), 6)

    def test_preflight_and_certificate_block_before_creation(self):
        for changes in ({"default_pool_id": str(uuid.UUID(int=8))}, {"protocol": "HTTP"},
                        {"provisioning_status": "PENDING_UPDATE"}, {"project_id": "wrong"}):
            original = copy.deepcopy(self.cli.listener)
            self.cli.listener.update(changes)
            with self.assertRaises(worker.RouteError):
                self.configure()
            self.cli.listener = original
        with patch.object(worker, "verify_certificate", side_effect=worker.RouteError("hostname_certificate_not_ready")):
            with self.assertRaisesRegex(worker.RouteError, "hostname_certificate_not_ready"):
                self.configure()
        self.assertEqual(self.cli.mutations(), [])

    def test_conflicting_request_and_remote_drift_are_not_repaired(self):
        receipt = self.configure()
        count = len(self.cli.mutations())
        with self.assertRaisesRegex(worker.RouteError, "application_intent_conflict"):
            self.configure({**self.request, "node_port": 30124})
        self.cli.objects["member"][receipt["resources"]["member"]]["address"] = "10.0.0.18"
        with self.assertRaisesRegex(worker.RouteError, "resource_mismatch_address"):
            self.configure()
        self.assertEqual(len(self.cli.mutations()), count)

    def test_relation_formats_are_strict(self):
        identifier = str(uuid.UUID(int=1))
        self.assertEqual(worker.relation_ids(identifier), {identifier})
        self.assertEqual(worker.relation_ids([{"id": identifier}]), {identifier})
        self.assertEqual(worker.relation_ids(""), set())
        with self.assertRaises(worker.RouteError):
            worker.relation_ids("not-an-id")

    def test_network_identity_mismatch_blocks_before_any_mutation(self):
        network = self.config["network"]
        for role, changes in (("runtime", {"device_id": str(uuid.UUID(int=99))}),
                              ("runtime", {"security_group_ids": []}),
                              ("amphora", {"project_id": network["runtime_project_id"]}),
                              ("amphora", {"fixed_ips": []})):
            port = self.cli.ports[network[role + "_port_id"]]
            original = copy.deepcopy(port)
            port.update(changes)
            with self.assertRaises(worker.RouteError):
                self.configure()
            self.cli.ports[network[role + "_port_id"]] = original
        self.assertEqual(self.cli.mutations() + self.cli.network_mutations(), [])

    def test_exact_existing_network_rule_reused_and_broad_rule_preserved(self):
        state_path = Path(self.config["state_dir"]) / "network-test.json"
        first = worker.ensure_network_rule(self.config, self.request, {}, state_path, self.cli)
        broad = copy.deepcopy(self.cli.network_rules[first])
        broad.update(id=str(uuid.uuid4()), remote_ip_prefix="0.0.0.0/0", port_range_min=30000,
                     port_range_max=32767, description="existing operator rule")
        self.cli.network_rules[broad["id"]] = broad
        self.assertEqual(worker.ensure_network_rule(self.config, self.request, {}, state_path, self.cli), first)
        self.assertEqual(len(self.cli.network_mutations()), 1)
        self.assertEqual(self.cli.network_rules[broad["id"]], broad)
        narrow = self.cli.network_rules[first]
        self.assertEqual((narrow["remote_ip_prefix"], narrow["port_range_min"], narrow["port_range_max"]),
                         ("10.0.0.40/32", 30123, 30123))
        self.assertEqual(narrow["description"], "railshot:" + self.request["application_id"])

    def test_network_lost_create_response_recovers_without_duplicate(self):
        self.cli.fail_network_create = True
        first = self.configure()
        self.assertEqual(self.configure(), first)
        self.assertEqual(len(self.cli.network_mutations()), 1)

    def test_unresolved_network_create_blocks_lb_and_is_not_repeated(self):
        self.cli.fail_network_create, self.cli.apply_network_create = True, False
        for _ in range(2):
            with self.assertRaisesRegex(worker.RouteError, "network_creation_unresolved"):
                self.configure()
        self.assertEqual(len(self.cli.network_mutations()), 1)
        self.assertEqual(self.cli.mutations(), [])

    def test_completed_network_rule_drift_is_not_repaired(self):
        receipt = self.configure()
        rule = self.cli.network_rules[receipt["network_rule_id"]]
        rule["port_range_max"] += 1
        with self.assertRaisesRegex(worker.RouteError, "network_rule_readback_mismatch"):
            self.configure()
        self.assertEqual(len(self.cli.network_mutations()), 1)


class RuntimeRegistrationTests(unittest.TestCase):
    def setUp(self):
        RouteWorkerTests.setUp(self)
        self.enterContext(patch.object(worker, 'ENVIRONMENTS', Path(self.temporary.name).resolve() / 'environments'))
        self.enterContext(patch.object(worker, 'CONTROLLER_STATE', Path(self.temporary.name).resolve() / 'controller-state'))
        self.ident = 'personal-' + str(uuid.UUID(int=100))
        self.runtime = {'project_id': self.config['network']['runtime_project_id'],
            'server_id': str(uuid.UUID(int=41)), 'port_id': str(uuid.UUID(int=42)),
            'security_group_id': str(uuid.UUID(int=43)), 'private_address': '10.0.0.18'}
        self.base = worker.operator_base(self.config)
        self.binding = worker.registration_binding(self.base, self.ident, 1, self.runtime)
        self.envelope = {'operation': 'register-runtime', 'binding': self.binding, 'runtime': self.runtime}
        self.project = {'id': self.runtime['project_id'], 'name': 'railshot', 'enabled': True}
        self.server = {'id': self.runtime['server_id'], 'tenant_id': self.runtime['project_id'], 'status': 'ACTIVE',
            'properties': {'railshot.target': self.ident, 'railshot.generation': '1'}}
        self.port = {'id': self.runtime['port_id'], 'device_id': self.runtime['server_id'],
            'project_id': self.runtime['project_id'], 'device_owner': 'compute:nova',
            'fixed_ips': [{'ip_address': self.runtime['private_address'], 'subnet_id': self.base['member_subnet_id']}],
            'network_id': str(uuid.UUID(int=30)), 'security_group_ids': [self.runtime['security_group_id']]}
        self.group = {'id': self.runtime['security_group_id'], 'project_id': self.runtime['project_id']}
        self.reads = []

    def call(self, arguments, service=('loadbalancer',)):
        self.reads.append((arguments, service))
        if not service:
            if arguments[:2] == ['project', 'show']:
                return copy.deepcopy(self.project)
            if arguments[:2] == ['server', 'show']:
                return copy.deepcopy(self.server)
            if arguments[:2] == ['port', 'list']:
                return [{'ID': self.runtime['port_id']}]
            if arguments[:3] == ['port', 'show', self.runtime['port_id']]:
                return copy.deepcopy(self.port)
            if arguments[:4] == ['security', 'group', 'show', self.runtime['security_group_id']]:
                return copy.deepcopy(self.group)
        return self.cli(arguments, service)

    def register(self):
        return worker.dispatch(self.config, self.envelope, self.call)

    def test_new_runtime_uses_fixed_base_without_changing_old_config(self):
        before = copy.deepcopy(self.config)
        first = self.register()
        self.assertEqual(first['binding'], self.binding)
        self.assertEqual(self.register(), first)
        self.assertEqual(self.config, before)
        selected = worker.environment_config(self.config, self.binding)
        self.assertEqual(selected['runtime_private_address'], '10.0.0.18')
        self.assertEqual(selected['network']['runtime_server_id'], self.runtime['server_id'])
        self.assertEqual(selected['loadbalancer_id'], self.config['loadbalancer_id'])
        self.assertEqual(Path(selected['state_dir']), worker.ENVIRONMENTS / self.ident / 'routes')
        self.assertFalse(any(action in args for args, _ in self.reads for action in ('create', 'set', 'delete')))

    def test_readiness_verifies_provider_without_creating_local_state(self):
        result = worker.dispatch(self.config, {**self.envelope, 'operation': 'verify-runtime'}, self.call)
        self.assertEqual(result['status'], 'verified')
        self.assertFalse(worker.ENVIRONMENTS.exists())

    def test_fresh_controller_base_requires_no_placeholder_old_runtime(self):
        self.assertEqual(worker.dispatch(self.base, self.envelope, self.call)['status'], 'registered')
        result = worker.dispatch(self.base, {'operation': 'unregister-runtime', 'binding': self.binding}, self.call)
        self.assertEqual(result['status'], 'unregistered')

    def test_existing_untagged_vm_can_be_claimed_but_foreign_tag_cannot(self):
        self.server['properties'] = {}
        self.register()
        self.server['properties'] = {'railshot.target': 'other', 'railshot.generation': '1'}
        with self.assertRaisesRegex(worker.RouteError, 'owner_mismatch'):
            self.register()

    def test_cross_environment_runtime_and_changed_generation_rejected(self):
        self.register()
        other = worker.registration_binding(self.base, 'personal-' + str(uuid.UUID(int=101)), 1, self.runtime)
        with self.assertRaisesRegex(worker.RouteError, 'already_registered'):
            worker.dispatch(self.config, {**self.envelope, 'binding': other}, self.call)
        changed = worker.registration_binding(self.base, self.ident, 2, self.runtime)
        with self.assertRaisesRegex(worker.RouteError, 'immutable'):
            worker.dispatch(self.config, {**self.envelope, 'binding': changed}, self.call)

    def test_mismatched_base_is_blocked_without_provider_calls(self):
        changed = {**self.base, 'listener_id': str(uuid.UUID(int=99))}
        other = worker.registration_binding(changed, self.ident, 1, self.runtime)
        with self.assertRaisesRegex(worker.RouteError, 'base_or_binding_changed'):
            worker.dispatch(self.config, {**self.envelope, 'binding': other}, self.call)
        self.assertEqual(self.reads, [])

    def test_provider_identity_mismatches_leave_no_binding(self):
        changes = [(self.project, 'name', 'admin'), (self.server, 'tenant_id', '0' * 32),
                   (self.port, 'device_id', str(uuid.UUID(int=99))),
                   (self.port, 'security_group_ids', [self.runtime['security_group_id'], str(uuid.UUID(int=99))]),
                   (self.group, 'project_id', '0' * 32)]
        for document, key, value in changes:
            with self.subTest(key=key):
                before = document[key]
                document[key] = value
                with self.assertRaises(worker.RouteError):
                    self.register()
                self.assertFalse((worker.ENVIRONMENTS / self.ident / 'binding.json').exists())
                document[key] = before

    def test_group_attached_to_another_vm_is_not_registered(self):
        original = self.call
        other_id = str(uuid.UUID(int=999))
        def shared(arguments, service=('loadbalancer',)):
            if arguments[:2] == ['port', 'list']:
                return [{'ID': self.runtime['port_id']}, {'ID': other_id}]
            if arguments[:3] == ['port', 'show', other_id]:
                return {**self.port, 'id': other_id, 'device_id': str(uuid.UUID(int=998))}
            return original(arguments, service)
        with self.assertRaisesRegex(worker.RouteError, 'security_group_shared'):
            worker.register_runtime(self.config, self.envelope, shared)
        self.assertFalse((worker.ENVIRONMENTS / self.ident / 'binding.json').exists())

    def test_unknown_readback_retry_is_safe_and_does_not_create_cloud_objects(self):
        with self.assertRaises(worker.RouteError):
            worker.register_runtime(self.config, self.envelope,
                lambda *args, **kwargs: (_ for _ in ()).throw(worker.RouteError('unknown', 'unknown')))
        self.assertFalse((worker.ENVIRONMENTS / self.ident / 'binding.json').exists())
        self.assertEqual(self.register()['status'], 'registered')

    def test_unregister_is_bound_idempotent_and_blocks_routes(self):
        self.register()
        request = {'operation': 'unregister-runtime', 'binding': self.binding}
        result = worker.dispatch(self.config, request, self.call)
        self.assertEqual(result['status'], 'unregistered')
        self.assertEqual(worker.dispatch(self.config, request, self.call), result)
        with self.assertRaisesRegex(worker.RouteError, 'environment_binding_mismatch'):
            worker.environment_config(self.config, self.binding)
        with self.assertRaisesRegex(worker.RouteError, 'immutable'):
            self.register()
        record = worker.read_private(worker.ENVIRONMENTS / self.ident / 'binding.json')
        self.assertEqual(record['status'], 'released')

    def test_unregister_refuses_active_or_unknown_application(self):
        self.register()
        selected = worker.environment_config(self.config, self.binding)
        worker.save(Path(selected['state_dir']) / ('app-' + 'a' * 24 + '.json'), {'status': 'configured'})
        with self.assertRaisesRegex(worker.RouteError, 'routes_still_present'):
            worker.dispatch(self.config, {'operation': 'unregister-runtime', 'binding': self.binding}, self.call)
        self.assertEqual(worker.environment_config(self.config, self.binding), selected)

    def test_environment_route_dispatch_cannot_select_arbitrary_path(self):
        self.register()
        request = {**self.request, 'private_address': self.runtime['private_address']}
        with patch.object(worker, 'configure', return_value={'status': 'configured'}) as configure:
            worker.dispatch(self.config, {'operation': 'environment-route', 'binding': self.binding, 'request': request}, self.call)
            self.assertEqual(configure.call_args.args[0]['network']['runtime_server_id'], self.runtime['server_id'])
        bad = {**self.binding, 'environment_id': '../old-config'}
        with self.assertRaises(worker.RouteError):
            worker.dispatch(self.config, {'operation': 'environment-route', 'binding': bad, 'request': request}, self.call)

    def test_registered_environment_route_delete_and_release_keep_legacy_state(self):
        self.register()
        selected = worker.environment_config(self.config, self.binding)
        cloud = OctaviaCLI(selected)
        request = {**self.request, 'private_address': self.runtime['private_address']}
        created = worker.dispatch(self.config, {'operation': 'environment-route',
            'binding': self.binding, 'request': request}, cloud)
        self.assertEqual(created['status'], 'configured')
        self.assertFalse((Path(self.config['state_dir']) / (request['application_id'] + '.json')).exists())
        lifecycle = {'operation': 'lifecycle-plan', 'action': 'delete', 'request': {
            key: request[key] for key in ('application_id', 'hostname', 'node_port')}}
        envelope = {'operation': 'environment-lifecycle', 'binding': self.binding, 'request': lifecycle}
        plan = worker.dispatch(self.config, envelope, cloud)
        envelope['request'] = {**lifecycle, 'operation': 'lifecycle-execute', 'expected': plan['expected']}
        self.assertEqual(worker.dispatch(self.config, envelope, cloud)['status'], 'succeeded')
        released = worker.dispatch(self.config, {'operation': 'unregister-runtime', 'binding': self.binding}, cloud)
        self.assertEqual(released['status'], 'unregistered')
        self.assertTrue(all(not objects for objects in cloud.objects.values()))
        self.assertFalse(cloud.network_rules)


class CertificateTests(unittest.TestCase):
    def test_fixed_public_certificate_and_ca_only(self):
        reference = str(uuid.UUID(int=9))
        def info(path):
            return SimpleNamespace(st_mode=(stat.S_IFDIR | 0o700 if path.name in ("certificates", "origin-ca") else
                                            stat.S_IFREG | 0o600),
                                   st_uid=123 if path.suffix == ".crt" or path.name == "certificates" else 0)
        with patch.object(Path, "lstat", info), patch.object(Path, "resolve", lambda path: path), \
                patch.object(worker.pwd, "getpwnam", return_value=SimpleNamespace(pw_uid=123)), \
                patch.object(worker.subprocess, "run") as run:
            worker.verify_certificate(reference, "first.railshot.io")
        self.assertEqual(run.call_args.args[0], ["/usr/bin/openssl", "verify", "-CAfile",
            "/opt/railshot/octavia/origin-ca/ca.pem", "-verify_hostname", "first.railshot.io", "-purpose",
            "sslserver", "/var/lib/octavia/certificates/" + reference + ".crt"])

    def test_cli_failure_never_includes_raw_output(self):
        error = subprocess.CalledProcessError(1, "unused", output="private", stderr="private")
        with patch.object(worker.subprocess, "run", side_effect=error):
            with self.assertRaisesRegex(worker.RouteError, "^provider_response_unknown$"):
                worker.run_cli(["show", str(uuid.UUID(int=1)), "-f", "json"])


if __name__ == "__main__":
    unittest.main()
