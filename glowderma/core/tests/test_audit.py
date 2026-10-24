from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from django.test import RequestFactory

from glowderma.core import audit

pytestmark = pytest.mark.django_db


@dataclass
class Actor:
    pk: int = 7
    full_name: str = "Administrator"
    username: str = "admin@example.uz"

    def get_username(self) -> str:
        return self.username


def test_audit_record_captures_request_meta() -> None:
    request = RequestFactory().post("/api/test/", REMOTE_ADDR="127.0.0.1", HTTP_USER_AGENT="Audit test agent")
    request.client_ip = "198.51.100.2"  # type: ignore[attr-defined]
    request.request_id = "request-123456"  # type: ignore[attr-defined]
    entry = audit.record(
        action="test.changed",
        entity_type="test",
        entity_id="123",
        actor=Actor(),
        request=request,
        before={"name": "Old"},
        after={"name": "New"},
        reason="Requested correction",
    )
    entry.refresh_from_db()
    assert entry.actor_type == "staff"
    assert entry.actor_id == 7
    assert entry.actor_label == "Administrator"
    assert entry.action == "test.changed"
    assert entry.entity_type == "test"
    assert entry.entity_id == "123"
    assert entry.before == {"name": "Old"}
    assert entry.after == {"name": "New"}
    assert entry.reason == "Requested correction"
    assert entry.ip == "198.51.100.2"
    assert entry.user_agent == "Audit test agent"
    assert entry.request_id == "request-123456"


def test_audit_record_without_request_is_system() -> None:
    entry = audit.record(action="test.created", entity_type="test", entity_id="123")
    assert entry.actor_type == "system"
    assert entry.actor_id is None
    assert entry.actor_label == ""
    assert entry.ip is None
    assert entry.user_agent == ""
    assert entry.request_id == ""
    assert entry.before is None
    assert entry.after is None


def test_audit_record_falls_back_to_username_and_remote_address() -> None:
    request = RequestFactory().get("/api/test/", REMOTE_ADDR="192.0.2.9")
    entry = audit.record(
        action="test.changed",
        entity_type="test",
        entity_id="123",
        actor=Actor(full_name=""),
        request=request,
    )
    assert entry.actor_label == "admin@example.uz"
    assert entry.ip == "192.0.2.9"


def test_audit_record_accepts_explicit_actor_type() -> None:
    entry = audit.record(
        action="test.imported", entity_type="test", entity_id="123", actor_type="integration"
    )
    assert entry.actor_type == "integration"
    assert entry.actor_id is None


def test_audit_created_at_uses_patchable_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = datetime(2020, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("glowderma.core.timeutils.now", lambda: expected)
    entry = audit.record(action="test.created", entity_type="test", entity_id="1")
    assert entry.created_at == expected
