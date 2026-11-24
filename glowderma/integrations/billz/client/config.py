"""The live Billz configuration and the call profiles (batch and interactive) that bound each call."""

from __future__ import annotations

from dataclasses import dataclass, field

from django.conf import settings


@dataclass(frozen=True, slots=True)
class CallProfile:
    """How long one call may wait and how often it is retried (module doc, review B5)."""

    name: str
    max_attempts: int  # transport errors / 5xx of repeatable calls, attempts in total
    max_throttled: int  # 429 answers tolerated per call (0: fail at the first)
    limiter_wait: float  # how far ahead the limiter may queue a request
    read_timeout_cap: float | None  # caps BILLZ_READ_TIMEOUT_SECONDS
    renew_wait: float  # how long to wait for another worker's token renewal
    wait_on_pause: bool  # False: a 429 pause in force fails fast instead of sleeping
    call_budget: float | None  # seconds one call may take in total (None: the active deadline only)


BATCH = CallProfile(
    name="batch",
    max_attempts=4,
    max_throttled=5,
    limiter_wait=60.0,
    read_timeout_cap=None,
    renew_wait=45.0,
    wait_on_pause=True,
    call_budget=None,
)


INTERACTIVE = CallProfile(
    name="interactive",
    max_attempts=2,
    max_throttled=0,
    limiter_wait=3.0,
    read_timeout_cap=10.0,
    renew_wait=5.0,
    wait_on_pause=False,
    call_budget=25.0,
)


@dataclass(frozen=True, slots=True)
class BillzConfig:
    """The live client's settings (``BILLZ_*``); the key is kept out of ``repr``."""

    base_url: str
    secret_key: str = field(repr=False)
    platform_id: str
    shop_id: str
    cashbox_id: str
    payment_type_id: str
    rate_limit_per_second: float = 1.5
    connect_timeout: float = 5.0
    read_timeout: float = 30.0
    page_size: int = 300
    full_sync_min_ratio: float = 0.8
    volume_field: str = ""

    @property
    def configured(self) -> bool:
        """Everything a sync and a sale push need is set (production refuses to start otherwise)."""
        return all((self.secret_key, self.shop_id, self.cashbox_id, self.payment_type_id))

    @classmethod
    def from_settings(cls) -> BillzConfig:
        return cls(
            base_url=str(settings.BILLZ_BASE_URL).strip().rstrip("/"),
            secret_key=str(settings.BILLZ_SECRET_KEY).strip(),
            platform_id=str(settings.BILLZ_PLATFORM_ID).strip(),
            shop_id=str(settings.BILLZ_SHOP_ID).strip(),
            cashbox_id=str(settings.BILLZ_CASHBOX_ID).strip(),
            payment_type_id=str(settings.BILLZ_PAYMENT_TYPE_ID).strip(),
            rate_limit_per_second=float(settings.BILLZ_RATE_LIMIT_PER_SECOND),
            connect_timeout=float(settings.BILLZ_CONNECT_TIMEOUT_SECONDS),
            read_timeout=float(settings.BILLZ_READ_TIMEOUT_SECONDS),
            page_size=int(settings.BILLZ_PAGE_SIZE),
            full_sync_min_ratio=float(settings.BILLZ_FULL_SYNC_MIN_RATIO),
            volume_field=str(settings.BILLZ_VOLUME_FIELD).strip(),
        )


__all__ = [
    "BATCH",
    "INTERACTIVE",
    "BillzConfig",
    "CallProfile",
]
