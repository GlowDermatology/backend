from django.urls import path

from .views import (
    ChangePasswordView,
    LoginView,
    LogoutView,
    MeView,
    MfaConfirmView,
    MfaDisableView,
    MfaRecoveryCodesView,
    MfaSetupView,
    MfaStatusView,
    MfaVerifyView,
    OAuthLinkView,
    OAuthProviderView,
    OwnPasswordLinkView,
    PasswordTokenCheckView,
    PasswordTokenSetView,
    RefreshView,
)

urlpatterns = [
    path("login/", LoginView.as_view(), name="admin-auth-login"),
    path("refresh/", RefreshView.as_view(), name="admin-auth-refresh"),
    path("logout/", LogoutView.as_view(), name="admin-auth-logout"),
    path("me/", MeView.as_view(), name="admin-auth-me"),
    path("change-password/", ChangePasswordView.as_view(), name="admin-auth-change-password"),
    path("password-link/", OwnPasswordLinkView.as_view(), name="admin-auth-password-link"),
    path("password-token/check/", PasswordTokenCheckView.as_view(), name="admin-auth-password-token-check"),
    path("password-token/set/", PasswordTokenSetView.as_view(), name="admin-auth-password-token-set"),
    path("mfa/", MfaStatusView.as_view(), name="admin-auth-mfa"),
    path("mfa/setup/", MfaSetupView.as_view(), name="admin-auth-mfa-setup"),
    path("mfa/confirm/", MfaConfirmView.as_view(), name="admin-auth-mfa-confirm"),
    path("mfa/disable/", MfaDisableView.as_view(), name="admin-auth-mfa-disable"),
    path("mfa/recovery-codes/", MfaRecoveryCodesView.as_view(), name="admin-auth-mfa-recovery-codes"),
    path("mfa/verify/", MfaVerifyView.as_view(), name="admin-auth-mfa-verify"),
    path("oauth/<str:provider>/", OAuthProviderView.as_view(), name="admin-auth-oauth"),
    path("oauth/<str:provider>/link/", OAuthLinkView.as_view(), name="admin-auth-oauth-link"),
]
