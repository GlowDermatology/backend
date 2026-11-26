"""Offline segmenter (tests, ``scene_spike --fake``): the product is whatever differs from the corners.

The background colour is the median of the four corner patches; pixels further than :data:`THRESHOLD` from it
(summed channel difference) are the product, cleaned with an opening. Good enough for studio-like test
photos, useless for a real office photo (that is what the live segmenters are for). :meth:`push` queues a
:class:`.ports.Segmentation` or an exception for tests; ``calls`` records request sizes.
"""

from __future__ import annotations

from collections import deque
from io import BytesIO
from statistics import median

from PIL import Image, ImageChops

from glowderma.core import compose
from glowderma.integrations.secret import Secret

from .ports import NoProduct, Segmentation, SegmentationRequest

THRESHOLD = 60


def corner_colour(image: Image.Image) -> tuple[int, int, int]:
    rgb = image.convert("RGB")
    side = max(2, min(rgb.size) // 20)
    patches = [
        rgb.crop(box).resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
        for box in (
            (0, 0, side, side),
            (rgb.width - side, 0, rgb.width, side),
            (0, rgb.height - side, side, rgb.height),
            (rgb.width - side, rgb.height - side, rgb.width, rgb.height),
        )
    ]
    return tuple(int(median(p[i] for p in patches)) for i in range(3))  # type: ignore[return-value,index]


def foreground(image: Image.Image) -> Image.Image:
    rgb = image.convert("RGB")
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, corner_colour(rgb)))
    red, green, blue = diff.split()
    total = ImageChops.add(ImageChops.add(red, green), blue)
    mask = total.point(lambda v: 255 if v >= THRESHOLD else 0)
    return compose.dilate(compose.erode(mask, 2), 2)


class FakeSegmenter:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []
        self._queue: deque[Segmentation | Exception] = deque()

    def push(self, outcome: Segmentation | Exception) -> None:
        self._queue.append(outcome)

    def reset(self) -> None:
        self.calls.clear()
        self._queue.clear()

    def segment(self, request: SegmentationRequest, *, api_key: Secret) -> Segmentation:
        self.calls.append((request.width, request.height))
        if self._queue:
            outcome = self._queue.popleft()
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        with Image.open(BytesIO(request.image.data)) as source:
            source.load()
            sent = source.convert("RGB")
        mask = compose.binary(
            foreground(sent).resize((request.width, request.height), Image.Resampling.BILINEAR)
        )
        box = mask.getbbox()
        if box is None:
            raise NoProduct("fake: nothing differs from the background")
        return Segmentation(
            product=mask,
            box=box,
            background="plain",
            input_tokens=258,
            output_tokens=200,
            model=request.model,
        )
