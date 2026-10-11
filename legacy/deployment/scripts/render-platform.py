#!/usr/bin/env python3
"""Render private platform workloads from published immutable image references."""
import argparse
import hashlib
import json
from pathlib import Path
import re

import yaml


def image_ref(images, name):
    value = images.get(name, "")
    if not re.fullmatch(rf"ghcr\.io/jasmin-softbank/railshot-{name}@sha256:[a-f0-9]{{64}}", value):
        raise ValueError(f"{name}: published Railshot GHCR digest required")
    return value


def render(images, target_id, dashboard_node_port=None, provider_targets=None, prepare_api_rollout=False,
           personal=False):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", target_id):
        raise ValueError("operator-approved target_id is required")
    if dashboard_node_port is not None and not 30000 <= dashboard_node_port <= 32767:
        raise ValueError("dashboard NodePort must be in 30000..32767")
    if provider_targets is None:
        provider_targets = {}
    if not isinstance(provider_targets, dict) or set(provider_targets) - {'aws', 'gcp', 'openstack', 'proxmox'}:
        raise ValueError("provider targets must map supported providers to registered target IDs")
    selections = {'aws': target_id}
    for provider, selected in provider_targets.items():
        if not isinstance(selected, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', selected):
            raise ValueError("provider target ID is invalid")
        if provider == 'aws' and selected != target_id or any(other != provider and value == selected for other, value in selections.items()):
            raise ValueError("provider targets must preserve the default and use distinct target IDs")
        selections[provider] = selected
    for name in ("dashboard", "api"):
        image_ref(images, name)
    if personal:
        image_ref(images, "personal-gateway")
    if "mcp" in images:
        image_ref(images, "mcp")
    source = Path(__file__).resolve().parents[1] / "manifests/platform.yaml"
    documents = list(yaml.safe_load_all(source.read_text()))
    if not personal:
        documents = [document for document in documents if document.get('metadata', {}).get('name') not in {
            'railshot-personal-gateway', 'railshot-personal-wireguard'}]
        api = next(document for document in documents
                   if document['kind'] == 'Deployment' and document['metadata']['name'] == 'railshot-api')
        pod = api['spec']['template']['spec']
        pod['initContainers'] = [item for item in pod.get('initContainers', [])
                                 if item['name'] != 'personal-gateway']
        for container in pod['containers'] + pod.get('initContainers', []):
            container['env'] = [item for item in container.get('env', [])
                                if item['name'] != 'RAILSHOT_PERSONAL_CONFIG']
            container['volumeMounts'] = [item for item in container.get('volumeMounts', [])
                                         if not item['name'].startswith('personal-')]
        pod['volumes'] = [item for item in pod.get('volumes', [])
                          if not item['name'].startswith('personal-')]
        policy = next(document for document in documents
                      if document['kind'] == 'NetworkPolicy' and document['metadata']['name'] == 'railshot-api-private')
        policy['spec']['ingress'] = [item for item in policy['spec']['ingress']
                                     if not any(port.get('port') == 51820 for port in item.get('ports', []))]
    if "mcp" not in images:
        documents = [document for document in documents if not
                     (document['metadata']['name'] == 'railshot-mcp' and document['kind'] == 'Deployment')
                     and document['metadata']['name'] != 'railshot-mcp-private']
    documents = [document for document in documents if document['kind'] != 'Job' or prepare_api_rollout]
    for document in documents:
        if document['kind'] == 'Job':
            document['spec']['template']['spec']['containers'][0]['image'] = images['api']
        if dashboard_node_port and document["kind"] == "Service" and document["metadata"]["name"] == "railshot-dashboard":
            document["spec"]["type"] = "NodePort"
            document["spec"]["externalTrafficPolicy"] = "Local"
            document["spec"]["ports"][0]["nodePort"] = dashboard_node_port
        if document["kind"] != "Deployment":
            continue
        containers = document["spec"]["template"]["spec"]["containers"]
        for container in containers:
            container["image"] = images[container["name"]]
        for init in document["spec"]["template"]["spec"].get("initContainers", []):
            init["image"] = images[init["name"]] if init["name"] in images else images["api"]
        container = containers[0]
        for item in container.get("env", []):
            if item["name"] == "RAILSHOT_TARGET_ID":
                item["value"] = target_id
        if container['name'] == 'api' and provider_targets:
            container['env'].extend([
                {'name': 'RAILSHOT_PROVIDER_TARGETS', 'value': json.dumps(provider_targets, sort_keys=True, separators=(',', ':'))},
                {'name': 'RAILSHOT_TARGET_IDS', 'value': ','.join([target_id, *sorted(set(selections.values()) - {target_id})])},
            ])
    api = next(item for item in documents if item['kind'] == 'Deployment' and item['metadata']['name'] == 'railshot-api')
    template = api['spec']['template']
    template_id = hashlib.sha256(json.dumps(template, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    template['spec']['containers'][0]['env'].append({'name': 'RAILSHOT_POD_TEMPLATE_ID', 'value': template_id})
    for item in documents:
        if item['kind'] == 'Job':
            item['spec']['template']['spec']['containers'][0]['env'].append({'name': 'RAILSHOT_DESIRED_TEMPLATE_ID', 'value': template_id})
    return {"apiVersion": "v1", "kind": "List", "items": documents}


def render_build_runner(images, name, url, node):
    # Keep privileged build Job/RBAC outside the restricted platform Argo project.
    for value in (name, node):
        if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", value):
            raise ValueError("reviewed runner and build-node names are required")
    if not isinstance(url, str) or not re.fullmatch(r"https://github\.com/Jasmin-Softbank/[A-Za-z0-9_.-]+", url):
        raise ValueError("a reviewed Jasmin-Softbank apps repository URL is required")
    image = image_ref(images, "ci-runner")
    source = Path(__file__).resolve().parents[1] / "manifests/build-runner.yaml"
    documents = list(yaml.safe_load_all(source.read_text()))
    next(item for item in documents if item["kind"] == "ClusterRole")["rules"][0]["resourceNames"] = [node]
    job = next(item for item in documents if item["kind"] == "Job")
    job["metadata"]["name"] = name
    pod = job["spec"]["template"]["spec"]
    pod["nodeSelector"]["kubernetes.io/hostname"] = node
    runner = pod["containers"][0]
    runner["image"] = image
    for item in runner["env"]:
        if item["name"] == "RAILSHOT_RUNNER_URL":
            item["value"] = url
        elif item["name"] == "RAILSHOT_RUNNER_NAME":
            item["value"] = name
    return {"apiVersion": "v1", "kind": "List", "items": documents}


def render_build_controller(images, url, node, runner_count=1):
    if type(runner_count) is not int or not 1 <= runner_count <= 64:
        raise ValueError("runner_count must be 1..64")
    runners = render_build_runner(images, 'railshot-runner-template', url, node)['items']
    template = next(item for item in runners if item['kind'] == 'Job')
    encoded = json.dumps(template, sort_keys=True, separators=(',', ':'))
    policy = {'version': 1, 'template_sha256': hashlib.sha256(encoded.encode()).hexdigest(),
              'repository': url.removeprefix('https://github.com/'), 'node': node, 'runner_count': runner_count,
              'image': image_ref(images, 'ci-runner')}
    source = Path(__file__).resolve().parents[1] / 'manifests/build-controller.yaml'
    documents = list(yaml.safe_load_all(source.read_text()))
    for document in documents:
        if document['kind'] == 'ConfigMap':
            document['data'] = {'job.json': encoded, 'policy.json': json.dumps(policy, sort_keys=True)}
        if document['kind'] == 'CronJob':
            document['spec']['jobTemplate']['spec']['template']['spec']['containers'][0]['image'] = image_ref(images, 'api')
    # The existing runner identity/RBAC is installed unchanged; its Job is a reviewed template only.
    return {'apiVersion': 'v1', 'kind': 'List', 'items': [item for item in runners if item['kind'] != 'Job'] + documents}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", type=Path, help="JSON map of component to GHCR digest")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--target-id")
    mode.add_argument("--build-runner-name", help="render a separate one-job build runner and scoped RBAC")
    mode.add_argument("--build-controller", action="store_true", help="render the platform CronJob that replenishes reviewed ephemeral runners")
    parser.add_argument("--runner-url")
    parser.add_argument("--runner-count", type=int, default=1, help="maximum isolated runners on the existing build node")
    parser.add_argument("--build-node", help="exact approved build worker hostname label")
    parser.add_argument("--dashboard-node-port", type=int, help="optional allocated ALB backend port; API stays private")
    parser.add_argument("--provider-targets", default="{}", help="optional JSON provider-to-target map; enable only after CI and CD registration")
    parser.add_argument('--prepare-api-rollout', action='store_true', help='prewarm the API image and prepare the existing single writer')
    parser.add_argument('--personal', action='store_true', help='enable the operator-preflighted personal gateway and configuration')
    args = parser.parse_args()
    try:
        if args.runner_count != 1 and not args.build_controller:
            raise ValueError("--runner-count requires --build-controller")
        images = json.loads(args.images.read_text())
        provider_targets = json.loads(args.provider_targets)
        if not isinstance(provider_targets, dict):
            raise ValueError("provider targets must be a JSON object")
        if args.build_runner_name or args.build_controller:
            if args.dashboard_node_port or provider_targets or args.personal:
                raise ValueError("build runner cannot configure dashboard ports or provider targets")
            output = (render_build_controller(images, args.runner_url, args.build_node, args.runner_count) if args.build_controller
                      else render_build_runner(images, args.build_runner_name, args.runner_url, args.build_node))
        else:
            if args.runner_url or args.build_node:
                raise ValueError("runner options require --build-runner-name or --build-controller")
            output = render(images, args.target_id, args.dashboard_node_port, provider_targets,
                            args.prepare_api_rollout, args.personal)
        print(json.dumps(output, indent=2))
    except (ValueError, TypeError, KeyError) as error:
        parser.exit(2, f"BLOCKED: {error}\n")
