"""Brute-force protection primitives shared by the admin API, the ops console, the panel MFA and the
customer sign-in: the per-account lockout ladder, the per-address failure budget, the atomic counter both
count with, and the canonical identifiers the throttles and guards key on. Nothing here imports the API,
console or services layers."""
