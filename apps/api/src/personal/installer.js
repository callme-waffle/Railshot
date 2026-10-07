import { createHash } from 'node:crypto';

const quote = (value) => `'${String(value).replaceAll("'", "'\\''")}'`;

export function installerScript(config) {
  if (!config?.public_url || !config?.artifact_url || !/^[a-f0-9]{64}$/i.test(config.artifact_sha256 || '')) throw new Error('Installer release is not configured');
  const protocols = config.test_allow_http === true ? '=http,https' : '=https';
  return `#!/usr/bin/env bash
set -euo pipefail
umask 077
read -r -p 'Railshot enrollment ID: ' enrollment_id </dev/tty
[[ "$enrollment_id" =~ ^[a-f0-9-]{36}$ ]] || { echo 'Invalid enrollment ID' >&2; exit 1; }
read -r -s -p 'Railshot one-time enrollment token: ' RAILSHOT_ENROLLMENT_TOKEN </dev/tty
printf '\\n' >/dev/tty
export RAILSHOT_ENROLLMENT_TOKEN
work_dir=$(mktemp -d)
trap 'rm -rf -- "$work_dir"; unset RAILSHOT_ENROLLMENT_TOKEN' EXIT
curl --disable --proto '${protocols}' --max-redirs 0 --fail --silent --show-error ${quote(config.artifact_url)} -o "$work_dir/release.tgz"
python3 - "$work_dir" ${quote(config.artifact_sha256.toLowerCase())} <<'PY'
import hashlib, pathlib, sys, tarfile
root = pathlib.Path(sys.argv[1]); archive = root / 'release.tgz'
if hashlib.sha256(archive.read_bytes()).hexdigest() != sys.argv[2]:
    raise SystemExit('Release digest mismatch')
with tarfile.open(archive) as source:
    members = source.getmembers()
    if any(m.issym() or m.islnk() or m.isdev() or not (m.isfile() or m.isdir())
           or m.name.startswith('/') or '..' in pathlib.PurePosixPath(m.name).parts for m in members):
        raise SystemExit('Unsafe release archive')
    source.extractall(root / 'source', filter='data')
PY
sudo --preserve-env=RAILSHOT_ENROLLMENT_TOKEN bash "$work_dir/source/deployment/bootstrap/install.sh" --personal-registration --api-url ${quote(config.public_url)} --enrollment-id "$enrollment_id" --source-root "$work_dir/source"${config.test_allow_http === true ? ' --test-allow-http' : ''}
`;
}
export function installerDigest(script) { return createHash('sha256').update(script).digest('hex'); }
