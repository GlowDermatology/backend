"""Read-only Django admin for the loyalty models (design 02 §6). Writes go through ``loyalty.services``.

The only secret column of the module is ``PosDevice.key_hash`` (review minor 20): excluded.
"""

from django.contrib import admin

from glowderma.core.admin import ReadOnlyAdmin, ReadOnlyTabularInline, register_read_only
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
