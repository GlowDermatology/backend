import re


def append_only_trigger_sql(table: str) -> tuple[str, str]:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", table):
        raise ValueError("Expected a plain SQL table identifier")
    forward = """
CREATE OR REPLACE FUNCTION glowderma_forbid_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'append-only table %', TG_TABLE_NAME; END $$;
"""
    forward += (
        f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION glowderma_forbid_mutation();"
    )
    return forward, f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};"
