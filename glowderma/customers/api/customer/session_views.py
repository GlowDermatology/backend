"""``me/sessions/*`` of the customer API (B-W1, customer contract §2.2.10): the "Faol seanslar" list — every
signed-in phone and browser of the customer — and signing one of them, or all the others, out."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, cast

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, serializers
from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import sessions as account_sessions
from glowderma.accounts.api.customer_base import CustomerAPIView, CustomerGenericViewSet
from glowderma.accounts.models import Customer
from glowderma.core import enums
from glowderma.core.api.fields import AwareDateTimeField
from glowderma.core.api.schema import (
    ACCEPT_LANGUAGE_HEADER,
    CUSTOMER_ERRORS,
    DEVICE_HEADERS,
    OPTIONAL_IDEMPOTENCY_KEY_HEADER,
)
from glowderma.core.idempotency import idempotent

PARAMETERS = [*DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER]
SESSION_ID_PARAM = OpenApiParameter(name="id", type=OpenApiTypes.UUID, location=OpenApiParameter.PATH)


class CustomerSessionSerializer(serializers.Serializer[Any]):
    """One signed-in device of the customer (``CustomerSession``)."""

    id = serializers.UUIDField()
    device = serializers.CharField(help_text='"Chrome (Windows)", "Glow Derma iOS (iPhone 15)".')
    platform = serializers.ChoiceField(choices=enums.OrderChannel.values, allow_null=True)
    location = serializers.CharField(help_text='"Tashkent, Tashkent, UZ"; "—" when unknown.')
    created_at = AwareDateTimeField(help_text="The sign-in.")
    updated_at = AwareDateTimeField(help_text="The last activity (recorded at most every 5 minutes).")
    current = serializers.BooleanField(help_text="This request's own session.")


class RevokedSessionsSerializer(serializers.Serializer[Any]):
    revoked = serializers.IntegerField(min_value=0)


def _sid(request: Request) -> str:
    return str(cast(dict[str, Any], request.auth)["sid"])


def session_payload(session: Any, *, language: str, current_sid: str | None) -> dict[str, Any]:
    view = account_sessions.session_view(session, language=language, current_sid=current_sid)
    row = asdict(view)
    row["platform"] = row["platform"] or None
    return row


class SessionListViewSet(mixins.ListModelMixin, CustomerGenericViewSet):
    serializer_class = CustomerSessionSerializer
    page_size_options = (20, 50)
    default_page_size = 20

    @extend_schema(
        operation_id="customer_sessions_list",
        description=(
            "Every open session of the customer (each signed-in phone or browser), most recently active "
            "first; `current` marks the caller's own."
        ),
        parameters=PARAMETERS,
        responses={200: CustomerSessionSerializer(many=True), **CUSTOMER_ERRORS},
    )
    def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        customer = cast(Customer, request.user)
        page = self.paginate_queryset(account_sessions.open_sessions(customer.pk))
        sid, language = _sid(request), self.language
        rows = [session_payload(session, language=language, current_sid=sid) for session in page or []]
        return self.get_paginated_response(CustomerSessionSerializer(rows, many=True).data)


class SessionDetailView(CustomerAPIView):
    @extend_schema(
        operation_id="customer_sessions_destroy",
        description=(
            "Sign one session out (the row menu «Chiqish»). The caller's own session may be named too: it is "
            "a logout. Unknown, already ended or another customer's session → 404."
        ),
        parameters=[SESSION_ID_PARAM, *PARAMETERS, OPTIONAL_IDEMPOTENCY_KEY_HEADER],
        request=None,
        responses={204: None, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
    def delete(self, request: Request, pk: Any) -> Response:
        customer = cast(Customer, request.user)
        try:
            account_sessions.revoke_session(
                customer.pk,
                str(pk),
                actor=customer,
                actor_type="customer",
                request=request,
                current_sid=_sid(request),
            )
        except account_sessions.SessionNotFound as exc:
            raise NotFound() from exc
        return Response(status=204)


class RevokeOtherSessionsView(CustomerAPIView):
    @extend_schema(
        operation_id="customer_sessions_revoke_others",
        description="«Boshqa qurilmalardan chiqish»: every session but the caller's own ends.",
        parameters=[*PARAMETERS, OPTIONAL_IDEMPOTENCY_KEY_HEADER],
        request=None,
        responses={200: RevokedSessionsSerializer, **CUSTOMER_ERRORS},
    )
    @idempotent(required=False)
    def post(self, request: Request) -> Response:
        customer = cast(Customer, request.user)
        count = account_sessions.revoke_other_sessions(
            customer.pk, _sid(request), actor=customer, request=request
        )
        return Response({"revoked": count})
