"""Shared release order and application container execution contract."""
import json
from pathlib import Path

# One vocabulary for execution, evidence and repair tasks. No log-text routing.
STAGE_CONTRACT = json.loads((Path(__file__).parent / 'contract/stages.json').read_text())


def stage_contract(layer):
    stage = STAGE_CONTRACT['layers'].get(layer, layer)
    return {'id': stage, **STAGE_CONTRACT['stages'].get(stage, {
        'owner': 'operator', 'completion': 'inspect missing execution evidence',
        'replay': 'reconcile_remote', 'repair': 'operator'})}


GATE_ORDER = ("L0", "L1", "L2", "L3")
# Keep explicit quality diagnostics and previously published bundle receipts readable.
FULL_GATE_ORDER = ("L0", "L1", "Q", "L2", "L4", "L3")
RELEASE_ORDERS = (GATE_ORDER, ("L0", "L1", "L2", "L4", "L3"), FULL_GATE_ORDER)
APP_UID = 65532


def quality_advisory(row):
    """Only completed quality checks/discovery may be non-blocking, never execution boundaries."""
    error = row.get("error") or {}
    return (row.get("layer") == "Q" and row.get("advisory") is True
            and row.get("ok") is False and row.get("outcome") in {"FAIL", "BLOCKED"}
            and error.get("outcome") == row.get("outcome")
            and ((error.get("code") == "GATE_CONFIG_INVALID" and error.get("phase") == "Q.discovery")
                 or (error.get("code") in {"GATE_CHECK_FAILED", "GATE_ENVIRONMENT_UNAVAILABLE"}
                     and error.get("phase") in {"Q", "Q.discovery", "Q.prepare", "Q.lint", "Q.type", "Q.unit", "Q.report"})))


def pod_security(uid=APP_UID):
    return {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid, "fsGroup": uid,
            "seccompProfile": {"type": "RuntimeDefault"}}


def container_security():
    return {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": False,
            "capabilities": {"drop": ["ALL"]}}


def docker_security():
    return ["--user", f"{APP_UID}:{APP_UID}", "--tmpfs", "/tmp",
            "--cap-drop=ALL", "--security-opt=no-new-privileges"]


def docker_command(image, command=None):
    # Kubernetes command replaces ENTRYPOINT and clears image CMD. Match it exactly.
    return ["--entrypoint", command[0], image, *command[1:]] if command else [image]
