"""Admin view of a customer's signed-in devices (B-W1, admin contract §2.6): the same session list the
customer sees under «Faol seanslar», and signing one session out (support: a lost phone, a shared
computer)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import sessions as account_sessions
from glowderma.accounts.api.base import AdminAPIView, AdminGenericViewSet
from glowderma.core import enums
from glowderma.core.api.fields import AwareDateTimeField
from glowderma.core.api.schema import ADMIN_ERRORS, OPTIONAL_IDEMPOTENCY_KEY_HEADER
from glowderma.core.idempotency import idempotent
from glowderma.customers import selectors

SESSION_ID_PARAM = OpenApiParameter(name="session_id", type=OpenApiTypes.UUID, location=OpenApiParameter.PATH)


class AdminCustomerSessionSerializer(serializers.Serializer[Any]):
    """One open session of the customer (``CustomerSession`` without ``current``)."""

    id = serializers.UUIDField()
    device = serializers.CharField()
    platform = serializers.ChoiceField(choices=enums.OrderChannel.values, allow_null=True)
    location = serializers.CharField(help_text='"Tashkent, Tashkent, UZ"; "—" when unknown.')
    created_at = AwareDateTimeField()
    updated_at = AwareDateTimeField()


def _payload(session: Any) -> dict[str, Any]:
    view = account_sessions.session_view(session, language=enums.ContentLanguage.UZ, current_sid=None)
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

    @extend_schema(
        operation_id="admin_customers_sessions_list",
        description=(
            "The customer's open sessions (signed-in phones and browsers), most recently active first."
        ),
        responses={200: AdminCustomerSessionSerializer(many=True), **ADMIN_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        customer_id = int(kwargs["pk"])
        if not selectors.customer_exists(customer_id):
            raise NotFound()
        page = self.paginate_queryset(account_sessions.open_sessions(customer_id))
        rows = [_payload(session) for session in page or []]
        return self.get_paginated_response(AdminCustomerSessionSerializer(rows, many=True).data)


class CustomerSessionDetailView(AdminAPIView):
    @extend_schema(
        operation_id="admin_customers_sessions_destroy",
        description=(
            "Sign one session of the customer out (reason `admin_revoked`; the device stops receiving pushes "
            "when no other session is left on it). Unknown or already ended session → 404."
        ),
        parameters=[SESSION_ID_PARAM, OPTIONAL_IDEMPOTENCY_KEY_HEADER],
        request=None,
        responses={204: None, **ADMIN_ERRORS},
    )
    @idempotent(required=False)
    def delete(self, request: Request, pk: int, session_id: Any) -> Response:
        if not selectors.customer_exists(int(pk)):
            raise NotFound()
        try:
            account_sessions.revoke_session(
                int(pk), str(session_id), actor=request.user, actor_type="staff", request=request
            )
        except account_sessions.SessionNotFound as exc:
            raise NotFound() from exc
        return Response(status=204)
