"""AES-256-GCM sealing of small secrets at rest (customer contract §2.1.1, §2.1.8).

``seal(plaintext, aad=...)`` returns ``base64url(nonce ‖ ciphertext ‖ tag)`` (no padding) with a random 96-bit
nonce under ``settings.SECRETBOX_KEY`` (base64url of exactly 32 bytes, plan §7); ``open_(token, aad=...)``
reverses it. The associated data binds a ciphertext to its row (``sms_code_id``, the social identity), so a
token copied to another row does not open. Used for the OTP payload in Redis and Apple's refresh token.
"""

from __future__ import annotations

import base64
import binascii
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

NONCE_BYTES = 12
TAG_BYTES = 16
KEY_BYTES = 32
# The base64url alphabet only (plan §7): ``b64decode(altchars=b"-_")`` alone would also accept "+" and "/".
BASE64URL = re.compile(r"[A-Za-z0-9_-]+={0,2}")


class SecretboxError(Exception):
    """The token is malformed, was tampered with, or was sealed under another key or associated data."""


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def decode_key(encoded: str) -> bytes:
    """The raw key of a base64url ``SECRETBOX_KEY`` (padding optional); exactly 32 bytes or ``ValueError``."""
    if not BASE64URL.fullmatch(encoded):
        raise ValueError("SECRETBOX_KEY must be base64url")
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("SECRETBOX_KEY must be base64url") from exc
    if len(raw) != KEY_BYTES:
        raise ValueError("SECRETBOX_KEY must decode to exactly 32 bytes")
    return raw


def _cipher() -> AESGCM:
    try:
        return AESGCM(decode_key(str(settings.SECRETBOX_KEY)))
    except ValueError as exc:
        raise ImproperlyConfigured(str(exc)) from exc


def seal(plaintext: bytes, *, aad: bytes) -> str:
    nonce = os.urandom(NONCE_BYTES)
    sealed = _cipher().encrypt(nonce, plaintext, aad)
    return base64.urlsafe_b64encode(nonce + sealed).rstrip(b"=").decode("ascii")


def open_(token: str, *, aad: bytes) -> bytes:
    try:
        raw = _b64decode(token)
    except (binascii.Error, ValueError) as exc:
        raise SecretboxError("malformed token") from exc
    if len(raw) < NONCE_BYTES + TAG_BYTES:  # exactly nonce + tag is a sealed empty plaintext
        raise SecretboxError("malformed token")
    try:
        return _cipher().decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], aad)
    except InvalidTag as exc:
        raise SecretboxError("token does not open") from exc
