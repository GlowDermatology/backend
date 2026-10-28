"""The «SEO holati» checklist of the admin product and category editors (B-W1; admin contract §2.3, §2.4).

The website's search quality depends on what staff type in, so the server lists what weakens a page —
missing or untranslated texts, too short or too long titles and descriptions, no photo — in the order the
editor should fix them (errors, then warnings, then hints). Pure functions over already loaded rows: the
product's images come from the serializer's prefetch, the category's active product count from its
annotation, so the checklist adds no query.

Length bounds follow what search engines show: a title of 15–60 characters, a description of 50–160.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from glowderma.core import enums

LANGUAGES = ("uz", "ru", "en")
TITLE_MIN, TITLE_MAX = 15, 60
META_MIN, META_MAX = 50, 160
DESCRIPTION_MIN = 80  # a product description shorter than this says too little for a product page
LANGUAGE_NAMES = {"uz": "o‘zbekcha", "ru": "ruscha", "en": "inglizcha"}
SEVERITY_ORDER = {enums.SeoSeverity.ERROR: 0, enums.SeoSeverity.WARNING: 1, enums.SeoSeverity.INFO: 2}


@dataclass(frozen=True, slots=True)
class SeoIssue:
    code: str
    severity: str
    language: str | None
    message: str


def _issue(code: str, severity: str, message: str, language: str | None = None) -> SeoIssue:
    return SeoIssue(code=code, severity=severity, language=language, message=message)


def _text(obj: Any, field: str, language: str) -> str:
    return str(getattr(obj, f"{field}_{language}", "") or "").strip()


def _meta_checks(obj: Any) -> list[SeoIssue]:
    """``seo_title`` / ``seo_description`` per language: missing is a hint (the website builds one), out of
    bounds a warning."""
    issues: list[SeoIssue] = []
    for language in LANGUAGES:
        label = LANGUAGE_NAMES[language]
        title = _text(obj, "seo_title", language)
        description = _text(obj, "seo_description", language)
        if not title:
            issues.append(
                _issue(
                    enums.SeoIssueCode.SEO_TITLE_MISSING,
                    enums.SeoSeverity.INFO,
                    f"SEO sarlavha ({label}) yo‘q: sayt nomdan foydalanadi.",
                    language,
                )
            )
        elif len(title) < TITLE_MIN:
            issues.append(
                _issue(
                    enums.SeoIssueCode.SEO_TITLE_SHORT,
                    enums.SeoSeverity.WARNING,
                    f"SEO sarlavha ({label}) juda qisqa: kamida {TITLE_MIN} belgi yozing.",
                    language,
                )
            )
        elif len(title) > TITLE_MAX:
            issues.append(
                _issue(
                    enums.SeoIssueCode.SEO_TITLE_LONG,
                    enums.SeoSeverity.WARNING,
                    f"SEO sarlavha ({label}) {TITLE_MAX} belgidan uzun: qidiruvda kesiladi.",
                    language,
                )
            )
        if not description:
            issues.append(
                _issue(
                    enums.SeoIssueCode.SEO_DESCRIPTION_MISSING,
                    enums.SeoSeverity.INFO,
                    f"SEO tavsif ({label}) yo‘q: sayt uni tavsifdan yasaydi.",
                    language,
                )
            )
        elif len(description) < META_MIN:
            issues.append(
                _issue(
                    enums.SeoIssueCode.SEO_DESCRIPTION_SHORT,
                    enums.SeoSeverity.WARNING,
                    f"SEO tavsif ({label}) juda qisqa: kamida {META_MIN} belgi yozing.",
                    language,
                )
            )
        elif len(description) > META_MAX:
            issues.append(
                _issue(
                    enums.SeoIssueCode.SEO_DESCRIPTION_LONG,
                    enums.SeoSeverity.WARNING,
                    f"SEO tavsif ({label}) {META_MAX} belgidan uzun: qidiruvda kesiladi.",
                    language,
                )
            )
    return issues


def _sorted(issues: list[SeoIssue]) -> list[SeoIssue]:
    return sorted(issues, key=lambda issue: SEVERITY_ORDER.get(enums.SeoSeverity(issue.severity), 3))


def product_issues(product: Any, *, image_count: int) -> list[SeoIssue]:
    """The checklist of one product (``image_count`` from the loaded images)."""
    issues: list[SeoIssue] = []
    if not _text(product, "name", "uz"):
        issues.append(
            _issue(
                enums.SeoIssueCode.NAME_MISSING,
                enums.SeoSeverity.ERROR,
                "O‘zbekcha nom yo‘q: sahifa Billz nomi bilan chiqadi.",
                "uz",
            )
        )
    for language in ("ru", "en"):
        if not _text(product, "name", language):
            issues.append(
                _issue(
                    enums.SeoIssueCode.NAME_TRANSLATION_MISSING,
                    enums.SeoSeverity.WARNING,
                    f"Nomning {LANGUAGE_NAMES[language]} tarjimasi yo‘q.",
                    language,
                )
            )
    for language in LANGUAGES:
        description = _text(product, "description", language)
        label = LANGUAGE_NAMES[language]
        if not description:
            issues.append(
                _issue(
                    enums.SeoIssueCode.DESCRIPTION_MISSING,
                    enums.SeoSeverity.ERROR if language == "uz" else enums.SeoSeverity.WARNING,
                    f"Tavsif ({label}) yo‘q.",
                    language,
                )
            )
        elif len(description) < DESCRIPTION_MIN:
            issues.append(
                _issue(
                    enums.SeoIssueCode.DESCRIPTION_SHORT,
                    enums.SeoSeverity.WARNING,
                    f"Tavsif ({label}) juda qisqa: kamida {DESCRIPTION_MIN} belgi yozing.",
                    language,
                )
            )
    if image_count == 0:
        issues.append(
            _issue(enums.SeoIssueCode.IMAGES_MISSING, enums.SeoSeverity.ERROR, "Mahsulotda rasm yo‘q.")
        )
    elif not _text(product, "name", "uz"):
        issues.append(
            _issue(
                enums.SeoIssueCode.ALT_TEXT_FALLBACK,
                enums.SeoSeverity.WARNING,
                "Rasmlarning alt matni mahsulot nomidan olinadi: o‘zbekcha nomni kiriting.",
                "uz",
            )
        )
    if getattr(product, "category_id", None) is None:
        issues.append(
            _issue(
                enums.SeoIssueCode.CATEGORY_MISSING,
                enums.SeoSeverity.WARNING,
                "Kategoriya tanlanmagan: sahifa katalog yo‘lida ko‘rinmaydi.",
            )
        )
    issues.extend(_meta_checks(product))
    return _sorted(issues)


def category_issues(category: Any, *, active_products: int) -> list[SeoIssue]:
    """The checklist of one category (``active_products`` from the list annotation)."""
    issues: list[SeoIssue] = []
    for language in ("ru", "en"):
        if not _text(category, "name", language):
            issues.append(
                _issue(
                    enums.SeoIssueCode.NAME_TRANSLATION_MISSING,
                    enums.SeoSeverity.WARNING,
                    f"Nomning {LANGUAGE_NAMES[language]} tarjimasi yo‘q.",
                    language,
                )
            )
    if not getattr(getattr(category, "image", None), "name", ""):
        issues.append(
            _issue(
                enums.SeoIssueCode.IMAGE_MISSING,
                enums.SeoSeverity.WARNING,
                "Kategoriya rasmi yo‘q: ijtimoiy tarmoqlarda umumiy rasm chiqadi.",
            )
        )
    if active_products == 0:
        issues.append(
            _issue(
                enums.SeoIssueCode.NO_ACTIVE_PRODUCTS,
                enums.SeoSeverity.WARNING,
                "Kategoriyada faol mahsulot yo‘q: sahifa bo‘sh ko‘rinadi.",
            )
        )
    issues.extend(_meta_checks(category))
    return _sorted(issues)


def status_payload(issues: list[SeoIssue]) -> dict[str, Any]:
    """``seo_status``: ``{"ok": no error or warning, "issues": [...]}``."""
    return {
        "ok": not any(issue.severity != enums.SeoSeverity.INFO for issue in issues),
        "issues": [
            {
                "code": issue.code,
                "severity": issue.severity,
                "language": issue.language,
                "message": issue.message,
            }
            for issue in issues
        ],
    }
