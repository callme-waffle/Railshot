"""Execute the actual host guard and policy parser with no host/root mutations."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[3]
TASKS = yaml.safe_load((ROOT / "infrastructure/ansible/ci.yml").read_text())[0]["tasks"]
GUARD = next(task["ansible.builtin.command"]["argv"][-1] for task in TASKS
             if "RAILSHOT_ALLOW_K3S_BUILD_WORKER" in task.get("environment", {}))
INSTALLER = next(task["ansible.builtin.copy"]["content"] for task in TASKS
                 if task.get("ansible.builtin.copy", {}).get("dest") == "/usr/local/sbin/railshot-ci-network")
POLICY = INSTALLER.split("<<'PYRAILSHOT_POLICY'\n", 1)[1].split("\nPYRAILSHOT_POLICY", 1)[0]
BUILD_CONFIG = {"node-label": ["railshot.io/node-role=build"],
                "node-taint": ["railshot.io/dedicated=build:NoSchedule"], "docker": False}


class HostPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # Only root ownership and systemd state are mocked. File types, modes,
        # symlinks and the config parser are exercised by the real shell guard.
        self.executable("stat", "import os, stat, sys\n"
                        "print(os.environ.get('TEST_OWNER', '0') + ':' + "
                        "format(stat.S_IMODE(os.stat(sys.argv[-1]).st_mode), 'o'))\n")
        self.executable("systemctl", "import os, sys\n"
                        "sys.exit(0 if sys.argv[-1] in os.environ.get('TEST_ACTIVE', 'k3s-agent').split(',') else 3)\n")

    def executable(self, name, body):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + body)
        path.chmod(0o755)

    def path(self, path):
        return self.root / path.lstrip("/")

    def agent(self, config=None):
        self.path("/var/lib/rancher/k3s/agent").mkdir(parents=True, exist_ok=True)
        target = self.path("/etc/rancher/k3s/config.yaml")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(BUILD_CONFIG if config is None else config))
        target.chmod(0o600)
        return target

    def run_guard(self, approved=False, **env):
        source = GUARD.replace("/var/lib/", str(self.root) + "/var/lib/")
        source = source.replace("/etc/", str(self.root) + "/etc/")
        return subprocess.run(["bash", "-c", source], capture_output=True, text=True, env={
            **os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "RAILSHOT_ALLOW_K3S_BUILD_WORKER": "true" if approved else "false",
            "RAILSHOT_ANSIBLE_PYTHON": sys.executable, **env}).returncode

    def test_standalone_default_and_explicit_build_agent(self):
        self.assertEqual(self.run_guard(), 0)
        self.assertNotEqual(self.run_guard(approved=True), 0)
        self.agent()
        self.assertNotEqual(self.run_guard(), 0)
        self.assertEqual(self.run_guard(approved=True), 0)
        self.assertNotEqual(self.run_guard(approved=True, TEST_ACTIVE=""), 0)
        for service in ("k3s", "kubelet"):
            with self.subTest(service=service):
                self.assertNotEqual(self.run_guard(approved=True, TEST_ACTIVE="k3s-agent," + service), 0)

    def test_control_plane_and_standard_kube_markers_are_refused(self):
        self.agent()
        for name in ("/var/lib/rancher/k3s/server", "/etc/rancher/k3s/k3s.yaml", "/etc/kubernetes"):
            with self.subTest(path=name):
                marker = self.path(name)
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.symlink_to(self.root / "missing")
                self.assertNotEqual(self.run_guard(approved=True), 0)
                marker.unlink()

    def test_ordinary_agent_and_ambiguous_or_docker_configs_are_refused(self):
        bad = [ {}, {**BUILD_CONFIG, "node-label": "railshot.io/node-role=build"},
                {**BUILD_CONFIG, "node-taint": []},
                {**BUILD_CONFIG, "node-label": BUILD_CONFIG["node-label"] + ["railshot.io/node-role=ops"]},
                {**BUILD_CONFIG, "node-taint+": ["other=value:NoSchedule"]} ]
        bad.extend({**BUILD_CONFIG, "docker": value} for value in (True, "false", 0, None))
        for config in bad:
            with self.subTest(config=config):
                self.agent(config)
                self.assertNotEqual(self.run_guard(approved=True), 0)

    def test_config_ownership_mode_symlink_and_dropins_are_refused(self):
        config = self.agent()
        self.assertNotEqual(self.run_guard(approved=True, TEST_OWNER="1000"), 0)
        config.chmod(0o644)
        self.assertNotEqual(self.run_guard(approved=True), 0)
        config.chmod(0o600)
        real = config.with_suffix(".real")
        config.rename(real)
        config.symlink_to(real)
        self.assertNotEqual(self.run_guard(approved=True), 0)
        config.unlink()
        real.rename(config)
        dropins = config.with_name("config.yaml.d")
        dropins.mkdir()
        self.assertNotEqual(self.run_guard(approved=True), 0)
        dropins.rmdir()
        dropins.symlink_to(self.root / "missing")
        self.assertNotEqual(self.run_guard(approved=True), 0)

    def test_failed_native_policy_update_invalidates_receipt_and_stops_before_migration(self):
        profile = self.path("/etc/railshot/ci-executor.yaml")
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text("{}")
        profile.chmod(0o644)
        receipt = self.path("/var/lib/railshot-ci/network-verified.sha256")
        receipt.parent.mkdir(parents=True)
        receipt.write_text("old-success")
        self.path("/run").mkdir()
        self.executable("jq", "pass\n")
        self.executable("flock", "pass\n")
        self.executable("nft", "import sys\nsys.exit(42)\n")
        self.executable("iptables", "raise RuntimeError('Migration must not run after an nft failure')\n")
        source = INSTALLER
        for prefix in ("/var/lib/", "/etc/", "/run/"):
            source = source.replace(prefix, str(self.root) + prefix)
        result = subprocess.run(["bash", "-c", source], capture_output=True, text=True, env={
            **os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"]})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(receipt.exists())
        self.assertNotIn("Migration must not run", result.stderr)


class NetworkPolicyTest(unittest.TestCase):
    def setUp(self):
        self.definitions = {}
        exec(compile(POLICY.split("expected = canonical(objects)")[0], "ci.yml:policy", "exec"), self.definitions)
        self.objects = copy.deepcopy(self.definitions["objects"])

    def policy_digest(self, objects, mode="--check", install_error=False):
        def output(command, **kwargs):
            self.assertEqual(command, ["nft", "-j", "list", "table", "inet", "railshot_ci"])
            return json.dumps({"nftables": objects})
        with patch("sys.argv", ["-", mode]), patch("subprocess.check_output", side_effect=output), \
             patch("subprocess.run") as run, contextlib.redirect_stdout(io.StringIO()) as stream:
            if install_error:
                run.side_effect = subprocess.CalledProcessError(1, ["nft"])
            exec(compile(POLICY, "ci.yml:policy", "exec"), {})
        json.loads(stream.getvalue())
        return hashlib.sha256(stream.getvalue().encode()).hexdigest(), run

    def test_kernel_handles_counters_and_set_order_do_not_invalidate_policy(self):
        baseline, _ = self.policy_digest(self.objects)
        for index, item in enumerate(self.objects):
            value = next(iter(item.values()))
            value["handle"] = index + 100
            for expr in value.get("expr", []):
                if "counter" in expr:
                    expr["counter"] = {"packets": 120, "bytes": 30000}
                right = expr.get("match", {}).get("right")
                if isinstance(right, dict) and isinstance(right.get("set"), list):
                    right["set"].reverse()
        self.assertEqual(baseline, self.policy_digest(self.objects)[0])

    def test_drifted_hook_priority_missing_table_and_dormant_fail_closed(self):
        for change in ("priority", "hook", "missing", "dormant"):
            with self.subTest(change=change):
                objects = copy.deepcopy(self.objects)
                if change == "missing":
                    objects = []
                elif change == "dormant":
                    objects[0]["table"]["flags"] = ["dormant"]
                elif change == "priority":
                    objects[1]["chain"]["prio"] = 0
                else:
                    objects[1]["chain"]["hook"] = "output"
                with self.assertRaises(SystemExit):
                    self.policy_digest(objects)

    def test_accept_inserted_into_owned_chains_and_deleted_drop_fail_closed(self):
        for chain in ("input", "forward", "egress"):
            with self.subTest(chain=chain):
                objects = copy.deepcopy(self.objects)
                index = next(i for i, item in enumerate(objects) if item.get("rule", {}).get("chain") == chain)
                rule = copy.deepcopy(objects[index])
                rule["rule"]["expr"] = [{"accept": None}]
                objects.insert(index, rule)
                with self.assertRaises(SystemExit):
                    self.policy_digest(objects)
        objects = copy.deepcopy(self.objects)
        objects[-1]["rule"]["expr"][-1] = {"return": None}
        with self.assertRaises(SystemExit):
            self.policy_digest(objects)

    def test_atomic_update_owns_only_one_table_and_failure_does_not_read_success(self):
        _, run = self.policy_digest(self.objects, mode="install")
        call = run.call_args
        self.assertEqual(call.args[0], ["nft", "-j", "-f", "-"])
        commands = json.loads(call.kwargs["input"])["nftables"]
        self.assertEqual(commands[:2], [{"add": {"table": {"family": "inet", "name": "railshot_ci"}}},
                                       {"delete": {"table": {"family": "inet", "name": "railshot_ci"}}}])
        self.assertTrue(all(next(iter(next(iter(command.values())).values())).get("table", "railshot_ci") == "railshot_ci"
                            for command in commands))
        self.assertNotIn("flush", json.dumps(commands))
        with self.assertRaises(subprocess.CalledProcessError):
            self.policy_digest(self.objects, mode="install", install_error=True)

    def test_policy_has_expected_host_ipv6_and_private_destination_drops(self):
        rules = [item["rule"] for item in self.objects if "rule" in item]
        comments = {rule.get("comment"): rule for rule in rules}
        for name in ("railshot-runtime-host-block", "railshot-ci-host-block",
                     "railshot-runtime-ipv6-host-block", "railshot-runtime-ipv6-forward-block",
                     "railshot-ci-ipv6-block", "railshot-deny-169.254.0.0/16", "railshot-deny-10.0.0.0/8",
                     "railshot-deny-other-egress"):
            self.assertEqual(comments[name]["expr"][-1], {"drop": None})
        hooks = [item["chain"] for item in self.objects if "chain" in item and "hook" in item["chain"]]
        self.assertEqual({(chain["hook"], chain["prio"], chain["policy"]) for chain in hooks},
                         {("input", -10, "accept"), ("forward", -10, "accept")})

    def test_native_probe_exercises_prior_and_later_accept_without_weakening_owned_table(self):
        # The actual packet verdict/counter check runs in the existing root
        # native acceptance script, never by mutating the developer's host.
        probe = (ROOT / "infrastructure/ansible/test-ci-network.sh").read_text()
        self.assertIn("for priority in -20 0", probe)
        self.assertIn('add chain inet %s', probe)
        self.assertIn('nft delete table inet "$probe_table"', probe)
        self.assertIn('counter railshot-deny-169.254.0.0/16', probe)
        self.assertIn('counter railshot-ci-host-block', probe)
        self.assertIn('policy_fingerprint=$(/usr/local/sbin/railshot-ci-network --fingerprint)', probe)
        self.assertNotIn('--fingerprint > /var/lib/railshot-ci/network-verified.sha256', probe)


if __name__ == "__main__":
    unittest.main()
