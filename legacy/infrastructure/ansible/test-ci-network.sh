#!/bin/bash
# Real container checks plus DROP-counter evidence; no SDK/cloud/registry credentials.
set -euo pipefail
test "$(id -u)" -eq 0 || { echo 'Run as root to inspect firewall counters.' >&2; exit 2; }
profile=/etc/railshot/ci-executor.yaml
test -f "$profile" && test ! -L "$profile"
test "$(stat -c '%u:%a' "$profile")" = '0:644'
jq -e '.schema_version == 1 and .builder.network == "railshot-quality"' "$profile" >/dev/null
# Every acceptance attempt invalidates the previous receipt before any work.
rm -f /var/lib/railshot-ci/network-verified.sha256
builder=$(jq -er '.builder.name' "$profile")
docker_version=$(docker version --format '{{json .}}')
buildx_version=$(docker buildx version)
systemctl is-active --quiet railshot-ci-network
docker network inspect railshot-quality | jq -e '.[0].Options["com.docker.network.bridge.name"] == "br-railshot" and .[0].EnableIPv6 == false' >/dev/null
image=python:3.12.14-slim-bookworm
docker image inspect "$image" >/dev/null 2>&1 || timeout 180s docker pull "$image" >&2
image_id=$(docker image inspect --format '{{.Id}}' "$image")
counter() {
  nft -j list table inet railshot_ci | python3 -c '
import json, sys
rules = [item["rule"] for item in json.load(sys.stdin)["nftables"] if "rule" in item]
matched = [rule for rule in rules if rule.get("comment") == sys.argv[1]]
if len(matched) != 1:
    raise SystemExit("Missing or duplicated owned nft counter: " + sys.argv[1])
counters = [expr["counter"]["packets"] for expr in matched[0]["expr"] if "counter" in expr]
if len(counters) != 1:
    raise SystemExit("Owned nft rule must have one packet counter")
print(counters[0])' "$1"
}
metadata_before=$(counter railshot-deny-169.254.0.0/16)
private_before=$(counter railshot-deny-10.0.0.0/8)
host_before=$(counter railshot-ci-host-block)
other_before=$(counter railshot-deny-other-egress)
name=railshot-network-check-$$
runtime_net=railshot-runtime-check-$$
runtime_peer=railshot-runtime-peer-$$
runtime_client=railshot-runtime-client-$$
build_tag=railshot-network-check:$$
check_dir=$(mktemp -d)
probe_table=railshot_ci_probe_$$
cleanup() {
  nft delete table inet "$probe_table" >/dev/null 2>&1 || true
  docker rm -f "$name" "$runtime_peer" "$runtime_client" >/dev/null 2>&1 || true
  docker network rm "$runtime_net" >/dev/null 2>&1 || true
  docker image rm "$build_tag" >/dev/null 2>&1 || true
  rm -rf "$check_dir"
}
trap cleanup EXIT
# Exercise real packets with ACCEPT in separate base chains both before and
# after the owned -10 hooks. nft ACCEPT must continue to the later base chain;
# terminal owned DROP counters below must still increase. Only CI interfaces
# are matched, and cleanup removes this temporary test table on every exit.
{
  echo "add table inet $probe_table"
  for priority in -20 0; do
    suffix=${priority#-}
    for hook in input forward; do
      printf 'add chain inet %s %s_%s { type filter hook %s priority %s; policy accept; }\n' "$probe_table" "$hook" "$suffix" "$hook" "$priority"
      for interface in br-railshot 'rsrun*'; do
        echo "add rule inet $probe_table ${hook}_${suffix} iifname \"$interface\" counter accept"
      done
    done
  done
} | nft -f -
install -d -m 0700 /var/lib/railshot-ci
cat > "$check_dir/probe.py" <<'PYPROBE'
import json, socket, urllib.request
registries = ['https://registry.npmjs.org/-/ping', 'https://pypi.org/simple/uv/', 'https://repo.maven.apache.org/maven2/']
checked = []
for url in registries:
    with urllib.request.urlopen(url, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError('Public registry did not return 200: ' + url)
        checked.append(url)
denied = [('metadata', '169.254.169.254', 80), ('private', '10.0.0.1', 80),
          ('host-gateway', '172.30.0.1', 80), ('non-http-port', '1.1.1.1', 22)]
for label, address, port in denied:
    try:
        connection = socket.create_connection((address, port), timeout=2)
    except TimeoutError:
        continue
    else:
        connection.close()
        raise RuntimeError('Forbidden destination connected: ' + label)
print(json.dumps({'public_registries': checked, 'connection_timeouts': [entry[0] for entry in denied]}))
PYPROBE
result=$(timeout 90s docker run --rm -i --name "$name" --network railshot-quality \
  --user 65532:65532 --read-only --cap-drop ALL --security-opt no-new-privileges \
  --cpus 1 --memory 128m --pids-limit 64 --entrypoint python "$image_id" - < "$check_dir/probe.py")
build_metadata_before=$(counter railshot-deny-169.254.0.0/16)
build_private_before=$(counter railshot-deny-10.0.0.0/8)
build_host_before=$(counter railshot-ci-host-block)
build_other_before=$(counter railshot-deny-other-egress)
# Exercise Dockerfile RUN through exactly the builder/network mode used by L2.
cat > "$check_dir/Dockerfile" <<DOCKERFILE
FROM $image
COPY probe.py /probe.py
RUN python /probe.py
DOCKERFILE
timeout 300s docker buildx build --builder "$builder" --network default --no-cache \
  --progress plain --load -t "$build_tag" "$check_dir" > "$check_dir/build.log" 2>&1 || {
  tail -n 80 "$check_dir/build.log" >&2; exit 1;
}
test "$(counter railshot-deny-169.254.0.0/16)" -gt "$build_metadata_before"
test "$(counter railshot-deny-10.0.0.0/8)" -gt "$build_private_before"
test "$(counter railshot-ci-host-block)" -gt "$build_host_before"
test "$(counter railshot-deny-other-egress)" -gt "$build_other_before"
# The daemon itself must reject host entitlement, even if a client asks for it.
cat > "$check_dir/Dockerfile.host" <<DOCKERFILE
FROM $image
RUN --network=host true
DOCKERFILE
if timeout 60s docker buildx build --builder "$builder" --allow network.host \
    --network host --progress plain -f "$check_dir/Dockerfile.host" "$check_dir" > "$check_dir/host-entitlement.log" 2>&1; then
  echo 'BuildKit accepted forbidden host entitlement' >&2; exit 1
fi
grep -Eq 'network.host is not allowed|entitlement network.host.*not allowed' "$check_dir/host-entitlement.log"
# L3 must retain same-run service connectivity and host health probes while
# refusing external traffic and NEW connections to the host bridge gateway.
bridge=$(printf 'rsrun-%08x' "$$")
docker network create --internal --driver bridge --opt "com.docker.network.bridge.name=$bridge" "$runtime_net" >/dev/null
gateway=$(docker network inspect "$runtime_net" --format '{{(index .IPAM.Config 0).Gateway}}')
runtime_host_before=$(counter railshot-runtime-host-block)
docker run -d --name "$runtime_peer" --network "$runtime_net" --user 65532:65532 \
  --read-only --cap-drop ALL --security-opt no-new-privileges --cpus 1 --memory 128m --pids-limit 64 \
  --entrypoint python "$image_id" -m http.server 8080 >/dev/null
# Docker 29 internal bridges do not publish host ports. Host-side observation
# uses the assigned private container address; no inbound socket is published.
health_ip=$(docker inspect "$runtime_peer" | jq -er --arg network "$runtime_net" '.[0].NetworkSettings.Networks[$network].IPAddress')
python3 - "$health_ip" <<'PYHEALTH'
import sys, time, urllib.request
for attempt in range(20):
    try:
        with urllib.request.urlopen('http://' + sys.argv[1] + ':8080', timeout=2) as response:
            assert response.status == 200
        break
    except OSError:
        if attempt == 19: raise
        time.sleep(.5)
PYHEALTH
runtime_result=$(timeout 30s docker run --rm -i --name "$runtime_client" --network "$runtime_net" \
  --user 65532:65532 --read-only --cap-drop ALL --security-opt no-new-privileges \
  --cpus 1 --memory 128m --pids-limit 64 --entrypoint python "$image_id" - "$runtime_peer" "$gateway" <<'PYRUNTIME'
import json, socket, sys, urllib.request
with urllib.request.urlopen('http://' + sys.argv[1] + ':8080', timeout=5) as response:
    assert response.status == 200
for target in (sys.argv[2], '169.254.169.254', '10.0.0.1', '1.1.1.1'):
    try:
        connection = socket.create_connection((target, 80), timeout=2)
    except OSError:
        continue
    connection.close()
    raise RuntimeError('Runtime reached forbidden destination: ' + target)
print(json.dumps({'same_run_http': 'PASS', 'external_and_host_connections': 'BLOCKED'}))
PYRUNTIME
)
runtime_host_after=$(counter railshot-runtime-host-block)
test "$runtime_host_after" -gt "$runtime_host_before"
metadata_after=$(counter railshot-deny-169.254.0.0/16)
private_after=$(counter railshot-deny-10.0.0.0/8)
host_after=$(counter railshot-ci-host-block)
other_after=$(counter railshot-deny-other-egress)
test "$metadata_after" -gt "$metadata_before"
test "$private_after" -gt "$private_before"
test "$host_after" -gt "$host_before"
test "$other_after" -gt "$other_before"
docker rm -f "$runtime_peer" >/dev/null
docker network rm "$runtime_net" >/dev/null
docker image rm "$build_tag" >/dev/null
# Write readiness only after Q, real BuildKit RUN and internal runtime checks.
# The gate rechecks this fingerprint before executing L2/L3; reconciliation or
# policy drift invalidates it. This is local evidence, not a remote attestation.
# Do not leave an empty success-looking file after a failed fingerprint.
policy_fingerprint=$(/usr/local/sbin/railshot-ci-network --fingerprint)
printf '%s\n' "$policy_fingerprint" > /var/lib/railshot-ci/network-verified.sha256
chmod 0600 /var/lib/railshot-ci/network-verified.sha256
/usr/local/sbin/railshot-ci-network --check
jq -n --arg image_id "$image_id" --argjson checks "$result" --argjson runtime "$runtime_result" \
  --argjson docker_version "$docker_version" --arg buildx_version "$buildx_version" \
  --argjson runtime_host "$((runtime_host_after-runtime_host_before))" \
  --argjson metadata "$((metadata_after-metadata_before))" --argjson private "$((private_after-private_before))" \
  --argjson host "$((host_after-host_before))" --argjson other "$((other_after-other_before))" \
  '{status:"PASS",network:"railshot-quality",builder:"railshot-ci",docker_version:$docker_version,buildx_version:$buildx_version,buildkit_run:"PASS",host_entitlement:"BLOCKED",runtime:$runtime,runtime_host_drop_packets:$runtime_host,image_id:$image_id,checks:$checks,drop_packet_deltas:{metadata:$metadata,private:$private,host:$host,other:$other}}'
