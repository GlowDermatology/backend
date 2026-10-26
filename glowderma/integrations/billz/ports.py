"""The Billz port (contract §2.3 "Products and Billz", design "Provider adapters").

The live adapter, its endpoints and the stubs are described in docs/integrations/billz.md.

Billz owns ``billz_name``, ``volume``, the reference price and the stock of every product. The catalog
module talks to Billz only through :class:`BillzPort`; ``registry.get_billz()`` picks the fake or the live
implementation from ``settings.BILLZ_MODE``.

``integrations`` sits below ``catalog`` in the layer contract, so an adapter can never read the catalog
tables itself. The fake adapter needs the products the shop already knows (to answer "the same Billz
fields, no drift"), so the caller hands it a :class:`KnownProducts` view — a small read protocol the
catalog module implements over its own selectors.

Catalog sync: ``iter_products(mode)`` yields every row of one run. A ``full`` run yields the **complete**
listing or raises (every page arrived, the count is plausible), so the caller may hide the products Billz no
longer lists; a ``delta`` run yields what changed since the last successful run. Once the caller has applied
every row it calls ``sync_succeeded(mode)``, and only then does the adapter advance its cursor (the delta
watermark), so a run that failed half way is fetched again.

Sale push (``commerce``'s ``billz.push_sale`` handler): ``push_sale(request, journal)`` records a paid online
order as a Billz sale, idempotently: the adapter calls ``journal.order_created`` as soon as Billz created the
sale, **before** anything else is sent, and the caller persists that id (``Purchase.billz_sale_id``) so a
retry after a crash at any later step resumes on the same Billz sale (``request.billz_order_id``).

Failures are typed: :class:`BillzUnavailable` is transient (transport, 429, 5xx, timeouts, auth: retry
later), :class:`BillzRejected` is Billz refusing for good (validation, unknown product, closed shift …: never
retried), :class:`BillzNotSupported` an explicit stub for what the Billz API does not offer (decision
2026-10-26: refunds of paid sales, per-receipt discovery of in-store sales, webhooks).
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

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
    """Billz did not answer or answered with an error; the caller maps it to ``code`` (a job error code)."""

    code = "provider_unavailable"


class BillzUnavailable(BillzError):
    """Transient: transport error, timeout, 429, 5xx, an ambiguous async answer or a token problem.

    The request may be retried later; a write whose outcome is unknown is re-checked before it is repeated.
    """


class BillzRejected(BillzError):
    """Billz refused the request for good (HTTP 4xx or a business ``error_code``): retrying cannot help."""

    def __init__(self, reason: str, *, error_code: int | None = None, http_status: int | None = None) -> None:
        self.reason = reason
        self.error_code = error_code
        self.http_status = http_status
        suffix = f" (Billz error {error_code})" if error_code is not None else ""
        status = f" [HTTP {http_status}]" if http_status is not None else ""
        super().__init__(f"{reason}{suffix}{status}")


class BillzListingIncomplete(BillzError):
    """A full listing did not arrive whole or is implausibly small: nothing is applied, nothing hidden."""

    code = "billz_listing_incomplete"


class BillzNotSupported(BillzError):
    """An explicit stub (decision 2026-10-26): the Billz public API has no documented call for ``feature``."""

    code = "billz_not_supported"

    def __init__(self, feature: str, detail: str) -> None:
        self.feature = feature
        super().__init__(f"Billz does not support {feature}: {detail}")


class KnownProducts(Protocol):
    """The products the catalog already holds, as Billz currently sees them (read-only)."""

    def get(self, billz_id: str) -> BillzProduct | None: ...

    def all(self) -> Iterator[BillzProduct]: ...


@dataclass(frozen=True, slots=True)
class SaleLine:
    billz_id: str  # the catalog's Billz id (the product barcode in Billz)
    quantity: int
    unit_price: int  # so'm, Glow Derma's price (what the customer paid per unit)


@dataclass(frozen=True, slots=True)
class SaleRequest:
    """A paid online order to record in Billz (``without_cashback``: cashback lives in Glow Derma only)."""

    order_id: int  # Glow Derma order number: the payment comment
    order_uuid: UUID  # ``Purchase.billz_order_uuid``: our correlation id (logs, the fake's echo)
    lines: Sequence[SaleLine]
    customer_phone: str  # +998… of the customer (loyalty identity); "" = no Billz client is linked
    billz_order_id: str = ""  # the Billz sale an earlier attempt created ("" = none yet)


@dataclass(frozen=True, slots=True)
class SaleReceipt:
    """The recorded sale: ``billz_order_id`` names it in the order history (``billz_sale_recorded``)."""

    billz_order_id: str  # Billz's sale UUID (the fake echoes ``order_uuid``)
    billz_sale_id: str  # stored on the purchase ("" when Billz created nothing, i.e. the fake)
    billz_sale_ref: str  # short number the cashier sees (``order_number``), ≤ 32 characters, or ""


class SaleJournal(Protocol):
    """Where the caller persists the Billz sale id the moment Billz created it (design "Billz sale")."""

    def order_created(self, billz_order_id: str, billz_sale_ref: str) -> None:
        """Persist (and commit) the id; raising stops the push before any item is sent."""
        ...


class BillzPort(Protocol):
    def is_configured(self) -> bool:
        """``True`` when the adapter can reach Billz (the fake always can)."""
        ...

    def get_product(self, billz_id: str) -> BillzProduct | None:
        """One product by Billz id or barcode; ``None`` when Billz does not know it."""
        ...

    def iter_products(self, mode: ImportMode) -> Iterator[BillzProduct]:
        """Every product of a ``delta`` (changed since the last sync) or ``full`` import (module doc)."""
        ...

    def sync_succeeded(self, mode: ImportMode) -> None:
        """Every row of the last ``iter_products(mode)`` was applied: advance the sync cursor."""
        ...

    def push_sale(self, request: SaleRequest, journal: SaleJournal) -> SaleReceipt:
        """Record the paid online order as a Billz sale, resuming ``request.billz_order_id`` when set."""
        ...

    def refund_sale(self, billz_order_id: str) -> None:
        """Stub: Billz documents no refund of a paid sale (always :class:`BillzNotSupported`)."""
        ...

    def find_store_sale(self, receipt_ref: str) -> None:
        """Stub: no per-receipt lookup of an in-store sale after a POS QR scan (L6/L7)."""
        ...

    def parse_webhook(self, body: bytes) -> None:
        """Stub: Billz sends no webhooks (always :class:`BillzNotSupported`)."""
        ...


# What the explicit stubs say (shared by the fake and the live adapter so both refuse identically).
REFUND_NOT_SUPPORTED = (
    "the public API documents no refund or return of a paid sale; staff refund the sale by hand in Billz"
)
STORE_SALE_NOT_SUPPORTED = (
    "the public API has no lookup of a sale by receipt and no sales feed (open questions L6/L7); "
    "in-store purchases are not discovered automatically"
)
WEBHOOKS_NOT_SUPPORTED = "Billz sends no webhooks; the catalog is polled (hourly delta, daily full sync)"


def refuse_refund(billz_order_id: str) -> None:
    raise BillzNotSupported("refunds of paid sales", REFUND_NOT_SUPPORTED)


def refuse_store_sale(receipt_ref: str) -> None:
    raise BillzNotSupported("in-store sale discovery", STORE_SALE_NOT_SUPPORTED)


def refuse_webhook(body: bytes) -> None:
    raise BillzNotSupported("webhooks", WEBHOOKS_NOT_SUPPORTED)
