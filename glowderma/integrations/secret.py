"""``Secret``: a credential held in memory only (the AI assistant's API key read from Vault).

The value never shows in ``repr()``, ``str()``, f-strings, tracebacks' local-variable dumps of the object,
pickles or JSON: only :meth:`Secret.reveal` returns it, and only the adapter that sends it calls that.
Keeping a key in a ``Secret`` from the Vault read to the provider call means a log line, an exception
message, a Celery argument or an audit row that receives the object by mistake carries ``Secret(***)``,
never the key.
"""

from __future__ import annotations

from typing import Any, NoReturn

REDACTED = "Secret(***)"


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("Secret holds a string")
        self._value = value

    def reveal(self) -> str:
        """The raw value, for the one call that sends it."""
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash((Secret, self._value))

    def __repr__(self) -> str:
        return REDACTED

    __str__ = __repr__

    def __format__(self, format_spec: str) -> str:
        return REDACTED

    def __reduce__(self) -> NoReturn:
        raise TypeError("a Secret is never pickled (Celery arguments, caches)")

    def __getstate__(self) -> Any:
        raise TypeError("a Secret is never pickled (Celery arguments, caches)")
