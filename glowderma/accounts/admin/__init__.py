"""Read-only Django admin for the accounts models.

``Customer`` is registered by ``glowderma.customers.admin``: its change page also shows the customer's
orders, loyalty account and appointments from the upper modules.

Secrets never reach the page: ``StaffUser.password``, ``Customer.password``, ``AuthSession.refresh_hash`` /
``previous_refresh_hash``, ``Device.fcm_token`` / ``key_hash``, ``SmsCode.code_hmac`` /
``verification_token_hash`` and ``CustomerSocialIdentity.provider_refresh_token`` (Apple's sealed refresh
token) are excluded (design 01 §11), and so is the TOTP seed of the ops console's 2FA devices
(``TOTPDevice.key``); recovery codes (``StaticToken``, keyed hashes) are never listed, only how many are left.
List pages show phones and emails masked (so does the customer page, glowderma.customers.admin).

``auth.Group`` is unregistered: staff have one role model (GST-1), groups are never used.

Writes on these pages (each confirmed and audited):

- the manager-only "2FA ni qayta o‘rnatish" action on the staff list
  (``glowderma.accounts.console.access.reset_two_factor``);
- «Sessiyani tugatish» (glowderma.core.admin_actions) signs one open customer session out through
  ``accounts.services.revoke_open_customer_session`` — the same service as
  ``DELETE admin/customers/{id}/sessions/{sid}/`` (reason ``admin_revoked``) — from the session's change page
  or its row on the list.

One module per subject: ``staff``, ``auth_sessions``, ``customer_records`` and ``otp_devices``; importing
this package (Django's admin autodiscovery) registers them all.
"""

from glowderma.accounts.admin import auth_sessions, customer_records, otp_devices, staff

__all__ = ["auth_sessions", "customer_records", "otp_devices", "staff"]
