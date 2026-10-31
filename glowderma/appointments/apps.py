from django.apps import AppConfig


class AppointmentsConfig(AppConfig):
    name = "glowderma.appointments"
    verbose_name = "Klinika qabullari"

    def ready(self) -> None:
        # ``professionals`` (a lower layer) asks how many open appointments a specialist still has before it
        # deactivates one (admin specialists API, FND-1); only this module can count them.
        from glowderma.appointments import selectors
        from glowderma.professionals import bookings

        bookings.register(selectors.upcoming_counts_by_professional)
