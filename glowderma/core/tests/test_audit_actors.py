"""``audit.record`` with customer and POS-device actors (plan §1 P0: no ``get_username`` on non-staff)."""

from types import SimpleNamespace

import pytest

from glowderma.core import audit


@pytest.mark.django_db
def test_customer_actor_is_recorded_with_the_masked_phone() -> None:
    customer = SimpleNamespace(pk=5, phone="+998901234567", name="Malika")
    entry = audit.record(action="customer.logout", entity_type="customer", entity_id="5", actor=customer)
    assert (entry.actor_type, entry.actor_id, entry.actor_label) == ("customer", 5, "+998 90 *** ** 67")


@pytest.mark.django_db
def test_pos_device_actor_is_an_integration() -> None:
    device = SimpleNamespace(pk=3, key_prefix="demo0001", name="Kassa 1")
    entry = audit.record(action="pos.qr_resolved", entity_type="customer", entity_id="5", actor=device)
    assert (entry.actor_type, entry.actor_id, entry.actor_label) == ("integration", 3, "Kassa 1")


@pytest.mark.django_db
def test_staff_and_system_actors_are_unchanged() -> None:
    staff = SimpleNamespace(pk=1, full_name="", get_username=lambda: "admin@glowderma.uz")
    entry = audit.record(action="x", entity_type="y", entity_id="1", actor=staff)
    assert (entry.actor_type, entry.actor_label) == ("staff", "admin@glowderma.uz")
    named = audit.record(
        action="x", entity_type="y", entity_id="1", actor=SimpleNamespace(pk=2, full_name="Aziz")
    )
    assert (named.actor_type, named.actor_label) == ("staff", "Aziz")
    system = audit.record(action="x", entity_type="y", entity_id="1")
    assert (system.actor_type, system.actor_id, system.actor_label) == ("system", None, "")
    explicit = audit.record(action="x", entity_type="y", entity_id="1", actor=staff, actor_type="integration")
    assert explicit.actor_type == "integration"


def test_unknown_actor_kinds_are_refused() -> None:
    with pytest.raises(TypeError, match="Unsupported audit actor"):
        audit.actor_identity(object())
