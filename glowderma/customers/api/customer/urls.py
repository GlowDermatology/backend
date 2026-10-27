"""Customer routes of `customers` (customer contract §2.0 rows 1–22); mounted by ``config.urls`` at
``api/v1/``."""

from django.urls import URLPattern, URLResolver, path

from glowderma.customers.api.customer import auth_views, catalog_views, me_views

urlpatterns: list[URLPattern | URLResolver] = [
    path("auth/send-code/", auth_views.SendCodeView.as_view()),
    path("auth/verify-code/", auth_views.VerifyCodeView.as_view()),
    path("auth/set-password/", auth_views.SetPasswordView.as_view()),
    path("auth/login/", auth_views.LoginView.as_view()),
    path("auth/reset-password/", auth_views.ResetPasswordView.as_view()),
    path("auth/refresh/", auth_views.RefreshView.as_view()),
    path("auth/logout/", auth_views.LogoutView.as_view()),
    path("auth/social/nonce/", auth_views.SocialNonceView.as_view()),
    path("auth/social/", auth_views.SocialView.as_view()),
    path("auth/social/complete/", auth_views.SocialCompleteView.as_view()),
    path("me/", me_views.MeView.as_view()),
    path("me/consent/", me_views.ConsentView.as_view()),
    path("me/image/", me_views.ImageView.as_view()),
    path("me/device/", me_views.DeviceView.as_view()),
    path("me/password/", me_views.PasswordView.as_view()),
    path("me/phone/code/", me_views.PhoneCodeView.as_view()),
    path("me/phone/", me_views.PhoneView.as_view()),
    path("me/email/code/", me_views.EmailCodeView.as_view()),
    path("me/email/", me_views.EmailView.as_view()),
    path("me/social/", me_views.SocialLinkView.as_view()),
    path("me/social/<str:provider>/", me_views.SocialUnlinkView.as_view()),
    path("me/addresses/", me_views.AddressListView.as_view()),
    path("me/addresses/<int:pk>/", me_views.AddressDetailView.as_view()),
    path("me/addresses/<int:pk>/default/", me_views.AddressDefaultView.as_view()),
    path("catalog/banners/", catalog_views.BannersView.as_view()),
    path("catalog/categories/", catalog_views.CategoriesView.as_view()),
    path("catalog/products/", catalog_views.ProductListView.as_view()),
    path("catalog/products/<int:pk>/", catalog_views.ProductDetailView.as_view()),
]
