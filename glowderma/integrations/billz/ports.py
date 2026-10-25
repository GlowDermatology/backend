"""The Billz port (contract §2.3 "Products and Billz", design "Provider adapters").

Billz owns ``billz_name``, ``volume``, the reference price and the stock of every product. The catalog
module talks to Billz only through :class:`BillzPort`; ``registry.get_billz()`` picks the fake or the live
implementation from ``settings.BILLZ_MODE``.

``integrations`` sits below ``catalog`` in the layer contract, so an adapter can never read the catalog
tables itself. The fake adapter needs the products the shop already knows (to answer "the same Billz
fields, no drift"), so the caller hands it a :class:`KnownProducts` view — a small read protocol the
catalog module implements over its own selectors.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

type ImportMode = Literal["delta", "full"]


@dataclass(frozen=True, slots=True)
class BillzProduct:
    """What Billz asserts about one product (the Billz-owned fields of ``catalog.Product``)."""

    billz_id: str
    billz_name: str
    volume: str
    price: int | None  # Billz's own price, reference only (contract: ``billz_price``)
    stock: int  # Billz can report negatives after returns
    synced_at: datetime  # when these values were read from Billz


class BillzError(Exception):
    """Billz did not answer or answered with an error; the caller maps it to ``provider_unavailable``."""


class KnownProducts(Protocol):
    """The products the catalog already holds, as Billz currently sees them (read-only)."""

    def get(self, billz_id: str) -> BillzProduct | None: ...

    def all(self) -> Iterator[BillzProduct]: ...


class BillzPort(Protocol):
    def is_configured(self) -> bool:
        """``True`` when the adapter can reach Billz (the fake always can)."""
        ...

    def get_product(self, billz_id: str) -> BillzProduct | None:
        """One product by Billz id or barcode; ``None`` when Billz does not know it."""
        ...

    def iter_products(self, mode: ImportMode) -> Iterator[BillzProduct]:
        """Every product of a ``delta`` (changed since the last sync) or ``full`` import."""
        ...
