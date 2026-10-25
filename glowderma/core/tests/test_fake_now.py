from datetime import UTC, datetime

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from config.env import env_datetime
from glowderma.core import timeutils


def test_now_is_the_real_clock_by_default() -> None:
    assert abs((timeutils.now() - datetime.now(UTC)).total_seconds()) < 5


def test_fake_now_pins_the_clock() -> None:
    instant = datetime(2026, 10, 24, 7, 5, tzinfo=UTC)
    with override_settings(FAKE_NOW=instant):
        assert timeutils.now() == instant
        assert timeutils.isoformat(timeutils.now()) == "2026-10-24T12:05:00+05:00"


def test_env_datetime_requires_an_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_NOW", "2026-10-24T12:05:00")
    with pytest.raises(ImproperlyConfigured, match="timezone offset"):
        env_datetime("FAKE_NOW")
    monkeypatch.setenv("FAKE_NOW", "yesterday")
    with pytest.raises(ImproperlyConfigured, match="ISO 8601"):
        env_datetime("FAKE_NOW")
    monkeypatch.setenv("FAKE_NOW", "2026-10-24T12:05:00+05:00")
    assert env_datetime("FAKE_NOW") == datetime.fromisoformat("2026-10-24T12:05:00+05:00")
    monkeypatch.delenv("FAKE_NOW")
    assert env_datetime("FAKE_NOW", None) is None
