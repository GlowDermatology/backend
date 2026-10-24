import logging
import re
from functools import lru_cache
from string import Formatter
from typing import Any

from django.core.exceptions import RequestDataTooBig
from django.http import Http404, HttpRequest, JsonResponse
from django.utils.translation import get_language, override
from rest_framework import exceptions, fields, relations, serializers, validators
from rest_framework.response import Response
from rest_framework.views import exception_handler

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
}


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


class InvalidTransition(exceptions.APIException):
    status_code = 409
    default_code = "invalid_transition"


class NotCancellable(exceptions.APIException):
    status_code = 409
    default_code = "not_cancellable"


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


class AuthFailed(exceptions.APIException):
    status_code = 401

    def __init__(self, code: str = "not_authenticated") -> None:
        if code not in {"not_authenticated", "SESSION_EXPIRED", "SESSION_REPLACED", "invalid_credentials"}:
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
        "message": message or MESSAGES.get(code, "Maydonlarni tekshiring."),
        "errors": errors or [],
        "request_id": getattr(request, "request_id", ""),
    }


def envelope_exception_handler(exc: Exception, context: dict[str, Any]) -> Response:
    response = exception_handler(exc, context)
    request = context.get("request")
    if isinstance(exc, RequestDataTooBig):
        return Response(error_body("payload_too_large", request), status=413)
    if response is None:
        logger.exception("Unexpected API exception", exc_info=exc)
        return Response(error_body("server_error", request), status=500)
    code = "not_found" if isinstance(exc, Http404) else str(getattr(exc, "default_code", "permission_denied"))
    errors = None
    message = None
    if isinstance(exc, exceptions.ValidationError):
        code = "validation_error"
        errors = flatten_errors(exc.detail)
    elif isinstance(exc, (exceptions.NotAuthenticated, exceptions.AuthenticationFailed)):
        code = "not_authenticated"
        response.status_code = 401
    elif isinstance(exc, AuthFailed):
        code = str(exc.get_codes())
    elif isinstance(exc, exceptions.Throttled):
        code = "throttled"
        message = f"Juda ko‘p urinish. {getattr(exc, 'wait', 0) or 0} soniyadan so‘ng qayta urinib ko‘ring."
    response.data = error_body(code, request, message=message, errors=errors)
    return response


def not_found_view(request: HttpRequest, exception: Exception) -> JsonResponse:
    return JsonResponse(error_body("not_found", request), status=404)


def server_error_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse(error_body("server_error", request), status=500)
