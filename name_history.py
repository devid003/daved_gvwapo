"""
Name history: keep a record of every username, display name and server nickname each member has used,
by Discord ID, so you can see who someone used to be.

What it can and can't do:
- It records names from now on (username and display-name changes through on_user_update, nickname changes
  through on_member_update, and the current name whenever someone joins).
- On first start it also backfills from everything the bot already saved with a date (the join log, command
  usage, the prompt log and member records), so there's history right away.
- Discord does NOT hand out names a member used before the bot ever recorded them, so the earliest name shown
  is the earliest the bot has on file, not necessarily their first-ever Discord name.
"""
import asyncio
import logging

import discord

import db
import discord_bot
from discord_bot import bot

log = logging.getLogger("namehistory")
run = asyncio.to_thread


async def _save(user_id: int, kind: str, name: str, guild_id: int = 0) -> None:
    try:
        await run(db.record_name, user_id, kind, name, guild_id)
    except Exception:                        # noqa: BLE001  recording a name must never break anything
        log.exception("record_name failed (%s %s)", kind, user_id)


async def _on_user_update(before: discord.User, after: discord.User) -> None:
    if before.name != after.name:
        await _save(after.id, "username", after.name)
    if (before.global_name or "") != (after.global_name or ""):
        await _save(after.id, "display", after.global_name or "")


async def _on_member_update(before: discord.Member, after: discord.Member) -> None:
    if (before.nick or "") != (after.nick or ""):
        await _save(after.id, "nick", after.nick or "", after.guild.id)


async def _on_member_join(member: discord.Member) -> None:
    await _save(member.id, "username", member.name)
    if member.global_name:
        await _save(member.id, "display", member.global_name)
    if member.nick:
        await _save(member.id, "nick", member.nick, member.guild.id)


# The bot is a plain discord.Client (one handler per event). The join gate (security.py) already set some of
# these, so we CHAIN: run whatever is there first, then ours, each guarded so one failing never blocks the other.
def _install(name: str, handler) -> None:
    existing = getattr(bot, name, None)

    async def chained(*args, **kw):
        if existing is not None:
            try:
                await existing(*args, **kw)
            except Exception:                 # noqa: BLE001
                log.exception("%s (earlier handler) failed", name)
        try:
            await handler(*args, **kw)
        except Exception:                     # noqa: BLE001
            log.exception("%s (name history) failed", name)

    chained.__name__ = name
    setattr(bot, name, chained)


for _fn, _name in [
    (_on_user_update, "on_user_update"),
    (_on_member_update, "on_member_update"),
    (_on_member_join, "on_member_join"),
]:
    _install(_name, _fn)


def _seed() -> None:
    """Backfill the history once, in the background, when the bot starts."""
    async def go():
        try:
            n = await run(db.name_seed_from_existing)
            if n:
                log.info("Name history: backfilled %d names from existing records", n)
        except Exception:                     # noqa: BLE001
            log.exception("Name history backfill failed")
    try:
        asyncio.ensure_future(go())
    except RuntimeError:
        pass


discord_bot.EXTRA_SETUP.append(_seed)
