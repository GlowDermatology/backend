"""Answer parsing, the claims lint, the length limits and the Uzbek apostrophe normaliser."""

from __future__ import annotations

import pytest

from glowderma.assistant import validation
from glowderma.assistant.validation import Facts
from glowderma.catalog.services import ALT_MAX_LENGTH, SEO_MAX_LENGTHS, TEXT_MAX_LENGTHS
from glowderma.core import enums

FILL = enums.AssistantMode.FILL_MISSING
PHOTOS = enums.AssistantMode.PHOTOS


def facts(**texts: str) -> Facts:
    """Facts of a product whose uz texts are ``texts`` (``volume`` 30 ml)."""
    return Facts.of({"volume": "30 ml", "texts": {"uz": texts, "ru": {}, "en": {}}})


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("texts.uz.name", ("texts", "uz", "name", None)),
        ("texts.ru.short_description", ("texts", "ru", "short_description", None)),
        ("seo.en.description", ("seo", "en", "description", None)),
        ("images.12.alt.ru", ("images", "ru", "alt", 12)),
    ],
)
def test_parse_key_accepts_the_assistant_vocabulary(key: str, expected: tuple[object, ...]) -> None:
    assert validation.parse_key(key) == expected


@pytest.mark.parametrize(
    "key",
    ["slug", "price", "status", "category", "billz_id", "volume", "texts.uz.slug", "seo.uz.keywords",
     "texts.de.name", "images.0.alt.uz", "images.x.alt.uz", "texts.uz.name.extra", "", None],
)  # fmt: skip
def test_parse_key_refuses_everything_else(key: object) -> None:
    assert validation.parse_key(key) is None  # type: ignore[arg-type]


def test_max_length_comes_from_the_catalog_limits() -> None:
    assert validation.max_length("texts.uz.description") == TEXT_MAX_LENGTHS["description"]
    assert validation.max_length("texts.ru.short_description") == TEXT_MAX_LENGTHS["short_description"]
    assert validation.max_length("seo.en.title") == SEO_MAX_LENGTHS["title"]
    assert validation.max_length("images.3.alt.uz") == ALT_MAX_LENGTH
    with pytest.raises(ValueError):
        validation.max_length("slug")


@pytest.mark.parametrize(
    ("raw", "normalised"),
    [
        ("Yog'li teri uchun o'simlik", "Yog‘li teri uchun o‘simlik"),
        ("O`zbek gʻalla", "O‘zbek g‘alla"),
        ("ma'lumot, san'at", "ma’lumot, san’at"),
        ("mo‘jiza", "mo‘jiza"),
    ],
)
def test_uzbek_apostrophes_are_normalised(raw: str, normalised: str) -> None:
    assert validation.normalize("texts.uz.description", f"  {raw} ") == normalised


def test_other_languages_are_only_trimmed() -> None:
    assert validation.normalize("texts.en.description", " It's good ") == "It's good"


def test_unsupported_number_and_percentage_are_rejected() -> None:
    data = facts(description="Gialuron kislotali serum, 30 ml")
    assert validation.check("texts.ru.description", "Сыворотка 30 мл", data, mode=FILL).rejected is None
    assert validation.check("texts.ru.description", "5% ниацинамида", data, mode=FILL).rejected == (
        validation.R_UNSUPPORTED_NUMBER
    )
    assert validation.check("texts.en.description", "Lasts 48 hours", data, mode=FILL).rejected == (
        validation.R_UNSUPPORTED_NUMBER
    )


@pytest.mark.parametrize(
    "text",
    ["Наносите 1–2 раза в день", "24 soat davomida namlaydi", "Apply 2 times a day", "Шаг 3"],
)
def test_a_small_bare_count_is_only_flagged(text: str) -> None:
    verdict = validation.check("texts.ru.usage", text, facts(), mode=FILL)
    assert verdict.rejected is None
    assert verdict.warnings == [validation.W_UNVERIFIED_NUMBER]


@pytest.mark.parametrize(
    "text",
    ["5 мл на лицо", "2% кислоты", "10mg", "3 g", "pH 5", "Lasts 25 hours", "0.5 часа", "1,5 раза"],
)
def test_quantities_and_bigger_numbers_stay_rejected(text: str) -> None:
    verdict = validation.check("texts.en.usage", text, facts(), mode=FILL)
    assert verdict.rejected == validation.R_UNSUPPORTED_NUMBER


def test_an_spf_value_the_data_does_not_state_is_rejected_even_when_small() -> None:
    sunscreen = facts(description="Quyoshdan himoya SPF 50")
    verdict = validation.check("texts.en.description", "SPF 15 sunscreen", sunscreen, mode=FILL)
    assert verdict.rejected == validation.R_UNSUPPORTED_NUMBER


def test_a_known_number_is_not_flagged() -> None:
    verdict = validation.check("texts.ru.description", "Объём 30 мл", facts(), mode=FILL)
    assert verdict.rejected is None and verdict.warnings == []


@pytest.mark.parametrize(
    "value",
    ["", "   ", None, "Test", " TEST ", "test123", "Тест", "тест2", "asdf", "asddff", "qwerty", "qwe",
     "Lorem ipsum dolor sit amet", "xxx", "XXXX", "todo", "TBD", "placeholder", "Sample", "example",
     "пример", "Образец", "namuna", "...", "---", "???", "123", "1 2 3", "!!!", "—"],
)  # fmt: skip
def test_placeholders(value: object) -> None:
    assert validation.is_placeholder(value) is True


@pytest.mark.parametrize(
    "value",
    ["Krem", "Test cream", "Testosterone-free", "Сыворотка", "Glow 30 ml", "lorem", "x", "Пример ухода"],
)
def test_real_content_is_not_a_placeholder(value: str) -> None:
    assert validation.is_placeholder(value) is False


def test_the_pos_stock_label_is_never_a_suggestion() -> None:
    data = Facts.of({"pos_stock_label": "NEW TONER 200ML", "texts": {}})
    for text in ("NEW TONER 200ML", "  new   toner 200ml ", "New Toner 200 ml", "New toner, 200ML"):
        assert validation.check("texts.uz.name", text, data, mode=FILL).rejected == validation.R_POS_LABEL
    assert validation.check("texts.uz.name", "New toner", data, mode=FILL).rejected is None
    assert validation.check("texts.uz.name", "NEW TONER 200ML", data, mode=PHOTOS).rejected == (
        validation.R_POS_LABEL
    )
    assert Facts.of({"texts": {}}).pos_label == ""


def test_spf_is_rejected_unless_the_data_says_it() -> None:
    assert validation.check("texts.en.description", "Daily SPF protection", facts(), mode=FILL).rejected == (
        validation.R_UNSUPPORTED_SPF
    )
    sunscreen = facts(description="Quyoshdan himoya SPF 50")
    assert validation.check("texts.en.description", "SPF 50 sunscreen", sunscreen, mode=FILL).rejected is None


@pytest.mark.parametrize(
    "text",
    [
        "Dermatologist tested formula",
        "Клинически доказанный эффект",
        "Ekzemani davolaydi",
        "Organic and vegan",
    ],
)
def test_certification_and_medical_claims_are_flagged(text: str) -> None:
    verdict = validation.check("texts.en.description", text, facts(), mode=FILL)
    assert verdict.rejected is None
    assert validation.W_UNSUPPORTED_CLAIM in verdict.warnings


def test_a_claim_the_data_already_makes_is_not_flagged() -> None:
    verdict = validation.check(
        "texts.en.description", "Vegan cream", facts(description="Vegan krem"), mode=FILL
    )
    assert verdict.warnings == []


def test_inci_mismatch_is_a_warning() -> None:
    data = facts(ingredients="Aqua, Glycerin, Niacinamide")
    same = validation.check("texts.ru.ingredients", "Aqua, Glycerin, Niacinamide", data, mode=FILL)
    assert same.warnings == []
    other = validation.check("texts.ru.ingredients", "Aqua, Glycerin, Retinol", data, mode=FILL)
    assert other.rejected is None and other.warnings == [validation.W_INCI_MISMATCH]


def test_over_the_limit_is_rejected_and_long_seo_is_a_warning() -> None:
    assert validation.check("seo.uz.title", "a" * 121, facts(), mode=FILL).rejected == validation.R_TOO_LONG
    assert validation.check("seo.uz.title", "a" * 70, facts(), mode=FILL).warnings == [
        validation.W_SEO_TITLE_LONG
    ]
    assert validation.check("seo.uz.description", "a" * 200, facts(), mode=FILL).warnings == [
        validation.W_SEO_DESCRIPTION_LONG
    ]
    assert validation.check("texts.uz.name", "", facts(), mode=FILL).rejected == validation.R_EMPTY


def test_photos_keep_numbers_but_ask_for_confirmation() -> None:
    verdict = validation.check("texts.uz.usage", "Kuniga 2 marta surting", facts(), mode=PHOTOS)
    assert verdict.rejected is None
    assert verdict.warnings == [validation.W_CONFIRM_PACKAGING]


def test_parse_answer_drops_unknown_forbidden_and_duplicate_keys() -> None:
    answer = {
        "suggestions": [
            {"key": "texts.ru.name", "value": "Сыворотка"},
            {"key": "texts.ru.name", "value": "второй"},
            {"key": "slug", "value": "new-url"},
            {"key": "price", "value": "1"},
            {"key": "texts.en.name", "value": 5},
            "junk",
        ]
    }
    values, dropped = validation.parse_answer(answer, ["texts.ru.name", "texts.en.name"])
    assert values == {"texts.ru.name": "Сыворотка"}
    assert dropped == 5


@pytest.mark.parametrize("answer", [None, [], {}, {"suggestions": "x"}, {"notes": []}])
def test_parse_answer_refuses_another_shape(answer: object) -> None:
    with pytest.raises(validation.BadAnswer):
        validation.parse_answer(answer, ["texts.ru.name"])


def test_build_suggestions_keeps_target_order_and_reports_rejections() -> None:
    values = {
        "texts.ru.description": "Описание 99",
        "texts.ru.name": "Сыворотка",
        "texts.en.name": "Serum",
        "seo.uz.title": "Serum",
    }
    snapshot = {"texts.ru.name": "", "texts.en.name": "Serum", "texts.ru.description": "", "seo.uz.title": ""}
    suggestions, warnings = validation.build_suggestions(
        values,
        targets=["texts.ru.name", "texts.en.name", "texts.ru.description", "seo.uz.title"],
        snapshot=snapshot,
        facts=facts(),
        mode=FILL,
    )
    assert [s["key"] for s in suggestions] == ["texts.ru.name", "seo.uz.title"]  # en unchanged → a warning
    assert suggestions[0] == {
        "key": "texts.ru.name",
        "current": "",
        "suggested": "Сыворотка",
        "warnings": [],
        "origin": "ai",
        "checked": True,
        "max_length": 120,
    }
    assert warnings == [
        {"code": "unchanged", "key": "texts.en.name"},
        {"code": "rejected", "key": "texts.ru.description", "reason": "unsupported_number"},
    ]


def test_unchanged_compares_the_normalised_values() -> None:
    suggestions, warnings = validation.build_suggestions(
        {"texts.uz.name": " Yog'li krem "},
        targets=["texts.uz.name"],
        snapshot={"texts.uz.name": "Yog‘li krem"},
        facts=facts(),
        mode=FILL,
    )
    assert suggestions == [] and warnings == [{"code": "unchanged", "key": "texts.uz.name"}]


def test_an_unverified_count_starts_unchecked() -> None:
    suggestions, warnings = validation.build_suggestions(
        {"texts.ru.usage": "Наносите 1–2 раза в день"},
        targets=["texts.ru.usage"],
        snapshot={},
        facts=facts(),
        mode=FILL,
    )
    assert warnings == []
    assert suggestions[0]["warnings"] == [validation.W_UNVERIFIED_NUMBER]
    assert suggestions[0]["checked"] is False


def test_parse_skipped() -> None:
    targets = ["texts.ru.name", "texts.en.name", "texts.uz.usage"]
    answer = {
        "suggestions": [],
        "skipped": [
            {"key": "texts.uz.usage", "reason": "  No directions\n in the data. "},
            {"key": "texts.uz.usage", "reason": "again"},
            {"key": "texts.ru.name", "reason": "answered anyway"},
            {"key": "slug", "reason": "not a target"},
            {"key": "texts.en.name", "reason": "x" * 500},
            {"key": "texts.en.name"},
            "junk",
        ],
    }
    assert validation.parse_skipped(answer, targets, ["texts.ru.name"]) == [
        {"code": "skipped", "key": "texts.uz.usage", "reason": "No directions in the data."},
        {"code": "skipped", "key": "texts.en.name", "reason": "x" * validation.SKIP_REASON_MAX},
    ]
    assert validation.parse_skipped({"skipped": "nope"}, targets, []) == []
    assert validation.parse_skipped(None, targets, []) == []


def test_flagged_and_photo_suggestions_start_unchecked() -> None:
    flagged, _ = validation.build_suggestions(
        {"texts.en.description": "Hypoallergenic cream"},
        targets=["texts.en.description"],
        snapshot={},
        facts=facts(),
        mode=FILL,
    )
    assert flagged[0]["checked"] is False
    photo, _ = validation.build_suggestions(
        {"texts.uz.name": "Krem"}, targets=["texts.uz.name"], snapshot={}, facts=facts(), mode=PHOTOS
    )
    assert photo[0]["checked"] is False and photo[0]["origin"] == "image"


def test_extracted_info() -> None:
    assert validation.extracted_info({"extracted": {"brand": " Glow ", "volume": "", "x": "y"}}) == {
        "brand": "Glow"
    }
    assert validation.extracted_info({"suggestions": []}) == {}
