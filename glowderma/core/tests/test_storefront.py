"""``core.storefront``: a change is sent only once its transaction commits, coalesced per tag set and window,
and due at the window's end (so a write committed inside a window is covered by that window's call)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from django.db import transaction
from django.db.models.signals import post_save

from glowderma.catalog.models import Category
from glowderma.catalog.tests.factories import CategoryFactory
from glowderma.core import storefront, timeutils
from glowderma.core.models import OutboxMessage

pytestmark = pytest.mark.django_db
T0 = datetime(2026, 11, 28, 10, 0, 0, 500_000, tzinfo=timeutils.TASHKENT)  # half a second into a window


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[float], None]:
    state = {"now": T0}
    monkeypatch.setattr(timeutils, "now", lambda: state["now"])

    def move(seconds: float) -> None:
        state["now"] = T0 + timedelta(seconds=seconds)

    return move


def messages() -> list[OutboxMessage]:
    return list(OutboxMessage.objects.filter(topic=storefront.TOPIC).order_by("id"))


def test_unknown_tags_are_refused() -> None:
    with pytest.raises(ValueError, match="shop:everything"):
        storefront.changed("shop:everything")


def test_nothing_is_sent_before_the_commit_and_one_message_after_it(
    clock: Callable[[float], None], django_capture_on_commit_callbacks: Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        with transaction.atomic():
            storefront.changed(storefront.HOME, storefront.PRODUCTS)
            assert messages() == []
    (message,) = messages()
    assert message.payload == {"tags": ["shop:home", "shop:products"]}
    # Due at the end of the 2-second window that holds T0 (10:00:00.5 → 10:00:02.0).
    assert message.available_at == T0.replace(second=2, microsecond=0)


def test_a_rolled_back_write_sends_nothing(django_capture_on_commit_callbacks: Any) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                storefront.changed(storefront.BANNERS)
                raise RuntimeError("rolled back")
    assert messages() == []


def test_writes_in_one_window_share_a_message_per_tag_set(
    clock: Callable[[float], None], django_capture_on_commit_callbacks: Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        storefront.changed(storefront.PRODUCTS)
    clock(1.0)  # 10:00:01.5, the same window
    with django_capture_on_commit_callbacks(execute=True):
        storefront.changed(storefront.PRODUCTS)
        storefront.changed(storefront.CATEGORIES, storefront.PRODUCTS)  # another tag set
    assert [m.payload["tags"] for m in messages()] == [
        ["shop:products"],
        ["shop:categories", "shop:products"],
    ]
    clock(2.0)  # 10:00:02.5, the next window: a new message, due at its own end
    with django_capture_on_commit_callbacks(execute=True):
        storefront.changed(storefront.PRODUCTS)
    assert len(messages()) == 3
    assert messages()[-1].available_at == T0.replace(second=4, microsecond=0)


def test_a_window_is_never_delivered_before_its_writes(
    clock: Callable[[float], None], django_capture_on_commit_callbacks: Any
) -> None:
    """The dispatcher claims only due messages: during the window the message waits for later writes."""
    from glowderma.core.tasks import _claim_outbox_message

    with django_capture_on_commit_callbacks(execute=True):
        storefront.changed(storefront.HOME)
    assert _claim_outbox_message() is None
    clock(1.6)  # 10:00:02.1: the window has ended
    claimed = _claim_outbox_message()
    assert claimed is not None and claimed.topic == storefront.TOPIC


def test_a_watched_model_sends_its_tags_but_a_fixture_load_does_not(
    django_capture_on_commit_callbacks: Any,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        category = CategoryFactory.create()
    assert {tag for m in messages() for tag in m.payload["tags"]} == {
        "shop:categories",
        "shop:products",
        "shop:banners",
    }
    OutboxMessage.objects.filter(topic=storefront.TOPIC).delete()
    with django_capture_on_commit_callbacks(execute=True):
        post_save.send(sender=Category, instance=category, created=False, raw=True, using="default")
    assert messages() == []
