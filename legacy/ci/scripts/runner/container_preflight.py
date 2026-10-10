#!/usr/bin/env python3
"""Fail closed before registering the trusted runner on its dedicated CI host."""
import base64
import json
import ipaddress
from http.client import HTTPSConnection
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import subprocess
import sys

ROOT = "/var/lib/railshot-runner"
TOKEN = "/run/secrets/github-runner-registration-token"
KUBERNETES = "/run/railshot-kubernetes"
SECCOMP = {"type": "Localhost", "localhostProfile": "railshot-codex-bwrap.json"}
APPARMOR = {"type": "Localhost", "localhostProfile": "railshot-codex-bwrap"}
NAMESPACE = "railshot-build"
SERVICE_ACCOUNT = "railshot-build-runner"
HOST_MARKER = "/etc/railshot/ci-runner-host"
READ_ONLY = {
    "/etc/railshot": "/etc/railshot",
    "/usr/local/sbin/railshot-ci-network": "/usr/local/sbin/railshot-ci-network",
    "/var/lib/railshot-ci": "/var/lib/railshot-ci",
    "/host/etc/rancher": "/etc/rancher",
    "/host/var/lib/rancher": "/var/lib/rancher",
}
POD_READ_ONLY = {target: source for target, source in READ_ONLY.items() if not target.startswith("/host/")}
READ_WRITE = {path: path for path in (
    "/var/run/docker.sock", "/run/railshot-ci-network.lock",
    ROOT + "/work", ROOT + "/runs", ROOT + "/codex",
)}


def validate_container(container):
    host = container["HostConfig"]
    if host.get("NetworkMode") != "host" or host.get("Privileged") or host.get("PidMode") == "host":
        raise ValueError("runner requires host network without privileged/host PID mode")
    if set(host.get("CapAdd") or []) != {"NET_ADMIN"}:
        raise ValueError("runner requires only NET_ADMIN in addition to Docker default capabilities")
    options = ["no-new-privileges" if item in {"no-new-privileges=true", "no-new-privileges:true"} else item.replace("apparmor:", "apparmor=", 1)
               for item in host.get("SecurityOpt") or []]
    expected_seccomp = json.loads((Path(__file__).parent / "railshot-codex-bwrap.json").read_text())
    seccomp = [item.split("=", 1)[1] for item in options if item.startswith("seccomp=")]
    if (len(seccomp) != 1 or json.loads(seccomp[0]) != expected_seccomp
            or set(item for item in options if not item.startswith("seccomp=")) != {"no-new-privileges", "apparmor=railshot-codex-bwrap"}
            or container.get("AppArmorProfile") != "railshot-codex-bwrap"):
        raise ValueError("reviewed seccomp/AppArmor profiles and no-new-privileges are required")
    mounts = {mount["Destination"]: mount for mount in container["Mounts"]}
    if set(mounts) != set(READ_ONLY) | set(READ_WRITE) | {TOKEN}:
        raise ValueError("unexpected/missing mount; do not mount kubeconfig or cloud credentials")
    for paths, writable in ((READ_ONLY, False), (READ_WRITE, True)):
        for target, source in paths.items():
            mount = mounts[target]
            if mount["Type"] != "bind" or mount["Source"] != source or mount["RW"] != writable:
                raise ValueError("incorrect mount: " + target)
    if mounts[TOKEN]["RW"] or mounts[TOKEN]["Type"] != "bind":
        raise ValueError("registration token must be a read-only file mount")


def owned_file(path, mode):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != mode:
        raise ValueError("root-owned file with mode " + oct(mode) + " required: " + path)


def host_mode():
    owned_file(HOST_MARKER, 0o644)
    marker = Path(HOST_MARKER).read_text().strip()
    if marker == "dedicated-ci-ubuntu-24.04":
        if any(os.environ.get(key) for key in ("RAILSHOT_POD_NAMESPACE", "RAILSHOT_POD_NAME", "RAILSHOT_POD_UID")):
            raise ValueError("standalone marker cannot authorize a Kubernetes runner")
        for path in ("/host/etc/rancher/k3s", "/host/var/lib/rancher/k3s"):
            if Path(path).exists() or Path(path).is_symlink():
                raise ValueError("refusing a K3s host in standalone mode")
        return "standalone"
    if marker == "ops-k3s-build-worker-ubuntu-24.04":
        # Host preparation checks agent-only state before writing this root-owned
        # approval. Never mount the agent credential directories into the Pod.
        return "kubernetes"
    raise ValueError("dedicated CI host marker mismatch")


def validate_pod(pod, namespace, name, uid):
    if (namespace != NAMESPACE or pod.get("apiVersion") != "v1" or pod.get("kind") != "Pod"
            or any(pod.get("metadata", {}).get(key) != value
                   for key, value in (("namespace", namespace), ("name", name), ("uid", uid)))):
        raise ValueError("runner Pod identity mismatch")
    spec = pod["spec"]
    selector = spec.get("nodeSelector", {})
    if (spec.get("hostNetwork") is not True or spec.get("hostPID", False)
            or spec.get("hostIPC", False) or spec.get("automountServiceAccountToken") is not False
            or spec.get("serviceAccountName") != SERVICE_ACCOUNT or spec.get("restartPolicy") != "Never"
            or spec.get("initContainers") or spec.get("ephemeralContainers")
            or set(selector) != {"railshot.io/node-role", "kubernetes.io/arch", "kubernetes.io/hostname"}
            or selector.get("railshot.io/node-role") != "build" or selector.get("kubernetes.io/arch") != "amd64"
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", selector.get("kubernetes.io/hostname", ""))
            or spec.get("nodeName") != selector["kubernetes.io/hostname"]):
        raise ValueError("runner Pod must use the dedicated build-node execution profile")
    tolerations = spec.get("tolerations", [])
    dedicated = {"key": "railshot.io/dedicated", "operator": "Equal", "value": "build", "effect": "NoSchedule"}
    if dedicated not in tolerations:
        raise ValueError("dedicated build taint toleration is required")
    for item in tolerations:
        if item == dedicated:
            continue
        # Kubernetes adds these two bounded node-health tolerations automatically.
        if (item.get("key") not in {"node.kubernetes.io/not-ready", "node.kubernetes.io/unreachable"}
                or item.get("operator") != "Exists" or item.get("effect") != "NoExecute"
                or type(item.get("tolerationSeconds")) is not int or not 0 <= item["tolerationSeconds"] <= 300
                or set(item) != {"key", "operator", "effect", "tolerationSeconds"}):
            raise ValueError("unexpected runner node toleration")
    containers = spec.get("containers", [])
    if len(containers) != 1 or containers[0].get("name") != "runner":
        raise ValueError("one trusted runner container is required")
    runner = containers[0]
    environment = {item["name"]: item for item in runner.get("env", [])}
    fields = {"RAILSHOT_POD_NAMESPACE": "metadata.namespace", "RAILSHOT_POD_NAME": "metadata.name",
              "RAILSHOT_POD_UID": "metadata.uid"}
    if runner.get("envFrom") or len(environment) != len(runner.get("env", [])):
        raise ValueError("runner environment must not import credentials")
    for key, field in fields.items():
        value = environment.get(key, {}).get("valueFrom", {})
        if (set(value) != {"fieldRef"} or value["fieldRef"].get("fieldPath") != field
                or value["fieldRef"].get("apiVersion", "v1") != "v1" or "value" in environment[key]):
            raise ValueError("runner identity must come from the Downward API")
    if any(set(item["valueFrom"]) != {"fieldRef"} for item in environment.values() if "valueFrom" in item):
        raise ValueError("runner environment must not import Secrets or ConfigMaps")
    security = runner.get("securityContext", {})
    if (security.get("privileged", False) or security.get("runAsUser", spec.get("securityContext", {}).get("runAsUser")) != 0
            or set(security.get("capabilities", {}).get("add", [])) != {"NET_ADMIN"}
            or security.get("allowPrivilegeEscalation") is not False
            or security.get("seccompProfile", spec.get("securityContext", {}).get("seccompProfile")) != SECCOMP
            or security.get("appArmorProfile", spec.get("securityContext", {}).get("appArmorProfile")) != APPARMOR):
        raise ValueError("unexpected runner Pod privileges")
    mounts = {mount["mountPath"]: mount for mount in runner.get("volumeMounts", [])}
    volumes = {volume["name"]: volume for volume in spec.get("volumes", [])}
    expected = set(POD_READ_ONLY) | set(READ_WRITE) | {TOKEN, KUBERNETES}
    if (set(mounts) != expected or len(mounts) != len(runner["volumeMounts"])
            or len(volumes) != len(spec["volumes"])
            or {mount["name"] for mount in mounts.values()} != set(volumes)
            or len(volumes) != len(mounts)):
        raise ValueError("unexpected/missing runner Pod volume or mount")
    for paths, writable in ((POD_READ_ONLY, False), (READ_WRITE, True)):
        for target, source in paths.items():
            mount, volume = mounts[target], volumes[mounts[target]["name"]]
            kind = "Socket" if source.endswith("docker.sock") else "File" if source in {
                "/run/railshot-ci-network.lock", "/usr/local/sbin/railshot-ci-network"} else "Directory"
            if (set(mount) - {"name", "mountPath", "readOnly"} or mount.get("readOnly", False) != (not writable)
                    or set(volume) != {"name", "hostPath"} or volume["hostPath"] != {"path": source, "type": kind}):
                raise ValueError("incorrect runner Pod hostPath mount: " + target)
    registration = mounts[TOKEN]
    secret = volumes[registration["name"]]
    if (registration != {"name": registration["name"], "mountPath": TOKEN, "subPath": "token", "readOnly": True}
            or set(secret) != {"name", "secret"}
            or secret["secret"] != {"secretName": "railshot-build-runner-registration", "defaultMode": 0o600,
                                    "items": [{"key": "token", "path": "token"}]}):
        raise ValueError("registration token must be the dedicated read-only 0600 Secret file")
    api_mount = mounts[KUBERNETES]
    projected = volumes[api_mount["name"]]
    if (api_mount != {"name": api_mount["name"], "mountPath": KUBERNETES, "readOnly": True}
            or set(projected) != {"name", "projected"}
            or set(projected["projected"]) != {"defaultMode", "sources"}
            or projected["projected"]["defaultMode"] != 0o400):
        raise ValueError("only the dedicated read-only Pod API projection is permitted")
    sources = projected["projected"]["sources"]
    if len(sources) != 2:
        raise ValueError("Pod API projection requires only a service account token and CA")
    token = next((source["serviceAccountToken"] for source in sources if set(source) == {"serviceAccountToken"}), None)
    ca = {"configMap": {"name": "kube-root-ca.crt", "items": [{"key": "ca.crt", "path": "ca.crt"}]}}
    if (token is None or ca not in sources or set(token) - {"path", "expirationSeconds"}
            or token.get("path") != "token" or type(token.get("expirationSeconds", 3600)) is not int
            or not 600 <= token.get("expirationSeconds", 3600) <= 3600):
        raise ValueError("Pod API token must use the default audience and a bounded lifetime")


def validate_node(node, pod):
    name = pod["spec"]["nodeName"]
    metadata = node.get("metadata", {})
    labels = metadata.get("labels", {})
    if (node.get("apiVersion") != "v1" or node.get("kind") != "Node" or metadata.get("name") != name
            or any(labels.get(key) != value for key, value in pod["spec"]["nodeSelector"].items())
            or {"node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master"} & set(labels)):
        raise ValueError("runner must be on the approved build agent, never a control-plane node")
    dedicated = {"key": "railshot.io/dedicated", "value": "build", "effect": "NoSchedule"}
    taints = [item for item in node.get("spec", {}).get("taints", []) if item.get("key") == dedicated["key"]]
    if taints != [dedicated]:
        raise ValueError("build node requires the exact dedicated=build:NoSchedule taint")


def api_read(host, port, context, token, path):
    # Direct HTTPS does not inherit proxies or follow redirects with the bearer token.
    connection = HTTPSConnection(str(host), port, context=context, timeout=10)
    try:
        connection.request("GET", path, headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("Kubernetes API read failed with HTTP " + str(response.status))
        raw = response.read(1024 * 1024 + 1)
    finally:
        connection.close()
    if len(raw) > 1024 * 1024:
        raise ValueError("oversized Kubernetes API response")
    return json.loads(raw)


def read_own_pod():
    namespace, name, uid = (os.environ.get(key, "") for key in (
        "RAILSHOT_POD_NAMESPACE", "RAILSHOT_POD_NAME", "RAILSHOT_POD_UID"))
    if (namespace != NAMESPACE or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]", name)
            or not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", uid)):
        raise ValueError("explicit Downward API Pod namespace, name and UID are required")
    host = ipaddress.ip_address(os.environ.get("KUBERNETES_SERVICE_HOST", ""))
    port = int(os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443"))
    if not 1 <= port <= 65535:
        raise ValueError("invalid Kubernetes HTTPS port")
    token = (Path(KUBERNETES) / "token").read_text().strip()
    if not token or len(token) > 16384 or any(character.isspace() for character in token):
        raise ValueError("invalid projected Pod API token")
    pieces = token.split(".")
    if len(pieces) != 3:
        raise ValueError("bound Pod service account JWT required")
    claims = json.loads(base64.urlsafe_b64decode(pieces[1] + "=" * (-len(pieces[1]) % 4)))
    bound = claims.get("kubernetes.io", {})
    if (bound.get("namespace") != namespace or bound.get("pod", {}).get("name") != name
            or bound.get("pod", {}).get("uid") != uid
            or bound.get("serviceaccount", {}).get("name") != SERVICE_ACCOUNT):
        raise ValueError("projected token must be bound to this runner Pod and service account")
    # The API server verifies this JWT's signature, issuer, audience and lifetime.
    context = ssl.create_default_context(cafile=str(Path(KUBERNETES) / "ca.crt"))
    pod = api_read(host, port, context, token, f"/api/v1/namespaces/{namespace}/pods/{name}")
    validate_pod(pod, namespace, name, uid)
    node = api_read(host, port, context, token, "/api/v1/nodes/" + pod["spec"]["nodeName"])
    validate_node(node, pod)
    return pod


def check_image():
    for command in ("python3", "git", "curl", "jq", "docker", "iptables", "ip6tables", "nft", "flock", "sysctl"):
        if not shutil.which(command):
            raise ValueError("missing executable: " + command)
    subprocess.run(["docker", "buildx", "version"], check=True)
    subprocess.run(["/home/runner/bin/Runner.Listener", "--version"], check=True)


def check_host():
    if os.geteuid() != 0:
        raise ValueError("trusted runner must use root on the dedicated CI VM")
    for key in ("KUBECONFIG", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE", "GOOGLE_APPLICATION_CREDENTIALS",
                "AZURE_CLIENT_SECRET", "ARM_CLIENT_SECRET", "OS_PASSWORD"):
        if os.environ.get(key):
            raise ValueError("operational credentials must not enter the CI runner: " + key)
    if os.environ.get("DOCKER_HOST") != "unix:///var/run/docker.sock":
        raise ValueError("only this CI VM's Docker socket is supported")
    runner_name = os.environ.get("RAILSHOT_RUNNER_NAME", "")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", runner_name):
        raise ValueError("reviewed runner workspace identity required")
    if os.environ.get("TMPDIR") != ROOT + "/work/" + runner_name + "/_temp":
        raise ValueError("TMPDIR must be inside the workspace's same-path host mount")
    mode = host_mode()
    owned_file("/usr/local/sbin/railshot-ci-network", 0o755)
    owned_file("/etc/railshot/ci-executor.yaml", 0o644)
    owned_file("/var/lib/railshot-ci/network-verified.sha256", 0o600)
    owned_file(TOKEN, 0o600)
    for suffix in ("work", "runs", "codex"):
        path = Path(ROOT) / suffix
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("private root-owned directory required: " + str(path))
    if mode == "standalone":
        container = json.loads(subprocess.check_output(["docker", "inspect", "railshot-ci-runner"], text=True))[0]
        validate_container(container)
    else:
        read_own_pod()
    # Reuse the same gate implementation as jobs; no second firewall/builder contract.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gate"))
    from gate import require_ci_network
    require_ci_network("railshot-quality")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--check-image"]:
            check_image()
        elif sys.argv[1:] == ["--check-builder"]:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gate"))
            from gate import require_ci_builder
            require_ci_builder()
        elif not sys.argv[1:]:
            check_host()
        else:
            raise ValueError("unsupported preflight argument")
    except Exception as exc:
        print("CI runner preflight failed: " + str(exc), file=sys.stderr)
        sys.exit(1)
