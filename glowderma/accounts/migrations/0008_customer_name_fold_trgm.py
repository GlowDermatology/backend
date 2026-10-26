"""accounts.0008 — the customer name trigram index on the folded name (gap CRIT-SEARCH-UZ-APOSTROPHE, GC-1).

The admin customers search compares ``core.search.SearchFold`` of the name and of the term (apostrophe
variants and ё folded), so the index moves from ``lower(name)`` to that expression. Non-atomic: the new index
is built ``CONCURRENTLY`` under a new name **before** the old one is dropped ``CONCURRENTLY``, so customer
writes (sign-ups, sign-ins) are never blocked and the search is never without an index. A failed build leaves
an INVALID index: drop it (``DROP INDEX CONCURRENTLY accounts_customer_fold_trgm``) and migrate again.
Reversible.
"""

import django.contrib.postgres.indexes
from django.contrib.postgres.operations import AddIndexConcurrently, RemoveIndexConcurrently
from django.db import migrations

import glowderma.core.search


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("accounts", "0007_session_auth_method_identity_client"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="customer",
            index=django.contrib.postgres.indexes.GinIndex(
                django.contrib.postgres.indexes.OpClass(
                    glowderma.core.search.SearchFold("name"), name="gin_trgm_ops"
                ),
                name="accounts_customer_fold_trgm",
            ),
        ),
        RemoveIndexConcurrently(model_name="customer", name="accounts_customer_name_trgm"),
    ]
