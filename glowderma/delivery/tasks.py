"""Beat tasks of `delivery` (customer contract §2.6, §2.14.3; Yandex Delivery plan §3.11)."""

from datetime import timedelta

from celery import shared_task  # type: ignore[import-untyped]

from glowderma.core import integration_modes, timeutils
from glowderma.delivery import selectors, services

# Unused checkout quotes are kept a day past their expiry (support can still see what a customer was shown).
QUOTE_RETENTION = timedelta(days=1)


@shared_task(name="delivery.refresh_bts_regions")  # type: ignore[untyped-decorator]
def refresh_bts_regions() -> int:
    """Refresh BTS's region list before it goes stale (beat every 6 h), so no customer request waits for the
    15+ BTS calls of a refresh; a failure keeps the cached list. Returns the number of regions cached (0 when
    nothing was fetched: delivery not offered, another worker fetching, or BTS down)."""
    if not integration_modes.delivery_available():
        return 0
    try:
        catalog = selectors.refresh_region_catalog(stale=selectors.cached_region_catalog())
    except Exception:  # noqa: BLE001 - a beat run must not fail loudly every 6 h; the next one retries
        selectors.logger.warning("bts.regions_refresh_failed", exc_info=True)
        return 0
    return len(catalog.regions) if catalog is not None else 0


@shared_task(name="delivery.purge_quotes")  # type: ignore[untyped-decorator]
def purge_quotes() -> int:
    """Delete the checkout quotes nobody used that expired more than a day ago (beat daily); a used quote
    stays with its order. Returns how many were deleted."""
    return services.purge_quotes(timeutils.now() - QUOTE_RETENTION)
