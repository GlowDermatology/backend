"""The storefront revalidation call (``integrations.storefront``): the live adapter's request and answers, the
registry, and the ``storefront.revalidate`` outbox handler."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
from django.test import override_settings

from glowderma.core import outbox, storefront
from glowderma.core.models import OutboxMessage
from glowderma.integrations.storefront import registry, tasks
from glowderma.integrations.storefront.fake import FakeStorefront
from glowderma.integrations.storefront.live import LiveStorefront
from glowderma.integrations.storefront.ports import StorefrontRefused
from glowderma.integrations.tests.live_fixtures import Script, connect_error

URL = "https://glowderma.test/api/revalidate"
SECRET = "s" * 40


def adapter(script: Script) -> LiveStorefront:
    return LiveStorefront(url=URL, secret=SECRET, transport=script.transport)


@pytest.fixture
def fake() -> Iterator[FakeStorefront]:
    recorder = registry.fake_storefront()
    recorder.clear()
    yield recorder
    recorder.clear()


def test_the_live_call_posts_the_tags_with_the_secret() -> None:
    script = Script().on("POST", "/api/revalidate", httpx.Response(200, json={"revalidated": ["shop:home"]}))
    adapter(script).revalidate(["shop:home", "shop:products"])
    (request,) = script.to("POST", "/api/revalidate")
    assert request.headers["Authorization"] == f"Bearer {SECRET}"
    assert json.loads(request.content) == {"tags": ["shop:home", "shop:products"]}


@pytest.mark.parametrize(
    ("status", "code"), [(400, "validation_error"), (401, "not_authenticated"), (404, "not_found")]
)
def test_a_refusal_no_retry_can_fix_is_permanent(status: int, code: str) -> None:
    script = Script().on("POST", "/api/revalidate", httpx.Response(status, json={"code": code}))
    with pytest.raises(StorefrontRefused, match=f"HTTP {status} {code}"):
        adapter(script).revalidate(["shop:home"])


def test_a_website_that_is_down_is_transient() -> None:
    script = Script().on("POST", "/api/revalidate", httpx.Response(502, text="bad gateway"))
    with pytest.raises(RuntimeError, match="HTTP 502"):
        adapter(script).revalidate(["shop:home"])
    unreachable = Script().on("POST", "/api/revalidate", connect_error)
    with pytest.raises(Exception) as caught:  # noqa: PT011 - the transport failure type is httpclient's
        adapter(unreachable).revalidate(["shop:home"])
    assert not isinstance(caught.value, StorefrontRefused)


def test_the_registry_runs_the_recorder_until_both_settings_are_set() -> None:
    assert not registry.configured()
    assert registry.get_storefront_cache() is registry.fake_storefront()
    with override_settings(STOREFRONT_REVALIDATE_URL=URL, STOREFRONT_REVALIDATE_SECRET=SECRET):
        assert registry.configured()
        live = registry.get_storefront_cache()
        assert isinstance(live, LiveStorefront)
        assert (live.url, live.secret) == (URL, SECRET)


def message(tags: list[str]) -> OutboxMessage:
    return OutboxMessage(topic=storefront.TOPIC, payload={"tags": tags}, dedupe_key="k")


def test_the_handler_sends_the_known_tags(fake: FakeStorefront) -> None:
    tasks.revalidate(message(["shop:products", "shop:evil", "shop:home"]))
    assert list(fake.calls) == [["shop:products", "shop:home"]]
    tasks.revalidate(message(["shop:evil"]))  # nothing known left: no call
    assert len(fake.calls) == 1


def test_the_handler_dead_letters_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    class Refusing:
        def revalidate(self, tags: list[str]) -> None:
            raise StorefrontRefused("HTTP 401 not_authenticated")

    monkeypatch.setattr(registry, "get_storefront_cache", Refusing)
    with pytest.raises(outbox.PermanentFailure, match="not_authenticated"):
        tasks.revalidate(message(["shop:home"]))


def test_the_handler_is_registered_for_the_topic() -> None:
    assert outbox._handlers[storefront.TOPIC] is tasks.revalidate


@pytest.mark.django_db
def test_an_admin_save_reaches_the_website_after_its_window(
    staff_client: Any,
    fake: FakeStorefront,
    monkeypatch: pytest.MonkeyPatch,
    django_capture_on_commit_callbacks: Any,
) -> None:
    """The whole chain: ``PUT home-sections/new/`` → commit → outbox (due at the window's end) → the
    dispatcher → the website's revalidation call with ``shop:home``."""
    from glowderma.core import timeutils
    from glowderma.core.tasks import dispatch_outbox

    start = timeutils.now()
    monkeypatch.setattr(timeutils, "now", lambda: start)
    with django_capture_on_commit_callbacks(execute=True):
        response = staff_client.put(
            "/api/v1/admin/home-sections/new/", {"ids": []}, format="json", HTTP_IDEMPOTENCY_KEY=str(uuid4())
        )
    assert response.status_code == 200, response.content
    dispatch_outbox()
    assert list(fake.calls) == []  # the window is still open: later writes may join it
    monkeypatch.setattr(timeutils, "now", lambda: start + storefront.WINDOW)
    dispatch_outbox()
    assert list(fake.calls) == [["shop:home"]]
    assert OutboxMessage.objects.get(topic=storefront.TOPIC).status == "done"
