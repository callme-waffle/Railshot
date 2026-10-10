"""Only allow structured non-secret diagnostics; do not print exception bodies."""
import re

_SECRET = re.compile(r"password|passwd|secret|token|private.?key|authorization|registration.?key|credential", re.I)


def sanitize(value):
    if isinstance(value, dict):
        return {str(key): "[REDACTED]" if _SECRET.search(str(key)) else sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return "[UNSUPPORTED]"


def installation_report(state, capabilities, access):
    return sanitize({"installation": state.get("stages", {}), "capabilities": capabilities, "vm_access": access,
                     "limitations": ["VM connectivity requires separate verify-vm execution", "No continuous command agent is installed", "No network tunnel or remote management route is configured"]})
