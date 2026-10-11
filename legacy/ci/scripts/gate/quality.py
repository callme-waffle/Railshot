"""Platform-owned quality plans. Repository commands run only in bounded Docker.

The caller must register an egress-filtered Docker network before dependency
installation. A named network is an administrator assertion, not a firewall.
"""
import base64
import binascii
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import resource
import shlex
import subprocess
import sys
import tempfile
try:
    import tomllib
except ImportError:  # Python 3.9/3.10 local checks; production runner uses 3.12+
    import tomli as tomllib
import uuid
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from observability import OperationError  # noqa: E402
from bundle import stage_source  # noqa: E402

NODE = "22.23.3"
PYTHON = "3.12.14"
UV = "0.12.18"
STAGE_CODES = {"prepare": 201, "lint": 202, "type": 203, "unit": 204, "report": 205}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "target", "build", ".next"}
MANIFESTS = {"package.json", "pyproject.toml", "requirements.txt", "pom.xml", "build.gradle", "build.gradle.kts"}
WEAKEN = re.compile(r"passWithNoTests|--if-present|--skipTests|skipTests\s*=\s*true|maven.test.skip\s*=\s*true|\|\|\s*true|\bexit\s+0\b")


def blocked(reason, *, error=None, **extra):
    error = error or OperationError("GATE_ENVIRONMENT_UNAVAILABLE", component="gate", phase="Q",
                                    retry_policy="after_configuration")
    detail = error.as_dict() if isinstance(error, OperationError) else error
    return {"ok": False, "status": detail["outcome"], "blocked": reason, "error": detail, **extra}


def discovery_blocked(exc, path):
    # Only registered legacy reason tokens survive; parser messages may contain
    # a complete uploaded line, credentials, or an absolute filesystem path.
    reasons = {"BUILD_ROOT_INVALID", "BUILD_GRAPH_UNRESOLVED", "TOOLCHAIN_UNRESOLVED", "TOOLCHAIN_CONFLICT",
               "MISSING_LOCK", "AMBIGUOUS_LOCK", "UNSUPPORTED", "QUALITY_POLICY", "MISSING_CHECKER",
               "NO_TESTS", "UNSUPPORTED_TEST_REPORTER", "MISSING_TOOLCHAIN"}
    token = str(exc).partition(":")[0]
    reason = token if token in reasons else "QUALITY_CONFIGURATION_INVALID"
    if str(exc) == "UNSUPPORTED: npm workspaces require an explicit project plan":
        reason = "UNSUPPORTED: npm workspaces require an explicit project plan"
    error = OperationError("GATE_CONFIG_INVALID", component="gate", phase="Q.discovery",
                           retry_policy="after_configuration", cause=exc)
    return blocked(reason, path=path, error=error)


def exact_version(path, default):
    if not path.exists():
        return default
    version = path.read_text().strip().removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"TOOLCHAIN_UNRESOLVED: {path.name} requires an exact version")
    return version


def test_files(root):
    return [p for p in root.rglob("*") if p.is_file() and not (set(p.relative_to(root).parts) & SKIP_DIRS)
            and (re.search(r"(^test_.*\.py$|.*_test\.py$|\.(test|spec)\.[cm]?[jt]sx?$|.*Tests?\.java$)", p.name))]


def javascript_manager(root, pkg):
    locks = {name: [f for f in files if (root / f).is_file()] for name, files in {
        "npm": ("package-lock.json", "npm-shrinkwrap.json"), "pnpm": ("pnpm-lock.yaml",), "yarn": ("yarn.lock",)}.items()}
    found = [name for name, files in locks.items() if files]
    if len(found) != 1:
        raise ValueError("MISSING_LOCK" if not found else "AMBIGUOUS_LOCK: multiple package managers")
    name = found[0]
    declared = pkg.get("packageManager", "")
    if declared and not re.fullmatch(r"(?:npm|pnpm|yarn)@\d+\.\d+\.\d+(?:\+sha(?:224|256|384|512)\.[0-9a-f]+)?", declared):
        raise ValueError("TOOLCHAIN_UNRESOLVED: packageManager needs an exact npm/pnpm/yarn version")
    if declared and declared.split("@")[0] != name:
        raise ValueError("TOOLCHAIN_CONFLICT: packageManager and lockfile disagree")
    if name == "npm":
        if "+" in declared:
            raise ValueError("TOOLCHAIN_UNRESOLVED: npm packageManager integrity suffix needs a verified Corepack npm profile")
        pin = declared or "npm@bundled-with-node-image"
        setup = ([f"npm install --global --prefix /tmp/npm {declared.split('+')[0]} --ignore-scripts --no-audit --no-fund",
                  'export PATH="/tmp/npm/bin:$PATH"', 'test "$(npm --version)" = ' + shlex.quote(declared.split('@')[1])] if declared else [])
        return name, pin, setup, "npm ci --engine-strict --no-audit --no-fund", "npm run ", " -- ", "./node_modules/.bin/"
    default = "pnpm@10.17.1" if name == "pnpm" else ("yarn@1.22.22" if "# yarn lockfile v1" in (root / "yarn.lock").read_text() else "yarn@4.10.3")
    pin = declared or default
    setup = ["npm install --global --prefix /tmp/pm corepack@0.34.0 --ignore-scripts --no-audit --no-fund",
             'export PATH="/tmp/pm/bin:$PATH" COREPACK_HOME=/tmp/corepack COREPACK_ENABLE_DOWNLOAD_PROMPT=0 COREPACK_ENABLE_AUTO_PIN=0',
             "corepack enable --install-directory /tmp/pm/bin", "corepack prepare " + shlex.quote(pin) + " --activate",
             'test "$(' + name + ' --version)" = ' + shlex.quote(pin.split('@')[1].split('+')[0])]
    install = ("pnpm install --frozen-lockfile --config.engine-strict=true" if name == "pnpm" else
               "yarn install --frozen-lockfile --non-interactive" if pin.startswith("yarn@1.") else "yarn install --immutable")
    return name, pin, setup, install, name + " run ", " ", name + " exec "


def npm_plan(root):
    pkg = json.loads((root / "package.json").read_text())
    if pkg.get("workspaces") or (root / "pnpm-workspace.yaml").exists():
        raise ValueError("UNSUPPORTED: npm workspaces require an explicit project plan")
    manager, pin, commands, install, run, test_separator, execute = javascript_manager(root, pkg)
    scripts = pkg.get("scripts", {})
    if any(WEAKEN.search(str(value)) for value in scripts.values()):
        raise ValueError("QUALITY_POLICY: bypass/skip flags in package scripts")
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    types = any(root.glob("tsconfig*.json")) or "typescript" in deps
    generated_tools = {}
    node = exact_version(root / ".node-version", exact_version(root / ".nvmrc", NODE))
    volta = pkg.get("volta", {}).get("node")
    if volta:
        if not re.fullmatch(r"\d+\.\d+\.\d+", volta):
            raise ValueError("TOOLCHAIN_UNRESOLVED: volta.node must be exact")
        node = volta
    if node.split(".")[0] not in {"22", "24"}:
        raise ValueError("UNSUPPORTED: approved Node profiles are 22 and 24")
    # Use the selected image's bundled semver implementation, not an advisory warning.
    version_check = """const fs=require('fs'),s=require('/usr/local/lib/node_modules/npm/node_modules/semver'),p=require('./package.json');const requirements=[p.engines?.node,p.volta?.node,...['.nvmrc','.node-version'].filter(x=>fs.existsSync(x)).map(x=>fs.readFileSync(x,'utf8').trim())].filter(Boolean);if(requirements.some(r=>!s.validRange(r)||!s.satisfies(process.version,r)))throw Error('TOOLCHAIN_CONFLICT: node declarations');"""
    commands += ["node -e " + shlex.quote(version_check), install]
    if "lint" in scripts:
        commands.append(run + "lint")
    elif "eslint" in deps and any(root.glob("eslint.config.*")):
        commands.append(execute + "eslint .")
    else:
        # Platform tooling lives outside the uploaded project; never rewrite its manifest or policy.
        legacy_config = any(root.glob(".eslintrc*")) and not any(root.glob("eslint.config.*"))
        generated_tools = {"eslint": "8.57.1" if legacy_config else "10.11.0", "globals": "17.13.0"}
        if types:
            generated_tools.update({"typescript-eslint": "8.71.0", "typescript": "5.9.3"})
        commands += ["npm install --prefix /tmp/quality-tools --ignore-scripts --no-audit --no-fund " +
                     " ".join(name + "@" + version for name, version in generated_tools.items())]
        if any(root.glob("eslint.config.*")) or any(root.glob(".eslintrc*")):
            # Existing config remains authoritative; plugin/version incompatibility is a preparation failure.
            commands.append(("ESLINT_USE_FLAT_CONFIG=false " if legacy_config else "") + "/tmp/quality-tools/node_modules/.bin/eslint .")
        else:
            config = "import globals from './node_modules/globals/index.js';\n"
            if types:
                config += "import ts from './node_modules/typescript-eslint/dist/index.js';\n"
            config += ("export default [{ignores:['**/node_modules/**','**/dist/**','**/build/**','**/.next/**']}," +
                       ("...ts.configs.recommended," if types else "") +
                       "{files:['**/*.{js,mjs,cjs,jsx,ts,tsx}'],languageOptions:{ecmaVersion:'latest',sourceType:'module',globals:{...globals.node,...globals.browser}},rules:" +
                       ("{}" if types else "{'no-undef':'error','no-unused-vars':['error',{argsIgnorePattern:'^_'}]}") + "}];\n")
            commands += ["printf %s " + shlex.quote(config) + " > /tmp/quality-tools/eslint.config.mjs",
                         "/tmp/quality-tools/node_modules/.bin/eslint --config /tmp/quality-tools/eslint.config.mjs ."]
    if types:
        type_script = next((x for x in ("typecheck", "type-check", "check-types") if x in scripts), None)
        if type_script:
            commands.append(run + type_script)
        elif "typescript" in deps and (root / "tsconfig.json").exists():
            commands.append(execute + "tsc --noEmit")
        elif (root / "tsconfig.json").exists():
            if "typescript" not in generated_tools:
                commands.append("npm install --prefix /tmp/quality-tools --ignore-scripts --no-audit --no-fund typescript@5.9.3")
                generated_tools["typescript"] = "5.9.3"
            commands.append("/tmp/quality-tools/node_modules/.bin/tsc --noEmit")
        else:
            raise ValueError("MISSING_CHECKER: TypeScript typecheck is required")
    if not test_files(root):
        raise ValueError("NO_TESTS: no existing unit test files discovered")
    test = scripts.get("test", "")
    report_format = "json"
    if re.search(r"\bvitest\b", test) and "vitest" in deps:
        commands.append(run + "test" + test_separator + "--run --reporter=json --outputFile=/tmp/unit.json")
    elif re.search(r"\bjest\b", test) and "jest" in deps:
        commands.append(run + "test" + test_separator + "--ci --runInBand --json --outputFile=/tmp/unit.json")
    else:
        # Only the native command and verified file/glob arguments are translated.
        # Appending Node flags after file arguments would silently miss the reporter.
        try:
            native = shlex.split(test)
        except ValueError as exc:
            raise ValueError("UNSUPPORTED_TEST_REPORTER: invalid test command") from exc
        if native[:2] != ["node", "--test"] or any(name in scripts for name in ("pretest", "posttest")):
            raise ValueError("UNSUPPORTED_TEST_REPORTER: use locked Jest/Vitest or plain node --test")
        for pattern in native[2:]:
            if (not re.fullmatch(r"[\w ./*?\[\]-]+\.[cm]?js", pattern) or pattern.startswith("-")
                    or Path(pattern).is_absolute() or ".." in Path(pattern).parts):
                raise ValueError("UNSUPPORTED_TEST_REPORTER: node --test accepts repository file/glob paths only")
            matches = list(root.glob(pattern))
            if not matches or any(not p.is_file() or not p.resolve().is_relative_to(root.resolve()) for p in matches):
                raise ValueError("UNSUPPORTED_TEST_REPORTER: Node test path must resolve inside the project")
        report_format = "junit"
        command = "node --test --test-reporter=junit --test-reporter-destination=/tmp/unit.xml"
        command += "".join(" " + shlex.quote(pattern) for pattern in native[2:])
        commands.append(command + ' || { railshot_node_rc=$?; cat /tmp/unit.xml; exit "$railshot_node_rc"; }')
        commands.append("printf '\\nRAILSHOT_JUNIT_BEGIN\\n'; cat /tmp/unit.xml; printf '\\nRAILSHOT_JUNIT_END\\n'")
    if report_format == "json":
        commands[-1] += ' || { railshot_test_rc=$?; if [ -f /tmp/unit.json ]; then cat /tmp/unit.json || :; fi; exit "$railshot_test_rc"; }'
        commands.append("node -e " + shlex.quote("const r=require('/tmp/unit.json');if(!Number.isInteger(r.numTotalTests)||r.numTotalTests<=0||r.numPendingTests||r.numTodoTests||!r.success)throw Error('NO_TESTS or skipped/failed tests');console.log('RAILSHOT_TESTS='+r.numTotalTests)"))
    return {"stack": "nextjs" if "next" in deps else "typescript" if types else "javascript",
            "image": f"node:{node}-bookworm-slim", "commands": commands,
            "checks": ["lint", "type" if types else "type:not-applicable", "unit"], "lock": install,
            "package_manager": manager, "package_manager_pin": pin, "report_format": report_format,
            "generated_tools": generated_tools, "generated_tools_location": "/tmp/quality-tools" if generated_tools else None}


def python_plan(root):
    has_pyproject = (root / "pyproject.toml").exists()
    data = tomllib.loads((root / "pyproject.toml").read_text()) if has_pyproject else {}
    # A pyproject containing only checker configuration can accompany requirements.
    project = has_pyproject and ("project" in data or "poetry" in data.get("tool", {}) or (root / "uv.lock").exists())
    if project and not (root / "uv.lock").exists():
        raise ValueError("MISSING_LOCK: pyproject.toml requires uv.lock")
    if not project and not (root / "requirements.txt").exists():
        raise ValueError("MISSING_LOCK: provide pyproject.toml + uv.lock or requirements.txt")
    lock = tomllib.loads((root / "uv.lock").read_text()) if project else {}
    tools = {p.get("name") for p in lock.get("package", [])}
    if not test_files(root):
        raise ValueError("NO_TESTS: no existing unit test files discovered")
    python = exact_version(root / ".python-version", PYTHON)
    if python.split(".")[:2] not in [["3", "12"], ["3", "13"]]:
        raise ValueError("UNSUPPORTED: approved Python profiles are 3.12 and 3.13")
    pins = {"ruff": "0.16.9", "mypy": "2.3.1", "pytest": "9.1.1"}
    artifact = "/tmp/railshot-python"
    environment = artifact + "/environment"
    # Determine missing tools from the installed environment, including indirect
    # dependencies. A locked/declared older checker must never be upgraded here.
    prepare = "\n".join([
        "import hashlib, importlib.metadata as M, json, pathlib",
        f"p = pathlib.Path({artifact!r})",
        "versions = {d.metadata['Name'].lower().replace('_', '-'): d.version for d in M.distributions()}",
        f"pins = {pins!r}",
        "missing = {name: version for name, version in pins.items() if name not in versions}",
        "frozen = (p / 'app-requirements.txt').read_text()",
        "(p / 'overlay.in').write_text(frozen + '\\n' + ''.join(f'{n}=={v}\\n' for n, v in missing.items()))",
        "record = {'app_versions': versions, 'generated_tools': missing, 'retained_tools': {n: versions[n] for n in pins if n in versions}}",
        "record['artifacts'] = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in p.iterdir() if f.is_file()}",
        "(p / 'provenance.json').write_text(json.dumps(record, sort_keys=True))",
        "print('RAILSHOT_PYTHON_PREPARED=' + json.dumps(record, sort_keys=True))",
    ])
    verify = "\n".join([
        "import hashlib, importlib.metadata as M, json, pathlib, sys",
        f"p = pathlib.Path({artifact!r})",
        "record = json.loads((p / 'provenance.json').read_text())",
        "assert all(M.version(n) == v for n, v in record['app_versions'].items()), 'APP_DEPENDENCY_CHANGED_BY_TOOL_OVERLAY'",
        f"record['tools'] = {{n: M.version(n) for n in {list(pins)!r}}}",
        "record['python'] = sys.version.split()[0]",
        "record['resolved_versions'] = {d.metadata['Name']: d.version for d in M.distributions()}",
        "(p / 'resolved-environment.json').write_text(json.dumps(record['resolved_versions'], sort_keys=True))",
        "record['artifacts']['resolved-environment.json'] = hashlib.sha256((p / 'resolved-environment.json').read_bytes()).hexdigest()",
        "(p / 'provenance.json').write_text(json.dumps(record, sort_keys=True))",
        "print('RAILSHOT_PYTHON_PROVENANCE=' + json.dumps(record, sort_keys=True))",
        "names = ['requirements.lock', 'app-requirements.txt', 'overlay.in', 'provenance.json', 'resolved-environment.json']",
        "artifacts = {name: (p / name).read_text() for name in names if name != 'requirements.lock' or (p / name).exists()}",
        "payload = json.dumps(artifacts, sort_keys=True)",
        "assert len(payload.encode()) <= 1024 * 1024, 'PYTHON_ARTIFACTS_TOO_LARGE'",
        "print('RAILSHOT_PYTHON_ARTIFACTS=' + payload)",
    ])
    report = "import xml.etree.ElementTree as E; r=E.parse('/tmp/unit.xml').getroot(); s=[r] if r.tag=='testsuite' else list(r.iter('testsuite')); n=sum(int(x.get('tests',0)) for x in s); skipped=sum(int(x.get('skipped',0)) for x in s); assert n>0 and skipped==0, 'NO_TESTS or skipped tests'; print('RAILSHOT_TESTS='+str(n))"
    zero_report = ("import pathlib,xml.etree.ElementTree as E; b=pathlib.Path('/tmp/unit.xml').read_bytes(); "
                   "assert len(b)<=4194304 and b'<!DOCTYPE' not in b; r=E.fromstring(b); "
                   "s=list(r.iter('testsuite')); assert r.tag in ('testsuite','testsuites') and s; "
                   "assert all(x.get('tests')=='0' for x in s) and not list(r.iter('testcase')); "
                   "assert not any(x.tag in ('failure','error','skipped') or any(int(x.get(k,'0')) for k in ('failures','errors','skipped','disabled')) for x in r.iter()); print('NO_TESTS')")
    commands = [f"pip install --disable-pip-version-check --no-cache-dir --target /tmp/tools uv=={UV}",
                'export PATH="/tmp/tools/bin:$PATH" PYTHONPATH=/tmp/tools UV_PYTHON_DOWNLOADS=never UV_PYTHON=python',
                f"mkdir -p {artifact}",
                f"export UV_PROJECT_ENVIRONMENT={environment} VIRTUAL_ENV={environment}"]
    if project:
        commands += ["uv sync --locked --all-groups --no-progress"]
        run = f"uv run --no-sync --with-requirements {artifact}/overlay.in "
    else:
        commands += [f"uv venv {environment} --python python",
                     f"uv pip compile requirements.txt --python python --generate-hashes --output-file {artifact}/requirements.lock --quiet",
                     f"uv pip sync --python {environment}/bin/python --require-hashes {artifact}/requirements.lock"]
        run = f"uv run --no-project --active --with-requirements {artifact}/overlay.in "
    # uv --with may override project constraints. Freeze every installed app
    # dependency into the overlay, so incompatible checker additions fail closed.
    commands += [f"uv pip freeze --exclude-editable --python {environment}/bin/python > {artifact}/app-requirements.txt",
                 f"{environment}/bin/python -c " + shlex.quote(prepare),
                 run + "python -c " + shlex.quote(verify),
                 run + "ruff check .", run + "mypy .", run + "pytest --junitxml=/tmp/unit.xml" +
                 ' || { railshot_pytest_rc=$?; if [ "$railshot_pytest_rc" -eq 5 ] && python -c ' + shlex.quote(zero_report) +
                 '; then exit 1; fi; exit "$railshot_pytest_rc"; }',
                 "python -c " + shlex.quote(report)]
    return {"stack": "fastapi" if "fastapi" in tools else "python", "image": f"python:{python}-slim-bookworm",
            "commands": commands, "checks": ["lint", "type", "unit"],
            "lock": "uv--locked" if project else "run-scoped-hashed-requirements",
            "generated_tools": {name: version for name, version in pins.items() if name not in tools},
            "provenance": {"policy": "missing installed checkers only; preserve app versions and repository files",
                           "generated_tools_status": "candidates; installed metadata decides at preparation",
                           "directory": artifact, "environment": artifact + "/resolved-environment.json",
                           "record": artifact + "/provenance.json",
                           "resolution": "uv.lock" if project else artifact + "/requirements.lock"},
            "requires_python": data.get("project", {}).get("requires-python")}


def java_toolchain(root, maven):
    """Uploaded wrappers are declarations, never executable checker authorities."""
    wrapper = root / (".mvn/wrapper/maven-wrapper.properties" if maven else "gradle/wrapper/gradle-wrapper.properties")
    if not (root / ("mvnw" if maven else "gradlew")).is_file() or not wrapper.is_file():
        raise ValueError("MISSING_TOOLCHAIN: checked-in wrapper declaration is required")
    properties = wrapper.read_text().replace(r"\:", ":")
    checksums = re.findall(r"(?m)^distributionSha256Sum\s*=\s*([0-9a-fA-F]{64})\s*$", properties)
    if len(checksums) != 1:
        raise ValueError("MISSING_LOCK: exactly one wrapper distribution SHA-256 is required")
    urls = re.findall(r"(?m)^distributionUrl\s*=\s*(\S+)\s*$", properties)
    version = "3.9.9" if maven else "8.14.3"
    url = (f"https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/{version}/apache-maven-{version}-bin.zip"
           if maven else f"https://services.gradle.org/distributions/gradle-{version}-bin.zip")
    if urls != [url]:
        raise ValueError("TOOLCHAIN_CONFLICT: wrapper distribution must match the approved native image version and origin")
    executable = "/usr/share/maven/bin/mvn" if maven else "/opt/gradle/bin/gradle"
    version_pattern = (r"^Apache Maven " + re.escape(version) + r"( |$)" if maven else r"^Gradle " + re.escape(version) + r"$")
    # Verify the declared archive integrity without executing its wrapper script.
    commands = ["export MAVEN_CONFIG= MAVEN_ARGS=", "java -version",
                f"{executable} --version > /tmp/railshot-java-version.txt",
                "cat /tmp/railshot-java-version.txt",
                "grep -Eq " + shlex.quote(version_pattern) + " /tmp/railshot-java-version.txt",
                "curl --fail --silent --show-error --location --max-time 180 --output /tmp/railshot-java-distribution.zip " + shlex.quote(url),
                "printf '%s  %s\\n' " + checksums[0].lower() + " /tmp/railshot-java-distribution.zip | sha256sum -c -",
                "rm /tmp/railshot-java-distribution.zip"]
    return executable, version, commands


def java_plan(root):
    maven = (root / "pom.xml").exists()
    executable, version, commands = java_toolchain(root, maven)
    if not test_files(root):
        raise ValueError("NO_TESTS: no existing Java tests discovered")
    build_files = [p for p in root.rglob("*") if p.is_file() and p.name in {"pom.xml", "build.gradle", "build.gradle.kts"}
                   and not set(p.relative_to(root).parts) & SKIP_DIRS]
    text = "\n".join(p.read_text() for p in build_files)
    if WEAKEN.search(text) or re.search(r"SNAPSHOT|<version>\s*(LATEST|RELEASE)|:[^'\"\s]*\+", text):
        raise ValueError("QUALITY_POLICY: dynamic dependencies or test bypass configuration")
    if maven:
        pom = ET.fromstring((root / "pom.xml").read_text())
        qualified = False
        for plugin in pom.findall("./{*}build/{*}plugins/{*}plugin"):
            artifact = plugin.findtext("{*}artifactId")
            if artifact not in {"maven-checkstyle-plugin", "maven-pmd-plugin", "spotbugs-maven-plugin"}:
                continue
            for execution in plugin.findall("./{*}executions/{*}execution"):
                phase = execution.findtext("{*}phase")
                goals = [g.text for g in execution.findall("./{*}goals/{*}goal")]
                if phase in {"validate", "compile", "test", "prepare-package", "package", "verify"} and "check" in goals and plugin.findtext("{*}version"):
                    qualified = True
        if not qualified:
            raise ValueError("MISSING_CHECKER: bind a versioned quality plugin check goal to Maven verify lifecycle")
        command = executable + " -B -ntp -Dmaven.repo.local=/tmp/maven/repository -DskipTests=false -Dmaven.test.skip=false -DfailIfNoTests=true clean verify"
        report_glob = "*/target/surefire-reports/TEST-*.xml"
    else:
        if not list(root.rglob("gradle.lockfile")) or not (root / "gradle/verification-metadata.xml").exists():
            raise ValueError("MISSING_LOCK: Gradle dependency locks and verification metadata are required")
        if not re.search(r"checkstyle|\bpmd\b|spotbugs", text):
            raise ValueError("MISSING_CHECKER: Gradle check must include an existing quality plugin")
        quality_task = "checkstyleMain checkstyleTest" if "checkstyle" in text else "pmdMain pmdTest" if re.search(r"\bpmd\b", text) else "spotbugsMain spotbugsTest"
        init_script = ("allprojects { dependencyLocking { lockAllConfigurations(); lockMode = LockMode.STRICT }; "
                       "tasks.withType(org.gradle.api.tasks.testing.Test).configureEach { testLogging { "
                       "exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL; "
                       "showCauses = true; showExceptions = true; showStackTraces = true } } }")
        commands.append("printf '%s\\n' " + shlex.quote(init_script) + " > /tmp/railshot-lock.gradle")
        command = (executable + " --no-daemon --console=plain --no-build-cache --rerun-tasks --dependency-verification=strict "
                   "--init-script /tmp/railshot-lock.gradle clean check " + quality_task)
        report_glob = "*/build/test-results/test/TEST-*.xml"
    # Only the /work copy is changed. Delete stale canonical artifacts before the
    # native checker; report files must also be newer than this attempt's marker.
    commands += [f"find . -path '{report_glob}' -type f -delete", "touch /tmp/railshot-java-start", command,
                 "printf '\\nRAILSHOT_JAVA_REPORTS_BEGIN\\n'",
                 f"find . -path '{report_glob}' -type f -newer /tmp/railshot-java-start -exec sh -c " + shlex.quote(
                     "for f do test ! -L \"$f\"; printf 'RAILSHOT_JAVA_REPORT='; printf %s \"$f\" | base64 | tr -d '\\n'; "
                     "printf ':'; base64 < \"$f\" | tr -d '\\n'; printf '\\n'; done") + " sh {} +",
                 "printf '\\nRAILSHOT_JAVA_REPORTS_END\\n'"]
    return {"stack": "maven" if maven else "gradle", "image": "maven:3.9.9-eclipse-temurin-21" if maven else "gradle:8.14.3-jdk21",
            "commands": commands, "checks": ["quality-plugin", "compile", "unit"], "report_format": "java-canonical",
            "toolchain": {"source": "trusted-image-native-binary", "executable": executable, "version": version,
                          "uploaded_wrapper_executed": False, "distribution_checksum_verified": "at preparation"},
            "lock": "declared-distribution-checksum" if maven else "gradle-lock-and-verification",
            "limitation": "Approved JDK 21/native Maven 3.9.9 or Gradle 8.14.3 only; other declared toolchains block. Build plugins/tests remain untrusted code, not an independent proof of test correctness."}


def discover(ws, selected_root=None):
    from prepare import select_build_roots
    ws = Path(ws).resolve()
    try:
        roots = select_build_roots(ws, selected_root)
    except (ValueError, OSError, ET.ParseError) as exc:
        return [discovery_blocked(exc, ".")]
    if not roots:
        return [blocked("UNSUPPORTED: no supported codebase manifest", path=".")]
    if selected_root is None and len(roots) > 1:
        return [blocked("BUILD_ROOT_SELECTION_REQUIRED: choose an application build root", path=".",
                        candidates=[root.relative_to(ws).as_posix() for root in roots])]
    plans = []
    for root in roots:
        rel = root.relative_to(ws).as_posix()
        try:
            if (root / "package.json").exists():
                plan = npm_plan(root)
            elif (root / "pom.xml").exists() or any((root / p).exists() for p in ("build.gradle", "build.gradle.kts")):
                plan = java_plan(root)
            else:
                plan = python_plan(root)
            plans.append({"path": rel, **plan})
        except (ValueError, KeyError, TypeError, OSError, ET.ParseError) as exc:
            plans.append(discovery_blocked(exc, rel))
    return plans


def quality_script(commands, setup=""):
    """Only this parent shell owns stage status; child output/exit codes cannot select it."""
    script = ["railshot_stage_code=201", "railshot_completed=0", """railshot_finish() {
  railshot_rc=$?
  trap - 0
  if [ "$railshot_rc" -eq 0 ] && [ "$railshot_completed" -eq 1 ]; then exit 0; fi
  case "$railshot_rc" in
    5|126|127|12[89]|1[3-8][0-9]|19[0-2]) exit 200 ;;
    *) exit "$railshot_stage_code" ;;
  esac
}
trap railshot_finish 0
trap 'trap - 0; exit 200' HUP INT QUIT TERM
set -eu""", setup]
    for stage, command in commands:
        script += [f"railshot_stage_code={STAGE_CODES[stage]}", command]
    script.append("railshot_completed=1")
    return "\n".join(script)


def docker_command(ws, plan, name, network):
    source = str(ws.resolve())
    if "," in source:
        raise ValueError("workspace path contains unsupported mount delimiter")
    # run_quality supplies a readable application-only snapshot. Keep .git out
    # even for callers inspecting/running this command builder directly.
    setup = ("mkdir -p /tmp/home\n"
             "for source in /source/* /source/.[!.]* /source/..?*; do\n"
             "  [ -e \"$source\" ] || continue\n"
             "  [ \"$source\" = /source/.git ] && continue\n"
             "  cp -R -- \"$source\" /work/\n"
             "done\ncd " + shlex.quote("/work/" + plan["path"]))
    script = quality_script([(command_stage(command), command) for command in plan["commands"]], setup)
    return ["docker", "run", "--name", name, "--platform", "linux/amd64", "--network", network,
            "--user", "65532:65532", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--cpus=2", "--memory=2g", "--memory-swap=2g", "--pids-limit=256",
            "--tmpfs", "/work:rw,exec,nosuid,nodev,size=2147483648,mode=1777",
            "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=1073741824,mode=1777",
            "--mount", f"type=bind,src={source},dst=/source,readonly",
            "-e", "HOME=/tmp/home", "-e", "CI=true", "-e", "UV_CACHE_DIR=/tmp/uv-cache",
            "-e", "GRADLE_USER_HOME=/tmp/gradle", "-e", "MAVEN_USER_HOME=/tmp/maven",
            "-e", "MAVEN_OPTS=-Duser.home=/tmp/home", "--entrypoint=sh",
            plan["image"], "-c", script]


def command_stage(command):
    if re.search(r"(?:sh ./mvnw|sh ./gradlew|/usr/share/maven/bin/mvn|/opt/gradle/bin/gradle) --version\b", command):
        return "prepare"
    if "RAILSHOT_TESTS=" in command or "RAILSHOT_JUNIT_" in command or "RAILSHOT_JAVA_REPORT" in command or (command.startswith("find . -path") and "-delete" not in command):
        return "report"
    if re.search(r"(?:npm|pnpm|yarn) run lint\b|(?:/|exec )eslint ", command) or "ruff check" in command:
        return "lint"
    if re.search(r"(?:npm|pnpm|yarn) run (typecheck|type-check|check-types)\b|(?:/|exec )tsc\b|mypy ", command):
        return "type"
    if re.search(r"(?:npm|pnpm|yarn) run test\b", command) or "node --test " in command or "pytest " in command or "sh ./mvnw " in command or "sh ./gradlew " in command or command.startswith(("/usr/share/maven/bin/mvn ", "/opt/gradle/bin/gradle ")):
        return "unit"
    return "prepare"


def diagnostic_fingerprint(detail, stage, project_path):
    normalized = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", detail).replace("\r\n", "\n")
    normalized = re.sub(r"(?m)^RAILSHOT_STAGE=.*\n?", "", normalized)
    normalized = re.sub(r'"(startTime|endTime|duration)"\s*:\s*\d+(?:\.\d+)?', r'"\1":0', normalized)
    normalized = re.sub(r'\b(time|timestamp)="[^"]*"', r'\1="<time>"', normalized)
    normalized = re.sub(r":\d+(?::\d+)?(?=[:\s)]|$)", ":#", normalized)
    normalized = re.sub(r"\b\d+(?:\.\d+)?(?:ms|s)\b", "<duration>", normalized)
    normalized = " ".join(normalized.split())
    return hashlib.sha256(json.dumps([project_path, stage, normalized], separators=(",", ":")).encode()).hexdigest()


def java_failure_kind(text):
    """Require the actual failed native plugin/task, not a mentioned checker name."""
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    kinds = set()
    for plugin in re.findall(r"Failed to execute goal\s+\S*?((?:maven-[a-z]+|spotbugs-maven)-plugin):[^\s]+", text):
        if plugin in {"maven-checkstyle-plugin", "maven-pmd-plugin", "spotbugs-maven-plugin"}:
            kinds.add("lint")
        elif plugin == "maven-compiler-plugin":
            kinds.add("type")
        elif plugin in {"maven-surefire-plugin", "maven-failsafe-plugin"}:
            kinds.add("unit")
    tasks = re.findall(r"(?m)^> Task (:[\w:-]+) FAILED\s*$", text)
    tasks += re.findall(r"Execution failed for task '(?P<task>:[\w:-]+)'", text)
    for task in tasks:
        name = task.rsplit(":", 1)[-1]
        if re.fullmatch(r"(?:checkstyle|pmd|spotbugs)(?:Main|Test)", name):
            kinds.add("lint")
        elif name in {"compileJava", "compileTestJava"}:
            kinds.add("type")
        elif name == "test":
            kinds.add("unit")
    return next(iter(kinds)) if len(kinds) == 1 else None


def quality_failure(text, returncode, project_path=".", *, stack=None, repair_scope="packaging"):
    """Only observed checker failures can request an already-approved source repair."""
    text = re.sub(r"(?m)^RAILSHOT_PYTHON_[A-Z_]+=.*\n?", "", text)
    stage = next((name for name, code in STAGE_CODES.items() if code == returncode), None)
    if returncode != 0 and stage is None:
        return blocked("QUALITY_EXECUTION_BLOCKED: no trusted stage exit status")
    no_tests = re.search(r"NO_TESTS|no tests (found|ran|collected)|collected 0 items", text, re.I)
    if repair_scope == "source" and stage == "unit" and no_tests:
        return {"ok": False, "status": "FAIL", "source_repair_eligible": True, "check": "unit",
                "failure_signature": "Q:QUALITY:unit:" + diagnostic_fingerprint("NO_TESTS", "unit", project_path),
                "errors": ["NO_TESTS: add behavioral tests collected by the existing runner; preserve existing tests and scripts"]}
    if returncode == 0 or stage == "report" or no_tests:
        return blocked("NO_TESTS_OR_REPORT: positive executed test evidence required")
    if stage == "prepare":
        return blocked("QUALITY_PREPARATION_BLOCKED: dependency/toolchain/runtime failure")
    source_dependency = (repair_scope == "source" and stage in {"lint", "type", "unit"} and
                         re.search(r"Cannot find module|Cannot find package|ERR_MODULE_NOT_FOUND|ModuleNotFoundError|No module named", text))
    environment = re.search(r"ENOTFOUND|ECONNRESET|ETIMEDOUT|TLS handshake|(?:HTTP\S*|status(?: code)?|E)\s*[:=]?\s*429\b|429 Too Many Requests|Could not resolve|Could not transfer artifact|Could not find artifact|not found in.*repositor|Failed to download|Failed to validate Maven distribution SHA|Dependency verification failed|command not found|: not found|not installed|requires.*Python|UnsupportedClassVersion|secret.*(missing|required)|API[_ -]?KEY.*(missing|required|not set)", text, re.I)
    if environment or (not source_dependency and re.search(r"Cannot find module|Cannot find package|ERR_MODULE_NOT_FOUND|ModuleNotFoundError|No module named", text)):
        return blocked("QUALITY_DEPENDENCY_OR_ENV_BLOCKED: restore required tool, dependency, network or secret")
    if stack in {"maven", "gradle"} and stage == "unit":
        stage = java_failure_kind(text)
        if stage is None:
            return blocked("QUALITY_DIAGNOSTIC_UNRESOLVED: no unique failed Java checker/task")
    # Keep output untrusted and mask common credential forms before fixer context.
    detail = re.sub(r"gh[pousr]_[A-Za-z0-9]+|AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{20,}|(?i:password|token|secret|api[_-]?key)\s*[=:]\s*\S+", "[REDACTED]", text[-4000:])
    return {"ok": False, "status": "FAIL", "source_repair_eligible": stage in {"lint", "type", "unit"},
            "check": stage, "failure_signature": f"Q:QUALITY:{stage}:" + diagnostic_fingerprint(detail, stage, project_path),
            "errors": ["QUALITY_CHECK_FAILED: " + stage + "\n" + detail]}


def java_test_count(text, stack):
    """Parse only fresh canonical files collected after the native checker completed."""
    if (text.count("RAILSHOT_JAVA_REPORTS_BEGIN") != 1 or text.count("RAILSHOT_JAVA_REPORTS_END") != 1):
        return 0
    chunks = re.findall(r"^RAILSHOT_JAVA_REPORTS_BEGIN\n(.*?)\nRAILSHOT_JAVA_REPORTS_END$", text, re.M | re.S)
    if len(chunks) != 1 or len(chunks[0].encode()) > 4 * 1024 * 1024:
        return 0
    rows = [row for row in chunks[0].splitlines() if row]
    if not rows or len(rows) > 1000:
        return 0
    total, seen = 0, set()
    canonical = r"(?:.*/)?target/surefire-reports/TEST-[^/]+\.xml" if stack == "maven" else r"(?:.*/)?build/test-results/test/TEST-[^/]+\.xml"
    try:
        for row in rows:
            if not row.startswith("RAILSHOT_JAVA_REPORT="):
                return 0
            path_data, xml_data = row.removeprefix("RAILSHOT_JAVA_REPORT=").split(":", 1)
            name = base64.b64decode(path_data, validate=True).decode()
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or not re.fullmatch(canonical, path.as_posix()) or path.as_posix() in seen:
                return 0
            seen.add(path.as_posix())
            data = base64.b64decode(xml_data, validate=True)
            if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
                return 0
            suite = ET.fromstring(data)
            if suite.tag != "testsuite" or any(node.tag in {"failure", "error", "skipped"} for node in suite.iter()):
                return 0
            if any(int(suite.get(key, "0")) != 0 for key in ("failures", "errors", "skipped", "disabled")):
                return 0
            cases = suite.findall("testcase")
            if int(suite.get("tests", "0")) != len(cases) or not cases:
                return 0
            if any(not case.get("name") or not case.get("classname") for case in cases):
                return 0
            total += len(cases)
    except (ValueError, UnicodeError, binascii.Error, ET.ParseError):
        return 0
    return total


def test_count(text, stack, report_format=None):
    if stack in {"maven", "gradle"}:
        return java_test_count(text, stack)
    if report_format == "junit":
        chunks = re.findall(r"^RAILSHOT_JUNIT_BEGIN\n(.*?)\nRAILSHOT_JUNIT_END$", text, re.S | re.M)
        if len(chunks) != 1 or "<!DOCTYPE" in chunks[0]:
            return 0
        try:
            root = ET.fromstring(chunks[0])
            if root.tag not in {"testsuites", "testsuite"}:
                return 0
            if any(element.tag in {"failure", "error", "skipped"} or
                   any(int(element.get(key, "0")) != 0 for key in ("failures", "errors", "skipped", "disabled"))
                   for element in root.iter()):
                return 0
            # Node also reports an empty test file as a passing file testcase.
            # Require explicit node:test cases; filename-only entries are not tests.
            return sum(1 for case in root.iter("testcase") if case.get("name") and
                       not re.search(r"\.[cm]?js$", case.get("name", "")))
        except (ET.ParseError, ValueError):
            return 0
    values = re.findall(r"^RAILSHOT_TESTS=(\d+)$", text, re.M)
    return int(values[-1]) if values else 0


def zero_node_report(text):
    """Valid native zero collection differs from absent, failed or corrupt evidence."""
    chunks = re.findall(r"^RAILSHOT_JUNIT_BEGIN\n(.*?)\nRAILSHOT_JUNIT_END$", text, re.S | re.M)
    if len(chunks) != 1 or "<!DOCTYPE" in chunks[0] or len(chunks[0].encode()) > 4 * 1024 * 1024:
        return False
    try:
        root = ET.fromstring(chunks[0])
        if root.tag not in {"testsuite", "testsuites"} or any(
                item.tag in {"failure", "error", "skipped"} or any(int(item.get(key, "0")) != 0 for key in
                ("failures", "errors", "skipped", "disabled")) for item in root.iter()):
            return False
        cases = list(root.iter("testcase"))
        if cases:
            return all(re.search(r"\.[cm]?js$", case.get("name", "")) for case in cases)
        suites = [item for item in root.iter() if item.tag in {"testsuite", "testsuites"} and "tests" in item.attrib]
        return bool(suites) and all(item.get("tests") == "0" for item in suites)
    except (ET.ParseError, ValueError):
        return False


def save_python_artifacts(text, run, index, *, required):
    rows = re.findall(r"^RAILSHOT_PYTHON_ARTIFACTS=(.*)$", text, re.M)
    if not rows and not required:
        return None
    if len(rows) != 1 or len(rows[0].encode()) > 1024 * 1024:
        raise ValueError("Python resolution evidence missing or oversized")
    files = json.loads(rows[0])
    expected = {"app-requirements.txt", "overlay.in", "provenance.json", "resolved-environment.json"}
    if not isinstance(files, dict) or not expected <= files.keys() <= expected | {"requirements.lock"}:
        raise ValueError("Invalid resolution artifact filenames")
    if not all(isinstance(value, str) for value in files.values()):
        raise ValueError("Invalid resolution artifact content")
    provenance = json.loads(files["provenance.json"])
    if not isinstance(provenance, dict) or not isinstance(provenance.get("artifacts"), dict):
        raise ValueError("Invalid resolution provenance")
    hashes = {name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()}
    if any(provenance.get("artifacts", {}).get(name) != digest for name, digest in hashes.items() if name != "provenance.json"):
        raise ValueError("Resolution artifact hash mismatch")
    directory = run / f"python-{index}"
    directory.mkdir(mode=0o700)
    for name, content in files.items():
        target = directory / name
        target.write_text(content)
        target.chmod(0o600)
    return {"directory": str(directory), "sha256": hashes}


def run_quality(ws, run, *, network=None, timeout=900, selected_root=None, repair_scope="packaging"):
    plans = discover(ws, selected_root=selected_root)
    try:
        run.mkdir(parents=True, exist_ok=True)
        (run / "quality-plan.json").write_text(json.dumps(plans, indent=2))
    except OSError as exc:
        return blocked("QUALITY_EVIDENCE_WRITE_FAILED", error=OperationError(
            "OBSERVATION_WRITE_FAILED", component="gate", phase="Q.plan-write", outcome="UNKNOWN",
            retry_policy="after_reconcile", side_effect="possible", cause=exc))
    if any(p.get("blocked") for p in plans):
        first = next(p for p in plans if p.get("blocked"))
        repairable = {"NO_TESTS", "MISSING_CHECKER", "UNSUPPORTED_TEST_REPORTER"}
        candidates = all(p.get("blocked") in repairable for p in plans if p.get("blocked"))
        for plan in plans:
            if plan.get("blocked") == "UNSUPPORTED_TEST_REPORTER":
                manifest = ws / plan["path"] / "package.json"
                script = json.loads(manifest.read_text()).get("scripts", {}).get("test") if manifest.is_file() else None
                candidates &= script in {None, '', 'echo "Error: no test specified" && exit 1', "echo 'Error: no test specified' && exit 1"}
        if repair_scope == "source" and candidates:
            reasons = [p["path"] + ": " + p["blocked"] for p in plans if p.get("blocked")]
            detail = "; ".join(reasons)
            return {"ok": False, "status": "FAIL", "source_repair_eligible": True, "check": "discovery",
                    "failure_signature": "Q:QUALITY:discovery:" + diagnostic_fingerprint(detail, "discovery", "."),
                    "errors": [detail], "projects": plans,
                    "error": OperationError("GATE_CHECK_FAILED", component="gate", phase="Q.discovery", outcome="FAIL").as_dict()}
        return blocked("; ".join(p["blocked"] for p in plans if p.get("blocked")), error=first.get("error"), projects=plans)
    if not network or network in {"host", "bridge", "none"} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", network):
        return blocked("DEPENDENCY_NETWORK_UNCONFIGURED: register a credential-free egress-filtered Docker network", projects=plans)
    results = []
    for plan in plans:
        name = "railshot-quality-" + uuid.uuid4().hex[:16]
        pending, primary_cause, launched, phase = None, None, False, "Q.snapshot"
        staging = None
        try:
            # TMPDIR is the runner's existing same-path host mount, checked by
            # container_preflight; no wider host mount is needed for siblings.
            staging = tempfile.TemporaryDirectory(prefix="railshot-quality-")
            source = stage_source(ws, Path(staging.name) / "source")
            phase = "Q.command"
            command = docker_command(source, plan, name, network)
            # Do not buffer unlimited untrusted output in Python memory.
            with tempfile.TemporaryFile() as log:
                launched = True
                proc = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                      timeout=timeout, preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_FSIZE, (8 * 1024 * 1024, 8 * 1024 * 1024)))
                phase = "Q.output"
                size = log.tell()
                log.seek(max(0, size - 4 * 1024 * 1024))
                text = log.read().decode(errors="replace")
            phase = "Q.evidence-write"
            # Private diagnostic evidence; workflow uploads only verdict/quality JSON.
            log_path = run / f"quality-{len(results)}.log"
            log_path.write_text(text)
            log_path.chmod(0o600)
            phase = "Q.report"
            evidence = save_python_artifacts(text, run, len(results), required=proc.returncode == 0) if plan.get("provenance") else None
            count = test_count(text, plan["stack"], plan.get("report_format")) if not proc.returncode else 0
            if proc.returncode not in {0, *STAGE_CODES.values()}:
                error = OperationError("GATE_EXECUTION_FAILED", component="gate", phase="Q.command",
                                       retry_policy="after_configuration", side_effect="possible",
                                       cause=subprocess.CalledProcessError(proc.returncode, []))
                pending = blocked("QUALITY_EXECUTION_BLOCKED: runtime/tool/image unavailable or output limit", error=error, projects=results)
            else:
                status = "PASS" if proc.returncode == 0 and count > 0 else "FAIL"
                reason = None if status == "PASS" else "NO_TESTS" if proc.returncode == 0 else "QUALITY_COMMAND_FAILED"
                failure = quality_failure(text, proc.returncode, plan["path"], stack=plan["stack"], repair_scope=repair_scope) if status != "PASS" else None
                if (status != "PASS" and proc.returncode == 0 and repair_scope == "source" and
                        plan.get("report_format") == "junit" and zero_node_report(text)):
                    failure = quality_failure("NO_TESTS", 204, plan["path"], repair_scope="source")
                sig = failure.get("failure_signature", "").rsplit(":", 1)[-1] if failure else ""
                sig = sig or hashlib.sha256((plan["path"] + ":" + (reason or "PASS")).encode()).hexdigest()
                results.append({"path": plan["path"], "status": failure.get("status", status) if failure else status,
                                "reason": reason, "tests": count, "exit_code": proc.returncode,
                                "fingerprint": sig, "resolution_evidence": evidence})
                if status != "PASS":
                    if failure.get("blocked"):
                        check = next((key for key, value in STAGE_CODES.items() if value == proc.returncode), "report")
                        failure["error"] = OperationError("GATE_ENVIRONMENT_UNAVAILABLE", component="gate", phase="Q." + check,
                                                           retry_policy="after_configuration", side_effect="possible",
                                                           cause=subprocess.CalledProcessError(proc.returncode, [])).as_dict()
                    else:
                        failure["error"] = OperationError("GATE_CHECK_FAILED", component="gate", phase="Q." + failure["check"],
                                                           outcome="FAIL", side_effect="possible",
                                                           cause=subprocess.CalledProcessError(proc.returncode, [])).as_dict()
                    pending = {**failure, "classification_source": "heuristic", "projects": results}
        except Exception as exc:
            primary_cause = exc
            if isinstance(exc, OperationError):
                error = exc
            elif phase == "Q.snapshot":
                error = OperationError("GATE_CONFIG_INVALID" if isinstance(exc, ValueError) else "GATE_ENVIRONMENT_UNAVAILABLE",
                                       component="gate", phase=phase, retry_policy="after_configuration", cause=exc)
            elif isinstance(exc, subprocess.TimeoutExpired):
                error = OperationError("GATE_EXECUTION_FAILED", component="gate", phase=phase, outcome="UNKNOWN",
                                       retry_policy="after_reconcile", side_effect="unknown", cause=exc)
            elif isinstance(exc, OSError) and phase == "Q.command":
                launched = False  # subprocess could not start; no container was requested.
                error = OperationError("GATE_ENVIRONMENT_UNAVAILABLE", component="gate", phase=phase,
                                       retry_policy="after_configuration", cause=exc)
            elif phase == "Q.evidence-write" or isinstance(exc, OSError):
                error = OperationError("OBSERVATION_WRITE_FAILED", component="gate", phase=phase, outcome="UNKNOWN",
                                       retry_policy="after_reconcile", side_effect="possible", cause=exc)
            elif isinstance(exc, (ValueError, ET.ParseError)):
                error = OperationError("GATE_EVIDENCE_MISMATCH" if launched else "GATE_CONFIG_INVALID",
                                       component="gate", phase=phase, retry_policy="after_reconcile" if launched else "after_configuration",
                                       side_effect="possible" if launched else "none", cause=exc)
            else:
                error = OperationError("INTERNAL_ERROR", component="gate", phase=phase, outcome="UNKNOWN",
                                       retry_policy="after_reconcile", side_effect="unknown" if launched else "none", cause=exc)
            pending = blocked("QUALITY_EXECUTION_BLOCKED", error=error, projects=results)
        finally:
            if launched:
                try:
                    cleanup = subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
                    if cleanup.returncode:
                        raise subprocess.CalledProcessError(cleanup.returncode, [])
                except (OSError, subprocess.SubprocessError) as exc:
                    # Keep both causal types if cleanup fails after a timed out run.
                    if primary_cause is not None:
                        exc.__cause__ = primary_cause
                    error = OperationError("GATE_EXECUTION_FAILED", component="gate", phase="Q.cleanup", outcome="UNKNOWN",
                                           retry_policy="after_reconcile", side_effect="unknown", cause=exc)
                    pending = blocked("QUALITY_CLEANUP_FAILED: discard the executor before reuse", error=error, projects=results)
            if staging is not None:
                try:
                    staging.cleanup()
                except OSError as exc:
                    error = OperationError("GATE_EXECUTION_FAILED", component="gate", phase="Q.snapshot-cleanup", outcome="UNKNOWN",
                                           retry_policy="after_reconcile", side_effect="possible", cause=exc)
                    pending = blocked("QUALITY_CLEANUP_FAILED: discard the executor before reuse", error=error, projects=results)
        if pending is not None:
            return pending
    return {"ok": True, "status": "PASS", "projects": results}
