import pytest
from django.core.exceptions import ImproperlyConfigured

from config.settings.base import database_config


def test_portless_database_url_uses_postgres_default() -> None:
    assert database_config("postgres://u:p@db.internal/glow")["PORT"] == 5432


@pytest.mark.parametrize(
    "url", ["sqlite:///db", "postgres://u:p@db/", "postgres://u:p@db:no/db", "postgres://[invalid/db"]
)
def test_database_url_rejects_invalid_values(url: str) -> None:
    with pytest.raises(ImproperlyConfigured, match="DATABASE_URL"):
        database_config(url)
