from django.db import migrations

from glowderma.core.db import append_only_trigger_sql

forward, reverse = append_only_trigger_sql("core_auditentry")


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]
    operations = [migrations.RunSQL(forward, reverse)]
