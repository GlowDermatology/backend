"""Shapes of the customer appointment routes (customer contract §2.11).

Every i18n field is rendered as one string in the resolved language (``context["lang"]``; fallback the Uzbek
text, then ``""``); enum values come with their ``*_label`` (§5.3).

One subject per module: ``catalogue`` (specialists, services, the clinic), ``slots`` and ``bookings``;
``fields`` holds the translated-field and value helpers.
"""

from glowderma.appointments.api.customer.serializers.bookings import (
    AppointmentServiceSerializer,
    AppointmentSpecialistSerializer,
    BookingSerializer,
    CancelSerializer,
    CustomerAppointmentSerializer,
)
from glowderma.appointments.api.customer.serializers.catalogue import (
    ClinicHoursSerializer,
    ClinicListSerializer,
    ClinicSerializer,
    LocationSerializer,
    ServiceListSerializer,
    ServiceOfferSerializer,
    SpecialistListSerializer,
    SpecialistSerializer,
    SpecialistServiceSerializer,
)
from glowderma.appointments.api.customer.serializers.fields import (
    AnyOffsetDateTimeField,
    coordinate,
    photo_url,
    pick,
    serializer_language,
)
from glowderma.appointments.api.customer.serializers.slots import (
    AvailabilityQuerySerializer,
    FreeSlotsSerializer,
    NextSlotSerializer,
    SlotSerializer,
    SlotsQuerySerializer,
    SlotWorkingHoursSerializer,
    SpecialistAvailabilityListSerializer,
    SpecialistAvailabilitySerializer,
)

__all__ = [
    "AnyOffsetDateTimeField",
    "AppointmentServiceSerializer",
    "AvailabilityQuerySerializer",
    "AppointmentSpecialistSerializer",
    "BookingSerializer",
    "CancelSerializer",
    "ClinicHoursSerializer",
    "ClinicListSerializer",
    "ClinicSerializer",
    "CustomerAppointmentSerializer",
    "FreeSlotsSerializer",
    "LocationSerializer",
    "NextSlotSerializer",
    "ServiceListSerializer",
    "ServiceOfferSerializer",
    "SlotSerializer",
    "SlotWorkingHoursSerializer",
    "SlotsQuerySerializer",
    "SpecialistAvailabilityListSerializer",
    "SpecialistAvailabilitySerializer",
    "SpecialistListSerializer",
    "SpecialistSerializer",
    "SpecialistServiceSerializer",
    "coordinate",
    "photo_url",
    "pick",
    "serializer_language",
]
