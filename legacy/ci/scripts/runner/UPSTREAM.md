# Agent runtime failure handling: upstream references

`native_preflight.py` adapts four namespace-error signatures from OpenAI Codex,
Copyright OpenAI, Apache-2.0:

- [Source, commit ca466061](https://github.com/openai/codex/blob/ca466061d64f0b44f416135c7fd06aa7af850bbc/codex-rs/sandboxing/src/bwrap.rs#L30)
- [Apache-2.0 license](https://github.com/openai/codex/blob/ca466061d64f0b44f416135c7fd06aa7af850bbc/LICENSE)
  (local copy: [LICENSE.apache-2.0](LICENSE.apache-2.0))

The upstream probe only decides whether to show a startup warning. Its timeout and
unrecognized failures are not approval evidence. RAILSHOT instead runs the bundled
CLI's `codex sandbox` with the exact agent permission overrides and fails closed on
any nonzero exit, timeout, missing success marker, or failed negative control. It
reads the allowed source directory and contract, tests write denial, reads a
host-readable file outside the allowed roots, and tests a loopback listener whose
reachability was first confirmed outside the sandbox. This makes no model call.
Each invocation records the actual UID, hostname, binary, permission digest and
result. A VM-host receipt cannot stand in for a Pod receipt.

The native command-item classifier checks SDK command exit status and output
before accepting a completed turn or applying files. Sandbox infrastructure
failure is `SDK_SANDBOX_UNAVAILABLE`, `BLOCKED`, `after_configuration`. The existing
loop owns attempts and checkpoints; unchanged permission errors are never blindly
retried, and this check does not disable isolation or change host privileges.

Other first-party implementation references (patterns only; no code imported):

- [Anthropic sandbox-runtime dependency checks](https://github.com/anthropics/sandbox-runtime/blob/117eb928202b53c80d3cb6527d88d1b90e4ca7a9/src/sandbox/sandbox-manager.ts#L686)
  (Apache-2.0): distinguish warnings from errors that block sandbox initialization.
- [OpenHands runtime readiness](https://github.com/OpenHands/software-agent-sdk/blob/41443fc14a8a38cf18ef72edf300627cf75f4549/openhands-workspace/openhands/workspace/remote_api/workspace.py#L293)
  (MIT): bounded readiness retries and terminal runtime failures have different paths.
- [GitHub Copilot environment setup](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/coding-agent/customize-the-agent-environment):
  preinstall tools and use ephemeral single-use self-hosted runners. The RAILSHOT
  runner includes `rg` and `jq`; packaging agents need no cluster credential.
- [GitHub MCP Server](https://github.com/github/github-mcp-server/blob/f10e4e1f923d46b86f2e80e849aa74084c847184/docs/server-configuration.md)
  (MIT) supports read-only Actions/repository tools. Existing `gh` diagnostics are
  sufficient here; no additional MCP service is required.
- [Kubernetes MCP Server configuration](https://github.com/containers/kubernetes-mcp-server/blob/db7117827b390e42232ba8451fb5eb0000a371fa/docs/configuration.md)
  (Apache-2.0) supports read-only tool allowlists and denied resources. It belongs
  in an operator diagnostic context with scoped RBAC, not in the source adapter.
  Existing operator `k3s` inspection remains the deployment diagnostic path.
