# Deployment decisions

## Java server with HTML templates

Input: `src/Main.java` starts an HTTP server and loads `public/index.html` and an image. There is no Dockerfile or workload spec.

Read the entrypoint and its file accesses. Package the Java runtime and required assets with the correct working directory; use the contract's allowed Java image/version and non-root user. Do not serve the template as a finished static page: its substitutions and form handlers belong to the server. Propose one spec whose port and route match the source.

If the source hard-codes `127.0.0.1`, record that fact. A packaging attempt may use an existing host flag or environment setting, but cannot invent one. Once the real container gate demonstrates an inaccessible listener, a source-scoped fixer can change the binding to an appropriate externally reachable address while preserving handlers and behavior. Packaging alone is not proof of reachability.

## Two workload specifications

Input: L1 reports that exactly one current or legacy spec is required, and both exist.

Compare service names, build contexts, ports, routes and data requirements. If one is a redundant copy, retain the intended definition and propose deletion of the duplicate with evidence. If they describe different intended services, reconcile them into the supported single-spec form only when intent is clear; do not arbitrarily discard a service. Run the complete gates afterward.

## Missing runtime asset or failed startup

Input: the image builds, but L3 reports a missing template or the wrong listener.

Inspect the Dockerfile's final stage, working directory, startup command and source file access. Copy the needed asset into the runtime stage or use an existing supported startup setting. Change source only if the observed cause requires it and this attempt has source scope. Do not add a second server or a synthetic always-successful health endpoint.

## References used to shape this guidance

- [Docker build best practices](https://docs.docker.com/build/building/best-practices/): separate build dependencies from runtime output, keep context focused, and use an appropriate runtime user. Railshot's current contract remains authoritative for image and execution restrictions.
- [Anthropic: Building effective agents](https://www.anthropic.com/engineering/building-effective-agents): use simple workflows, concrete environmental feedback and bounded iteration. Railshot uses deterministic gates as the evaluator; it does not add an evaluator Agent or orchestration framework.
