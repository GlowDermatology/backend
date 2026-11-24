"""Request and response shapes of the customer auth, profile and catalog routes (customer contract §2.1–§2.3),
one subject per module: ``auth`` (OTP, registration, login, refresh), ``social``, ``me`` (profile, consent,
device, password), ``contacts`` (phone and e-mail), ``addresses``, ``sessions`` and ``catalog``; ``fields``
holds the shared field factories.

Request serializers raise contract field codes (``core.api.exceptions.field_error``; endpoint-specific codes
and the customer field keys of C§5.0 through ``message_key``); response serializers exist for the OpenAPI
schema only — the views return the selectors' dictionaries.
"""

from glowderma.customers.api.customer.serializers.addresses import (
    REFERENCE_CODE,
    AddressWriteSerializer,
    CustomerAddressListSerializer,
    CustomerAddressSerializer,
)
from glowderma.customers.api.customer.serializers.auth import (
    SIGNED_IN_PURPOSES,
    AuthTokensSerializer,
    CodeIssueSerializer,
    CustomerLoginSerializer,
    CustomerLogoutSerializer,
    CustomerRefreshSerializer,
    IdentifierSerializer,
    NextStepSerializer,
    OtpInfoSerializer,
    RefreshResponseSerializer,
    ResetPasswordSerializer,
    SendCodeSerializer,
    SetPasswordSerializer,
    VerificationSerializer,
    VerifyCodeSerializer,
)
from glowderma.customers.api.customer.serializers.catalog import (
    CATEGORY_SLUG_MAX_LENGTH,
    SEARCH_MAX_LENGTH,
    CustomerBannerLinkSerializer,
    CustomerBannerListSerializer,
    CustomerBannerSerializer,
    CustomerCategoryListSerializer,
    CustomerCategorySerializer,
    CustomerProductImageSerializer,
    ProductCardSerializer,
    ProductCategorySerializer,
    ProductDetailSerializer,
    ProductListQuerySerializer,
)
from glowderma.customers.api.customer.serializers.contacts import (
    MeEmailCodeSerializer,
    MeEmailSerializer,
    MePhoneCodeSerializer,
    MePhoneSerializer,
)
from glowderma.customers.api.customer.serializers.fields import (
    CODE_PATTERN,
    CustomerEmailField,
    CustomerPhoneField,
    clean_code,
    clean_name,
)
from glowderma.customers.api.customer.serializers.me import (
    ConsentSerializer,
    ConsentStateSerializer,
    CustomerMeSerializer,
    DeviceSerializer,
    DeviceStateSerializer,
    ImageUrlSerializer,
    MePasswordSerializer,
    MePatchSerializer,
    ReauthSerializer,
)
from glowderma.customers.api.customer.serializers.sessions import (
    CustomerSessionSerializer,
    RevokedSessionsSerializer,
)
from glowderma.customers.api.customer.serializers.social import (
    MeSocialLinkSerializer,
    NonceSerializer,
    SocialCompleteSerializer,
    SocialSerializer,
)

__all__ = [
    "CATEGORY_SLUG_MAX_LENGTH",
    "CODE_PATTERN",
    "REFERENCE_CODE",
    "SEARCH_MAX_LENGTH",
    "SIGNED_IN_PURPOSES",
    "AddressWriteSerializer",
    "AuthTokensSerializer",
    "CodeIssueSerializer",
    "ConsentSerializer",
    "ConsentStateSerializer",
    "CustomerAddressListSerializer",
    "CustomerAddressSerializer",
    "CustomerBannerLinkSerializer",
    "CustomerBannerListSerializer",
    "CustomerBannerSerializer",
    "CustomerCategoryListSerializer",
    "CustomerCategorySerializer",
    "CustomerEmailField",
    "CustomerLoginSerializer",
    "CustomerLogoutSerializer",
    "CustomerMeSerializer",
    "CustomerPhoneField",
    "CustomerProductImageSerializer",
    "CustomerSessionSerializer",
    "CustomerRefreshSerializer",
    "DeviceSerializer",
    "DeviceStateSerializer",
    "IdentifierSerializer",
    "ImageUrlSerializer",
    "MeEmailCodeSerializer",
    "MeEmailSerializer",
    "MePasswordSerializer",
    "MePatchSerializer",
    "MePhoneCodeSerializer",
    "MePhoneSerializer",
    "MeSocialLinkSerializer",
    "NextStepSerializer",
    "NonceSerializer",
    "OtpInfoSerializer",
    "ProductCardSerializer",
    "ProductCategorySerializer",
    "ProductDetailSerializer",
    "ProductListQuerySerializer",
    "ReauthSerializer",
    "RefreshResponseSerializer",
    "ResetPasswordSerializer",
    "RevokedSessionsSerializer",
    "SendCodeSerializer",
    "SetPasswordSerializer",
    "SocialCompleteSerializer",
    "SocialSerializer",
    "VerificationSerializer",
    "VerifyCodeSerializer",
    "clean_code",
    "clean_name",
]
