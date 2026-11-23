"""Locked reads of staff rows shared by the staff writes (admin contract §2.16, decision GST-1)."""

from __future__ import annotations

from glowderma.accounts.models import StaffUser


def lock_active_managers() -> list[int]:
    """Lock the active managers (by id, one lock order for every caller) and return their ids.

    Every write that could leave no active manager (demotion, deactivation) takes these locks before the
    target row, so two managers demoting or deactivating each other at the same time serialize and the
    second one sees the first one's result (PostgreSQL re-checks the locked rows' conditions).
    """
    return list(
        StaffUser.objects.select_for_update()
        .filter(is_active=True, is_manager=True)
        .order_by("pk")
        .values_list("pk", flat=True)
    )


__all__ = ["lock_active_managers"]
