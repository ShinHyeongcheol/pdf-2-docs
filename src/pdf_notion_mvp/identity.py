import hashlib
import json


def operation_key(fingerprint: str, section_id: str, block_id: str) -> str:
    canonical = json.dumps([fingerprint, section_id, block_id], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()
