# Historical source reference

This directory preserves source from the former remote `main` commit
`645e28aefa7f3d3996c57ea6853c6e2f67c5bcc4`. It is a reference archive. Current
implementation, builds, tests, and deployment continue to use the existing
repository paths outside `legacy/`, such as `apps/`, `ci/`, `deployment/`,
`infrastructure/`, `gitops/`, `observability/`, `contracts/`, and `examples/`.

Do not run or maintain the archived implementation. Historical documentation and
commands apply to the original checkout. Consult the active root README and
repository instructions for current work. The archive may be removed after its
migration and comparison value has ended, following an explicit team decision.

## Scope

| Archived paths | Purpose |
| --- | --- |
| `apps/` | Application source, tests, manifests, and dependency locks |
| `ci/`, `contracts/` | CI source, tests, fixtures, schemas, and contracts |
| `deployment/` | Deployment source, tests, fixtures, templates, and pinned versions |
| `infrastructure/` | Provider, Ansible, and Terraform source, tests, and locks |
| `gitops/`, `observability/` | GitOps and monitoring source, configuration examples, and tests |
| `examples/` | Versioned application examples and their build configuration |
| `.github/workflows/` | Historical workflow definitions; this nested directory cannot activate repository workflows |
| `package.json`, `package-lock.json` | Original root workspace and dependency configuration |
| `.dockerignore`, `.gitattributes`, `.gitignore` | Original versioned source configuration |

The snapshot contains **832 original Git blobs**, including **30 executable
files**. Original content and Git modes are preserved. The baseline has no
symbolic links or submodules. `SNAPSHOT.json` records the original commit, tree,
relative path, mode, and blob ID for every archived source file.

Excluded baseline material comprises `docs/` (99 files),
`deployment/scripts/tests/results/` (568 generated result and log files),
`.mcp.json`, `AGENT.md`, `README.md`, and `README.ja.md` (4 root reference files).
The original root READMEs and instructions remain accessible through the source
commit. This README, `AGENTS.md`, and `SNAPSHOT.json` are new archive metadata.

Only files selected from the immutable commit tree are included. Local worktree
files, `.git`, `.aws`, actual `.env` files, dependency installations, private
runtime credentials, and temporary outputs are excluded. The versioned
`deployment/.env.example` is retained as a configuration example: its token is
empty and its API token setting refers to a file path, not credential content.
Versioned credential implementation source and placeholder templates are
retained; they contain no copied local credential files.

## Isolation

The root `.dockerignore` excludes `/legacy/` from Docker contexts. Current
Dockerfiles and release packaging select active paths explicitly. The root npm
workspace pattern is `apps/*`, so this archive is not a workspace and does not
participate in the current dashboard build or dependency installation.

The root `.ignore` excludes this archive from normal `rg` searches. Use
`rg --no-ignore legacy/` deliberately when researching historical source. The
root `pytest.ini` adds `--ignore=legacy` to repository collection without changing
pytest's default directory exclusions. An explicitly targeted archived pytest
file or `--override-ini addopts=` can bypass this guard and is not a supported
current test command.

Current Node tests run within active package paths, for example:

```sh
npm test --workspace @railshot/agent
npm test --workspace railshot-api
npm test --prefix ci/browser --workspaces=false
```

A bare repository-root `node --test` recursively discovers historical tests and
is not supported. Node does not consume `.ignore` or pytest configuration; run
supported tests from their active package paths. The local archive review
environment provides Node 18. The applications require Node 22; application and
container runtime validation is a separate check.

## Provenance verification

On 2026-10-10 the archive was created directly from Git objects, without reading
local dependency, credential, or untracked runtime files. Every selected file's
content and executable mode was compared with its original Git tree entry.

The baseline `ci/scripts/source_snapshot.py` credential signature pattern was
applied to all 832 selected blobs without printing matched values. Its matches
were confined to seven test files containing synthetic rejection/redaction
fixtures; no production source matched. The `.env.example` and credential
configuration templates were checked without dumping their values. Prohibited
private/runtime paths were absent from the selected source list.

Active tracked source remains at its existing paths. Archive integration changes
are limited to `.dockerignore`, `.ignore`, and `pytest.ini`; the original source
files and current dependency/build manifests are unchanged.
