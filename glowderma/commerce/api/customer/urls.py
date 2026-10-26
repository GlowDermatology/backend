"""Customer routes of `commerce` (customer contract §2.0 rows 24–35); mounted by ``config.urls`` at
``api/v1/``.

``delivery/calculate/`` is served here (it reads the cart, loyalty and ``cashback_max``);
``delivery/regions/`` is ``delivery``'s."""

from django.urls import URLPattern, URLResolver, path

from glowderma.commerce.api.customer import views

urlpatterns: list[URLPattern | URLResolver] = [
    path("cart/", views.CartView.as_view()),
    path("cart/items/", views.CartItemsView.as_view()),
    path("cart/items/<int:product_id>/", views.CartItemView.as_view()),
    path("delivery/calculate/", views.DeliveryCalculateView.as_view()),
    path("orders/checkout/", views.CheckoutView.as_view()),
    path("orders/<int:pk>/pay/", views.PayView.as_view()),
    path("orders/<int:pk>/cancel/", views.CancelView.as_view()),
    path("orders/<int:pk>/reorder/", views.ReorderView.as_view()),
    path("me/orders/", views.MyOrdersViewSet.as_view({"get": "list"})),
    path("me/orders/<int:pk>/", views.MyOrdersViewSet.as_view({"get": "retrieve"})),
]
