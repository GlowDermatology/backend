"""Customer lifecycle (customer contract §2.2.7, §6.16): deleting an account, reactivating it and the
scrubs of a dormant or deleted profile.
"""

from __future__ import annotations

from django.db import transaction

from glowderma.accounts.models import Address, AuthSession, Customer, CustomerSocialIdentity, Device
from glowderma.accounts.services.customer import auth_sessions, identities, locks, profile
from glowderma.core import enums, timeutils
from glowderma.core.models import IdempotencyRecord


@transaction.atomic
def delete_customer(customer: Customer | int) -> Customer:
    """Soft-delete a customer (decision A1, customer contract §2.2.3 step 5): scrub, revoke, deactivate; keep
    the row.

    Under the customer-level lock (``FOR NO KEY UPDATE``, so a concurrent capture's ``Purchase`` insert does
    not wait): ``status=deleted``; name, image, address text, password and e-mail address (hybrid identity)
    scrubbed; ``language`` reset to ``uz``;
    consent (``terms_version``, ``consent_at``, ``consent_ip``) and ``last_login_at`` cleared (the audit log
    keeps the consent history); addresses and social identities deleted (one ``apple.revoke`` outbox message
    per Apple identity holding a token); every device inactive with no push, token or key hash; every session
    revoked ``deactivated`` (tombstones and the QR keys' deletion on commit); the customer's stored
    Idempotency-Key responses deleted (they hold Me and Address payloads); every session's user agent, IP and
    location blanked (B-W1). ``LoyaltyAccount``,
    ``Purchase``, ``Order`` and ``Appointment`` rows keep pointing at it (``PROTECT``). Idempotent: an already
    deleted customer is returned unchanged.
    """
    customer_id = customer if isinstance(customer, int) else customer.pk
    stored = locks.lock_customer(customer_id)
    if stored.status == enums.CustomerStatus.DELETED:
        return _refreshed(customer, stored)
    instant = timeutils.now()
    profile.delete_image_on_commit(stored)
    stored.status = enums.CustomerStatus.DELETED
    stored.deleted_at = instant
    stored.name = ""
    stored.address_text = ""
    stored.image = ""
    stored.password = ""
    stored.email = None  # hybrid identity: the address goes with the scrub (the phone stays, A1)
    stored.email_verified_at = None
    stored.language = enums.ContentLanguage.UZ
    stored.terms_version = ""
    stored.consent_at = None
    stored.consent_ip = None
    stored.last_login_at = None
    stored.save(
        update_fields=[
            "status",
            "deleted_at",
            "name",
            "address_text",
            "image",
            "password",
            "email",
            "email_verified_at",
            "language",
            "terms_version",
            "consent_at",
            "consent_ip",
            "last_login_at",
            "updated_at",
        ]
    )
    Address.objects.filter(customer_id=stored.pk).delete()
    for identity in CustomerSocialIdentity.objects.filter(customer_id=stored.pk):
        identities.enqueue_apple_revoke(identity)
    CustomerSocialIdentity.objects.filter(customer_id=stored.pk).delete()
    Device.objects.filter(customer_id=stored.pk).update(
        is_active=False, push_enabled=False, fcm_token="", key_hash="", updated_at=instant
    )
    auth_sessions.revoke_customer_sessions(stored.pk, enums.SessionRevokeReason.DEACTIVATED)
    # B-W1: the session list's client facts are personal data; the rows themselves go with the 90-day
    # retention of accounts.cleanup_auth
    AuthSession.objects.filter(customer_id=stored.pk).update(user_agent="", last_ip=None, location="")
    # The stored responses of the customer's Idempotency-Key requests (Me and Address payloads: phone, name,
    # address text, coordinates) go with the scrub instead of outliving it by up to 24 h.
    IdempotencyRecord.objects.filter(audience="customer", actor_id=stored.pk).delete()
    return _refreshed(customer, stored)


def _refreshed(customer: Customer | int, stored: Customer) -> Customer:
    if isinstance(customer, int):
        return stored
    customer.refresh_from_db()
    return customer


@transaction.atomic
def reactivate_customer(customer: Customer, *, name: str = "") -> Customer:
    """Re-registration with the same phone: ``active`` again, ``registered_at`` kept (first registration)."""
    stored = locks.lock_customer(customer.pk)
    if stored.status == enums.CustomerStatus.ACTIVE:
        customer.refresh_from_db()
        return customer
    stored.status = enums.CustomerStatus.ACTIVE
    stored.deleted_at = None
    stored.reactivated_at = timeutils.now()
    if name:
        stored.name = name
    stored.save(update_fields=["status", "deleted_at", "reactivated_at", "name", "updated_at"])
    customer.refresh_from_db()
    return customer


def scrub_profile_for_dormant(customer_id: int, *, language: str) -> None:
    """The profile part of the dormant reset (customer contract §2.2.3, M2-A16): name, image (file deleted on
    commit), address text and addresses gone, the e-mail address too (hybrid identity: the previous holder of
    the number keeps no way in), language = the request language, consent cleared; the user agent, IP and
    location of the previous holder's sessions blanked as at deletion (B-W1 session facts; the callers run it
    before the new holder's session starts)."""
    stored = Customer.objects.get(pk=customer_id)
    profile.delete_image_on_commit(stored)
    Customer.objects.filter(pk=customer_id).update(
        name="",
        image="",
        address_text="",
        email=None,
        email_verified_at=None,
        language=language,
        terms_version="",
        consent_at=None,
        consent_ip=None,
        updated_at=timeutils.now(),
    )
    Address.objects.filter(customer_id=customer_id).delete()
    AuthSession.objects.filter(customer_id=customer_id).update(user_agent="", last_ip=None, location="")


def scrub_deleted_for_reactivation(customer_id: int) -> None:
    """Backstop before a deleted row is reactivated (registration, social completion): whatever a write that
    raced ``DELETE me/`` left on it — password, image, e-mail, addresses, social identities (Apple revoked) —
    never
    reaches the next holder of the number (customer contract §6.17)."""
    stored = Customer.objects.get(pk=customer_id)
    if stored.status != enums.CustomerStatus.DELETED:
        return
    profile.delete_image_on_commit(stored)
    Customer.objects.filter(pk=customer_id).update(
        password="", image="", address_text="", email=None, email_verified_at=None, updated_at=timeutils.now()
    )
    Address.objects.filter(customer_id=customer_id).delete()
    for identity in list(CustomerSocialIdentity.objects.filter(customer_id=customer_id)):
        identities.unlink_customer_identity(identity)


__all__ = [
    "delete_customer",
    "reactivate_customer",
    "scrub_profile_for_dormant",
    "scrub_deleted_for_reactivation",
]
