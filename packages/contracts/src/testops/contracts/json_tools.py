"""Canonical JSON helpers used for immutable contract digests."""

from __future__ import annotations

import json
from typing import Any


def canonical_json_bytes(value: object) -> bytes:
    """Serialize a model or JSON value deterministically for hashing."""

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)  # type: ignore[union-attr]
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Return a prefixed SHA-256 digest for canonical JSON."""

    import hashlib

    return f"sha256:{hashlib.sha256(canonical_json_bytes(value)).hexdigest()}"
