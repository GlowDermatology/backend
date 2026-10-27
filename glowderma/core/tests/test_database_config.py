import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from config.settings.base import database_config


def test_portless_database_url_uses_postgres_default() -> None:
    assert database_config("postgres://u:p@db.internal/glow")["PORT"] == 5432


def test_database_config_without_pool() -> None:
    config = database_config("postgres://u:p@db.internal:5433/glow")
    assert config["CONN_HEALTH_CHECKS"] is True
    assert config["CONN_MAX_AGE"] == 0
    assert config["OPTIONS"] == {"connect_timeout": 3}
    assert "pool" not in config["OPTIONS"]


def test_database_config_with_pool_forbids_persistent_connections() -> None:
    config = database_config("postgres://u:p@db.internal/glow", pool=True)
    pool = config["OPTIONS"]["pool"]
    assert pool == {"min_size": 1, "max_size": 4, "timeout": 10}
    assert config["OPTIONS"]["connect_timeout"] == 3
    assert config["CONN_MAX_AGE"] == 0
    assert config["CONN_HEALTH_CHECKS"] is True


def test_test_settings_do_not_pool() -> None:
    assert "pool" not in settings.DATABASES["default"]["OPTIONS"]


@pytest.mark.parametrize(
    "url", ["sqlite:///db", "postgres://u:p@db/", "postgres://u:p@db:no/db", "postgres://[invalid/db"]
)
def test_database_url_rejects_invalid_values(url: str) -> None:
    with pytest.raises(ImproperlyConfigured, match="DATABASE_URL"):
        database_config(url)
