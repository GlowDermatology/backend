"""Admin API errors of the staff invite and forced password change (feat/admin-ux, admin contract §1.6).

Both are raised with their Uzbek source text; the admin error envelope renders ``code`` and ``message``.
"""

from rest_framework import exceptions

from glowderma.core.uzsource import N_

PASSWORD_CHANGE_REQUIRED = N_("Avval parolingizni o‘zgartiring.")
PASSWORD_TOKEN_INVALID = N_("Havola yaroqsiz yoki muddati o‘tgan. Menejerdan yangi havola so‘rang.")


class PasswordChangeRequired(exceptions.APIException):
    """403: the account must set its own password before the admin API answers anything but the allow-list
    (``allow_password_change_pending`` views: ``auth/me/``, ``auth/change-password/``,
    ``auth/password-link/``; ``auth/logout/`` needs no bearer)."""

    status_code = 403
    default_code = "password_change_required"
    default_detail = PASSWORD_CHANGE_REQUIRED


class PasswordTokenInvalid(exceptions.APIException):
    """400: one uniform answer for an unknown, used, revoked or expired password link, or one of an inactive
    account — the caller learns nothing about which."""

    status_code = 400
    default_code = "token_invalid"
    default_detail = PASSWORD_TOKEN_INVALID


# --- admin panel MFA (feat/admin-ux, admin contract §2.1 "MFA") ---------------------------------------------

MFA_CODE_INVALID = N_("Kod noto‘g‘ri yoki allaqachon ishlatilgan. Ilovadagi yangi kodni kiriting.")
MFA_CHALLENGE_INVALID = N_("Kirish muddati tugadi yoki bekor qilindi. Email va parolni qaytadan kiriting.")


class MfaCodeInvalid(exceptions.APIException):
    """400: the TOTP or recovery code is wrong, already used, or not of this account's admin panel device
    (one answer for every reason)."""

    status_code = 400
    default_code = "mfa_code_invalid"
    default_detail = MFA_CODE_INVALID


class MfaChallengeInvalid(exceptions.APIException):
    """400: the sign-in challenge of ``auth/mfa/verify/`` is unknown, expired (5 min), already used, out of
    attempts, or died with a password change, a deactivation or an MFA reset: sign in again."""

    status_code = 400
    default_code = "mfa_challenge_invalid"
    default_detail = MFA_CHALLENGE_INVALID
