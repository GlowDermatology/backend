"""The views of the staff admin API (``auth/`` and ``staff/``, admin contract §2.1, §2.16), one subject per
module: ``auth_sessions`` (login, refresh, logout), ``profile`` (``me/``), ``passwords``, ``two_factor``
(the panel MFA), ``social`` (OAuth sign-in and linking) and ``staff_members``; ``schema`` holds the shared
OpenAPI declarations. The response bodies are built by ``glowderma.accounts.api.admin.payloads``."""

from glowderma.accounts.api.admin.views.auth_sessions import (
    LoginView,
    LogoutView,
    RefreshView,
)
from glowderma.accounts.api.admin.views.passwords import (
    ChangePasswordView,
    OwnPasswordLinkView,
    PasswordTokenCheckView,
    PasswordTokenSetView,
)
from glowderma.accounts.api.admin.views.profile import (
    MeView,
)
from glowderma.accounts.api.admin.views.schema import (
    ERRORS,
    LOGIN_RESULT,
    PROVIDER_PARAM,
)
from glowderma.accounts.api.admin.views.social import (
    VERIFIER_TTL_SECONDS,
    OAuthLinkView,
    OAuthProviderView,
    claim_code_verifier,
)
from glowderma.accounts.api.admin.views.staff_members import (
    STAFF_ID_DESCRIPTION,
    StaffViewSet,
)
from glowderma.accounts.api.admin.views.two_factor import (
    MfaConfirmView,
    MfaDisableView,
    MfaRecoveryCodesView,
    MfaSetupView,
    MfaStatusView,
    MfaVerifyView,
)

__all__ = [
    "ERRORS",
    "LOGIN_RESULT",
    "PROVIDER_PARAM",
    "STAFF_ID_DESCRIPTION",
    "VERIFIER_TTL_SECONDS",
    "ChangePasswordView",
    "LoginView",
    "LogoutView",
    "MeView",
    "MfaConfirmView",
    "MfaDisableView",
    "MfaRecoveryCodesView",
    "MfaSetupView",
    "MfaStatusView",
    "MfaVerifyView",
    "OAuthLinkView",
    "OAuthProviderView",
    "OwnPasswordLinkView",
    "PasswordTokenCheckView",
    "PasswordTokenSetView",
    "RefreshView",
    "StaffViewSet",
    "claim_code_verifier",
]
