"""Strict checks of the model's answer and of the values a person applies.

* :func:`parse_answer` — the answer must be ``{"suggestions": [{"key", "value"}, …]}``; an item whose key is
  not one of this request's targets (or not a product key at all: slug, price, status …) or whose value is
  not a string is dropped and counted; a wrong overall shape is :class:`BadAnswer` (the job retries once).
* :func:`check` — one value: its length against the catalog's own limits (imported, never duplicated), the
  claims lint (a number, a percentage or an SPF the product data does not contain → **rejected**; a
  certification / medical / clinical term the data does not contain → **flagged**), an ingredient list whose
  INCI names differ from the product's → warning. Photos mode reads new facts from the packaging, so its
  numbers are not rejected; every photo suggestion is flagged "confirm against packaging" and starts
  unchecked.
* :func:`normalize` — trims, and writes the Uzbek o‘ / g‘ and the tutuq belgisi the way the codebase does
  (U+2018 after o / g, U+2019 elsewhere).
* :func:`is_placeholder` — test input such as ``Test``, ``asdf`` or ``...`` counts as an empty field (target
  selection) and is never shown to the model as a fact (the frontend implements the same rule).

A number the product data does not contain is rejected when it is a quantity (attached to ``%``, SPF or a
unit such as ml / мг) or larger than :data:`SMALL_COUNT_MAX`; a bare small count ("1–2 раза в день",
"24 soat") is only flagged ``unverified_number`` and starts unchecked. A suggestion equal to the internal
POS stock label (``billz_name``) is rejected (``pos_label``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from glowderma.catalog.services import (
    ALT_MAX_LENGTH,
    LANGUAGES,
    SEO_FIELDS,
    SEO_MAX_LENGTHS,
    TEXT_FIELDS,
    TEXT_MAX_LENGTHS,
)
from glowderma.core import enums

# soft SEO recommendations (catalog.seo uses the same ranges for its checklist)
SEO_TITLE_RECOMMENDED = 60
SEO_DESCRIPTION_RECOMMENDED = 160

# warning codes on a suggestion (the admin panel labels them)
W_UNSUPPORTED_CLAIM = "unsupported_claim"
W_INCI_MISMATCH = "inci_mismatch"
W_CONFIRM_PACKAGING = "confirm_packaging"
W_SEO_TITLE_LONG = "seo_title_long"
W_SEO_DESCRIPTION_LONG = "seo_description_long"
W_UNVERIFIED_NUMBER = "unverified_number"
# reasons a suggestion is rejected (job-level warnings ``{"code": "rejected", "key", "reason"}``)
R_TOO_LONG = "too_long"
R_UNSUPPORTED_NUMBER = "unsupported_number"
R_UNSUPPORTED_SPF = "unsupported_spf"
R_EMPTY = "empty"
R_POS_LABEL = "pos_label"
# a bare integer up to this the data does not contain is a count ("1–2 times a day"), only flagged
SMALL_COUNT_MAX = 24
SKIP_REASON_MAX = 200

_KEY_RE = re.compile(
    r"^(?:texts\.(?P<tl>uz|ru|en)\.(?P<tf>[a-z_]+)"
    r"|seo\.(?P<sl>uz|ru|en)\.(?P<sf>[a-z]+)"
    r"|images\.(?P<img>[1-9][0-9]{0,18})\.alt\.(?P<al>uz|ru|en))$"
)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
# a unit right after a number makes it a quantity (never a harmless count)
_UNIT_AFTER_RE = re.compile(
    r"\s*(?:%|‰|(?:spf|спф|ml|мл|mg|мг|mcg|мкг|µg|g|gr|г|гр|kg|кг|l|л|iu|ме|oz|ppm|ph)(?!\w))",
    re.IGNORECASE,
)
_UNIT_BEFORE_RE = re.compile(r"(?<!\w)(?:spf|спф|ph)\s*$", re.IGNORECASE)
_PLACEHOLDER_RE = re.compile(
    r"^(?:test|тест|test\d*|тест\d*|asd+f*|qwe+r*t*y*|lorem ipsum.*|xxx+|todo|tbd|placeholder|sample|example"
    r"|пример|образец|namuna|\.+|-+|\?+)$"
)
_NON_WORD_RE = re.compile(r"[\W_]+")
_SPF_RE = re.compile(r"\bspf\b|\bспф\b", re.IGNORECASE)
_INCI_TOKEN_RE = re.compile(r"\b[A-Z][A-Za-z0-9-]{2,}\b")
_UZ_OKINA_RE = re.compile(r"([oOgG])['`\u2019\u02bb\u02bc]")
_UZ_TUTUQ_RE = re.compile(r"(?<=\w)(?<![oOgG])['`\u2018\u02bb\u02bc](?=\w)")

# Certification, clinical and medical terms (stems, lower case) per language: flagged unless the product data
# already says them. Kept short and conservative: the warning asks the person to verify, nothing more.
CLAIM_TERMS: tuple[str, ...] = (
    # en
    "organic", "vegan", "halal", "cruelty-free", "cruelty free", "dermatologist", "dermatologically",
    "hypoallergenic", "clinical", "clinically", "proven", "certified", "cures", "heals", "treats",
    "treatment", "therapeutic", "medical", "eczema", "psoriasis", "dermatitis", "rosacea", "fda",
    # ru
    "органическ", "веган", "халяль", "халял", "дерматолог", "гипоаллерген", "клиническ", "доказан",
    "сертифицир", "лечит", "лечени", "излечива", "вылечива", "терапевтическ", "медицинск", "экзем",
    "псориаз", "дерматит", "розаце",
    # uz
    "organik", "halol", "dermatolog", "gipoallergen", "klinik", "isbotlangan", "sertifikat",
    "davolaydi", "davolash", "shifo", "terapevtik", "tibbiy", "ekzema", "psoriaz", "dermatit", "rozatsea",
)  # fmt: skip


class BadAnswer(ValueError):
    """The answer does not have the schema's shape (the job retries once, then fails ``ai_bad_output``)."""


def parse_key(key: str) -> tuple[str, str, str, int | None] | None:
    """``(kind, language, field, image_id)`` of a product key the assistant may write, else ``None``.

    ``kind`` is ``texts`` / ``seo`` / ``images`` (field ``alt``). Slug, price, status, category, Billz ids,
    volume and stock are not in this vocabulary, so nothing can ever target them."""
    match = _KEY_RE.match(key) if isinstance(key, str) else None
    if match is None:
        return None
    if match["tl"]:
        return ("texts", match["tl"], match["tf"], None) if match["tf"] in TEXT_FIELDS else None
    if match["sl"]:
        return ("seo", match["sl"], match["sf"], None) if match["sf"] in SEO_FIELDS else None
    return "images", match["al"], "alt", int(match["img"])


def max_length(key: str) -> int:
    parsed = parse_key(key)
    if parsed is None:
        raise ValueError(f"not an assistant key: {key!r}")
    kind, _language, name, _image = parsed
    if kind == "texts":
        return TEXT_MAX_LENGTHS[name]
    if kind == "seo":
        return SEO_MAX_LENGTHS[name]
    return ALT_MAX_LENGTH


def normalize_uzbek(text: str) -> str:
    """o‘ / g‘ with U+2018, the tutuq belgisi (between letters) with U+2019."""
    return _UZ_TUTUQ_RE.sub("\u2019", _UZ_OKINA_RE.sub("\\1\u2018", text))


def normalize(key: str, value: str) -> str:
    value = value.strip()
    parsed = parse_key(key)
    if parsed is not None and parsed[1] == "uz":
        value = normalize_uzbek(value)
    return value


def _number(token: str) -> str:
    return token.replace(",", ".")


def collapse(value: Any) -> str:
    """Trimmed, case-folded text with whitespace runs collapsed to one space (comparison form)."""
    return " ".join(str(value or "").split()).casefold()


def _bare(value: Any) -> str:
    """Case-folded letters and digits only: ``New toner, 200 ml`` and ``NEW TONER 200ML`` compare equal."""
    return _NON_WORD_RE.sub("", str(value or "").casefold())


def is_placeholder(value: Any) -> bool:
    """Empty, without a letter, or test input (``Test``, ``asdf``, ``lorem ipsum…``, ``...``): such a value
    counts as an empty field and is never a fact. The frontend implements the same rule."""
    text = collapse(value)
    if not text or not any(char.isalpha() for char in text):
        return True
    return _PLACEHOLDER_RE.match(text) is not None


def _quantity(value: str, match: re.Match[str]) -> bool:
    """The number at ``match`` is a quantity: a decimal, attached to a unit / %, or preceded by SPF / pH."""
    token = match.group()
    if not token.isdigit():
        return True
    return bool(_UNIT_AFTER_RE.match(value, match.end()) or _UNIT_BEFORE_RE.search(value, 0, match.start()))


@dataclass(frozen=True)
class Facts:
    """What the product data says (numbers, words, INCI names) — the lint's reference."""

    text: str
    numbers: frozenset[str]
    inci: frozenset[str]
    pos_label: str = ""  # the POS stock label (``billz_name``), :func:`_bare`: never a customer text

    @classmethod
    def of(cls, data: Mapping[str, Any]) -> Facts:
        text = json.dumps(data, ensure_ascii=False).lower()
        texts = data.get("texts") or {}
        ingredients = " ".join(
            str((texts.get(language) or {}).get("ingredients") or "") for language in LANGUAGES
        )
        return cls(
            text=text,
            numbers=frozenset(_number(n) for n in _NUMBER_RE.findall(text)),
            inci=frozenset(t.lower() for t in _INCI_TOKEN_RE.findall(ingredients)),
            pos_label=_bare(data.get("pos_stock_label")),
        )


@dataclass
class Verdict:
    rejected: str | None = None
    warnings: list[str] = field(default_factory=list)


def check(key: str, value: str, facts: Facts, *, mode: str) -> Verdict:
    """The verdict on one (normalised) value."""
    verdict = Verdict()
    if not value:
        verdict.rejected = R_EMPTY
        return verdict
    if len(value) > max_length(key):
        verdict.rejected = R_TOO_LONG
        return verdict
    lowered = value.lower()
    from_photos = mode == enums.AssistantMode.PHOTOS
    if not from_photos:
        if _SPF_RE.search(value) and not _SPF_RE.search(facts.text):
            verdict.rejected = R_UNSUPPORTED_SPF
            return verdict
        unknown = [m for m in _NUMBER_RE.finditer(value) if _number(m.group()) not in facts.numbers]
        if any(_quantity(value, m) or int(m.group()) > SMALL_COUNT_MAX for m in unknown):
            verdict.rejected = R_UNSUPPORTED_NUMBER
            return verdict
        if unknown:
            verdict.warnings.append(W_UNVERIFIED_NUMBER)
    if facts.pos_label and _bare(value) == facts.pos_label:
        verdict.rejected = R_POS_LABEL
        return verdict
    if any(term in lowered and term not in facts.text for term in CLAIM_TERMS):
        verdict.warnings.append(W_UNSUPPORTED_CLAIM)
    _kind, _language, name, _image = parse_key(key) or ("", "", "", None)
    if name == "ingredients" and facts.inci:
        if {t.lower() for t in _INCI_TOKEN_RE.findall(value)} - facts.inci:
            verdict.warnings.append(W_INCI_MISMATCH)
    if key.startswith("seo."):
        if name == "title" and len(value) > SEO_TITLE_RECOMMENDED:
            verdict.warnings.append(W_SEO_TITLE_LONG)
        if name == "description" and len(value) > SEO_DESCRIPTION_RECOMMENDED:
            verdict.warnings.append(W_SEO_DESCRIPTION_LONG)
    if from_photos:
        verdict.warnings.append(W_CONFIRM_PACKAGING)
    return verdict


def parse_answer(data: Any, targets: Sequence[str]) -> tuple[dict[str, str], int]:
    """``{key: raw value}`` of the answer's valid items (first one per key wins) and the dropped count."""
    if not isinstance(data, dict) or not isinstance(data.get("suggestions"), list):
        raise BadAnswer("the answer has no suggestions list")
    allowed = set(targets)
    values: dict[str, str] = {}
    dropped = 0
    for item in data["suggestions"]:
        key = item.get("key") if isinstance(item, dict) else None
        value = item.get("value") if isinstance(item, dict) else None
        if not isinstance(key, str) or key not in allowed or not isinstance(value, str) or key in values:
            dropped += 1
            continue
        values[key] = value
    return values, dropped


def parse_skipped(data: Any, targets: Sequence[str], answered: Iterable[str]) -> list[dict[str, str]]:
    """The answer's ``skipped`` items as job warnings ``{"code": "skipped", "key", "reason"}``: one per
    requested key the answer did not also suggest (first one wins); anything else is ignored."""
    items = data.get("skipped") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    allowed = set(targets) - set(answered)
    seen: set[str] = set()
    skipped: list[dict[str, str]] = []
    for item in items:
        key = item.get("key") if isinstance(item, dict) else None
        reason = item.get("reason") if isinstance(item, dict) else None
        if not isinstance(key, str) or key not in allowed or key in seen:
            continue
        seen.add(key)
        text = " ".join(reason.split()) if isinstance(reason, str) else ""
        skipped.append({"code": "skipped", "key": key, "reason": text[:SKIP_REASON_MAX]})
    return skipped


def extracted_info(data: Any) -> dict[str, str]:
    """Photos mode: the brand / volume the model read from the packaging (information only, never applied)."""
    extracted = data.get("extracted") if isinstance(data, dict) else None
    if not isinstance(extracted, dict):
        return {}
    info: dict[str, str] = {}
    for name in ("brand", "volume"):
        value = extracted.get(name)
        if isinstance(value, str) and value.strip():
            info[name] = value.strip()[:120]
    return info


def build_suggestions(
    values: Mapping[str, str],
    *,
    targets: Sequence[str],
    snapshot: Mapping[str, Any],
    facts: Facts,
    mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The job's suggestion rows (in target order) and the job-level warnings (rejected keys, and keys whose
    suggestion equals the current value: ``unchanged``). ``snapshot`` holds the working values the person
    sees (their unsaved draft over the saved product)."""
    suggestions: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    from_photos = mode == enums.AssistantMode.PHOTOS
    for key in targets:
        if key not in values:
            continue
        value = normalize(key, values[key])
        current = str(snapshot.get(key) or "")
        if value == normalize(key, current):
            warnings.append({"code": "unchanged", "key": key})  # nothing to suggest
            continue
        verdict = check(key, value, facts, mode=mode)
        if verdict.rejected:
            warnings.append({"code": "rejected", "key": key, "reason": verdict.rejected})
            continue
        suggestions.append(
            {
                "key": key,
                "current": current,
                "suggested": value,
                "warnings": verdict.warnings,
                "origin": "image" if from_photos else "ai",
                "checked": not from_photos
                and W_UNSUPPORTED_CLAIM not in verdict.warnings
                and W_UNVERIFIED_NUMBER not in verdict.warnings,
                "max_length": max_length(key),
            }
        )
    return suggestions, warnings
