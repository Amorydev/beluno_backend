"""Every command the API accepts, over REST and sync push alike."""

from __future__ import annotations

from pydantic import BaseModel

from beluno.api.commands import crews, finance, plans, profile
from beluno.api.finance_presenters import present_finance_current
from beluno.api.presenters import present_current
from beluno.modules.context import CommandContext
from beluno.sync.commands import CommandRegistry

ALL_COMMANDS = [
    *plans.COMMANDS,
    *profile.COMMANDS,
    *crews.COMMANDS,
    *finance.COMMANDS,
]


async def present_conflict(ctx: CommandContext, entity: object) -> BaseModel | None:
    return await present_finance_current(ctx, entity) or await present_current(ctx, entity)


def build_registry() -> CommandRegistry:
    return CommandRegistry(ALL_COMMANDS, present_conflict=present_conflict)
