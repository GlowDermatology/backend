from django.apps import AppConfig


class LoyaltyConfig(AppConfig):
    name = "glowderma.loyalty"

    def ready(self) -> None:
        from glowderma.core import readiness
        from glowderma.loyalty import selectors

        # health/?ready=1 (admin §2.15): core cannot import loyalty, so the check is registered here
        readiness.register_cashback_policy_check(selectors.cashback_policy_missing)
