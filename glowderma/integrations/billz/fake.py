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
``ProductStock.synced_at`` being refreshed. ``synced_at`` of every yielded row is ``timeutils.now()``.
"""

from collections.abc import Iterator
from dataclasses import dataclass

from glowderma.core import timeutils

from .ports import BillzProduct, ImportMode, KnownProducts

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

    def __init__(self, known: KnownProducts | None = None) -> None:
        self.known: KnownProducts = known if known is not None else _NoKnownProducts()

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
