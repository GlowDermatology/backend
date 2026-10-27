"""Database-level guarantees shared by every module: append-only trigger, retention purge window."""

import re
from collections.abc import Iterator
from contextlib import contextmanager

from django.db import connection, transaction

# The only append-only tables a retention job may delete from, and only inside purge_window() (decision T6).
PURGEABLE_TABLES: tuple[str, ...] = ("core_auditentry", "integrations_webhookinbox")

# Every trigger migration re-runs this CREATE OR REPLACE, so the body must stay the single source of truth:
# a migration that emitted an older body would silently revert the purge window.
FORBID_MUTATION_FUNCTION = """
CREATE OR REPLACE FUNCTION glowderma_forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE'
       AND current_setting('glowderma.allow_purge', true) = 'on'
       AND TG_TABLE_NAME IN ('core_auditentry', 'integrations_webhookinbox') THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'append-only table %', TG_TABLE_NAME;
END $$;
"""

# The pre-0008 body, kept so core.0008 can be reversed on a scratch database.
LEGACY_FORBID_MUTATION_FUNCTION = """
CREATE OR REPLACE FUNCTION glowderma_forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'append-only table %', TG_TABLE_NAME; END $$;
"""


def append_only_trigger_sql(table: str) -> tuple[str, str]:
    """Forward and reverse SQL for the append-only trigger of ``table`` (used from RunSQL migrations)."""
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
        raise ValueError("Expected a plain SQL table identifier")
    forward = FORBID_MUTATION_FUNCTION + (
        f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION glowderma_forbid_mutation();"
    )
    return forward, f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};"


@contextmanager
def purge_window() -> Iterator[None]:
    """A transaction in which retention jobs may DELETE from PURGEABLE_TABLES.

    ``SET LOCAL`` scopes the switch to this transaction only; the ORM's AppendOnlyQuerySet still refuses, so
    callers issue a plain SQL ``DELETE … WHERE <timestamp> < %s`` via ``connection.cursor()`` and audit it.
    """
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL glowderma.allow_purge = 'on'")
        yield
