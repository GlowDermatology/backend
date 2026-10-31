"""Helpers for the ops console action tests (glowderma.core.admin_actions): logged-in clients of each role,
the flash messages of a response and the confirm POST."""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import Client

PASSWORD = "Saffron!Harbor-Long72"  # noqa: S105 - a test password
ROLES = ("anon", "customer_like", "staff", "manager")


def console_client(role: str, *, enforce_csrf_checks: bool = False) -> Client:
    """``anon`` (no session), ``customer_like`` (an active user without ``is_staff``), ``staff`` (a plain
    active staff member) or ``manager`` (``is_manager``)."""
    client = Client(enforce_csrf_checks=enforce_csrf_checks)
    if role == "anon":
        return client
    flags = {
        "customer_like": {"is_staff": False},
        "staff": {"is_staff": True},
        "manager": {"is_staff": True, "is_manager": True},
    }[role]
    user = get_user_model().objects.create_user(f"{role}@ops.uz", PASSWORD, **flags)
    client.force_login(user)
    return client


def user_of(client: Client) -> Any:
    return get_user_model().objects.get(pk=client.session["_auth_user_id"])


def messages_of(response: Any) -> list[tuple[str, str]]:
    """``[(level tag, text)]`` of the messages stored for the response's request."""
    return [(message.level_tag, str(message)) for message in get_messages(response.wsgi_request)]


def confirm(client: Client, url: str, **data: Any) -> Any:
    """POST the confirm step of an action (the hidden ``confirm`` flag plus ``data``)."""
    return client.post(url, {"confirm": "True", **data})


def denied(response: Any) -> bool:
    """Refused by the console: the admin's login redirect (no staff session) or 403 (no grant)."""
    if response.status_code == 403:
        return True
    return response.status_code == 302 and "/login/" in response["Location"]
