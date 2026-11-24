"""``me/addresses/*`` of the customer API (customer contract §2.2.7); another customer's address id answers
404."""

from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts.models import Address
from glowderma.core.idempotency import idempotent
from glowderma.customers import selectors, services
from glowderma.customers.api.customer import serializers as s
from glowderma.customers.api.customer.schemas.addresses import (
    ADDRESS_DEFAULT_POST,
    ADDRESS_DETAIL_DELETE,
    ADDRESS_DETAIL_PATCH,
    ADDRESS_LIST_GET,
    ADDRESS_LIST_POST,
)
from glowderma.customers.api.customer.views.common import MeBase


class AddressListView(MeBase):
    @ADDRESS_LIST_GET
    def get(self, request: Request) -> Response:
        rows = selectors.customer_addresses(self.customer.pk)
        return Response({"results": [selectors.address_payload(row) for row in rows]})

    @ADDRESS_LIST_POST
    @idempotent()
    def post(self, request: Request) -> Response:
        serializer = s.AddressWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        address = services.add_address(self.customer.pk, sid=self.sid, **serializer.validated_data)
        return Response(selectors.address_payload(address), status=201)


class AddressDetailView(MeBase):
    """The address row is re-read under the customer-level lock by the service (404 when it is not the
    caller's, or another request removed it meanwhile)."""

    @ADDRESS_DETAIL_PATCH
    @idempotent(required=False)
    def patch(self, request: Request, pk: int) -> Response:
        if not Address.objects.filter(pk=pk, customer_id=self.customer.pk).exists():
            raise NotFound  # 404 before validation for a foreign or unknown id
        serializer = s.AddressWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updated = services.update_address(self.customer.pk, pk, sid=self.sid, **serializer.validated_data)
        return Response(selectors.address_payload(updated))

    @ADDRESS_DETAIL_DELETE
    @idempotent(required=False)
    def delete(self, request: Request, pk: int) -> Response:
        services.remove_address(self.customer.pk, pk, sid=self.sid)
        return Response(status=204)


class AddressDefaultView(MeBase):
    @ADDRESS_DEFAULT_POST
    @idempotent(required=False)
    def post(self, request: Request, pk: int) -> Response:
        address = services.set_default_address(self.customer.pk, pk, sid=self.sid)
        return Response(selectors.address_payload(address))


__all__ = [
    "AddressDefaultView",
    "AddressDetailView",
    "AddressListView",
]
