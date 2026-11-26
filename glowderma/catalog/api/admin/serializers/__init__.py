"""Admin serializers of `catalog` (contract §2.3 Products, §2.4 Categories, §2.5 Banners), one subject per
module: ``products``, ``product_texts``, ``product_images``, ``seo_texts``, ``categories`` and ``banners``;
``fields`` holds the shared rules and the translated name and title shapes.

Write serializers validate shapes and resolve references (category slugs, Billz ids, banner link targets)
and hand a ``services.ProductInput`` / ``services.BannerInput`` to the services. Read serializers render the
contract shapes from the selectors' annotated querysets (``selectors.product_queryset`` and friends).
"""

from glowderma.catalog.api.admin.serializers.banners import (
    BannerLinkSerializer,
    BannerListQuerySerializer,
    BannerPatchSerializer,
    BannerResultsSerializer,
    BannerSerializer,
    BannerTitlePatchSerializer,
    BannerTitleSerializer,
    BannerWriteSerializer,
)
from glowderma.catalog.api.admin.serializers.categories import (
    CategoryListQuerySerializer,
    CategoryNameSerializer,
    CategoryPatchSerializer,
    CategoryResultsSerializer,
    CategorySerializer,
    CategoryWriteSerializer,
)
from glowderma.catalog.api.admin.serializers.fields import (
    BILLZ_ID_PATTERN,
    UZ_NAME_MIN_LENGTH,
    IdOrderSerializer,
    SlugOrderSerializer,
    StrictBooleanField,
)
from glowderma.catalog.api.admin.serializers.product_images import (
    ImageAltSerializer,
    ImageOrderSerializer,
    ImagePatchSerializer,
    ImageRenditionsPutSerializer,
    ImagesResponseSerializer,
    ProductImageSerializer,
    image_alt,
)
from glowderma.catalog.api.admin.serializers.product_texts import (
    TEXT_LIMITS,
    ProductTextSerializer,
    ProductTextsPatchSerializer,
    ProductTextsSerializer,
    ProductUzTextPatchSerializer,
    ProductUzTextSerializer,
    product_texts,
)
from glowderma.catalog.api.admin.serializers.products import (
    BillzStatusSerializer,
    LastChangeSerializer,
    ProductListQuerySerializer,
    ProductPatchSerializer,
    ProductSerializer,
    ProductStatsSerializer,
    ProductWriteSerializer,
    validate_product_slug,
)
from glowderma.catalog.api.admin.serializers.seo_texts import (
    SeoIssueSerializer,
    SeoStatusSerializer,
    SeoTextSerializer,
    SeoTextsSerializer,
    seo_texts,
)

__all__ = [
    "BILLZ_ID_PATTERN",
    "TEXT_LIMITS",
    "UZ_NAME_MIN_LENGTH",
    "BannerLinkSerializer",
    "BannerListQuerySerializer",
    "BannerPatchSerializer",
    "BannerResultsSerializer",
    "BannerSerializer",
    "BannerTitlePatchSerializer",
    "BannerTitleSerializer",
    "BannerWriteSerializer",
    "BillzStatusSerializer",
    "CategoryListQuerySerializer",
    "CategoryNameSerializer",
    "CategoryPatchSerializer",
    "CategoryResultsSerializer",
    "CategorySerializer",
    "CategoryWriteSerializer",
    "IdOrderSerializer",
    "ImageAltSerializer",
    "ImageOrderSerializer",
    "ImagePatchSerializer",
    "ImageRenditionsPutSerializer",
    "ImagesResponseSerializer",
    "LastChangeSerializer",
    "ProductImageSerializer",
    "ProductListQuerySerializer",
    "ProductPatchSerializer",
    "ProductSerializer",
    "ProductStatsSerializer",
    "ProductTextSerializer",
    "ProductTextsPatchSerializer",
    "ProductTextsSerializer",
    "ProductUzTextPatchSerializer",
    "ProductUzTextSerializer",
    "ProductWriteSerializer",
    "SeoIssueSerializer",
    "SeoStatusSerializer",
    "SeoTextSerializer",
    "SeoTextsSerializer",
    "SlugOrderSerializer",
    "StrictBooleanField",
    "image_alt",
    "product_texts",
    "seo_texts",
    "validate_product_slug",
]
