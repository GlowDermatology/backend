"""Draft copies of the terms and privacy texts (GS-2): ``PATCH settings/`` edits the drafts and only
``POST settings/terms/publish/`` copies them into the published columns, so ``terms_version`` always names the
text a customer accepted.

The data step fills an empty draft from its published column (the texts saved before this migration), so
staff keep editing what they wrote. Idempotent (only empty drafts are filled); reverse is a no-op because the
schema reverse drops the draft columns.
"""

from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

DOCUMENTS = ("terms", "privacy_policy")
LANGUAGES = ("uz", "ru", "en")


def copy_published_into_drafts(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    SiteSettings = apps.get_model("site_config", "SiteSettings")
    for document in DOCUMENTS:
        for language in LANGUAGES:
            draft, published = f"{document}_draft_{language}", f"{document}_{language}"
            SiteSettings.objects.filter(**{draft: ""}).exclude(**{published: ""}).update(
                **{draft: models.F(published)}
            )


class Migration(migrations.Migration):
    dependencies = [("site_config", "0002_seed_singleton")]
    operations = [
        *(
            migrations.AddField(
                model_name="sitesettings",
                name=f"{document}_draft_{language}",
                field=models.TextField(blank=True, default=""),
                preserve_default=False,
            )
            for document in DOCUMENTS
            for language in LANGUAGES
        ),
        migrations.RunPython(copy_published_into_drafts, migrations.RunPython.noop),
    ]
