"""Customer routes of `customers` (customer contract §2.0 rows 1–22); mounted by ``config.urls`` at
``api/v1/``."""

from django.urls import URLPattern, URLResolver, path

from glowderma.customers.api.customer import views

urlpatterns: list[URLPattern | URLResolver] = [
    path("auth/send-code/", views.SendCodeView.as_view()),
    path("auth/verify-code/", views.VerifyCodeView.as_view()),
    path("auth/set-password/", views.SetPasswordView.as_view()),
    path("auth/login/", views.LoginView.as_view()),
    path("auth/reset-password/", views.ResetPasswordView.as_view()),
    path("auth/refresh/", views.RefreshView.as_view()),
    path("auth/logout/", views.LogoutView.as_view()),
    path("auth/social/nonce/", views.SocialNonceView.as_view()),
    path("auth/social/", views.SocialView.as_view()),
    path("auth/social/complete/", views.SocialCompleteView.as_view()),
    path("me/", views.MeView.as_view()),
    path("me/consent/", views.ConsentView.as_view()),
    path("me/image/", views.ImageView.as_view()),
    path("me/device/", views.DeviceView.as_view()),
    path("me/password/", views.PasswordView.as_view()),
    path("me/phone/code/", views.PhoneCodeView.as_view()),
    path("me/phone/", views.PhoneView.as_view()),
    path("me/email/code/", views.EmailCodeView.as_view()),
    path("me/email/", views.EmailView.as_view()),
    path("me/social/", views.SocialLinkView.as_view()),
    path("me/social/<str:provider>/", views.SocialUnlinkView.as_view()),
    path("me/sessions/", views.SessionListViewSet.as_view({"get": "list"})),
    path("me/sessions/revoke-others/", views.RevokeOtherSessionsView.as_view()),
    path("me/sessions/<uuid:pk>/", views.SessionDetailView.as_view()),
    path("me/addresses/", views.AddressListView.as_view()),
    path("me/addresses/<int:pk>/", views.AddressDetailView.as_view()),
    path("me/addresses/<int:pk>/default/", views.AddressDefaultView.as_view()),
    path("home/", views.HomeView.as_view()),
    path("catalog/banners/", views.BannersView.as_view()),
    path("catalog/categories/", views.CategoriesView.as_view()),
    path("catalog/products/", views.ProductListView.as_view()),
    path("catalog/feed/", views.ProductFeedView.as_view()),
    path("catalog/products/<int:pk>/", views.ProductDetailView.as_view()),
    path("catalog/products/<slug:slug>/", views.ProductBySlugView.as_view()),
]
