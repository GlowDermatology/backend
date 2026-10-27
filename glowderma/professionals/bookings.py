"""How many open future appointments each specialist has, answered by ``appointments`` (FND-1).

The admin API of ``professionals`` shows the figure on every specialist and refuses to deactivate one that
still has ``requested`` / ``confirmed`` appointments ahead (409 ``conflict`` unless ``force``). Only
``appointments`` can count them and ``professionals`` may not import it (layers), so ``appointments``
registers the counter at start-up (``AppointmentsConfig.ready``), like ``loyalty.hold_grace``.

Without a registration the answer is unknown, and an unknown answer must never let a deactivation through
silently: :func:`upcoming_counts` raises ``ImproperlyConfigured`` (``appointments`` is always installed, so
this only catches a broken settings module).
"""

from collections.abc import Callable, Collection
from datetime import datetime

from django.core.exceptions import ImproperlyConfigured

type UpcomingCounter = Callable[[Collection[int], datetime], dict[int, int]]

_counter: UpcomingCounter | None = None


def register(counter: UpcomingCounter) -> None:
    global _counter  # noqa: PLW0603 - one process-wide registration, made once at app start-up
    _counter = counter


def upcoming_counts(professional_ids: Collection[int], instant: datetime) -> dict[int, int]:
    """``{professional_id: n}`` of the ``requested`` / ``confirmed`` appointments starting at or after
    ``instant``; every id is present (``0`` when none)."""
    ids = list(professional_ids)
    if not ids:
        return {}
    if _counter is None:
        raise ImproperlyConfigured("appointments did not register the upcoming-appointments counter")
    counts = _counter(ids, instant)
    return {pk: int(counts.get(pk, 0)) for pk in ids}
