"""One-time codes and verification tokens (customer contract §2.1).

Hybrid identity: one engine for codes sent by SMS (to a phone) and by e-mail (to an address). Every rule is
keyed by the code's *target* (:class:`~glowderma.accounts.services.otp.targets.OtpTarget`): the per-target
advisory lock, the lockout, the daily caps (codes and wrong codes), the resend window and the rows themselves;
the fid and IP day counters are shared by both channels and each channel has its own global hourly breaker.
Codes are stored as HMACs only.
"""
