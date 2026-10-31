"""Helpers for Django-admin page tests: what a rendered change page actually shows.

Unfold's templates carry no ``field-<name>`` classes on read-only rows, so tests read the field names from
the response's ``adminform`` instead of grepping the markup.
"""

from typing import Any


def rendered_field_names(response: Any) -> set[str]:
    """Names of the fields (read-only or editable) laid out on a change page response."""
    names: set[str] = set()
    for fieldset in response.context_data["adminform"]:
        for line in fieldset:
            for field in line:
                inner = field.field
                names.add(str(inner["name"] if isinstance(inner, dict) else inner.name))
    return names
