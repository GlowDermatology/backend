"""The improve guidance's version 1 asked for "keeping every fact unchanged" wording-only rewrites, which
contradicts the improve task since rules 2026-11-19.1 (write a short, empty or poor text fully from the
facts).

A database whose improve template still has only the seeded version 1, active and unedited, gets the new
default text as an active version 2 (version 1 is retired and stays available for a rollback in the ops
console). A template an administrator has already versioned is left alone; a fresh database already seeds the
new text as version 1 (``defaults.ensure_rows``), so nothing happens there.
"""

from typing import Any

from django.db import migrations

OLD_IMPROVE_V1 = (
    "Rewrite only the selected texts to meet the requested goal while keeping every fact unchanged.\n"
    "- professional: precise, calm, expert wording; remove filler and slang.\n"
    "- clear: short sentences, simple words, the most important information first.\n"
    "- natural: fluent, idiomatic phrasing a native speaker would write.\n"
    "- seo: work in the product type and the main use naturally once or twice; never stuff keywords.\n"
    "- shorter: cut length by roughly a third without losing facts.\n"
    "Never add new claims, ingredients or benefits, and keep names, numbers and units exactly as "
    "given."
)


def upgrade(apps: Any, schema_editor: Any) -> None:
    from glowderma.assistant import defaults

    using = schema_editor.connection.alias
    PromptTemplate = apps.get_model("assistant", "PromptTemplate")
    PromptVersion = apps.get_model("assistant", "PromptVersion")
    template = PromptTemplate.objects.using(using).filter(purpose="improve").first()
    if template is None:
        return
    versions = list(PromptVersion.objects.using(using).filter(template=template))
    if len(versions) != 1:
        return
    (v1,) = versions
    new_text = defaults.INSTRUCTIONS["improve"]
    if (
        v1.number != 1
        or v1.status != "active"
        or v1.instructions != OLD_IMPROVE_V1
        or new_text == OLD_IMPROVE_V1
    ):
        return
    v1.status = "retired"
    v1.save(update_fields=["status", "updated_at"])
    PromptVersion.objects.using(using).create(
        template=template, number=2, instructions=new_text, status="active"
    )


class Migration(migrations.Migration):
    dependencies = [("assistant", "0003_job_draft")]

    operations = [migrations.RunPython(upgrade, migrations.RunPython.noop)]
