from django.urls import path

from .views import ChangePasswordView, LoginView, LogoutView, MeView, RefreshView

urlpatterns = [
    path("login/", LoginView.as_view(), name="admin-auth-login"),
    path("refresh/", RefreshView.as_view(), name="admin-auth-refresh"),
    path("logout/", LogoutView.as_view(), name="admin-auth-logout"),
    path("me/", MeView.as_view(), name="admin-auth-me"),
    path("change-password/", ChangePasswordView.as_view(), name="admin-auth-change-password"),
]
