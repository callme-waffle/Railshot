# Local acceptance deployment: one Linux network namespace for API and gateway.
# Build from the repository root. Central registration requires kubectl and git.
FROM node:24-bookworm-slim
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-yaml python3-jsonschema wireguard-tools iproute2 iptables sudo openssh-client curl ca-certificates util-linux git \
    && rm -rf /var/lib/apt/lists/* \
    && usermod --login railshot --home /home/railshot node \
    && groupmod --new-name railshot node \
    && mkdir -p /home/railshot && chown railshot:railshot /home/railshot
# Same kubectl release as apps/api/runtime-tools.json; architecture-specific pinned digests.
ARG TARGETARCH
RUN case "$TARGETARCH" in \
      amd64) digest=8efbb9435132a190920eb65a47a8c1ecf755ad85ab57a600c9bedbab460bb7a8 ;; \
      arm64) digest=5b045a4712674c88a56fd98eef4285689738b7fbe8735e1b9ee3509521af5cb4 ;; \
      *) exit 1 ;; esac \
    && curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
       "https://dl.k8s.io/release/v1.34.11/bin/linux/$TARGETARCH/kubectl" -o /tmp/kubectl \
    && echo "$digest  /tmp/kubectl" | sha256sum --check --strict \
    && install -m 0755 /tmp/kubectl /usr/local/bin/kubectl && rm /tmp/kubectl \
    && kubectl version --client=true --output=json && git --version
WORKDIR /opt/railshot
COPY package.json package-lock.json ./
COPY apps/api/package.json apps/api/package.json
COPY apps/dashboard/package.json apps/dashboard/package.json
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund --workspace=apps/api --include-workspace-root=false
COPY apps/api/src/ apps/api/src/
COPY apps/dashboard/index.html apps/dashboard/app.js apps/dashboard/styles.css apps/dashboard/
COPY apps/dashboard/src/ apps/dashboard/src/
COPY contracts/application.mjs contracts/application.mjs
COPY apps/agent/ apps/agent/
COPY deployment/bootstrap/client_setup/ deployment/bootstrap/client_setup/
COPY deployment/bootstrap/install.sh deployment/bootstrap/personal-install.sh deployment/bootstrap/personal-registration.sh deployment/bootstrap/revoke_personal_identity.py deployment/bootstrap/requirements.lock deployment/bootstrap/
COPY deployment/bootstrap/uninstall.sh deployment/bootstrap/install_payload.py deployment/bootstrap/
COPY deployment/bootstrap/templates/agent-authorized-keys.README deployment/bootstrap/templates/
COPY infrastructure/providers/openstack/ infrastructure/providers/openstack/
COPY deployment/scripts/personal_wireguard.py deployment/scripts/package-personal-client.py deployment/scripts/
COPY infrastructure/ansible/run.py infrastructure/ansible/transport.py infrastructure/ansible/runtime.yml infrastructure/ansible/guest.yml infrastructure/ansible/ansible.cfg infrastructure/ansible/
COPY infrastructure/ansible/tasks/guest-checks.yml infrastructure/ansible/tasks/
COPY ci/scripts/storage.py ci/scripts/execution.py ci/scripts/publication.py ci/scripts/process.py ci/scripts/observability.py ci/scripts/
COPY ci/scripts/gate/bundle.py ci/scripts/gate/
COPY ci/scripts/runner/runtime_boundary.py ci/scripts/runner/
COPY ci/scripts/schemas/railshot.schema.json ci/scripts/schemas/
COPY contracts/ansible-request.schema.json contracts/
COPY deployment/scripts/common.sh deployment/scripts/personal_runtime.py deployment/scripts/applications.py deployment/scripts/environment.py deployment/scripts/
COPY deployment/scripts/application_release.py deployment/scripts/application_routes.py deployment/scripts/application_lifecycle.py deployment/scripts/lifecycle_runtime.py deployment/scripts/openstack_route_worker.py deployment/scripts/
COPY deployment/cloudflared/register.py deployment/cloudflared/render.py deployment/cloudflared/
COPY gitops/dns.py gitops/gcp_routes.py gitops/openstack_routes.py gitops/application_cleanup.py gitops/logs.py gitops/
COPY gitops/argo.py gitops/credentials.py gitops/bridge.py gitops/handoff.py gitops/edge.py gitops/service_name.py gitops/
COPY gitops/argo/ gitops/argo/
COPY deployment/bootstrap/preflight.sh deployment/bootstrap/install-k3s.sh deployment/bootstrap/health.sh deployment/bootstrap/runtime-healthz.py deployment/bootstrap/
COPY deployment/cilium/install.sh deployment/cilium/preflight.py deployment/cilium/health.sh deployment/cilium/
COPY deployment/airgap/versions.json deployment/airgap/
COPY deployment/manifests/personal/container-init.py deployment/manifests/personal/container-entrypoint.sh deployment/manifests/personal/control-bootstrap.py deployment/manifests/personal/control-token-minter.py deployment/manifests/personal/
COPY deployment/manifests/runtime-registration-access.yaml deployment/manifests/runtime-registration-access.yaml
COPY deployment/manifests/personal/railshot-personal-gateway /usr/local/sbin/railshot-personal-gateway
COPY deployment/manifests/personal/gateway.sudoers /etc/sudoers.d/railshot-personal-gateway
COPY --chmod=0555 apps/api/git-askpass.sh /usr/local/bin/railshot-git-askpass
ENV GIT_ASKPASS=/usr/local/bin/railshot-git-askpass GIT_TERMINAL_PROMPT=0
RUN python3 deployment/scripts/personal_runtime.py --help >/dev/null \
    && python3 deployment/scripts/application_lifecycle.py --help >/dev/null \
    && python3 deployment/scripts/application_routes.py --help >/dev/null \
    && python3 deployment/manifests/personal/control-bootstrap.py --help >/dev/null \
    && python3 deployment/manifests/personal/control-token-minter.py --help >/dev/null
RUN chmod 0755 /usr/local/sbin/railshot-personal-gateway deployment/manifests/personal/container-entrypoint.sh \
    && chmod 0440 /etc/sudoers.d/railshot-personal-gateway \
    && visudo -cf /etc/sudoers.d/railshot-personal-gateway \
    && test -f deployment/bootstrap/client_setup/credentials.py \
    && mkdir -p public \
    && python3 deployment/scripts/package-personal-client.py --output /opt/railshot/public/personal-client.tgz \
    && cp deployment/bootstrap/install.sh public/install.sh \
    && cp deployment/bootstrap/personal-install.sh public/personal-install.sh \
    && chmod -R a+rX /opt/railshot
ENV RAILSHOT_STATE_DIR=/var/lib/railshot \
    RAILSHOT_PERSONAL_CONFIG=/var/lib/railshot/config/personal.json \
    RAILSHOT_BIND_HOST=0.0.0.0 PORT=4173 PYTHONDONTWRITEBYTECODE=1
EXPOSE 4173/tcp 4184/tcp 51820/udp
HEALTHCHECK --interval=15s --timeout=3s CMD curl --fail --silent http://127.0.0.1:4173/healthz >/dev/null || exit 1
ENTRYPOINT ["/opt/railshot/deployment/manifests/personal/container-entrypoint.sh"]
