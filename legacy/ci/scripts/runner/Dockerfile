# Official GitHub runner 2.337.0, Ubuntu 24.04, Docker CLI + Buildx included.
# Update this digest deliberately; --disableupdate keeps the running image immutable.
ARG RUNNER_IMAGE=ghcr.io/actions/actions-runner:2.337.0@sha256:e5496277be5d09bc968b3d64911b74e219ac4a3f2edce956a3ecf9271bea1ef4
FROM ${RUNNER_IMAGE} AS runner
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-yaml iptables nftables iproute2 procps util-linux ca-certificates ripgrep jq \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -m 0755 /opt/hostedtoolcache \
    && install -d -m 0700 /var/lib/railshot-runner/work/_temp
COPY ci/scripts /opt/railshot/ci/scripts
COPY --chmod=0755 ci/scripts/runner/entrypoint.sh /usr/local/bin/railshot-runner
ENV RUNNER_ALLOW_RUNASROOT=1 \
    RUNNER_TOOL_CACHE=/opt/hostedtoolcache \
    AGENT_TOOLSDIRECTORY=/opt/hostedtoolcache \
    DOCKER_HOST=unix:///var/run/docker.sock \
    DOCKER_CONFIG=/home/runner/.docker \
    AWS_EC2_METADATA_DISABLED=true \
    TMPDIR=/var/lib/railshot-runner/work/_temp
WORKDIR /home/runner
ENTRYPOINT ["/usr/local/bin/railshot-runner"]
