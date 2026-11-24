"""Customer password checks: the constant-time login comparison and the contract's password rules."""

from __future__ import annotations

import re
import secrets
from difflib import SequenceMatcher
from typing import Any

from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.hashers import check_password, make_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Customer
from glowderma.core.api.exceptions import (
    field_error,
)
from glowderma.customers.services.common import identifiers


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = make_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


_DUMMY_HASH: str | None = None


def check_login_password(customer: Customer | None, raw_password: str) -> bool:
    """Exactly one hash for every phone state (customer contract §2.1.4 step 4, §6.5): the stored hash, or a
    dummy Argon2 check when none is compared (unknown phone, deleted customer, social-only account)."""
    if customer is None or not customer.password:
        check_password(raw_password, _dummy_hash())
        return False
    return account_services.check_customer_password(customer, raw_password)


def field_refusal(name: str, code: str, key: str | None = None, **params: Any) -> ValidationError:
    if key is None:
        return ValidationError({name: [field_error(code)]})
    return ValidationError({name: [field_error(code, message_key=key, params=params or None)]})


def _too_similar(password: str, identifier: str, max_similarity: float = 0.7) -> bool:
    """Django's ``UserAttributeSimilarityValidator`` rule applied to the phone's digits (customer contract
    §2.1.3: "not similar to the phone") or to an e-mail address, its local part and the local part's words
    (hybrid identity); the validator itself needs a model instance."""
    password = password.lower()
    if identifiers.is_email(identifier):
        local = identifier.partition("@")[0]
        parts = {identifier, local, *(word for word in re.split(r"\W+", local) if len(word) >= 3)}
        value = identifier
    else:
        value = identifier.lstrip("+")
        parts = {value, value[3:], value[-9:], value[-7:]}
    if not value or len(password) >= 10 * len(value):
        return False
    for part in parts:
        if part and SequenceMatcher(a=password, b=part).quick_ratio() >= max_similarity:
            return True
    return False


PASSWORD_CODE_KEYS = {
    "password_too_short": "field.password_too_short",
    "password_too_common": "field.password_too_common",
    "password_entirely_numeric": "field.password_entirely_numeric",
    "password_too_similar": "field.password_too_similar",
}


def validate_customer_password(
    password: str, identifier: str, *, field: str = "password", current_hash: str = ""
) -> None:
    """M2-A11: ≥ ``CUSTOMER_PASSWORD_MIN_LENGTH``, not common, not only digits, not similar to the phone (or
    the e-mail address, hybrid identity; ``""`` = nothing to compare); plus ``password_unchanged`` against
    ``current_hash`` (customer contract §2.1.3, §2.1.5, §2.2.9)."""
    limit = int(settings.CUSTOMER_PASSWORD_MIN_LENGTH)
    validators: list[Any] = [
        password_validation.MinimumLengthValidator(min_length=limit),
        password_validation.CommonPasswordValidator(),
        password_validation.NumericPasswordValidator(),
    ]
    details = []
    for validator in validators:
        try:
            validator.validate(password)
        except DjangoValidationError as exc:
            for error in exc.error_list:
                code = error.code or "invalid"
                key = PASSWORD_CODE_KEYS.get(code)
                if key is None:
                    details.append(field_error("invalid"))
                elif code == "password_too_short":
                    details.append(field_error(code, message_key=key, limit=limit))
                else:
                    details.append(field_error(code, message_key=key))
    if identifier and _too_similar(password, identifier):
        key = (
            "field.password_too_similar_email"
            if identifiers.is_email(identifier)
            else "field.password_too_similar"
        )
        details.append(field_error("password_too_similar", message_key=key))
    if not details and current_hash and check_password(password, current_hash):
        details.append(field_error("password_unchanged", message_key="field.password_unchanged"))
    if details:
        raise ValidationError({field: details})


__all__ = [
    "PASSWORD_CODE_KEYS",
    "check_login_password",
    "field_refusal",
    "validate_customer_password",
]
