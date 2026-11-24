"""Pay (customer contract §2.7): start a payment of the order with the chosen provider."""

from __future__ import annotations

from urllib.parse import urlencode

from django.conf import settings
from rest_framework.request import Request
from rest_framework.response import Response

from glowderma.accounts import services as account_services
from glowderma.accounts.api.customer_base import CustomerAPIView
from glowderma.accounts.api.throttles import CustomerRateThrottle
from glowderma.commerce import payment_ref, selectors, services, state, uzum_flow
from glowderma.commerce.api.customer.schemas.payment import PAY_POST
from glowderma.commerce.api.customer.serializers import (
    CustomerPayLinkSerializer,
    CustomerPaySerializer,
)
from glowderma.commerce.api.customer.throttles import PayThrottle
from glowderma.commerce.api.customer.views.common import field_refusal, own_order, signed_in_customer
from glowderma.core import enums, integration_modes, timeutils
from glowderma.core.api.exceptions import (
    AlreadyPaid,
    NotPayable,
    OrderExpired,
    PaymentInProgress,
    ProviderUnavailable,
)
from glowderma.core.idempotency import already_answered, idempotency_key, idempotent
from glowderma.integrations.click.registry import get_click
from glowderma.integrations.payme.registry import get_payme


def _return_url(order_id: int, *, platform: str | None = None, language: str = "uz") -> str:
    """Where the provider page sends the payer back (C§2.7.2): the app's https link ``PAYMENT_RETURN_URL``
    with ``order_id`` appended, or — for a website request (``X-GD-Platform: web``) — the website's own page
    ``PAYMENT_WEB_RETURN_URL``, a template with ``{order_id}``, ``{lang}`` and ``{locale_prefix}`` (``""`` for
    Uzbek, ``/ru``, ``/en``). Both are server-configured, never taken from the client."""
    if platform == enums.OrderChannel.WEB:
        lang = language if language in enums.ContentLanguage.values else enums.ContentLanguage.UZ
        prefix = "" if lang == enums.ContentLanguage.UZ else f"/{lang}"
        return str(settings.PAYMENT_WEB_RETURN_URL).format(order_id=order_id, lang=lang, locale_prefix=prefix)
    base = str(settings.PAYMENT_RETURN_URL)
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urlencode({'order_id': order_id})}"


class PayView(CustomerAPIView):
    throttle_classes = [CustomerRateThrottle, PayThrottle]

    @PAY_POST
    def post(self, request: Request, pk: int) -> Response:
        # Uzum: the hosted form is registered here, before the @idempotent transaction and holding no row lock
        # while Uzum answers (uzum_flow.prepare_form); the handler only hands out the stored form. A replayed
        # key (or one the decorator refuses) asks Uzum nothing.
        if idempotency_key(request) is not None and not already_answered(request):
            self._prepare_uzum(request, pk)
        return self._pay(request, pk)

    def _prepare_uzum(self, request: Request, pk: int) -> None:
        customer = signed_in_customer(request)
        body = CustomerPaySerializer(data=request.data)
        if not body.is_valid() or not customer.phone:
            return  # the handler answers the refusal
        order = selectors.customer_order(pk, customer.pk)
        if order is None:
            return
        if (body.validated_data["provider"] or order.payment_provider) != enums.PaymentProvider.UZUM:
            return
        device = self.device
        uzum_flow.prepare_form(
            order.pk,
            customer=customer,
            language=self.language,
            return_url=_return_url(
                order.pk, platform=device.platform if device is not None else None, language=self.language
            ),
        )

    @idempotent()
    def _pay(self, request: Request, pk: int) -> Response:
        customer = signed_in_customer(request)
        account_services.require_phone(customer)  # hybrid identity, C§2.2.10
        own_order(customer, pk)
        body = CustomerPaySerializer(data=request.data)
        body.is_valid(raise_exception=True)
        order = services.lock_order(pk)  # every state read below runs under the Order lock
        now = timeutils.now()
        if order.status == enums.OrderStatus.CANCELLED:  # also while a refund of its payment runs
            raise NotPayable()
        if order.status in state.PAID_STATUSES or selectors.honoured_paid(order.pk):
            raise AlreadyPaid()
        if not selectors.is_payable(order, None, now, attaching=True):
            raise OrderExpired()
        provider = body.validated_data["provider"] or order.payment_provider
        flying = selectors.inflight_payment(order.pk, now)
        if flying is not None and flying.provider != provider:
            raise PaymentInProgress(
                details={"provider": flying.provider, "order_id": order.pk},
                params={"provider": selectors.provider_brand(flying.provider)},
            )
        if provider not in integration_modes.payable_providers():
            raise field_refusal("provider", "invalid_choice")
        payment = services.ensure_payable_payment(order.pk, provider, at=now)
        reference = payment_ref.build(order.pk)
        device = self.device
        return_url = _return_url(
            order.pk, platform=device.platform if device is not None else None, language=self.language
        )
        scenario = services.fake_scenario(order, provider)
        pay_by = order.reservation_expires_at
        if provider == enums.PaymentProvider.CLICK:
            web_url = get_click(scenario).pay_url(reference, payment.amount, return_url)
        elif provider == enums.PaymentProvider.PAYME:
            web_url = get_payme(scenario).checkout_url(
                reference, payment.amount * 100, self.language, return_url
            )
        elif provider == enums.PaymentProvider.UZUM:
            stored = uzum_flow.stored_link(
                payment, now
            )  # registered by _prepare_uzum, before this transaction
            if stored is None:
                if not uzum_flow.form_window_open(order, now):
                    raise OrderExpired()
                raise ProviderUnavailable()  # Uzum did not register it, or another tap is doing so: retry
            web_url = stored
            session_ends = selectors.uzum_session_ends(payment)
            if session_ends is not None and (pay_by is None or session_ends < pay_by):
                pay_by = session_ends  # the form closes before the reservation does
        else:
            raise ValueError(f"Unknown payment provider {provider!r}")
        link = {
            "order_id": order.pk,
            "payment_id": payment.id,
            "provider": provider,
            "amount": payment.amount,
            "pay_by": pay_by,
            "account_ref": reference,
            "web_url": web_url,
            "app_url": None,
            "return_url": return_url,
        }
        return Response(CustomerPayLinkSerializer(link).data)


__all__ = [
    "PayView",
]
