# Deployment mistakes to avoid

- A missing deployment spec is a packaging task, not automatically `OUT_OF_SCOPE`. Propose the permitted spec, Dockerfile and ignore file when evidence is sufficient.
- Missing tests or completed lint/type/unit failures are advisory for deployment. Do not add tests or checker setup, weaken checks, fabricate locks, or replace the app with a placeholder. Preserve existing scripts and behavior.
- A failed sandbox launch, denied read or unreadable workspace is a runner problem, not an application diagnosis. Check tool exit status; SDK completion does not prove source inspection succeeded. Return no speculative patch or requests to broaden permissions. State what could not be read and the required operator action.
- Do not substitute a VM-host check, fixture result, previous deployment, or another environment's success for this run's evidence. Only the outer executor can establish build, deployment and public-response success.
