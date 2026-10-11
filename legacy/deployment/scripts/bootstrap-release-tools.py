#!/usr/bin/env python3
"""Install pinned native release tools once, in an isolated prefix on the control host."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
PREFIX = Path('/opt/railshot-release')


def checked(body, expected):
    if hashlib.sha256(body).hexdigest() != expected:
        raise ValueError('RELEASE_TOOL_CHECKSUM_MISMATCH')
    return body


def install():
    if os.geteuid() != 0 or platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise ValueError('LINUX_AMD64_CONTROL_ROOT_REQUIRED')
    if not all(shutil.which(name) for name in ('unzip', 'dpkg-deb', 'tar')):
        raise ValueError('INSTALL_UNZIP_DPKG_TAR_BEFORE_BOOTSTRAP')
    tools = json.loads((ROOT / 'apps/api/runtime-tools.json').read_text())['tools']
    selected = {key: tools[key] for key in ('terraform', 'aws', 'kubectl', 'ssm', 'gcloud')}
    PREFIX.mkdir(mode=0o755, exist_ok=True)
    if PREFIX.resolve() != PREFIX or PREFIX.stat().st_uid != 0 or PREFIX.stat().st_mode & 0o022:
        raise ValueError('RELEASE_TOOL_PREFIX_NOT_OWNED')
    receipt = PREFIX / 'tools.json'
    if receipt.exists():
        if json.loads(receipt.read_text()) != selected:
            raise ValueError('RELEASE_TOOL_UPGRADE_REQUIRES_REVIEW')
    else:
        # Never overwrite an unknown or partially installed tool set.
        if (PREFIX / 'bin').exists() or (PREFIX / 'aws-cli').exists():
            raise ValueError('RELEASE_TOOL_RECONCILIATION_REQUIRED')
        with tempfile.TemporaryDirectory(prefix='download-', dir=PREFIX) as directory:
            temporary = Path(directory)
            for key, tool in selected.items():
                with urlopen(tool['url'], timeout=90) as response:
                    body = response.read(500_000_001)
                if len(body) > 500_000_000:
                    raise ValueError('RELEASE_TOOL_DOWNLOAD_TOO_LARGE')
                (temporary / tool['filename']).write_bytes(checked(body, tool['sha256']))
            (PREFIX / 'bin').mkdir(mode=0o755)
            subprocess.run(['unzip', '-q', str(temporary / 'terraform.zip'), '-d', str(temporary / 'terraform')], check=True)
            for key, origin in [('terraform', temporary / 'terraform/terraform'), ('kubectl', temporary / 'kubectl')]:
                shutil.copyfile(origin, PREFIX / 'bin' / key); (PREFIX / 'bin' / key).chmod(0o755)
            subprocess.run(['unzip', '-q', str(temporary / 'aws.zip'), '-d', directory], check=True)
            subprocess.run([str(temporary / 'aws/install'), '--install-dir', str(PREFIX / 'aws-cli'), '--bin-dir', str(PREFIX / 'bin')], check=True)
            subprocess.run(['dpkg-deb', '-x', str(temporary / 'ssm.deb'), str(PREFIX / 'ssm')], check=True)
            (PREFIX / 'bin/session-manager-plugin').symlink_to(PREFIX / 'ssm/usr/local/sessionmanagerplugin/bin/session-manager-plugin')
            subprocess.run(['tar', '-xzf', str(temporary / 'gcloud.tar.gz'), '-C', str(PREFIX)], check=True)
            (PREFIX / 'bin/gcloud').symlink_to(PREFIX / 'google-cloud-sdk/bin/gcloud')
            receipt.write_text(json.dumps(selected, sort_keys=True) + '\n'); receipt.chmod(0o644)
    for key, args in [('terraform', ['version', '-json']), ('aws', ['--version']), ('kubectl', ['version', '--client=true', '-o', 'json']), ('ssm', ['--version']), ('gcloud', ['version', '--format=json'])]:
        result = subprocess.check_output([str(PREFIX / 'bin' / ('session-manager-plugin' if key == 'ssm' else key)), *args], text=True)
        if selected[key]['version'] not in result:
            raise ValueError('RELEASE_TOOL_VERSION_MISMATCH')
    print(json.dumps({'status': 'verified', 'versions': {key: value['version'] for key, value in selected.items()}}))


if __name__ == '__main__':
    install()
