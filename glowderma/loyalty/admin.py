"""Read-only Django admin for the loyalty models (design 02 §6). Writes go through ``loyalty.services``.

The only secret column of the module is ``PosDevice.key_hash`` (review minor 20): excluded.
"""

from django.contrib import admin

from glowderma.core.admin import (
    DANGER,
    INFO,
    SUCCESS,
    WARNING,
    ReadOnlyAdmin,
    ReadOnlyTabularInline,
    customer_column,
    date_range,
    dropdown,
    money_column,
    register_read_only,
    status_column,
)
from glowderma.core.labels import LEDGER_KIND
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

LEDGER_KIND_VARIANTS = {"credit": SUCCESS, "debit": INFO, "adjustment": WARNING, "reversal": DANGER}
HOLD_STATE_VARIANTS = {"active": INFO, "captured": SUCCESS, "released": WARNING}


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
    search_fields = ("=version",)
    ordering = ("-version",)
    autocomplete_fields = ("author",)
    inlines = (CashbackTierInline,)
    readonly_fields = ("state", "effective_to", "superseded_by")  # derived (design 02 §1.1), change page only


class PurchaseAdmin(ReadOnlyAdmin):
    list_display = (
        "id",
        customer_column(),
        "source",
        "at",
        money_column("amount", "Summa"),
        money_column("cashback_amount", "Cashback"),
        "order",
        "billz_sale_ref",
        "reversed",
    )
    list_filter = (dropdown("source"), "reversed", date_range("at"))
    list_select_related = ("customer", "order")
    search_fields = ("billz_sale_id", "billz_sale_ref", "=order__id", "customer__phone")
    ordering = ("-at", "-id")
    autocomplete_fields = ("customer", "order", "policy", "credited_entry", "reversal_entry")
    inlines = (PurchaseItemInline,)
    date_hierarchy = "at"


admin.site.register(CashbackPolicy, CashbackPolicyAdmin)
admin.site.register(Purchase, PurchaseAdmin)
register_read_only(
    CashbackTier,
    list_display=("id", "policy", "from_amount", "to_amount", "percent", "created_at"),
    search_fields=("=policy__version",),
    ordering=("-policy_id", "from_amount"),
    autocomplete_fields=("policy",),
)
register_read_only(
    LoyaltyAccount,
    list_display=("id", customer_column(), money_column("balance", "Balans"), "updated_at"),
    list_select_related=("customer",),
    search_fields=("customer__phone", "customer__name"),
    ordering=("-updated_at", "-id"),
    autocomplete_fields=("customer",),
)
register_read_only(
    LedgerEntry,
    list_display=(
        "id",
        "at",
        "account",
        status_column("kind", "Turi", LEDGER_KIND_VARIANTS, LEDGER_KIND),
        money_column("amount", "Summa"),
        money_column("balance_after", "Balans"),
        "source_type",
        "source_ref",
        "order",
        "actor",
    ),
    list_filter=(dropdown("kind"), dropdown("source_type"), date_range("at")),
    search_fields=("source_ref", "=order__id", "account__customer__phone"),
    ordering=("-at", "-id"),
    autocomplete_fields=("account", "order", "purchase", "policy", "reverses", "actor"),
    date_hierarchy="at",
)
register_read_only(
    CashbackHold,
    list_display=(
        "id",
        "order",
        "account",
        money_column("amount", "Summa"),
        status_column("state", "Holat", HOLD_STATE_VARIANTS),
        "expires_at",
        "captured_at",
        "released_at",
    ),
    list_filter=(dropdown("state"), dropdown("release_reason")),
    search_fields=("=order__id",),
    ordering=("-id",),
    autocomplete_fields=("account", "order", "captured_entry"),
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
        money_column("unit_price", "Narx"),
        "promo",
    ),
    search_fields=("=purchase__id", "billz_product_id", "name"),
    ordering=("-purchase_id", "position"),
    autocomplete_fields=("purchase", "product"),
)
register_read_only(
    PosDevice,
    list_display=("id", "name", "location_label", "key_prefix", "is_active", "last_used_at", "created_by"),
    list_filter=("is_active",),
    search_fields=("name",),
    ordering=("name",),
    autocomplete_fields=("created_by",),
    exclude=("key_hash",),
)
