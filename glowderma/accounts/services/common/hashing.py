"""Hashing of the secrets the accounts module stores only as digests: refresh tokens, device keys,
verification tokens and password-link tokens.
"""

from __future__ import annotations

import hashlib


def hash_secret(raw: str) -> str:
    """SHA-256 hex of a secret kept only as a digest: refresh tokens (staff and customer), device keys,
    verification and password-link tokens, and the social replay-guard cache keys."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = ["hash_secret"]
