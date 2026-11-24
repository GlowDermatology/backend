"""The refusal texts of the admin ``appointments`` serializers."""

from __future__ import annotations

from glowderma.core.uzsource import N_

MESSAGES = {
    "specialist_not_found": N_("Mutaxassis topilmadi."),
    "service_not_found": N_("Xizmat topilmadi."),
    "customer_not_found": N_("Bu raqam bilan mijoz topilmadi."),
    "walk_in_name": N_("Bu raqam bilan mijoz topilmadi: yangi mijozning ismini kiriting."),
    "customer_inactive": N_("Bu raqamdagi mijoz hisobi faol emas."),
    "customer_id_not_found": N_("Mijoz topilmadi."),
    "one_customer_ref": N_("customer_id yoki phone dan faqat bittasini yuboring."),
}


__all__ = [
    "MESSAGES",
]
