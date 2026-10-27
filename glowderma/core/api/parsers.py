"""Request parsers: JSON that refuses text Postgres and UTF-8 cannot store.

``json.loads`` turns an escaped lone surrogate (``"\\ud800"``) into a Python ``str`` that no UTF-8 encoder
accepts. Such a value would reach a column or a ``jsonb`` document and fail inside the database as a 500,
so the parser answers ``400 parse_error`` instead. Paired surrogates (emoji) decode to one code point and
pass; form and multipart bodies are decoded with replacement characters by Django and need no check.
"""

import re
from collections.abc import Mapping
from typing import Any

from rest_framework import parsers
from rest_framework.exceptions import ParseError

_SURROGATE = re.compile("[\ud800-\udfff]")


def has_lone_surrogate(value: object) -> bool:
    """``value`` (decoded JSON) holds a string or key with an unpaired UTF-16 surrogate anywhere."""
    stack: list[object] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _SURROGATE.search(item):
                return True
        elif isinstance(item, Mapping):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


class JSONParser(parsers.JSONParser):
    def parse(self, stream: Any, media_type: str | None = None, parser_context: Any = None) -> Any:
        data = super().parse(stream, media_type, parser_context)
        if has_lone_surrogate(data):
            raise ParseError("JSON parse error - unpaired surrogate")
        return data
