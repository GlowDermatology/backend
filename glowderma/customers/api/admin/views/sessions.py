"""Admin view of a customer's signed-in devices (B-W1, admin contract §2.6): the same session list the
customer sees under «Faol seanslar», and signing one session out (support: a lost phone, a shared
computer)."""

from __future__ import annotations

from typing import Any

from rest_framework import mixins
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import selectors as account_selectors
from glowderma.accounts import services as account_services
from glowderma.accounts.api.base import AdminAPIView, AdminGenericViewSet
from glowderma.core import enums
from glowderma.core.idempotency import idempotent
from glowderma.customers import selectors
from glowderma.customers.api.admin.schemas.sessions import (
    CUSTOMER_SESSION_DETAIL_DELETE,
    CUSTOMER_SESSIONS_LIST,
)
from glowderma.customers.api.admin.serializers.sessions import AdminCustomerSessionSerializer


def _payload(session: Any) -> dict[str, Any]:
    view = account_selectors.session_view(session, language=enums.ContentLanguage.UZ, current_sid=None)
    return {
        "id": view.id,
        "device": view.device,
        "platform": view.platform or None,
        "location": view.location,
        "created_at": view.created_at,
        "updated_at": view.updated_at,
    }


class CustomerSessionsViewSet(mixins.ListModelMixin, AdminGenericViewSet):
    serializer_class = AdminCustomerSessionSerializer
    page_size_options = (20, 50)
    default_page_size = 20

    @CUSTOMER_SESSIONS_LIST
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        customer_id = int(kwargs["pk"])
        if not selectors.customer_exists(customer_id):
            raise NotFound()
        page = self.paginate_queryset(account_selectors.open_sessions(customer_id))
        rows = [_payload(session) for session in page or []]
        return self.get_paginated_response(AdminCustomerSessionSerializer(rows, many=True).data)


class CustomerSessionDetailView(AdminAPIView):
    @CUSTOMER_SESSION_DETAIL_DELETE
    @idempotent(required=False)
    def delete(self, request: Request, pk: int, session_id: Any) -> Response:
        if not selectors.customer_exists(int(pk)):
            raise NotFound()
        try:
            account_services.revoke_open_customer_session(
                int(pk), str(session_id), actor=request.user, actor_type="staff", request=request
            )
        except account_services.SessionNotFound as exc:
            raise NotFound() from exc
        return Response(status=204)


__all__ = [
    "CustomerSessionDetailView",
    "CustomerSessionsViewSet",
]
