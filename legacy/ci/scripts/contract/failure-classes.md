# Failure classes

The classifier (deterministic) assigns one class to the first failing gate step and decides the next action before any LLM is called. The fixer only receives classes marked "fixer".

| Class | Meaning | Typical signals | Next action | LLM attempts |
|---|---|---|---|---|
| F1 | Dependency resolution or install | `No matching distribution`, `ERESOLVE`, `ModuleNotFoundError` at build, missing system library | Rule table first, then fixer | 0 by default; bounded by configured N |
| F2 | Build context | `COPY failed`, file not found in context, `.dockerignore` excludes a needed path | Rule table first, then fixer | up to N |
| F3 | Base image or architecture | `exec format error`, unknown base, disallowed base | fixer may select an allowed compatible base; architecture and policy gates rerun | up to N |
| F4 | Start, port, bind, health | container exits, connection refused, health path 404/5xx, timeout | fixer | up to N |
| F5 | Spec schema or policy violation | JSON Schema error, gate L1 rule violation, render error | fixer | up to N |
| F6 | CRITICAL vulnerability with a fix | Trivy CRITICAL | fixer (base or package version only) | up to N |
| F7 | Application code defect | stack trace in app code, failing app tests, missing app feature | source scope: bounded fixer; packaging scope: stop with exact needed change | up to N in source scope |
| F8 | Transient infrastructure | registry 5xx, DNS timeout, runner lost, rate limit | Stop; operator reconciles before retry | 0 |
| F9 | Infra plan error or policy | `terraform validate/plan` error, destroy or replace in plan, cost policy deny | fixer for spec errors; destroy/replace always stops for confirmation | up to N |
| INJ | Suspected prompt injection | agent reports INJECTION_SUSPECTED, or deny-listed phrases in logs sent to the agent | Stop, flag for review | 0 |

Loop stops on: gate pass, `give_up`, class F8/INJ (also F7 in packaging scope), environmental/unknown outcomes, the same normalized signature twice, or the role's attempt limit (default: at most two packaging calls and two repair calls; repair limit 0 disables all SDK calls).

Q is advisory when explicitly enabled. Missing tests/checkers or completed
lint/type/unit failures do not request source/test changes. Environment,
source-integrity and unknown execution failures still block. Default gates are
L0/L1/L2/L3; optional Q and L4 remain NOT_RUN unless active. Every proposal binds
and reruns the exact active profile; see [source repair bounds](../runner/SOURCE-REPAIR.md).

The v2 failure fingerprint preserves compiler codes and causal error text while
normalizing ephemeral hashes and timing. The bounded excerpt and source/log
references are diagnostic facts, never model instructions. Jev's separate API
classification is a hypothesis and has no retry or write authority.
