# Build API and agent MCP as separate images from the repository root.
# Build from the repository root. Registry index digest verified on 2026-10-02.
ARG NODE_IMAGE=node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c
ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
FROM ${NODE_IMAGE} AS dependencies
WORKDIR /app
COPY package.json package-lock.json ./
COPY apps/api/package.json apps/api/package.json
COPY apps/dashboard/package.json apps/dashboard/package.json
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund --workspace=apps/api --include-workspace-root=false

FROM ${NODE_IMAGE} AS mcp-dependencies
WORKDIR /app
COPY package.json package-lock.json ./
COPY apps/agent/package.json apps/agent/package.json
COPY apps/api/package.json apps/api/package.json
COPY apps/dashboard/package.json apps/dashboard/package.json
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund --workspace=apps/agent --include-workspace-root=false

FROM ${NODE_IMAGE} AS base
ENV NODE_ENV=production
WORKDIR /app/apps/api
COPY --from=dependencies /app/node_modules /app/node_modules
COPY apps/api/package.json ./package.json
COPY apps/api/src/ ./src/
COPY contracts/application.mjs /app/contracts/application.mjs
COPY ci/scripts/contract/telemetry.json ci/scripts/contract/stages.json /app/ci/scripts/contract/
USER 1000:1000

# Local ARM64 control-plane image used only by the credential-renewal CronJob.
# It deliberately omits the API runtime and its amd64-only provisioning tools.
FROM ${PYTHON_IMAGE} AS local-renewal
ARG TARGETARCH
USER root
RUN test "$TARGETARCH" = arm64 \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --disable-pip-version-check --no-cache-dir PyYAML==6.0.3 jsonschema==4.23.0
RUN curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
      https://dl.k8s.io/release/v1.34.11/bin/linux/arm64/kubectl -o /tmp/kubectl \
    && echo '5b045a4712674c88a56fd98eef4285689738b7fbe8735e1b9ee3509521af5cb4  /tmp/kubectl' | sha256sum --check --strict \
    && install -m 0755 /tmp/kubectl /usr/local/bin/kubectl \
    && rm /tmp/kubectl \
    && kubectl version --client=true --output=json >/dev/null
WORKDIR /app
COPY gitops/credentials.py gitops/argo.py gitops/handoff.py /app/gitops/
COPY ci/scripts/execution.py ci/scripts/observability.py ci/scripts/process.py ci/scripts/publication.py ci/scripts/storage.py /app/ci/scripts/
COPY ci/scripts/gate/bundle.py /app/ci/scripts/gate/
COPY ci/scripts/runner/runtime_boundary.py /app/ci/scripts/runner/
COPY ci/scripts/schemas/railshot.schema.json /app/ci/scripts/schemas/
ENV PYTHONDONTWRITEBYTECODE=1
RUN cd /app/gitops && python3 -c 'import credentials' \
    && test "$(stat -c %a /app/gitops/credentials.py)" = 644
USER 1000:1000
ENTRYPOINT ["python3", "/app/gitops/credentials.py"]

# Root-only WireGuard sidecar. The general API remains a separate non-root
# container and can reach this image only through the Pod-local Unix socket.
FROM ${PYTHON_IMAGE} AS personal-gateway
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates wireguard-tools iproute2 iptables \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/railshot
COPY deployment/scripts/personal_wireguard.py deployment/scripts/personal_gateway_service.py deployment/scripts/
COPY --chmod=0555 deployment/manifests/personal/gateway-service-entrypoint.sh /usr/local/bin/railshot-personal-gateway-service
RUN python3 deployment/scripts/personal_wireguard.py --help >/dev/null \
    && python3 deployment/scripts/personal_gateway_service.py --help >/dev/null
ENV PYTHONDONTWRITEBYTECODE=1
EXPOSE 51820/udp
ENTRYPOINT ["/usr/local/bin/railshot-personal-gateway-service"]

FROM base AS api
USER root
ENV PATH=/opt/railshot-python/bin:${PATH}
COPY apps/api/runtime-install.sh apps/api/runtime-smoke.py apps/api/runtime-tools.json ./
COPY apps/api/prepare-state.js ./
COPY deployment/manifests/personal/production-compose-prepare.py /app/deployment/manifests/personal/production-compose-prepare.py
COPY ci/requirements-dev.txt ci/requirements-test.txt /app/ci/
COPY gitops/bridge.py gitops/argo.py gitops/handoff.py /app/gitops/
COPY gitops/edge.py gitops/service_name.py /app/gitops/
COPY gitops/dns.py gitops/gcp_routes.py /app/gitops/
COPY gitops/openstack_routes.py /app/gitops/
COPY gitops/application_cleanup.py /app/gitops/
COPY gitops/credentials.py gitops/logs.py /app/gitops/
COPY gitops/workload_diagnostics.py /app/gitops/
COPY observability/register.py observability/bootstrap.py observability/render.py observability/compose.yaml observability/runtime_health.py /app/observability/
COPY ci/scripts/execution.py ci/scripts/observability.py ci/scripts/process.py ci/scripts/publication.py ci/scripts/storage.py /app/ci/scripts/
COPY ci/scripts/gate/bundle.py /app/ci/scripts/gate/
COPY ci/scripts/runner/runtime_boundary.py /app/ci/scripts/runner/
COPY ci/scripts/runner/replenish.py /app/ci/scripts/runner/
COPY ci/scripts/schemas/railshot.schema.json /app/ci/scripts/schemas/
COPY infrastructure/providers/terraform_tools/provision.py infrastructure/providers/terraform_tools/capabilities.py infrastructure/providers/terraform_tools/costs.py infrastructure/providers/terraform_tools/access.py /app/infrastructure/providers/terraform_tools/
COPY infrastructure/providers/terraform_tools/budget.py /app/infrastructure/providers/terraform_tools/
COPY infrastructure/terraform/aws/ /app/infrastructure/terraform/aws/
COPY infrastructure/terraform/gcp/ /app/infrastructure/terraform/gcp/
COPY infrastructure/terraform/aws-edge/ /app/infrastructure/terraform/aws-edge/
COPY infrastructure/terraform/gcp-edge/ /app/infrastructure/terraform/gcp-edge/
COPY infrastructure/ansible/run.py infrastructure/ansible/transport.py infrastructure/ansible/ansible.cfg infrastructure/ansible/guest.yml infrastructure/ansible/runtime.yml /app/infrastructure/ansible/
COPY infrastructure/ansible/cluster.py infrastructure/ansible/database.py infrastructure/ansible/inputs.py infrastructure/ansible/application_database.py infrastructure/ansible/database.yml infrastructure/ansible/application-database.yml /app/infrastructure/ansible/
COPY infrastructure/ansible/playbooks/ /app/infrastructure/ansible/playbooks/
COPY infrastructure/ansible/roles/ /app/infrastructure/ansible/roles/
COPY infrastructure/ansible/tasks/guest-checks.yml /app/infrastructure/ansible/tasks/
COPY infrastructure/ansible/group_vars/all.yml /app/infrastructure/ansible/group_vars/
COPY contracts/ansible-request.schema.json contracts/ansible-job.schema.json /app/contracts/
COPY deployment/scripts/common.sh deployment/scripts/environment.py /app/deployment/scripts/
COPY deployment/scripts/applications.py deployment/scripts/application_release.py deployment/scripts/application_routes.py /app/deployment/scripts/
COPY deployment/scripts/application_lifecycle.py deployment/scripts/lifecycle_runtime.py /app/deployment/scripts/
COPY deployment/scripts/openstack_route_worker.py deployment/scripts/personal_wireguard.py /app/deployment/scripts/
COPY deployment/scripts/personal_runtime.py /app/deployment/scripts/
COPY deployment/scripts/personal-production-preflight.py /app/deployment/scripts/
COPY deployment/cloudflared/register.py deployment/cloudflared/render.py /app/deployment/cloudflared/
COPY deployment/bootstrap/preflight.sh deployment/bootstrap/install-k3s.sh deployment/bootstrap/health.sh deployment/bootstrap/runtime-healthz.py /app/deployment/bootstrap/
COPY deployment/cilium/install.sh deployment/cilium/preflight.py deployment/cilium/health.sh /app/deployment/cilium/
COPY deployment/airgap/versions.json /app/deployment/airgap/
RUN RAILSHOT_CONTAINER_BUILD=1 bash runtime-install.sh
COPY --chmod=0555 apps/api/git-askpass.sh /usr/local/bin/railshot-git-askpass
ENV GIT_ASKPASS=/usr/local/bin/railshot-git-askpass GIT_TERMINAL_PROMPT=0 PYTHONDONTWRITEBYTECODE=1 CHECKPOINT_DISABLE=1
USER 1000:1000
RUN python3 runtime-smoke.py
# Preserve the local source-asset route for existing CLI/local HTTP clients.
COPY apps/dashboard/index.html apps/dashboard/app.js apps/dashboard/styles.css /app/apps/dashboard/
COPY apps/dashboard/src/ /app/apps/dashboard/src/
# The installer endpoint packages these reviewed public files from the image.
COPY deployment/bootstrap/install.sh deployment/bootstrap/uninstall.sh deployment/bootstrap/install_payload.py deployment/bootstrap/claim_token.py deployment/bootstrap/requirements.lock /app/deployment/bootstrap/
COPY deployment/bootstrap/client_setup/ /app/deployment/bootstrap/client_setup/
COPY deployment/bootstrap/templates/agent-authorized-keys.README /app/deployment/bootstrap/templates/
COPY infrastructure/providers/openstack/__init__.py infrastructure/providers/openstack/cli.py infrastructure/providers/openstack/identity.py infrastructure/providers/openstack/discovery.py infrastructure/providers/openstack/access.py /app/infrastructure/providers/openstack/
COPY infrastructure/providers/openstack/templates/cloud-init.yaml.tmpl /app/infrastructure/providers/openstack/templates/
COPY apps/agent/__init__.py apps/agent/install_forced_command.py apps/agent/protocol.py apps/agent/runner.py apps/agent/sender.py /app/apps/agent/
EXPOSE 4173
HEALTHCHECK --interval=30s --timeout=3s CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||4173)+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "src/server.js"]

FROM ${NODE_IMAGE} AS mcp
ENV NODE_ENV=production
WORKDIR /app/apps/agent
COPY --from=mcp-dependencies /app/node_modules /app/node_modules
COPY apps/agent/package.json ./package.json
COPY apps/agent/src/ ./src/
USER 1000:1000
# Default local stdio entrypoint; the platform workload overrides this with remote-mcp.js.
EXPOSE 4185
CMD ["node", "src/mcp.js"]
