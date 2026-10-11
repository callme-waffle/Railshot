import base64
import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import container_preflight as preflight
from container_preflight import READ_ONLY, READ_WRITE, TOKEN, validate_container


class ContainerBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.container = {
            "HostConfig": {"NetworkMode": "host", "Privileged": False, "PidMode": "", "CapAdd": ["NET_ADMIN"], "SecurityOpt": ["no-new-privileges", "apparmor=railshot-codex-bwrap", "seccomp=" + (Path(preflight.__file__).parent / "railshot-codex-bwrap.json").read_text()]},
            "AppArmorProfile": "railshot-codex-bwrap",
            "Mounts": [
                {"Type": "bind", "Source": source, "Destination": target, "RW": writable}
                for paths, writable in ((READ_ONLY, False), (READ_WRITE, True))
                for target, source in paths.items()
            ] + [{"Type": "bind", "Source": "/private/token", "Destination": TOKEN, "RW": False}],
        }

    def test_same_path_private_vm_profile(self):
        validate_container(self.container)
        self.container["HostConfig"]["SecurityOpt"] = [value.replace("no-new-privileges", "no-new-privileges:true").replace("apparmor=", "apparmor:") for value in self.container["HostConfig"]["SecurityOpt"]]
        validate_container(self.container)

    def test_wrong_host_privilege_or_network(self):
        for key, value in (("NetworkMode", "bridge"), ("Privileged", True), ("PidMode", "host"),
                           ("CapAdd", ["NET_ADMIN", "SYS_ADMIN"])):
            with self.subTest(key=key):
                container = copy.deepcopy(self.container)
                container["HostConfig"][key] = value
                with self.assertRaises(ValueError):
                    validate_container(container)

    def test_compose_refuses_missing_or_unconfined_profiles(self):
        for options in ([], ["seccomp=unconfined"],
                        [value for value in self.container["HostConfig"]["SecurityOpt"] if value != "no-new-privileges"]):
            with self.subTest(options=options):
                container = copy.deepcopy(self.container)
                container["HostConfig"]["SecurityOpt"] = options
                with self.assertRaises(ValueError):
                    validate_container(container)
        container = copy.deepcopy(self.container)
        container["AppArmorProfile"] = "unconfined"
        with self.assertRaises(ValueError):
            validate_container(container)

    def test_unexpected_credentials_mount(self):
        self.container["Mounts"].append({"Type": "bind", "Source": "/root/.kube", "Destination": "/root/.kube", "RW": False})
        with self.assertRaises(ValueError):
            validate_container(self.container)

    def test_path_mapping_receipt_and_token_are_not_relaxed(self):
        for target, field, value in (("/var/lib/railshot-runner/work", "Source", "/another/path"),
                                     ("/var/lib/railshot-ci", "RW", True), (TOKEN, "RW", True)):
            with self.subTest(target=target):
                container = copy.deepcopy(self.container)
                next(m for m in container["Mounts"] if m["Destination"] == target)[field] = value
                with self.assertRaises(ValueError):
                    validate_container(container)


def pod_fixture():
    mounts, volumes = [], []
    for paths, writable in ((preflight.POD_READ_ONLY, False), (READ_WRITE, True)):
        for target, source in paths.items():
            name = "host-" + str(len(volumes))
            kind = "Socket" if source.endswith("docker.sock") else "File" if source in {
                "/run/railshot-ci-network.lock", "/usr/local/sbin/railshot-ci-network"} else "Directory"
            mounts.append({"name": name, "mountPath": target, "readOnly": not writable})
            volumes.append({"name": name, "hostPath": {"path": source, "type": kind}})
    mounts += [{"name": "registration-token", "mountPath": TOKEN, "subPath": "token", "readOnly": True},
               {"name": "kubernetes-api", "mountPath": preflight.KUBERNETES, "readOnly": True}]
    volumes += [{"name": "registration-token", "secret": {
        "secretName": "railshot-build-runner-registration", "defaultMode": 0o600,
        "items": [{"key": "token", "path": "token"}]}},
        {"name": "kubernetes-api", "projected": {"defaultMode": 0o400, "sources": [
            {"serviceAccountToken": {"path": "token", "expirationSeconds": 3600}},
            {"configMap": {"name": "kube-root-ca.crt", "items": [{"key": "ca.crt", "path": "ca.crt"}]}}
        ]}}]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": {
        "namespace": "railshot-build", "name": "runner-attempt-abc", "uid": "11111111-1111-1111-1111-111111111111"},
        "spec": {"hostNetwork": True, "automountServiceAccountToken": False,
                 "serviceAccountName": "railshot-build-runner", "restartPolicy": "Never",
                 "nodeName": "worker-01", "nodeSelector": {"railshot.io/node-role": "build",
                    "kubernetes.io/arch": "amd64", "kubernetes.io/hostname": "worker-01"},
                 "tolerations": [{"key": "railshot.io/dedicated", "operator": "Equal", "value": "build", "effect": "NoSchedule"},
                                 {"key": "node.kubernetes.io/not-ready", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 300}],
                 "volumes": volumes, "containers": [{"name": "runner", "securityContext": {
                     "runAsUser": 0, "allowPrivilegeEscalation": False, "seccompProfile": preflight.SECCOMP, "appArmorProfile": preflight.APPARMOR,
                     "capabilities": {"add": ["NET_ADMIN"]}}, "volumeMounts": mounts,
                     "env": [{"name": key, "valueFrom": {"fieldRef": {"apiVersion": "v1", "fieldPath": value}}}
                             for key, value in (("RAILSHOT_POD_NAMESPACE", "metadata.namespace"),
                                                ("RAILSHOT_POD_NAME", "metadata.name"), ("RAILSHOT_POD_UID", "metadata.uid"))]}]}}


class KubernetesBoundaryTest(unittest.TestCase):
    def validate(self, pod):
        return preflight.validate_pod(pod, "railshot-build", "runner-attempt-abc", "11111111-1111-1111-1111-111111111111")

    def test_actual_pod_dedicated_profile(self):
        self.validate(pod_fixture())

    def test_live_node_requires_build_labels_taint_and_no_control_plane_role(self):
        pod = pod_fixture()
        node = {"apiVersion": "v1", "kind": "Node", "metadata": {
            "name": "worker-01", "labels": pod["spec"]["nodeSelector"]},
            "spec": {"taints": [{"key": "railshot.io/dedicated", "value": "build", "effect": "NoSchedule"}]}}
        preflight.validate_node(node, pod)
        for mutation in ("wrong-node", "wrong-label", "control-plane", "master", "no-taint", "wrong-taint"):
            with self.subTest(mutation=mutation):
                bad = copy.deepcopy(node)
                if mutation == "wrong-node":
                    bad["metadata"]["name"] = "another-worker"
                elif mutation == "wrong-label":
                    bad["metadata"]["labels"]["railshot.io/node-role"] = "operations"
                elif mutation in ("control-plane", "master"):
                    bad["metadata"]["labels"]["node-role.kubernetes.io/" + mutation] = ""
                elif mutation == "no-taint":
                    bad["spec"]["taints"] = []
                else:
                    bad["spec"]["taints"][0]["effect"] = "PreferNoSchedule"
                with self.assertRaises(ValueError):
                    preflight.validate_node(bad, pod)

    def test_pod_identity_scheduling_and_privilege_drift_fail(self):
        for section, key, value in (
                ("metadata", "uid", "other-pod"), ("metadata", "namespace", "default"),
                ("spec", "hostNetwork", False), ("spec", "hostPID", True), ("spec", "hostIPC", True),
                ("spec", "automountServiceAccountToken", True), ("spec", "serviceAccountName", "default"),
                ("spec", "restartPolicy", "Always"), ("spec", "nodeName", "control-plane"),
                ("spec", "nodeSelector", {"kubernetes.io/arch": "amd64"}),
                ("spec", "tolerations", [{"operator": "Exists"}]), ("spec", "initContainers", [{"name": "escape"}])):
            with self.subTest(section=section, key=key):
                pod = pod_fixture(); pod[section][key] = value
                with self.assertRaises(ValueError):
                    self.validate(pod)
        for key, value in (("privileged", True), ("runAsUser", 1000), ("allowPrivilegeEscalation", True),
                           ("capabilities", {"add": ["NET_ADMIN", "SYS_ADMIN"]}), ("seccompProfile", {"type": "Unconfined"}), ("seccompProfile", {"type": "RuntimeDefault"}),
                           ("appArmorProfile", {"type": "Unconfined"}), ("appArmorProfile", {"type": "RuntimeDefault"})):
            with self.subTest(key=key):
                pod = pod_fixture(); pod["spec"]["containers"][0]["securityContext"][key] = value
                with self.assertRaises(ValueError):
                    self.validate(pod)

    def test_mount_modes_and_projected_credentials_fail_closed(self):
        for mutation in ("host-path", "private-rancher", "writable-receipt", "extra-secret", "registration-mode", "registration-symlink",
                         "projection-mode", "foreign-audience", "unbounded-token", "extra-projection", "literal-identity", "secret-env"):
            with self.subTest(mutation=mutation):
                pod = pod_fixture(); spec = pod["spec"]; runner = spec["containers"][0]
                if mutation == "host-path":
                    spec["volumes"][0]["hostPath"]["path"] = "/root/.kube"
                elif mutation == "private-rancher":
                    spec["volumes"].append({"name": "rancher", "hostPath": {"path": "/var/lib/rancher", "type": "Directory"}})
                    runner["volumeMounts"].append({"name": "rancher", "mountPath": "/host/var/lib/rancher", "readOnly": True})
                elif mutation == "writable-receipt":
                    next(item for item in runner["volumeMounts"] if item["mountPath"] == "/var/lib/railshot-ci")["readOnly"] = False
                elif mutation == "extra-secret":
                    spec["volumes"].append({"name": "cloud", "secret": {"secretName": "cloud"}})
                elif mutation == "registration-mode":
                    spec["volumes"][-2]["secret"]["defaultMode"] = 0o644
                elif mutation == "registration-symlink":
                    del runner["volumeMounts"][-2]["subPath"]
                elif mutation == "projection-mode":
                    spec["volumes"][-1]["projected"]["defaultMode"] = 0o644
                elif mutation == "foreign-audience":
                    spec["volumes"][-1]["projected"]["sources"][0]["serviceAccountToken"]["audience"] = "another-service"
                elif mutation == "unbounded-token":
                    spec["volumes"][-1]["projected"]["sources"][0]["serviceAccountToken"]["expirationSeconds"] = 86400
                elif mutation == "extra-projection":
                    spec["volumes"][-1]["projected"]["sources"].append({"secret": {"name": "other"}})
                elif mutation == "literal-identity":
                    runner["env"][0] = {"name": "RAILSHOT_POD_NAMESPACE", "value": "railshot-build"}
                else:
                    runner["envFrom"] = [{"secretRef": {"name": "cloud"}}]
                with self.assertRaises(ValueError):
                    self.validate(pod)

    def test_root_marker_preserves_standalone_rejection_and_explicit_worker_approval(self):
        cases = [
            ("dedicated-ci-ubuntu-24.04", set(), set(), "standalone"),
            ("dedicated-ci-ubuntu-24.04", {"/host/etc/rancher/k3s"}, set(), None),
            ("dedicated-ci-ubuntu-24.04", set(), {"/host/etc/rancher/k3s"}, None),
            # K3s credentials are not mounted; approved mode checks the live Node next.
            ("ops-k3s-build-worker-ubuntu-24.04", set(), set(), "kubernetes"),
            ("allow-any-k3s", {"/host/var/lib/rancher/k3s/agent"}, set(), None),
        ]
        for marker, existing, symlinks, expected in cases:
            with self.subTest(marker=marker, existing=existing), patch.object(preflight, "owned_file") as owned, \
                    patch.object(Path, "read_text", return_value=marker), \
                    patch.object(Path, "exists", lambda path: str(path) in existing), \
                    patch.object(Path, "is_dir", lambda path: str(path) in existing), \
                    patch.object(Path, "is_symlink", lambda path: str(path) in symlinks):
                if expected:
                    self.assertEqual(preflight.host_mode(), expected)
                else:
                    with self.assertRaises(ValueError):
                        preflight.host_mode()
                owned.assert_called_once_with(preflight.HOST_MARKER, 0o644)
        with patch.object(preflight, "owned_file", side_effect=ValueError("wrong ownership")), self.assertRaises(ValueError):
            preflight.host_mode()
        with patch.object(preflight, "owned_file"), patch.object(Path, "read_text", return_value="dedicated-ci-ubuntu-24.04"), \
                patch.dict(os.environ, {"RAILSHOT_POD_NAMESPACE": "railshot-build"}), self.assertRaisesRegex(ValueError, "standalone marker"):
            preflight.host_mode()

    def test_pod_read_uses_bound_identity_verified_ca_and_read_only_https(self):
        pod = pod_fixture(); metadata = pod["metadata"]
        claims = {"kubernetes.io": {"namespace": metadata["namespace"], "pod": {"name": metadata["name"], "uid": metadata["uid"]},
                                   "serviceaccount": {"name": "railshot-build-runner"}}}
        token = "header." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".signature"
        environment = {"RAILSHOT_POD_NAMESPACE": metadata["namespace"], "RAILSHOT_POD_NAME": metadata["name"],
                       "RAILSHOT_POD_UID": metadata["uid"], "KUBERNETES_SERVICE_HOST": "10.53.0.1"}
        node = {"apiVersion": "v1", "kind": "Node", "metadata": {"name": "worker-01", "labels": pod["spec"]["nodeSelector"]},
                "spec": {"taints": [{"key": "railshot.io/dedicated", "value": "build", "effect": "NoSchedule"}]}}
        response = Mock(status=200); response.read.side_effect = [json.dumps(pod).encode(), json.dumps(node).encode()]
        with patch.dict(os.environ, environment, clear=True), patch.object(Path, "read_text", return_value=token), \
                patch.object(preflight.ssl, "create_default_context") as context, patch.object(preflight, "HTTPSConnection") as https:
            connection = https.return_value; connection.getresponse.return_value = response
            self.assertEqual(preflight.read_own_pod(), pod)
            context.assert_called_once_with(cafile="/run/railshot-kubernetes/ca.crt")
            self.assertEqual(https.call_count, 2)
            for call in https.call_args_list:
                self.assertEqual(call.args, ("10.53.0.1", 443))
                self.assertEqual(call.kwargs, {"context": context.return_value, "timeout": 10})
            self.assertEqual([call.args for call in connection.request.call_args_list], [
                ("GET", "/api/v1/namespaces/railshot-build/pods/runner-attempt-abc"), ("GET", "/api/v1/nodes/worker-01")])
            self.assertEqual(connection.close.call_count, 2)
            response.status = 302
            with self.assertRaisesRegex(ValueError, "HTTP 302"):
                preflight.read_own_pod()
            self.assertEqual(connection.request.call_count, 3)  # Redirect is never followed.
            os.environ["RAILSHOT_POD_UID"] = "22222222-2222-2222-2222-222222222222"
            with self.assertRaisesRegex(ValueError, "bound to this runner Pod"):
                preflight.read_own_pod()
            self.assertEqual(connection.request.call_count, 3)  # Foreign Pod token never reaches API.


if __name__ == "__main__":
    unittest.main()
