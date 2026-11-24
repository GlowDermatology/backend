"""What the admin ``professionals`` views share: the plain query dict and the specialist lookup."""

from __future__ import annotations

from rest_framework.exceptions import NotFound
from rest_framework.request import Request

from glowderma.professionals.models import (
    Professional,
)


def query_params(request: Request) -> dict[str, str]:
    # A plain dict: DRF reads a missing BooleanField of a QueryDict as ``False``.
    return request.query_params.dict()


def professional_or_404(pk: int) -> Professional:
    professional = Professional.objects.filter(pk=pk).first()
    if professional is None:
        raise NotFound
    return professional


__all__ = [
    "professional_or_404",
    "query_params",
]
