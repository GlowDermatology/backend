"""Drive the ops console settings page (``/django-admin/settings/``) the way a browser does: read the
forms the page rendered (every input and textarea, the hidden ``section``, ``<name>__was`` digests, CSRF
token and publish fingerprint included) and post a card with the fields a person changed on top of them."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

from django.test import Client

from glowderma.site_config.forms import SECTION_FORMS
from glowderma.site_config.models import SiteSettings

URL = "/django-admin/settings/"
_NOT_SUBMITTED = frozenset({"submit", "button", "reset", "image", "file", "checkbox", "radio"})


class _FormsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, str]] = []
        self._form: dict[str, str] | None = None
        self._textarea: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "form":
            self._form = {}
            self.forms.append(self._form)
        elif self._form is None:
            return
        elif tag == "input" and values.get("name") and values.get("type", "text") not in _NOT_SUBMITTED:
            self._form[values["name"]] = values.get("value", "")
        elif tag == "textarea" and values.get("name"):
            self._textarea = values["name"]
            self._form[self._textarea] = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._form = None
        elif tag == "textarea" and self._form is not None and self._textarea is not None:
            # A browser drops the one line break that follows ``<textarea>`` (Django renders it there).
            value = self._form[self._textarea]
            self._form[self._textarea] = value[2:] if value.startswith("\r\n") else value.removeprefix("\n")
            self._textarea = None

    def handle_data(self, data: str) -> None:
        if self._form is not None and self._textarea is not None:
            self._form[self._textarea] += data


def forms_of(content: bytes | str) -> dict[str, dict[str, str]]:
    """The page's forms keyed by their ``section``: ``store``, ``contacts``, ``legal`` and ``publish``."""
    parser = _FormsParser()
    parser.feed(content.decode() if isinstance(content, bytes) else content)
    parser.close()
    return {form["section"]: form for form in parser.forms if "section" in form}


def page_forms(client: Client) -> dict[str, dict[str, str]]:
    """GET the page and return its forms (:func:`forms_of`)."""
    response = client.get(URL)
    assert response.status_code == 200, response.status_code
    return forms_of(response.content)


def post_card(client: Client, section: str, **values: Any) -> Any:
    """Render the page, then post the ``section`` card as rendered with ``values`` typed over it."""
    return client.post(URL, {**page_forms(client)[section], **values})


def card_data(section: str, **values: Any) -> dict[str, Any]:
    """The POST body the page would render for ``section`` (without a CSRF token): for a client that cannot
    open the page (a refused role)."""
    form = SECTION_FORMS[section](SiteSettings.objects.get(pk=1))
    return {
        "section": section,
        **{name: str(form.initial[name]) for name in form.COLUMNS},
        **dict(form.was),
        **values,
    }


def squash(content: bytes | str) -> str:
    """The text with every run of whitespace collapsed to one space."""
    text = content.decode() if isinstance(content, bytes) else content
    return re.sub(r"\s+", " ", text)


def visible(fragment: str) -> str:
    """An HTML fragment as text: tags dropped, whitespace collapsed, trimmed."""
    return squash(re.sub(r"<[^>]+>", " ", fragment)).strip()


def by_testid(content: bytes | str, testid: str) -> str:
    """The visible text of the element carrying ``data-testid="<testid>"`` (a ``<p>`` or ``<span>``)."""
    text = content.decode() if isinstance(content, bytes) else content
    found = re.search(rf'data-testid="{re.escape(testid)}"[^>]*>(.*?)</(?:p|span)>', text, re.S)
    assert found, testid
    return visible(found.group(1))
