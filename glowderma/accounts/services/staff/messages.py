"""Staff-visible Uzbek messages of the staff services (admin contract §2.1, §2.16), marked with ``N_`` and
translated at the edge by the error envelope.
"""

from __future__ import annotations

from glowderma.core.uzsource import N_

SOCIAL_SUBJECT_LINKED = N_("Bu ijtimoiy hisob allaqachon bog‘langan.")
SOCIAL_PROVIDER_LINKED = N_("Bu provayder allaqachon bog‘langan.")


STAFF_EMAIL_TAKEN = N_("Bu email bilan xodim allaqachon bor (nofaol bo‘lsa, uni qayta faollashtiring).")
STAFF_LAST_MANAGER = N_("Kamida bitta faol menejer qolishi kerak. Avval boshqa xodimni menejer qiling.")
STAFF_SELF_DEACTIVATE = N_("O‘z hisobingizni faolsizlantira olmaysiz.")
STAFF_SELF_DEMOTE = N_("O‘zingizni menejerlikdan chiqara olmaysiz.")
STAFF_SUPERUSER_MANAGER = N_("Superadmin doim menejer bo‘ladi: uni menejerlikdan chiqarib bo‘lmaydi.")
STAFF_SELF_RESET = N_("O‘z parolingizni «Parolni o‘zgartirish» orqali almashtiring.")
STAFF_SUPERUSER_RESET = N_(
    "Superadminga vaqtinchalik parolni faqat superadmin bera oladi. Havolani uning e-pochtasiga yuboring "
    "yoki superadmindan so‘rang."
)


STAFF_EMAIL_INVITE_UNAVAILABLE = N_("E-pochta orqali yuborish sozlanmagan: vaqtinchalik parolni tanlang.")
STAFF_LINK_UNAVAILABLE = N_("E-pochta orqali yuborish sozlanmagan.")
STAFF_LINK_INACTIVE = N_("Nofaol hisobga havola yuborilmaydi. Avval uni qayta faollashtiring.")
STAFF_LINK_SELF = N_("O‘zingizga havola «Parolni o‘zgartirish» sahifasidan so‘raladi.")
STAFF_LINK_NOT_PENDING = N_("Parolingiz o‘rnatilgan: uni «Parolni o‘zgartirish» orqali almashtiring.")
PASSWORD_UNCHANGED = N_("Yangi parol eskisidan farq qilsin.")
PASSWORD_INCORRECT = N_("Joriy parol noto‘g‘ri.")


MFA_SELF_RESET = N_("O‘z ikki bosqichli himoyangizni «Sozlamalar»da o‘chiring.")
MFA_NOT_ENABLED_FOR = N_("Bu xodimda ikki bosqichli himoya yoqilmagan.")
MFA_SUPERUSER_RESET = N_("Superadminning ikki bosqichli himoyasini faqat superadmin o‘chira oladi.")


__all__ = [
    "SOCIAL_SUBJECT_LINKED",
    "SOCIAL_PROVIDER_LINKED",
    "STAFF_EMAIL_TAKEN",
    "STAFF_LAST_MANAGER",
    "STAFF_SELF_DEACTIVATE",
    "STAFF_SELF_DEMOTE",
    "STAFF_SUPERUSER_MANAGER",
    "STAFF_SELF_RESET",
    "STAFF_SUPERUSER_RESET",
    "STAFF_EMAIL_INVITE_UNAVAILABLE",
    "STAFF_LINK_UNAVAILABLE",
    "STAFF_LINK_INACTIVE",
    "STAFF_LINK_SELF",
    "STAFF_LINK_NOT_PENDING",
    "PASSWORD_INCORRECT",
    "PASSWORD_UNCHANGED",
    "MFA_SELF_RESET",
    "MFA_NOT_ENABLED_FOR",
    "MFA_SUPERUSER_RESET",
]
