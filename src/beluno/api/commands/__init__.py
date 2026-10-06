"""Every command the API accepts, over REST and sync push alike."""

from __future__ import annotations

from beluno.api.commands import groups, plans, profile, series, travel
from beluno.api.presenters import present_current
from beluno.sync.commands import CommandRegistry

ALL_COMMANDS = [
    *groups.COMMANDS,
    *plans.COMMANDS,
    *travel.COMMANDS,
    *series.COMMANDS,
    *profile.COMMANDS,
]


def build_registry() -> CommandRegistry:
    return CommandRegistry(ALL_COMMANDS, present_conflict=present_current)
