"""Transactional writes of `customers`, the top-layer orchestrator of the customer account.

Admin (admin contract §2.6): the manual cashback adjustment, a thin orchestration over
``loyalty.services.adjust``, which runs the contract checks in order (``amount`` 1..10 000 000, ``reason``
3..300 trimmed, then ``subtract`` > ``available`` under the ``LoyaltyAccount`` lock), posts the ``adjustment``
entry through ``post_entry`` and audits ``cashback.adjusted`` ``{direction, amount, reason, balance_after,
ledger_entry_id}`` with the staff actor in the same transaction — so this module adds no second audit row.
Deleted customers are allowed: the balance is kept for a later re-registration (decision A1).

Customer API (customer contract §2.1, §2.2; M2 plan §2 P1): the auth flows — ``register``, ``login`` (known
and new device), ``reset_password`` with the dormant-account rule (``dormant_reset``), ``social_sign_in``,
``complete_social`` and the Apple token exchange and revocation of refused flows — and the signed-in
writes of ``me/*``: ``update_profile``, ``accept_consent``, ``set_image`` / ``delete_image``, the addresses,
``link_social`` / ``unlink_social``, ``change_password`` and ``delete_account``. They take ids and scalars,
write other modules only through their services (``accounts``, ``commerce``, ``appointments``) and take the
customer-level lock (``FOR NO KEY UPDATE``) before any other row (C§2.7.1); a signed-in write re-checks the
customer and the request's session under it (``accounts.services.lock_signed_in_customer``, C§6.17).

The package groups them by subject: ``cashback`` (the admin adjustment), ``auth`` (registration, login,
reset and the dormant-account rule), ``social`` (the token checks, sign-in, the social ticket, its completion
and the Apple token revocation), ``me`` (re-authentication, social links, password, contacts, profile,
addresses and deletion) and ``common`` (identifiers, results, passwords, terms, identities and the audit
trail). Callers import this facade.
"""

from glowderma.customers.services.auth.dormancy import (
    dormant_reset,
)
from glowderma.customers.services.auth.login import (
    login,
)
from glowderma.customers.services.auth.recovery import (
    reset_password,
)
from glowderma.customers.services.auth.registration import (
    register,
)
from glowderma.customers.services.cashback import (
    AdjustmentOutcome,
    CustomerNotFound,
    adjust_cashback,
)
from glowderma.customers.services.common.identifiers import (
    LOGIN_DEVICE_SCOPE,
    LOGIN_SCOPE,
    REAUTH_SCOPE,
    customer_guard_key,
    customer_identifier,
    is_email,
    phone_guard_key,
    target_kwargs,
)
from glowderma.customers.services.common.passwords import (
    PASSWORD_CODE_KEYS,
    check_login_password,
    validate_customer_password,
)
from glowderma.customers.services.common.results import (
    DeviceContext,
    DeviceStep,
    PhoneStep,
    SignIn,
)
from glowderma.customers.services.me.addresses import (
    add_address,
    remove_address,
    set_default_address,
    update_address,
)
from glowderma.customers.services.me.contacts import (
    attach_email,
    attach_phone,
    password_signs_in,
    send_attach_phone_code,
    send_verify_email_code,
)
from glowderma.customers.services.me.deletion import (
    OPEN_APPOINTMENT_STATUSES,
    DeletionResult,
    delete_account,
)
from glowderma.customers.services.me.password import (
    change_password,
)
from glowderma.customers.services.me.profile import (
    accept_consent,
    delete_image,
    set_image,
    update_profile,
)
from glowderma.customers.services.me.reauth import (
    reauthenticate,
    session_is_fresh,
)
from glowderma.customers.services.me.social_links import (
    link_social,
    unlink_social,
)
from glowderma.customers.services.social.apple_revocation import (
    RefusedAppleToken,
    revoke_refused_apple_tokens,
)
from glowderma.customers.services.social.completion import (
    complete_social,
)
from glowderma.customers.services.social.sign_in import (
    social_sign_in,
)
from glowderma.customers.services.social.tickets import (
    SOCIAL_APPLE_KEY,
    SOCIAL_TICKET_SALT,
    SOCIAL_TICKET_USED_KEY,
    SocialTicket,
    read_social_ticket,
)
from glowderma.customers.services.social.tokens import (
    DEFAULT_TOKEN_TYPES,
    NONCE_WILDCARD,
    PROVIDER_LABELS,
    TOKEN_PAIRS,
    verify_social_token,
)

__all__ = [
    "DEFAULT_TOKEN_TYPES",
    "LOGIN_DEVICE_SCOPE",
    "LOGIN_SCOPE",
    "NONCE_WILDCARD",
    "OPEN_APPOINTMENT_STATUSES",
    "PASSWORD_CODE_KEYS",
    "PROVIDER_LABELS",
    "REAUTH_SCOPE",
    "SOCIAL_APPLE_KEY",
    "SOCIAL_TICKET_SALT",
    "SOCIAL_TICKET_USED_KEY",
    "TOKEN_PAIRS",
    "AdjustmentOutcome",
    "CustomerNotFound",
    "DeletionResult",
    "DeviceContext",
    "DeviceStep",
    "PhoneStep",
    "RefusedAppleToken",
    "SignIn",
    "SocialTicket",
    "accept_consent",
    "add_address",
    "adjust_cashback",
    "attach_email",
    "attach_phone",
    "change_password",
    "check_login_password",
    "complete_social",
    "customer_guard_key",
    "customer_identifier",
    "delete_account",
    "delete_image",
    "dormant_reset",
    "is_email",
    "link_social",
    "login",
    "password_signs_in",
    "phone_guard_key",
    "read_social_ticket",
    "reauthenticate",
    "register",
    "remove_address",
    "reset_password",
    "revoke_refused_apple_tokens",
    "send_attach_phone_code",
    "send_verify_email_code",
    "session_is_fresh",
    "set_default_address",
    "set_image",
    "social_sign_in",
    "target_kwargs",
    "unlink_social",
    "update_address",
    "update_profile",
    "validate_customer_password",
    "verify_social_token",
]
