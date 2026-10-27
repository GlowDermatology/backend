"""The Billz port as the catalog sees it: the ``BILLZ_MODE`` adapter over the catalog's own rows."""

from glowderma.catalog.selectors import CatalogKnownProducts
from glowderma.integrations.billz.ports import BillzPort
from glowderma.integrations.billz.registry import get_billz


def get_port() -> BillzPort:
    return get_billz(CatalogKnownProducts())
