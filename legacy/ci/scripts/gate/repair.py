"""Native dependency preparation for operator-authorized source repair.

The model never writes locks. Resolution uses the existing credential-free,
read-only Docker source mount and filtered network; only bounded locks return.
"""
import base64
import hashlib
import json
from pathlib import Path
import shlex
import tempfile
import time
import uuid

from bundle import stage_source
from prepare import select_build_roots
from quality import NODE, docker_command, exact_version, javascript_manager
from process import run_bounded

LOCKS = ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dependency_fields(package):
    return {key: package.get(key) for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")}


def prepare_locks(workspace, run, *, network, selected_root=None, observer=None):
    """Runs before a gate snapshot; receipts are outside the uploaded workspace."""
    from gate import require_ci_network
    from runner.run_agent import source_change_allowed
    ws, run = Path(workspace).resolve(), Path(run).resolve()
    receipt_path = run / "native-locks.json"
    receipts = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
    roots = select_build_roots(ws, selected_root)
    if len(roots) != 1:
        roots = []  # Root selection remains an explicit operator decision.
    for root in roots:
        manifest = root / "package.json"
        if not manifest.is_file():
            continue  # Python/Java preserve their existing native-lock contracts.
        relative = manifest.relative_to(ws).as_posix()
        package = json.loads(manifest.read_text())
        original = run_bounded(["git", "show", "HEAD:" + relative], cwd=ws, timeout=10)
        if original.returncode:
            continue
        source_change_allowed(relative, manifest.read_text(), original.stdout)
        locks = [root / name for name in LOCKS if (root / name).is_file()]
        if len(locks) > 1 or package.get("workspaces") or (root / "pnpm-workspace.yaml").exists():
            continue
        key = locks[0].relative_to(ws).as_posix() if locks else None
        dependency_sha = hashlib.sha256(json.dumps(dependency_fields(package), sort_keys=True).encode()).hexdigest()
        prior = receipts.get(key, {})
        if locks and prior.get("sha256") == sha(locks[0]) and prior.get("dependencies_sha256") == dependency_sha:
            prior["manifest_sha256"] = sha(manifest)
            continue
        mismatch = False
        if locks and locks[0].name.endswith(".json"):
            try:
                locked = json.loads(locks[0].read_text()).get("packages", {}).get("", {})
                mismatch = dependency_fields(locked) != dependency_fields(package)
            except (ValueError, AttributeError):
                mismatch = True
        if locks and not prior and not mismatch and dependency_fields(package) == dependency_fields(json.loads(original.stdout)):
            continue
        require_ci_network(network)
        name = "railshot-lock-" + uuid.uuid4().hex[:16]
        with tempfile.TemporaryDirectory(prefix="railshot-lock-") as temp:
            staged = stage_source(ws, Path(temp) / "source")
            build = staged / root.relative_to(ws)
            if not locks:
                manager = package.get("packageManager", "npm").split("@")[0]
                lock_name = {"npm": "package-lock.json", "pnpm": "pnpm-lock.yaml", "yarn": "yarn.lock"}.get(manager)
                if lock_name is None:
                    continue
                (build / lock_name).write_text("# yarn lockfile v1\n" if package.get("packageManager", "").startswith("yarn@1.") else "")
            else:
                lock_name = locks[0].name
            manager, pin, setup, *_ = javascript_manager(build, package)
            install = ("npm install --package-lock-only --ignore-scripts --no-audit --no-fund" if manager == "npm" else
                       "pnpm install --lockfile-only --ignore-scripts" if manager == "pnpm" else
                       "yarn install --ignore-scripts --non-interactive" if pin.startswith("yarn@1.") else
                       "YARN_ENABLE_SCRIPTS=false yarn install --mode=skip-build")
            if not locks:
                setup += ["rm -- " + shlex.quote(lock_name)]  # Only the empty staged marker, never uploaded files.
            emit = "node -e " + shlex.quote("const f=require('fs');const p=" + json.dumps(lock_name) +
                    ";const b=f.readFileSync(p);if(b.length>4194304)throw Error('lock too large');console.log('RAILSHOT_NATIVE_LOCK='+b.toString('base64'))")
            node = exact_version(root / ".node-version", exact_version(root / ".nvmrc", NODE))
            if node.split(".")[0] not in {"22", "24"}:
                raise ValueError("unsupported native lock Node profile")
            plan = {"path": root.relative_to(ws).as_posix(), "image": "node:" + node + "-bookworm-slim",
                    "commands": setup + [install, emit]}
            try:
                started = time.monotonic()
                result = None
                try:
                    result = run_bounded(docker_command(staged, plan, name, network), timeout=600)
                finally:
                    if observer is not None:
                        observer(["native.dependencies"], result, started, None if result is not None else "ProcessUnavailable")
                if result.returncode:
                    raise ValueError("native lock resolution failed; dependency/network configuration needs repair")
                rows = [line.partition("=")[2] for line in result.stdout.splitlines() if line.startswith("RAILSHOT_NATIVE_LOCK=")]
                if len(rows) != 1:
                    raise ValueError("native lock evidence is missing or ambiguous")
                content = base64.b64decode(rows[0], validate=True)
                if not content or len(content) > 4 * 1024 * 1024 or b"\0" in content:
                    raise ValueError("invalid native lock output")
                content.decode("utf-8")
                if lock_name.endswith(".json"):
                    parsed = json.loads(content)
                    if parsed.get("lockfileVersion") not in {2, 3} or not isinstance(parsed.get("packages"), dict):
                        raise ValueError("invalid npm native lock")
            finally:
                cleanup = run_bounded(["docker", "rm", "-f", name], timeout=30)
                if cleanup.returncode:
                    raise ValueError("native lock container cleanup failed")
        target = root / lock_name
        if target.is_symlink():
            raise ValueError("native lock target is a symlink")
        target.write_bytes(content)
        target.chmod(0o644)
        receipts[target.relative_to(ws).as_posix()] = {"sha256": sha(target), "manifest": relative,
            "manifest_sha256": sha(manifest), "package_manager": manager, "package_manager_pin": pin,
            "dependencies_sha256": dependency_sha, "source": "native-isolated-resolution"}
    run.mkdir(parents=True, exist_ok=True)
    temporary = receipt_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(receipts, sort_keys=True))
    temporary.chmod(0o600)
    temporary.replace(receipt_path)
    return receipts


def verified_lock(ws, path, receipts):
    row = receipts.get(path, {})
    manifest = Path(path).parent / "package.json"
    return (Path(path).name in LOCKS and row.get("source") == "native-isolated-resolution" and
            row.get("manifest") == manifest.as_posix() and row.get("sha256") == sha(ws / path) and
            row.get("manifest_sha256") == sha(ws / manifest))
