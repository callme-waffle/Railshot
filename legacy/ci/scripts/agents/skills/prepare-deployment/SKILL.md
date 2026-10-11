---
name: prepare-deployment
description: Propose evidence-backed packaging or repairs for an observed build/start/health failure.
---

# Prepare an application for Railshot

## Responsibility

Work inside the sanitized CI copy, not the user's repository or running cluster. Intake and deterministic packaging have already run. The supplied role and failed stage identify what needs investigation. After validating your proposal, the host applies files and runs its configured gates. Publication, GitOps, routing and public endpoint verification happen downstream; do not generate their infrastructure or receipts.

## Investigation

Start with the bounded evidence and previous-attempt summary. Follow only the relevant build/start command, imports, assets and runtime configuration. Inventory and README claims are leads to verify; a `public` directory alone does not establish a static site. Read additional source ranges or referenced logs only to resolve missing facts. Read a complete file before returning its replacement.

Preserve application behavior, identity, persistence requirements and toolchain. Prefer an existing supported flag or container definition. Under packaging scope change only deployment artifacts. Source scope permits changes needed for an observed build/start/health failure, not suspected future defects. Cite observed source/log locations in `evidence_refs`; explanations remain hypotheses, not proof of cause.

## Proposal and feedback

Explain every changed path in `files_changed.why`. Delete an existing writable text file only after tracing references and establishing that it is redundant or conflicting; explain what preserves its needed behavior. Protected files stay protected for every operation.

The host records the gate plan and applies path, scope and size checks before writing. Do not generate a gate plan or run validation commands. Actual gates determine success and whether another model call is allowed. Use the attempt count supplied in the task; there is no separate fixed SDK budget in these instructions. Stop with a precise `give_up` when evidence is insufficient, the cause needs operator action, or there is no different evidence-backed fix after a repeated failure.

Read [references/examples.md](references/examples.md) only when a Java/server template, duplicate spec, or runtime repair example is relevant.
