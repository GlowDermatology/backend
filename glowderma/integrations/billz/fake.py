"""Deterministic Billz for tests and local end-to-end runs (``BILLZ_MODE=fake``).

The admin e2e scripts rely on these rules; change them only together with the frontend.

``get_product(billz_id)``
    * a product the catalog already holds (``KnownProducts.get``) → its current Billz-owned fields (so a
      lookup never "drifts" a known product);
    * an id ending in ``0000`` → ``None`` (Billz does not know it → ``billz_not_found``);
    * anything else → a synthetic product ``{billz_name: "BILLZ ITEM <id>", volume: "50 ml",
      price: 50_000 + (int(id) % 400) * 1_000, stock: int(id) % 37}``.

``iter_products(mode)`` (``delta`` and ``full`` yield the same rows)
    * first the three NEW products ``4607019901`` "NEW SERUM 30ML" (199 000, stock 11, "30 ml"),
      ``4607019902`` "NEW CREAM 50ML" (249 000, stock 7, "50 ml") and ``4607019903`` "NEW TONER 200ML"
      (129 000, stock 23, "200 ml");
    * then every known product with exactly the same Billz fields it already has (stock unchanged).

So an import is idempotent: the only visible effect is those three hidden ``is_new_from_billz`` products
appearing once (the second run leaves them unchanged), ``billz.last_sync_at`` moving to now and
``ProductStock.synced_at`` being refreshed. ``synced_at`` of every yielded row is ``timeutils.now()``. A
``full`` run lists every known product, so the catalog's reconciliation never hides anything through the
fake; ``sync_succeeded`` keeps no cursor.

``push_sale(request, journal)`` (``commerce``'s ``billz.push_sale``; the order's ``metadata["fake"]["billz"]``
is the scenario)
    * no scenario → accepted; Billz "echoes" no sale (``billz_sale_id = ""``, the journal is never called) and
      the receipt names ``request.order_uuid`` as the Billz order id;
    * ``{"push": "down"}`` → :class:`BillzUnavailable` (transient: the outbox retries, ten failures make the
      message dead);
    * ``{"push": "rejected"}`` → :class:`BillzRejected` with Billz error 20036 (permanent: dead at once).

The stubs (``refund_sale``, ``find_store_sale``, ``parse_webhook``) refuse exactly like the live adapter: a
fake never invents what Billz cannot do.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from glowderma.core import timeutils

from .ports import (
    BillzProduct,
    BillzRejected,
    BillzUnavailable,
    ImportMode,
    KnownProducts,
    SaleJournal,
    SaleReceipt,
    SaleRequest,
    refuse_refund,
    refuse_store_sale,
    refuse_webhook,
)

NOT_FOUND_SUFFIX = "0000"


@dataclass(frozen=True, slots=True)
class _NewItem:
    billz_id: str
    billz_name: str
    volume: str
    price: int
    stock: int


NEW_ITEMS: tuple[_NewItem, ...] = (
    _NewItem("4607019901", "NEW SERUM 30ML", "30 ml", 199_000, 11),
    _NewItem("4607019902", "NEW CREAM 50ML", "50 ml", 249_000, 7),
    _NewItem("4607019903", "NEW TONER 200ML", "200 ml", 129_000, 23),
)
NEW_ITEM_IDS = frozenset(item.billz_id for item in NEW_ITEMS)


class _NoKnownProducts:
    def get(self, billz_id: str) -> BillzProduct | None:
        return None

    def all(self) -> Iterator[BillzProduct]:
        return iter(())


def synthetic_product(billz_id: str) -> BillzProduct:
    """The deterministic product the fake invents for an unknown id (``id`` is 6..14 ASCII digits)."""
    number = int(billz_id)
    return BillzProduct(
        billz_id=billz_id,
        billz_name=f"BILLZ ITEM {billz_id}",
        volume="50 ml",
        price=50_000 + (number % 400) * 1_000,
        stock=number % 37,
        synced_at=timeutils.now(),
    )


class FakeBillz:
    name = "billz"

    def __init__(self, known: KnownProducts | None = None, scenario: Mapping[str, Any] | None = None) -> None:
        self.known: KnownProducts = known if known is not None else _NoKnownProducts()
        self.scenario: Mapping[str, Any] = scenario or {}

    def is_configured(self) -> bool:
        return True

    def get_product(self, billz_id: str) -> BillzProduct | None:
        existing = self.known.get(billz_id)
        if existing is not None:
            return existing
        if billz_id.endswith(NOT_FOUND_SUFFIX):
            return None
        return synthetic_product(billz_id)

    def iter_products(self, mode: ImportMode) -> Iterator[BillzProduct]:
        synced_at = timeutils.now()
        for item in NEW_ITEMS:
            yield BillzProduct(
                billz_id=item.billz_id,
                billz_name=item.billz_name,
                volume=item.volume,
                price=item.price,
                stock=item.stock,
                synced_at=synced_at,
            )
        for product in self.known.all():
            if product.billz_id in NEW_ITEM_IDS:
                continue  # already yielded above with the same fields
            yield BillzProduct(
                billz_id=product.billz_id,
                billz_name=product.billz_name,
                volume=product.volume,
                price=product.price,
                stock=product.stock,
                synced_at=synced_at,
            )

    def sync_succeeded(self, mode: ImportMode) -> None:
        return None  # the fake keeps no cursor

    def push_sale(self, request: SaleRequest, journal: SaleJournal) -> SaleReceipt:
        outcome = self.scenario.get("push")
        if outcome == "down":
            raise BillzUnavailable("Billz did not answer (fake: down)")
        if outcome == "rejected":
            raise BillzRejected("product not found (fake: rejected)", error_code=20036, http_status=400)
        return SaleReceipt(billz_order_id=str(request.order_uuid), billz_sale_id="", billz_sale_ref="")

    def refund_sale(self, billz_order_id: str) -> None:
        refuse_refund(billz_order_id)

    def find_store_sale(self, receipt_ref: str) -> None:
        refuse_store_sale(receipt_ref)

    def parse_webhook(self, body: bytes) -> None:
        refuse_webhook(body)
