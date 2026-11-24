"""``me/sessions/*`` of the customer API (B-W1, customer contract §2.2.10): the "Faol seanslar" list — every
signed-in phone and browser of the customer — and signing one of them, or all the others, out."""

from __future__ import annotations

from typing import Any, cast

from rest_framework import mixins
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import selectors as account_selectors
from glowderma.accounts import services as account_services
from glowderma.accounts.api.customer_base import (
    CustomerAPIView,
    CustomerGenericViewSet,
)
from glowderma.accounts.models import Customer
from glowderma.core.idempotency import idempotent
from glowderma.customers.api.customer.payloads import session_payload
from glowderma.customers.api.customer.schemas.sessions import (
    REVOKE_OTHER_SESSIONS_POST,
    SESSION_DETAIL_DELETE,
    SESSION_LIST_LIST,
)
from glowderma.customers.api.customer.serializers.sessions import (
    CustomerSessionSerializer,
)


def _sid(request: Request) -> str:
    return str(cast(dict[str, Any], request.auth)["sid"])


class SessionListViewSet(mixins.ListModelMixin, CustomerGenericViewSet):
    serializer_class = CustomerSessionSerializer
    page_size_options = (20, 50)
    default_page_size = 20

    @SESSION_LIST_LIST
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        customer = cast(Customer, request.user)
        page = self.paginate_queryset(account_selectors.open_sessions(customer.pk))
        sid, language = _sid(request), self.language
        rows = [session_payload(session, language=language, current_sid=sid) for session in page or []]
        return self.get_paginated_response(CustomerSessionSerializer(rows, many=True).data)


class SessionDetailView(CustomerAPIView):
    @SESSION_DETAIL_DELETE
    @idempotent(required=False)
    def delete(self, request: Request, pk: Any) -> Response:
        customer = cast(Customer, request.user)
        try:
            account_services.revoke_open_customer_session(
                customer.pk,
                str(pk),
                actor=customer,
                actor_type="customer",
                request=request,
                current_sid=_sid(request),
            )
        except account_services.SessionNotFound as exc:
            raise NotFound() from exc
        return Response(status=204)


class RevokeOtherSessionsView(CustomerAPIView):
    @REVOKE_OTHER_SESSIONS_POST
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        customer = cast(Customer, request.user)
        count = account_services.revoke_other_customer_sessions(
            customer.pk, _sid(request), actor=customer, request=request
        )
        return Response({"revoked": count})


__all__ = [
    "RevokeOtherSessionsView",
    "SessionDetailView",
    "SessionListViewSet",
]
