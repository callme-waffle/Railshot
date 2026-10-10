"""Independent fake-cluster + localhost edge: no AWS, GitHub or real Kubernetes calls."""
import copy
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("verify_platform", Path(__file__).resolve().parents[1] / "verify-platform.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)
verify_snapshot = verify.snapshot


class PlatformVerificationTests(unittest.TestCase):
    def test_one_probe_reports_preparation_and_old_revision_without_sleeping(self):
        revision = 'a' * 40
        for phase, hook, other, expected in [
            ('Running', 'Running', False, 'API_PREPARATION_PENDING'),
            ('Running', 'Failed', False, 'API_PREPARATION_RETRY'),
            ('Running', 'Running', True, 'ARGO_PREVIOUS_REVISION_RUNNING'),
            ('Failed', 'Failed', False, 'ARGO_SYNC_FAILED'),
        ]:
            app = {'status': {'operationState': {'phase': phase, 'syncResult': {
                'revision': 'b' * 40 if other else revision,
                'resources': [{'kind': 'Job', 'name': 'railshot-api-prepare', 'hookPhase': hook}]}}}}
            with self.subTest(expected=expected), patch.object(verify, 'kubectl', return_value=app):
                # snapshot's bound default is replaced explicitly to keep this offline.
                with patch.object(verify, 'snapshot', side_effect=lambda r, i: verify_snapshot(r, i, lambda *a: app)):
                    if phase == 'Failed':
                        with self.assertRaisesRegex(verify.NotReady, expected):
                            verify.local(revision, {})
                    else:
                        self.assertEqual(verify.local(revision, {}), {'status': 'waiting', 'code': expected, 'revision': revision})

    def test_remote_repeats_only_completed_probes_and_keeps_final_image_proof(self):
        revision = 'a' * 40
        images = verify.expected(revision, 'b' * 64, 'c' * 64)
        command = {'Command': {'CommandId': '12345678-1234-1234-1234-123456789012'}}
        waiting = {'Status': 'Success', 'ResponseCode': 0, 'StandardOutputContent': json.dumps({
            'status': 'waiting', 'revision': revision, 'code': 'API_PREPARATION_PENDING'})}
        done = {'Status': 'Success', 'ResponseCode': 0, 'StandardOutputContent': json.dumps({
            'status': 'cluster_verified', 'revision': revision, 'components': {k: {'image': v} for k, v in images.items()}})}
        replies = iter([command, waiting, command, done]); calls = []
        def ssm(*args):
            calls.append(args[0]); return next(replies)
        with patch.object(verify.time, 'sleep'), patch('builtins.print') as output:
            proof = verify.remote(revision, images, '2', 'e' * 64, call=ssm, health=lambda: {'health': 'succeeded'})
        self.assertEqual(proof['status'], 'verified')
        self.assertEqual(calls, ['send-command', 'get-command-invocation'] * 2)
        self.assertIn('API_PREPARATION_PENDING', str(output.call_args_list))

    def test_revision_running_digest_readiness_and_external_http_are_all_required(self):
        revision = "a" * 40
        images = verify.expected(revision, "b" * 64, "c" * 64)
        cluster = {("applications.argoproj.io", "railshot-platform"): {
            "spec": {"project": "railshot-platform", "source": {"repoURL": verify.REPOSITORY,
                     "targetRevision": "deployment/platform", "path": "gitops/applications/railshot-platform"},
                     "destination": {"server": "https://kubernetes.default.svc", "namespace": "railshot-system"}},
            "status": {"sync": {"revision": revision, "status": "Synced"}, "health": {"status": "Healthy"}}}}
        for component, image in images.items():
            name = "railshot-" + component
            cluster[("deployments.apps", name)] = {
                "metadata": {"uid": "deployment-" + component, "generation": 3},
                "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": component, "image": image}]}}},
                "status": {"observedGeneration": 3, "replicas": 1, "updatedReplicas": 1, "readyReplicas": 1, "availableReplicas": 1}}
            cluster[("replicasets.apps", "app=" + name)] = {"items": [{"metadata": {
                "uid": "rs-" + component, "ownerReferences": [{"uid": "deployment-" + component, "controller": True}]}}]}
            cluster[("pods", "app=" + name)] = {"items": [{
                "metadata": {"uid": "pod-" + component, "name": name + "-one",
                             "ownerReferences": [{"uid": "rs-" + component, "controller": True}]},
                "spec": {"containers": [{"name": component, "image": image}]},
                "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                           "containerStatuses": [{"name": component, "ready": True, "imageID": image}]}}]}

        def reader(kind, name, namespace, selector=None):
            self.assertIn(namespace, {"argocd", "railshot-system"})
            return cluster[(kind, name or selector)]

        proof = verify.snapshot(revision, images, reader)
        for failure in ("revision", "digest", "readiness", "ownership"):
            changed = copy.deepcopy(cluster)
            if failure == "revision":
                changed[("applications.argoproj.io", "railshot-platform")]["status"]["sync"]["revision"] = "d" * 40
            else:
                pod = changed[("pods", "app=railshot-api")]["items"][0]
                if failure == "digest":
                    pod["status"]["containerStatuses"][0]["imageID"] = images["api"].replace("c" * 64, "d" * 64)
                elif failure == "readiness":
                    pod["status"]["conditions"][0]["status"] = "False"
                else:
                    pod["metadata"]["ownerReferences"][0]["uid"] = "foreign-replicaset"
            with self.subTest(failure=failure), self.assertRaises(verify.NotReady):
                verify.snapshot(revision, images, lambda kind, name, namespace, selector=None: changed[(kind, name or selector)])

        class Edge(BaseHTTPRequestHandler):
            api_status = 200

            def do_GET(self):
                self.send_response(self.api_status if self.path == "/api/v1/targets" else 200)
                self.end_headers()
                self.wfile.write(b'ok\n' if self.path == "/healthz" else b'{"items":[],"next_marker":null}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Edge)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        calls = []

        def ssm(*args):
            calls.append(args)
            if args[0] == "send-command":
                self.assertEqual(args[args.index("--instance-ids") + 1], verify.INSTANCE)
                self.assertEqual(args[args.index("--document-name") + 1], verify.DOCUMENT)
                self.assertEqual(args[args.index("--document-version") + 1], "2")
                self.assertEqual(args[args.index("--document-hash") + 1], "e" * 64)
                parameters = json.loads(args[args.index("--parameters") + 1])
                self.assertEqual(parameters, {"Revision": [revision], "DashboardDigest": ["b" * 64], "ApiDigest": ["c" * 64]})
                return {"Command": {"CommandId": "12345678-1234-1234-1234-123456789012"}}
            self.assertEqual(args[0], "get-command-invocation")
            return {"Status": "Success", "ResponseCode": 0, "StandardOutputContent": json.dumps(proof)}

        with patch.object(verify, "PUBLIC_URL", f"http://127.0.0.1:{server.server_port}"):
            result = verify.remote(revision, images, "2", "e" * 64, call=ssm)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["public_http"]["api"], "succeeded")
            self.assertEqual([call[0] for call in calls], ["send-command", "get-command-invocation"])
            Edge.api_status = 503
            with self.assertRaises(verify.NotReady):
                verify.remote(revision, images, "2", "e" * 64, call=ssm)
        self.assertEqual(verify.PUBLIC_URL, "https://railshot.io")
        for bad in ("x" * 40, "a" * 40 + ";id"):
            with self.assertRaises(verify.NotReady):
                verify.expected(bad, "b" * 64, "c" * 64)


    def test_remote_wait_is_visible_and_failure_preserves_only_known_reasons(self):
        revision, images = "a" * 40, verify.expected("a" * 40, "b" * 64, "c" * 64)
        for code, expected in [("ARGO_REVISION_NOT_HEALTHY", "ARGO_REVISION_NOT_HEALTHY"),
                               ("untrusted-secret-value", "REMOTE_VERIFIER_FAILED")]:
            responses = iter([{"Command": {"CommandId": "12345678-1234-1234-1234-123456789012"}},
                              {"Status": "InProgress"},
                              {"Status": "Failed", "StandardOutputContent": json.dumps({"status": "failed", "code": code})}])
            with patch.object(verify.time, "sleep"), patch("builtins.print") as output:
                with self.assertRaisesRegex(verify.NotReady, "^" + expected + "$"):
                    verify.remote(revision, images, "2", "e" * 64, call=lambda *args: next(responses))
                messages = " ".join(str(call.args[0]) for call in output.call_args_list)
                self.assertIn("Waiting for cluster verification: SSM=InProgress", messages)
                self.assertNotIn("untrusted-secret-value", messages)


if __name__ == "__main__":
    unittest.main()
