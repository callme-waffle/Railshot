import importlib.util
import hashlib
import json
import os
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]

spec = importlib.util.spec_from_file_location("platform_render", Path(__file__).parents[1] / "render-platform.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PlatformTests(unittest.TestCase):
    def test_build_job_uses_dedicated_agent_and_scoped_read_only_identity(self):
        images = {"ci-runner": "ghcr.io/jasmin-softbank/railshot-ci-runner@sha256:" + "a" * 64}
        result = module.render_build_runner(images, "build-test-1", "https://github.com/Jasmin-Softbank/railshot-apps", "railshot-build-worker-aws-01")
        resources = {item["kind"]: item for item in result["items"]}
        self.assertEqual(resources['Role']['rules'], [{'apiGroups':[''], 'resources':['pods'], 'verbs':['get']}])
        self.assertEqual(resources['ClusterRole']['rules'], [{'apiGroups':[''], 'resources':['nodes'], 'resourceNames':['railshot-build-worker-aws-01'], 'verbs':['get']}])
        job = resources['Job']
        self.assertEqual(job['spec']['backoffLimit'], 0)
        pod = job['spec']['template']['spec']
        self.assertEqual(pod['nodeSelector']['railshot.io/node-role'], 'build')
        self.assertEqual(pod['nodeSelector']['kubernetes.io/hostname'], 'railshot-build-worker-aws-01')
        self.assertEqual(pod['containers'][0]['image'], images['ci-runner'])
        self.assertFalse(any('/rancher' in v.get('hostPath', {}).get('path', '') for v in pod['volumes']))
        for name, url, node in [('bad/name', 'https://github.com/Jasmin-Softbank/apps', 'worker'),
                                ('runner', 'https://github.com/unreviewed/apps', 'worker'),
                                ('runner', 'https://github.com/Jasmin-Softbank/apps', '../node')]:
            with self.assertRaises(ValueError):
                module.render_build_runner(images, name, url, node)
        with self.assertRaises(ValueError):
            module.render_build_runner({'ci-runner':'ghcr.io/jasmin-softbank/railshot-ci-runner:latest'}, 'runner', 'https://github.com/Jasmin-Softbank/apps', 'worker')

    def test_gitops_manifests_keep_platform_permissions_and_sync_explicit(self):
        install = yaml.safe_load((ROOT / "gitops/argo/kustomization.yaml").read_text())
        self.assertEqual(install["kind"], "Kustomization")
        self.assertEqual(install["namespace"], "argocd")
        self.assertEqual(len(install["resources"]), 1)
        self.assertRegex(install["resources"][0],
                         r"^https://raw\.githubusercontent\.com/argoproj/argo-cd/[a-f0-9]{40}/manifests/install\.yaml$")
        documents = list(yaml.safe_load_all((ROOT / "gitops/applications/railshot-platform.yaml").read_text()))
        self.assertEqual(len(documents), 2)
        resources = {item["kind"]: item for item in documents}
        self.assertEqual(set(resources), {"AppProject", "Application"})
        project, application = resources["AppProject"], resources["Application"]
        for item in documents:
            self.assertEqual(item["apiVersion"], "argoproj.io/v1alpha1")
            self.assertEqual(item["metadata"]["namespace"], "argocd")
            self.assertNotIn("finalizers", item["metadata"])  # No cascading workload removal.
        self.assertEqual(application["spec"]["project"], project["metadata"]["name"])
        self.assertEqual(project["spec"]["sourceRepos"], ["https://github.com/Jasmin-Softbank/Railshot.git"])
        self.assertEqual(project["spec"]["clusterResourceWhitelist"], [])
        self.assertEqual({(item["group"], item["kind"]) for item in project["spec"]["namespaceResourceWhitelist"]},
                         {("batch", "Job"), ("apps", "Deployment"), ("", "Service"), ("", "PersistentVolumeClaim"), ("networking.k8s.io", "NetworkPolicy")})
        destination = {"server": "https://kubernetes.default.svc", "namespace": "railshot-system"}
        self.assertEqual(project["spec"]["destinations"], [destination])
        self.assertEqual(application["spec"]["destination"], destination)
        source = application["spec"]["source"]
        self.assertEqual(source["repoURL"], project["spec"]["sourceRepos"][0])
        self.assertEqual(source["path"], "gitops/applications/railshot-platform")
        self.assertEqual(source["directory"], {"include": "workload.json"})
        self.assertEqual(source["targetRevision"], "deployment/platform")
        self.assertEqual(application["spec"]["syncPolicy"]["automated"], {"prune": False, "selfHeal": True})

        # The operator owns metadata isolation; Argo cannot weaken this policy.
        metadata = yaml.safe_load((ROOT / 'deployment/manifests/product-metadata.yaml').read_text())
        self.assertEqual(metadata['apiVersion'], 'cilium.io/v2')
        self.assertEqual(metadata['kind'], 'CiliumClusterwideNetworkPolicy')
        self.assertEqual(metadata['metadata'], {'name': 'railshot-platform-metadata'})
        self.assertEqual(len(metadata['specs']), 5)
        for rule in metadata['specs']:
            self.assertEqual(set(rule), {'endpointSelector', 'enableDefaultDeny', 'egressDeny'})
            self.assertEqual(rule['enableDefaultDeny'], {'ingress': False, 'egress': False})
            self.assertEqual(rule['egressDeny'], [{'toCIDR': ['169.254.169.254/32', 'fd00:ec2::254/128']}])
        def denied(labels):
            for rule in metadata['specs']:
                selector = rule['endpointSelector']
                if not all(labels.get(key) == value for key, value in selector.get('matchLabels', {}).items()):
                    continue
                expressions = selector.get('matchExpressions', [])
                self.assertTrue(all(expr['operator'] == 'NotIn' for expr in expressions))
                if all(labels.get(expr['key']) not in expr['values'] for expr in expressions):
                    return True
            return False
        namespace = 'k8s:io.kubernetes.pod.namespace'
        service_account = 'k8s:io.cilium.k8s.policy.serviceaccount'
        api = {namespace: 'railshot-system', 'k8s:app': 'railshot-api', service_account: 'railshot-product'}
        for labels, expected in [(api, False), ({**api, service_account: 'default'}, True),
                                 ({**api, 'k8s:app': 'railshot-dashboard'}, True),
                                 ({namespace: 'railshot-system'}, True), ({namespace: 'argocd'}, True),
                                 ({namespace: 'kube-system', 'k8s:k8s-app': 'kube-dns'}, True),
                                 ({namespace: 'kube-system', 'k8s:app': 'local-path-provisioner'}, True),
                                 ({namespace: 'kube-system', 'k8s:k8s-app': 'cilium'}, False),
                                 ({namespace: 'railshot-build'}, False), ({namespace: 'tenant-demo'}, False)]:
            with self.subTest(labels=labels):
                self.assertEqual(denied(labels), expected)
        # The executor document passed AWS ValidatePolicy on 2026-10-03 (KST).
        # SSM context/target/document and edge-read simulations were refreshed;
        # the earlier 57-check receipt applies to the prior policy bytes only.
        # The exact edge hash below separately passed AWS ValidatePolicy with
        # zero findings on 2026-10-03. This is not an IAM rollout or proof that a
        # live principal can execute lifecycle deletes; scope tests are separate.
        policy = (ROOT / 'infrastructure/terraform/control/product-executor-policy.json').read_bytes()
        self.assertEqual(hashlib.sha256(policy).hexdigest(),
                         '0ab96416ae2537a339b5f2fbe7a3d1331b9f7063ca56596c53dedcf66602f814')
        edge_policy = (ROOT / 'infrastructure/terraform/control/product-edge-policy.json').read_bytes()
        self.assertEqual(hashlib.sha256(edge_policy).hexdigest(),
                         'fb401be9f21ee946592cd56f1e2991359df0d60785bca03ddf36decb7a118dfb')
        self.assertLessEqual(len(json.dumps(json.loads(edge_policy), separators=(',', ':'))), 6144)

    @unittest.skipUnless(os.environ.get("RAILSHOT_ARGO_SCHEMA_MANIFEST"),
                         "Set RAILSHOT_ARGO_SCHEMA_MANIFEST to the kubectl kustomize output for native Argo schema validation")
    def test_platform_declarations_match_rendered_upstream_argo_crds(self):
        from jsonschema import Draft7Validator
        rendered = Path(os.environ["RAILSHOT_ARGO_SCHEMA_MANIFEST"])
        schemas = {}
        for item in yaml.safe_load_all(rendered.read_text()):
            if item["kind"] != "CustomResourceDefinition" or item["spec"]["group"] != "argoproj.io":
                continue
            for version in item["spec"]["versions"]:
                if version["name"] == "v1alpha1" and version["served"]:
                    schemas[item["spec"]["names"]["kind"]] = version["schema"]["openAPIV3Schema"]
        self.assertTrue({"Application", "AppProject"} <= set(schemas), "Rendered Argo CRDs are missing")
        for item in yaml.safe_load_all((ROOT / "gitops/applications/railshot-platform.yaml").read_text()):
            with self.subTest(kind=item["kind"]):
                errors = sorted(Draft7Validator(schemas[item["kind"]]).iter_errors(item), key=lambda error: str(error.path))
                self.assertFalse(errors, "\n".join(f"{list(error.path)}: {error.message}" for error in errors))

    def test_pinned_images_private_api_and_configured_readiness(self):
        images = {name: f"ghcr.io/jasmin-softbank/railshot-{name}@sha256:" + "a" * 64 for name in ("dashboard", "api")}
        output = module.render(images, "k3s-aws")
        self.assertFalse(any(item['kind'] == 'Deployment' and item['metadata']['name'] == 'railshot-mcp' for item in output['items']))
        self.assertTrue(any(item['kind'] == 'Service' and item['metadata']['name'] == 'railshot-mcp' for item in output['items']))
        with_mcp = module.render({**images, 'mcp': f"ghcr.io/jasmin-softbank/railshot-mcp@sha256:{'b' * 64}"}, "k3s-aws")
        mcp = next(item for item in with_mcp['items'] if item['kind'] == 'Deployment' and item['metadata']['name'] == 'railshot-mcp')
        self.assertEqual(mcp['spec']['template']['spec']['containers'][0]['image'], f"ghcr.io/jasmin-softbank/railshot-mcp@sha256:{'b' * 64}")
        api = next(item for item in output["items"] if item["kind"] == "Deployment" and item["metadata"]["name"] == "railshot-api")
        self.assertFalse(any(item['name'] == 'personal-gateway' for item in api['spec']['template']['spec']['initContainers']))
        self.assertFalse(any(item['metadata']['name'].startswith('railshot-personal-') for item in output['items']))
        container = api["spec"]["template"]["spec"]["containers"][0]
        for item in output['items']:
            if item['kind'] == 'Deployment':
                self.assertEqual(item['spec']['template']['spec']['nodeSelector']['railshot.io/node-role'], 'platform')
        self.assertEqual(container["image"], images["api"])
        self.assertEqual(api['spec']['strategy']['type'], 'Recreate')
        self.assertEqual(api['spec']['template']['spec']['securityContext']['fsGroupChangePolicy'], 'OnRootMismatch')
        self.assertEqual(api['spec']['replicas'], 1)
        self.assertEqual(api['spec']['template']['spec']['initContainers'][0]['image'], images['api'])
        self.assertTrue(any(v.get('persistentVolumeClaim') for v in api['spec']['template']['spec']['volumes']))
        environment = {value['name']: value.get('value') for value in container['env']}
        self.assertNotIn('RAILSHOT_PROVIDER_TARGETS', environment)
        self.assertNotIn('RAILSHOT_TARGET_IDS', environment)
        self.assertEqual(environment['TMPDIR'], '/var/lib/railshot/tmp')
        self.assertEqual(environment['TF_PLUGIN_CACHE_DIR'], '/var/lib/railshot/provider-cache')
        for name in ('GOOGLE_APPLICATION_CREDENTIALS', 'CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE'):
            credential = next(value for value in container['env'] if value['name'] == name)
            self.assertEqual(credential['valueFrom']['configMapKeyRef'],
                             {'name': 'railshot-environments', 'key': 'google_credentials_file', 'optional': True})
        profiles = next(value for value in container['env'] if value['name'] == 'RAILSHOT_PROFILES_FILE')
        self.assertEqual(profiles['valueFrom']['configMapKeyRef'],
                         {'name': 'railshot-environments', 'key': 'profiles_file', 'optional': True})
        for entry in (container, api['spec']['template']['spec']['initContainers'][0]):
            observer = next(value for value in entry['env'] if value['name'] == 'RAILSHOT_OBSERVER_PRODUCT_FILE')
            self.assertEqual(observer['valueFrom']['configMapKeyRef'],
                             {'name': 'railshot-environments', 'key': 'observer_file', 'optional': True})
        self.assertEqual(environment['RAILSHOT_OBSERVER_CONFIG'], '/var/lib/railshot/config/observer.json')
        self.assertIn({'name': 'state', 'mountPath': '/var/lib/railshot'}, container['volumeMounts'])
        self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/readyz")
        self.assertEqual(container["readinessProbe"]["periodSeconds"], 5)
        self.assertEqual(container["livenessProbe"]["timeoutSeconds"], 5)
        self.assertTrue(all(item["spec"]["type"] == "ClusterIP" for item in output["items"] if item["kind"] == "Service"))
        public = module.render(images, "k3s-aws", 31080)
        services = {item["metadata"]["name"]: item["spec"] for item in public["items"] if item["kind"] == "Service"}
        self.assertEqual(services["railshot-dashboard"]["ports"][0]["nodePort"], 31080)
        self.assertEqual(services["railshot-api"]["type"], "ClusterIP")
        with self.assertRaises(ValueError):
            module.render(images, "k3s-aws", 443)
        for bad in ("ghcr.io/jasmin-softbank/railshot-api:latest", "attacker.invalid/api@sha256:" + "a" * 64):
            with self.assertRaises(ValueError):
                module.render({**images, "api": bad}, "k3s-aws")
        with self.assertRaises(ValueError):
            module.render(images, "../target")

    def test_personal_gateway_is_explicit_and_keeps_api_unprivileged(self):
        images = {name: f'ghcr.io/jasmin-softbank/railshot-{name}@sha256:' + 'a' * 64
                  for name in ('dashboard', 'api', 'personal-gateway')}
        output = module.render(images, 'k3s-aws', personal=True)
        api = next(item for item in output['items'] if item['kind'] == 'Deployment'
                   and item['metadata']['name'] == 'railshot-api')
        pod = api['spec']['template']['spec']
        gateway = next(item for item in pod['initContainers'] if item['name'] == 'personal-gateway')
        application = pod['containers'][0]
        self.assertEqual(gateway['securityContext']['capabilities'], {'drop': ['ALL'], 'add': ['NET_ADMIN']})
        self.assertEqual(gateway['securityContext']['runAsUser'], 0)
        self.assertEqual(gateway['securityContext']['runAsGroup'], 1000)
        self.assertEqual(application['securityContext']['capabilities'], {'drop': ['ALL']})
        self.assertEqual(application['securityContext']['allowPrivilegeEscalation'], False)
        self.assertNotIn('personal-gateway-key', {item['name'] for item in application['volumeMounts']})
        private_config = next(item for item in pod['initContainers'] if item['name'] == 'private-config')
        self.assertIn('personal-gateway-ipc', {item['name'] for item in private_config['volumeMounts']})
        self.assertIn('railshot-personal-gateway', {item['metadata']['name'] for item in output['items']
                                                    if item['kind'] == 'PersistentVolumeClaim'})
        policy = next(item for item in output['items'] if item['kind'] == 'NetworkPolicy'
                      and item['metadata']['name'] == 'railshot-api-private')
        self.assertTrue(any({'port': 51820, 'protocol': 'UDP'} in row.get('ports', [])
                            for row in policy['spec']['ingress']))

    def test_api_preparation_uses_incoming_image_on_the_existing_api_node_without_state_mounts(self):
        images = {name: f'ghcr.io/jasmin-softbank/railshot-{name}@sha256:' + 'a' * 64 for name in ('dashboard', 'api')}
        output = module.render(images, 'k3s-aws', prepare_api_rollout=True)
        job = next(item for item in output['items'] if item['kind'] == 'Job')
        self.assertEqual(job['metadata']['annotations']['argocd.argoproj.io/hook'], 'PreSync')
        pod = job['spec']['template']['spec']
        self.assertEqual(pod['containers'][0]['image'], images['api'])
        self.assertFalse(pod['automountServiceAccountToken'])
        self.assertEqual([volume['name'] for volume in pod['volumes']], ['api-token'])
        self.assertEqual(pod['affinity']['podAffinity']['requiredDuringSchedulingIgnoredDuringExecution'][0],
                         {'labelSelector': {'matchLabels': {'app': 'railshot-api'}}, 'topologyKey': 'kubernetes.io/hostname'})
        api = next(item for item in output['items'] if item['kind'] == 'Deployment' and item['metadata']['name'] == 'railshot-api')
        api_env = {value['name']: value.get('value') for value in api['spec']['template']['spec']['containers'][0]['env']}
        hook_env = {value['name']: value.get('value') for value in pod['containers'][0]['env']}
        self.assertEqual(api_env['RAILSHOT_POD_TEMPLATE_ID'], hook_env['RAILSHOT_DESIRED_TEMPLATE_ID'])
        self.assertRegex(hook_env['RAILSHOT_DESIRED_TEMPLATE_ID'], r'^[a-f0-9]{64}$')

    def test_optional_provider_targets_preserve_aws_and_render_matching_ci_admission(self):
        images = {name: f'ghcr.io/jasmin-softbank/railshot-{name}@sha256:' + 'a' * 64 for name in ('dashboard', 'api')}
        output = module.render(images, 'k3s-aws', provider_targets={'gcp': 'k3s-gcp', 'openstack': 'k3s-openstack'})
        containers = {item['metadata']['name']: item['spec']['template']['spec']['containers'][0]
                      for item in output['items'] if item['kind'] == 'Deployment'}
        entries = containers['railshot-api']['env']
        env = {item['name']: item.get('value') for item in entries}
        self.assertEqual(len(entries), len(env))
        self.assertEqual(env['RAILSHOT_TARGET_ID'], 'k3s-aws')
        self.assertEqual(env['RAILSHOT_TARGET_PROVIDER'], 'aws')
        self.assertEqual(json.loads(env['RAILSHOT_PROVIDER_TARGETS']), {'gcp': 'k3s-gcp', 'openstack': 'k3s-openstack'})
        self.assertEqual(env['RAILSHOT_TARGET_IDS'], 'k3s-aws,k3s-gcp,k3s-openstack')
        self.assertFalse(any(item['name'] in {'RAILSHOT_PROVIDER_TARGETS', 'RAILSHOT_TARGET_IDS'}
                             for item in containers['railshot-dashboard'].get('env', [])))
        for invalid in ([], 'openstack', {'unknown': 'k3s-unknown'}, {'openstack': '../target'}, {'openstack': 1},
                        {'aws': 'replacement'}, {'openstack': 'k3s-aws'}, {'openstack': 'shared', 'proxmox': 'shared'}):
            with self.subTest(provider_targets=invalid), self.assertRaises(ValueError):
                module.render(images, 'k3s-aws', provider_targets=invalid)
