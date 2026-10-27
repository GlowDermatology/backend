"""URL slugs for the public website (B-W1 SEO): Uzbek- and Cyrillic-safe transliteration.

``slugify_uz("Ko‘pikli yuvish geli 150 ml")`` → ``"kopikli-yuvish-geli-150-ml"``; ``slugify_uz("Сыворотка с
гиалуроновой кислотой")`` → ``"syvorotka-s-gialuronovoy-kislotoy"``. Uzbek Latin apostrophe letters (oʻ, gʻ
in every spelling: ʻ ‘ ’ ' `) lose the mark; Uzbek and Russian Cyrillic is transliterated to Latin (ш → sh,
ч → ch, ў → o, қ → q, ғ → g, ҳ → h, ё → yo, ю → yu, я → ya, ж → j, х → x, ц → ts); other accents are folded.
A slug is ``^[a-z0-9]+(-[a-z0-9]+)*$`` and never only digits (a digits-only path segment is an id).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from typing import Any

from django.db import IntegrityError, transaction

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
DIGITS_ONLY_PATTERN = r"^[0-9]+$"
SLUG_RE = re.compile(SLUG_PATTERN)

_APOSTROPHES = "'`´ʻʼʹʽ‘’‛′"
_CYRILLIC = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "j", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "x", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya", "ў": "o", "қ": "q", "ғ": "g", "ҳ": "h", "і": "i", "ї": "yi", "є": "ye",
}  # fmt: skip


def _transliterate(text: str) -> str:
    # NFKC first: compatibility forms (ligatures, full-width letters) unfold and a decomposed Cyrillic
    # й / ё composes again before the table sees it (NFKD here would turn й into и); then the rest is folded.
    out: list[str] = []
    for char in unicodedata.normalize("NFKC", text).lower().replace("ß", "ss"):
        if char in _APOSTROPHES:
            continue
        out.append(_CYRILLIC.get(char, char))
    folded = unicodedata.normalize("NFKD", "".join(out))
    return "".join(char for char in folded if not unicodedata.combining(char))


def slugify_uz(text: str, *, max_length: int = 80) -> str:
    """The slug of ``text`` (``""`` when nothing usable remains), cut at a word boundary to ``max_length``."""
    slug = re.sub(r"[^a-z0-9]+", "-", _transliterate(text or "")).strip("-")
    if len(slug) > max_length:
        cut = slug[:max_length]
        slug = cut.rsplit("-", 1)[0] if "-" in cut else cut
    return slug.strip("-")


def derive_slug(*sources: str, prefix: str, fallback: str = "", max_length: int) -> str:
    """The slug base of a new row: the first of ``sources`` that slugifies to something; a digits-only
    result (a barcode as the Billz name, a name like "2024" or "007") becomes ``"<prefix>-<digits>"`` — a
    digits-only path segment is an id; nothing usable → ``fallback`` (itself slugified the same way), else
    ``prefix``. Shared by the models' ``save`` and the backfill migrations, so both always give a slug the
    ``*_slug_format`` check accepts."""
    for source in (*sources, fallback):
        slug = slugify_uz(source or "", max_length=max_length)
        if not slug:
            continue
        if re.fullmatch(DIGITS_ONLY_PATTERN, slug):
            slug = f"{prefix}-{slug}"[:max_length]
        return slug
    return prefix


def is_valid_slug(value: str, *, max_length: int) -> bool:
    return (
        0 < len(value) <= max_length
        and SLUG_RE.fullmatch(value) is not None
        and re.fullmatch(DIGITS_ONLY_PATTERN, value) is None
    )


def candidate(base: str, attempt: int, *, max_length: int) -> str:
    """``base`` for the first attempt, then ``base-2``, ``base-3``, … (the suffix always fits)."""
    if attempt <= 1:
        return base[:max_length].strip("-")
    suffix = f"-{attempt}"
    return base[: max_length - len(suffix)].strip("-") + suffix


def next_free_slug(model: Any, base: str, *, max_length: int, exclude_pk: Any = None, skip: int = 0) -> str:
    """The first of ``base``, ``base-2``, ``base-3``, … that no other row of ``model`` holds (``skip`` free
    candidates are passed over: a retry after a lost race takes the next one)."""
    taken_rows = model._default_manager.filter(slug__startswith=base[: max_length - 4])
    if exclude_pk is not None:
        taken_rows = taken_rows.exclude(pk=exclude_pk)
    taken = set(taken_rows.values_list("slug", flat=True))
    attempt = 1
    while True:
        value = candidate(base, attempt, max_length=max_length)
        if value not in taken:
            if skip <= 0:
                return value
            skip -= 1
        attempt += 1


def save_with_unique_slug(
    instance: Any, save: Callable[[], None], *, base: str, constraint: str, max_length: int, attempts: int = 5
) -> None:
    """Give ``instance`` the first free slug derived from ``base`` and ``save()`` it in a savepoint; a
    concurrent row that took the same slug first (``constraint`` violated) makes it read the taken slugs
    again and retry (the unique violation is raised only once that row committed, so the next read sees
    it)."""
    for attempt in range(attempts):
        instance.slug = next_free_slug(type(instance), base, max_length=max_length, exclude_pk=instance.pk)
        try:
            with transaction.atomic():
                save()
        except IntegrityError as exc:
            name = getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None)
            if (name or str(exc)).find(constraint) < 0 or attempt == attempts - 1:
                raise
            continue
        return
