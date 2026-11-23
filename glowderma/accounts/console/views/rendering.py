"""How the console's own pages render: the Unfold site context and a never-cached response."""

from __future__ import annotations

from typing import Any

from django.contrib import admin
from django.http import HttpRequest, HttpResponse
from django.template.response import TemplateResponse
from django.utils.cache import add_never_cache_headers


def console_context(request: HttpRequest, title: str, **extra: Any) -> dict[str, Any]:
    request.current_app = admin.site.name
    return {**admin.site.each_context(request), "title": title, "subtitle": None, **extra}


def render_step(
    request: HttpRequest, template: str, context: dict[str, Any], status: int = 200
) -> HttpResponse:
    response = TemplateResponse(request, template, context, status=status)
    add_never_cache_headers(response)
    return response


__all__ = [
    "console_context",
    "render_step",
]
