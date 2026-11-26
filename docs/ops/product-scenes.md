# Product scenes and screen fitting

**Status:** built, ships switched off. Staff use it from the product editor's image tiles (admin API §2.20).
Superusers switch it on and set it up in the ops console under **AI yordamchi → Mahsulot sahnalari**
(`/django-admin/ai/scenes/`).

## What staff get

- **Fit to screens:** a 4:5 portrait and a square copy of a photo, framed on the product. They can be set by
  hand with a cropper, or proposed by one cheap analyst call. The storefront and the app use them where they
  exist and fall back to the photo itself.
- **Studio scene:** the product on a clean background with a soft shadow, composed on our server. The only
  provider calls are the analyst and the cut-out.
- **AI scene:** the product in a generated scene (marble, silk, water, botanicals) from Nano Banana.

Accepting a scene adds a **new** gallery image; the original photo is never changed. Candidates nobody
reviews expire after 72 h.

## Cut-out sources (console: "mahsulotni kesib olish")

- **Analyst:** Gemini Flash Lite (`analyst_model`) always reads the photo for the product's box, hands, how
  much a hand hides, and whether the product is cut off by the frame. Gemini 2.5 models are closed to new
  API keys, and current text models return placeholder masks, so the pixels come from a cutter.
- **`gemini_matte`** (default): Nano Banana repaints everything but the product green. We key the green and
  refuse the result if the repaint does not line up with the photo. Needs billing on the Google project.
- **`bria`**: Bria RMBG background removal, about $0.018 per photo. Its key goes in Vault through
  **AI provayderlar → Bria** like the Gemini key. The worker needs outbound HTTPS to
  `engine.prod.bria-api.com` and to the host of Bria's result URLs.
- **`gemini_json`**: only for keys that still have a segmenting model.

The cutter's mask must agree with the analyst's box, or the job fails with `scene_mask_unusable`.

## What it does

Staff photograph products on a phone, often in an office and sometimes holding the product. The pipeline builds a clean scene around the product. The product itself is never redrawn.

1. **Segment.** One Gemini call (the existing content-model port, `integrations/segmentation/gemini_json.py`) returns:
   - the product's box and mask;
   - hand regions;
   - whether a hand covers the product.

   Hands, sleeves, desks, monitors and the background are excluded from the mask. Enclosed holes in the mask, such as label letters, are filled, except where a hand is.
2. **Occlusion.** The server measures the part of the product a hand hides: the convex hull of the visible mask, intersected with the hands, minus the visible part. The model's own estimate counts when it is higher.
   - At or below 0.3 %, nothing is hidden: the hand is simply cut away (warning `hand_removed`).
   - Up to `max_occluded_pct` (15 %, the owner's decision; console setting), the image model may fill the gap. The result records exactly where (`generated_inside`) for staff to check (warning `product_partly_generated`).
   - Above it, the photo is refused with `scene_hand_covers_product` ("retake the photo").
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

## Going live

1. `migrate` (catalog `0013_productimage_scenes`, assistant `0005_scenes`, `0006_seed_scenes`).
2. Restart the API, the worker and beat (new beat entry `assistant-scenes-housekeeping`, hourly).
3. Enable billing on the Google AI Studio project of the Gemini key: image models answer 429 without it.
4. In the console, set the analyst model (`gemini-3.5-flash-lite` by default) and check the prices against
   Google's and Bria's pages.
5. Optional: put the Bria key in **AI provayderlar → Bria → Kalit** and press "Test"; choose `bria` as the
   cut-out source.
6. Switch on **sahnalar yoqilgan** (fitting and studio scenes), then **AI sahnalar yoqilgan** after a test.

There are no `.env` changes: keys live in Vault, and everything else is in the console.
