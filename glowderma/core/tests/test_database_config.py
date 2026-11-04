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


@pytest.fixture
def postgres_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("POSTGRES_NAME", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_HOST", "POSTGRES_PORT"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("postgres_env")
def test_separate_database_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "POSTGRES_NAME": "custom_db",
        "POSTGRES_USER": "custom_user",
        "POSTGRES_PASSWORD": "raw:@/#%password",
        "POSTGRES_HOST": "db.internal",
        "POSTGRES_PORT": "5440",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    config = database_config("postgres://old:old@localhost/old", pool=True)
    assert config["NAME"] == "custom_db"
    assert config["USER"] == "custom_user"
    assert config["PASSWORD"] == values["POSTGRES_PASSWORD"]
    assert config["HOST"] == "db.internal"
    assert config["PORT"] == 5440
    assert config["OPTIONS"]["pool"]["max_size"] == 4


@pytest.mark.usefixtures("postgres_env")
def test_separate_database_requires_production_credentials() -> None:
    with pytest.raises(ImproperlyConfigured, match="POSTGRES_NAME is required"):
        database_config()


@pytest.mark.usefixtures("postgres_env")
@pytest.mark.parametrize("port", ["oops", "0", "65536"])
def test_separate_database_rejects_invalid_port(monkeypatch: pytest.MonkeyPatch, port: str) -> None:
    monkeypatch.setenv("POSTGRES_PORT", port)
    with pytest.raises(ImproperlyConfigured, match="POSTGRES_PORT"):
        database_config(local=True)


@pytest.mark.usefixtures("postgres_env")
def test_separate_database_local_defaults() -> None:
    config = database_config(local=True)
    assert config["NAME"] == "glowderma"
    assert config["HOST"] == "localhost"
    assert config["PORT"] == 5433
