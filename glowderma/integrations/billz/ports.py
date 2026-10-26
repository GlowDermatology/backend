"""The Billz port (contract §2.3 "Products and Billz", design "Provider adapters").

The live adapter, its endpoints and the stubs are described in docs/integrations/billz.md.

Billz owns ``billz_name``, ``volume``, the reference price and the stock of every product. The catalog
module talks to Billz only through :class:`BillzPort`; ``registry.get_billz()`` picks the fake or the live
implementation from ``settings.BILLZ_MODE``.

``integrations`` sits below ``catalog`` in the layer contract, so an adapter can never read the catalog
tables itself. The fake adapter needs the products the shop already knows (to answer "the same Billz
fields, no drift"), so the caller hands it a :class:`KnownProducts` view — a small read protocol the
catalog module implements over its own selectors.

Stock freshness (gap X-STOCK-FRESHNESS): ``read_stock(billz_ids, budget_seconds=…)`` reads the current Billz
row of each barcode (one lookup per barcode, one deadline for all of them) and never raises for a Billz
failure: what it could not read because Billz failed (transport error, 5xx, auth, circuit open) is
reported :attr:`StockRead.unavailable`; what it did not reach because the call's own budget ran out
(:class:`BillzOutOfTime`: the deadline, the limiter wait, a 429 pause) or because one barcode's search was too
ambiguous is :attr:`StockRead.deferred` — except a request the deadline cut short before Billz answered any
lookup of the call, which is Billz not answering (unavailable); what Billz does not know
:attr:`StockRead.unknown`.

Catalog sync: ``iter_products(mode)`` yields every row of one run. A ``full`` run yields the **complete**
listing or raises (every page arrived, the count is plausible), so the caller may hide the products Billz no
longer lists; a ``delta`` run yields what changed since the last successful run. Once the caller has applied
every row it calls ``sync_succeeded(mode)``, and only then does the adapter advance its cursor (the delta
watermark), so a run that failed half way is fetched again.

Sale push (``commerce``'s ``billz.push_sale`` handler): ``push_sale(request, journal)`` records a paid online
order as a Billz sale, idempotently, through the caller's :class:`SaleJournal`: under the adapter's sale lock
it re-reads what the caller persisted (:meth:`SaleJournal.progress`); it calls ``journal.order_created`` as
soon as Billz created the sale, **before** anything else is sent, so a retry after a crash at any later step
resumes on the same Billz sale; and it calls ``journal.before_payment`` right before the payment request,
which re-checks the order (:class:`SaleCancelled` stops the push) and persists the payment attempt, so an
answer lost in transit is never followed by a second payment.

Failures are typed: :class:`BillzUnavailable` is transient (transport, 429, 5xx, timeouts, auth,
configuration: retry later), with three deferrals the caller retries without spending its attempts —
:class:`BillzShiftClosed` (the shift or cash box is closed), :class:`BillzPaymentPending` (a payment whose
outcome is still unknown) and :class:`BillzSaleBusy` (another worker is pushing the same sale);
:class:`BillzRejected` is Billz refusing for good (validation, unknown product …: never retried) and its
subclass :class:`BillzCheckByHand` the adapter stopping on purpose because staff must look at the Billz sale
(a bill that differs from what the customer paid, a payment outcome that stayed unknown);
:class:`BillzNotSupported` an explicit stub for what the Billz API does not offer (decision 2026-10-26:
refunds of paid sales, per-receipt discovery of in-store sales, webhooks). Every error carries ``http_status``
/ ``error_code`` when Billz answered, and ``not_applied`` when Billz certainly did not apply the request (it
was never sent, or Billz answered with a refusal).
"""

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID

type ImportMode = Literal["delta", "full"]


@dataclass(frozen=True, slots=True)
class BillzProduct:
    """What Billz asserts about one product (the Billz-owned fields of ``catalog.Product``)."""

    billz_id: str
    billz_name: str
    volume: str | None  # None = not asserted (no ``BILLZ_VOLUME_FIELD``): the stored volume is left alone
    price: int | None  # Billz's own price, reference only (contract: ``billz_price``)
    stock: int  # Billz can report negatives after returns
    synced_at: datetime  # when these values were read from Billz


@dataclass(frozen=True, slots=True)
class StockRead:
    """What one :meth:`BillzPort.read_stock` call learned; every requested barcode is in exactly one set."""

    rows: Mapping[str, BillzProduct]  # barcode → Billz's row (``synced_at`` = when that barcode was asked)
    unknown: frozenset[str]  # Billz does not know the barcode (no sellable stock)
    unavailable: frozenset[str]  # not read because Billz failed: transport error, 5xx, auth, circuit open
    # not reached because the call's own budget ran out (BillzOutOfTime) or the search was ambiguous
    deferred: frozenset[str] = frozenset()


class BillzError(Exception):
    """Billz did not answer or answered with an error; the caller maps it to ``code`` (a job error code).

    ``http_status`` / ``error_code``: Billz's answer, when there was one. ``not_applied``: Billz certainly did
    not apply the request (it was never sent, or Billz answered with a refusal) — the sale push forgets a
    payment attempt only then.
    """

    code = "provider_unavailable"

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        error_code: int | None = None,
        not_applied: bool = False,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.error_code = error_code
        self.not_applied = not_applied


class BillzUnavailable(BillzError):
    """Transient: transport error, timeout, 429, 5xx, an ambiguous async answer or a token problem.

    The request may be retried later; a write whose outcome is unknown is re-checked before it is repeated.
    """


class BillzOutOfTime(BillzUnavailable):
    """The call's own budget ran out, not Billz: the deadline passed, the limiter's wait (our shared rate
    limit, or Billz's 429 pause) was used up, or a read was cut short by the deadline. ``read_stock`` reports
    the barcodes it did not reach :attr:`StockRead.deferred` rather than unavailable (review: a healthy but
    slow or rate-limited Billz must not refuse a checkout). ``cut_short``: a request was in flight when the
    deadline cut it (Billz had not answered yet), as opposed to a wait that never sent anything."""

    cut_short = False


class BillzMisconfigured(BillzUnavailable):
    """Billz refuses for a reason only a configuration fix can cure (decision D3): a redirect, 403, a route
    that does not exist, a cash box of another shop (20009), a seller without access (20085), a refused key.

    Retryable — every sale would otherwise die at once and nothing could replay it — and logged at ERROR so
    monitoring alerts; once the setting is fixed, ``manage.py requeue_outbox`` replays what died meanwhile.
    """


class BillzShiftClosed(BillzUnavailable):
    """The shift or the cash box is closed (20010, 20082, 20033): the sale waits for the next shift (D2)."""


class BillzPaymentPending(BillzUnavailable):
    """A payment of this sale was sent and its outcome is still unknown: look again after ``retry_after``,
    never pay again meanwhile (review B2)."""

    def __init__(self, message: str, *, retry_after: timedelta) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class BillzSaleBusy(BillzUnavailable):
    """Another worker holds this sale's lock (it is pushing the sale now): look again after ``retry_after``
    without spending an attempt (review NEW-9). Nothing was sent."""

    def __init__(self, message: str, *, retry_after: timedelta) -> None:
        super().__init__(message, not_applied=True)
        self.retry_after = retry_after


class BillzRejected(BillzError):
    """Billz refused the request for good (HTTP 4xx or a business ``error_code``): retrying cannot help."""

    def __init__(self, reason: str, *, error_code: int | None = None, http_status: int | None = None) -> None:
        self.reason = reason
        suffix = f" (Billz error {error_code})" if error_code is not None else ""
        status = f" [HTTP {http_status}]" if http_status is not None else ""
        super().__init__(
            f"{reason}{suffix}{status}", http_status=http_status, error_code=error_code, not_applied=True
        )


class BillzCheckByHand(BillzRejected):
    """The adapter stopped the sale push on purpose: staff must check the Billz sale by hand (a bill that is
    not what the customer paid, decision D1; a payment whose outcome stayed unknown, review B2)."""


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
    """A paid online order to record in Billz (``without_cashback``: cashback lives in Glow Derma only).

    The sale is recorded at what the customer was charged for the goods: Σ ``unit_price`` × ``quantity``
    (decision D1); the cashback spent and the delivery fee stay outside Billz.
    """

    order_id: int  # Glow Derma order number: the payment comment
    order_uuid: UUID  # ``Purchase.billz_order_uuid``: our correlation id (logs, the fake's echo)
    lines: Sequence[SaleLine]
    customer_phone: str  # +998… of the customer (loyalty identity); "" = no Billz client is linked


@dataclass(frozen=True, slots=True)
class SaleProgress:
    """What the caller persisted about the sale so far (:meth:`SaleJournal.progress`)."""

    billz_order_id: str = ""  # the Billz sale an earlier attempt created ("" = none yet)
    billz_sale_ref: str = ""
    payment_attempted_at: datetime | None = None  # a payment request whose outcome is not known yet
    pushed: bool = False  # another attempt finished the push already


@dataclass(frozen=True, slots=True)
class SaleReceipt:
    """The recorded sale: ``billz_order_id`` names it in the order history (``billz_sale_recorded``)."""

    billz_order_id: str  # Billz's sale UUID (the fake echoes ``order_uuid``)
    billz_sale_id: str  # stored on the purchase ("" when Billz created nothing, i.e. the fake)
    billz_sale_ref: str  # short number the cashier sees (``order_number``), ≤ 32 characters, or ""


class SaleCancelled(Exception):
    """Raised by :meth:`SaleJournal.before_payment`: the order was cancelled meanwhile, nothing is paid."""


class SaleJournal(Protocol):
    """Where the caller persists the progress of a sale push (design "Billz sale"; every write committed)."""

    def progress(self) -> SaleProgress:
        """The persisted progress, read under the adapter's sale lock (never a copy read before it)."""
        ...

    def order_created(self, billz_order_id: str, billz_sale_ref: str) -> None:
        """Persist (and commit) the id; raising stops the push before any item is sent."""
        ...

    def before_payment(self) -> None:
        """Right before the payment request: re-check the order (raise :class:`SaleCancelled` to stop) and
        persist (commit) the payment attempt, so a lost answer is never followed by a second payment."""
        ...

    def payment_not_applied(self) -> None:
        """Billz certainly did not take the payment (never sent, or refused): forget the attempt."""
        ...


class BillzPort(Protocol):
    def is_configured(self) -> bool:
        """``True`` when the adapter can reach Billz (the fake always can)."""
        ...

    def get_product(self, billz_id: str) -> BillzProduct | None:
        """One product by Billz id or barcode; ``None`` when Billz does not know it."""
        ...

    def read_stock(
        self, billz_ids: Sequence[str], *, budget_seconds: float, interactive: bool = True
    ) -> StockRead:
        """The current Billz row of each barcode, in the given order, all within ``budget_seconds``
        (``interactive``: the customer-request client profile, else the batch one). Never raises for a Billz
        failure: the barcodes it could not read are :attr:`StockRead.unavailable` (module doc)."""
        ...

    def iter_products(self, mode: ImportMode) -> Iterator[BillzProduct]:
        """Every product of a ``delta`` (changed since the last sync) or ``full`` import (module doc)."""
        ...

    def sync_succeeded(self, mode: ImportMode) -> None:
        """Every row of the last ``iter_products(mode)`` was applied: advance the sync cursor."""
        ...

    def push_sale(self, request: SaleRequest, journal: SaleJournal) -> SaleReceipt:
        """Record the paid online order as a Billz sale, resuming the sale ``journal.progress()`` names."""
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
WEBHOOKS_NOT_SUPPORTED = (
    "Billz sends no webhooks; the catalog is polled (a delta every BILLZ_DELTA_SYNC_SECONDS, a daily full "
    "sync) and stale cart lines are re-read at cart writes and checkout"
)


def refuse_refund(billz_order_id: str) -> None:
    raise BillzNotSupported("refunds of paid sales", REFUND_NOT_SUPPORTED)


def refuse_store_sale(receipt_ref: str) -> None:
    raise BillzNotSupported("in-store sale discovery", STORE_SALE_NOT_SUPPORTED)


def refuse_webhook(body: bytes) -> None:
    raise BillzNotSupported("webhooks", WEBHOOKS_NOT_SUPPORTED)
