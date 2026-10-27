"""Admin routes of ``catalog`` (mounted under ``api/v1/admin/`` by ``config.urls``).

Every route name is prefixed ``admin-`` and unique across modules; the auth matrix test walks them all.
``categories/order/`` and ``banners/order/`` are declared before the detail routes so the reserved segment
is never read as a slug / id (the category view also refuses ``order`` as a slug). The Billz import and
``jobs/`` routes of contract §2.3 / §2.14 are declared here too, because their views need the admin view base
from ``accounts``, which ``integrations`` may not import (layer contract).
"""

from django.urls import URLPattern, URLResolver, path

from .views import (
    BannerViewSet,
    BillzImportView,
    CategoryViewSet,
    JobDetailView,
    ProductImageDetailView,
    ProductImagesOrderView,
    ProductImagesView,
    ProductStatsView,
    ProductViewSet,
)

products = ProductViewSet.as_view({"get": "list", "post": "create"})
product = ProductViewSet.as_view({"get": "retrieve", "patch": "partial_update"})
categories = CategoryViewSet.as_view({"get": "list", "post": "create"})
categories_order = CategoryViewSet.as_view({"put": "reorder"})
category = CategoryViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"})
banners = BannerViewSet.as_view({"get": "list", "post": "create"})
banners_order = BannerViewSet.as_view({"put": "reorder"})
banner = BannerViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"})

urlpatterns: list[URLPattern | URLResolver] = [
    path("products/", products, name="admin-products-list"),
    path("products/stats/", ProductStatsView.as_view(), name="admin-products-stats"),
    path("products/<int:pk>/", product, name="admin-products-detail"),
    path("products/<int:pk>/images/", ProductImagesView.as_view(), name="admin-product-images"),
    path(
        "products/<int:pk>/images/order/", ProductImagesOrderView.as_view(), name="admin-product-images-order"
    ),
    path(
        "products/<int:pk>/images/<int:image_id>/",
        ProductImageDetailView.as_view(),
        name="admin-product-image-detail",
    ),
    path("categories/", categories, name="admin-categories-list"),
    path("categories/order/", categories_order, name="admin-categories-order"),
    path("categories/<slug:slug>/", category, name="admin-categories-detail"),
    path("banners/", banners, name="admin-banners-list"),
    path("banners/order/", banners_order, name="admin-banners-order"),
    path("banners/<int:pk>/", banner, name="admin-banners-detail"),
    # Billz import and job polling (the views sit in catalog: see views.py "Billz import and jobs").
    path("integrations/billz/import/", BillzImportView.as_view(), name="admin-billz-import"),
    path("jobs/<int:pk>/", JobDetailView.as_view(), name="admin-jobs-detail"),
]
