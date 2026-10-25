"""Read-only Django admin for the loyalty models (design 02 §6). Writes go through ``loyalty.services``.

The only secret column of the module is ``PosDevice.key_hash`` (review minor 20): excluded.
"""

from typing import TYPE_CHECKING

from django.contrib import admin
from django.db import models
from django.http import HttpRequest

from glowderma.core.admin import ReadOnlyAdmin, register_read_only
from glowderma.loyalty.models import (
    CashbackHold,
    CashbackPolicy,
    CashbackTier,
    LedgerEntry,
    LoyaltyAccount,
    PosDevice,
    Purchase,
    PurchaseItem,
)

if TYPE_CHECKING:
    _TabularInline = admin.TabularInline[models.Model, models.Model]
else:
    _TabularInline = admin.TabularInline  # the runtime class is not generic


class ReadOnlyTabularInline(_TabularInline):
    """An inline that only lists rows: no add/change/delete, every field read-only.

    The view grant mirrors ``ReadOnlyAdmin``'s (every active ``is_staff`` user): Django's default falls
    back to ``user.has_perm(...)``, which would drop the inline for exactly the users the read-only admin
    exists for. ``core.admin`` has no inline base on this branch; ``commerce.admin`` carries the same class.
    """

    extra = 0
    can_delete = False
    show_change_link = True

    def has_view_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        user = request.user
        return bool(user.is_active and user.is_staff)

    def has_add_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: models.Model | None = None) -> bool:
        return False

    def get_readonly_fields(self, request: HttpRequest, obj: models.Model | None = None) -> list[str]:
        # ``fields`` is declared flat (no fieldset-style nesting) on every inline below.
        return [name for name in self.fields or () if isinstance(name, str)]


class CashbackTierInline(ReadOnlyTabularInline):
    model = CashbackTier
    fields = ("from_amount", "to_amount", "percent", "created_at")
    ordering = ("from_amount",)


class PurchaseItemInline(ReadOnlyTabularInline):
    model = PurchaseItem
    fields = ("position", "name", "billz_product_id", "quantity", "unit_price", "promo", "line_total")
    ordering = ("position",)


class CashbackPolicyAdmin(ReadOnlyAdmin):
    list_display = ("version", "effective_from", "basis", "earn_on_cashback_paid", "author", "created_at")
    list_filter = ("basis",)
    ordering = ("-version",)
    raw_id_fields = ("author",)
    inlines = (CashbackTierInline,)
    readonly_fields = ("state", "effective_to", "superseded_by")  # derived (design 02 §1.1), change page only


class PurchaseAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        "customer",
        "source",
        "at",
        "amount",
        "cashback_amount",
        "order",
        "billz_sale_ref",
        "reversed",
    )
    list_filter = ("source", "reversed")
    search_fields = ("billz_sale_id", "billz_sale_ref", "=order__id", "customer__phone")
    ordering = ("-at", "-id")
    raw_id_fields = ("customer", "order", "policy", "credited_entry", "reversal_entry")
    inlines = (PurchaseItemInline,)
    date_hierarchy = "at"


admin.site.register(CashbackPolicy, CashbackPolicyAdmin)
admin.site.register(Purchase, PurchaseAdmin)
register_read_only(
    CashbackTier,
    list_display=("id", "policy", "from_amount", "to_amount", "percent", "created_at"),
    search_fields=("=policy__version",),
    ordering=("-policy_id", "from_amount"),
    raw_id_fields=("policy",),
)
register_read_only(
    LoyaltyAccount,
    list_display=("id", "customer", "balance", "updated_at"),
    search_fields=("customer__phone",),
    ordering=("-updated_at", "-id"),
    raw_id_fields=("customer",),
)
register_read_only(
    LedgerEntry,
    list_display=(
        "id",
        "at",
        "account",
        "kind",
        "amount",
        "balance_after",
        "source_type",
        "source_ref",
        "order",
        "actor",
    ),
    list_filter=("kind", "source_type"),
    search_fields=("source_ref", "=order__id", "account__customer__phone"),
    ordering=("-at", "-id"),
    raw_id_fields=("account", "order", "purchase", "policy", "reverses", "actor"),
    date_hierarchy="at",
)
register_read_only(
    CashbackHold,
    list_display=("id", "order", "account", "amount", "state", "expires_at", "captured_at", "released_at"),
    list_filter=("state", "release_reason"),
    search_fields=("=order__id",),
    ordering=("-id",),
    raw_id_fields=("account", "order", "captured_entry"),
)
register_read_only(
    PurchaseItem,
    list_display=(
        "id",
        "purchase",
        "position",
        "name",
        "billz_product_id",
        "quantity",
        "unit_price",
        "promo",
    ),
    search_fields=("=purchase__id", "billz_product_id", "name"),
    ordering=("-purchase_id", "position"),
    raw_id_fields=("purchase", "product"),
)
register_read_only(
    PosDevice,
    list_display=("id", "name", "location_label", "key_prefix", "is_active", "last_used_at", "created_by"),
    list_filter=("is_active",),
    search_fields=("name",),
    ordering=("name",),
    raw_id_fields=("created_by",),
    exclude=("key_hash",),
)
