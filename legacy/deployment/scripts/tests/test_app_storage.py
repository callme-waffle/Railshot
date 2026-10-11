"""Verify the renamed provisioner and helper resolve the same owned resources."""
from pathlib import Path
import json
import unittest

import yaml


class AppStorageManifest(unittest.TestCase):
    def test_driver_and_helper_references_and_retained_volume_root(self):
        path = Path(__file__).resolve().parents[2] / 'manifests/gcp-app-storage.yaml'
        objects = list(yaml.safe_load_all(path.read_text()))
        by_kind = {item['kind']: item for item in objects}
        pod = by_kind['Deployment']['spec']['template']['spec']
        driver = pod['containers'][0]
        args = driver['command']
        config = by_kind['ConfigMap']
        self.assertEqual(args[args.index('--configmap-name') + 1], config['metadata']['name'])
        self.assertEqual(args[args.index('--service-account-name') + 1], pod['serviceAccountName'])
        self.assertEqual(pod['serviceAccountName'], by_kind['ServiceAccount']['metadata']['name'])
        self.assertEqual(pod['volumes'][0]['configMap']['name'], config['metadata']['name'])
        helper = yaml.safe_load(config['data']['helperPod.yaml'])
        self.assertEqual(args[args.index('--helper-image') + 1], helper['spec']['containers'][0]['image'])
        paths = json.loads(config['data']['config.json'])['nodePathMap']
        self.assertEqual(paths, [
            {'node': 'DEFAULT_PATH_FOR_NON_LISTED_NODES', 'paths': []},
            {'node': 'railshot-gcp-poc', 'paths': ['/var/lib/rancher/railshot-volumes']}])
        storage = by_kind['StorageClass']
        self.assertEqual(storage['metadata']['annotations']['defaultVolumeType'], 'local')
        self.assertEqual(storage['volumeBindingMode'], 'WaitForFirstConsumer')
        for item in objects:
            self.assertEqual(item['metadata']['labels']['app.kubernetes.io/managed-by'], 'railshot')


if __name__ == '__main__':
    unittest.main()
