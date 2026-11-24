"""OpenAPI declarations for customer inbox routes (customer contract §2.12): the feed with its unread badge,
read one / all, and the delivery acknowledgement of the app's push handler. Every query starts from the
signed-in customer and stays inside the history horizon; another customer's row answers 404 (read) or is
ignored (ack). The three POSTs take an optional ``Idempotency-Key`` (customer contract §1.7: absolute writes,
a retry converges)."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema

from glowderma.core.api.schema import (
    ACCEPT_LANGUAGE_HEADER,
    CUSTOMER_ERRORS,
    DEVICE_HEADERS,
    OPTIONAL_IDEMPOTENCY_KEY_HEADER,
)
from glowderma.notifications.api.customer.serializers import (
    AckRequestSerializer,
    AckSerializer,
    InboxItemSerializer,
    ReadAllSerializer,
    ReadSerializer,
    UnreadCountSerializer,
)

HEADERS = [*DEVICE_HEADERS, ACCEPT_LANGUAGE_HEADER]

WRITE_HEADERS = [*HEADERS, OPTIONAL_IDEMPOTENCY_KEY_HEADER]

INBOX_LIST = extend_schema(
    operation_id="customer_notifications_list",
    description=(
        "The inbox, newest first, inside the history horizon; also the rows whose push could not be "
        "sent. `unread_count` is the badge."
    ),
    parameters=HEADERS,
    responses={200: InboxItemSerializer(many=True), **CUSTOMER_ERRORS},
)

UNREAD_COUNT_GET = extend_schema(
    operation_id="customer_notifications_unread_count",
    description="The unread badge (polling).",
    parameters=HEADERS,
    responses={200: UnreadCountSerializer, **CUSTOMER_ERRORS},
)

READ_POST = extend_schema(
    operation_id="customer_notifications_read",
    description="Mark one row read (idempotent); reading implies delivery (counted like an ack).",
    parameters=WRITE_HEADERS,
    request=None,
    responses={200: ReadSerializer, **CUSTOMER_ERRORS},
)

READ_ALL_POST = extend_schema(
    operation_id="customer_notifications_read_all",
    description="Mark every unread row read (each acknowledged first).",
    parameters=WRITE_HEADERS,
    request=None,
    responses={200: ReadAllSerializer, **CUSTOMER_ERRORS},
)

ACK_POST = extend_schema(
    operation_id="customer_notifications_ack",
    description=(
        "Delivery acknowledgement from the push handler (1..50 ids). Unknown ids and ids of other "
        "customers are ignored silently; a retry never counts twice."
    ),
    parameters=WRITE_HEADERS,
    request=AckRequestSerializer,
    responses={200: AckSerializer, **CUSTOMER_ERRORS},
)


__all__ = [
    "ACK_POST",
    "HEADERS",
    "INBOX_LIST",
    "READ_ALL_POST",
    "READ_POST",
    "UNREAD_COUNT_GET",
    "WRITE_HEADERS",
]
