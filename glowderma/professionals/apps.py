from django.apps import AppConfig


class ProfessionalsConfig(AppConfig):
    name = "glowderma.professionals"
    verbose_name = "Klinika jadvali"

    def ready(self) -> None:
        from glowderma.professionals import storefront

        storefront.connect()  # the website's cache tags of specialist and clinic writes (core.storefront)
