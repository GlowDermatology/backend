"""Template tags of the console's language switcher (``glowderma.core.console_i18n``)."""

from typing import Any

from django import template
from django.urls import reverse

from glowderma.core import console_i18n

register = template.Library()


@register.simple_tag(takes_context=True)
def ops_language_switch(context: template.Context) -> dict[str, Any]:
    """What the sign-in pages' switcher needs (``admin/ops/language_switch.html``): the languages, the
    viewer's current one, the form's action and the page to return to."""
    request = context["request"]
    return {
        "languages": [{"code": code, "name": name} for code, name in console_i18n.LANGUAGES],
        "current": console_i18n.console_language(request),
        "action": reverse("ops_set_language"),
        "next": request.get_full_path(),
    }
