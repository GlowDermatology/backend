"""Secrets masked out of the JSON documents the ops console shows (``glowderma.core.admin``).

Outbox payloads, job parameters and results, provider state, webhook rows and audit before/after documents
are JSON written by many modules; a few carry credentials on purpose (``apple.revoke``'s ``sealed_token``,
``push.account_security``'s ``farewell_tokens``, ``push.session_replaced``'s ``fcm_token``, Click's
``sign_string``). :func:`mask_secrets` copies a document with the value of every *secret-like key* replaced
by :data:`MASK`, at any depth (inside lists too). The decision is made on the key alone, never by guessing
from a value:

* a key is split into lowercase words (``farewell_tokens`` → farewell, tokens; ``X-GD-Signature`` → x, gd,
  signature; ``fcmToken`` → fcm, token); a plural ``s`` is ignored;
* a word in :data:`SECRET_WORDS` makes the key secret wherever it stands (``sealed_token``, ``key_hash``,
  ``HTTP_AUTHORIZATION``, ``code_hmac``);
* ``key`` and ``code`` are secret only as the *last* word after a qualifier of :data:`QUALIFIED_WORDS`
  (``api_key``, ``x-api-key``, ``verification_code``) — so ``dedupe_key``, ``error_code``, ``sms_code_id`` or
  Payme's ``code`` stay readable; :data:`SECRET_KEYS` lists whole keys that are secret on their own (``key``,
  ``sign``);
* ids, statuses, amounts and the rest (``order_id``, ``refund_id``, ``event``, ``click_paydoc_id``,
  ``sign_time``) are shown as they are.

An empty value (``None``, ``""``, ``[]``, ``{}``) and a boolean are shown as they are: they reveal nothing and
tell the operator whether a token was there at all.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

MASK = "«yashirilgan»"

SECRET_WORDS: frozenset[str] = frozenset(
    {
        "apikey",
        "assertion",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "cvc",
        "cvv",
        "fcm",
        "hash",
        "hmac",
        "jwt",
        "otp",
        "passphrase",
        "passwd",
        "password",
        "pin",
        "privatekey",
        "pwd",
        "refresh",
        "salt",
        "sealed",
        "secret",
        "signature",
        "token",
    }
)
# ``<qualifier>_<word>`` with the word last: secret (``api_key``, ``auth_code``); any other ``…_key`` or
# ``…_code`` is an identifier or a status (``dedupe_key``, ``error_code``).
QUALIFIED_WORDS: dict[str, frozenset[str]] = {
    "key": frozenset(
        {
            "access",
            "api",
            "auth",
            "client",
            "device",
            "encryption",
            "hmac",
            "master",
            "merchant",
            "partner",
            "private",
            "secret",
            "session",
            "shared",
            "signing",
        }
    ),
    "code": frozenset(
        {
            "access",
            "auth",
            "authorization",
            "confirmation",
            "email",
            "login",
            "recovery",
            "sms",
            "verification",
        }
    ),
}
# Whole keys (words joined by ``_``) that are secret on their own.
SECRET_KEYS: frozenset[str] = frozenset({"auth", "key", "sign", "sign_string", "x_auth"})

_WORDS = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def key_words(key: str) -> list[str]:
    """``key`` as lowercase singular words (camelCase, ``-``, ``_`` and spaces split)."""
    return [_singular(word.lower()) for word in _WORDS.findall(key)]


def is_secret_key(key: Any) -> bool:
    """Whether the value under ``key`` is masked (module doc)."""
    words = key_words(str(key))
    if not words:
        return False
    if "_".join(words) in SECRET_KEYS or any(word in SECRET_WORDS for word in words):
        return True
    last = words[-1]
    qualifiers = QUALIFIED_WORDS.get(last)
    return qualifiers is not None and any(word in qualifiers for word in words[:-1])


def _is_empty(value: Any) -> bool:
    return value is None or isinstance(value, bool) or (isinstance(value, str | list | dict) and not value)


def mask_secrets(value: Any) -> Any:
    """A copy of the JSON document ``value`` with the value of every secret-like key replaced by
    :data:`MASK` (nested mappings and lists included); everything else unchanged."""
    if isinstance(value, Mapping):
        return {
            key: (MASK if is_secret_key(key) and not _is_empty(item) else mask_secrets(item))
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [mask_secrets(item) for item in value]
    return value
