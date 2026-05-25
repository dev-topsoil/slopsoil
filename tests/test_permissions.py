"""Tests for the Role bitflag hierarchy and get_user_role() lookup."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from permissions import Role, get_user_role


# ---------------------------------------------------------------------------
# Role bitflag hierarchy
# ---------------------------------------------------------------------------


def test_role_values_are_supersets():
    """ADMIN ⊇ FRIEND ⊇ VIEWER ⊇ NONE (each contains all lower bits)."""
    assert (Role.ADMIN & Role.FRIEND) == Role.FRIEND
    assert (Role.ADMIN & Role.VIEWER) == Role.VIEWER
    assert (Role.FRIEND & Role.VIEWER) == Role.VIEWER
    assert (Role.NONE & Role.VIEWER) != Role.VIEWER


@pytest.mark.parametrize(
    "user_role, required, passes",
    [
        (Role.ADMIN, Role.ADMIN, True),
        (Role.ADMIN, Role.FRIEND, True),
        (Role.ADMIN, Role.VIEWER, True),
        (Role.FRIEND, Role.FRIEND, True),
        (Role.FRIEND, Role.VIEWER, True),
        (Role.FRIEND, Role.ADMIN, False),
        (Role.VIEWER, Role.VIEWER, True),
        (Role.VIEWER, Role.FRIEND, False),
        (Role.VIEWER, Role.ADMIN, False),
        (Role.NONE, Role.VIEWER, False),
        (Role.NONE, Role.FRIEND, False),
        (Role.NONE, Role.ADMIN, False),
    ],
)
def test_role_check_logic(user_role: Role, required: Role, passes: bool):
    assert ((user_role & required) == required) is passes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bot(
    allowed_ids: set[int] | None = None,
    friend_ids: set[int] | None = None,
    guild_member_ids: set[int] | None = None,
) -> MagicMock:
    import discord

    bot = MagicMock()
    bot.allowed_ids = allowed_ids or set()

    # Build fake relationships for friend_ids
    relationships = []
    for uid in (friend_ids or set()):
        rel = MagicMock()
        rel.type = discord.RelationshipType.friend
        rel.user.id = uid
        relationships.append(rel)
    bot.relationships = relationships

    # Build fake guilds with get_member
    guild = MagicMock()
    member_ids = guild_member_ids or set()

    def _get_member(user_id: int):
        return MagicMock() if user_id in member_ids else None

    guild.get_member.side_effect = _get_member
    bot.guilds = [guild]

    return bot


# ---------------------------------------------------------------------------
# get_user_role()
# ---------------------------------------------------------------------------


def test_admin_user_returns_admin():
    bot = _make_bot(allowed_ids={100})
    assert get_user_role(bot, 100) == Role.ADMIN


def test_friend_user_returns_friend():
    bot = _make_bot(friend_ids={200})
    assert get_user_role(bot, 200) == Role.FRIEND


def test_guild_member_returns_viewer():
    bot = _make_bot(guild_member_ids={300})
    assert get_user_role(bot, 300) == Role.VIEWER


def test_unknown_user_returns_none():
    bot = _make_bot()
    assert get_user_role(bot, 999) == Role.NONE


def test_admin_beats_friend():
    """If a user is both in allowed_ids and friends list, they get ADMIN."""
    bot = _make_bot(allowed_ids={100}, friend_ids={100})
    assert get_user_role(bot, 100) == Role.ADMIN


def test_friend_beats_guild_member():
    """If a user is both a friend and a guild member, they get FRIEND."""
    bot = _make_bot(friend_ids={200}, guild_member_ids={200})
    assert get_user_role(bot, 200) == Role.FRIEND


def test_non_friend_relationship_ignored():
    """Only RelationshipType.friend relationships count."""
    import discord

    bot = _make_bot()
    rel = MagicMock()
    rel.type = discord.RelationshipType.blocked
    rel.user.id = 42
    bot.relationships = [rel]
    assert get_user_role(bot, 42) == Role.NONE


def test_no_guilds_returns_none():
    bot = _make_bot()
    bot.guilds = []
    assert get_user_role(bot, 500) == Role.NONE


def test_multiple_guilds_member_found_in_second():
    """User found in a later guild still returns VIEWER."""
    import discord

    bot = _make_bot()
    guild1 = MagicMock()
    guild1.get_member.return_value = None
    guild2 = MagicMock()
    guild2.get_member.return_value = MagicMock()
    bot.guilds = [guild1, guild2]
    bot.relationships = []
    assert get_user_role(bot, 77) == Role.VIEWER
