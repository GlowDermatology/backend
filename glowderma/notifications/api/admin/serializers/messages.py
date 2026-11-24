"""The refusal texts of the admin ``notifications`` serializers and the placeholder rule of their texts."""

from __future__ import annotations

import re

from glowderma.core.uzsource import N_

# A ``{`` that does not open ``{raqam}`` (the DB CHECK uses the same pattern).
UNKNOWN_PLACEHOLDER = re.compile(r"\{(?!raqam\})")


MESSAGES = {
    "title_min": N_("Sarlavhani kiriting."),
    "title_max": N_("60 belgidan oshmasin."),
    "body_min": N_("Matnni kiriting."),
    "body_max": N_("180 belgidan oshmasin."),
    "product_not_found": N_("Mahsulot topilmadi yoki yashirin."),
    "customer_not_found": N_("Bu raqam bilan mijoz topilmadi."),
    "in_past": N_("Kelajakdagi vaqtni tanlang."),
    "unknown_placeholder": N_("Faqat {raqam} o‘rinbosari ishlatiladi."),
    "label_min": N_("Qurilma nomini kiriting."),
    "label_max": N_("60 belgidan oshmasin."),
}


__all__ = [
    "MESSAGES",
    "UNKNOWN_PLACEHOLDER",
]
