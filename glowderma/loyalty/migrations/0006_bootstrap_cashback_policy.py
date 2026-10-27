"""loyalty.0006 — the first cashback policy of an install (gap CB-9.2.4; open questions L1–L3).

Without any ``CashbackPolicy`` every purchase earns 0 (``engine.rate_for`` → ``None``), and the admin editor
can only schedule a version in the future. When the table is **empty** this inserts version 1 with the
provisional defaults of ``docs/decisions/open-questions.md``:

* ``basis`` ``before_purchase`` (L1), ``earn_on_cashback_paid`` false (L2), author ``NULL`` (system, "Tizim");
* tiers (L3) ``[0, 20 M)`` 1 %, ``[20 M, 30 M)`` 2 %, ``[30 M, ∞)`` 3 % — the top band is open-ended, so a
  customer above 50 M keeps 3 % until the client answers L3 (staff add a version to change it);
* ``effective_from`` 2026-06-01 00:00 Tashkent, before any real purchase (go-live is later), so the policy
  covers every purchase of the install; ``created_at`` 2026-05-20 10:00 Tashkent because the database requires
  ``effective_from > created_at``. Both instants are those of the design's v1 ("1-iyun – 14-avg 2026"): a
  demo seeded over a migrated database (``seed_demo`` without ``--reset``) keeps its pre-June purchases
  uncredited, as the design dataset (Malika's timeline) expects.

The insert is audited as ``cashback.policy_version_created`` (actor system, reason naming this migration).
Idempotent: nothing happens when any version exists (a rerun, or a database whose staff already created
one). Insert only — the policy tables are append-only. The reverse is a no-op: the rows cannot be deleted
(``migrate loyalty 0001`` or ``zero`` drops the tables anyway). ``settings.LOYALTY_BOOTSTRAP_POLICY`` is
false only under ``config.settings.test``, whose tests build their own versions from v1.
"""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

TASHKENT = ZoneInfo("Asia/Tashkent")
VERSION = 1
CREATED_AT = datetime(2026, 5, 20, 10, 0, tzinfo=TASHKENT)
EFFECTIVE_FROM = datetime(2026, 6, 1, 0, 0, tzinfo=TASHKENT)
BASIS = "before_purchase"
EARN_ON_CASHBACK_PAID = False
TIERS: tuple[tuple[int, int | None, str], ...] = (
    (0, 20_000_000, "1.00"),
    (20_000_000, 30_000_000, "2.00"),
    (30_000_000, None, "3.00"),
)
REASON = "migration loyalty.0006: first cashback policy with the L1–L3 defaults"


def bootstrap_policy(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    if not getattr(settings, "LOYALTY_BOOTSTRAP_POLICY", True):
        return
    database = schema_editor.connection.alias
    CashbackPolicy = apps.get_model("loyalty", "CashbackPolicy")
    CashbackTier = apps.get_model("loyalty", "CashbackTier")
    AuditEntry = apps.get_model("core", "AuditEntry")
    if CashbackPolicy.objects.using(database).exists():
        return
    policy = CashbackPolicy.objects.using(database).create(
        version=VERSION,
        effective_from=EFFECTIVE_FROM,
        basis=BASIS,
        earn_on_cashback_paid=EARN_ON_CASHBACK_PAID,
        author=None,
        created_at=CREATED_AT,
    )
    for from_amount, to_amount, percent in TIERS:
        CashbackTier.objects.using(database).create(
            policy=policy,
            from_amount=from_amount,
            to_amount=to_amount,
            percent=Decimal(percent),
            created_at=CREATED_AT,
        )
    AuditEntry.objects.using(database).create(
        actor_type="system",
        actor_id=None,
        actor_label="",
        action="cashback.policy_version_created",
        entity_type="cashback_policy",
        entity_id=str(VERSION),
        after={
            "version": VERSION,
            "effective_from": EFFECTIVE_FROM.isoformat(),
            "basis": BASIS,
            "earn_on_cashback_paid": EARN_ON_CASHBACK_PAID,
            "tiers": [{"from": low, "to": high, "percent": percent} for low, high, percent in TIERS],
            "superseded_version": None,
        },
        reason=REASON,
    )


class Migration(migrations.Migration):
    dependencies = [("loyalty", "0005_purchase_unpushed_online_idx")]
    operations = [migrations.RunPython(bootstrap_policy, migrations.RunPython.noop)]
