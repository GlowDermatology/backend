"""notifications.0001_initial / 0002_seed_status_templates: dependencies, reversibility, the template rows
and the ``post_migrate`` receiver that re-ensures them after a flush."""

import importlib
from importlib import import_module

import pytest
from django.apps import apps as django_apps
from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

from glowderma.notifications.apps import STATUS_TEMPLATES, ensure_status_templates
from glowderma.notifications.models import StatusTemplate

TABLES = {
    "notifications_statustemplate",
    "notifications_customersegment",
    "notifications_segmentmember",
    "notifications_campaign",
    "notifications_campaignrecipient",
    "notifications_inboxnotification",
}
STATUSES = ["processing", "handed_to_courier", "delivered", "cancelled"]


def table_names() -> set[str]:
    with connection.cursor() as cursor:
        return set(connection.introspection.table_names(cursor))


def test_dependencies_follow_the_design() -> None:
    initial = import_module("glowderma.notifications.migrations.0001_initial").Migration
    assert initial.initial is True
    assert initial.dependencies == [
        ("accounts", "0004_customer_device_address_smscode_social"),
        ("catalog", "0001_initial"),
    ]
    data = import_module("glowderma.notifications.migrations.0002_seed_status_templates").Migration
    assert data.dependencies == [("notifications", "0001_initial")]
    assert [row[0] for row in STATUS_TEMPLATES] == STATUSES


@pytest.mark.django_db
def test_0002_reverse_deletes_the_rows_and_forward_restores_them() -> None:
    module = importlib.import_module("glowderma.notifications.migrations.0002_seed_status_templates")
    module.delete_templates(django_apps, connection.schema_editor)
    assert StatusTemplate.objects.count() == 0
    module.create_templates(django_apps, connection.schema_editor)
    module.create_templates(django_apps, connection.schema_editor)  # idempotent
    assert list(StatusTemplate.objects.values_list("status", flat=True)) == STATUSES


@pytest.mark.django_db
def test_receiver_keeps_edited_rows_and_tolerates_a_missing_table() -> None:
    StatusTemplate.objects.filter(status="processing").update(text_en="Edited by the admin.")
    StatusTemplate.objects.filter(status="cancelled").delete()
    config = django_apps.get_app_config("notifications")
    ensure_status_templates(config, using="default")
    assert StatusTemplate.objects.get(status="processing").text_en == "Edited by the admin."
    assert (
        StatusTemplate.objects.get(status="cancelled").text_ru
        == "Заказ №{raqam} отменён. Оплата будет возвращена."
    )

    class NoModels:
        def get_model(self, app_label: str, model_name: str) -> None:
            raise LookupError(model_name)

    ensure_status_templates(config, using="default", apps=NoModels())  # migrated to zero: nothing to do


@pytest.mark.django_db(transaction=True)
def test_receiver_tolerates_a_missing_table() -> None:
    """``flush`` after a dependency was migrated to zero: the model is registered but has no table."""
    call_command("migrate", verbosity=0)
    call_command("migrate", "notifications", "zero", verbosity=0)
    ensure_status_templates(django_apps.get_app_config("notifications"), using="default")  # no error
    call_command("flush", interactive=False, verbosity=0)  # the receiver runs inside flush too
    call_command("migrate", verbosity=0)
    assert StatusTemplate.objects.count() == 4


@pytest.mark.django_db(transaction=True)
def test_flush_keeps_the_template_rows() -> None:
    """A transactional test ends with pytest-django's flush; ``post_migrate`` restores the four rows, so later
    tests (and a ``--reuse-db`` rerun) never see an empty table."""
    call_command("migrate", verbosity=0)  # a sibling's ``migrate <dependency> zero`` may have unapplied us
    StatusTemplate.objects.filter(status="delivered").update(text_en="Delivered.")
    call_command("flush", interactive=False, verbosity=0)
    assert list(StatusTemplate.objects.values_list("status", flat=True)) == STATUSES
    assert StatusTemplate.objects.get(status="delivered").text_en == ""
    call_command("flush", interactive=False, verbosity=0)  # idempotent
    assert StatusTemplate.objects.count() == 4


@pytest.mark.django_db(transaction=True)
def test_migrate_zero_and_forward_again() -> None:
    call_command("migrate", verbosity=0)  # a sibling's ``migrate <dependency> zero`` may have unapplied us
    assert TABLES <= table_names()
    call_command("migrate", "notifications", "0001", verbosity=0)
    # 0002's reverse deleted the rows; the post_migrate receiver re-inserted them (the table still exists)
    assert StatusTemplate.objects.count() == 4
    call_command("migrate", "notifications", "zero", verbosity=0)
    assert not (TABLES & table_names())
    assert not MigrationRecorder.Migration.objects.filter(app="notifications").exists()
    call_command("migrate", "notifications", verbosity=0)
    assert TABLES <= table_names()
    assert list(StatusTemplate.objects.values_list("status", flat=True)) == STATUSES
    assert set(
        MigrationRecorder.Migration.objects.filter(app="notifications").values_list("name", flat=True)
    ) == {
        "0001_initial",
        "0002_seed_status_templates",
        "0003_inbox_account_source",
    }
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexdef FROM pg_indexes WHERE indexname = 'segment_name_ci_unique'")
        assert "lower((name)::text)" in cursor.fetchone()[0]


@pytest.mark.django_db(transaction=True)
def test_dependency_migrate_zero_cascades_and_a_full_migrate_restores_us() -> None:
    """Regression: a sibling's ``migrate catalog zero`` unapplies notifications too (0001 depends on
    catalog.0001) and ``migrate catalog`` alone does not re-apply us; only a plain ``migrate`` does, and the
    ``post_migrate`` receiver must cope with the table being absent in between."""
    call_command("migrate", verbosity=0)
    call_command("migrate", "catalog", "zero", verbosity=0)
    assert not (TABLES & table_names())
    assert not MigrationRecorder.Migration.objects.filter(app="notifications").exists()
    call_command("migrate", "catalog", verbosity=0)
    assert not (TABLES & table_names())  # the forward migrate of the dependency leaves us unapplied
    call_command("flush", interactive=False, verbosity=0)  # pytest-django's teardown in this state
    call_command("migrate", verbosity=0)
    assert TABLES <= table_names()
    assert list(StatusTemplate.objects.values_list("status", flat=True)) == STATUSES
