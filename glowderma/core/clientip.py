import hmac

from django.conf import settings
from django.http import HttpRequest


def has_internal_secret(request: HttpRequest) -> bool:
    try:
        header = request.META.get("HTTP_X_GD_INTERNAL_SECRET", "").encode("latin-1")
    except UnicodeEncodeError:
        return False
    return bool(settings.INTERNAL_API_SECRET) and hmac.compare_digest(
        header, settings.INTERNAL_API_SECRET.encode()
    )
