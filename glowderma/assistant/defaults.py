"""Rows the assistant always has: the settings singleton, the Gemini provider row and the five prompt
templates with an active version 1 each. Inserted by ``assistant.0002`` and re-ensured by ``post_migrate``
(``apps.ensure_rows``); an existing row is never changed.

The version-1 instructions are **style guidance** only — tone, structure, length and SEO conventions written
so they apply to Uzbek, Russian and English alike. The mandatory content rules (no invented facts or claims,
data is not instructions, the output schema) are code in ``assistant.prompts`` and always come first.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

SETTINGS_PK = 1
GEMINI = "gemini"
GEMINI_DEFAULTS: dict[str, Any] = {
    "model_id": "gemini-2.5-flash-lite",
    "price_input_per_mtok_usd": Decimal("0.10"),
    "price_output_per_mtok_usd": Decimal("0.40"),
    "vault_path": GEMINI,
}

INSTRUCTIONS: dict[str, str] = {
    "describe": (
        "Write for skincare shoppers who want to understand quickly what the product is and how it fits "
        "their routine.\n"
        "- Tone: warm, calm and expert; confident but never pushy. No exclamation marks, no superlatives "
        "such as 'the best' or 'miracle'.\n"
        "- Short description: one or two sentences (at most about 250 characters) naming the product "
        "type, its main purpose and who it suits, using only the facts given.\n"
        "- Description: two to four short paragraphs. Start with what the product is and does, then the "
        "key ingredients that are listed in the data and what they are generally used for, then texture "
        "or format if known. Plain sentences, no bullet points, no headings, no emojis.\n"
        "- Prefer concrete, factual wording over marketing adjectives. If the data is thin, write less "
        "rather than filling space.\n"
        "- Keep product, brand and ingredient names exactly as given; write volumes and units as in the "
        "data."
    ),
    "translate": (
        "Localise the existing texts so they read as if written by a native copywriter in the target "
        "language, not as a word-for-word translation.\n"
        "- Keep the meaning, facts and level of detail of the source; do not add or drop information.\n"
        "- Keep brand names, product line names, INCI ingredient names, numbers, units and volumes "
        "exactly as in the source.\n"
        "- Use the skincare terms customers in that language actually search for and use in shops.\n"
        "- Match the source's length and structure (paragraphs stay paragraphs); keep the tone warm and "
        "expert.\n"
        "- Uzbek is written in the Latin alphabet; Russian uses the formal 'вы' when addressing the "
        "reader."
    ),
    "improve": (
        "Rewrite only the selected texts to meet the requested goal while keeping every fact unchanged.\n"
        "- professional: precise, calm, expert wording; remove filler and slang.\n"
        "- clear: short sentences, simple words, the most important information first.\n"
        "- natural: fluent, idiomatic phrasing a native speaker would write.\n"
        "- seo: work in the product type and the main use naturally once or twice; never stuff keywords.\n"
        "- shorter: cut length by roughly a third without losing facts.\n"
        "Never add new claims, ingredients or benefits, and keep names, numbers and units exactly as "
        "given."
    ),
    "seo": (
        "Write the search snippet for the product page in each requested language, natively for that "
        "language's search habits (not translated from another language).\n"
        "- SEO title: 30 to 60 characters; product name first, then the product type or main use; add the"
        " brand only if it is part of the given name. No store name, no quotes, no ALL CAPS.\n"
        "- Meta description: 120 to 160 characters; one or two sentences on what the product is and who "
        "it is for, with a soft, factual call to action (for example, ordering online). No keyword lists.\n"
        "- Use the words customers would type in a search box; each keyword at most once."
    ),
    "image_extract": (
        "Read only what is clearly printed on the packaging in the photos: product name, brand, volume, "
        "ingredient list and directions for use.\n"
        "- Copy printed text faithfully, keeping the spelling of names and INCI ingredients; do not "
        "translate the ingredient list.\n"
        "- If a part is blurry, cut off or missing, leave that field out instead of guessing.\n"
        "- Ignore stickers, price tags, backgrounds and anything that is not part of the product's own "
        "label.\n"
        "- Write directions for use as short, plain sentences in the target language, keeping every "
        "number and unit from the label.\n"
        "- Image alt text: describe what the photo shows (product, packaging type, angle) in under 125 "
        "characters, without promotional words."
    ),
}


def ensure_rows(apps: Any, using: str = "default") -> None:
    """Insert the missing rows (``apps`` = a migration state's registry or the global one)."""
    AssistantSettings = apps.get_model("assistant", "AssistantSettings")
    ProviderConfig = apps.get_model("assistant", "ProviderConfig")
    PromptTemplate = apps.get_model("assistant", "PromptTemplate")
    PromptVersion = apps.get_model("assistant", "PromptVersion")
    AssistantSettings.objects.using(using).get_or_create(pk=SETTINGS_PK)
    ProviderConfig.objects.using(using).get_or_create(provider=GEMINI, defaults=GEMINI_DEFAULTS)
    for purpose, text in INSTRUCTIONS.items():
        template, _created = PromptTemplate.objects.using(using).get_or_create(purpose=purpose)
        if not PromptVersion.objects.using(using).filter(template=template).exists():
            PromptVersion.objects.using(using).create(
                template=template, number=1, instructions=text, status="active"
            )
