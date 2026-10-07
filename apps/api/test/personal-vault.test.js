import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { constants, generateKeyPairSync, publicEncrypt, randomBytes } from 'node:crypto';
import { mkdtemp, writeFile, chmod, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { createPersonalVault } from '../src/personal-vault.js';

test('personal vault publishes a stable key ID and unwraps only its OAEP data key', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-vault-'));
  try {
    const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 3072 });
    const path = join(directory, 'private.pem');
    await writeFile(path, privateKey.export({ format: 'pem', type: 'pkcs8' }), { mode: 0o600 });
    const vault = await createPersonalVault(path);
    const announced = vault.publicKey();
    assert.match(announced.vault_key_id, /^[0-9a-f]{64}$/);
    assert.match(announced.vault_public_key, /BEGIN PUBLIC KEY/);
    const dek = randomBytes(32);
    const wrapped_dek = publicEncrypt({ key: publicKey, padding: constants.RSA_PKCS1_OAEP_PADDING, oaepHash: 'sha256' }, dek).toString('base64');
    const metadata = { key_id: announced.vault_key_id, wrapped_dek };
    assert.equal(vault.validate(metadata), true);
    assert.equal(vault.unwrap(metadata), dek.toString('base64'));
    assert.throws(() => vault.unwrap({ ...metadata, key_id: '0'.repeat(64) }), /metadata invalid/);
    assert.throws(() => vault.unwrap({ ...metadata, wrapped_dek: wrapped_dek.slice(0, -4) + 'AAAA' }), /unlock failed/);
    await chmod(path, 0o644);
    await assert.rejects(createPersonalVault(path), /Invalid private/);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('Python client AES-GCM vault interoperates with backend RSA unlock', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'railshot-vault-cross-'));
  try {
    const { privateKey } = generateKeyPairSync('rsa', { modulusLength: 3072 });
    const path = join(directory, 'private.pem');
    await writeFile(path, privateKey.export({ format: 'pem', type: 'pkcs8' }), { mode: 0o600 });
    const backend = await createPersonalVault(path);
    const announced = backend.publicKey();
    const python = `import json, os, sys
sys.path.insert(0, os.path.join(os.getcwd(), 'deployment/bootstrap'))
from client_setup.credentials import AssistedCredentialStore
from pathlib import Path
request = json.load(sys.stdin)
store = AssistedCredentialStore(Path(request['directory']))
if request['action'] == 'save':
    print(json.dumps(store.save({'application_credential_secret': 'secret'}, target_id='personal-test',
        generation=1, project_id='project-test', public_key_pem=request['public_key'], key_id=request['key_id'])))
else:
    value = store.unlock(request['unlock_key'], target_id='personal-test', generation=1, project_id='project-test')
    print('ok' if value == {'application_credential_secret': 'secret'} else 'bad')
`;
    const call = (data) => spawnSync('python3', ['-c', python], { input: JSON.stringify({ directory, ...data }), encoding: 'utf8' });
    const saved = call({ action: 'save', public_key: announced.vault_public_key, key_id: announced.vault_key_id });
    assert.equal(saved.status, 0, saved.stderr);
    const unlockKey = backend.unwrap(JSON.parse(saved.stdout));
    const opened = call({ action: 'open', unlock_key: unlockKey });
    assert.equal(opened.status, 0, opened.stderr);
    assert.equal(opened.stdout.trim(), 'ok');
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
