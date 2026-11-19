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
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
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
# reasons a suggestion is rejected (job-level warnings ``{"code": "rejected", "key", "reason"}``)
R_TOO_LONG = "too_long"
R_UNSUPPORTED_NUMBER = "unsupported_number"
R_UNSUPPORTED_SPF = "unsupported_spf"
R_EMPTY = "empty"

_KEY_RE = re.compile(
    r"^(?:texts\.(?P<tl>uz|ru|en)\.(?P<tf>[a-z_]+)"
    r"|seo\.(?P<sl>uz|ru|en)\.(?P<sf>[a-z]+)"
    r"|images\.(?P<img>[1-9][0-9]{0,18})\.alt\.(?P<al>uz|ru|en))$"
)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
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


@dataclass(frozen=True)
class Facts:
    """What the product data says (numbers, words, INCI names) — the lint's reference."""

    text: str
    numbers: frozenset[str]
    inci: frozenset[str]

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
        if any(_number(n) not in facts.numbers for n in _NUMBER_RE.findall(value)):
            verdict.rejected = R_UNSUPPORTED_NUMBER
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
    """The job's suggestion rows (in target order) and the job-level warnings (rejected keys)."""
    suggestions: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    from_photos = mode == enums.AssistantMode.PHOTOS
    for key in targets:
        if key not in values:
            continue
        value = normalize(key, values[key])
        current = str(snapshot.get(key) or "")
        if value == current:
            continue  # nothing to suggest
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
                "checked": not from_photos and W_UNSUPPORTED_CLAIM not in verdict.warnings,
                "max_length": max_length(key),
            }
        )
    return suggestions, warnings
