"""The published terms a registration and a consent accept (customer contract §2.1.3, §2.2.4)."""

from __future__ import annotations

from glowderma.core.api.exceptions import (
    TermsNotPublished,
)
from glowderma.customers.services.common import passwords
from glowderma.site_config import selectors as site_config_selectors


def require_published_terms() -> site_config_selectors.Terms:
    terms = site_config_selectors.current_terms()
    if not terms.published:
        raise TermsNotPublished()
    return terms


def check_terms_version(terms_version: str, terms: site_config_selectors.Terms) -> None:
    if terms_version != terms.version:
        raise passwords.field_refusal("terms_version", "terms_outdated", "field.terms_outdated")


__all__ = [
    "check_terms_version",
    "require_published_terms",
]
