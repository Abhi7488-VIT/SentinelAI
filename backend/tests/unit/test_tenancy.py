"""Role ordering and role-assignment policy."""

from __future__ import annotations

import pytest

from app.core.tenancy import can_assign_role
from app.models.organization import Role

OWN, ADM, ENG, VIEW = Role.OWNER, Role.ADMIN, Role.ENGINEER, Role.VIEWER


def test_role_order() -> None:
    assert OWN.at_least(ADM)
    assert ADM.at_least(ENG)
    assert ENG.at_least(VIEW)
    assert not VIEW.at_least(ENG)
    assert not ENG.at_least(ADM)
    assert not ADM.at_least(OWN)
    assert all(r.at_least(r) for r in Role)


@pytest.mark.parametrize(
    ("actor", "current", "new", "allowed"),
    [
        (OWN, None, OWN, True),
        (OWN, ADM, VIEW, True),
        (OWN, OWN, None, True),
        (ADM, None, ENG, True),
        (ADM, VIEW, ENG, True),
        (ADM, ENG, None, True),
        (ADM, None, ADM, False),
        (ADM, None, OWN, False),
        (ADM, ADM, VIEW, False),
        (ADM, OWN, None, False),
        (ADM, ENG, ADM, False),
        (ENG, None, VIEW, False),
        (VIEW, None, VIEW, False),
    ],
)
def test_can_assign_role(
    actor: Role, current: Role | None, new: Role | None, allowed: bool
) -> None:
    assert can_assign_role(actor, current, new) is allowed
