#!/usr/bin/env python3
"""Deterministic release gate: L0 patch, L1 spec, L2 build, L4 scan, L3 runtime.

usage:
  gate.py WORKSPACE RUN [--layers L0,L1,L2,L4,L3]
  gate.py --self-test

Writes RUN/verdict.json and, on failure, RUN/failure.txt (input for the fixer).
The workspace is a git repo whose HEAD is the imported source; agent changes are uncommitted.
L2–L4 need Docker; without it they report "blocked" and the verdict is not ok.
"""
import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

import yaml

PLATFORM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM))
sys.path.insert(0, str(PLATFORM / "runner"))
from observability import OperationError, event_record  # noqa: E402
from run_agent import path_ok, writable_rules, source_change_allowed, deletion_allowed  # noqa: E402
from quality import run_quality  # noqa: E402
from bundle import source_digest, source_spec, stage_source  # noqa: E402
from process import run_bounded  # noqa: E402
from storage import durable_write
from runner.capacity import host_capacity
from execution import APP_UID, GATE_ORDER, FULL_GATE_ORDER, RELEASE_ORDERS, docker_security, docker_command, quality_advisory, stage_contract  # noqa: E402
from progress import Progress  # noqa: E402
from diagnostics import Diagnostics, fingerprint, redact  # noqa: E402

DIAGNOSTICS = None
PROGRESS = None

ORDER = GATE_ORDER

SECRET = re.compile(r"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|sk-ant-[A-Za-z0-9_-]{20,}|xox[bpas]-[0-9A-Za-z-]{10,}"
                    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?i:password\s*[=:]\s*\S+)")
TRIVY = "aquasec/trivy:0.74.0"
PG = "postgres:16"
CI_NETWORK = "railshot-quality"
CI_NETWORK_HELPER = "/usr/local/sbin/railshot-ci-network"
CI_PROFILE_PATH = PLATFORM / "contract/ci-executor.yaml"
MAX_IMAGE_BYTES = 2 * 1024 ** 3          # ponytail: uncompressed proxy for the 1 GiB compressed rule
CLASS_RULES = [                          # (layer, regex, class) — first match wins
    ("L2", r"No matching distribution|ERESOLVE|npm ERR!|ModuleNotFoundError|Could not find a version|Unable to locate package|pip.*error", "F1"),
    ("L2", r"exec format error|no match for platform", "F3"),
    ("L2", r"TLS handshake timeout|i/o timeout|429 Too Many Requests|connection reset by peer|temporary failure in name resolution", "F8"),
    ("L2", r"COPY failed|failed to compute cache key|not found|no such file", "F2"),
    ("L3", r"ModuleNotFoundError|ImportError|Cannot find module", "F1"),
    ("L3", r"migrat", "F9"),
    ("L4", r"CRITICAL", "F6"),
]
DEFAULT_CLASS = {"L0": "F5", "L1": "F5", "Q": "QUALITY", "L2": "F2", "L3": "F4", "L4": "F5"}


def sh(cmd, cwd=None, timeout=900, check=False, raw=False):
    started, result, error = time.monotonic(), None, None
    try:
        result = run_bounded(cmd, cwd=cwd, timeout=timeout, check=check, raw=raw,
                             on_tick=PROGRESS.tick if PROGRESS is not None else None)
        return result
    except Exception as exc:
        error = type(exc).__name__
        raise
    finally:
        if DIAGNOSTICS is not None:
            try:
                DIAGNOSTICS.process(cmd, result, started, error)
            except (OSError, ValueError, TypeError):
                DIAGNOSTICS.missing.append('process_capture_unavailable')


def docker_ok():
    try:
        return sh(["docker", "version", "--format", "{{.Server.Version}}"], timeout=10).stdout.strip() != ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


# ---------- L0: patch policy ----------

def changed_files(ws):
    # Compare to imported HEAD, not the mutable index. Disable rename detection so
    # a rename is always the forbidden deletion plus its added destination.
    raw = sh(["git", "diff", "HEAD", "--name-status", "-z", "--no-renames", "--no-ext-diff", "--no-textconv", "--"],
             cwd=ws, check=True, raw=True).stdout
    fields = [os.fsdecode(value) for value in raw.split(b"\0")[:-1]]
    if len(fields) % 2:
        raise ValueError("malformed NUL-delimited git diff")
    changes = {path: "D" if code == "D" else "A" if code == "A" else "M"
               for code, path in zip(fields[::2], fields[1::2])}
    # No --exclude-standard: new ignored files remain part of the patch policy.
    others = sh(["git", "ls-files", "--others", "-z", "--"], cwd=ws, check=True, raw=True).stdout
    for value in others.split(b"\0"):
        path = os.fsdecode(value)
        if path:
            changes.setdefault(path, "A")
    return [(kind, path) for path, kind in sorted(changes.items())]


def added_lines(ws, changes):
    if not changes:
        return []
    diff = sh(["git", "diff", "HEAD", "-U0", "--no-color", "--no-renames", "--text", "--no-textconv", "--no-ext-diff", "--", *[path for _, path in changes]],
              cwd=ws, check=True).stdout
    lines, in_hunk = [], False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@ "):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            lines.append(line[1:])
    untracked = {os.fsdecode(value) for value in sh(["git", "ls-files", "--others", "-z", "--"],
                                                 cwd=ws, check=True, raw=True).stdout.split(b"\0")}
    for kind, path in changes:
        if kind == "A" and path in untracked:
            lines += (ws / path).read_text(errors="replace").splitlines()
    return lines


def l0(ws, paths, *, repair_scope="packaging", native_locks=None):
    from repair import verified_lock
    changes = changed_files(ws)
    allow, protected = writable_rules("contract/paths.yaml", scope=repair_scope)
    errors, native, deleted_bytes = [], set(), 0
    for kind, path in changes:
        if repair_scope == "source" and kind != "D" and not (ws / path).is_symlink() and verified_lock(ws, path, native_locks or {}):
            native.add(path)
            continue
        if kind == "D":
            try:
                original = sh(["git", "show", "HEAD:" + path], cwd=ws, check=True, raw=True).stdout
                mode = sh(["git", "ls-tree", "HEAD", "--", path], cwd=ws, check=True).stdout.split()[0]
                if mode not in {"100644", "100755"}:
                    raise ValueError("cannot delete a non-regular file: " + path)
                deletion_allowed(path, original, allow, protected, repair_scope=repair_scope)
                deleted_bytes += len(original)
            except (ValueError, subprocess.CalledProcessError) as exc:
                errors.append(str(exc))
        elif not path_ok(path, allow, protected):
            errors.append(f"path not writable: {path}")
        elif (ws / path).is_symlink():
            errors.append(f"symlink: {path}")
        elif b"\0" in (ws / path).read_bytes()[:8000]:
            errors.append(f"binary file: {path}")
        elif repair_scope == "source":
            original = sh(["git", "show", "HEAD:" + path], cwd=ws)
            try:
                source_change_allowed(path, (ws / path).read_text(), original.stdout if original.returncode == 0 else None)
            except ValueError as exc:
                errors.append(str(exc))
    lim = paths["limits"]
    if len(changes) - len(native) > lim["max_files_changed"]:
        errors.append(f"too many files changed: {len(changes)} > {lim['max_files_changed']}")
    lines = added_lines(ws, [c for c in changes if c[0] != "D" and c[1] not in native])
    if deleted_bytes + sum(len(l) + 1 for l in lines) > lim["max_patch_bytes"]:
        errors.append(f"patch too large: > {lim['max_patch_bytes']} bytes")
    for pat in paths["forbidden_patterns"]:
        hit = next((l for l in lines if re.search(pat, l)), None)
        if hit is not None:
            errors.append(f"forbidden pattern {pat!r}: {hit.strip()[:120]}")
    return errors, [p for _, p in changes]


# ---------- L1: static ----------

def parse_dockerfile(text):
    """Return stages: [{'from': image, 'alias': str|None, 'lines': [instr...]}]; handles line continuations."""
    joined = re.sub(r"\\\n", " ", text)
    stages = []
    for raw in joined.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"(?i)FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", line)
        if m:
            stages.append({"from": m.group(1), "alias": m.group(2), "lines": []})
        elif stages:
            stages[-1]["lines"].append(line)
    return stages


def split_ref(ref):
    ref = ref.split("@")[0]
    return tuple(ref.rsplit(":", 1)) if ":" in ref.split("/")[-1] else (ref, "")


def base_allowed(image, allow):
    repo, tag = split_ref(image)
    return any(repo == arepo and (atag == "" or tag.startswith(atag)) for arepo, atag in map(split_ref, allow))


def check_dockerfile(ws, svc, allow):
    rel = svc["build"]["dockerfile"]
    for raw in (svc["build"].get("context", "."), rel):
        if Path(raw).is_absolute() or ".." in Path(raw).parts or "\\" in raw:
            return [f"{svc['name']}: build context/Dockerfile must be relative without traversal"]
    ctx = ws / svc["build"].get("context", ".")
    path = ctx / rel if not (ws / rel).exists() else ws / rel
    for candidate in (ctx, path):
        resolved = candidate.resolve()
        if resolved != ws.resolve() and ws.resolve() not in resolved.parents:
            return [f"{svc['name']}: build context/Dockerfile escapes source workspace"]
    if not path.exists():
        return [f"{svc['name']}: Dockerfile not found: {rel}"]
    stages = parse_dockerfile(path.read_text())
    if not stages:
        return [f"{svc['name']}: no FROM"]
    errs, aliases = [], set()
    for index, st in enumerate(stages):
        if st["from"] not in aliases and not base_allowed(st["from"], allow):
            errs.append(f"{svc['name']}: base image not allowed: {st['from']}")
        for line in st["lines"]:
            # COPY/ADD --from and BuildKit RUN --mount can name external images.
            refs = re.findall(r"(?i)--from=(\S+)|--mount=\S*?from=([^,\s]+)", line)
            for pair in refs:
                ref = next(value for value in pair if value).strip("\"'")
                if ref not in aliases and not (ref.isdigit() and int(ref) < index) and not base_allowed(ref, allow):
                    errs.append(f"{svc['name']}: external build image not allowed: {ref}")
        if st["alias"]:
            aliases.add(st["alias"])
    final = stages[-1]["lines"]
    users = [l.split(None, 1)[1] for l in final if l.upper().startswith("USER ")]
    if not users:
        errs.append(f"{svc['name']}: final stage has no USER (C3)")
    else:
        uid = users[-1].split(":")[0]
        if not uid.isdigit() or int(uid) < 10000:
            errs.append(f"{svc['name']}: final USER must be numeric >= 10000, got {users[-1]} (C3)")
    if any(re.match(r"(?i)(COPY|ADD)\s.*\.env\b", l) for st in stages for l in st["lines"]):
        errs.append(f"{svc['name']}: copies a .env file (C6)")
    if not svc.get("command"):
        cmds = [l for l in final if re.match(r"(?i)(CMD|ENTRYPOINT)\s", l)]
        if not cmds:
            errs.append(f"{svc['name']}: no CMD/ENTRYPOINT and no spec command")
        elif not re.match(r"(?i)(CMD|ENTRYPOINT)\s+\[", cmds[-1]):
            errs.append(f"{svc['name']}: use exec-form CMD [...] (C4)")
    return errs


def dockerignore_errors(path):
    if not path.exists():
        return ["missing .dockerignore (C6)"]
    patterns = {line.strip().strip("/") for line in path.read_text().splitlines()
                if line.strip() and not line.lstrip().startswith(("#", "!"))}
    # This is a small configuration check, not a Dockerignore interpreter.
    # L2 physically excludes .git and .env* at every depth from the build input.
    required = {".git": {".git", "**/.git"}, ".env": {".env", ".env*", "**/.env*"}}
    return [f".dockerignore must exclude {name} (C6)" for name, accepted in required.items()
            if not patterns.intersection(accepted)]


def validate_semantics(spec):
    """Validate CI workload inputs without generating deployment resources."""
    import jsonschema
    jsonschema.validate(spec, json.loads((PLATFORM / "schemas/railshot.schema.json").read_text()))
    names = [svc["name"] for svc in spec["services"]]
    if len(names) != len(set(names)):
        raise ValueError("service names must be unique")
    for svc in spec["services"]:
        if svc.get("storage") and (len(spec["services"]) != 1 or svc.get("replicas", 1) != 1 or svc.get("migrate")):
            raise ValueError("persistent storage requires one service, one replica and no migration Job")
        if set(svc.get("env", {})) & {"PORT", "DATABASE_URL", "MIGRATION_DATABASE_URL"} or set(svc.get("secrets", [])) & {"PORT", "MIGRATION_DATABASE_URL"}:
            raise ValueError("PORT and database role bindings are platform-owned; use DATABASE_URL secret for an external DB")


def l1(ws, *, app_id=None):
    import jsonschema
    try:
        spec_path = source_spec(ws)
    except ValueError as exc:
        return [str(exc)], None
    try:
        spec = yaml.safe_load(spec_path.read_text())
        jsonschema.validate(spec, json.loads((PLATFORM / "schemas/railshot.schema.json").read_text()))
    except (yaml.YAMLError, jsonschema.ValidationError) as exc:
        raise OperationError("GATE_CHECK_FAILED", component="gate", phase="L1", outcome="FAIL", cause=exc) from exc
    allow = yaml.safe_load((PLATFORM / "contract/catalog.yaml").read_text())["base_images"]
    errs = [e for s in spec["services"] for e in check_dockerfile(ws, s, allow)]
    if app_id is not None and spec["app"] != app_id:
        errs.insert(0, f"APP/spec.app mismatch: trusted app identity is {app_id}; set spec.app to this exact value")
    errs.extend(dockerignore_errors(ws / ".dockerignore"))
    try:
        validate_semantics(spec)
    except ValueError as exc:
        raise OperationError("GATE_CHECK_FAILED", component="gate", phase="L1", outcome="FAIL", cause=exc) from exc
    return errs, spec


# ---------- L2–L4: docker ----------

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_status(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return None


def ci_profile():
    """Versioned trusted JSON/YAML contract; never selected or overridden by an upload."""
    try:
        raw = CI_PROFILE_PATH.read_bytes()
        profile = json.loads(raw)
        builder = profile["builder"]
        if (profile["schema_version"] != 1 or builder["network"] != CI_NETWORK
                or not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", builder["name"])
                or builder["driver"] != "remote"
                or not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", builder["container"])
                or not re.fullmatch(r"moby/buildkit:v[0-9]+\.[0-9]+\.[0-9]+", builder["image"])
                or builder["flags"] != ["--oci-worker-net=bridge"]
                or any(type(builder[key]) is not int or builder[key] <= 0 for key in ("memory_bytes", "cpu_period", "cpu_quota"))):
            raise ValueError("invalid trusted executor profile")
        return builder, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OperationError("GATE_CONFIG_INVALID", component="gate", phase="executor-profile",
                             retry_policy="after_configuration", cause=exc) from exc


def require_ci_network(network):
    """Only the locally verified, credential-free Linux worker profile is supported."""
    if network != CI_NETWORK or not Path(CI_NETWORK_HELPER).is_file():
        raise OperationError("GATE_ENVIRONMENT_UNAVAILABLE", component="gate", phase="network", retry_policy="after_configuration")
    _, profile_hash = ci_profile()
    command = [CI_NETWORK_HELPER, "--check", profile_hash]
    if os.geteuid() != 0:
        command = ["sudo", "-n", *command]
    try:
        sh(command, timeout=20, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        # This read-only probe cannot start a build/container; timeout here is
        # unavailable preflight evidence, not an uncertain app execution.
        raise OperationError("GATE_ENVIRONMENT_UNAVAILABLE", component="gate", phase="network",
                             retry_policy="after_configuration", cause=exc) from exc


def require_ci_builder():
    try:
        return _check_ci_builder()
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise OperationError("GATE_ENVIRONMENT_UNAVAILABLE", component="gate", phase="builder",
                             retry_policy="after_configuration", cause=exc) from exc


def _check_ci_builder():
    expected, _ = ci_profile()
    # Buildx 0.30.x supports formatted ls, but not formatted inspect. Some
    # versions emit an identical row twice; conflicting rows still fail closed.
    rows = [json.loads(line) for line in sh(["docker", "buildx", "ls", "--format", "{{json .}}"],
                                          timeout=20, check=True).stdout.splitlines() if line.strip()]
    matches = {json.dumps(row, sort_keys=True): row for row in rows if row.get("Name") == expected["name"]}
    if len(matches) != 1:
        raise ValueError("CI_BUILDER_UNVERIFIED")
    builder = next(iter(matches.values()))
    if (builder.get("Driver") != expected["driver"] or len(builder.get("Nodes", [])) != 1
            or builder["Nodes"][0].get("Endpoint") != "docker-container://" + expected["container"]):
        raise ValueError("CI_BUILDER_UNVERIFIED")
    container = json.loads(sh(["docker", "inspect", expected["container"]],
                              timeout=20, check=True).stdout)[0]
    if (set(container["NetworkSettings"]["Networks"]) != {CI_NETWORK}
            or container["HostConfig"]["NetworkMode"] != CI_NETWORK
            or not container["State"]["Running"]
            or container["Config"]["Image"] != expected["image"]
            or container["Config"].get("Cmd") != expected["flags"]
            or container["Config"].get("Entrypoint") != ["buildkitd"]
            or container["Config"].get("Labels", {}).get("railshot.component") != "ci-builder"
            or container["HostConfig"].get("Memory") != expected["memory_bytes"]
            or container["HostConfig"].get("MemorySwap") != expected["memory_bytes"]
            or container["HostConfig"].get("CpuQuota") != expected["cpu_quota"]
            or container["HostConfig"].get("CpuPeriod") != expected["cpu_period"]):
        raise ValueError("CI_BUILDER_UNVERIFIED")


def l2(ws, spec, run_id, *, network=None):
    require_ci_network(network)
    require_ci_builder()
    builder, _ = ci_profile()
    images, errs = {}, []
    for s in spec["services"]:
        tag = f"railshot-gate/{spec['app']}-{s['name']}:{run_id}"
        ctx = ws / s["build"].get("context", ".")
        df = ws / s["build"]["dockerfile"] if (ws / s["build"]["dockerfile"]).exists() else ctx / s["build"]["dockerfile"]
        # BuildKit does not accept an arbitrary Docker network in --network. Its
        # managed builder container is attached to the filtered bridge instead.
        # The digest excludes Git metadata. Exclude it physically from every
        # context, including nested contexts and Dockerfile-specific ignore files.
        with tempfile.TemporaryDirectory(prefix="railshot-build-") as staging:
            clean = stage_source(ws, Path(staging) / "source")
            # Preserve L2's physical secret exclusion regardless of Dockerignore
            # negations. Filter only the verified private copy, never source.
            for path in sorted(clean.rglob(".env*"), key=lambda p: len(p.parts), reverse=True):
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            p = sh(["docker", "buildx", "build", "--builder", builder["name"], "--network", "default",
                    "--platform", "linux/amd64", "--load", "-f", str(clean / df.relative_to(ws)),
                    "-t", tag, str(clean / ctx.relative_to(ws))], timeout=1800)
        if p.returncode:
            errs.append(f"{s['name']}: build failed\n{p.stderr[-6000:]}")
        else:
            images[s["name"]] = tag
    return errs, images


def l3(spec, images, run_id, *, network=None):
    require_ci_network(network)
    if not re.fullmatch(r"[0-9a-f]{16}", run_id):
        raise ValueError("invalid runtime network identity")
    if spec.get("egress"):
        raise OperationError("GATE_ENVIRONMENT_UNAVAILABLE", component="gate", phase="runtime-egress",
                             retry_policy="after_configuration")
    errs, net = [], f"railshot-gate-{run_id}"
    bridge = "rsrun-" + run_id[:8]
    # --internal blocks external/inter-network traffic, but alone still permits
    # host gateway access. The verified worker also blocks INPUT from rsrun-*.
    sh(["docker", "network", "create", "--internal", "--driver", "bridge",
        "--opt", f"com.docker.network.bridge.name={bridge}", net], check=True)
    started = []
    try:
        info = json.loads(sh(["docker", "network", "inspect", net], timeout=15, check=True).stdout)[0]
        if (info.get("Driver") != "bridge" or info.get("Internal") is not True or info.get("EnableIPv6") is not False
                or info.get("Options", {}).get("com.docker.network.bridge.name") != bridge):
            raise OperationError("GATE_ENVIRONMENT_UNAVAILABLE", component="gate", phase="runtime-network",
                                 retry_policy="after_configuration", side_effect="possible")
        db_url, migration_url = None, None
        if "postgres" in spec.get("resources", {}):
            pg = f"{net}-pg"
            sh(["docker", "run", "-d", "--name", pg, "--network", net, "--cpus=1", "--memory=1g", "--pids-limit=128", "-e", "POSTGRES_PASSWORD=gate",
                "-e", "POSTGRES_DB=app", PG], check=True)
            started.append(pg)
            for _ in range(60):
                if sh(["docker", "exec", pg, "pg_isready", "-U", "postgres"]).returncode == 0:
                    break
                time.sleep(1)
            sh(["docker", "exec", pg, "psql", "-U", "postgres", "-d", "app", "-v", "ON_ERROR_STOP=1", "-c", postgres_role_sql()], check=True)
            db_url = f"postgresql://app_rw:gate-runtime@{pg}:5432/app"
            migration_url = f"postgresql://app_owner:gate-owner@{pg}:5432/app"
        for s in spec["services"]:
            if s["name"] not in images:
                continue
            env = ["-e", f"PORT={s['port']}"] + [x for k, v in s.get("env", {}).items() for x in ("-e", f"{k}={v}")]
            if db_url:
                env += ["-e", f"DATABASE_URL={db_url}"]
            if s.get("migrate"):
                migration_name = f"{net}-{s['name']}-migrate"
                started.append(migration_name)
                p = sh(["docker", "run", "--name", migration_name, "--network", net, "--cpus=1", "--memory=1g", "--pids-limit=128",
                        *docker_security(), *env,
                        "-e", f"MIGRATION_DATABASE_URL={migration_url}", "-e", f"DATABASE_URL={migration_url}",
                        *docker_command(images[s["name"]], s["migrate"]["command"])], timeout=300)
                if p.returncode:
                    errs.append(f"{s['name']}: migration failed\n{(p.stdout + p.stderr)[-6000:]}")
                    continue
                if db_url:
                    sh(["docker", "exec", pg, "psql", "-U", "postgres", "-d", "app", "-v", "ON_ERROR_STOP=1", "-c",
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_rw; GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_rw;"], check=True)
            name = f"{net}-{s['name']}"
            cmd = ["docker", "run", "-d", "--name", name, "--network", net, *docker_security(),
                   "--cpus=1", "--memory=1g", "--pids-limit=128", *env]
            if s.get("storage"):
                # CI storage is temporary; durable PVC verification belongs to CD.
                cmd += ["--tmpfs", f"{s['storage']['mountPath']}:rw,uid=65532,gid=65532,mode=0700,size=1g"]
            started.append(name)
            p = sh(cmd + docker_command(images[s["name"]], s.get("command")))
            if p.returncode:
                errs.append(f"{s['name']}: container did not start\n{p.stderr[-3000:]}")
                continue
            attachments = json.loads(sh(["docker", "inspect", "--format", "{{json .NetworkSettings.Networks}}", name],
                                        timeout=15, check=True).stdout)
            if set(attachments) != {net}:
                raise ValueError("runtime container network identity changed")
            raw_address = attachments[net].get("IPAddress", "")
            if not raw_address:
                logs = sh(["docker", "logs", "--tail", "200", name])
                state = sh(["docker", "inspect", "-f", "{{json .State}}", name])
                errs.append(f"{s['name']}: container has no runtime address; startup failed\n"
                            f"{state.stdout[-2000:]}\n{(logs.stdout + logs.stderr)[-6000:]}")
                continue
            address = ipaddress.IPv4Address(raw_address)
            if not address.is_private or address.is_loopback or address.is_link_local:
                raise ValueError("runtime container requires a private bridge address")
            # Internal Docker 29 bridges do not provide published host ports.
            # The trusted host observes the assigned address without publishing.
            origin = f"http://{address}:{s['port']}"
            health, ok = s.get("health", "/"), False
            for _ in range(60):
                code = http_status(f"{origin}{health}")
                if code and code < 400:
                    ok = True
                    break
                if sh(["docker", "inspect", "-f", "{{.State.Running}}", name]).stdout.strip() != "true":
                    break
                time.sleep(1)
            if not ok:
                logs = sh(["docker", "logs", "--tail", "200", name])
                errs.append(f"{s['name']}: health {health} not 2xx/3xx within 60s (last status {http_status(f'{origin}{health}')})\n"
                            f"{(logs.stdout + logs.stderr)[-6000:]}")
                continue
            route = s.get("route")
            if route and route != health:
                code = http_status(f"{origin}{route}")
                if code is None or code >= 500:
                    errs.append(f"{s['name']}: route {route} answered {code} (C11)")
    finally:
        cleanup_failed = False
        for c in started:
            try:
                cleanup_failed |= sh(["docker", "rm", "-f", c], timeout=15).returncode != 0
            except (OSError, subprocess.TimeoutExpired):
                cleanup_failed = True
        try:
            cleanup_failed |= sh(["docker", "network", "rm", net], timeout=15).returncode != 0
        except (OSError, subprocess.TimeoutExpired):
            cleanup_failed = True
        if cleanup_failed:
            raise OperationError("GATE_EXECUTION_FAILED", component="gate", phase="runtime-cleanup", outcome="UNKNOWN",
                                 retry_policy="after_reconcile", side_effect="unknown")
    return errs


def postgres_role_sql():
    """Ephemeral fixture credentials; runtime cannot use the migration owner role."""
    return """CREATE ROLE app_owner LOGIN PASSWORD 'gate-owner';
CREATE ROLE app_rw LOGIN PASSWORD 'gate-runtime';
ALTER DATABASE app OWNER TO app_owner;
ALTER SCHEMA public OWNER TO app_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE app TO app_rw;
GRANT USAGE ON SCHEMA public TO app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE app_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO app_rw;"""


def archive_config_id(archive, image_id):
    """Bind Trivy's config digest to Docker's classic ID or containerd manifest ID."""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("invalid image identity")
    with tarfile.open(archive, "r:") as tar:
        def read(name):
            matches = [member for member in tar.getmembers() if member.name == name]
            if len(matches) != 1 or not matches[0].isfile() or matches[0].size > 2 * 1024 * 1024:
                raise ValueError("invalid archive identity entry")
            return tar.extractfile(matches[0]).read()
        def blob(digest):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("invalid archive digest")
            data = read("blobs/sha256/" + digest.split(":", 1)[1])
            if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("archive content digest differs")
            return data
        # Docker 29's containerd store exposes a manifest digest as .Id.
        # Verify that immutable manifest, then its referenced config bytes.
        native_name = "blobs/sha256/" + image_id.split(":", 1)[1]
        if native_name in tar.getnames():
            native = json.loads(blob(image_id))
            if native.get("schemaVersion") == 2 and isinstance(native.get("config"), dict):
                config_id = native["config"]["digest"]
                config = json.loads(blob(config_id))
                if config.get("os") != "linux" or config.get("architecture") != "amd64":
                    raise ValueError("unsupported archive platform")
                return config_id
        # Classic Docker stores expose the content hash of config JSON directly.
        manifests = json.loads(read("manifest.json"))
        if not isinstance(manifests, list) or len(manifests) != 1:
            raise ValueError("expected one archived image")
        config = read(manifests[0]["Config"])
        if "sha256:" + hashlib.sha256(config).hexdigest() != image_id:
            raise ValueError("archive config differs from Docker identity")
        return image_id


def l4(images, *, network=None):
    require_ci_network(network)
    errs = []
    for svc, image_id in images.items():
        size = int(sh(["docker", "image", "inspect", "-f", "{{.Size}}", image_id], check=True).stdout.strip())
        if size > MAX_IMAGE_BYTES:
            errs.append(f"{svc}: image too large ({size // 2**20} MiB)")
            continue
        # The scanner reads one immutable archive, never the privileged Docker socket.
        with tempfile.TemporaryDirectory(prefix="railshot-scan-") as temp:
            archive = Path(temp) / "image.tar"
            sh(["docker", "image", "save", "--output", str(archive), image_id], check=True)
            try:
                config_id = archive_config_id(archive, image_id)
            except (ValueError, KeyError, TypeError, tarfile.TarError) as exc:
                raise OperationError("GATE_EVIDENCE_MISMATCH", component="gate", phase="scanner-archive",
                                     retry_policy="after_reconcile", cause=exc) from exc
            archive.chmod(0o644)  # parent remains private; scanner UID can read its bind mount
            # A Trivy DB is larger than a small application's memory budget.
            # Keep per-run DB and extraction files on private temporary disk,
            # not the application's tmpfs; delete them after container cleanup.
            cache = Path(temp) / "cache"
            cache.mkdir(mode=0o700)
            os.chown(cache, APP_UID, APP_UID)
            name = "railshot-scan-" + uuid.uuid4().hex[:16]
            try:
                p = sh(["docker", "run", "--name", name, "--network", network,
                        *docker_security(), "--read-only", "--cpus=1", "--memory=2g", "--memory-swap=2g", "--pids-limit=128",
                        "--mount", f"type=bind,src={archive},dst=/scan/image.tar,readonly",
                        "--mount", f"type=bind,src={cache},dst=/cache",
                        "-e", "TMPDIR=/cache", "-e", "GOMAXPROCS=1",
                        TRIVY, "image", "--input", "/scan/image.tar", "--cache-dir", "/cache/db", "--parallel", "1",
                        "--scanners", "vuln,secret", "--severity", "CRITICAL", "--ignore-unfixed",
                        "--exit-code", "23", "--quiet", "--format", "json"], timeout=1800)
                if p.returncode not in (0, 23):
                    raise OperationError("GATE_ENVIRONMENT_UNAVAILABLE", component="gate", phase="scanner",
                                         retry_policy="after_configuration",
                                         cause=subprocess.CalledProcessError(p.returncode, "trivy"))
                report = json.loads(p.stdout)
                if (report.get("SchemaVersion") != 2 or report.get("ArtifactType") != "container_image"
                        or report.get("Metadata", {}).get("ImageID") != config_id
                        or not isinstance(report.get("Results", []), list)):
                    raise OperationError("GATE_EVIDENCE_MISMATCH", component="gate", phase="scanner",
                                         retry_policy="after_reconcile")
                findings = [v for row in report.get("Results", []) for key in ("Vulnerabilities", "Secrets")
                            for v in (row.get(key) or []) if v.get("Severity") == "CRITICAL"]
                if bool(findings) != (p.returncode == 23):
                    raise OperationError("GATE_EVIDENCE_MISMATCH", component="gate", phase="scanner",
                                         retry_policy="after_reconcile")
                if findings:
                    # Never expose Trivy secret Match/raw source values in fixer input.
                    errs.append(f"{svc}: trivy CRITICAL findings: {len(findings)}")
            finally:
                cleanup = sh(["docker", "rm", "-f", name], timeout=15)
                if cleanup.returncode:
                    raise OperationError("GATE_EXECUTION_FAILED", component="gate", phase="scanner-cleanup",
                                         outcome="UNKNOWN", retry_policy="after_reconcile", side_effect="unknown")
    return errs


# ---------- verdict ----------

def classify(layer, text):
    if re.search(r"TLS handshake timeout|i/o timeout|429 Too Many Requests|connection reset by peer|temporary failure in name resolution|connection refused|cannot connect to.*docker|failed to download.*database|EAI_AGAIN|ENOTFOUND|ETIMEDOUT", text, re.I):
        return "F8"
    for lay, pat, cls in CLASS_RULES:
        if lay == layer and re.search(pat, text, re.I):
            return cls
    return DEFAULT_CLASS[layer]


def signature(layer, cls, text):
    return fingerprint(layer, cls, text)


def excerpt(text, limit=4000):
    return redact(text)[:limit]


def validate_layers(layers):
    if not layers or any(layer not in FULL_GATE_ORDER for layer in layers) or len(set(layers)) != len(layers):
        return "INVALID_LAYERS: require nonempty unique known layers"
    if list(layers) != sorted(layers, key=FULL_GATE_ORDER.index):
        return "INVALID_LAYERS: required order is " + ",".join(ORDER)
    if any(layer in layers for layer in ("L2", "L4", "L3")) and "L1" not in layers:
        return "INVALID_LAYERS: Docker stages require L1 spec validation"
    if any(layer in layers for layer in ("L4", "L3")) and "L2" not in layers:
        return "INVALID_LAYERS: scan/runtime require the L2 image from this run"
    return None


def execution_error(exc, phase):
    """Classify the boundary, never exception text or untrusted program output."""
    if isinstance(exc, OperationError):
        return exc
    static = phase in {"CONFIG", "L0", "L1"}
    if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)):
        return OperationError("GATE_EXECUTION_FAILED", component="gate", phase=phase, outcome="UNKNOWN",
                              retry_policy="after_reconcile", side_effect="none" if static else "unknown", cause=exc)
    if static and isinstance(exc, (OSError, subprocess.CalledProcessError)):
        return OperationError("GATE_ENVIRONMENT_UNAVAILABLE", component="gate", phase=phase,
                              retry_policy="after_configuration", cause=exc)
    return OperationError("INTERNAL_ERROR", component="gate", phase=phase, outcome="UNKNOWN",
                          retry_policy="after_reconcile", side_effect="none" if static else "unknown", cause=exc)


def observe_layer(result, run_id, attempt_id, error=None):
    """Add a safe envelope without replacing existing gate/fixer fields."""
    detail = error.as_dict() if isinstance(error, OperationError) else error or result.get("error")
    if detail is None and not result.get("ok"):
        unavailable = bool(result.get("blocked"))
        detail = OperationError("GATE_ENVIRONMENT_UNAVAILABLE" if unavailable else "GATE_CHECK_FAILED",
                                component="gate", phase=result["layer"], outcome="BLOCKED" if unavailable else "FAIL",
                                retry_policy="after_configuration" if unavailable else "never",
                                side_effect="none" if unavailable or result["layer"] in {"L0", "L1"} else "possible").as_dict()
    outcome = detail["outcome"] if detail else "PASS"
    result.update(outcome=outcome, error=detail)
    result["event"] = event_record("gate.layer.completed", component="gate", phase=result["layer"], outcome=outcome,
                                   run_id=run_id, attempt_id=attempt_id, error=detail)
    return result


def finish_verdict(verdict, run, run_id, attempt_id, *, persist=True):
    """The returned envelope also reports evidence-write failures, even if disk is unavailable."""
    verdict.update(run_id=run_id, attempt_id=attempt_id)
    verdict["error"] = next((r["error"] for r in reversed(verdict["layers"])
                             if r.get("error") and r["error"]["outcome"] == verdict["status"]), None)
    def complete_event():
        return event_record("gate.completed", component="gate", phase="complete", outcome=verdict["status"],
                            run_id=run_id, attempt_id=attempt_id, error=verdict["error"],
                            attributes={"release_eligible": verdict["release_eligible"], "checks_ok": verdict["checks_ok"]})
    verdict["event"] = complete_event()
    if persist and DIAGNOSTICS is not None and DIAGNOSTICS.run == run:
        try:
            verdict['diagnostics'] = DIAGNOSTICS.finish(verdict)
        except (OSError, ValueError, KeyError, TypeError):
            verdict['diagnostics'] = {'state': 'unavailable', 'reason': 'capture_failed'}
    if persist:
        try:
            run.mkdir(parents=True, exist_ok=True)
            failure = verdict["failure"]
            if failure:
                (run / "failure.txt").write_text(
                    f"layer: {failure['layer']}\nclass: {failure['class']}\nsignature: {failure['signature']}\n"
                    f"--- first error (untrusted program output, secrets masked) ---\n{failure['excerpt']}\n")
            (run / "verdict.json").write_text(json.dumps(verdict, indent=2, ensure_ascii=False))
        except OSError as exc:
            error = OperationError("OBSERVATION_WRITE_FAILED", component="gate", phase="evidence-write", outcome="UNKNOWN",
                                   retry_policy="after_reconcile", side_effect="possible", cause=exc)
            verdict.update(ok=False, release_eligible=False, checks_ok=False, status="UNKNOWN", error=error.as_dict())
            verdict["event"] = complete_event()
    return verdict


def build_binding(source, spec, network, run_id):
    # Cache scope is one operation and the entire source, not guessed changed paths.
    # Trusted implementation changes also invalidate reuse.
    trusted = [Path(__file__), CI_PROFILE_PATH, *(PLATFORM / name for name in (
        'execution.py', 'gate/bundle.py', 'process.py', 'source_snapshot.py', 'contract/stages.json'))]
    return {'run_id': run_id, 'source_sha256': source, 'spec': spec, 'network': network,
            'implementation': {str(p.relative_to(PLATFORM)): hashlib.sha256(p.read_bytes()).hexdigest() for p in trusted}}


def reuse_build(path, binding):
    if path is None:
        return None
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError('invalid build receipt')
    receipt = json.loads(path.read_bytes())
    if receipt.get('binding') != binding:
        return None
    if receipt.get('version') != 1 or receipt.get('stage') != 'image.build' or receipt.get('outcome') != 'PASS':
        raise ValueError('invalid build receipt')
    images, identifiers = receipt['images'], receipt['image_ids']
    if set(images) != {svc['name'] for svc in binding['spec']['services']} or set(images) != set(identifiers):
        raise ValueError('invalid build image set')
    for service, tag in images.items():
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', identifiers[service]):
            raise ValueError('invalid build image identifier')
        actual = sh(['docker', 'image', 'inspect', '--format', '{{.Id}}', tag], timeout=15)
        if actual.returncode or actual.stdout.strip() != identifiers[service]:
            return None
    return images, identifiers


def run_gate(ws, run, layers, *, selected_root=None, quality_network=None, repair_scope="packaging", native_locks=None, app_id=None, build_receipt=None):
    global DIAGNOSTICS, PROGRESS
    try:
        with host_capacity('gate'):
            return _run_gate(ws, run, layers, selected_root=selected_root, quality_network=quality_network,
                             repair_scope=repair_scope, native_locks=native_locks, app_id=app_id, build_receipt=build_receipt)
    finally:
        DIAGNOSTICS = PROGRESS = None


def _run_gate(ws, run, layers, *, selected_root=None, quality_network=None, repair_scope="packaging", native_locks=None, app_id=None, build_receipt=None):
    global DIAGNOSTICS, PROGRESS
    DIAGNOSTICS = PROGRESS = None
    observation_id = os.environ.get("RAILSHOT_RUN_ID") or str(uuid.uuid4())
    attempt_id = os.environ.get("RAILSHOT_ATTEMPT_ID")
    invalid = validate_layers(layers)
    if repair_scope not in {"packaging", "source"}:
        invalid = "INVALID_REPAIR_SCOPE"
    if app_id is not None and (not isinstance(app_id, str) or not re.fullmatch(r'[a-z][a-z0-9-]{1,28}[a-z0-9]', app_id)):
        invalid = "INVALID_APP_ID"
    inside_source = run.resolve() == ws.resolve() or ws.resolve() in run.resolve().parents
    if build_receipt is not None and Path(build_receipt).resolve().is_relative_to(ws.resolve()):
        invalid = 'INVALID_BUILD_RECEIPT_PATH'
    if inside_source:
        invalid = "INVALID_RUN_PATH: gate artifacts must be outside the source workspace"
    before, config_error = None, None
    try:
        if not invalid:
            before = source_digest(ws)
            paths = yaml.safe_load((PLATFORM / "contract/paths.yaml").read_text())
            if not isinstance(paths, dict) or not {"limits", "forbidden_patterns"} <= paths.keys():
                raise ValueError("invalid trusted patch policy")
            run.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        invalid = "CONFIGURATION_BLOCKED"
        config_error = OperationError("GATE_CONFIG_INVALID", component="gate", phase="CONFIG",
                                      retry_policy="after_configuration", cause=exc)
    if invalid:
        config_error = config_error or OperationError("GATE_CONFIG_INVALID", component="gate", phase="CONFIG",
                                                      retry_policy="after_configuration")
        result = observe_layer({"layer": "CONFIG", "ok": False, "blocked": invalid}, observation_id, attempt_id, config_error)
        return finish_verdict({"ok": False, "release_eligible": False, "checks_ok": False, "status": "BLOCKED",
                               "layers": [result], "failure": None}, run, observation_id, attempt_id, persist=not inside_source)
    results, failure, spec, images, image_ids = [], None, None, {}, {}
    try:
        progress = Progress(run, observation_id, attempt_id, layers)
    except OperationError as exc:
        result = observe_layer({'layer': 'EVIDENCE', 'ok': False, 'blocked': exc.code}, observation_id, attempt_id, exc)
        return finish_verdict({'ok': False, 'release_eligible': False, 'checks_ok': False, 'status': 'UNKNOWN',
                               'layers': [result], 'failure': None}, run, observation_id, attempt_id)
    PROGRESS = progress
    try:
        DIAGNOSTICS = Diagnostics(ws, run, observation_id, attempt_id, layers, repair_scope, before)
        DIAGNOSTICS.capture()
    except (OSError, ValueError, TypeError):
        DIAGNOSTICS = None
    run_id = uuid.uuid4().hex[:16]  # Docker identity is separate from the durable parent run ID.
    for layer in layers:
        errs, error, reused = [], None, False
        try:
            progress.start(layer)
            if DIAGNOSTICS is not None:
                DIAGNOSTICS.layer = layer
            if layer == "L0":
                errs, changed = l0(ws, paths, repair_scope=repair_scope, native_locks=native_locks)
                result = {"layer": layer, "ok": not errs, "changed": changed, "errors": errs}
            elif layer == "L1":
                errs, spec = l1(ws, app_id=app_id)
                if spec and selected_root is not None:
                    chosen = (ws / selected_root).resolve()
                    if not chosen.is_relative_to(ws.resolve()) or any(
                            not (ws / svc["build"].get("context", ".")).resolve().is_relative_to(chosen)
                            for svc in spec["services"]):
                        errs.append("selected quality root must cover every released service build context")
                result = {"layer": layer, "ok": not errs, "errors": errs}
            elif layer == "Q":
                require_ci_network(quality_network)
                result = {"layer": layer, **run_quality(ws, run, network=quality_network, selected_root=selected_root, repair_scope=repair_scope)}
                errs = result.get("errors", [])
            else:
                if not docker_ok():
                    raise OperationError("GATE_ENVIRONMENT_UNAVAILABLE", component="gate", phase=layer,
                                         retry_policy="after_configuration")
                if layer == "L2":
                    binding = build_binding(before, spec, quality_network, observation_id)
                    cached = reuse_build(build_receipt, binding)
                    if cached:
                        images, image_ids = cached
                        reused = True
                    else:
                        errs, images = l2(ws, spec, run_id, network=quality_network)
                    for service, tag in images.items():
                        image_id = sh(["docker", "image", "inspect", "--format", "{{.Id}}", tag], timeout=15, check=True).stdout.strip()
                        if (not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
                                or reused and image_id != image_ids[service]):
                            raise OperationError("GATE_EVIDENCE_MISMATCH", component="gate", phase=layer,
                                                 retry_policy="after_reconcile", side_effect="possible")
                        image_ids[service] = image_id
                    if not errs and source_digest(ws) == before:
                        receipt = {'version': 1, 'stage': 'image.build', 'outcome': 'PASS',
                                   'binding': binding, 'images': images, 'image_ids': image_ids}
                        durable_write(run / 'build.json', json.dumps(receipt, sort_keys=True).encode())
                elif layer == "L3":
                    required = sorted({key for svc in spec["services"] for key in svc.get("secrets", [])
                                       if key != "DATABASE_URL" or "postgres" not in spec.get("resources", {})})
                    if required:
                        result = {"layer": layer, "ok": False,
                                  "blocked": "MISSING_SECRET: external-service validation needs scoped secret injection",
                                  "required_secret_names": required}
                    else:
                        errs = l3(spec, image_ids, run_id, network=quality_network)
                else:
                    errs = l4(image_ids, network=quality_network)
                if layer != "L3" or not required:
                    result = {"layer": layer, "ok": not errs, "errors": [e.splitlines()[0] for e in errs]}
        except Exception as exc:  # no failed execution may become a release pass
            error = execution_error(exc, layer)
            result = {"layer": layer, "ok": False}
            if error.outcome == "FAIL":
                errs = [str(error)]  # Registered safe summary, never the underlying exception text.
                result["errors"] = errs
            else:
                result["blocked"] = error.code
        result['reused'] = reused
        results.append(observe_layer(result, observation_id, attempt_id, error))
        if layer == "Q":
            results[-1]["advisory"] = True
            results[-1]["advisory"] = quality_advisory(results[-1])
            results[-1]["event"]["attributes"]["advisory"] = results[-1]["advisory"]
        try:
            # Each gate has an independent result artifact even though the bounded
            # repair loop runs in one job to retain its workspace/image store.
            stage = stage_contract(layer)
            receipt = {'version': 1, 'stage': stage['id'], 'input_sha256': before,
                       'run_id': observation_id, 'attempt_id': attempt_id,
                       'outcome': results[-1]['outcome'], 'reused': reused,
                       'image_ids': image_ids if layer in ('L2', 'L3', 'L4') else {},
                       'error': results[-1].get('error'), 'replay': stage['replay']}
            durable_write(run / (layer + '.json'), json.dumps(receipt, sort_keys=True).encode())
            progress.complete(results[-1])
        except (OperationError, KeyError, OSError) as exc:
            observation_error = exc if isinstance(exc, OperationError) else Progress.failure(exc)
            results[-1] = observe_layer({'layer': layer, 'ok': False, 'blocked': observation_error.code},
                                        observation_id, attempt_id, observation_error)
            break
        if quality_advisory(results[-1]):
            continue
        if results[-1].get("blocked"):
            break
        if results[-1].get("errors"):
            text = "\n\n".join(errs)
            cls = classify(layer, text)
            failure = {"layer": layer, "class": cls, "signature": signature(layer, cls, text), "excerpt": excerpt(text),
                       "classification_source": "heuristic"}
            if layer == "Q":
                failure["signature"] = result.get("failure_signature", failure["signature"])
                failure["source_repair_eligible"] = result.get("source_repair_eligible") is True
                failure["check"] = result.get("check")
            break
        if not results[-1]["ok"]:
            break
    after = None
    try:
        after = source_digest(ws)
        if before != after:
            error = OperationError("GATE_EVIDENCE_MISMATCH", component="gate", phase="SOURCE",
                                   retry_policy="after_reconcile", side_effect="possible")
            results.append(observe_layer({"layer": "SOURCE", "ok": False, "blocked": "SOURCE_MUTATED: source changed during gate execution"},
                                         observation_id, attempt_id, error))
        if images:
            for service, tag in images.items():
                image_id = sh(["docker", "image", "inspect", "--format", "{{.Id}}", tag], timeout=15, check=True).stdout.strip()
                if image_id != image_ids.get(service):
                    raise OperationError("GATE_EVIDENCE_MISMATCH", component="gate", phase="EVIDENCE",
                                         retry_policy="after_reconcile", side_effect="possible")
    except Exception as exc:
        error = exc if isinstance(exc, OperationError) else OperationError(
            "GATE_EXECUTION_FAILED", component="gate", phase="EVIDENCE", outcome="UNKNOWN",
            retry_policy="after_reconcile", side_effect="unknown", cause=exc)
        results.append(observe_layer({"layer": "EVIDENCE", "ok": False, "blocked": error.code}, observation_id, attempt_id, error))
    required = [r for r in results if not quality_advisory(r)]
    checks_ok = failure is None and all(r["ok"] for r in required) and len(results) == len(layers)
    ok = checks_ok and tuple(layers) in RELEASE_ORDERS
    status = ("PASS" if ok else "INCOMPLETE" if checks_ok else "UNKNOWN" if any(r["outcome"] == "UNKNOWN" for r in required)
              else "BLOCKED" if any(r["outcome"] == "BLOCKED" for r in required) else "FAIL")
    verdict = {"ok": ok, "release_eligible": ok, "checks_ok": checks_ok, "status": status, "layers": results, "failure": failure,
               "source_sha256": after, "images": images, "image_ids": image_ids, "repair_scope": repair_scope, "selected_root": selected_root, "app_id": app_id}
    return finish_verdict(verdict, run, observation_id, attempt_id)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workspace", nargs="?")
    ap.add_argument("run", nargs="?")
    ap.add_argument("--layers", default=",".join(ORDER))
    ap.add_argument("--quality-network", help="trusted CI network profile for Q/L2/L3; requires locally verified railshot-quality worker, never supplied by upload")
    ap.add_argument("--selected-root", help="trusted selected project path within the uploaded workspace")
    ap.add_argument("--reuse-build", type=Path, help="trusted prior build receipt from the same operation")
    ap.add_argument("--app-id", help="trusted operator app identity, never inferred from uploaded source")
    ap.add_argument("--repair-scope", choices=["packaging", "source"], default="packaging", help="trusted operator patch scope; uploaded specs cannot grant it")
    ap.add_argument("--native-locks", type=Path, help="trusted native-resolution receipt outside the source workspace")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    ws = Path(a.workspace).resolve()
    if a.native_locks and a.native_locks.resolve().is_relative_to(ws):
        ap.error("native lock receipt must be outside the source workspace")
    v = run_gate(ws, Path(a.run).resolve(), a.layers.split(","), quality_network=a.quality_network,
                 repair_scope=a.repair_scope, selected_root=a.selected_root, app_id=a.app_id,
                 native_locks=json.loads(a.native_locks.read_text()) if a.native_locks else None, build_receipt=a.reuse_build)
    print(json.dumps({"ok": v["ok"], "failure": v["failure"] and {k: v["failure"][k] for k in ("layer", "class", "signature")},
                      "status": v["status"], "error": v["error"], "event": v["event"],
                      "layers": [(r["layer"], r["ok"], r.get("blocked")) for r in v["layers"]]}, ensure_ascii=False))
    return 0 if v["ok"] else 1


def self_test():
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d) / "ws"
        ws.mkdir()
        (ws / "app.py").write_text("print('hi')\n")
        (ws / "tests").mkdir()
        (ws / "tests/test_app.py").write_text("def test(): assert True\n")
        sh(["git", "init", "-q"], cwd=ws, check=True)
        sh(["git", "add", "-A"], cwd=ws, check=True)
        sh(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "import"], cwd=ws, check=True)
        # good patch
        (ws / ".railshot").mkdir()
        (ws / ".railshot/railshot.yaml").write_text(yaml.safe_dump({"apiVersion": "railshot/v0", "app": "demo", "services": [
            {"name": "web", "build": {"dockerfile": "Dockerfile"}, "port": 8080, "health": "/", "route": "/"}]}))
        (ws / "Dockerfile").write_text("FROM python:3.12-slim-bookworm AS base\nCOPY app.py /app/app.py\n"
                                       "FROM gcr.io/distroless/base-debian12\nCOPY --from=base /app /app\nUSER 65532\n"
                                       "EXPOSE 8080\nCMD [\"python\", \"/app/app.py\"]\n")
        (ws / ".dockerignore").write_text(".git\n.env*\n")
        v = run_gate(ws, Path(d) / "run1", ["L0", "L1"])
        assert v["checks_ok"] and not v["ok"] and v["status"] == "INCOMPLETE", v
        # reward hack: delete a test + weaken with || true + root user
        (ws / "tests/test_app.py").unlink()
        (ws / "Dockerfile").write_text("FROM ubuntu:24.04\nUSER root\nHEALTHCHECK CMD exit 0\nCMD python app.py || true\n")
        v = run_gate(ws, Path(d) / "run2", ["L0", "L1"])
        assert not v["ok"] and v["failure"]["layer"] == "L0", v
        errs = " ".join(v["layers"][0]["errors"])
        assert "deleted file" in errs and "forbidden pattern" in errs, errs
        assert (Path(d) / "run2/failure.txt").exists()
        # L1 alone catches the Dockerfile problems
        sh(["git", "checkout", "-q", "--", "tests"], cwd=ws)
        v = run_gate(ws, Path(d) / "run3", ["L1"])
        l1errs = " ".join(v["layers"][0]["errors"])
        assert "base image not allowed" in l1errs and "numeric" in l1errs and "exec-form" in l1errs, l1errs
    assert classify("L2", "ERROR: No matching distribution found for flask==9") == "F1"
    assert classify("L2", "net/http: TLS handshake timeout") == "F8"
    assert signature("L3", "F4", "Error: listen EADDRINUSE 0.0.0.0:8080\n").startswith("v2:L3:F4:")
    secret = "ghp_" + "a" * 36
    assert secret not in excerpt("token " + secret)
    print("self-test ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
