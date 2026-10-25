"""OAuth providers for staff social sign-in (contract 2.1, milestone M0b).

:mod:`ports` holds the provider protocol and the claims it returns, :mod:`fake` the deterministic
in-process providers used by tests and local end-to-end runs, :mod:`google` and :mod:`facebook` the
live adapters, and :mod:`registry` picks one by name and ``settings.OAUTH_MODE``.
"""
