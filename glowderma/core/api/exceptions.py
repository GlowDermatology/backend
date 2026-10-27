"""Error envelope (admin API v1, section 1.6).

Field error codes come in two groups:

* **Generic codes** carry a default Uzbek message from ``FIELD_MESSAGES`` (``required``, ``invalid``,
  ``read_only``, ``file_too_large``, ``unsupported_image``, ``max_items``, ``mismatch``, ...).
  :func:`field_error` builds such an ``ErrorDetail``; :func:`flatten_errors` also swaps DRF's stock
  English messages for the Uzbek default.
* **Endpoint-specific codes** (``ENDPOINT_FIELD_CODES``: ``customer_not_found``, ``in_past``,
  ``empty_range``, ``overlap``, ``out_of_range``, ``effective_from_in_past``, ``insufficient_balance``,
  ``billz_not_found``, ``required_for_active``, ``export_too_large``, ``unknown_placeholder``,
  ``slot_unavailable``, ``outside_working_hours``) have no default: the view raises
  them with the explicit message its contract table lists, e.g.
  ``ValidationError({"amount": [field_error("insufficient_balance", "Balansdan ko‘p ayirib bo‘lmaydi.")]})``.

Customer audience (customer contract §1.6, §5): views whose class carries ``audience`` in
``CUSTOMER_AUDIENCES`` (the customer, public, auth, POS and webhook base views) get the envelope with an extra
``details`` key, and every ``message`` (top level and per field) is rendered through ``core.i18n`` in the
resolved request language. Customer codes are :class:`CustomerError` subclasses carrying their catalog key;
endpoint-specific field codes take ``field_error(code, message_key="field.…")``. Admin bodies never change.
"""

import logging
import math
import re
from functools import lru_cache
from string import Formatter
from typing import Any, ClassVar

from django.core.exceptions import BadRequest, RequestDataTooBig, SuspiciousOperation
from django.http import Http404, HttpRequest, JsonResponse
from django.utils.translation import get_language, override
from rest_framework import exceptions, fields, relations, serializers, validators
from rest_framework.response import Response
from rest_framework.views import exception_handler, set_rollback

from glowderma.core import i18n

logger = logging.getLogger(__name__)
MESSAGES = {
    "validation_error": "Maydonlarni tekshiring.",
    "not_authenticated": "Avtorizatsiya talab qilinadi.",
    "SESSION_EXPIRED": "Sessiya tugadi. Qayta kiring.",
    "SESSION_REPLACED": "Boshqa qurilmadan kirildi.",
    "invalid_credentials": "Login yoki parol noto‘g‘ri.",
    "permission_denied": "Ruxsat yo‘q.",
    "not_found": "Topilmadi.",
    "request_in_progress": "So‘rov bajarilmoqda. Birozdan so‘ng qayta urinib ko‘ring.",
    "idempotency_key_reused": "Bu kalit boshqa so‘rov uchun ishlatilgan.",
    "idempotency_key_required": "Idempotency-Key sarlavhasi kerak.",
    "server_error": "Kutilmagan xato.",
    "payload_too_large": "Fayl juda katta.",
    "provider_unavailable": "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring.",
    "oauth_exchange_failed": "Ijtimoiy tarmoq orqali kirish amalga oshmadi.",
    "social_account_not_allowed": "Bu hisob administrator sifatida ro‘yxatdan o‘tmagan.",
}
FIELD_MESSAGES = {
    "required": "Majburiy maydon.",
    "null": "Majburiy maydon.",
    "blank": "Maydonni to‘ldiring.",
    "invalid": "Noto‘g‘ri qiymat.",
    "invalid_choice": "Ruxsat etilmagan qiymat.",
    "min_length": "Kamida {limit} ta belgi kiriting.",
    "max_length": "{limit} belgidan oshmasin.",
    "min_value": "Qiymat {limit} dan kam bo‘lmasin.",
    "max_value": "Qiymat {limit} dan oshmasin.",
    "max_decimal_places": "Ko‘pi bilan 2 ta kasr raqam.",
    "unique": "Bu qiymat band.",
    "does_not_exist": "Topilmadi.",
    "read_only": "Bu maydonni o‘zgartirib bo‘lmaydi.",
    "file_too_large": "Rasm 5 MB dan oshmasin.",
    "unsupported_image": "Faqat JPEG, PNG yoki WebP rasm.",
    "max_items": "Ko‘pi bilan {limit} ta.",
    "mismatch": "Ro‘yxat joriy elementlarga mos emas.",
}
# Raised by views with the explicit message of the endpoint's contract table (see the module docstring).
ENDPOINT_FIELD_CODES = frozenset(
    {
        "customer_not_found",
        "in_past",
        "empty_range",
        "overlap",
        "out_of_range",
        "effective_from_in_past",
        "insufficient_balance",
        "billz_not_found",
        "required_for_active",
        "export_too_large",
        # notifications (§2.8) and the admin appointment slots (T10 / §7 item 18)
        "unknown_placeholder",
        "slot_unavailable",
        "outside_working_hours",
        # cashback policy versions (§2.7): "no tiers"
        "min_items",
        # customer API (customer contract §2): rendered from their catalog key (field_error(message_key=…))
        "otp_invalid",
        "password_too_short",
        "password_too_common",
        "password_entirely_numeric",
        "password_too_similar",
        "password_unchanged",
        "password_incorrect",
        "terms_outdated",
        "push_token_missing",
        "latlng_pair",
        "address_incomplete",
    }
)
# View ``audience`` values whose errors use the customer envelope (``details``) and the i18n catalog.
CUSTOMER_AUDIENCES = frozenset({"customer", "customer_auth", "public", "pos", "webhook"})
AUTH_FAILURE_CODES = frozenset(
    {
        "not_authenticated",
        "SESSION_EXPIRED",
        "SESSION_REPLACED",
        "invalid_credentials",
        "social_account_not_allowed",
    }
)


def _uz_param(value: Any) -> Any:
    """An ``int`` grouped the Uzbek way (re-grouped per language on render); anything else unchanged."""
    if isinstance(value, int) and not isinstance(value, bool):
        return i18n.format_number(value, "uz")
    return value


def field_error(
    code: str,
    message: str | None = None,
    *,
    limit: int | str | None = None,
    message_key: str | None = None,
    params: dict[str, Any] | None = None,
) -> exceptions.ErrorDetail:
    """An ``ErrorDetail`` with a contract code.

    Generic codes default to their ``FIELD_MESSAGES`` entry (``limit`` fills ``{limit}``); endpoint-specific
    codes require an explicit ``message``. Any other code is a typo: the frontend maps codes to UI, so an
    invented one would ship silently. Customer views pass ``message_key`` (a ``field.*`` catalog key,
    customer contract §5.0) instead of a message: the detail carries the Uzbek text and the customer handler
    renders the key in the request language (``params`` and ``limit`` fill its placeholders). Pass amounts
    and limits as ``int``: they are grouped with ``i18n.format_number`` per language (``12 000`` in uz/ru,
    ``12,000`` in en); a ``str`` param or limit is copied unchanged into every language.
    """
    if code not in FIELD_MESSAGES and code not in ENDPOINT_FIELD_CODES:
        raise ValueError(f"Unknown field code {code!r}")
    if message_key is not None:
        if message is not None:
            raise ValueError("Pass either message or message_key")
        if not message_key.startswith("field.") or message_key not in i18n.CATALOG:
            raise ValueError(f"Unknown field catalog key {message_key!r}")
        values = {name: _uz_param(value) for name, value in (params or {}).items()}
        if limit is not None:
            values.setdefault("limit", _uz_param(limit))
        return exceptions.ErrorDetail(i18n.t(message_key, "uz", **values), code=code)
    if message is None:
        template = FIELD_MESSAGES.get(code)
        if template is None:
            raise ValueError(f"Field code {code!r} needs an explicit message")
        if "{limit}" in template:
            if limit is None:
                raise ValueError(f"Field code {code!r} needs a limit")
            template = template.format(limit=limit)
        message = template
    return exceptions.ErrorDetail(message, code=code)


@lru_cache(maxsize=64)
def _stock_templates(code: str, language: str) -> tuple[str, ...]:
    templates: set[str] = set()
    with override(language):
        for module in (fields, relations, serializers):
            for candidate in vars(module).values():
                if isinstance(candidate, type) and issubclass(candidate, fields.Field):
                    message = candidate.default_error_messages.get(code)
                    if message is not None:
                        templates.add(str(message))
        if code == "unique":
            templates.update(
                (str(validators.UniqueValidator.message), str(validators.UniqueTogetherValidator.message))
            )
    return tuple(templates)


def _field_message(value: Any, code: str) -> str:
    message = str(value)
    replacement = FIELD_MESSAGES.get(code)
    if replacement is None:
        return message
    if message in ("", code) and "{limit}" not in replacement:
        return replacement
    # Match only framework defaults; serializer-specific messages remain intact.
    languages = {get_language() or "uz", "en"}
    for language in languages:
        for template in _stock_templates(code, language):
            pattern = ""
            names: set[str] = set()
            for literal, name, _format_spec, _conversion in Formatter().parse(template):
                pattern += re.escape(literal)
                if name is not None:
                    if name in names:
                        pattern += f"(?P={name})"
                    else:
                        names.add(name)
                        pattern += f"(?P<{name}>.+?)"
            match = re.fullmatch(pattern, message)
            if match is not None:
                if "{limit}" in replacement:
                    limit = match.groupdict().get(code)
                    if limit is None:
                        continue
                    return replacement.format(limit=limit)
                return replacement
    return message


class ContractValidationError(exceptions.ValidationError):
    """A 400 ``validation_error`` whose top-level ``message`` comes from the endpoint's contract table.

    ``detail`` is the usual ``{field: [ErrorDetail]}`` mapping; ``message`` replaces the generic
    "Maydonlarni tekshiring." (e.g. ``"Summani kiriting."`` on ``customers/{id}/cashback-adjustments/``).
    """

    def __init__(self, detail: Any, *, message: str) -> None:
        super().__init__(detail)
        self.top_level_message = message


class Conflict(exceptions.APIException):
    status_code = 409
    default_code = "conflict"
    default_detail = MESSAGES["validation_error"]


class InvalidTransition(exceptions.APIException):
    status_code = 409
    default_code = "invalid_transition"
    default_detail = MESSAGES["validation_error"]


class NotCancellable(exceptions.APIException):
    status_code = 409
    default_code = "not_cancellable"
    default_detail = MESSAGES["validation_error"]


class RequestInProgress(exceptions.APIException):
    status_code = 409
    default_code = "request_in_progress"
    default_detail = MESSAGES[default_code]
    wait = 1


class IdempotencyKeyRequired(exceptions.APIException):
    status_code = 400
    default_code = "idempotency_key_required"
    default_detail = MESSAGES[default_code]


class IdempotencyKeyReused(exceptions.APIException):
    status_code = 422
    default_code = "idempotency_key_reused"
    default_detail = MESSAGES[default_code]


class ProviderUnavailable(exceptions.APIException):
    status_code = 503
    default_code = "provider_unavailable"
    default_detail = MESSAGES[default_code]


class OAuthExchangeFailed(exceptions.APIException):
    status_code = 400
    default_code = "oauth_exchange_failed"
    default_detail = MESSAGES[default_code]


class AuthFailed(exceptions.APIException):
    status_code = 401

    def __init__(self, code: str = "not_authenticated") -> None:
        if code not in AUTH_FAILURE_CODES:
            raise ValueError("Unknown authentication failure code")
        super().__init__(MESSAGES[code], code=code)


class CustomerError(exceptions.APIException):
    """A customer-audience error code (customer contract §1.6) with its catalog key (§5.0).

    ``key`` defaults to the class's ``default_key``, then ``error.<code>``; ``params`` fill the key's
    placeholders; ``details`` is the code's ``details`` object (``null`` otherwise); ``retry_after`` (seconds)
    becomes the ``Retry-After`` header and, unless given, fills the key's ``{n}`` (seconds) or ``{m}``
    (minutes, rounded up) and, for the codes whose ``details`` is ``{"retry_after": int}`` (C§1.6), the
    ``details`` too, so ``OtpResendTooSoon(retry_after=30)`` is complete. The exception's own detail is the
    Uzbek text, so a misuse outside a customer view still reads correctly; customer views render the key in
    the request language.
    """

    error_code: ClassVar[str] = ""
    default_key: ClassVar[str | None] = None
    retry_after_details: ClassVar[bool] = False
    status_code = 409

    def __init__(
        self,
        code: str | None = None,
        status: int | None = None,
        *,
        key: str | None = None,
        details: Any = None,
        params: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        code = code or self.error_code
        if not code:
            raise ValueError("CustomerError needs a code")
        if status is not None:
            self.status_code = status
        self.key = key or self.default_key or f"error.{code}"
        if self.key not in i18n.CATALOG:
            raise ValueError(f"No catalog key {self.key!r} for code {code!r}")
        self.params: dict[str, Any] = dict(params or {})
        if retry_after is not None:
            self.wait = max(1, int(retry_after))
            names = i18n.placeholders(i18n.CATALOG[self.key][i18n.DEFAULT_LANGUAGE])
            if "n" in names:
                self.params.setdefault("n", self.wait)
            if "m" in names:
                self.params.setdefault("m", math.ceil(self.wait / 60))
            if details is None and self.retry_after_details:
                details = {"retry_after": self.wait}
        self.details = details
        super().__init__(i18n.t(self.key, "uz", **self.params), code=code)


class DeviceRequired(CustomerError):
    """400 ``device_required``: Device headers missing/malformed."""

    error_code = "device_required"
    status_code = 400


class OtpInvalid(CustomerError):
    """400 ``otp_invalid``: Wrong SMS code; details.attempts_left."""

    error_code = "otp_invalid"
    status_code = 400


class OtpExpired(CustomerError):
    """400 ``otp_expired``: No live code for (phone, purpose)."""

    error_code = "otp_expired"
    status_code = 400


class VerificationInvalid(CustomerError):
    """400 ``verification_invalid``: Verification token unusable."""

    error_code = "verification_invalid"
    status_code = 400


class SocialTokenInvalid(CustomerError):
    """400 ``social_token_invalid``: Provider token, nonce or pair refused."""

    error_code = "social_token_invalid"
    status_code = 400


class SocialTicketInvalid(CustomerError):
    """400 ``social_ticket_invalid``: Social ticket unusable or of another device."""

    error_code = "social_ticket_invalid"
    status_code = 400


class PasswordChanged(CustomerError):
    """401 ``password_changed``: Session revoked because the password was reset or changed."""

    error_code = "password_changed"
    status_code = 401


class PhoneRegistered(CustomerError):
    """409 ``phone_registered``: An active customer has it."""

    error_code = "phone_registered"
    status_code = 409


class PhoneNotRegistered(CustomerError):
    """409 ``phone_not_registered``: No active customer has this phone."""

    error_code = "phone_not_registered"
    status_code = 409


class EmailRegistered(CustomerError):
    """409 ``email_registered``: An active customer has this e-mail (hybrid identity)."""

    error_code = "email_registered"
    status_code = 409


class EmailNotRegistered(CustomerError):
    """409 ``email_not_registered``: No active customer has this e-mail."""

    error_code = "email_not_registered"
    status_code = 409


class PhoneTaken(CustomerError):
    """409 ``phone_taken``: Attaching a phone that belongs to another account (no merge: sign in with it)."""

    error_code = "phone_taken"
    status_code = 409


class EmailTaken(CustomerError):
    """409 ``email_taken``: Attaching an e-mail that belongs to another account."""

    error_code = "email_taken"
    status_code = 409


class PhoneRequired(CustomerError):
    """409 ``phone_required``: Checkout, pay, booking and the QR need a verified phone first."""

    error_code = "phone_required"
    status_code = 409


class ReauthRequired(CustomerError):
    """409 ``reauth_required``: A sign-in-method change of an account with no password, phone or e-mail needs
    a fresh sign-in."""

    error_code = "reauth_required"
    status_code = 409


class SocialProviderLinked(CustomerError):
    """409 ``social_provider_linked``: The customer already has this provider."""

    error_code = "social_provider_linked"
    status_code = 409


class SocialIdentityLinked(CustomerError):
    """409 ``social_identity_linked``: The identity belongs to another customer."""

    error_code = "social_identity_linked"
    status_code = 409


class LastSignInMethod(CustomerError):
    """409 ``last_sign_in_method``: Unlinking the only way to sign in."""

    error_code = "last_sign_in_method"
    status_code = 409


class TermsUpdateRequired(CustomerError):
    """409 ``terms_update_required``: Consent older than the current terms."""

    error_code = "terms_update_required"
    status_code = 409


class ProductUnavailable(CustomerError):
    """409 ``product_unavailable``: Product not visible."""

    error_code = "product_unavailable"
    status_code = 409


class CartFull(CustomerError):
    """409 ``cart_full``: Too many distinct lines; details.max_lines."""

    error_code = "cart_full"
    status_code = 409


class CartEmpty(CustomerError):
    """409 ``cart_empty``: Checkout of an empty cart."""

    error_code = "cart_empty"
    status_code = 409


class PriceChanged(CustomerError):
    """409 ``price_changed``: expected_total differs."""

    error_code = "price_changed"
    status_code = 409


class OpenOrderExists(CustomerError):
    """409 ``open_order_exists``: An unpaid order exists."""

    error_code = "open_order_exists"
    status_code = 409


class DeliveryUnavailable(CustomerError):
    """409 ``delivery_unavailable``: BTS refuses the route or delivery is not offered."""

    error_code = "delivery_unavailable"
    status_code = 409


class OnlineSalesUnavailable(CustomerError):
    """409 ``online_sales_unavailable``: Online selling is off (fake Billz)."""

    error_code = "online_sales_unavailable"
    status_code = 409


class NotPayable(CustomerError):
    """409 ``not_payable``: The order cannot be paid."""

    error_code = "not_payable"
    status_code = 409


class OrderExpired(CustomerError):
    """409 ``order_expired``: The payment time ran out."""

    error_code = "order_expired"
    status_code = 409


class AlreadyPaid(CustomerError):
    """409 ``already_paid``: The order is paid."""

    error_code = "already_paid"
    status_code = 409


class PaymentInProgress(CustomerError):
    """409 ``payment_in_progress``: A provider transaction is in flight."""

    error_code = "payment_in_progress"
    status_code = 409


class SlotTaken(CustomerError):
    """409 ``slot_taken``: Booking race lost."""

    error_code = "slot_taken"
    status_code = 409


class BookingLimit(CustomerError):
    """409 ``booking_limit``: Too many open appointments."""

    error_code = "booking_limit"
    status_code = 409


class AddressLimit(CustomerError):
    """409 ``address_limit``: Too many saved addresses."""

    error_code = "address_limit"
    status_code = 409


class QrInvalid(CustomerError):
    """409 ``qr_invalid``: POS: token unusable (one answer for all)."""

    error_code = "qr_invalid"
    status_code = 409


class AppUpdateRequired(CustomerError):
    """426 ``app_update_required``: App below the minimum version."""

    error_code = "app_update_required"
    status_code = 426


class OtpResendTooSoon(CustomerError):
    """429 ``otp_resend_too_soon``: Resend wait not over."""

    error_code = "otp_resend_too_soon"
    retry_after_details = True
    status_code = 429


class OtpDailyLimit(CustomerError):
    """429 ``otp_daily_limit``: Daily code cap reached."""

    error_code = "otp_daily_limit"
    retry_after_details = True
    status_code = 429


class OtpNetworkLimit(CustomerError):
    """429 ``otp_network_limit``: IP day cap reached."""

    error_code = "otp_network_limit"
    retry_after_details = True
    status_code = 429


class OtpLocked(CustomerError):
    """429 ``otp_locked``: Lockout or daily wrong-code cap."""

    error_code = "otp_locked"
    retry_after_details = True
    status_code = 429


class SmsUnavailable(CustomerError):
    """503 ``sms_unavailable``: The global SMS budget tripped."""

    error_code = "sms_unavailable"
    retry_after_details = True
    status_code = 503


class EmailServiceUnavailable(CustomerError):
    """503 ``email_unavailable``: The global e-mail budget tripped (e-mail twin of ``sms_unavailable``)."""

    error_code = "email_unavailable"
    retry_after_details = True
    status_code = 503


class TermsNotPublished(CustomerError):
    """503 ``terms_not_published``: Registration while the terms are unpublished."""

    error_code = "terms_not_published"
    status_code = 503


class OutOfStock(CustomerError):
    """409 ``OUT_OF_STOCK``: ``lines`` (checkout, key ``error.OUT_OF_STOCK``) or one product (cart add/patch,
    key ``error.out_of_stock_one``), per customer contract §5.0."""

    error_code = "OUT_OF_STOCK"

    def __init__(self, *, details: Any = None, one: bool = False) -> None:
        super().__init__(key="error.out_of_stock_one" if one else "error.OUT_OF_STOCK", details=details)


# Codes whose catalog key depends on the resource (customer contract §5.0).
NOT_CANCELLABLE_KEYS = {
    "order": "error.order_not_cancellable",
    "appointment": "error.appointment_not_cancellable",
    "": "error.not_cancellable",
}
DELIVERY_UNAVAILABLE_KEYS = {
    "route": "error.delivery_unavailable",
    "not_offered": "error.delivery_not_offered",
}


def not_cancellable(resource: str = "") -> CustomerError:
    """409 ``not_cancellable`` with the key of ``resource`` (``order``, ``appointment`` or any other)."""
    return CustomerError("not_cancellable", 409, key=NOT_CANCELLABLE_KEYS[resource])


def flatten_errors(detail: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str | None, str]] = set()

    def visit(value: Any, path: str | None = None) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                field = None if key == "non_field_errors" else ".".join(filter(None, (path, str(key))))
                visit(child, field)
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                field = (
                    ".".join(filter(None, (path, str(index)))) if isinstance(child, (dict, list)) else path
                )
                visit(child, field)
        else:
            code = str(getattr(value, "code", "invalid"))
            if (path, code) not in seen:
                seen.add((path, code))
                result.append({"field": path, "code": code, "message": _field_message(value, code)})

    visit(detail)
    return result


def error_body(
    code: str, request: Any, *, message: str | None = None, errors: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "code": code,
        "message": MESSAGES.get(code, "Maydonlarni tekshiring.") if message is None else message,
        "errors": errors or [],
        "request_id": getattr(request, "request_id", ""),
    }


def _is_stock_exception(exc: exceptions.APIException) -> bool:
    return type(exc).__module__ == exceptions.__name__


def _uses_default_detail(exc: exceptions.APIException) -> bool:
    detail, default = str(exc.detail), str(exc.default_detail)
    if detail == default:
        return True
    # DRF formats these class defaults in the constructor without retaining the arguments.
    if isinstance(exc, (exceptions.MethodNotAllowed, exceptions.UnsupportedMediaType)):
        pattern = "".join(
            re.escape(literal) + (".*" if name is not None else "")
            for literal, name, _format_spec, _conversion in Formatter().parse(default)
        )
        return re.fullmatch(pattern, detail, flags=re.DOTALL) is not None
    return False


def envelope_exception_handler(exc: Exception, context: dict[str, Any]) -> Response:
    if getattr(context.get("view"), "audience", None) in CUSTOMER_AUDIENCES:
        return customer_exception_handler(exc, context)
    response = exception_handler(exc, context)
    request = context.get("request")
    if isinstance(exc, SuspiciousOperation):
        logging.getLogger(f"django.security.{type(exc).__name__}").error(
            str(exc),
            exc_info=exc,
            extra={"status_code": 413 if isinstance(exc, RequestDataTooBig) else 400, "request": request},
        )
    if isinstance(exc, RequestDataTooBig):
        set_rollback()
        return Response(error_body("payload_too_large", request), status=413)
    if isinstance(exc, (BadRequest, SuspiciousOperation)):
        set_rollback()
        return Response(error_body("bad_request", request), status=400)
    if response is None:
        set_rollback()
        logger.exception(
            "Unexpected API exception request_id=%s path=%s",
            getattr(request, "request_id", ""),
            getattr(request, "path", ""),
            exc_info=exc,
        )
        return Response(error_body("server_error", request), status=500)
    code = "not_found" if isinstance(exc, Http404) else str(getattr(exc, "default_code", "permission_denied"))
    errors = None
    message = None
    if isinstance(exc, exceptions.APIException) and not isinstance(exc, exceptions.ValidationError):
        code = str(exc.get_codes())
        message = str(exc.detail)
        # Swap DRF's English defaults (and anything with a MESSAGES entry) for the Uzbek text; a project
        # subclass raised bare keeps its own default_detail instead of the generic "check the fields".
        if _uses_default_detail(exc) and (code in MESSAGES or _is_stock_exception(exc)):
            message = MESSAGES.get(code, MESSAGES["validation_error"])
    if isinstance(exc, exceptions.ValidationError):
        code = "validation_error"
        errors = flatten_errors(exc.detail)
        message = getattr(exc, "top_level_message", None)
    elif isinstance(exc, exceptions.ParseError) and code == "parse_error":
        message = MESSAGES.get("parse_error", MESSAGES["validation_error"])
    elif isinstance(exc, (exceptions.NotAuthenticated, exceptions.AuthenticationFailed)):
        code = "not_authenticated"
        response.status_code = 401
        if str(exc.detail) == str(exc.default_detail):
            message = MESSAGES[code]
    elif isinstance(exc, AuthFailed):
        code = str(exc.get_codes())
    elif isinstance(exc, exceptions.Throttled):
        code = "throttled"
        wait = getattr(exc, "wait", None)
        message = (
            "Juda ko‘p urinish."
            if wait is None
            else f"Juda ko‘p urinish. {wait} soniyadan so‘ng qayta urinib ko‘ring."
        )
    response.data = error_body(code, request, message=message, errors=errors)
    return response


# --- customer audience (customer contract §1.6, §5) ---

_STATUS_FALLBACK_CODES = {
    400: "bad_request",
    401: "not_authenticated",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "throttled",
    503: "provider_unavailable",
}
# Paths outside the customer audience: their 404/500 pages keep the admin envelope byte for byte. Matched per
# segment, with or without the trailing slash (``/api/v1/admin`` is not redirected: ``admin/`` is no route).
_NON_CUSTOMER_PREFIXES = ("/api/v1/admin", "/api/v1/schema", "/api/v1/docs", "/api/v1/health")


def _placeholder_pattern(template: str) -> re.Pattern[str]:
    pattern = ""
    names: set[str] = set()
    for literal, name, _spec, _conversion in Formatter().parse(template):
        pattern += re.escape(literal)
        if name is not None:
            pattern += f"(?P={name})" if name in names else f"(?P<{name}>.+?)"
            names.add(name)
    return re.compile(pattern)


@lru_cache(maxsize=1)
def _field_patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple(
        (key, _placeholder_pattern(texts["uz"]))
        for key, texts in i18n.CATALOG.items()
        if key.startswith("field.")
    )


# An integer as ``i18n.format_number(n, "uz")`` renders it: optional U+2212 minus, NBSP-grouped ASCII digits.
_UZ_NUMBER = re.compile("(\u2212?)([0-9]{1,3}(?:\u00a0[0-9]{3})*)")


def _localized_value(value: str, lang: str) -> str:
    match = _UZ_NUMBER.fullmatch(value)
    if match is None:
        return value
    number = int(match.group(2).replace("\u00a0", ""))
    return i18n.format_number(-number if match.group(1) else number, lang)


def localize_field_message(code: str, message: str, lang: str) -> str:
    """Render a field error's Uzbek ``message`` in ``lang`` through the catalog.

    The detail of a field error is its Uzbek text (``field_error`` renders the catalog key in Uzbek, DRF's own
    texts are swapped for ``FIELD_MESSAGES`` first), so the key is found by matching the Uzbek templates:
    ``field.<code>`` first, then every other ``field.*`` key; the placeholders read back from the text fill
    the target language, and a value written as an Uzbek-formatted integer is re-grouped for ``lang``
    (``field_error`` formats ``int`` params that way). A text that matches no template (a serializer-specific
    message) is returned unchanged.
    """
    preferred = f"field.{code}"
    patterns = sorted(_field_patterns(), key=lambda item: item[0] != preferred)
    for key, pattern in patterns:
        match = pattern.fullmatch(message)
        if match is not None:
            values = {name: _localized_value(value, lang) for name, value in match.groupdict().items()}
            return i18n.t(key, lang, **values)
    return message


def _request_customer(request: Any) -> Any:
    """The authenticated customer of a DRF request without re-running authentication (else ``None``)."""
    user = vars(request).get("_user") if request is not None and hasattr(request, "__dict__") else None
    if getattr(user, "is_authenticated", False) and isinstance(getattr(user, "language", None), str):
        return user
    return None


def request_language(request: Any) -> str:
    return i18n.resolve_language(request, _request_customer(request))


def customer_error_body(
    code: str,
    request: Any,
    *,
    lang: str | None = None,
    key: str | None = None,
    params: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
    details: Any = None,
    status: int | None = None,
) -> dict[str, Any]:
    """The customer envelope ``{code, message, errors, details, request_id}`` with a localized ``message``.

    ``key`` defaults to ``error.<code>``; a code without a catalog key (never expected: a contract test maps
    every code a customer view can raise) falls back to the key of its HTTP status and logs a warning.
    """
    if key is None:
        key = f"error.{code}"
        if key not in i18n.CATALOG:
            fallback = _STATUS_FALLBACK_CODES.get(status or 0, "server_error")
            logger.warning("No catalog key for customer error code=%s status=%s", code, status)
            key = f"error.{fallback}"
    values = dict(params or {})
    if key == "error.throttled":
        values.setdefault("n", 60)
    return {
        "code": code,
        "message": i18n.t(key, lang or request_language(request), **values),
        "errors": errors or [],
        "details": details,
        "request_id": getattr(request, "request_id", ""),
    }


def customer_exception_handler(exc: Exception, context: dict[str, Any]) -> Response:
    """The envelope of customer, public, auth, POS and webhook views: ``details`` plus localized messages."""
    response = exception_handler(exc, context)
    request = context.get("request")
    lang = request_language(request)
    if isinstance(exc, SuspiciousOperation):
        logging.getLogger(f"django.security.{type(exc).__name__}").error(
            str(exc),
            exc_info=exc,
            extra={"status_code": 413 if isinstance(exc, RequestDataTooBig) else 400, "request": request},
        )
    if isinstance(exc, RequestDataTooBig):
        set_rollback()
        return Response(customer_error_body("payload_too_large", request, lang=lang), status=413)
    if isinstance(exc, (BadRequest, SuspiciousOperation)):
        set_rollback()
        return Response(customer_error_body("bad_request", request, lang=lang), status=400)
    if response is None:
        set_rollback()
        logger.exception(
            "Unexpected API exception request_id=%s path=%s",
            getattr(request, "request_id", ""),
            getattr(request, "path", ""),
            exc_info=exc,
        )
        return Response(customer_error_body("server_error", request, lang=lang), status=500)
    key: str | None = None
    params: dict[str, Any] = {}
    details: Any = None
    errors: list[dict[str, Any]] | None = None
    if isinstance(exc, CustomerError):
        code, key, params, details = str(exc.get_codes()), exc.key, exc.params, exc.details
    elif isinstance(exc, exceptions.ValidationError):
        code = "validation_error"
        errors = [
            {**item, "message": localize_field_message(item["code"], item["message"], lang)}
            for item in flatten_errors(exc.detail)
        ]
    elif isinstance(exc, Http404) or isinstance(exc, exceptions.NotFound):
        code = "not_found"
    elif isinstance(exc, (exceptions.NotAuthenticated, exceptions.AuthenticationFailed)):
        code = "not_authenticated"
        response.status_code = 401
    elif isinstance(exc, exceptions.Throttled):
        code = "throttled"
        wait = getattr(exc, "wait", None)
        params = {"n": int(wait) if wait is not None else 60}
    elif isinstance(exc, exceptions.APIException):
        codes = exc.get_codes()
        code = codes if isinstance(codes, str) else str(exc.default_code)
    else:
        code = "permission_denied"
    view = context.get("view")
    if response.status_code == 401 and not response.has_header("WWW-Authenticate") and view is not None:
        challenge = view.get_authenticate_header(request) if request is not None else None
        if challenge:
            response["WWW-Authenticate"] = challenge
    response.data = customer_error_body(
        code,
        request,
        lang=lang,
        key=key,
        params=params,
        errors=errors,
        details=details,
        status=response.status_code,
    )
    return response


def is_customer_path(path: str) -> bool:
    if not path.startswith("/api/v1/"):
        return False
    return not any(path == prefix or path.startswith(prefix + "/") for prefix in _NON_CUSTOMER_PREFIXES)


def _customer_page(request: HttpRequest, code: str, status: int) -> JsonResponse:
    lang = i18n.resolve_language(request)
    response = JsonResponse(customer_error_body(code, request, lang=lang), status=status)
    response["Content-Language"] = lang
    response["Cache-Control"] = "no-store"
    return response


def not_found_view(request: HttpRequest, exception: Exception) -> JsonResponse:
    if is_customer_path(request.path):
        return _customer_page(request, "not_found", 404)
    return JsonResponse(error_body("not_found", request), status=404)


def server_error_view(request: HttpRequest) -> JsonResponse:
    logger.exception(
        "Unexpected server exception request_id=%s path=%s", getattr(request, "request_id", ""), request.path
    )
    if is_customer_path(request.path):
        return _customer_page(request, "server_error", 500)
    return JsonResponse(error_body("server_error", request), status=500)
