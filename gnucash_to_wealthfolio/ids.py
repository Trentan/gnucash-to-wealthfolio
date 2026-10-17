import hashlib
import uuid

GNUCASH_NAMESPACE = uuid.UUID("7f3b9a10-6c44-4d8e-9a12-5b8d4e1f2c3a")


def stable_id(prefix: str, parts: list[str]) -> str:
    """Compute a 64-char SHA-256 hex digest matching Wealthfolio's stable_id utility."""
    hasher = hashlib.sha256()
    hasher.update(prefix.encode("utf-8"))
    for part in parts:
        hasher.update(b":")
        hasher.update(part.encode("utf-8"))
    return hasher.hexdigest()


def deterministic_uuid(namespace_tag: str, key: str) -> str:
    """Compute a deterministic UUIDv5 string for a GnuCash entity."""
    return str(uuid.uuid5(GNUCASH_NAMESPACE, f"{namespace_tag}:{key}"))
