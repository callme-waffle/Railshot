import { createHash, createPrivateKey, createPublicKey, privateDecrypt, constants } from 'node:crypto';
import { lstat, readFile } from 'node:fs/promises';

const KEY_ID = /^[a-f0-9]{64}$/;
const WRAPPED = /^[A-Za-z0-9+/]+={0,2}$/;

/** Backend-only RSA key used to unlock a customer-host credential for one job. */
export async function createPersonalVault(privateKeyFile) {
  const info = await lstat(privateKeyFile);
  if (!info.isFile() || info.uid !== process.getuid() || info.mode & 0o077 || info.size > 16 * 1024) {
    throw new Error('Invalid private personal vault key');
  }
  const privateKey = createPrivateKey(await readFile(privateKeyFile));
  if (privateKey.asymmetricKeyType !== 'rsa' || (privateKey.asymmetricKeyDetails?.modulusLength || 0) < 3072) {
    throw new Error('Personal vault requires RSA 3072 or stronger');
  }
  const publicKey = createPublicKey(privateKey);
  const der = publicKey.export({ type: 'spki', format: 'der' });
  const keyId = createHash('sha256').update(der).digest('hex');
  const pem = publicKey.export({ type: 'spki', format: 'pem' }).toString();

  function unwrap(metadata) {
    if (!metadata || typeof metadata !== 'object' || metadata.key_id !== keyId
        || !KEY_ID.test(metadata.key_id) || typeof metadata.wrapped_dek !== 'string'
        || !WRAPPED.test(metadata.wrapped_dek) || metadata.wrapped_dek.length > 2048) {
      throw new Error('Personal vault metadata invalid');
    }
    const encoded = Buffer.from(metadata.wrapped_dek, 'base64');
    if (encoded.toString('base64') !== metadata.wrapped_dek) throw new Error('Personal vault encoding invalid');
    let key;
    try {
      key = privateDecrypt({ key: privateKey, padding: constants.RSA_PKCS1_OAEP_PADDING, oaepHash: 'sha256' }, encoded);
      if (key.length !== 32) throw new Error('Personal vault data key invalid');
      return key.toString('base64');
    } catch {
      throw new Error('Personal vault unlock failed');
    } finally {
      key?.fill(0);
    }
  }

  return {
    publicKey: () => ({ vault_public_key: pem, vault_key_id: keyId }),
    validate: (metadata) => { unwrap(metadata); return true; },
    unwrap,
  };
}
