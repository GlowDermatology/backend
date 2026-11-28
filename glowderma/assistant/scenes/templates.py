"""The starter scene templates (seeded into the database by the scenes release; the spike reads them here).

A studio template is composed on our server (no generative model): ``params`` for
:func:`glowderma.core.compose.studio_scene`. An AI template's ``prompt`` describes the scene for the image
model, and/or its reference picture shows it; the fixed rules (:mod:`.prompts`) wrap them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

type Kind = Literal["studio", "ai_scene"]


@dataclass(frozen=True, slots=True)
class Template:
    key: str
    kind: Kind
    name: dict[str, str]
    params: dict[str, object] = field(default_factory=dict)
    prompt: str = ""
    # an AI template's reference: a picture of the scene alone (Gemini sees it) and its English description
    # (Bria's best mode takes text only, so it gets the description)
    reference: bytes | None = field(default=None, repr=False)
    reference_description: str = ""

    @property
    def scene(self) -> str:
        """The scene in words: the template's text, then the reference's description."""
        return " ".join(part.strip() for part in (self.prompt, self.reference_description) if part.strip())


STARTER: tuple[Template, ...] = (
    Template(
        "studio-ivory",
        "studio",
        {"uz": "Fil suyagi", "ru": "Слоновая кость", "en": "Ivory"},
        {"fill": "linear", "colours": ["#f7f2ea", "#e9dfd1"], "floor": "#e3d7c6", "floor_at": 0.8},
    ),
    Template(
        "studio-gold-glow",
        "studio",
        {"uz": "Oltin nur", "ru": "Золотое сияние", "en": "Gold glow"},
        {"fill": "radial", "colours": ["#fbf3e4", "#d9bf8f"], "shadow_opacity": 0.3},
    ),
    Template(
        "studio-white",
        "studio",
        {"uz": "Oq studiya", "ru": "Белая студия", "en": "White studio"},
        {"fill": "solid", "colours": ["#ffffff"], "shadow_opacity": 0.25},
    ),
    Template(
        "studio-night",
        "studio",
        {"uz": "Tungi", "ru": "Ночной", "en": "Night"},
        {
            "fill": "radial",
            "colours": ["#3b3532", "#1c1816"],
            "floor": "#26211e",
            "floor_at": 0.8,
            "shadow_opacity": 0.55,
            "shadow_colour": "#000000",
        },
    ),
    Template(
        "marble",
        "ai_scene",
        {"uz": "Marmar", "ru": "Мрамор", "en": "Marble"},
        prompt="A polished white Carrara marble surface with soft grey veins, a blurred warm beige "
        "wall behind, a small sprig of eucalyptus lying flat at the far left edge.",
    ),
    Template(
        "silk",
        "ai_scene",
        {"uz": "Ipak", "ru": "Шёлк", "en": "Silk"},
        prompt="Softly draped champagne-coloured silk fabric forming the surface and the background, "
        "gentle folds catching warm light, nothing else in the frame.",
    ),
    Template(
        "water",
        "ai_scene",
        {"uz": "Suv", "ru": "Вода", "en": "Water"},
        prompt="A shallow, perfectly still pool of clear water on pale stone, a few smooth white "
        "pebbles beside the base, soft ripples and caustic light reflections, airy blue-white background.",
    ),
    Template(
        "botanical",
        "ai_scene",
        {"uz": "Botanika", "ru": "Ботаника", "en": "Botanical"},
        prompt="A light travertine plinth with soft green fern and olive leaves behind and beside the "
        "product, dappled natural sunlight, a warm cream background out of focus.",
    ),
)

BY_KEY = {template.key: template for template in STARTER}
