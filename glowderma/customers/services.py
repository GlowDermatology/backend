"""Transactional writes of `customers` (contract §2.6): the manual cashback adjustment.

Thin orchestration over ``loyalty.services.adjust``, which runs the contract checks in order (``amount``
1..10 000 000, ``reason`` 3..300 trimmed, then ``subtract`` > ``available`` under the ``LoyaltyAccount``
lock), posts the ``adjustment`` entry through ``post_entry`` and audits ``cashback.adjusted``
``{direction, amount, reason, balance_after, ledger_entry_id}`` with the staff actor in the same transaction
— so this module adds no second audit row. Deleted customers are allowed: the balance is kept for a later
re-registration (decision A1).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import PurePosixPath
from typing import Any

from django.conf import settings
from django.contrib.auth import password_validation
from django.contrib.auth.hashers import check_password, make_password
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from rest_framework.exceptions import ValidationError

from glowderma.accounts import services as account_services
from glowderma.accounts import topics
from glowderma.accounts.api.guard import LoginGuard
from glowderma.accounts.api.throttles import phone_throttle_ident
from glowderma.accounts.models import Address, Customer, CustomerSocialIdentity, Device
from glowderma.appointments import services as appointment_services
from glowderma.appointments.models import Appointment
from glowderma.commerce import selectors as commerce_selectors
from glowderma.commerce import services as commerce_services
from glowderma.commerce.models import Order
from glowderma.core import audit, enums, integration_modes, outbox, timeutils
from glowderma.core.api.exceptions import (
    AddressLimit,
    AuthFailed,
    LastSignInMethod,
    PaymentInProgress,
    PhoneNotRegistered,
    PhoneRegistered,
    ProviderUnavailable,
    SocialIdentityLinked,
    SocialProviderLinked,
    SocialTicketInvalid,
    SocialTokenInvalid,
    TermsNotPublished,
    VerificationInvalid,
    field_error,
)
from glowderma.core.images import ImageError, process_image
from glowderma.core.media import absolute_media_url, media_name
from glowderma.core.pii import pii_ref
from glowderma.customers import selectors
from glowderma.integrations.oauth import registry as oauth_registry
from glowderma.integrations.oauth.ports import AppTokenClaims, OAuthExchangeError
from glowderma.loyalty import services as loyalty_services
from glowderma.loyalty.selectors import LedgerRow
from glowderma.site_config import selectors as site_config_selectors

logger = logging.getLogger(__name__)


class CustomerNotFound(Exception):
    """The customer id does not exist (the view answers 404)."""


@dataclass(frozen=True, slots=True)
class AdjustmentOutcome:
    """``POST customers/{id}/cashback-adjustments/`` 201: the new entry, ``balance`` and ``available``."""

    entry: LedgerRow
    balance: int
    available: int


def adjust_cashback(
    customer_id: int,
    *,
    direction: str,
    amount: Any,
    reason: Any,
    actor_id: int,
    request: Any = None,
) -> AdjustmentOutcome:
    """Add or subtract cashback by hand.

    ``amount`` and ``reason`` are passed as received (``loyalty.services.adjust`` decides what "not an int"
    means). Raises :class:`CustomerNotFound`, ``loyalty.services.AdjustmentValidationError`` and
    ``loyalty.services.InsufficientBalance`` (both carry the contract field errors and top-level message).
    """
    if not selectors.customer_exists(customer_id):
        raise CustomerNotFound(customer_id)
    with transaction.atomic():
        result = loyalty_services.adjust(customer_id, direction, amount, reason, actor_id, request=request)
        entry = selectors.ledger_entry(result.entry_id)
    return AdjustmentOutcome(entry=entry, balance=result.balance, available=result.available)


# --- M2 customer API orchestrators (customer contract §2.1, §2.2; plan §2 P1) -----------------------------
#
# Services take ids and scalars and return dataclasses; cross-module writes go through the lower modules'
# services (accounts, commerce, appointments). Refusals that must leave a trace (a failed password, a used
# verification attempt) are recorded outside the transaction that the refusal rolls back.


PROVIDER_LABELS = {"google": "Google", "facebook": "Facebook", "apple": "Apple"}
DEFAULT_TOKEN_TYPES: dict[str, str] = {
    enums.CustomerSocialProvider.GOOGLE: enums.SocialTokenType.ID_TOKEN,
    enums.CustomerSocialProvider.APPLE: enums.SocialTokenType.ID_TOKEN,
    enums.CustomerSocialProvider.FACEBOOK: enums.SocialTokenType.ACCESS_TOKEN,
}
# Customer contract §2.1.8: only these four (provider, token type) pairs exist.
TOKEN_PAIRS = frozenset(
    {
        ("google", "id_token"),
        ("apple", "id_token"),
        ("facebook", "access_token"),
        ("facebook", "id_token"),
    }
)
SOCIAL_TICKET_SALT = "gd.social"
SOCIAL_APPLE_KEY = "gd:social:apple:{jti}"
SOCIAL_TICKET_USED_KEY = "gd:social:ticket:{jti}"
NONCE_WILDCARD = "*"
LOGIN_SCOPE = "customer_login"
LOGIN_DEVICE_SCOPE = "customer_login_device"
REAUTH_SCOPE = "customer_reauth"


@dataclass(frozen=True, slots=True)
class SignIn:
    """A successful sign-in: the new session (``AuthTokens``) and the customer it belongs to."""

    started: account_services.SessionStart
    customer_id: int
    is_new_customer: bool = False
    reactivated: bool = False


@dataclass(frozen=True, slots=True)
class DeviceStep:
    """``NextStep`` ``verify_device`` (customer contract §2.1.4 step 6)."""

    issue: account_services.CodeIssue


@dataclass(frozen=True, slots=True)
class PhoneStep:
    """``NextStep`` ``verify_phone`` (customer contract §2.1.8): the signed ticket bound to this install."""

    ticket: str
    provider: str
    email: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class DeviceContext:
    """The device headers of the request and its client IP / language."""

    fid: str
    platform: str
    app_version: str
    ip: str = ""
    lang: str = "uz"


# --- shared helpers


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


def phone_guard_key(phone: str) -> str:
    return phone_throttle_ident(phone) or "invalid"


def _field(name: str, code: str, key: str | None = None, **params: Any) -> ValidationError:
    if key is None:
        return ValidationError({name: [field_error(code)]})
    return ValidationError({name: [field_error(code, message_key=key, params=params or None)]})


def _too_similar(password: str, phone: str, max_similarity: float = 0.7) -> bool:
    """Django's ``UserAttributeSimilarityValidator`` rule applied to the phone's digits (customer contract
    §2.1.3: "not similar to the phone"); the validator itself needs a model instance."""
    value = phone.lstrip("+")
    password = password.lower()
    if len(password) >= 10 * len(value):
        return False
    for part in {value, value[3:], value[-9:], value[-7:]}:
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
    password: str, phone: str, *, field: str = "password", current_hash: str = ""
) -> None:
    """M2-A11: ≥ ``CUSTOMER_PASSWORD_MIN_LENGTH``, not common, not only digits, not similar to the phone;
    plus ``password_unchanged`` against ``current_hash`` (customer contract §2.1.3, §2.1.5, §2.2.9)."""
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
    if _too_similar(password, phone):
        key = PASSWORD_CODE_KEYS["password_too_similar"]
        details.append(field_error("password_too_similar", message_key=key))
    if not details and current_hash and check_password(password, current_hash):
        details.append(field_error("password_unchanged", message_key="field.password_unchanged"))
    if details:
        raise ValidationError({field: details})


def _audit(action: str, customer: Customer | None, request: Any, after: Any = None) -> None:
    audit.record(
        action=action,
        entity_type="customer",
        entity_id=str(customer.pk) if customer is not None else "",
        actor=customer,
        actor_type="customer",
        request=request,
        after=after,
    )


def _account_security(
    customer_id: int, event: str, *, provider: str = "", farewell_tokens: list[str] | tuple[str, ...] = ()
) -> None:
    payload = topics.account_security_payload(
        customer_id, event, provider=provider, farewell_tokens=farewell_tokens
    )
    outbox.enqueue(
        topics.PUSH_ACCOUNT_SECURITY,
        payload,
        dedupe_key=topics.account_security_key(customer_id, event, payload["event_id"]),
    )


def _unlink(identity: CustomerSocialIdentity, *, reason: str, customer: Customer, request: Any) -> None:
    account_services.enqueue_apple_revoke(identity)
    _audit("customer.social_unlinked", customer, request, {"provider": identity.provider, "reason": reason})
    identity.delete()


def _stamp_consent(customer: Customer, terms_version: str, ip: str) -> None:
    Customer.objects.filter(pk=customer.pk).update(
        terms_version=terms_version,
        consent_at=timeutils.now(),
        consent_ip=ip or None,
        updated_at=timeutils.now(),
    )


def _require_published_terms() -> site_config_selectors.Terms:
    terms = site_config_selectors.current_terms()
    if not terms.published:
        raise TermsNotPublished()
    return terms


def _check_terms_version(terms_version: str, terms: site_config_selectors.Terms) -> None:
    if terms_version != terms.version:
        raise _field("terms_version", "terms_outdated", "field.terms_outdated")


def dormant_reset(customer_id: int, *, lang: str, last_activity: datetime | None, request: Any) -> None:
    """The dormant-account rule (customer contract §2.2.3, M2-A16), inside the caller's transaction and under
    its customer-level lock: the history horizon moves to now; name, image, addresses and cart are scrubbed;
    language becomes the request language; consent is cleared; every identity is unlinked (Apple revoked) and
    every device key cleared. Balance, ledger and purchases stay. Audit ``customer.history_reset``."""
    now = timeutils.now()
    customer = Customer.objects.get(pk=customer_id)
    account_services.move_history_horizon(customer_id, at=now)
    if customer.image:
        customer.image.delete(save=False)
    Customer.objects.filter(pk=customer_id).update(
        name="",
        image="",
        address_text="",
        language=lang,
        terms_version="",
        consent_at=None,
        consent_ip=None,
        updated_at=now,
    )
    Address.objects.filter(customer_id=customer_id).delete()
    for identity in list(CustomerSocialIdentity.objects.filter(customer_id=customer_id)):
        _unlink(identity, reason="dormant", customer=customer, request=request)
    Device.objects.filter(customer_id=customer_id).update(key_hash="", updated_at=now)
    commerce_services.delete_cart_lines(customer_id)
    _audit(
        "customer.history_reset",
        customer,
        request,
        {
            "reason": "dormant",
            "last_activity_at": timeutils.isoformat(last_activity) if last_activity else None,
        },
    )


# --- registration, login, reset (customer contract §2.1.3–§2.1.5)


def register(
    verification_token: str,
    *,
    password: str,
    name: str,
    terms_version: str,
    language: str,
    device: DeviceContext,
    request: Any = None,
) -> SignIn:
    """``auth/set-password/``: a new customer, or the reactivation of a deleted one (same id, history horizon
    at ``reactivated_at``); password, consent, the first session with a device key; audit
    ``customer.registered``."""
    terms = _require_published_terms()
    row = account_services.verification_row(verification_token, purpose=enums.OtpPurpose.REGISTER)
    validate_customer_password(password, row.phone)
    _check_terms_version(terms_version, terms)
    with transaction.atomic():
        phone, _ = account_services.consume_verification(
            verification_token, purpose=enums.OtpPurpose.REGISTER
        )
        customer = Customer.objects.select_for_update(no_key=True).filter(phone=phone).first()
        reactivated = False
        if customer is not None and customer.status == enums.CustomerStatus.ACTIVE:
            raise PhoneRegistered()
        if customer is None:
            try:
                with transaction.atomic():
                    customer = Customer.objects.create(phone=phone, name=name, language=language)
            except IntegrityError as exc:  # a concurrent registration won
                raise PhoneRegistered() from exc
        else:
            customer = account_services.reactivate_customer(customer, name=name)
            account_services.move_history_horizon(customer.pk, at=customer.reactivated_at or timeutils.now())
            Customer.objects.filter(pk=customer.pk).update(language=language)
            reactivated = True
        account_services.set_customer_password(customer, password)
        _stamp_consent(customer, terms_version, device.ip)
        started = account_services.start_customer_session(
            customer.pk, device.fid, device.platform, device.app_version, issue_device_key=True
        )
        _audit(
            "customer.registered",
            customer,
            request,
            {"reactivated": reactivated, "terms_version": terms_version},
        )
        return SignIn(started, customer.pk, is_new_customer=not reactivated, reactivated=reactivated)


def login(
    phone: str,
    password: str,
    verification_token: str | None,
    *,
    device: DeviceContext,
    device_key: str | None,
    request: Any = None,
) -> SignIn | DeviceStep:
    """``auth/login/`` steps 1–7 (customer contract §2.1.4)."""
    customer = account_services.active_customer_by_phone(phone)
    known = customer is not None and account_services.device_is_known(customer.pk, device.fid, device_key)
    guard = LoginGuard()
    if known and customer is not None:
        device_id = Device.objects.filter(customer_id=customer.pk, fid=device.fid).values_list(
            "pk", flat=True
        )[0]
        scope, key = LOGIN_DEVICE_SCOPE, str(device_id)
    else:
        scope, key = LOGIN_SCOPE, phone_guard_key(phone)
    guard.check(scope, key)
    with transaction.atomic():
        if not known and not verification_token:
            block = account_services.otp_blocked(phone, fid=device.fid, ip=device.ip, now=timeutils.now())
            if block is not None:
                raise block
        ok = check_login_password(customer, password)
        if ok and customer is not None:
            guard.reset(scope, key)
            if known:
                started = account_services.start_customer_session(
                    customer.pk, device.fid, device.platform, device.app_version, issue_device_key=False
                )
                _audit(
                    "customer.login_succeeded", customer, request, {"method": "password", "new_device": False}
                )
                return SignIn(started, customer.pk)
            if not verification_token:
                issue = account_services.issue_login_code(
                    phone, ip=device.ip, fid=device.fid, lang=device.lang, platform=device.platform
                )
                return DeviceStep(issue)
            account_services.consume_verification(
                verification_token, purpose=enums.OtpPurpose.LOGIN_NEW_DEVICE, phone=phone
            )
            started = account_services.start_customer_session(
                customer.pk, device.fid, device.platform, device.app_version, issue_device_key=True
            )
            _audit("customer.login_succeeded", customer, request, {"method": "password", "new_device": True})
            return SignIn(started, customer.pk)
    guard.record_failure(scope, key)
    _audit("customer.login_failed", customer, request, {"method": "password"})
    raise AuthFailed("invalid_credentials")


def reset_password(
    verification_token: str, *, password: str, device: DeviceContext, request: Any = None
) -> SignIn:
    """``auth/reset-password/`` (customer contract §2.1.5): the unauthenticated recovery."""
    row = account_services.verification_row(verification_token, purpose=enums.OtpPurpose.RESET_PASSWORD)
    customer = account_services.active_customer_by_phone(row.phone)
    if customer is None:
        raise PhoneNotRegistered()
    validate_customer_password(password, row.phone, current_hash=customer.password)
    with transaction.atomic():
        account_services.consume_verification(
            verification_token, purpose=enums.OtpPurpose.RESET_PASSWORD, phone=row.phone
        )
        customer = account_services.lock_customer(customer.pk)
        if customer.status != enums.CustomerStatus.ACTIVE:
            raise PhoneNotRegistered()
        dormant, last_activity = selectors.is_dormant(customer.pk, now=timeutils.now())
        account_services.set_customer_password(customer, password)
        account_services.revoke_customer_sessions(customer.pk, enums.SessionRevokeReason.PASSWORD_CHANGED)
        for identity in list(CustomerSocialIdentity.objects.filter(customer_id=customer.pk)):
            _unlink(identity, reason="password_reset", customer=customer, request=request)
        Device.objects.filter(customer_id=customer.pk).exclude(fid=device.fid).update(
            key_hash="", updated_at=timeutils.now()
        )
        if dormant:
            dormant_reset(customer.pk, lang=device.lang, last_activity=last_activity, request=request)
        started = account_services.start_customer_session(
            customer.pk, device.fid, device.platform, device.app_version, issue_device_key=True
        )
        _account_security(
            customer.pk, enums.AccountSecurityEvent.PASSWORD_RESET, farewell_tokens=started.farewell_tokens
        )
        _audit("customer.password_reset", customer, request)
        return SignIn(started, customer.pk)


# --- social sign-in (customer contract §2.1.8, §2.1.9, §2.2.8)


def _nonce_accepted(nonce: str | None, fid: str) -> bool:
    if nonce == NONCE_WILDCARD and integration_modes.dev_features_allowed() and settings.OAUTH_MODE == "fake":
        return True
    return bool(nonce) and account_services.consume_social_nonce(str(nonce), fid)


def verify_social_token(
    provider: str, token: str, token_type: str | None, nonce: str | None, *, fid: str
) -> AppTokenClaims:
    """The checks of customer contract §2.1.8: availability (404), the four (provider, token type) pairs, the
    single-use nonce bound to this install (ID tokens), the provider verification and the replay cache."""
    verifier = oauth_registry.get_app_verifier(provider)
    kind = token_type or DEFAULT_TOKEN_TYPES[provider]
    if (provider, kind) not in TOKEN_PAIRS:
        raise SocialTokenInvalid()
    if kind == enums.SocialTokenType.ID_TOKEN and not _nonce_accepted(nonce, fid):
        raise SocialTokenInvalid()
    try:
        claims = verifier.verify_app_token(token, kind, nonce)
    except OAuthExchangeError as exc:
        if exc.kind == "unavailable":
            raise ProviderUnavailable() from exc
        raise SocialTokenInvalid() from exc
    if kind == enums.SocialTokenType.ID_TOKEN and not account_services.remember_id_token(
        provider, token, expires_at=claims.expires_at
    ):
        raise SocialTokenInvalid()
    return claims


def _exchange_apple(code: str | None, claims: AppTokenClaims) -> tuple[str, str]:
    """Exchange Apple's authorization code before any row lock; ``(sealed refresh token, client_id)``."""
    if not code:
        raise _field("authorization_code", "required")
    client_id = claims.audience
    try:
        refresh = oauth_registry.get_apple_service().exchange_code(
            code, client_id=client_id, subject=claims.subject
        )
    except OAuthExchangeError as exc:
        if exc.kind == "unavailable":
            raise ProviderUnavailable() from exc
        raise SocialTokenInvalid() from exc
    return account_services.seal_apple_refresh(refresh, client_id=client_id), client_id


def social_sign_in(
    provider: str,
    token: str,
    token_type: str | None,
    nonce: str | None,
    authorization_code: str | None,
    *,
    device: DeviceContext,
    request: Any = None,
) -> SignIn | PhoneStep:
    """``auth/social/``: a linked identity of an active customer signs in (no new-device SMS, M2-A10), an
    unknown one gets the ``verify_phone`` ticket (Apple's code is exchanged now, the sealed token waits in
    Redis for the completion)."""
    claims = verify_social_token(provider, token, token_type, nonce, fid=device.fid)
    identity = (
        CustomerSocialIdentity.objects.select_related("customer")
        .filter(provider=provider, subject=claims.subject)
        .first()
    )
    if identity is not None and identity.customer.status == enums.CustomerStatus.ACTIVE:
        sealed = client_id = ""
        if provider == enums.CustomerSocialProvider.APPLE and not identity.provider_refresh_token:
            sealed, client_id = _exchange_apple(authorization_code, claims)
        with transaction.atomic():
            fields: dict[str, Any] = {"last_login_at": timeutils.now()}
            if sealed:
                fields.update(provider_refresh_token=sealed, provider_client_id=client_id)
            CustomerSocialIdentity.objects.filter(pk=identity.pk).update(**fields)
            started = account_services.start_customer_session(
                identity.customer_id,
                device.fid,
                device.platform,
                device.app_version,
                issue_device_key=True,
                auth_method=provider,
            )
            _audit("customer.login_succeeded", identity.customer, request, {"method": provider})
            return SignIn(started, identity.customer_id)
    jti = secrets.token_hex(16)
    ttl = int(settings.SOCIAL_TICKET_TTL_SECONDS)
    if provider == enums.CustomerSocialProvider.APPLE:
        sealed, client_id = _exchange_apple(authorization_code, claims)
        cache.set(SOCIAL_APPLE_KEY.format(jti=jti), {"sealed": sealed, "client_id": client_id}, ttl)
    ticket = signing.TimestampSigner(salt=SOCIAL_TICKET_SALT).sign_object(
        {
            "provider": provider,
            "subject": claims.subject,
            "email": claims.email,
            "jti": jti,
            "fid": pii_ref(device.fid),
        }
    )
    return PhoneStep(ticket, provider, claims.email, ttl)


@dataclass(frozen=True, slots=True)
class SocialTicket:
    provider: str
    subject: str
    email: str
    jti: str


def read_social_ticket(ticket: str, *, fid: str) -> SocialTicket:
    """A valid ticket (signature, ≤ ``SOCIAL_TICKET_TTL_SECONDS``, unused, issued to this install), else 400
    ``social_ticket_invalid`` (C§2.1.1 row 4, §2.1.9). "Unused" is the ``gd:social:ticket:{jti}`` marker a
    completion claims; :func:`complete_social` still claims it atomically (``cache.add``)."""
    try:
        data = signing.TimestampSigner(salt=SOCIAL_TICKET_SALT).unsign_object(
            ticket, max_age=int(settings.SOCIAL_TICKET_TTL_SECONDS)
        )
    except signing.BadSignature as exc:
        raise SocialTicketInvalid() from exc
    if (
        not isinstance(data, dict)
        or data.get("fid") != pii_ref(fid)
        or data.get("provider") not in enums.CustomerSocialProvider.values
    ):
        raise SocialTicketInvalid()
    jti = str(data.get("jti") or "")
    if not jti or cache.get(SOCIAL_TICKET_USED_KEY.format(jti=jti)) is not None:
        raise SocialTicketInvalid()
    return SocialTicket(str(data["provider"]), str(data["subject"]), str(data.get("email") or ""), jti)


def complete_social(
    ticket: str,
    verification_token: str,
    *,
    password: str | None,
    name: str | None,
    terms_version: str | None,
    language: str | None,
    device: DeviceContext,
    request: Any = None,
) -> SignIn:
    """``auth/social/complete/`` (customer contract §2.1.9)."""
    social = read_social_ticket(ticket, fid=device.fid)
    row = account_services.verification_row(verification_token, purpose=enums.OtpPurpose.LINK_SOCIAL)
    if cache.get(account_services.social_otp_key(row.pk)) != social.jti:
        raise VerificationInvalid()
    apple: dict[str, str] | None = None
    if social.provider == enums.CustomerSocialProvider.APPLE:
        stored = cache.get(SOCIAL_APPLE_KEY.format(jti=social.jti))
        if not isinstance(stored, dict) or not stored.get("sealed"):
            raise SocialTicketInvalid()
        apple = stored
    phone = row.phone
    customer = Customer.objects.filter(phone=phone).first()
    active = customer is not None and customer.status == enums.CustomerStatus.ACTIVE
    terms = None
    if not active:
        if not name or len(name.strip()) < 2:
            raise _field("name", "required") if not name else _field("name", "min_length", "field.name_min")
        if not terms_version:
            raise _field("terms_version", "required")
        terms = _require_published_terms()
        _check_terms_version(terms_version, terms)
    elif customer is not None and customer.password:
        if not password:
            raise _field("password", "required")
        guard = LoginGuard()
        key = phone_guard_key(phone)
        guard.check(LOGIN_SCOPE, key)
        if not account_services.check_customer_password(customer, password):
            guard.record_failure(LOGIN_SCOPE, key)
            raise _field("password", "password_incorrect", "field.password_incorrect")
        guard.reset(LOGIN_SCOPE, key)
    used_key = SOCIAL_TICKET_USED_KEY.format(jti=social.jti)
    if not cache.add(used_key, 1, int(settings.SOCIAL_TICKET_TTL_SECONDS)):
        raise SocialTicketInvalid()
    try:
        result = _complete_social(
            social, verification_token, phone, active, apple, name, terms_version, language, device, request
        )
    except Exception:
        cache.delete(used_key)
        raise
    cache.delete(SOCIAL_APPLE_KEY.format(jti=social.jti))
    return result


def _complete_social(
    social: SocialTicket,
    verification_token: str,
    phone: str,
    active: bool,
    apple: dict[str, str] | None,
    name: str | None,
    terms_version: str | None,
    language: str | None,
    device: DeviceContext,
    request: Any,
) -> SignIn:
    with transaction.atomic():
        account_services.consume_verification(
            verification_token, purpose=enums.OtpPurpose.LINK_SOCIAL, phone=phone
        )
        customer = Customer.objects.select_for_update(no_key=True).filter(phone=phone).first()
        created = reactivated = False
        if customer is not None and customer.status == enums.CustomerStatus.ACTIVE:
            if not active:  # registered meanwhile: the name/terms branch no longer applies
                raise PhoneRegistered()
            if not customer.password:
                dormant, last_activity = selectors.is_dormant(customer.pk, now=timeutils.now())
                if dormant:
                    dormant_reset(customer.pk, lang=device.lang, last_activity=last_activity, request=request)
        else:
            if active:  # deleted meanwhile
                raise VerificationInvalid()
            if customer is None:
                try:
                    with transaction.atomic():
                        customer = Customer.objects.create(
                            phone=phone, name=(name or "").strip(), language=language or device.lang
                        )
                except IntegrityError as exc:
                    raise PhoneRegistered() from exc
                created = True
            else:
                customer = account_services.reactivate_customer(customer, name=(name or "").strip())
                account_services.move_history_horizon(
                    customer.pk, at=customer.reactivated_at or timeutils.now()
                )
                Customer.objects.filter(pk=customer.pk).update(language=language or device.lang)
                reactivated = True
            _stamp_consent(customer, str(terms_version), device.ip)
        label = PROVIDER_LABELS[social.provider]
        if CustomerSocialIdentity.objects.filter(customer_id=customer.pk, provider=social.provider).exists():
            raise SocialProviderLinked(details={"provider": social.provider}, params={"provider": label})
        if CustomerSocialIdentity.objects.filter(provider=social.provider, subject=social.subject).exists():
            raise SocialIdentityLinked(details={"provider": social.provider}, params={"provider": label})
        try:
            with transaction.atomic():
                CustomerSocialIdentity.objects.create(
                    customer_id=customer.pk,
                    provider=social.provider,
                    subject=social.subject,
                    email_at_link=social.email[:254] if "@" in social.email else "",
                    last_login_at=timeutils.now(),
                    provider_refresh_token=apple["sealed"] if apple else "",
                    provider_client_id=apple["client_id"] if apple else "",
                )
        except IntegrityError as exc:
            raise SocialIdentityLinked(
                details={"provider": social.provider}, params={"provider": label}
            ) from exc
        started = account_services.start_customer_session(
            customer.pk,
            device.fid,
            device.platform,
            device.app_version,
            issue_device_key=True,
            auth_method=social.provider,
        )
        if not created and not reactivated:
            _account_security(
                customer.pk,
                enums.AccountSecurityEvent.SOCIAL_LINKED,
                provider=social.provider,
                farewell_tokens=started.farewell_tokens,
            )
        _audit("customer.social_linked", customer, request, {"provider": social.provider})
        if created or reactivated:
            _audit(
                "customer.registered",
                customer,
                request,
                {"reactivated": reactivated, "terms_version": terms_version},
            )
        return SignIn(started, customer.pk, is_new_customer=created, reactivated=reactivated)


# --- signed-in re-authentication (customer contract §1.8, §2.2.3, §2.2.8, §2.2.9)


def reauthenticate(
    customer: Customer,
    *,
    password: str | None,
    verification_token: str | None,
    secret_field: str = "password",  # noqa: S107 - a field name, not a secret
) -> bool:
    """The password checks of a signed-in customer: ``customer_reauth`` guard (checked and reset), each
    failure also counted in ``customer_login`` (never checked). A social-only account needs a
    ``reset_password`` verification token of its own phone, which is only checked here and consumed by the
    caller under the customer-level lock. Returns whether a verification token must be consumed."""
    if customer.password:
        if not password:
            raise _field(secret_field, "required")
        guard = LoginGuard()
        key = str(customer.pk)
        guard.check(REAUTH_SCOPE, key)
        if not account_services.check_customer_password(customer, password):
            guard.record_failure(REAUTH_SCOPE, key)
            guard.record_failure(LOGIN_SCOPE, phone_guard_key(customer.phone))
            raise _field(secret_field, "password_incorrect", "field.password_incorrect")
        guard.reset(REAUTH_SCOPE, key)
        return False
    if not verification_token:
        raise _field("verification_token", "required")
    account_services.verification_row(
        verification_token, purpose=enums.OtpPurpose.RESET_PASSWORD, phone=customer.phone
    )
    return True


def _consume_reauth(customer: Customer, verification_token: str | None, needed: bool) -> None:
    if needed:
        account_services.consume_verification(
            str(verification_token), purpose=enums.OtpPurpose.RESET_PASSWORD, phone=customer.phone
        )


def link_social(
    customer_id: int,
    provider: str,
    token: str,
    token_type: str | None,
    nonce: str | None,
    authorization_code: str | None,
    *,
    password: str | None,
    verification_token: str | None,
    fid: str,
    request: Any = None,
) -> None:
    """``POST me/social/`` (customer contract §2.2.8)."""
    oauth_registry.get_app_verifier(provider)  # 404 first
    customer = Customer.objects.get(pk=customer_id)
    needed = reauthenticate(customer, password=password, verification_token=verification_token)
    claims = verify_social_token(provider, token, token_type, nonce, fid=fid)
    sealed = client_id = ""
    if provider == enums.CustomerSocialProvider.APPLE:
        sealed, client_id = _exchange_apple(authorization_code, claims)
    label = PROVIDER_LABELS[provider]
    with transaction.atomic():
        customer = account_services.lock_customer(customer_id)
        _consume_reauth(customer, verification_token, needed)
        if CustomerSocialIdentity.objects.filter(customer_id=customer_id, provider=provider).exists():
            raise SocialProviderLinked(details={"provider": provider}, params={"provider": label})
        if CustomerSocialIdentity.objects.filter(provider=provider, subject=claims.subject).exists():
            raise SocialIdentityLinked(details={"provider": provider}, params={"provider": label})
        try:
            with transaction.atomic():
                CustomerSocialIdentity.objects.create(
                    customer_id=customer_id,
                    provider=provider,
                    subject=claims.subject,
                    email_at_link=claims.email[:254] if "@" in claims.email else "",
                    provider_refresh_token=sealed,
                    provider_client_id=client_id,
                )
        except IntegrityError as exc:
            raise SocialIdentityLinked(details={"provider": provider}, params={"provider": label}) from exc
        _account_security(customer_id, enums.AccountSecurityEvent.SOCIAL_LINKED, provider=provider)
        _audit("customer.social_linked", customer, request, {"provider": provider, "via": "settings"})


def unlink_social(
    customer_id: int,
    provider: str,
    *,
    password: str | None,
    verification_token: str | None,
    request: Any = None,
) -> None:
    """``DELETE me/social/{provider}/`` (customer contract §2.2.8): re-authentication first, then the
    idempotent no-op, ``last_sign_in_method``, the unlink (Apple revoked) and the notice."""
    customer = Customer.objects.get(pk=customer_id)
    needed = reauthenticate(customer, password=password, verification_token=verification_token)
    with transaction.atomic():
        customer = account_services.lock_customer(customer_id)
        _consume_reauth(customer, verification_token, needed)
        identity = CustomerSocialIdentity.objects.filter(customer_id=customer_id, provider=provider).first()
        if identity is None:
            return
        if (
            not customer.password
            and CustomerSocialIdentity.objects.filter(customer_id=customer_id).count() <= 1
        ):
            raise LastSignInMethod(details={"provider": provider})
        _unlink(identity, reason="customer", customer=customer, request=request)
        _account_security(customer_id, enums.AccountSecurityEvent.SOCIAL_UNLINKED, provider=provider)


def change_password(
    customer_id: int,
    *,
    current_password: str | None,
    verification_token: str | None,
    password: str,
    sid: str,
    request: Any = None,
) -> None:
    """``POST me/password/`` (customer contract §2.2.9): set or change the password while signed in; the
    current session, its device key and every identity are kept; other sessions → ``password_changed``."""
    customer = Customer.objects.get(pk=customer_id)
    needed = reauthenticate(
        customer,
        password=current_password,
        verification_token=verification_token,
        secret_field="current_password",  # noqa: S106 - a field name
    )
    validate_customer_password(password, customer.phone, current_hash=customer.password)
    first = not customer.password
    with transaction.atomic():
        customer = account_services.lock_customer(customer_id)
        _consume_reauth(customer, verification_token, needed)
        account_services.set_customer_password(customer, password)
        account_services.revoke_customer_sessions(
            customer_id, enums.SessionRevokeReason.PASSWORD_CHANGED, except_sid=sid
        )
        _account_security(customer_id, enums.AccountSecurityEvent.PASSWORD_CHANGED)
        _audit("customer.password_changed", customer, request, {"first_password": first})


# --- profile, consent, image, addresses (customer contract §2.2.2–§2.2.7)


def accept_consent(customer_id: int, *, terms_version: str, ip: str, request: Any = None) -> None:
    terms = site_config_selectors.current_terms()
    if not terms.published:
        raise _field("terms_version", "terms_outdated", "field.terms_outdated")
    _check_terms_version(terms_version, terms)
    with transaction.atomic():
        customer = account_services.lock_customer(customer_id)
        _stamp_consent(customer, terms_version, ip)
        _audit("customer.consent_accepted", customer, request, {"terms_version": terms_version})


def update_profile(customer_id: int, changes: dict[str, Any], *, fid: str) -> None:
    """``PATCH me/``: name and language on the customer, ``push_enabled`` on the current device (requires a
    stored FCM token: ``push_token_missing``). No audit (own profile)."""
    with transaction.atomic():
        account_services.lock_customer(customer_id)
        if "push_enabled" in changes:
            device = Device.objects.select_for_update().get(customer_id=customer_id, fid=fid)
            if changes["push_enabled"] and not device.fcm_token:
                raise _field("push_enabled", "push_token_missing", "field.push_token_missing")
            device.push_enabled = bool(changes["push_enabled"])
            device.save(update_fields=["push_enabled", "updated_at"])
        fields = {name: changes[name] for name in ("name", "language") if name in changes}
        if fields:
            Customer.objects.filter(pk=customer_id).update(**fields, updated_at=timeutils.now())
    logger.info("customer.profile_updated customer_id=%s fields=%s", customer_id, sorted(changes))


def set_image(customer_id: int, upload: Any) -> str:
    """``PUT me/image/``: re-encoded and EXIF-stripped (``core.images``), the previous file deleted."""
    try:
        processed = process_image(upload)
    except ImageError as exc:
        raise _field("file", exc.code) from exc
    with transaction.atomic():
        customer = account_services.lock_customer(customer_id)
        previous = customer.image.name if customer.image else ""
        name = media_name("customers", customer_id, processed.content, processed.extension)
        customer.image.save(PurePosixPath(name).name, ContentFile(processed.content), save=True)
        new_name = str(customer.image.name)
        if previous and previous != new_name:
            transaction.on_commit(lambda: default_storage.delete(previous), robust=True)
    return absolute_media_url(new_name)


def delete_image(customer_id: int) -> None:
    with transaction.atomic():
        customer = account_services.lock_customer(customer_id)
        if customer.image:
            previous = str(customer.image.name)
            Customer.objects.filter(pk=customer_id).update(image="", updated_at=timeutils.now())
            transaction.on_commit(lambda: default_storage.delete(previous), robust=True)


def add_address(customer_id: int, **fields: Any) -> Address:
    """``POST me/addresses/``: at most ``ADDRESS_MAX_PER_CUSTOMER`` (409 ``address_limit``)."""
    limit = int(settings.ADDRESS_MAX_PER_CUSTOMER)
    with transaction.atomic():
        account_services.lock_customer(customer_id)
        if Address.objects.filter(customer_id=customer_id).count() >= limit:
            raise AddressLimit(details={"limit": limit}, params={"limit": limit})
        return account_services.add_address(customer_id, **fields)


# --- account deletion (customer contract §2.2.3)


@dataclass(frozen=True, slots=True)
class DeletionResult:
    orders_cancelled: int
    appointments_cancelled: int


OPEN_APPOINTMENT_STATUSES = (enums.AppointmentStatus.REQUESTED, enums.AppointmentStatus.CONFIRMED)


def delete_account(
    customer_id: int,
    *,
    password: str | None = None,
    verification_token: str | None = None,
    request: Any = None,
) -> DeletionResult:
    """``DELETE me/`` steps 1–6: re-authentication (``customer_reauth``), then in one transaction under the
    customer-level lock (``FOR NO KEY UPDATE``, before any order lock): 409 ``payment_in_progress`` while a
    Click payment is in flight (nothing written), cancel unpaid orders and open future appointments, delete
    the cart lines, ``accounts.services.delete_customer`` and audit ``customer.deleted``."""
    customer = Customer.objects.get(pk=customer_id)
    needed = reauthenticate(customer, password=password, verification_token=verification_token)
    with transaction.atomic():
        customer = account_services.lock_customer(customer_id)
        inflight = commerce_selectors.customer_inflight_order(customer_id)
        if inflight is not None:
            raise PaymentInProgress(
                details={"provider": enums.PaymentProvider.CLICK.value, "order_id": inflight},
                params={"provider": "Click"},
            )
        _consume_reauth(customer, verification_token, needed)
        now = timeutils.now()
        order_ids = list(
            Order.objects.filter(customer_id=customer_id, status=enums.OrderStatus.AWAITING_PAYMENT)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        for order_id in order_ids:
            commerce_services.cancel_by_customer(order_id, customer_id, at=now)
        appointment_ids = list(
            Appointment.objects.filter(
                customer_id=customer_id, status__in=OPEN_APPOINTMENT_STATUSES, start__gt=now
            )
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        for appointment_id in appointment_ids:
            appointment_services.cancel_by_customer(
                appointment_id, customer_id, reason="account_deleted", at=now
            )
        commerce_services.delete_cart_lines(customer_id)
        account_services.delete_customer(customer_id)
        result = DeletionResult(len(order_ids), len(appointment_ids))
        _audit(
            "customer.deleted",
            customer,
            request,
            {
                "orders_cancelled": result.orders_cancelled,
                "appointments_cancelled": result.appointments_cancelled,
            },
        )
        return result
