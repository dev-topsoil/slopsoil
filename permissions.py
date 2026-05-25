from __future__ import annotations

from enum import IntFlag
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import discord
from discord.ext import commands

if TYPE_CHECKING:
    from bot import SlopSoil


class Role(IntFlag):
    """Permission roles using cascading bitflags.

    Each role's bit pattern is a superset of every less-privileged role, so the
    single check ``(user_role & required) == required`` enforces the full
    hierarchy without any special-casing:

        ADMIN  (0b111) ⊇  FRIEND (0b110) ⊇  VIEWER (0b100)

    NONE (0b000) has no bits set and therefore fails every role check.
    """

    NONE   = 0b000  # unrecognized user — no access
    VIEWER = 0b100  # any member of a guild the bot is in
    FRIEND = 0b110  # on the bot's friends list
    ADMIN  = 0b111  # listed in ALLOWED_USER_IDS


@runtime_checkable
class _BotView(Protocol):
    """Minimal bot interface required by get_user_role — easy to mock in tests."""

    allowed_ids: set[int]
    relationships: list
    guilds: list


def get_user_role(bot: _BotView, user_id: int) -> Role:
    """Return the highest Role that applies to *user_id*.

    Evaluated in priority order: admin > friend > guild member > none.
    Accepts any object that satisfies _BotView (the real SlopSoil bot or a
    test double).
    """
    if user_id in bot.allowed_ids:
        return Role.ADMIN

    if any(
        r.type == discord.RelationshipType.friend and r.user.id == user_id
        for r in bot.relationships
    ):
        return Role.FRIEND

    if any(guild.get_member(user_id) is not None for guild in bot.guilds):
        return Role.VIEWER

    return Role.NONE


def require_role(role: Role) -> commands.check:
    """Decorator that restricts a command to users whose role satisfies *role*.

    Passes when ``(user_role & role) == role``, i.e. the user's role contains
    all bits of the required role.  Because ADMIN's bit pattern includes every
    lower-privilege role's bits, admins automatically pass every check.

    Usage::

        @require_role(Role.VIEWER)   # viewer, friend, or admin
        @require_role(Role.FRIEND)   # friend or admin only
        @require_role(Role.ADMIN)    # admin only
    """

    async def predicate(ctx: commands.Context) -> bool:
        if ctx.bot.user and ctx.author.id == ctx.bot.user.id:
            return True
        user_role = get_user_role(ctx.bot, ctx.author.id)  # type: ignore[arg-type]
        return (user_role & role) == role

    return commands.check(predicate)
