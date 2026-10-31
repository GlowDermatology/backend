"""The console's Uzbek label migrations (``00NN_uz_*``) are state only: no SQL on PostgreSQL, one linear chain
per app (no two leaves after the parallel console branches merged)."""

from __future__ import annotations

import io

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.loader import MigrationLoader

LOADER = MigrationLoader(None, ignore_no_migrations=True)
LABEL_MIGRATIONS = sorted(
    key
    for key in LOADER.disk_migrations
    if "_uz_" in key[1] and LOADER.disk_migrations[key].__module__.startswith("glowderma.")
)


def test_every_console_app_has_label_migrations() -> None:
    apps = {app for app, _name in LABEL_MIGRATIONS}
    assert {"accounts", "catalog", "commerce", "core", "professionals", "site_config"} <= apps
    assert ("professionals", "0007_uz_field_labels") in LABEL_MIGRATIONS


@pytest.mark.django_db
@pytest.mark.parametrize(("app", "name"), LABEL_MIGRATIONS)
def test_a_label_migration_prints_no_sql(app: str, name: str) -> None:
    out = io.StringIO()
    call_command("sqlmigrate", app, name, stdout=out)
    statements = [
        line
        for line in out.getvalue().splitlines()
        if line.strip() and not line.startswith("--") and line.strip() not in ("BEGIN;", "COMMIT;")
    ]
    assert statements == [], f"{app}.{name} changes the schema: {statements[:3]}"


def test_one_leaf_per_app() -> None:
    leaves = LOADER.graph.leaf_nodes()
    apps = [app for app, _name in leaves]
    assert len(apps) == len(set(apps)), leaves
    assert connection.vendor == "postgresql"


def test_the_professionals_label_migrations_follow_each_other() -> None:
    form = LOADER.disk_migrations[("professionals", "0006_uz_form_labels")]
    field = LOADER.disk_migrations[("professionals", "0007_uz_field_labels")]
    assert form.dependencies == [("professionals", "0005_uz_choice_labels")]
    assert field.dependencies == [("professionals", "0006_uz_form_labels")]
