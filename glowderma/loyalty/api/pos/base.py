"""``PosAPIView`` (customer contract §1.1, §2.10): the base of every ``pos/*`` route (**K**).

``PosKeyAuthentication`` (``Authorization: Api-Key <key>``, failed attempts bounded per IP) +
``IsActivePosDevice`` + ``PosRateThrottle`` (``pos``, per device). The ``pos`` audience gives the customer
error envelope (``details``) with messages in Uzbek unless ``Accept-Language`` asks otherwise. It lives here,
not in ``accounts``, because its authentication reads ``loyalty.PosDevice``. Cashier terminals send no device
headers.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from glowderma.accounts.api.throttles import PosRateThrottle
from glowderma.core.api.exceptions import request_language
from glowderma.core.api.parsers import JSONParser
from glowderma.loyalty.authentication import IsActivePosDevice, PosKeyAuthentication


class PosAPIView(APIView):
    audience: ClassVar[str] = "pos"
    parser_classes = [JSONParser]
    authentication_classes = [PosKeyAuthentication]
    permission_classes = [IsActivePosDevice]
    throttle_classes = [PosRateThrottle]

    def finalize_response(self, request: Request, response: Response, *args: Any, **kwargs: Any) -> Response:
        response = super().finalize_response(request, response, *args, **kwargs)
        if not response.has_header("Content-Language"):
            response["Content-Language"] = request_language(request)
        if not response.has_header("Cache-Control"):
            response["Cache-Control"] = "no-store"
        return response
