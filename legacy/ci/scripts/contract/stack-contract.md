# Railshot Stack Contract v0

This file describes the current CI workload admission contract. Agents propose changes and deterministic gates decide CI success. It does not select or install a customer runtime, ingress, database operator or CD controller.

## 1. What an agent may produce

| File | Purpose |
|---|---|
| `Dockerfile`, `<service>.Dockerfile` | Container build for a service |
| `.dockerignore` | Keeps secrets, VCS data and build junk out of the context |
| `.railshot/railshot.yaml` | Workload spec (`schemas/railshot.schema.json`) |

New specs use `apiVersion: railshot/v0`. Existing `.jasmin/jasmin.yaml` and `jasmin/v0` remain readable and may be repaired in place. A workspace must contain exactly one spec. New bundles use `railshot.yaml`; readers also verify historical `jasmin.yaml` bundles without rewriting their bytes or hashes.

Direct CLI calls default to packaging-only repair. The customer workflow allows source repair only after an observed L2 build or L3 runtime failure; initial adaptation uses packaging scope. Missing tests/checkers and completed Q quality failures are advisory, so do not add tests, checker setup or unrelated source changes for deployment. Existing tests, checker policy, dependency versions, migrations and generated files remain protected; only the native isolated package manager can generate locks. `paths.yaml` and the runner's content checks are the exact authority. Agents do not produce Kubernetes manifests, Terraform values, DNS records or certificates; those belong to separate target owners.

## 2. Container rules

| ID | Rule | Gate layer |
|---|---|---|
| C1 | Builds with `docker buildx build --platform linux/amd64` from the declared context. | L2 build |
| C2 | Base images come from the allowlist in §5. The final stage is a slim, distroless or unprivileged variant. L2 records built image IDs for scan, runtime and artifact release; automatic rewriting of FROM tags to digests is not implemented. | L1 static / L2 evidence |
| C3 | The final stage sets `USER` to a numeric non-root UID of 10000 or higher (default 65532). | L1, L4 |
| C4 | The process listens on `0.0.0.0` and on the port declared in `railshot.yaml` (1024–65535). | L3 readiness |
| C5 | The declared health path answers HTTP 2xx or 3xx within 60 s of start, without auth and without side effects. The platform HTTP probe decides; a Dockerfile `HEALTHCHECK` is allowed but does not replace this probe. | L3 readiness |
| C6 | No secrets in the image, build args or context: no `.env*`, keys, tokens or credential files. `.dockerignore` excludes `.git`, `.env*`, `node_modules`, caches and build outputs. | L0, L4 |
| C7 | No CRITICAL vulnerability that has a fixed version. Fix it by moving to a newer base image or package version; ignore files are forbidden. | L4 conformance |
| C8 | Image size is at most 1 GiB compressed. | L4 |
| C9 | Build-time data generation (building a database from JSON, exporting frontend data, compiling assets) runs in a build stage; results are copied into the final stage read-only. | L2 |
| C10 | One main process per container. No `sshd`, no process supervisor unless the runtime requires it. | L1 |
| C11 | Each declared route answers from the running service, not only from a static placeholder. | L3 behavior |

## 3. Workload spec rules

- One entry in `services` per independently running process. A static frontend is its own service (served by an unprivileged web server image) unless another service already serves it.
- At least one service has a `route`. Service names must be unique. Target routing is configured separately.
- `env` holds non-secret values only. Secret names go in `secrets`; runtime injection is a separate target contract. The CI currently blocks required external secrets.
- Do not infer deployment resources from the CI limits in §4. The spec records facts about the app and explicit user choices, nothing else.
- `resources` may only request what `catalog.yaml` offers.
- A single-service, single-replica application may declare `storage: {mountPath: /var/opt/memos, sizeGi: 1}`. One namespace-owned PVC uses the operator-installed `railshot-persistent` StorageClass. The path is restricted to `/data`, `/var/lib/<name>` or `/var/opt/<name>` and their safe subdirectories. Persistent workloads use Recreate to avoid concurrent SQLite writers. Database migration Jobs are not combined with local application storage.
- L3 provides temporary isolated writable storage at that same path. CI does not claim durable storage success; the target must bind the real PVC and verify runtime readiness. Stop and update preserve it; explicit permanent application deletion includes its data.

## 4. CI limits and target boundary

L1 validates schema, unique service names and reserved PORT/database bindings. Static replicas remain limited to 1–3 and size to S/M/L. Autoscaling requests are rejected as outside the current CI support; there is no controller selection flag or deployment resource generation.

L3 runs the tested image in an isolated ephemeral container environment. When requested, it starts temporary PostgreSQL 17, uses owner permissions for migration and a restricted runtime role for the app, and checks the declared health/routes. This does not choose a production DB operator, order a real rollout, verify backups or recover customer data.

A complete gate verdict binds source/spec and tested image identities. The trusted publisher exports or publishes those same images and returns remote digests. The current workflow hands the original spec, verdict and bundle manifest plus `images.json` to CD. It does not assign domains, issue certificates, configure Gateway/ALB, install a cluster or claim deployment success. See [registry contract](registry.md).

An agent's `give_up` is advisory: a fresh complete passing gate verdict still permits publication. A completed proposal rejected before any writes also receives a final gate evaluation after its repair allowance is exhausted. The rejected proposal and its error remain in the attempt receipt. Partial checks, unresolved required capabilities, missing workload specs, uncertain SDK outcomes and partial writes never establish release eligibility.

## 5. Base image allowlist

| Use | Images |
|---|---|
| Python | `python:3.12-slim`, `python:3.13-slim` |
| Node.js | `node:22-slim`, `node:24-slim` |
| Go, Rust (build stage) | `golang:1`, `rust:1` |
| Final stage, static binaries | `gcr.io/distroless/static-debian12`, `gcr.io/distroless/base-debian12` |
| Static web | `nginxinc/nginx-unprivileged:stable-alpine` |
| JVM | `eclipse-temurin:21-jre` runtime; reviewed Maven/Gradle builder profiles in `catalog.yaml` |

## 6. Forbidden changes

- Editing, renaming or deleting existing tests, CI configuration, policies, this contract, or agent instruction files. New behavioral tests and additive package setup require trusted source scope; lockfiles are produced only by the isolated native resolver, never by the model. Existing real test scripts and dependency versions cannot change.
- Weakening a check: `|| true`, `exit 0` in commands, skipped tests, `--no-verify`, `.trivyignore`, lower scan severity, a health path that stays green while the app is down.
- Changing application source code without trusted source-repair scope. Scope cannot be granted by uploaded code or the model. Each proposal must plan all gates before the runner writes files; complete deterministic gates decide release eligibility.
- Fetching and executing remote scripts during the build (`curl … | sh`).
