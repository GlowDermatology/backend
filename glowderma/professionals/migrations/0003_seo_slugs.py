"""B-W1 SEO: ``Professional.slug``, backfilled from the name (unique, never only digits)."""

from typing import Any

from django.conf import settings
from django.db import migrations, models

from glowderma.core import slugs

SLUG_MAX_LENGTH = 160


def backfill_professional_slugs(apps: Any, schema_editor: Any) -> None:
    professional_model = apps.get_model("professionals", "Professional")
    taken: set[str] = set(professional_model.objects.exclude(slug="").values_list("slug", flat=True))
    for row in professional_model.objects.filter(slug="").order_by("id").only("id", "name"):
        base = slugs.slugify_uz(row.name, max_length=SLUG_MAX_LENGTH) or "mutaxassis"
        attempt = 1
        while (value := slugs.candidate(base, attempt, max_length=SLUG_MAX_LENGTH)) in taken:
            attempt += 1
        taken.add(value)
        professional_model.objects.filter(pk=row.pk).update(slug=value)


class Migration(migrations.Migration):
    dependencies = [
        ("professionals", "0002_qualifications_staff_user_service_category"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="professional",
            name="slug",
            field=models.CharField(blank=True, db_default="", default="", max_length=160),
        ),
        migrations.RunPython(backfill_professional_slugs, migrations.RunPython.noop, elidable=True),
        migrations.AddConstraint(
            model_name="professional",
            constraint=models.UniqueConstraint(
                condition=models.Q(("slug", ""), _negated=True),
                fields=("slug",),
                name="professional_slug_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="professional",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("slug", ""),
                    models.Q(
                        ("slug__regex", "^[a-z0-9]+(?:-[a-z0-9]+)*$"),
                        models.Q(("slug__regex", "^[0-9]+$"), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="professional_slug_format",
            ),
        ),
    ]
