"""The ops console's sign-in: password → TOTP second factor (or first-time enrolment) → verified session.

* ``twofactor`` — TOTP devices (django-otp), enrolment, the QR code and the one-time recovery codes.
* ``state`` — the session keys of a sign-in in progress and of a console session (login and idle stamps).
* ``views`` / ``urls`` — the Unfold-styled steps, mounted ahead of ``admin.site.urls`` by ``config.urls``.
* ``middleware`` — the idle and absolute limits of a console session.

``glowderma.core.admin_site.OpsAdminSite.has_permission`` enforces the verified session on every console
view; nothing here relies on a view remembering to check it.
"""
