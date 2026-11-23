"""The ops console's sign-in: password → TOTP second factor (or first-time enrolment) → verified session.

* ``access`` — manager-granted console access, the enrolment window and its four-eyes rule.
* ``events`` — the console's audit trail (``channel=ops_console``).
* ``forms`` — the TOTP and recovery-code forms.
* ``state`` — the session keys of a sign-in in progress and of a console session (login and idle stamps).
* ``views`` / ``urls`` — the Unfold-styled steps, mounted ahead of ``admin.site.urls`` by ``config.urls``.
* ``middleware`` — the idle and absolute limits of a console session.

``glowderma.core.admin_site.OpsAdminSite.has_permission`` enforces the verified session on every console
view; nothing here relies on a view remembering to check it. The TOTP devices and recovery codes live in
``glowderma.accounts.second_factor`` (the admin panel's optional MFA shares them).
"""
