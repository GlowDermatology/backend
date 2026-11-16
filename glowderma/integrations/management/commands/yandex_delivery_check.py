"""Ask Yandex Delivery what the warehouse point offers: ``tariffs`` and a ``check-price`` per class (§3.2).

Read-only at Yandex (no claim is created). The class names found are stored in
``IntegrationState(yandex).data["tariffs"]`` (with ``tariffs_checked_at``, ``tariffs_mode`` and
``tariffs_point``), which the warehouse settings consult before a method may be enabled in live mode. The
warehouse point comes from the options because the command must work before the warehouse is configured.
Exit status 1 when Yandex refuses or cannot be reached (nothing is stored when ``tariffs`` fails).
"""

from __future__ import annotations

import sys
from decimal import Decimal
from typing import Any, NoReturn

from django.core.management.base import BaseCommand, CommandError, CommandParser

from glowderma.core import timeutils
from glowderma.integrations import services
from glowderma.integrations.yandex_delivery.ports import ParcelItem, Point, YandexError
from glowderma.integrations.yandex_delivery.registry import get_yandex_delivery, yandex_delivery_mode

PROVIDER = "yandex"
# The checkout classes (Standard = courier, Express = express); cargo is never offered to customers.
CHECKED_CLASSES = ("courier", "express")
# Without --to-*, the price is probed towards a point about 1.1 km north of the warehouse.
PROBE_OFFSET_DEGREES = 0.01


def _latitude(value: str) -> float:
    number = float(value)
    if not -90 <= number <= 90:
        raise ValueError("latitude out of range")
    return number


def _longitude(value: str) -> float:
    number = float(value)
    if not -180 <= number <= 180:
        raise ValueError("longitude out of range")
    return number


def _weight(value: str) -> Decimal:
    try:
        weight = Decimal(value)
    except ArithmeticError as exc:
        raise ValueError("not a number") from exc
    if not weight.is_finite() or weight <= 0:
        raise ValueError("weight must be positive")
    return weight


def _describe(error: YandexError) -> str:
    code = getattr(error, "code", "")
    return f"{type(error).__name__} {code}: {error}".replace("  ", " ")


class Command(BaseCommand):
    help = "Read the Yandex Delivery tariffs and check-price at the warehouse point; store the classes found"

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--lat", type=_latitude, required=True, help="warehouse latitude")
        parser.add_argument("--lon", type=_longitude, required=True, help="warehouse longitude")
        parser.add_argument("--address", required=True, help="warehouse full address (city, street, house)")
        parser.add_argument("--to-lat", type=_latitude, help="probe destination latitude")
        parser.add_argument("--to-lon", type=_longitude, help="probe destination longitude")
        parser.add_argument("--to-address", help="probe destination full address")
        parser.add_argument("--weight-kg", type=_weight, default=Decimal("1"), help="probe parcel weight")

    def handle(self, *args: Any, **options: Any) -> None:
        address = str(options["address"]).strip()
        if not address:
            raise CommandError("--address must not be empty")
        if (options["to_lat"] is None) != (options["to_lon"] is None):
            raise CommandError("--to-lat and --to-lon go together")
        warehouse = Point(lon=options["lon"], lat=options["lat"], fullname=address)
        destination = Point(
            lon=options["to_lon"] if options["to_lon"] is not None else warehouse.lon,
            lat=options["to_lat"] if options["to_lat"] is not None else warehouse.lat + PROBE_OFFSET_DEGREES,
            fullname=str(options["to_address"] or address).strip(),
        )
        mode = yandex_delivery_mode()
        adapter = get_yandex_delivery()
        self.stdout.write(f"mode={mode}")
        try:
            tariffs = adapter.tariffs(warehouse)
        except YandexError as exc:
            self.fail(f"tariffs failed: {_describe(exc)}")
        names = [tariff.name for tariff in tariffs]
        self.stdout.write("tariffs: " + (", ".join(names) or "(none)"))
        failed = False
        item = ParcelItem(title="probe", weight_kg=options["weight_kg"])
        for taxi_class in CHECKED_CLASSES:
            if taxi_class not in names:
                self.stdout.write(f"check-price {taxi_class}: not offered at this point")
                continue
            try:
                estimate = adapter.check_price(warehouse, destination, item, taxi_class)
            except YandexError as exc:
                failed = True
                self.stdout.write(f"check-price {taxi_class}: {_describe(exc)}")
                continue
            self.stdout.write(
                f"check-price {taxi_class}: {estimate.price} {estimate.currency}"
                f" (raw {estimate.raw_price}), eta {estimate.eta_minutes} min,"
                f" distance {estimate.distance_m} m, zone {estimate.zone_id or '-'}"
            )
        services.update_provider_data(
            PROVIDER,
            {
                "tariffs": names,
                "tariffs_checked_at": timeutils.now().isoformat(),
                "tariffs_mode": mode,
                "tariffs_point": [warehouse.lon, warehouse.lat],
            },
        )
        self.stdout.write('stored IntegrationState(yandex).data["tariffs"]')
        if failed:
            sys.exit(1)

    def fail(self, message: str) -> NoReturn:
        self.stderr.write(message)
        sys.exit(1)
