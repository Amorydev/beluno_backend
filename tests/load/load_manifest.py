"""The seed manifest as typed objects, plus round-robin assignment of people to Locust users."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import count
from pathlib import Path

log = logging.getLogger("beluno.load")


@dataclass(frozen=True)
class Person:
    name: str
    participant_id: str
    token: str | None

    @property
    def registered(self) -> bool:
        return self.token is not None


@dataclass(frozen=True)
class Plan:
    plan_id: str
    people: tuple[Person, ...]

    @property
    def registered(self) -> tuple[Person, ...]:
        return tuple(person for person in self.people if person.registered)

    @property
    def participant_ids(self) -> list[str]:
        return [person.participant_id for person in self.people]


@dataclass(frozen=True)
class Manifest:
    trips: tuple[Plan, ...]
    quick: tuple[Plan, ...]
    settle: tuple[Plan, ...]
    busy: Plan
    base_currency: str


def _plan(raw: dict[str, object]) -> Plan:
    people = tuple(
        Person(name=item["name"], participant_id=item["participant_id"], token=item["token"])
        for item in raw["people"]  # type: ignore[attr-defined]
    )
    return Plan(plan_id=str(raw["plan_id"]), people=people)


def load_manifest(path: str | Path) -> Manifest:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    expires = datetime.fromisoformat(raw["token_expires_at"])
    if expires <= datetime.now(UTC):
        log.warning("manifest tokens expired at %s: run load_seed.py --remint", expires)
    return Manifest(
        trips=tuple(_plan(plan) for plan in raw["trips"]),
        quick=tuple(_plan(plan) for plan in raw["quick"]),
        settle=tuple(_plan(plan) for plan in raw["settle"]),
        busy=_plan(raw["busy"]),
        base_currency=raw["base_currency"],
    )


class Assigner:
    """Hands out ``(plan, person)`` pairs in turn so users spread over plans and people.

    Registered people are listed plan by plan in a round-robin over plans, so the first
    users land on different plans before any plan gets a second user.
    """

    def __init__(self, plans: tuple[Plan, ...]) -> None:
        pairs: list[tuple[Plan, Person]] = []
        depth = max((len(plan.registered) for plan in plans), default=0)
        for position in range(depth):
            for plan in plans:
                if position < len(plan.registered):
                    pairs.append((plan, plan.registered[position]))
        if not pairs:
            raise RuntimeError("the manifest has no registered people for this scenario")
        self._pairs = pairs
        self._next = count()

    def take(self) -> tuple[Plan, Person]:
        return self._pairs[next(self._next) % len(self._pairs)]
