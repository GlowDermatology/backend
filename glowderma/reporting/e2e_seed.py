"""Rows for the models the demo seeds leave empty, so the ops console's browser suite (``manage.py e2e_console
seed-coverage``, ``e2e_admin/``) opens a change page of every registered model and sees every secret column
filled. Development data only: reached through the e2e command, which refuses to run outside the e2e settings.
"""

from __future__ import annotations

import hashlib
import io
import secrets
import uuid
from datetime import timedelta
from typing import Any

from django.contrib import admin
from django.contrib.auth.hashers import make_password
from django.core.files.base import ContentFile
from django.db import models, transaction
from django_otp.plugins.otp_static.models import StaticDevice
from PIL import Image

from glowderma.accounts.console import twofactor
from glowderma.accounts.models import Customer, CustomerSocialIdentity, Device, SocialIdentity, StaffUser
from glowderma.core import timeutils
from glowderma.core.models import IdempotencyRecord
from glowderma.core.seeding import placeholder_art

OWNER_EMAIL = "e2e-owner@e2e.uz"


def _owner() -> StaffUser:
    owner = StaffUser.objects.filter(email=OWNER_EMAIL).first()
    if owner is None:
        owner = StaffUser.objects.create(
            email=OWNER_EMAIL, full_name="E2E Owner", is_staff=True, is_manager=True
        )
        owner.set_unusable_password()
        owner.save(update_fields=["password"])
    return owner


def _empty(label: str) -> bool:
    model = next(m for m in admin.site._registry if m._meta.label_lower == label)
    return not model._default_manager.exists()


def fill_empty_models() -> dict[str, Any]:
    from glowderma.catalog.models import Banner, Product
    from glowderma.commerce.models import Cart, CartItem
    from glowderma.loyalty.models import PosDevice
    from glowderma.notifications.models import Campaign, CampaignRecipient
    from glowderma.professionals.models import Professional, WorkingHoursOverride

    created: list[str] = []
    now = timeutils.now()
    with transaction.atomic():
        owner = _owner()
        customer = Customer.objects.filter(phone__isnull=False).order_by("pk").first()
        if _empty("accounts.socialidentity"):
            SocialIdentity.objects.create(
                staff=owner, provider="google", subject=f"e2e-{uuid.uuid4().hex}", email_at_link=OWNER_EMAIL
            )
            created.append("accounts.socialidentity")
        if _empty("catalog.banner"):
            art = placeholder_art("banner")
            width, height = Image.open(io.BytesIO(art)).size
            banner = Banner(title_uz="E2E banner", image_width=width, image_height=height)
            banner.save()  # the upload path is built from the id
            banner.image.save("e2e.webp", ContentFile(art), save=False)
            banner.image_width, banner.image_height = width, height
            banner.save()
            created.append("catalog.banner")
        if _empty("commerce.cart") and customer is not None:
            cart = Cart.objects.create(customer=customer)
            product = Product.objects.order_by("pk").first()
            if product is not None:
                CartItem.objects.create(cart=cart, product=product, quantity=2, price_at_add=product.price)
            created.extend(["commerce.cart", "commerce.cartitem"])
        if _empty("core.idempotencyrecord"):
            IdempotencyRecord.objects.create(
                audience="admin",
                actor_id=owner.pk,
                method="POST",
                path="/api/v1/admin/products/",
                key=uuid.uuid4(),
                request_hash=hashlib.sha256(secrets.token_bytes(16)).hexdigest(),
                response_status=201,
                response_body={"access_token": f"e2e-secret-{secrets.token_hex(12)}"},
                expires_at=now + timedelta(hours=24),
            )
            created.append("core.idempotencyrecord")
        if _empty("loyalty.posdevice"):
            PosDevice.objects.create(
                name="E2E kassa",
                location_label="E2E",
                key_prefix=secrets.token_hex(4),
                key_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
            )
            created.append("loyalty.posdevice")
        if _empty("notifications.campaignrecipient") and customer is not None:
            campaign = Campaign.objects.order_by("pk").first()
            device = Device.objects.filter(customer=customer).first()
            if campaign is not None:
                CampaignRecipient.objects.create(campaign=campaign, customer=customer, device=device)
                created.append("notifications.campaignrecipient")
        if _empty("professionals.workinghoursoverride"):
            professional = Professional.objects.order_by("pk").first()
            if professional is not None:
                WorkingHoursOverride.objects.create(
                    professional=professional, date=(now + timedelta(days=40)).date(), start=None, end=None
                )
                created.append("professionals.workinghoursoverride")
        if _empty("otp_totp.totpdevice"):
            twofactor.enrolment_device(owner, None)
            created.append("otp_totp.totpdevice")
        if not StaticDevice.objects.filter(user=owner).exists():
            twofactor.issue_recovery_codes(owner)
            created.append("otp_static.staticdevice")
        # Secret columns the demo leaves blank: a customer password hash and Apple's sealed refresh token.
        if not Customer.objects.exclude(password="").exists():
            Customer.objects.create(
                phone=f"+99897{secrets.randbelow(10**7):07d}",
                name="E2E parolli mijoz",
                password=make_password(secrets.token_urlsafe(16)),
            )
            created.append("accounts.customer (password)")
        if not CustomerSocialIdentity.objects.exclude(provider_refresh_token="").exists():
            identity = CustomerSocialIdentity.objects.order_by("-pk").first()
            if identity is not None:
                identity.provider_refresh_token = f"sealed-e2e-{secrets.token_urlsafe(24)}"
                identity.save(update_fields=["provider_refresh_token"])
                created.append("accounts.customersocialidentity (refresh token)")
    empty = [m._meta.label_lower for m in admin.site._registry if not _has_rows(m)]
    return {"created": created, "still_empty": empty}


def _has_rows(model: type[models.Model]) -> bool:
    return bool(model._default_manager.exists())
