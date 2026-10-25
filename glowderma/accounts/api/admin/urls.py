from django.urls import path

from .views import (
    ChangePasswordView,
    LoginView,
    LogoutView,
    MeView,
    OAuthLinkView,
    OAuthProviderView,
    RefreshView,
)

urlpatterns = [
    path("login/", LoginView.as_view(), name="admin-auth-login"),
    path("refresh/", RefreshView.as_view(), name="admin-auth-refresh"),
    path("logout/", LogoutView.as_view(), name="admin-auth-logout"),
    path("me/", MeView.as_view(), name="admin-auth-me"),
    path("change-password/", ChangePasswordView.as_view(), name="admin-auth-change-password"),
    path("oauth/<str:provider>/", OAuthProviderView.as_view(), name="admin-auth-oauth"),
    path("oauth/<str:provider>/link/", OAuthLinkView.as_view(), name="admin-auth-oauth-link"),
]
