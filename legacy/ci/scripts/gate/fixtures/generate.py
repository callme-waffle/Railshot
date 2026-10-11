#!/usr/bin/env python3
"""Copy trusted fixtures, then generate REAL locks/wrappers with native container tools.

No hand-written lock data. Output must be outside this fixture source tree.
The administrator supplies the credential-free, egress-filtered Docker network.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parent
STACKS = ("npm-js", "npm-ts", "nextjs", "fastapi", "maven-spring", "gradle-spring")
ALIASES = {"pnpm-js": "pnpm@10.17.1", "yarn-js": "yarn@4.10.3"}
EXTRA_STACKS = {"requirements-fastapi": "fastapi", "node-test-js": "npm-js"}
NODE_IMAGE = "node:22.23.3-bookworm-slim"
PYTHON_IMAGE = "python:3.12.14-slim-bookworm"


def generation_plan(stack):
    if stack == "node-test-js":
        return generation_plan("npm-js")
    if stack == "requirements-fastapi":
        return PYTHON_IMAGE, """pip install --disable-pip-version-check --no-cache-dir --target /tmp/tools uv==0.12.18
export PATH="/tmp/tools/bin:$PATH" PYTHONPATH=/tmp/tools UV_PYTHON_DOWNLOADS=never
python --version
uv --version
test "$(uv --version | cut -d ' ' -f 2)" = 0.12.18
uv venv /tmp/native-env --python /usr/local/bin/python
uv pip compile requirements.txt --python /usr/local/bin/python --generate-hashes --output-file requirements.native.lock
uv pip sync --python /tmp/native-env/bin/python --require-hashes requirements.native.lock""", ["requirements.txt"]
    if stack in ALIASES:
        pin = ALIASES[stack]
        manager, version = pin.split("@")
        install = "pnpm install --lockfile-only --ignore-scripts" if manager == "pnpm" else "yarn install --mode=skip-build"
        return NODE_IMAGE, f'''npm install --global --prefix /tmp/pm corepack@0.34.0 --ignore-scripts --no-audit --no-fund
export PATH="/tmp/pm/bin:$PATH" COREPACK_HOME=/tmp/corepack COREPACK_ENABLE_DOWNLOAD_PROMPT=0 COREPACK_ENABLE_AUTO_PIN=0
corepack enable --install-directory /tmp/pm/bin
corepack prepare {pin} --activate
node --version
{manager} --version
test "$(node --version)" = v22.23.3
test "$({manager} --version)" = {version}
{install}''', ["pnpm-lock.yaml" if manager == "pnpm" else "yarn.lock"]
    if stack in {"npm-js", "npm-ts", "nextjs"}:
        return NODE_IMAGE, """npm install --global --prefix /tmp/npm npm@12.2.0 --ignore-scripts --no-audit --no-fund
export PATH="/tmp/npm/bin:$PATH"
node --version
npm --version
test "$(node --version)" = v22.23.3
test "$(npm --version)" = 12.2.0
npm install --package-lock-only --ignore-scripts --no-audit --no-fund""", ["package-lock.json"]
    if stack == "fastapi":
        return PYTHON_IMAGE, """pip install --disable-pip-version-check --no-cache-dir --target /tmp/tools uv==0.12.18
export PATH="/tmp/tools/bin:$PATH" PYTHONPATH=/tmp/tools UV_PYTHON_DOWNLOADS=never
python --version
uv --version
test "$(uv --version | cut -d ' ' -f 2)" = 0.12.18
uv lock --python /usr/local/bin/python""", ["uv.lock"]
    if stack == "maven-spring":
        return "maven:3.9.9-eclipse-temurin-21", """java -version
mvn --version
mvn -B -ntp -Dmaven.repo.local=/tmp/maven/repository org.apache.maven.plugins:maven-wrapper-plugin:3.3.4:wrapper -Dmaven=3.9.9 -Dtype=bin
curl --fail --silent --show-error --location https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/3.9.9/apache-maven-3.9.9-bin.zip --output /tmp/maven.zip
printf '\\ndistributionSha256Sum=%s\\n' "$(sha256sum /tmp/maven.zip | cut -d ' ' -f 1)" >> .mvn/wrapper/maven-wrapper.properties
MAVEN_CONFIG= sh ./mvnw --version""", ["mvnw", ".mvn/wrapper/maven-wrapper.properties", ".mvn/wrapper/maven-wrapper.jar"]
    if stack == "gradle-spring":
        return "gradle:8.14.3-jdk21", """java -version
gradle --version
checksum=$(curl --fail --silent --show-error --location https://services.gradle.org/distributions/gradle-8.14.3-bin.zip.sha256)
gradle --no-daemon --console=plain wrapper --gradle-version 8.14.3 --distribution-type bin --gradle-distribution-sha256-sum "$checksum"
GRADLE_USER_HOME=/tmp/gradle-record gradle --no-daemon --console=plain --refresh-dependencies --write-locks --write-verification-metadata sha256 check
rm -rf /tmp/gradle /tmp/gradle-record
GRADLE_USER_HOME=/tmp/gradle-clean gradle --no-daemon --console=plain --dependency-verification=strict check""", ["gradlew", "gradle/wrapper/gradle-wrapper.jar", "gradle/wrapper/gradle-wrapper.properties", "gradle.lockfile", "gradle/verification-metadata.xml"]
    raise ValueError("unsupported fixture")


def hashes(directory, names):
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in names}


def generate(stack, output, network, timeout):
    target = output / stack
    if target.exists():
        raise ValueError(f"refusing to overwrite existing fixture: {target}")
    shutil.copytree(ROOT / ("npm-js" if stack in ALIASES else EXTRA_STACKS.get(stack, stack)), target)
    if stack == "requirements-fastapi":
        config = target / "pyproject.toml"
        config.write_text("[tool.ruff.lint]" + config.read_text().split("[tool.ruff.lint]", 1)[1])
        (target / "uv.lock").unlink(missing_ok=True)
        (target / "requirements.txt").write_text("fastapi==0.141.1\nuvicorn==0.53.0\nhttpx==0.28.1\n")
        (target / "requirements.txt").chmod(0o644)  # trusted fixture input; readable by Q's non-root UID
        (target / "Dockerfile").write_text(f'''FROM {PYTHON_IMAGE} AS dependencies
WORKDIR /app
RUN pip install --no-cache-dir uv==0.12.18
COPY requirements.txt ./
RUN uv venv /app/.venv --python /usr/local/bin/python && uv pip compile requirements.txt --generate-hashes --output-file /tmp/requirements.lock && uv pip sync --python /app/.venv/bin/python --require-hashes /tmp/requirements.lock
FROM {PYTHON_IMAGE}
WORKDIR /app
COPY --from=dependencies /app/.venv ./.venv
COPY app.py ./
USER 10001
CMD ["/app/.venv/bin/uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
''')
    if stack == "node-test-js":
        manifest = target / "package.json"
        package = json.loads(manifest.read_text())
        package["name"] = "railshot-fixture-node-test-js"
        package["scripts"] = {"test": "node --test", "start": "node src/server.js"}
        package.pop("devDependencies", None)
        manifest.write_text(json.dumps(package, indent=2) + "\n")
        (target / "eslint.config.mjs").unlink()
        # Trusted E2E fixture only; product preparation never writes user tests.
        (target / "src/app.test.js").write_text('''import assert from "node:assert/strict";
import test from "node:test";
import { once } from "node:events";
import { app } from "./app.js";

test("health returns the application result", async () => {
  const server = app.listen(0, "127.0.0.1");
  await once(server, "listening");
  try {
    const response = await fetch(`http://127.0.0.1:${server.address().port}/health`);
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { status: "ready" });
  } finally {
    await new Promise((resolve, reject) => server.close(error => error ? reject(error) : resolve()));
  }
});
''')
    if stack in ALIASES:
        pin = ALIASES[stack]
        manager = pin.split("@")[0]
        manifest = target / "package.json"
        package = json.loads(manifest.read_text())
        package["name"] = "railshot-fixture-" + stack
        package["packageManager"] = pin
        if manager == "yarn":
            # Yarn does not auto-install Vitest's Vite peer as npm/pnpm do.
            package["devDependencies"]["vite"] = "8.3.1"
        manifest.write_text(json.dumps(package, indent=2) + "\n")
        lock = "pnpm-lock.yaml" if manager == "pnpm" else "yarn.lock .yarnrc.yml"
        install = ("pnpm install --frozen-lockfile --prod" if manager == "pnpm" else
                   "yarn install --immutable && YARN_ENABLE_IMMUTABLE_INSTALLS=true yarn workspaces focus --all --production")
        if manager == "yarn":
            (target / ".yarnrc.yml").write_text("nodeLinker: node-modules\n")
            (target / ".yarnrc.yml").chmod(0o644)
        (target / "Dockerfile").write_text(f'''FROM {NODE_IMAGE} AS dependencies
WORKDIR /app
COPY package.json {lock} ./
RUN npm install --global corepack@0.34.0 --ignore-scripts --no-audit --no-fund && corepack enable && corepack prepare {pin} --activate && {install}
FROM {NODE_IMAGE}
WORKDIR /app
COPY --from=dependencies /app/node_modules ./node_modules
COPY package.json ./
COPY src ./src
USER 10001
CMD ["node", "src/server.js"]
''')
    image, script, required = generation_plan(stack)
    name = "railshot-fixturegen-" + uuid.uuid4().hex[:12]
    command = ["docker", "run", "--name", name, "--platform", "linux/amd64", "--network", network,
               "--user", f"{os.getuid()}:{os.getgid()}", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
               "--cpus=2", "--memory=2g", "--memory-swap=2g", "--pids-limit=256", "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=1073741824,mode=1777",
               "--mount", f"type=bind,src={target.resolve()},dst=/work", "--workdir", "/work",
               "-e", "HOME=/tmp/home", "-e", "MAVEN_OPTS=-Duser.home=/tmp/home", "-e", "MAVEN_USER_HOME=/tmp/maven",
               "-e", "GRADLE_USER_HOME=/tmp/gradle", "--entrypoint=sh", image, "-c", "set -eu\nmkdir -p /tmp/home\n" + script]
    receipt = {"stack": stack, "status": "RUNNING", "command": command, "image": image, "started": int(time.time()),
               "note": "native lock/wrapper generation, not a product gate or E2E pass"}
    output.mkdir(parents=True, exist_ok=True)
    log_path = output / f"{stack}-generation.log"
    try:
        with log_path.open("w") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        receipt["exit_code"] = result.returncode
        receipt["status"] = "GENERATED" if result.returncode == 0 and all((target / p).is_file() for p in required) else "FAILED"
        if receipt["status"] == "GENERATED":
            if stack == "requirements-fastapi":
                resolution = output / (stack + "-native.lock")
                shutil.move(target / "requirements.native.lock", resolution)
                receipt["native_resolution"] = {"path": resolution.name, "sha256": hashlib.sha256(resolution.read_bytes()).hexdigest()}
            # Keep generated dependency evidence, not generation-time build outputs.
            for directory in ("node_modules", ".venv", ".gradle", ".yarn", "build", "target"):
                if (target / directory).is_dir():
                    shutil.rmtree(target / directory)
            receipt["generated_sha256"] = hashes(target, required)
            actual = subprocess.run(["docker", "image", "inspect", "--format", "{{json .}}", image], check=True, capture_output=True, text=True, timeout=30)
            inspected = json.loads(actual.stdout)
            receipt["image_id"] = inspected["Id"]
            receipt["repo_digests"] = inspected.get("RepoDigests", [])
    except (OSError, subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError) as exc:
        receipt.update(status="BLOCKED", error=type(exc).__name__)
    finally:
        try:
            clean = subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)
            receipt["cleanup_ok"] = clean.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            receipt["cleanup_ok"] = False
        if not receipt["cleanup_ok"]:
            receipt["status"] = "BLOCKED"
        receipt["finished"] = int(time.time())
        receipt["log"] = log_path.name
        (output / f"{stack}-generation.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stacks", default=",".join(STACKS))
    parser.add_argument("--network", required=True)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    stacks = args.stacks.split(",")
    if any(s not in (*STACKS, *ALIASES, *EXTRA_STACKS) for s in stacks) or len(stacks) != len(set(stacks)):
        parser.error("choose unique supported stack names")
    if args.network in {"host", "bridge", "none"} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.network):
        parser.error("supply the administrator-registered egress-filtered network")
    output = args.output.resolve()
    if output == ROOT or ROOT in output.parents or "," in str(output):
        parser.error("output must be outside fixture source, without Docker mount delimiters")
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for stack in stacks:
        result = generate(stack, output, args.network, args.timeout)
        results.append({"stack": stack, "status": result["status"]})
        print(json.dumps(results[-1]), flush=True)
    return 0 if all(r["status"] == "GENERATED" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
