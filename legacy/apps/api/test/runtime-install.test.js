import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFileSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../../../', import.meta.url));
const smoke = join(root, 'apps/api/runtime-smoke.py');

test('API runtime pins reuse CI dependencies and the K3s version policy', () => {
  const requirements = execFileSync('python3', [smoke, '--requirements'], { encoding: 'utf8' }).trim().split('\n');
  assert.equal(requirements.length, 3);
  assert.match(requirements[0], /^ansible-core==/);
  const rows = execFileSync('python3', [smoke, '--downloads'], { encoding: 'utf8' }).trim().split('\n');
  assert.equal(rows.length, 5); // Real manifest parser rejects nonofficial URLs, mutable versions and malformed checksums.
  const manifest = JSON.parse(readFileSync(join(root, 'apps/api/runtime-tools.json')));
  const workflow = readFileSync(join(root, '.github/workflows/platform-checks.yml'), 'utf8');
  assert.ok(workflow.includes(`terraform_version: ${manifest.tools.terraform.version}`));
  execFileSync('bash', ['-n', join(root, 'apps/api/runtime-install.sh')]);
  assert.throws(() => execFileSync('bash', [join(root, 'apps/api/runtime-install.sh')], {
    env: { ...process.env, RAILSHOT_CONTAINER_BUILD: '0' }, stdio: 'pipe',
  }), /API image build/); // No apt or downloaded executable is started outside the explicit image-build mode.
});

test('runtime download verification detects altered artifact bytes', () => {
  const directory = mkdtempSync(join(tmpdir(), 'railshot-runtime-check-'));
  try {
    writeFileSync(join(directory, 'tool'), 'verified bytes');
    const script = `import importlib.util, hashlib\ns=importlib.util.spec_from_file_location('smoke', ${JSON.stringify(smoke)})\nm=importlib.util.module_from_spec(s);s.loader.exec_module(m)\nrows={'tool':{'filename':'tool','sha256':hashlib.sha256(b'verified bytes').hexdigest()}}\nm.verify_artifacts(${JSON.stringify(directory)},rows)\n`;
    execFileSync('python3', ['-c', script]);
    writeFileSync(join(directory, 'tool'), 'modified bytes');
    assert.throws(() => execFileSync('python3', ['-c', script], { stdio: 'pipe' }), /checksum differs/);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});
