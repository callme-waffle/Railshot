"""Fail when deployment defaults cease to be user-overridable or unsafe TLS is added."""
from pathlib import Path
import re
root = Path(__file__).resolve().parents[2]
ansible = root / "infrastructure/ansible"
for path in ansible.rglob("*.yml"):
    text = path.read_text()
    assert not re.search(r"validate_certs:\s*(false|no)", text), str(path)
for path in (ansible / "playbooks").glob("*.yml"):
    assert "vars_files:" not in path.read_text(), f"Defaults must not override inventory: {path}"
text = (ansible / "roles/deployment_defaults/defaults/main.yml").read_text()
assert "replication_mode: synchronous" in text
assert "synchronous_mode_strict: true" in text
assert "backup_enabled: false" in text
print("Contract guards passed (not a live deployment test).")
