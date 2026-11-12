"""Template tags of the ops console's changelists (``{% load ops_admin %}``).

``ops_applied_filters`` lists every field lookup a changelist is filtered by — the dashboard drill-downs pass
lookups no sidebar filter shows (``?status__in=pending,failed&available_at__lt=…``), so without this bar the
user could not tell why the list is short. Used by ``admin/ops/applied_filters.html``
(``ReadOnlyAdmin.list_before_template``).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from django import template
from django.contrib.admin import SimpleListFilter
from django.contrib.admin.utils import NotRelationField, get_model_from_relation
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models.constants import LOOKUP_SEP
from django.utils.dateparse import parse_datetime
from django.utils.text import capfirst
from django.utils.translation import gettext, ngettext
from django.utils.translation import gettext_lazy as _

from glowderma.core.timeutils import TASHKENT

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

register = template.Library()

# Changelist parameters that are not field lookups (search, ordering, page, popup, facets, …).
NOT_LOOKUPS = frozenset({"q", "o", "p", "e", "all", "_to_field", "_popup", "_changelist_filters", "_facets"})
OPERATORS: dict[str, StrOrPromise] = {
    "exact": ":",
    "iexact": ":",
    "in": ":",
    "gt": ">",
    "gte": "≥",
    "lt": "<",
    "lte": "≤",
    "contains": _("o‘z ichiga oladi:"),
    "icontains": _("o‘z ichiga oladi:"),
    "startswith": _("boshlanishi:"),
    "istartswith": _("boshlanishi:"),
}
SHOWN_VALUES = 5
TRUE_VALUES = ("1", "true", "True", "yes")


def _split(
    model: type[models.Model], param: str
) -> tuple[list[models.Field[Any, Any]], list[str], str] | None:
    """``(fields along the path, JSON keys after a JSON field, lookup)`` of ``param``; ``None`` when it is not
    a field lookup."""
    parts = param.split(LOOKUP_SEP)
    lookup = "exact"
    if len(parts) > 1 and parts[-1] in (*OPERATORS, "isnull"):
        lookup = parts.pop()
    fields: list[models.Field[Any, Any]] = []
    current: Any = model
    for index, part in enumerate(parts):
        try:
            field = current._meta.get_field(part)
        except FieldDoesNotExist, AttributeError:
            return None
        fields.append(field)
        if isinstance(field, models.JSONField):
            return fields, parts[index + 1 :], lookup
        if index < len(parts) - 1:
            try:
                current = get_model_from_relation(field)
            except NotRelationField:
                return None
    return (fields, [], lookup) if fields else None


def _text(field: models.Field[Any, Any], raw: str) -> str:
    choices = dict(field.flatchoices) if field.choices else {}
    if raw in choices:
        return str(choices[raw])
    if isinstance(field, models.BooleanField):
        return gettext("Ha") if raw in TRUE_VALUES else gettext("Yo‘q")
    if isinstance(field, models.DateTimeField):
        parsed = parse_datetime(raw)
        if isinstance(parsed, datetime):
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(TASHKENT)
            return parsed.strftime("%d.%m.%Y %H:%M")
    return raw


def _value(field: models.Field[Any, Any], lookup: str, raw: str) -> str:
    if lookup == "isnull":
        return gettext("bo‘sh") if raw in TRUE_VALUES else gettext("bo‘sh emas")
    if lookup != "in":
        return _text(field, raw)
    values = [value for value in raw.split(",") if value]
    shown = ", ".join(_text(field, value) for value in values[:SHOWN_VALUES])
    if len(values) > SHOWN_VALUES:
        shown += " " + ngettext("… (jami %(count)s ta)", "… (jami %(count)s ta)", len(values)) % {
            "count": len(values)
        }
    return shown


def _choice_filters(filter_specs: list[Any]) -> dict[str, Any]:
    """``{parameter: filter}`` of the sidebar's choice filters (``SimpleListFilter``): their own title and
    choice label name the parameter better than its field path."""
    found: dict[str, Any] = {}
    for spec in filter_specs:
        if isinstance(spec, SimpleListFilter):
            for parameter in spec.expected_parameters():
                found[str(parameter)] = spec
    return found


def applied_filters(
    model: type[models.Model], params: dict[str, Any], filter_specs: list[Any] | None = None
) -> list[dict[str, str]]:
    """The field lookups among ``params`` as ``{"label", "op", "value"}`` rows, in parameter order."""
    rows = []
    choice_filters = _choice_filters(filter_specs or [])
    for param, value in params.items():
        if param in NOT_LOOKUPS:
            continue
        if param in choice_filters:
            spec = choice_filters[param]
            raw = value[-1] if isinstance(value, list) else str(value)
            labels = {str(key): str(text) for key, text in spec.lookup_choices}
            rows.append({"label": capfirst(str(spec.title)), "op": ":", "value": labels.get(raw, raw)})
            continue
        found = _split(model, param)
        if found is None:
            continue  # a filter's own parameter (a date range's ``…_from``): the filter panel shows it
        fields, keys, lookup = found
        raw = value[-1] if isinstance(value, list) else str(value)
        label = " › ".join([*(capfirst(str(field.verbose_name)) for field in fields), *keys])
        rows.append(
            {
                "label": label,
                "op": "" if lookup == "isnull" else str(OPERATORS.get(lookup, lookup)),
                "value": _value(fields[-1], lookup, raw),
            }
        )
    return rows


@register.simple_tag
def ops_applied_filters(cl: Any) -> list[dict[str, str]]:
    """``{% ops_applied_filters cl as applied %}``: the changelist's field lookups (see the module doc)."""
    return applied_filters(cl.model, dict(cl.params), list(getattr(cl, "filter_specs", [])))


@register.simple_tag
def ops_result_count(cl: Any) -> str:
    """``{% ops_result_count cl %}``: the changelist's total, «jami 287 ta buyurtma» (in Uzbek a noun after
    a number stays singular). English and Russian give the total alone ("total: 287"): a model name
    cannot be declined after a number from a catalog entry."""
    count = cl.result_count
    return ngettext("jami %(count)s ta %(name)s", "jami %(count)s ta %(name)s", count) % {
        "count": count,
        "name": cl.opts.verbose_name,
    }
