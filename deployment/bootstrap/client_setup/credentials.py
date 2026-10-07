"""Customer-local credentials. Remote control requires a server-assisted unlock."""
import base64
import hashlib
import json
import os
from pathlib import Path
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from .state import atomic_private_write, read_private


def _b64(value):
    return base64.b64encode(value).decode('ascii')


def _unb64(value):
    if not isinstance(value, str):
        raise ValueError('VAULT_INVALID')
    return base64.b64decode(value, validate=True)


def vault_aad(target_id, generation, project_id):
    if (not all(isinstance(x, str) and x for x in (target_id, project_id))
            or type(generation) is not int or generation < 1):
        raise ValueError('VAULT_BINDING_INVALID')
    return json.dumps({'generation': generation, 'project_id': project_id,
                       'target_id': target_id}, sort_keys=True, separators=(',', ':')).encode()


def validate_server_key(public_key_pem, key_id):
    public = serialization.load_pem_public_key(public_key_pem.encode() if isinstance(public_key_pem, str) else public_key_pem)
    if not isinstance(public, rsa.RSAPublicKey) or public.key_size < 3072:
        raise ValueError('VAULT_PUBLIC_KEY_INVALID')
    observed_id = hashlib.sha256(public.public_bytes(serialization.Encoding.DER,
                        serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()
    if observed_id != key_id:
        raise ValueError('VAULT_PUBLIC_KEY_MISMATCH')
    return public


class AssistedCredentialStore:
    """AES-GCM auth; its DEK is RSA-wrapped for the enrolled backend only."""
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.path = self.directory / 'credentials.vault'

    def save(self, auth, *, target_id, generation, project_id, public_key_pem, key_id, data_key=None):
        if self.path.exists() or self.path.is_symlink():
            raise ValueError('VAULT_ALREADY_EXISTS')
        if (self.directory / 'credentials.key').exists() or (self.directory / 'credentials.enc').exists():
            raise ValueError('LEGACY_VAULT_REQUIRES_MIGRATION')
        public = validate_server_key(public_key_pem, key_id)
        aad = vault_aad(target_id, generation, project_id)
        dek, nonce = (os.urandom(32) if data_key is None else data_key), os.urandom(12)
        if not isinstance(dek, bytes) or len(dek) != 32:
            raise ValueError('VAULT_DATA_KEY_INVALID')
        ciphertext = AESGCM(dek).encrypt(nonce, json.dumps(auth, sort_keys=True,
                                             separators=(',', ':')).encode(), aad)
        wrapped = public.encrypt(dek, padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()),
                                       algorithm=hashes.SHA256(), label=None))
        document = {'version': 2, 'target_id': target_id, 'generation': generation,
                    'project_id': project_id, 'key_id': key_id, 'nonce': _b64(nonce),
                    'ciphertext': _b64(ciphertext), 'wrapped_dek': _b64(wrapped)}
        atomic_private_write(self.path, json.dumps(document, sort_keys=True).encode())
        return self.metadata()

    def document(self):
        doc = json.loads(read_private(self.path))
        if not isinstance(doc, dict) or set(doc) != {'version', 'target_id', 'generation',
                'project_id', 'key_id', 'nonce', 'ciphertext', 'wrapped_dek'} or doc['version'] != 2:
            raise ValueError('VAULT_INVALID')
        return doc

    def metadata(self):
        doc = self.document()
        return {key: doc[key] for key in ('generation', 'project_id', 'key_id', 'wrapped_dek')}

    def unlock(self, unlock_key, *, target_id, generation, project_id):
        doc = self.document()
        if (doc['target_id'], doc['generation'], doc['project_id']) != (target_id, generation, project_id):
            raise ValueError('VAULT_BINDING_INVALID')
        key = _unb64(unlock_key)
        if len(key) != 32:
            raise ValueError('VAULT_UNLOCK_INVALID')
        nonce, ciphertext = _unb64(doc['nonce']), _unb64(doc['ciphertext'])
        if len(nonce) != 12:
            raise ValueError('VAULT_INVALID')
        return json.loads(AESGCM(key).decrypt(nonce, ciphertext,
                          vault_aad(target_id, generation, project_id)))


class CredentialStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.key_path = directory / "credentials.key"
        self.path = directory / "credentials.enc"

    def save(self, auth: dict):
        if self.key_path.exists() or self.key_path.is_symlink():
            key = read_private(self.key_path)
        else:
            if self.path.exists():
                raise ValueError("Encrypted credentials exist but key is missing")
            key = Fernet.generate_key()
            atomic_private_write(self.key_path, key)
        atomic_private_write(self.path, Fernet(key).encrypt(json.dumps(auth).encode()))

    def load(self) -> dict:
        return json.loads(Fernet(read_private(self.key_path)).decrypt(read_private(self.path)))
