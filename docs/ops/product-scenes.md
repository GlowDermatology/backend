# Product scenes: evaluation run

**Status:** evaluation only. There are no endpoints, no database tables and no settings yet. This page covers the offline pipeline and `manage.py scene_spike`, which runs it on real photos before the staff feature is built.

## What it does

Staff photograph products on a phone, often in an office and sometimes holding the product. The pipeline builds a clean scene around the product. The product itself is never redrawn.

1. **Segment.** One Gemini call (the existing content-model port, `integrations/segmentation/gemini_json.py`) returns:
   - the product's box and mask;
   - hand regions;
   - whether a hand covers the product.

   Hands, sleeves, desks, monitors and the background are excluded from the mask. Enclosed holes in the mask, such as label letters, are filled, except where a hand is.
2. **Occlusion.** The server measures the part of the product a hand hides: the convex hull of the visible mask, intersected with the hands, minus the visible part. The model's own estimate counts when it is higher.
   - At or below 0.3 %, nothing is hidden: the hand is simply cut away (warning `hand_removed`).
   - Up to `max_occluded_pct` (5 %, the owner's decision), the image model may fill the gap. The result records exactly where (`generated_inside`) for staff to check (warning `product_partly_generated`).
   - Above 5 %, the photo is refused with `scene_hand_covers_product` ("retake the photo").
   - Studio templates cannot fill anything, so any real occlusion is refused with `scene_occlusion_needs_ai`.
   - A product the photo's edge cuts off (the model's flag, or a box within 1 % of a border) is refused with `scene_product_cut_off` before any image call: the missing part does not exist in the photo.
3. **Layout.** The cut-out goes on a 4:5 master (`core/compose.py`).
   - A product leaning 3–25° is straightened first.
   - The product stands on a fixed baseline (82 % of the height) inside a centred safe zone, so the middle square crop also holds the whole product.
4. **Scene.**
   - **Studio templates:** background and contact shadow are composed in Pillow.
   - **AI templates:** Nano Banana receives the canvas (the product on a grey placeholder) with a locked-product prompt: no hands, people, office or text, and a contact shadow at the baseline.
5. **Paste-back.** The photo's own product pixels go back on top. Every pixel deeper than the 2–3 px edge band is the original. The run asserts `interior_difference == 0` and fails if it is not.
6. **Checks** (AI only):
   - Registration catches a product the model moved or resized (more than 1 % shift, or any scale change).
   - The interior-change score catches a restyled product.
   - One cheap QA call catches hands, people, office remnants, extra products or stray text.
   - A failed attempt is retried once with a stricter prompt, then refused with `scene_product_drift` or `scene_result_rejected`.

## Running it

```bash
uv run python manage.py scene_spike --input <photo folder> --out <report folder> \
  --templates studio-ivory,studio-night,marble,silk --key-source env
```

**Key sources** (`--key-source`):
- `env` (default): reads the key from `$GEMINI_API_KEY`.
- `vault`: uses the assistant's active Gemini key.
- `proxy`: a placeholder that a network proxy replaces.

**Options:**
- `--fake`: runs offline, with no key and no network.
- `--segmenter matte`: tries the green-matte fallback. It costs one image call and cannot see hands.
- `--size 1K|2K`, `--image-model`, `--segment-model`, `--max-occluded`, `--no-qa`, `--no-straighten`.

The command needs no database and stores nothing outside `--out`. It does not use `AI_MODE` or the console settings.

## Reading the report

Open `report.html`. Each photo shows:

- **`outline.jpg`:** the mask outline on the full photo. **This is the check that matters.** A cap, pump or label outside the line, or a finger inside it, breaks the rule in a way the paste-back cannot catch.
- **`hidden.jpg`:** the product pixels a hand hides.
- **For each template:**
  - the canvas sent to the model, and the model's answer;
  - the final 4:5 image and its square crop;
  - the generated-inside highlight.

`report.json` holds the same data, with the costs. Prices use the defaults in `pipeline.Prices`; confirm them on Google's pricing page.

## Go / no-go (30–40 real photos)

| Check | Target |
|---|---|
| Product interior unchanged (`rule_held`) | every result |
| Mask usable at full size, judged on `outline.jpg` | ≥ 85 % of plain and office photos, ≥ 80 % of hand-held ones |
| AI scenes accepted within two attempts | ≥ 90 %, with no hands or office in accepted results |
| Cost per accepted AI scene | ≤ about $0.12 |

**What each outcome means:**
- **All pass:** build the staff feature.
- **AI scenes fail:** ship Studio templates and screen-fit only.
- **Masks fail:** ship manual cropping only, and decide on a dedicated background remover (Bria).
