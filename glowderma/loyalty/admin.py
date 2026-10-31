"""Read-only Django admin for the loyalty models (design 02 §6). Writes go through ``loyalty.services``.

The only secret column of the module is ``PosDevice.key_hash`` (review minor 20): excluded.
"""

from typing import Any

from django.contrib import admin

from glowderma.core.admin import (
    DANGER,
    INFO,
    SUCCESS,
    WARNING,
    ReadOnlyAdmin,
    ReadOnlyTabularInline,
    customer_column,
    customer_link_column,
    date_range,
    display,
    dropdown,
    money_column,
    number_link_column,
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

POLICY_STATE_LABELS = {
    "active": "Amalda",
    "scheduled": "Rejalashtirilgan",
    "expired": "Muddati tugagan",
    "superseded": "Almashtirilgan",
}
POLICY_STATE_VARIANTS = {"active": SUCCESS, "scheduled": INFO, "expired": WARNING, "superseded": WARNING}
LEDGER_KIND_VARIANTS = {"credit": SUCCESS, "debit": INFO, "adjustment": WARNING, "reversal": DANGER}
HOLD_STATE_VARIANTS = {"active": INFO, "captured": SUCCESS, "released": WARNING}


class CashbackTierInline(ReadOnlyTabularInline):
    model = CashbackTier
    fields = ("from_amount_som", "to_amount_som", "percent", "created_at")
    ordering = ("from_amount",)
    from_amount_som = staticmethod(money_column("from_amount", "Summadan"))
    to_amount_som = staticmethod(money_column("to_amount", "Summagacha"))


class PurchaseItemInline(ReadOnlyTabularInline):
    model = PurchaseItem
    fields = ("position", "name", "billz_product_id", "quantity", "unit_price_som", "promo", "line_total_som")
    ordering = ("position",)
    unit_price_som = staticmethod(money_column("unit_price", "Narx"))
    line_total_som = staticmethod(money_column("line_total", "Jami"))


class CashbackPolicyAdmin(ReadOnlyAdmin):
    list_display = ("version", "effective_from", "basis", "earn_on_cashback_paid", "author", "created_at")
    list_filter = ("basis",)
    search_fields = ("=version",)
    ordering = ("-version",)
    autocomplete_fields = ("author",)
    inlines = (CashbackTierInline,)
    # derived (design 02 §1.1), change page only
    readonly_fields = ("policy_state", "policy_effective_to", "policy_superseded_by")

    @display(description="Holat", label=POLICY_STATE_VARIANTS)
    def policy_state(self, policy: CashbackPolicy) -> tuple[str, str]:
        return (policy.state, POLICY_STATE_LABELS.get(policy.state, policy.state))

    @display(description="Amal qilish tugashi", empty_value="-")
    def policy_effective_to(self, policy: CashbackPolicy) -> Any:
        return policy.effective_to

    @display(description="O‘rnini olgan versiya", empty_value="-")
    def policy_superseded_by(self, policy: CashbackPolicy) -> str | None:
        return None if policy.superseded_by is None else f"v{policy.superseded_by}"


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


@display(description="Manba", ordering="source_type")
def ledger_source(entry: LedgerEntry) -> str:
    """The entry's source; an order's reference repeats the «Buyurtma» column, so only other sources
    (a Billz sale number, an admin note) show their reference."""
    label = str(entry.get_source_type_display())
    if entry.source_type == "order" or not entry.source_ref:
        return label
    return f"{label} · {entry.source_ref}"


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
        customer_link_column("account", "Hisob"),
        status_column("kind", "Turi", LEDGER_KIND_VARIANTS, LEDGER_KIND),
        money_column("amount", "Summa"),
        money_column("balance_after", "Balans"),
        ledger_source,
        number_link_column("order", "Buyurtma"),
        "actor",
    ),
    list_select_related=("account__customer", "actor"),
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
        number_link_column("order", "Buyurtma"),
        customer_link_column("account", "Hisob"),
        money_column("amount", "Summa"),
        status_column("state", "Holat", HOLD_STATE_VARIANTS),
        "expires_at",
        "captured_at",
        "released_at",
    ),
    list_filter=(dropdown("state"), dropdown("release_reason")),
    search_fields=("=order__id",),
    ordering=("-id",),
    list_select_related=("account__customer",),
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
