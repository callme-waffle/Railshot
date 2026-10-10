# Build from the repository root. Registry index digests verified on 2026-10-02.
ARG NODE_IMAGE=node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c
ARG NGINX_IMAGE=nginx:stable-alpine@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94
ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
FROM ${NODE_IMAGE} AS build
WORKDIR /build
COPY package.json package-lock.json ./
COPY apps/dashboard/package.json apps/dashboard/package.json
COPY apps/api/package.json apps/api/package.json
RUN npm ci --ignore-scripts --no-audit --no-fund --workspace=apps/dashboard --include-workspace-root=false
COPY apps/dashboard/ apps/dashboard/
COPY contracts/application.mjs contracts/application.mjs
RUN npm run build --workspace=apps/dashboard

FROM ${PYTHON_IMAGE} AS personal-release
WORKDIR /source
COPY apps/agent/ apps/agent/
COPY ci/scripts/storage.py ci/scripts/storage.py
COPY contracts/ansible-request.schema.json contracts/ansible-request.schema.json
COPY deployment/airgap/versions.json deployment/airgap/versions.json
COPY deployment/bootstrap/ deployment/bootstrap/
COPY deployment/cilium/ deployment/cilium/
COPY deployment/cloudflared/render.py deployment/cloudflared/render.py
COPY deployment/scripts/common.sh deployment/scripts/lifecycle_runtime.py deployment/scripts/package-personal-client.py deployment/scripts/package-personal-release.py deployment/scripts/
COPY infrastructure/ansible/ infrastructure/ansible/
COPY infrastructure/providers/openstack/ infrastructure/providers/openstack/
RUN python3 deployment/scripts/package-personal-release.py --source /source --output /personal

FROM ${NGINX_IMAGE} AS dashboard
COPY apps/dashboard/nginx.conf /etc/nginx/nginx.conf
COPY --chmod=0555 apps/dashboard/start.sh /usr/local/bin/railshot-dashboard
COPY --from=build /build/apps/dashboard/dist/ /usr/share/nginx/html/
COPY --from=personal-release /personal/ /usr/share/nginx/html/personal/
USER 101:101
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
ENTRYPOINT ["/usr/local/bin/railshot-dashboard"]
