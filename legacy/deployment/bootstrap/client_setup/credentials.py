"""Local encrypted credential vault. Same-host root compromise is out of scope."""
import json
from pathlib import Path
from cryptography.fernet import Fernet
from .state import atomic_private_write, read_private


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
