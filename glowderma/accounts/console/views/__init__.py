"""The console's second step and its exits, Unfold-styled like the login page.

``verify`` (TOTP code), ``recovery`` (one-time recovery code) and ``enrol`` (first TOTP device: QR + manual
key, confirmed by a code, then the ten recovery codes shown once) all need a pending sign-in from the password
step and share the ``django_admin_otp`` lockout (LoginGuard: five failures a minute lock the user out for
1, 2, 4, 8, then 15 minutes) plus a per-address budget (``security.address_budget.IpFailureBudget``).
``cancel`` drops a pending sign-in, ``logout`` ends a console session.

Enrolment needs an open enrolment window (``access.enrolment_window``: a grant or a 2FA reset opened it, the
same manager did not also reset the password); otherwise the pending sign-in ends with a message to ask a
manager. Its recovery codes are shown by a separate GET page (``recovery_codes``: POST → redirect → GET),
exactly once from the session, so a reload never re-posts the enrolment form.

``csrf_failure`` is ``CSRF_FAILURE_VIEW``: the console's own 403 page (in the console's language) instead
of Django's English one.

The package is a facade over ``password`` (the login step), ``verify``, ``recovery``, ``enrolment``,
``session`` (cancel, logout) and ``csrf``; ``code_steps`` and ``rendering`` hold what the steps share.
"""

from glowderma.accounts.console.views.csrf import csrf_failure
from glowderma.accounts.console.views.enrolment import enrol, recovery_codes
from glowderma.accounts.console.views.password import ConsolePasswordView, guarded_admin_login
from glowderma.accounts.console.views.recovery import recovery
from glowderma.accounts.console.views.session import cancel, logout
from glowderma.accounts.console.views.verify import verify

__all__ = [
    "ConsolePasswordView",
    "cancel",
    "csrf_failure",
    "enrol",
    "guarded_admin_login",
    "logout",
    "recovery",
    "recovery_codes",
    "verify",
]
