"""``POST pos/cashback/resolve-qr/`` (customer contract §2.10): a cashier scans the customer's personal QR.

The token is consumed (``GETDEL``) by the first scan; every failure answers the same 409 ``qr_invalid`` (no
oracle). The response names neither the customer nor our id: an opaque per-device ``customer_ref``, the phone
(what Billz needs) and its mask, the available cashback and the tier percent. Every resolve is audited
(``pos.qr_resolved`` / ``pos.qr_rejected``, actor type ``integration``).
"""

from __future__ import annotations

from typing import Any, cast

from drf_spectacular.utils import extend_schema
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import selectors as account_selectors
from glowderma.accounts.models import Customer
from glowderma.core import audit, enums, timeutils
from glowderma.core.api.exceptions import QrInvalid
from glowderma.core.api.schema import ACCEPT_LANGUAGE_HEADER, CustomerErrorEnvelopeSerializer
from glowderma.core.pii import mask_phone
from glowderma.loyalty import qr, selectors
from glowderma.loyalty.api.customer.serializers import ResolvedQrSerializer, ResolveQrSerializer
from glowderma.loyalty.api.pos.base import PosAPIView
from glowderma.loyalty.models import PosDevice

POS_ERRORS = dict.fromkeys((400, 401, 409, 429), CustomerErrorEnvelopeSerializer)


def _reject(device: PosDevice, reason: str, request: Request) -> QrInvalid:
    audit.record(
        action="pos.qr_rejected",
        entity_type="pos_device",
        entity_id=str(device.pk),
        actor=device,
        request=request,
        after={"pos_device_id": device.pk, "reason": reason},
    )
    return QrInvalid()


class ResolveQrView(PosAPIView):
    @extend_schema(
        operation_id="pos_cashback_resolve_qr",
        description=(
            "Resolve a scanned personal QR token (single use). Every failure answers 409 `qr_invalid` with "
            "the same body. `customer_ref` is opaque and stable per (POS device, customer)."
        ),
        parameters=[ACCEPT_LANGUAGE_HEADER],
        request=ResolveQrSerializer,
        responses={200: ResolvedQrSerializer, **POS_ERRORS},
    )
    def post(self, request: Request) -> Response:
        device = cast(PosDevice, request.user)
        body = ResolveQrSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data: dict[str, Any] = dict(body.validated_data)
        token = str(data["token"])
        if not qr.is_well_formed(token):
            raise _reject(device, "format", request)
        resolved = qr.resolve_qr(token)
        customer = (
            Customer.objects.filter(pk=resolved.customer_id, status=enums.CustomerStatus.ACTIVE).first()
            if resolved is not None
            and (not resolved.session_id or account_selectors.customer_session_open(resolved.session_id))
            else None
        )
        if customer is None:
            raise _reject(device, "unknown", request)
        available = selectors.account_summary(customer.pk).available
        tier = selectors.tier_for(selectors.customer_totals(customer.pk).lifetime)
        audit.record(
            action="pos.qr_resolved",
            entity_type="pos_device",
            entity_id=str(device.pk),
            actor=device,
            request=request,
            after={"pos_device_id": device.pk, "customer_id": customer.pk, "sale_ref": data.get("sale_ref")},
        )
        payload = {
            "customer_ref": qr.pos_customer_ref(device.pk, customer.pk),
            "phone": customer.phone,
            "phone_mask": mask_phone(customer.phone),
            "available": available,
            "percent": tier.percent if tier is not None else None,
            "resolved_at": timeutils.now(),
        }
        return Response(ResolvedQrSerializer(payload).data)
