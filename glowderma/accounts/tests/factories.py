"""factory-boy factories for the accounts module.

``TEST_PASSWORD`` is the one shared password every factory-made staff user can log in with; tests
that need the raw refresh token of a factory-made session read it with :func:`raw_refresh`.
Call the factories as ``StaffUserFactory.create(...)``: the typed ``create`` returns the model.
"""

from typing import Any
from weakref import WeakKeyDictionary

from factory.base import Factory
from factory.declarations import Sequence, SubFactory
from factory.django import DjangoModelFactory
from factory.helpers import post_generation

from glowderma.accounts import services
from glowderma.accounts.models import AuthSession, StaffUser

TEST_PASSWORD = "Saffron!Harbor-Long72"  # noqa: S105

_refresh_tokens: WeakKeyDictionary[AuthSession, str] = WeakKeyDictionary()


class StaffUserFactory(DjangoModelFactory[StaffUser]):
    class Meta:
        model = StaffUser
        skip_postgeneration_save = True

    email = Sequence(lambda n: f"staff{n + 1}@x.uz")  # type: ignore[no-untyped-call]
    full_name = Sequence(lambda n: f"Staff User {n + 1}")  # type: ignore[no-untyped-call]
    is_active = True

    @post_generation  # type: ignore[untyped-decorator]
    def password(obj: StaffUser, create: bool, extracted: str | None, **kwargs: Any) -> None:
        """Hash ``password=`` (default ``TEST_PASSWORD``) after the row exists, then persist it."""
        obj.set_password(TEST_PASSWORD if extracted is None else extracted)
        if create:
            obj.save(update_fields=["password"])


class AuthSessionFactory(Factory[AuthSession]):
    """A staff session started through ``services.start_staff_session`` (never a raw ``create``)."""

    class Meta:
        model = AuthSession

    staff = SubFactory(StaffUserFactory)  # type: ignore[no-untyped-call]
    device_label = "pytest"

    @classmethod
    def _create(cls, model_class: type[AuthSession], *args: Any, **kwargs: Any) -> AuthSession:
        session, refresh = services.start_staff_session(kwargs["staff"], device_label=kwargs["device_label"])
        _refresh_tokens[session] = refresh
        return session


def raw_refresh(session: AuthSession) -> str:
    """The raw refresh token of a session made by ``AuthSessionFactory``."""
    return _refresh_tokens[session]
