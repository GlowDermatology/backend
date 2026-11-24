"""The views of the customer API's ``customers`` routes (customer contract §2.1–§2.3), one subject per module:
``auth`` (OTP, registration, login, reset, refresh, logout), ``social`` (social sign-in), ``me`` (profile,
deletion, consent, image, device, password), ``me_social`` (linked social identities), ``contacts`` (phone and
e-mail), ``addresses``, ``sessions`` and ``catalog``; ``common`` holds what several of them share. Their
OpenAPI declarations live in ``glowderma.customers.api.customer.schemas``; the response bodies are built by
``glowderma.customers.api.customer.payloads``.

``Idempotency-Key`` (C§1.7): required on ``POST me/password/``, ``POST me/social/`` and ``POST
me/addresses/``, optional and honoured when sent on every other ``me/`` write. A refusal rolls the key's
transaction back; what a refusal keeps on purpose lives outside it: the re-authentication guard counters
(cache) and the revocation of an Apple token a refused link exchanged
(:func:`customers.services.revoke_refused_apple_tokens`).
"""

from glowderma.customers.api.customer.views.addresses import (
    AddressDefaultView,
    AddressDetailView,
    AddressListView,
)
from glowderma.customers.api.customer.views.auth import (
    LoginView,
    LogoutView,
    RefreshView,
    ResetPasswordView,
    SendCodeView,
    SetPasswordView,
    VerifyCodeView,
)
from glowderma.customers.api.customer.views.catalog import (
    BannersView,
    CategoriesView,
    ProductBySlugView,
    ProductDetailView,
    ProductFeedView,
    ProductListView,
)
from glowderma.customers.api.customer.views.contacts import (
    EmailCodeView,
    EmailView,
    PhoneCodeView,
    PhoneView,
)
from glowderma.customers.api.customer.views.home import (
    HomeView,
)
from glowderma.customers.api.customer.views.me import (
    ConsentView,
    DeviceView,
    ImageView,
    MeView,
    PasswordView,
)
from glowderma.customers.api.customer.views.me_social import (
    SocialLinkView,
    SocialUnlinkView,
)
from glowderma.customers.api.customer.views.sessions import (
    RevokeOtherSessionsView,
    SessionDetailView,
    SessionListViewSet,
)
from glowderma.customers.api.customer.views.social import (
    SocialCompleteView,
    SocialNonceView,
    SocialView,
)

__all__ = [
    "AddressDefaultView",
    "AddressDetailView",
    "AddressListView",
    "BannersView",
    "CategoriesView",
    "ConsentView",
    "DeviceView",
    "EmailCodeView",
    "EmailView",
    "HomeView",
    "ImageView",
    "LoginView",
    "LogoutView",
    "MeView",
    "PasswordView",
    "PhoneCodeView",
    "PhoneView",
    "ProductBySlugView",
    "ProductDetailView",
    "ProductFeedView",
    "ProductListView",
    "RefreshView",
    "ResetPasswordView",
    "RevokeOtherSessionsView",
    "SendCodeView",
    "SessionDetailView",
    "SessionListViewSet",
    "SetPasswordView",
    "SocialCompleteView",
    "SocialLinkView",
    "SocialNonceView",
    "SocialUnlinkView",
    "SocialView",
    "VerifyCodeView",
]
