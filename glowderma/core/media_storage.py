"""Which media names the database refers to, and whether a storage holds them all."""

from collections.abc import Iterator
from typing import Any

from django.apps import apps
from django.db import models

# Text columns that hold a media storage name (not a FileField): (app label, model, column).
TEXT_NAME_COLUMNS = (("commerce", "OrderItem", "image_url"),)  # "products/1/9f2c.webp" at checkout


def stored_names() -> Iterator[str]:
    """Every non-empty media name referenced by a ``FileField``/``ImageField`` or a text name column."""
    for model in apps.get_models():
        for field in model._meta.get_fields():
            if isinstance(field, models.FileField):
                yield from _values(model, field.name)
    for app_label, model_name, column in TEXT_NAME_COLUMNS:
        yield from _values(apps.get_model(app_label, model_name), column)


def _values(model: type[models.Model], column: str) -> Iterator[str]:
    for value in model._default_manager.exclude(**{column: ""}).values_list(column, flat=True).iterator():
        if value:
            yield str(value)


def missing_names(storage: Any) -> list[str]:
    """The referenced names ``storage`` does not hold, sorted and without duplicates."""
    return sorted(name for name in set(stored_names()) if not storage.exists(name))
