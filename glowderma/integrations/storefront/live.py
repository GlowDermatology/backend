"""The live storefront adapter: ``POST {STOREFRONT_REVALIDATE_URL}`` with ``Authorization: Bearer
{STOREFRONT_REVALIDATE_SECRET}`` and ``{"tags": [...]}`` (frontend ``src/app/api/revalidate/route.ts``).

Statuses: 2xx done; 400 (an unknown tag), 401 (a wrong secret) and 404 (the website has no secret
configured, so the route is off) are :class:`~.ports.StorefrontRefused`; anything else, and a transport
failure, is transient. The secret never reaches a log line (``httpclient`` logs the path only).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import httpx
from django.conf import settings

from glowderma.integrations import httpclient
from glowderma.integrations.storefront.ports import StorefrontRefused

PROVIDER = "storefront"
DEADLINE_SECONDS = 10.0
REFUSED_STATUSES = frozenset({400, 401, 404})


@dataclass(frozen=True, slots=True)
class LiveStorefront:
    url: str
    secret: str
    transport: httpx.BaseTransport | None = None  # tests: httpx.MockTransport

    @classmethod
    def from_settings(cls) -> LiveStorefront:
        return cls(url=settings.STOREFRONT_REVALIDATE_URL, secret=settings.STOREFRONT_REVALIDATE_SECRET)

    def revalidate(self, tags: Sequence[str]) -> None:
        with httpclient.new_client(self.transport) as http:
            answer = httpclient.send(
                http,
                "POST",
                self.url,
                provider=PROVIDER,
                deadline=httpclient.Deadline(DEADLINE_SECONDS),
                json={"tags": list(tags)},
                headers={"Authorization": f"Bearer {self.secret}"},
            )
        if answer.ok:
            return
        code = httpclient.safe_text(answer.json_object().get("code", ""), 40)
        if answer.status in REFUSED_STATUSES:
            raise StorefrontRefused(f"HTTP {answer.status} {code}".strip())
        raise RuntimeError(f"storefront answered HTTP {answer.status} {code}".strip())


__all__ = [
    "LiveStorefront",
]
