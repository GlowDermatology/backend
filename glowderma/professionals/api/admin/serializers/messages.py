"""The refusal texts of the admin ``professionals`` serializers (Uzbek source, translated by the console
catalog)."""

from __future__ import annotations

from glowderma.core.uzsource import N_

MESSAGES = {
    "clock": N_("Vaqtni SS:DD ko‘rinishida kiriting."),
    "grid": N_("Vaqt 5 daqiqalik qadamda bo‘lsin."),
    "duration_grid": N_("Davomiylik 5 daqiqaga karrali bo‘lsin."),
    "end_before_start": N_("Tugash vaqti boshlanishidan keyin bo‘lsin."),
    "break_pair": N_("Tanaffusning boshlanishi va tugashini birga kiriting."),
    "break_outside": N_("Tanaffus ish vaqti ichida bo‘lsin."),
    "hours_pair": N_("Boshlanish va tugash vaqtini birga kiriting (ikkalasi bo‘sh bo‘lsa, dam olish kuni)."),
    "weekday_twice": N_("Bu hafta kuni ikki marta berilgan."),
    "staff_not_found": N_("Xodim topilmadi."),
    "staff_taken": N_("Bu xodim boshqa mutaxassisga bog‘langan."),
    "slot_step": N_("Ruxsat etilgan qadamlar: 10, 15, 20, 30 yoki 60 daqiqa."),
    "price_max": N_("Narx juda katta."),
    "override_past": N_("O‘tgan sanani tanlab bo‘lmaydi."),
    "override_far": N_("Ko‘pi bilan bir yil oldinga kiritish mumkin."),
    "block_past": N_("Tugagan vaqtni band qilib bo‘lmaydi."),
    "block_long": N_("Ko‘pi bilan 366 kunlik davr."),
    "range": N_("Oxirgi sana boshlang‘ich sanadan oldin bo‘lmasin."),
    "range_far": N_("Sana bugundan ko‘pi bilan 10 yil uzoqlikda bo‘lsin."),
    "decimals": N_("Ko‘pi bilan 6 ta kasr raqam."),
}


__all__ = [
    "MESSAGES",
]
