"""Screen-shaped copies of a product photo: a 4:5 portrait (product cards, product page, cart) and a square
(home rails, feed, the app's cards, thumbnails).

A rendition is a crop of the stored image, ``crop = [x, y, w, h]`` normalised to the image's size, stored as
``{"crop": [...], "mode": "auto" | "manual", "name": "<storage name>"}`` under its ratio key in
``ProductImage.renditions``. Its files sit next to the original: ``<stem>_r45-<hash>.webp`` (or ``_r11-``),
plus the usual ``_1000`` / ``_400`` variants (``core.variants.store_variants``). The hash covers the
original's bytes and the crop, so a changed crop gets new, cache-safe URLs.

:func:`auto_crop` frames a product box (normalised ``[x0, y0, x1, y1]``) with a margin, centred, inside the
image; without a box it is the largest centred crop.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from io import BytesIO
from typing import Any, Final, Literal

from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from PIL import Image

from glowderma.core.media import url_fields
from glowderma.core.variants import delete_with_variants, store_variants

type Ratio = Literal["4:5", "1:1"]
type Crop = tuple[float, float, float, float]

# width / height of each shape, and the tag in its file names
RATIOS: Final[dict[Ratio, tuple[float, str]]] = {"4:5": (0.8, "r45"), "1:1": (1.0, "r11")}
MAX_SIDE: Final[int] = 2000  # long side of the stored rendition (its variants: 1000 and 400)
MIN_SIDE: Final[int] = 200  # px of the source a crop must keep on its short side
RATIO_TOLERANCE: Final[float] = 0.02
MARGIN: Final[float] = 0.14  # space kept around the product box on each side (share of the box)


class CropError(ValueError):
    """``code``: ``invalid_crop`` (outside the image, wrong shape) or ``crop_too_small``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def as_ratio(value: str) -> Ratio:
    """``value`` as a :data:`Ratio` (``KeyError`` for an unknown shape)."""
    if value == "4:5":
        return "4:5"
    if value == "1:1":
        return "1:1"
    raise KeyError(value)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def auto_crop(ratio: Ratio, width: int, height: int, box: Any = None) -> Crop:
    """The crop of a ``width × height`` image for ``ratio``: the product box with :data:`MARGIN` around it,
    centred on the box and kept inside the image (the largest centred crop without a box)."""
    target = RATIOS[ratio][0]
    if not _valid_box(box):
        crop_w = min(width, height * target)
        crop_h = crop_w / target
        return _normalised((width - crop_w) / 2, (height - crop_h) / 2, crop_w, crop_h, width, height)
    x0, y0, x1, y1 = (float(v) for v in box)
    bw, bh = (x1 - x0) * width, (y1 - y0) * height
    need_w = bw * (1 + 2 * MARGIN)
    need_h = bh * (1 + 2 * MARGIN)
    crop_w = max(need_w, need_h * target)
    crop_h = crop_w / target
    if crop_w > width:  # the image is too narrow: the widest crop that fits
        crop_w, crop_h = width, width / target
    if crop_h > height:
        crop_h, crop_w = height, height * target
    cx, cy = (x0 + x1) / 2 * width, (y0 + y1) / 2 * height
    left = _clamp(cx - crop_w / 2, 0, width - crop_w)
    top = _clamp(cy - crop_h / 2, 0, height - crop_h)
    return _normalised(left, top, crop_w, crop_h, width, height)


def _valid_box(box: Any) -> bool:
    if not isinstance(box, list | tuple) or len(box) != 4:
        return False
    if not all(isinstance(v, int | float) for v in box):
        return False
    x0, y0, x1, y1 = (float(v) for v in box)
    return 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1


def _normalised(left: float, top: float, crop_w: float, crop_h: float, width: int, height: int) -> Crop:
    return (
        round(left / width, 5),
        round(top / height, 5),
        round(crop_w / width, 5),
        round(crop_h / height, 5),
    )


def check_crop(ratio: Ratio, crop: Any, width: int, height: int) -> Crop:
    """A staff crop: inside the image, the ratio's shape (±2 %), at least :data:`MIN_SIDE` px."""
    if (
        not isinstance(crop, list | tuple)
        or len(crop) != 4
        or not all(isinstance(v, int | float) for v in crop)
    ):
        raise CropError("invalid_crop")
    x, y, w, h = (float(v) for v in crop)
    slack = 0.002
    if w <= 0 or h <= 0 or x < -slack or y < -slack or x + w > 1 + slack or y + h > 1 + slack:
        raise CropError("invalid_crop")
    pixel_w, pixel_h = w * width, h * height
    if abs(pixel_w / pixel_h - RATIOS[ratio][0]) > RATIO_TOLERANCE * RATIOS[ratio][0]:
        raise CropError("invalid_crop")
    if min(pixel_w, pixel_h) < MIN_SIDE:
        raise CropError("crop_too_small")
    x, y = _clamp(x, 0, 1 - w), _clamp(y, 0, 1 - h)
    return (round(x, 5), round(y, 5), round(min(w, 1.0), 5), round(min(h, 1.0), 5))


def render(content: bytes, ratio: Ratio, crop: Crop) -> bytes:
    """The WebP (q88) of ``crop`` of the image ``content``, exactly the ratio's shape, long side ≤
    :data:`MAX_SIDE`."""
    with Image.open(BytesIO(content)) as source:
        source.load()
        image = source.convert("RGB")
    width, height = image.size
    x, y, w, h = crop
    left, top = round(x * width), round(y * height)
    crop_w = max(1, round(w * width))
    crop_h = max(1, round(crop_w / RATIOS[ratio][0]))
    crop_h = min(crop_h, height - top)
    piece = image.crop((left, top, left + crop_w, top + crop_h))
    target_w = min(crop_w, round(MAX_SIDE * RATIOS[ratio][0]) if RATIOS[ratio][0] < 1 else MAX_SIDE)
    target_h = round(target_w / RATIOS[ratio][0])
    piece = piece.resize((target_w, target_h), Image.Resampling.LANCZOS)
    output = BytesIO()
    piece.save(output, format="WEBP", quality=88, method=5)
    return output.getvalue()


def rendition_name(original: str, ratio: Ratio, crop: Crop, digest: str) -> str:
    """``products/1/ab.jpg`` → ``products/1/ab_r45-<8 hex>.webp``."""
    head, slash, segment = original.rpartition("/")
    stem = segment.rpartition(".")[0] or segment
    tag = sha256(f"{digest}|{ratio}|{crop}".encode()).hexdigest()[:8]
    return f"{head}{slash}{stem}_{RATIOS[ratio][1]}-{tag}.webp"


def store(storage: Storage, original: str, content: bytes, ratio: Ratio, crop: Crop, digest: str) -> str:
    """Write the rendition and its variants (kept if already there); return its storage name."""
    name = rendition_name(original, ratio, crop, digest)
    if not storage.exists(name):
        data = render(content, ratio, crop)
        saved = storage.save(name, ContentFile(data))
        if saved != name:  # a concurrent writer: same crop, same bytes
            storage.delete(saved)
        store_variants(storage, name, data)
    return name


def delete(storage: Storage, renditions: Mapping[str, Any]) -> None:
    for entry in renditions.values():
        if isinstance(entry, dict) and entry.get("name"):
            delete_with_variants(storage, str(entry["name"]))


def names(renditions: Mapping[str, Any]) -> set[str]:
    return {str(e["name"]) for e in renditions.values() if isinstance(e, dict) and e.get("name")}


# the API's names of the shapes (customer and admin APIs)
API_NAMES: Final[dict[Ratio, str]] = {"4:5": "portrait", "1:1": "square"}
FROM_API: Final[dict[str, Ratio]] = {name: ratio for ratio, name in API_NAMES.items()}


def api_urls(renditions: Mapping[str, Any] | None) -> dict[str, dict[str, str | None] | None]:
    """``{"portrait": {url, medium_url, thumbnail_url} | None, "square": …}`` (the API's shape)."""
    renditions = renditions or {}
    out: dict[str, dict[str, str | None] | None] = {}
    for ratio, api_name in API_NAMES.items():
        entry = renditions.get(ratio)
        name = entry.get("name") if isinstance(entry, dict) else None
        out[api_name] = url_fields("", str(name)) if name else None
    return out


def api_detail(renditions: Mapping[str, Any] | None) -> dict[str, dict[str, Any] | None]:
    """The admin API's shape: the URLs plus ``crop`` and ``mode`` per shape."""
    renditions = renditions or {}
    out: dict[str, dict[str, Any] | None] = {}
    for ratio, api_name in API_NAMES.items():
        entry = renditions.get(ratio)
        if not isinstance(entry, dict) or not entry.get("name"):
            out[api_name] = None
            continue
        out[api_name] = {
            "crop": list(entry.get("crop") or []),
            "mode": entry.get("mode") or "auto",
            **url_fields("", str(entry["name"])),
        }
    return out
