"""``manage.py scene_spike``: run the product scene pipeline over a folder of real photos for review.

Nothing is stored in the database or the media storage: per photo and template it writes the mask outline on
the full photo (what a human checks: a cap outside the line or a finger inside it breaks the rule the
paste-back cannot catch), the canvas sent to the image model, the model's answer, the final 4:5 master, its
square crop, the interior-difference image (must be black), the generated-inside highlight, and a summary in
``report.json`` / ``report.html``.

The adapters are built directly (``--fake`` for offline runs, otherwise the live Gemini adapters), so the run
does not depend on ``AI_MODE`` or the assistant's console settings. The key comes from ``--key-source``:
``env`` (the variable named by ``--key-env``), ``vault`` (the assistant's active Gemini key) or ``proxy`` (a
placeholder the session's network proxy replaces; the real key never enters this process).
"""

from __future__ import annotations

import html
import json
import os
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from glowderma.assistant.scenes import pipeline
from glowderma.assistant.scenes.templates import BY_KEY, Template
from glowderma.core import compose
from glowderma.integrations.llm.bria_image import BriaSceneModel
from glowderma.integrations.llm.fake import FakeContentModel, FakeImageModel
from glowderma.integrations.llm.gemini import GeminiContentModel
from glowderma.integrations.llm.gemini_image import GeminiImageModel
from glowderma.integrations.llm.ports import LLMError
from glowderma.integrations.secret import Secret
from glowderma.integrations.segmentation.bria import BriaCutter
from glowderma.integrations.segmentation.combined import CombinedSegmenter
from glowderma.integrations.segmentation.fake import FakeSegmenter
from glowderma.integrations.segmentation.gemini_json import GeminiJsonSegmenter
from glowderma.integrations.segmentation.gemini_matte import GeminiMatteCutter
from glowderma.integrations.segmentation.ports import Segmenter

STYLE = (
    "<style>body{font:14px system-ui;margin:16px;background:#faf8f5}"
    "figure{display:inline-block;margin:4px;vertical-align:top}"
    "img{max-width:240px;max-height:300px;border:1px solid #ddd}"
    "section{border-top:1px solid #ccc;padding:8px 0}.err{color:#b00}code{font-size:12px}</style>"
)
HIDDEN_KEYS = ("outline", "mask", "hidden")
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
PROXY_PLACEHOLDER = "proxy-injected"  # noqa: S105 - not a key: the proxy replaces the header


def _key(source: str, env_name: str, provider: str = "gemini") -> Secret:
    if source == "proxy":
        return Secret(PROXY_PLACEHOLDER)
    if source == "env":
        value = os.environ.get(env_name, "")
        if not value:
            raise CommandError(f"{env_name} is empty")
        return Secret(value)
    from glowderma.assistant import credentials

    try:
        return credentials.active_key(provider)
    except Exception as exc:  # noqa: BLE001 - Vault or a missing key: the message names no secret
        raise CommandError(f"no active {provider} key in Vault ({type(exc).__name__})") from None


def build_providers(
    *,
    fake: bool,
    segmenter: str,
    api_key: Secret,
    image_model: str = pipeline.Config.image_model,
    bria_key: Secret | None = None,
    scene_provider: str = "gemini",
) -> pipeline.Providers:
    if fake:
        return pipeline.Providers(
            segmenter=FakeSegmenter(), image_model=FakeImageModel(), content_model=FakeQA(), api_key=api_key
        )
    content, image = GeminiContentModel(), GeminiImageModel()
    chosen: Segmenter
    if segmenter == "json":
        chosen = GeminiJsonSegmenter(content)
    else:
        analyst = GeminiJsonSegmenter(content, require_mask=False)
        if segmenter == "bria":
            chosen = CombinedSegmenter(analyst, BriaCutter(), cutter_key=bria_key or Secret(""))
        else:
            cutter = GeminiMatteCutter(image, model=image_model)
            chosen = CombinedSegmenter(analyst, cutter, cutter_key=api_key)
    if scene_provider == "bria":
        return pipeline.Providers(
            segmenter=chosen,
            image_model=BriaSceneModel(),
            content_model=content,
            api_key=api_key,
            image_key=bria_key or Secret(""),
        )
    return pipeline.Providers(segmenter=chosen, image_model=image, content_model=content, api_key=api_key)


class FakeQA(FakeContentModel):
    """The schema-shaped fake answers ``false`` to every QA question: a passing check."""


def photos(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in PHOTO_SUFFIXES)


def _save(image: Any, path: Path, fmt: str = "PNG") -> str:
    path.write_bytes(compose.encode(image, fmt, 92))
    return path.name


def review_overlay(photo: Any, analysis: pipeline.Analysis) -> Any:
    """The photo with the product mask's outline (magenta), the hull (cyan) and the hands (yellow)."""
    image = compose.outline_overlay(photo, analysis.occlusion.hull, (0, 200, 220))
    if analysis.segmentation.hands is not None:
        image = compose.outline_overlay(image, analysis.segmentation.hands, (250, 210, 0))
    return compose.outline_overlay(image, analysis.mask)


def run_one(
    photo_path: Path,
    templates: list[Template],
    providers: pipeline.Providers,
    config: pipeline.Config,
    prices: pipeline.Prices,
    out: Path,
) -> dict[str, Any]:
    folder = out / photo_path.stem
    folder.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {"photo": photo_path.name, "results": []}
    try:
        photo = pipeline.load_photo(photo_path.read_bytes())
    except Exception as exc:  # noqa: BLE001 - a file that is not an image
        entry["error"] = f"unreadable ({type(exc).__name__})"
        return entry
    entry["size"] = list(photo.size)
    usage = pipeline.Usage()
    try:
        analysis = pipeline.analyse(photo, providers, config, usage)
    except (pipeline.SceneError, LLMError) as exc:
        entry["error"] = exc.code
        entry["detail"] = str(exc)  # ours, never the provider's text (adapters map errors)
        entry["cost_usd"] = str(usage.cost(prices))
        return entry
    seg = analysis.segmentation
    entry["analysis"] = {
        "box": list(analysis.box),
        "occluded_pct": analysis.occluded_pct,
        "model_covered_pct": seg.covered_percent,
        "hand_present": seg.hand_present,
        "hand_covers_product": seg.hand_covers_product,
        "background": seg.background,
        "raw": seg.raw,
        "warnings": analysis.warnings,
        "seconds": analysis.seconds,
        "outline": _save(review_overlay(photo, analysis), folder / "outline.jpg", "JPEG"),
        "mask": _save(analysis.mask, folder / "mask.png"),
    }
    if analysis.occlusion.percent > 0:
        entry["analysis"]["hidden"] = _save(
            compose.tint(photo, analysis.occlusion.hidden), folder / "hidden.jpg", "JPEG"
        )
    analysis_cost = usage.cost(prices)
    for template in templates:
        scene_usage = pipeline.Usage()
        row: dict[str, Any] = {"template": template.key, "kind": template.kind}
        started = time.monotonic()
        try:
            if template.kind == "studio":
                result = pipeline.studio(analysis, template, config)
            else:
                result = pipeline.ai_scene(analysis, template, providers, config, scene_usage)
        except (pipeline.SceneError, LLMError) as exc:
            row["error"] = exc.code
            row["attempts"] = [asdict(a) for a in getattr(exc, "attempts", [])]
        else:
            prefix = folder / template.key
            row["final"] = _save(result.final, prefix.with_name(f"{template.key}.webp"), "WEBP")
            row["square"] = _save(
                result.final.crop(result.square), prefix.with_name(f"{template.key}_1x1.webp"), "WEBP"
            )
            row["interior_difference"] = result.interior_difference
            row["warnings"] = result.warnings
            row["generated_inside_pct"] = result.generated_inside_pct
            row["attempts"] = [asdict(a) for a in result.attempts]
            if result.canvas is not None:
                row["canvas"] = _save(result.canvas, prefix.with_name(f"{template.key}_canvas.png"))
            if result.generated is not None:
                row["generated"] = _save(
                    result.generated, prefix.with_name(f"{template.key}_model.webp"), "WEBP"
                )
            if result.generated_inside is not None:
                row["generated_inside"] = _save(
                    compose.tint(result.final, result.generated_inside),
                    prefix.with_name(f"{template.key}_generated_inside.webp"),
                    "WEBP",
                )
        row["seconds"] = round(time.monotonic() - started, 2)
        row["cost_usd"] = str(scene_usage.cost(prices) + analysis_cost)
        row["image_calls"] = scene_usage.image_calls
        entry["results"].append(row)
    entry["analysis_cost_usd"] = str(analysis_cost)
    return entry


def render_html(entries: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    def img(folder: str, name: str | None, label: str) -> str:
        if not name:
            return ""
        src = html.escape(f"{folder}/{name}")
        caption = html.escape(label)
        link = f'<a href="{src}"><img src="{src}" loading="lazy"></a>'
        return f"<figure>{link}<figcaption>{caption}</figcaption></figure>"

    parts = [
        "<!doctype html><meta charset=utf-8><title>Scene spike</title>",
        STYLE,
        f"<h1>Scene spike</h1><pre>{html.escape(json.dumps(summary, indent=2))}</pre>",
    ]
    for entry in entries:
        folder = Path(entry["photo"]).stem
        parts.append(f"<section><h2>{html.escape(entry['photo'])}</h2>")
        if entry.get("error"):
            parts.append(f"<p class=err>{html.escape(entry['error'])}</p></section>")
            continue
        a = entry["analysis"]
        parts.append(
            "<p><code>"
            + html.escape(json.dumps({k: v for k, v in a.items() if k not in HIDDEN_KEYS}))
            + "</code></p>"
        )
        parts.append(
            img(folder, a.get("outline"), "mask outline on the photo")
            + img(folder, a.get("hidden"), "hidden by a hand")
        )
        for row in entry["results"]:
            label = f"{row['template']} ({row['kind']})"
            if row.get("error"):
                parts.append(f"<p class=err>{html.escape(label)}: {html.escape(row['error'])}</p>")
                continue
            meta = {
                k: row[k]
                for k in ("interior_difference", "generated_inside_pct", "warnings", "seconds", "cost_usd")
            }
            parts.append(f"<h3>{html.escape(label)}</h3><p><code>{html.escape(json.dumps(meta))}</code></p>")
            parts.append(
                img(folder, row.get("canvas"), "sent")
                + img(folder, row.get("generated"), "model")
                + img(folder, row.get("final"), "final 4:5")
                + img(folder, row.get("square"), "square")
                + img(folder, row.get("generated_inside"), "generated inside product")
            )
        parts.append("</section>")
    return "\n".join(parts)


def summarise(entries: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for entry in entries for row in entry.get("results", [])]
    ai = [row for row in rows if row["kind"] == "ai_scene"]
    ai_ok = [row for row in ai if not row.get("error")]
    first_try = [row for row in ai_ok if len(row.get("attempts", [])) == 1]
    total_cost = sum((Decimal(row["cost_usd"]) for row in rows), Decimal(0))
    return {
        "photos": len(entries),
        "photo_errors": {e["photo"]: e["error"] for e in entries if e.get("error")},
        "results": len(rows),
        "rule_held": all(
            row.get("interior_difference", 0) == 0 and row.get("error") != pipeline.RULE_BROKEN
            for row in rows
        ),
        "ai_scenes": len(ai),
        "ai_accepted": len(ai_ok),
        "ai_first_attempt": len(first_try),
        "errors": sorted({row["error"] for row in rows if row.get("error")}),
        "cost_usd": str(total_cost),
        "cost_per_accepted_ai_usd": str(
            (sum((Decimal(r["cost_usd"]) for r in ai), Decimal(0)) / len(ai_ok)).quantize(Decimal("0.0001"))
        )
        if ai_ok
        else None,
    }


class Command(BaseCommand):
    requires_system_checks: list[str] = []  # no database: the run must work on a laptop with photos only
    help = "Run the product scene pipeline over a folder of photos and write a review report (no database)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--input", required=True, type=Path, help="folder of product photos")
        parser.add_argument("--out", required=True, type=Path, help="report folder (created)")
        parser.add_argument(
            "--templates", default="studio-ivory,marble", help="comma-separated template keys"
        )
        parser.add_argument("--fake", action="store_true", help="offline fakes, no network")
        parser.add_argument("--segmenter", choices=("json", "matte", "bria"), default="matte")
        parser.add_argument(
            "--scene-provider", choices=("gemini", "bria"), default="gemini", help="who paints AI scenes"
        )
        parser.add_argument("--segment-model", default=pipeline.Config.segment_model)
        parser.add_argument("--image-model", default=pipeline.Config.image_model)
        parser.add_argument("--qa-model", default=pipeline.Config.qa_model)
        parser.add_argument("--size", choices=("1K", "2K", "4K"), default=pipeline.Config.image_size)
        parser.add_argument("--max-occluded", type=float, default=pipeline.Config.max_occluded_pct)
        parser.add_argument("--max-attempts", type=int, default=pipeline.Config.max_attempts)
        parser.add_argument("--no-qa", action="store_true")
        parser.add_argument("--no-straighten", action="store_true")
        parser.add_argument("--key-source", choices=("env", "vault", "proxy"), default="env")
        parser.add_argument("--key-env", default="GEMINI_API_KEY")
        parser.add_argument("--bria-key-env", default="BRIA_API_KEY")

    def handle(self, *args: Any, **options: Any) -> None:
        source: Path = options["input"]
        out: Path = options["out"]
        if not source.is_dir():
            raise CommandError(f"{source} is not a folder")
        keys = [key.strip() for key in str(options["templates"]).split(",") if key.strip()]
        unknown = [key for key in keys if key not in BY_KEY]
        if unknown:
            raise CommandError(f"unknown templates {unknown}; known: {sorted(BY_KEY)}")
        templates = [BY_KEY[key] for key in keys]
        api_key = Secret("fake-key") if options["fake"] else _key(options["key_source"], options["key_env"])
        bria_key = None
        if "bria" in (options["segmenter"], options["scene_provider"]) and not options["fake"]:
            bria_key = _key(options["key_source"], options["bria_key_env"], "bria")
        providers = build_providers(
            fake=options["fake"],
            segmenter=options["segmenter"],
            api_key=api_key,
            image_model=options["image_model"],
            bria_key=bria_key,
            scene_provider=options["scene_provider"],
        )
        config = pipeline.Config(
            segment_model=options["segment_model"],
            image_model=options["image_model"],
            qa_model=options["qa_model"],
            image_size=options["size"],
            max_occluded_pct=options["max_occluded"],
            max_attempts=options["max_attempts"],
            qa_check=not options["no_qa"],
            straighten=not options["no_straighten"],
        )
        prices = pipeline.Prices()
        out.mkdir(parents=True, exist_ok=True)
        entries = []
        for path in photos(source):
            self.stdout.write(f"{path.name} …")
            entries.append(run_one(path, templates, providers, config, prices, out))
        summary = summarise(entries)
        (out / "report.json").write_text(
            json.dumps({"summary": summary, "photos": entries}, indent=2, default=str)
        )
        (out / "report.html").write_text(render_html(entries, summary))
        self.stdout.write(json.dumps(summary, indent=2))
        if not summary["rule_held"]:
            raise CommandError("the product interior changed in at least one result")
