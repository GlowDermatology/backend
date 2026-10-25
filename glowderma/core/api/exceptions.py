"""Error envelope (admin API v1, section 1.6).

Field error codes come in two groups:

* **Generic codes** carry a default Uzbek message from ``FIELD_MESSAGES`` (``required``, ``invalid``,
  ``read_only``, ``file_too_large``, ``unsupported_image``, ``max_items``, ``mismatch``, ...).
  :func:`field_error` builds such an ``ErrorDetail``; :func:`flatten_errors` also swaps DRF's stock
  English messages for the Uzbek default.
* **Endpoint-specific codes** (``ENDPOINT_FIELD_CODES``: ``customer_not_found``, ``in_past``,
  ``empty_range``, ``overlap``, ``out_of_range``, ``effective_from_in_past``, ``insufficient_balance``,
  ``billz_not_found``, ``required_for_active``, ``export_too_large``) have no default: the view raises
  them with the explicit message its contract table lists, e.g.
  ``ValidationError({"amount": [field_error("insufficient_balance", "Balansdan ko‘p ayirib bo‘lmaydi.")]})``.
"""

import logging
import re
from functools import lru_cache
from string import Formatter
from typing import Any

from django.core.exceptions import BadRequest, RequestDataTooBig, SuspiciousOperation
from django.http import Http404, HttpRequest, JsonResponse
from django.utils.translation import get_language, override
from rest_framework import exceptions, fields, relations, serializers, validators
from rest_framework.response import Response
from rest_framework.views import exception_handler, set_rollback

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
    }
)
AUTH_FAILURE_CODES = frozenset(
    {
        "not_authenticated",
        "SESSION_EXPIRED",
        "SESSION_REPLACED",
        "invalid_credentials",
        "social_account_not_allowed",
    }
)


def field_error(
    code: str, message: str | None = None, *, limit: int | str | None = None
) -> exceptions.ErrorDetail:
    """An ``ErrorDetail`` with a contract code.

    Generic codes default to their ``FIELD_MESSAGES`` entry (``limit`` fills ``{limit}``); endpoint-specific
    codes require an explicit ``message``.
    """
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
        if _uses_default_detail(exc):
            message = MESSAGES.get(code, MESSAGES["validation_error"])
    if isinstance(exc, exceptions.ValidationError):
        code = "validation_error"
        errors = flatten_errors(exc.detail)
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


def not_found_view(request: HttpRequest, exception: Exception) -> JsonResponse:
    return JsonResponse(error_body("not_found", request), status=404)


def server_error_view(request: HttpRequest) -> JsonResponse:
    logger.exception(
        "Unexpected server exception request_id=%s path=%s", getattr(request, "request_id", ""), request.path
    )
    return JsonResponse(error_body("server_error", request), status=500)
