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

- **Analyst:** Gemini Flash Lite (`analyst_model`) always reads the photo for the product's box, hands,
  whether a finger covers it, and whether the frame cuts it off. It is asked for JSON only, never masks:
  a lite model writing masks ran out of tokens after 90 s (`ai_bad_output`). Live it answers in 2-3 s.
  Gemini 2.5 models are closed to new API keys, and current text models cannot draw masks, so the pixels
  come from a cutter.
- **`gemini_matte`** (default): Nano Banana repaints everything but the product green. We key the green and
  refuse the result if the repaint does not line up with the photo. Needs billing on the Google project.
- **`bria`**: Bria RMBG background removal, about $0.018 per photo (one call). Its key goes in Vault
  through **AI provayderlar → Bria** like the Gemini key. Plain RMBG keeps a holding hand as foreground, so
  the call carries a prompt ("only the product, not the hand"); with a prompt Bria works asynchronously, and
  the worker polls its status URL (about 15 s). The worker needs outbound HTTPS to
  `engine.prod.bria-api.com` (API and status) and `temp.bria.ai` (the result image, fetched without the
  key). The cutter gets a copy of up to 2048 px.
- **`gemini_json`**: only for keys that still have a segmenting model.

The cutter's mask must agree with the analyst's box, or the job fails with `scene_mask_unusable`.

## AI scene providers (console: "AI sahna provayderi")

Who paints an AI scene's background. Each provider uses its own key from **AI provayderlar** (Gemini's or
Bria's); the analyst and the QA check always use Gemini's (the free lite model is enough).

| | Gemini (Nano Banana, `gemini`) | Bria (Fibo 2, `bria`) |
| --- | --- | --- |
| What it gets | The grey-placeholder canvas and the locked-product rules | The same canvas as a PNG whose alpha is the product, and one plain English scene description (template + style, at most 110 words) |
| Endpoint | Gemini API, image model from the console | `POST /v2/image/edit/replace_background`, `mode: "fibo2"`, `original_quality: true`, async + polling |
| Product | Redrawn by the model, then our pixels pasted back (drift check) | Pasted back by Bria and again by us; live drift 0 |
| Finger bites | Fills up to `max_occluded_pct` (marked for review) | Cannot fill: refused with `scene_occlusion_not_fillable` |
| Needs | Billing on the Google project | The Bria key (the same one as the cutter) |
| Cost | About $0.04-0.11 per image (tokens) | $0.032 per image (`price_bria_scene_call`) |
| Time | 10-40 s | About 20 s |

Live on the owner's photos (Bria cut-out, Bria scene, Gemini QA): 3 of 4 accepted on the first attempt, the
product interior identical in all; the QA check refused one with an extra bottle in the scene. A jar keeps a
faint pink fringe where the cut-out met the fingers. Gemini has not been compared yet (no billing).

## Template previews, typed scenes and saved templates

- **Previews.** Every template card in the scene dialog shows a 4:5 sample picture. It is the bundled sample
  product (`assistant/scenes/assets/sample-product.webp`, the owner's green bottle, cut out once) placed in the
  scene. Studio previews are drawn at once and for free, and redrawn when the colours change. AI previews cost
  one image call each (Bria: $0.032), are made only when someone presses **Namuna yaratish** (or **Yo‘q
  namunalarni yaratish** on the scenes page), run as jobs of kind `preview` (in the budget, one at a time, one
  pending per template), and are cleared when the prompt changes. A failed preview shows its reason on the
  template page.
- **Typed scenes.** In the AI tab staff can pick **O‘z sahnasi** and describe the scene in any language (at most
  400 characters). The worker turns it into an English scene description with the analyst model (one cheap text
  call) and refuses one that asks for people, hands, text or brands (`scene_custom_refused`). Everything else is
  as for a template: the product rule, the checks, the budget.
- **Saved templates.** A finished typed scene can be saved as a template for everyone (**Shablon sifatida
  saqlash**, a name). It gets the English description, goes last in the list, and its preview is the scene the
  staff member made. The console marks it "xodim saqlagan"; edit or switch it off there like any template.

## Banner images (the banner dialog's «AI bilan yaratish»)

- **What.** A banner's image made by the AI scene provider: 1-3 products in a scene (each from its main photo,
  cut out and pasted back unchanged, as for product scenes) or a scene alone. The image is exactly 2.15:1, the
  app's banner (no crop on phones); the website's 2:1 banner shows its middle, and every product stands inside
  that middle, on the right. The lower left stays calm for the title the website and the app draw over it.
- **How.** Made at 21:9 (Gemini: `aspect_ratio` 21:9; Bria: replace-background on our 21:9 canvas with products,
  text-to-image `/v2/image/generate` at 21:9 and 1 MP without), cropped to the middle 2.15:1. A text check (the
  analyst model) rejects text, people, logos and stray products; one retry.
- **Cost.** Per banner with Bria: one cut-out per product (the cutter) plus one scene image, two with a retry;
  Gemini: the analyst per product, the image call, the check. Same budgets, daily limit and one-at-a-time worker
  as scenes (a banner with three products takes up to a few minutes; the task's limit is 450 s).
- **Use.** Staff check it in the dialog against the banner's real frames (app and website, with the title) and
  save the banner with it; the image goes through the banner upload path (variants included). An unused image
  expires with the scenes' candidates.

## What it does

Staff photograph products on a phone, often in an office and sometimes holding the product. The pipeline builds a clean scene around the product. The product itself is never redrawn.

1. **Segment.** One Gemini call (the existing content-model port, `integrations/segmentation/gemini_json.py`) returns:
   - the product's box and mask;
   - hand regions;
   - whether a hand covers the product.

   Hands, sleeves, desks, monitors and the background are excluded from the mask. Enclosed holes in the mask, such as label letters, are filled, except where a hand is.
2. **Occlusion.** The server measures the part of the product a hand hides. With a hand mask: the convex hull of the visible mask, intersected with the hands, minus the visible part. With a cutter (it drops the fingers with the background): the bites in the silhouette, i.e. a morphological closing over 5 % of the product's long side minus the visible part. A bottle's neck is wider than a finger's bite, so it is not counted (the convex hull counted 4.8 %). The analyst's own percentage is used only when there is no pixel mask (screen-fit).
   - Known limit: tuned on the first real photos (a thumb's bite 0.67 %, a neck 0 %). A finger wider than about 10 % of the product's long side, or one lying across the whole product, leaves a gap the closing does not bridge, so it is not counted; staff see it in the review before accepting.
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
   - **AI templates, Gemini:** Nano Banana receives the canvas (the product on a grey placeholder) with a locked-product prompt: no hands, people, office or text, and a contact shadow at the baseline.
   - **AI templates, Bria:** Bria receives the canvas with the product's alpha and builds only the background (see "AI scene providers").
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

1. `migrate` (catalog `0013_productimage_scenes`, assistant `0005_scenes`, `0006_seed_scenes`,
   `0007_scene_provider`). The deploy runs it (`backend-migrate`).
2. Restart the API, the worker and beat (new beat entry `assistant-scenes-housekeeping`, hourly).
3. For Gemini AI scenes (or the Gemini matte cutter), enable billing on the Google AI Studio project of the
   Gemini key: image models answer 429 without it. Bria needs only its key.
4. In the console, set the analyst model (`gemini-3.5-flash-lite` by default) and check the prices against
   Google's and Bria's pages.
5. Optional: put the Bria key in **AI provayderlar → Bria → Kalit** and press "Test"; choose `bria` as the
   cut-out source and, without Google billing, as the AI scene provider.
6. Switch on **sahnalar yoqilgan** (fitting and studio scenes), then **AI sahnalar yoqilgan** after a test.

There are no `.env` changes: keys live in Vault, and everything else is in the console.
