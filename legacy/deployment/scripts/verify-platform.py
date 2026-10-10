#!/usr/bin/env python3
"""Read-only platform acceptance: exact Argo revision, ready digest-bound Pods, public HTTP."""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

INSTANCE = "i-033ae2db907fde68e"
REGION = "ap-northeast-2"
DOCUMENT = "Railshot-VerifyPlatform"
PUBLIC_URL = "https://railshot.io"
REPOSITORY = "https://github.com/Jasmin-Softbank/Railshot.git"


class NotReady(Exception):
    """Only fixed reason codes cross the SSM/Actions output boundary."""


REASONS = {"ARGO_REVISION_NOT_HEALTHY", "DEPLOYMENT_NOT_READY", "POD_COUNT_NOT_READY",
           "POD_DIGEST_NOT_READY", "KUBERNETES_READ_FAILED", "CLUSTER_READ_FAILED",
           "API_PREPARATION_PENDING", "API_PREPARATION_RETRY", "ARGO_PREVIOUS_REVISION_RUNNING",
           "ARGO_SYNC_FAILED"}


def expected(revision, dashboard_digest, api_digest):
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise NotReady("INVALID_REVISION")
    digests = {"dashboard": dashboard_digest, "api": api_digest}
    if any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in digests.values()):
        raise NotReady("INVALID_IMAGE_DIGEST")
    return {name: f"ghcr.io/jasmin-softbank/railshot-{name}@sha256:{value}"
            for name, value in digests.items()}


def kubectl(kind, name, namespace, selector=None):
    command = ["/usr/local/bin/k3s", "kubectl", "--request-timeout=5s", "-n", namespace,
               "get", kind, "-o", "json"]
    command += [name] if name else ["-l", selector]
    result = subprocess.run(command, capture_output=True, text=True, timeout=8)
    if result.returncode:
        raise NotReady("KUBERNETES_READ_FAILED")
    return json.loads(result.stdout)


def snapshot(revision, images, read=kubectl):
    app = read("applications.argoproj.io", "railshot-platform", "argocd")
    source = app.get("spec", {}).get("source", {})
    status = app.get("status", {})
    operation = status.get("operationState", {})
    operation_revision = operation.get("syncResult", {}).get("revision")
    if operation_revision == revision and operation.get("phase") in {"Failed", "Error"}:
        raise NotReady("ARGO_SYNC_FAILED")
    if operation.get("phase") in {"Running", "Terminating"}:
        if operation_revision and operation_revision != revision:
            raise NotReady("ARGO_PREVIOUS_REVISION_RUNNING")
        preparation = [row for row in operation.get("syncResult", {}).get("resources", [])
                       if row.get("kind") == "Job" and row.get("name") == "railshot-api-prepare"]
        if any(row.get("hookPhase") in {"Failed", "Error"} for row in preparation):
            raise NotReady("API_PREPARATION_RETRY")
        if any(row.get("hookPhase") in {"Running", "Pending"} for row in preparation):
            raise NotReady("API_PREPARATION_PENDING")
    if (source.get("repoURL") != REPOSITORY or source.get("targetRevision") != "deployment/platform"
            or source.get("path") != "gitops/applications/railshot-platform"
            or app.get("spec", {}).get("project") != "railshot-platform"
            or app.get("spec", {}).get("destination") != {"server": "https://kubernetes.default.svc", "namespace": "railshot-system"}
            or status.get("sync", {}).get("revision") != revision
            or status.get("sync", {}).get("status") != "Synced"
            or status.get("health", {}).get("status") != "Healthy"):
        raise NotReady("ARGO_REVISION_NOT_HEALTHY")
    observed = {}
    for component, image in images.items():
        name = "railshot-" + component
        deployment = read("deployments.apps", name, "railshot-system")
        spec, state = deployment["spec"], deployment.get("status", {})
        containers = spec["template"]["spec"]["containers"]
        if (spec.get("replicas", 1) != 1 or len(containers) != 1
                or containers[0].get("name") != component or containers[0].get("image") != image
                or state.get("observedGeneration", 0) < deployment["metadata"]["generation"]
                or any(state.get(key, 0) != 1 for key in ("replicas", "updatedReplicas", "readyReplicas", "availableReplicas"))):
            raise NotReady("DEPLOYMENT_NOT_READY")
        replicas = read("replicasets.apps", None, "railshot-system", "app=" + name)["items"]
        owners = {item["metadata"]["uid"] for item in replicas
                  if any(owner.get("uid") == deployment["metadata"]["uid"] and owner.get("controller") is True
                         for owner in item["metadata"].get("ownerReferences", []))}
        pods = read("pods", None, "railshot-system", "app=" + name)["items"]
        pods = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp")
                and any(owner.get("uid") in owners and owner.get("controller") is True
                        for owner in pod["metadata"].get("ownerReferences", []))]
        if len(pods) != 1:
            raise NotReady("POD_COUNT_NOT_READY")
        pod = pods[0]
        state = pod.get("status", {})
        running = state.get("containerStatuses", [])
        pod_containers = pod.get("spec", {}).get("containers", [])
        if (state.get("phase") != "Running" or not any(c.get("type") == "Ready" and c.get("status") == "True"
                                                       for c in state.get("conditions", []))
                or len(pod_containers) != 1 or pod_containers[0].get("image") != image
                or len(running) != 1 or running[0].get("name") != component or running[0].get("ready") is not True
                or running[0].get("imageID", "").removeprefix("docker-pullable://") != image):
            raise NotReady("POD_DIGEST_NOT_READY")
        observed[component] = {"image": image, "pod": pod["metadata"]["name"], "pod_uid": pod["metadata"]["uid"]}
    return {"status": "cluster_verified", "revision": revision, "components": observed}


def local(revision, images):
    # One short read per pinned SSM command. The caller owns the deadline and can
    # show the actual Argo phase instead of hiding ten minutes inside InProgress.
    try:
        return snapshot(revision, images)
    except NotReady as error:
        if str(error) == "ARGO_SYNC_FAILED":
            raise
        return {"status": "waiting", "code": str(error), "revision": revision}
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        return {"status": "waiting", "code": "CLUSTER_READ_FAILED", "revision": revision}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def public_health():
    # The hosted Actions runner verifies the public edge, never the control node's localhost.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    for path in ("/healthz", "/api/v1/targets"):
        try:
            with opener.open(Request(PUBLIC_URL + path, headers={"Cache-Control": "no-cache"}), timeout=10) as response:
                body = response.read(65537)
                if response.status != 200 or len(body) > 65536:
                    raise NotReady("PUBLIC_HTTP_FAILED")
            if path == "/healthz":
                if body != b"ok\n":
                    raise NotReady("PUBLIC_HEALTH_BODY_MISMATCH")
            else:
                value = json.loads(body)
                if not isinstance(value, dict) or not isinstance(value.get("items"), list) or "next_marker" not in value:
                    raise NotReady("PUBLIC_API_BODY_MISMATCH")
        except (OSError, URLError, HTTPError, ValueError):
            raise NotReady("PUBLIC_HTTP_FAILED") from None
    return {"url": PUBLIC_URL, "health": "succeeded", "api": "succeeded"}


def aws(*args):
    result = subprocess.run(["aws", "--region", REGION, "--cli-connect-timeout", "5", "--cli-read-timeout", "15",
                             "ssm", *args, "--output", "json"], capture_output=True, text=True, timeout=25,
                            env={**os.environ, "AWS_MAX_ATTEMPTS": "1", "AWS_PAGER": ""})
    if result.returncode:
        if "InvocationDoesNotExist" in result.stderr:
            return {"Status": "Pending"}
        raise NotReady("SSM_REQUEST_FAILED")
    return json.loads(result.stdout)


def remote(revision, images, version, document_hash, call=aws, health=public_health):
    if not re.fullmatch(r"[1-9][0-9]*", version) or not re.fullmatch(r"[a-f0-9]{64}", document_hash):
        raise NotReady("INVALID_DOCUMENT_PIN")
    parameters = {"Revision": [revision], **{name.title() + "Digest": [image.rsplit(":", 1)[1]]
                                           for name, image in images.items()}}
    # Send each read-only probe once. An uncertain submission fails; there is no
    # session or arbitrary-command fallback.
    started = time.monotonic()
    # Preparation allows 15 min including image pull; allow 2 min for rollout.
    deadline, next_log = started + 1020, started
    command_id = None
    reason = "CLUSTER_NOT_READY"
    print(f"Verifying Argo revision {revision} and ready API/dashboard image digests; then public HTTPS.", file=sys.stderr, flush=True)
    while time.monotonic() < deadline:
        if command_id is None:
            command = call("send-command", "--document-name", DOCUMENT, "--document-version", version,
                           "--document-hash", document_hash, "--document-hash-type", "Sha256",
                           "--instance-ids", INSTANCE, "--timeout-seconds", "60", "--parameters", json.dumps(parameters))
            command_id = command["Command"]["CommandId"]
            if not re.fullmatch(r"[a-f0-9-]{36}", command_id):
                raise NotReady("INVALID_COMMAND_ID")
        result = call("get-command-invocation", "--command-id", command_id, "--instance-id", INSTANCE)
        if result.get("Status") == "Success":
            if result.get("ResponseCode") != 0:
                raise NotReady("REMOTE_VERIFIER_FAILED")
            proof = json.loads(result.get("StandardOutputContent", ""))
            if proof.get("status") == "waiting":
                if proof.get("revision") != revision or proof.get("code") not in REASONS:
                    raise NotReady("REMOTE_PROOF_MISMATCH")
                reason = proof["code"]
                print(f"Waiting for platform: {reason}, elapsed={int(time.monotonic() - started)}s/1020s, revision={revision}.", file=sys.stderr, flush=True)
                command_id = None
                time.sleep(min(10, max(0, deadline - time.monotonic())))
                continue
            if (proof.get("status") != "cluster_verified" or proof.get("revision") != revision
                    or {key: item.get("image") for key, item in proof.get("components", {}).items()} != images):
                raise NotReady("REMOTE_PROOF_MISMATCH")
            return {**proof, "status": "verified", "command_id": command_id, "public_http": health(),
                    "verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        if result.get("Status") not in {"Pending", "InProgress", "Delayed"}:
            try:
                failure = json.loads(result.get("StandardOutputContent", ""))
                reason = failure.get("code") if failure.get("status") == "failed" else None
            except (ValueError, AttributeError):
                reason = None
            raise NotReady(reason if reason in REASONS else "REMOTE_VERIFIER_FAILED")
        now = time.monotonic()
        if now >= next_log:
            print(f"Waiting for cluster verification: SSM={result['Status']}, elapsed={int(now - started)}s/1020s, revision={revision}.", file=sys.stderr, flush=True)
            next_log = now + 30
        time.sleep(5)
    raise NotReady(reason if reason in REASONS else "SSM_VERIFICATION_TIMEOUT")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("local", "remote"))
    parser.add_argument("--revision", required=True)
    parser.add_argument("--dashboard-digest", required=True)
    parser.add_argument("--api-digest", required=True)
    parser.add_argument("--document-version", default="")
    parser.add_argument("--document-hash", default="")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    try:
        images = expected(args.revision, args.dashboard_digest, args.api_digest)
        result = (local(args.revision, images) if args.mode == "local"
                  else remote(args.revision, images, args.document_version, args.document_hash))
        code = 0
    except (NotReady, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        result = {"status": "failed", "code": str(error) if isinstance(error, NotReady) else "VERIFICATION_FAILED"}
        code = 1
    encoded = json.dumps(result) + "\n"
    if args.receipt:
        args.receipt.write_text(encoded)
    print(encoded, end="")
    raise SystemExit(code)
