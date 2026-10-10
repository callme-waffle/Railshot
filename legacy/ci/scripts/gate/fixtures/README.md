# Real CI fixtures and evidence runner

These are six small applications with real external dependencies and real tests.
They exercise the current intake → L0 → L1 → Q → L2 → L4 → L3 implementation.
The source fixtures intentionally contain **no invented package lock, wrapper
binary, or distribution checksum**. Generate those with the native tools before
running the gate. Generation is preparation evidence, not an application pass.

| Source | Runtime / package tool | Actual quality tools and unit boundary |
| --- | --- | --- |
| `npm-js` | Node 22.23.3 / npm 12.2.0 | ESLint 10.11.0; Vitest 5.0.1 + Supertest against Express `/health`; static types not applicable |
| `npm-ts` | Node 22.23.3 / npm 12.2.0 | ESLint + typescript-eslint 8.70.1; TypeScript 5.9.3; Vitest + Supertest |
| `nextjs` | Node 22.23.3 / npm 12.2.0; Next 15.5.26 | ESLint; TypeScript; Vitest calls the real App Router health handler; full gate builds/starts Next |
| `fastapi` | Python 3.12.14 / uv 0.12.18 | Ruff 0.16.8; mypy 2.3.1; pytest 9.1.1 + httpx TestClient against FastAPI |
| `maven-spring` | JDK 21 / Maven 3.9.9; Spring Boot 3.5.16 | Maven Checkstyle 3.6.0 `check`, Java compilation, Spring MockMvc/JUnit test through `verify` |
| `gradle-spring` | JDK 21 / Gradle 8.14.3; Spring Boot 3.5.16 | Checkstyle 10.26.1, Java compilation, Spring MockMvc/JUnit test through `check`; native dependency locks and verification metadata |

Java Q executes the approved image's native Maven/Gradle binary, never the uploaded
`mvnw`/`gradlew` script. The checked-in wrapper declaration must match the approved
distribution URL/version and its SHA-256 is verified during preparation. A different
declared version blocks instead of silently changing toolchains. `clean verify` /
`clean check --rerun-tasks` removes stale compiled outputs; the Q collector deletes
old canonical XML reports before execution and accepts only newly written Surefire
or Gradle test reports with actual, successful testcase elements. Arbitrary XML in
stdout or a `tests` count without testcase elements is not execution evidence.
This does not prove that an uploaded build plugin or test is honest or sufficient.

Version selection is an explicit fixture profile, not a claim that every runtime
or newest release is supported. Maven wrapper checksum pins the Maven download;
Maven still has no npm-style transitive lock. Image tags are resolved at execution
and their actual image IDs/digests are recorded. Java's `eclipse-temurin:21-jre`
runtime tag remains mutable. Package vulnerability failures must remain failures.

On 2026-10-01 the npm 10.9.2 and bundled 10.9.9 profiles both failed native lock
generation in Arborist's peer-set resolver (`null.edgesOut`). The same JS source
generated a real lock with npm 12.2.0 in the isolated CI environment. These three
fixtures now explicitly pin 12.2.0 in generation, packageManager and Docker build;
this is a fixture change, not an automatic upgrade policy for uploaded apps.
The actual registry metadata requires Node `^22.22.2 || ^24.15.0 || >=26.0.0`,
which the selected 22.23.3 image meets. Full gate evidence is still separate.

## Generate with actual package managers

Run on the credential-free disposable CI machine with Docker and the
administrator's already configured egress-filtered network. A network name alone
does not install an egress policy. The generator never creates cloud resources.
Use a **new output directory on every attempt**, preserving failed attempts.

```sh
python3 ci/scripts/gate/fixtures/generate.py \
  --output /tmp/railshot-fixtures-r2 \
  --network railshot-quality
```

For a short first batch add `--stacks npm-js,npm-ts,fastapi`. For the second batch
use another new output and `--stacks nextjs,maven-spring,gradle-spring`.
Optional `--stacks pnpm-js,yarn-js` reuses the same Express source with exact
pnpm 10.17.1 / Yarn 4.10.3 (Corepack 0.34.0), native lock generation and a matching
Dockerfile. Yarn uses its supported `node-modules` linker. These aliases can also
be passed to `e2e.py`; they are not run by default and add no duplicated app code.

The generator mounts only its new fixture copy writable, with 2 CPUs, 2 GiB RAM,
256 PIDs, a read-only container root and executable temporary tool storage. It
uses native `npm install --package-lock-only`, `uv lock`, Maven Wrapper plugin,
and Gradle wrapper / `--write-locks --write-verification-metadata sha256 check`.
It records each actual Docker command, tool stdout/version lines, return code,
image ID/digests and generated-file SHA-256s in sibling `*-generation.json` and
`*-generation.log` files. Node/npm/uv versions are checked before generation.
Generated build outputs are removed from the copied upload after success.

## Run the existing gate

The runner needs the same Python dependencies as `gate.py` (PyYAML, jsonschema,
Python 3.13+). Registry access, Docker and the gate's existing Trivy prerequisite
must be available on the executor. No host npm, Python application, Maven or
Gradle command is executed. Tests remain inside the product quality container.

```sh
python3 ci/scripts/gate/e2e.py \
  --fixtures /tmp/railshot-fixtures-r2 \
  --output /tmp/railshot-e2e-quality-r2 \
  --stacks npm-js,npm-ts,fastapi \
  --cases good,lint,type,unit,missing-env,missing-tool,missing-lock,no-tests \
  --mode quality --network railshot-quality

python3 ci/scripts/gate/e2e.py \
  --fixtures /tmp/railshot-fixtures-r2 \
  --output /tmp/railshot-e2e-full-r2 \
  --stacks npm-js,npm-ts,fastapi --cases good \
  --mode full --network railshot-quality
```

Every case is copied fresh and changed **before intake establishes its imported
baseline**. `lint`, `type`, `unit` and `missing-env` change application source,
preserving existing checker configuration and test assertions. `missing-tool`
selects a nonexistent npm checker command or removes a Java wrapper.
`missing-lock` removes native lock evidence (only the distribution checksum for
Maven); `no-tests` removes test files to verify rejection. These deliberate test
inputs are not a permission for the product fixer to change policy or tests.

Use `--stacks npm-js --cases private-source --mode full` to reproduce the
ephemeral runner's `umask 077`: the upload/intake retain directories `0700` and
files `0600` (executables `0700`). Q and L2 use the same validated source snapshot
helper, excluding `.git`, beneath a private temporary directory. Snapshot
directories and executable files are `0755`; other files are `0644`. Q mounts its
snapshot read-only for UID `65532`; L2 supplies the snapshot to the existing
restricted BuildKit builder so Docker COPY retains readable application files.
L2 also physically removes `.env*` from its snapshot regardless of ignore-file
negations. Original source, credentials and run records keep their private modes.
The full case must pass Q, build, scan and non-root runtime health while preserving
the original source digest, including its modes. `--mode quality` runs Q alone and
does not establish that the built application's runtime can read its source.

JavaScript's `type` case and Python's `missing-tool` case are explicitly
`NOT_APPLICABLE`; the latter needs a separately native-generated Python lock
without the required checker. They are never reported as executed passes.
Missing checkers automatically provided by newer product profiles need their own
positive fixture and native lock generation; this matrix does not claim that
coverage yet. pnpm/Yarn alias runs must be verified separately. Monorepo/subdirectory selection, source-fixer SDK repair,
SAST beyond the configured linters, deployment/Argo and external services are
also outside this six-source fixture matrix.

`summary.json` and each case's `result.json` distinguish:

- `quality_status`: the observed Q result, including its test count/fingerprint.
- `full_gate_status`: `NOT_RUN` for quality-only runs; actual verdict in full mode.
- `release_eligible`: true only if the existing complete gate says so.
- `assertion: MATCH`: this case produced its expected behavior. An expected
  failure matching does **not** mean that the application passed or can release.

The runner preserves actual invocation/log paths, native generation evidence,
the selected quality command plan, actual quality image IDs, and the whole gate
verdict. Missing-env matching also requires the trusted unit-stage exit code and
the expected missing-key diagnostic in the private quality log. Missing-command
matching requires the exact nonexistent tool name, so an unrelated Docker or
network failure cannot satisfy that negative case. Log paths and SHA-256s are
recorded without copying private log contents into JSON. An executor timeout
requires cleanup verification before reuse. Run this on the dedicated disposable
executor, not a shared machine with unrelated untrusted jobs.

Java lint/type/unit negatives additionally require the corresponding failed native
Maven plugin or Gradle task in the diagnostic and the same Q checker classification.
An unrelated unit failure cannot satisfy a lint or type fixture. The parser and
local collector regressions are separate from a fresh Docker E2E run.

## What is verified

Local checks: six fixture L1 baselines, 36 source-variant transformations,
generator shell syntax, Python syntax/CLI help. These are **not** container E2E
results. Actual generation/gate evidence is produced only under the selected
external output directory. Read its current receipts before claiming success;
no static fixture README can certify a later remote run.

Primary references:

- [npm ci](https://docs.npmjs.com/cli/v10/commands/npm-ci): lock/manifest mismatch must fail rather than update the lock.
- [uv sync / lock semantics](https://docs.astral.sh/uv/concepts/projects/sync/): `--locked` checks freshness.
- [Vitest guide](https://vitest.dev/guide/) and [FastAPI testing](https://fastapi.tiangolo.com/tutorial/testing/): real unit reporters/TestClient.
- [Maven Wrapper](https://maven.apache.org/tools/wrapper/) and [Maven lifecycle](https://maven.apache.org/guides/introduction/introduction-to-the-lifecycle.html): native wrapper and `verify`.
- [Gradle Wrapper](https://docs.gradle.org/current/userguide/gradle_wrapper.html), [dependency locking](https://docs.gradle.org/current/userguide/dependency_locking.html), [dependency verification](https://docs.gradle.org/current/userguide/dependency_verification.html): generate real wrapper/checksums and resolved metadata.
- [Spring Boot 3.5 system requirements](https://docs.spring.io/spring-boot/3.5/system-requirements.html): compatible Java/Maven/Gradle versions.

### JVM execution corrections observed on the CI executor

The initial real Java run exposed preparation failures, then an actual image
vulnerability failure. Preserve those attempts; do not disable their checks.

- Maven now generates the native **bin** wrapper and wrapper JAR. The prior
  only-script wrapper changed its ZIP URL to TAR when `unzip` was absent, then
  compared the TAR download with the recorded ZIP checksum. The bin wrapper
  verifies and expands the ZIP with Java. Its `--version` must succeed during
  trusted fixture generation. Q now uses the image's native executable and verifies
  the declared distribution checksum without executing the uploaded wrapper.
- Clear the official Maven image's `MAVEN_CONFIG` cache-directory environment
  setting before the bin wrapper runs: the wrapper otherwise treats `/root/.m2`
  as a Maven CLI lifecycle argument. The fixture Dockerfile applies this
  normalization; Q clears both `MAVEN_CONFIG` and `MAVEN_ARGS` before native Maven.
- Gradle verification metadata is generated with a fresh user home and refreshed
  dependencies, then checked in another fresh user home with strict verification.
  This prevents the initial wrapper-generation cache from omitting plugin
  classpath `.module` checksums. The gate uses the same pinned Gradle 8.14.3/JDK21
  image, verifies the wrapper's declaration/checksum, and uses the checked-in
  dependency locks and verification metadata with the native Gradle executable.
- The trusted Gradle init script requests full test exception causes. The default
  lifecycle log printed only exception classes/lines, so a real `API_KEY required`
  failure initially looked like a source-test failure. Full causes let the
  existing environment classifier block the run without weakening or skipping
  tests. [Gradle TestLogging](https://docs.gradle.org/current/dsl/org.gradle.api.tasks.testing.logging.TestLogging.html)
- Both Spring fixtures explicitly select **Tomcat 10.1.60**. The real r4 Gradle
  image contained Boot's default Tomcat 10.1.55 and Trivy rejected three CRITICAL
  findings. Apache documents that 10.1.58 did not pass its release vote, so the
  scanner's suggested version number must not be assumed to be downloadable.
  The selected 10.1.60 exists in Maven Central and includes subsequent published
  security fixes. [Apache Tomcat security notes](https://tomcat.apache.org/security-10.html)

These corrections do not themselves assert full-gate success. The corresponding
fresh-generation and complete-gate receipts remain the authority. JVM local
regressions in `test_java_profiles.py` additionally check that wrapper preparation
and dependency-integrity failures cannot request application source repair.
