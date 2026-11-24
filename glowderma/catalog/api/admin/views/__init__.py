"""Admin views of `catalog` (contract §2.3 Products and Billz, §2.4 Categories, §2.5 Banners), one subject per
module: ``products`` (with the stats), ``product_images``, ``categories``, ``banners`` and ``billz_jobs`` (the
Billz import and its jobs); ``forms`` holds the multipart form helpers and ``common`` the per-action parsers
and the plain query dict. Their OpenAPI declarations live in ``glowderma.catalog.api.admin.schemas``.
"""

from glowderma.catalog.api.admin.views.banners import (
    BannerViewSet,
)
from glowderma.catalog.api.admin.views.billz_jobs import (
    BillzImportView,
    JobDetailView,
)
from glowderma.catalog.api.admin.views.categories import (
    CategoryViewSet,
)
from glowderma.catalog.api.admin.views.product_images import (
    ProductImageDetailView,
    ProductImagesOrderView,
    ProductImagesView,
)
from glowderma.catalog.api.admin.views.products import (
    ProductStatsView,
    ProductViewSet,
)

__all__ = [
    "BannerViewSet",
    "BillzImportView",
    "CategoryViewSet",
    "JobDetailView",
    "ProductImageDetailView",
    "ProductImagesOrderView",
    "ProductImagesView",
    "ProductStatsView",
    "ProductViewSet",
]
