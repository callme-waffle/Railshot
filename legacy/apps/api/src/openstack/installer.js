import { createHash } from 'node:crypto';
import { lstat, readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import yazl from 'yazl';

const repository = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', '..');
// Match install.sh's required_files exactly. This allowlist prevents packaging unrelated
// deployment files or a private file accidentally left in the source checkout.
const required = [
  'deployment/bootstrap/install.sh',
  'deployment/bootstrap/uninstall.sh',
  'deployment/bootstrap/install_payload.py',
  'deployment/bootstrap/requirements.lock',
  'deployment/bootstrap/client_setup/__init__.py',
  'deployment/bootstrap/client_setup/main.py',
  'deployment/bootstrap/client_setup/preflight.py',
  'deployment/bootstrap/client_setup/wireguard.py',
  'deployment/bootstrap/client_setup/credentials.py',
  'deployment/bootstrap/client_setup/state.py',
  'deployment/bootstrap/client_setup/report.py',
  'deployment/bootstrap/templates/agent-authorized-keys.README',
  'infrastructure/providers/openstack/__init__.py',
  'infrastructure/providers/openstack/cli.py',
  'infrastructure/providers/openstack/identity.py',
  'infrastructure/providers/openstack/discovery.py',
  'infrastructure/providers/openstack/access.py',
  'infrastructure/providers/openstack/templates/cloud-init.yaml.tmpl',
  'apps/agent/__init__.py',
  'apps/agent/install_forced_command.py',
  'apps/agent/protocol.py',
  'apps/agent/runner.py',
  'apps/agent/sender.py',
];
const tokenClientPath = 'deployment/bootstrap/claim_token.py';

export async function buildOpenStackInstaller(root = repository) {
  const script = await readFile(join(root, required[0]), 'utf8');
  const declaration = /^required_files=\(\s*\n([\s\S]*?)^\)/m.exec(script);
  const declared = declaration?.[1].trim().split(/\s+/);
  if (!declared || declared.length !== required.length || required.some((path, index) => path !== declared[index])) {
    throw new Error('OpenStack installer file list changed; review the package allowlist');
  }
  const zip = new yazl.ZipFile();
  let total = 0;
  for (const name of required) {
    const path = join(root, name);
    const metadata = await lstat(path);
    if (!metadata.isFile() || metadata.nlink !== 1) throw new Error('Unsafe installer source');
    const bytes = await readFile(path);
    total += bytes.length;
    if (total > 2 * 1024 * 1024) throw new Error('OpenStack installer package is too large');
    zip.addBuffer(bytes, name, { mtime: new Date('1980-01-01T00:00:00Z'), mode: 0o644 });
  }
  const tokenClientFile = join(root, tokenClientPath);
  const tokenClientMetadata = await lstat(tokenClientFile);
  if (!tokenClientMetadata.isFile() || tokenClientMetadata.nlink !== 1) throw new Error('Unsafe token client source');
  const tokenClient = await readFile(tokenClientFile, 'utf8');
  total += Buffer.byteLength(tokenClient);
  if (total > 2 * 1024 * 1024) throw new Error('OpenStack installer package is too large');
  zip.addBuffer(Buffer.from(tokenClient), tokenClientPath, { mtime: new Date('1980-01-01T00:00:00Z'), mode: 0o644 });
  zip.end();
  const chunks = [];
  for await (const chunk of zip.outputStream) chunks.push(chunk);
  const archive = Buffer.concat(chunks);
  return { archive, sha256: createHash('sha256').update(archive).digest('hex'), script, tokenClient };
}
