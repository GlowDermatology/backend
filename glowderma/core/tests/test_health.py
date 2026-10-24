from unittest.mock import patch

import pytest
from django.test import Client


def test_health_ok() -> None:
    response = Client().get("/api/v1/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_health_ready_ok() -> None:
    response = Client().get("/api/v1/health/?ready=1")
    assert response.status_code == 200
    assert response.json()["checks"] == dict.fromkeys(("database", "cache", "broker"), "ok")


@pytest.mark.django_db
def test_health_ready_db_failure_503() -> None:
    with patch("glowderma.core.api.health.connection.cursor", side_effect=RuntimeError):
        response = Client().get("/api/v1/health/?ready=1")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "error"
