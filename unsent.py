"""
Deleted / unsent message log: when a member deletes (unsends) a message, repost who wrote it and what it
said to a channel you choose, with the time it was first sent, and keep it in a web log too.

What it can and can't do:
- Discord only tells the bot about a deleted message if the bot already had that message (saw it while it was
  online). Messages sent before the bot started, or while it was offline, can't be brought back.
- It needs the "Message Content Intent" turned on in the Developer Portal, so the bot can read the text.
- It skips messages the link filter deleted, so a blocked link isn't reported twice.
"""
import asyncio
import logging

import discord

import db
import discord_bot
from discord_bot import bot

log = logging.getLogger("unsent")
run = asyncio.to_thread


# ------------------------- settings -------------------------
def _s(name: str, gid: int) -> str:
    return f"{name}:{gid}"


def on_sync(gid: int) -> bool:
    return db.get_setting(_s("unsent_on", gid), "0") == "1"


def set_on(gid: int, on: bool) -> None:
    db.set_setting(_s("unsent_on", gid), "1" if on else "0")


def report_channel_sync(gid: int) -> int:
    import security
    v = db.get_setting(security.report_key("unsent", gid), "")
    return int(v) if v.isdigit() else 0


def content_ok() -> bool:
    """Can the bot actually read message text? (needs the Message Content Intent)"""
    return bool(getattr(bot._connection._intents, "message_content", False)) and not discord_bot.GATE_STATE.get("content_missing")


def status_sync(guild) -> dict:
    return {"content": content_ok()}


# ------------------------- helpers -------------------------
def _was_link_filtered(message, author, gid: int) -> bool:
    """True if the link filter is what deleted this message (so we don't report it twice)."""
    try:
        import linkguard
    except Exception:                            # noqa: BLE001
        return False
    try:
        if not linkguard.on_sync(gid):
            return False
        wl = linkguard.whitelist_sync(gid)
        if linkguard._exempt(author, gid, wl):
            return False
        return linkguard.has_link(message.content or "")
    except Exception:                            # noqa: BLE001
        return False


async def _report(message, author, content: str) -> None:
    gid = message.guild.id
    cid = await run(report_channel_sync, gid)
    if not cid:
        return
    ch = message.guild.get_channel(cid)
    if ch is None:
        return
    body = (content or "").strip()
    if len(body) > 1500:
        body = body[:1500] + "\u2026"
    sent = message.created_at
    where = getattr(message.channel, "mention", None) or (
        "#" + getattr(message.channel, "name", "a channel"))
    e = discord.Embed(title="\U0001f5d1\ufe0f Message unsent", color=0xB42318,
                      description=f"**{author.mention}** deleted a message in {where}.")
    e.add_field(name="Message", value=(body or "*(no text \u2014 only an attachment or embed)*"), inline=False)
    if sent is not None:
        ts = int(sent.timestamp())
        e.add_field(name="Sent", value=f"<t:{ts}:F> (<t:{ts}:R>)", inline=False)
    e.set_footer(text=f"{author} \u2022 {author.id}")
    e.timestamp = discord.utils.utcnow()
    try:
        await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as ex:
        log.warning("Unsent report failed in %s: %s", gid, ex)


# ------------------------- the handler -------------------------
async def _on_message_delete(message: discord.Message) -> None:
    if message.guild is None:
        return
    author = getattr(message, "author", None)
    if author is None or getattr(author, "bot", False):
        return
    gid = message.guild.id
    if gid not in await run(db.allowed_guild_ids):
        return
    if not await run(on_sync, gid):
        return
    if _was_link_filtered(message, author, gid):
        return
    content = message.content or ""
    sent = message.created_at
    try:
        await run(db.log_deleted_msg, gid, author.id, str(author), content, message.channel.id,
                  getattr(message.channel, "name", ""),
                  sent.replace(tzinfo=None) if sent is not None else None)
    except Exception:                            # noqa: BLE001  logging must never break anything
        log.exception("log_deleted_msg failed in %s", gid)
    await _report(message, author, content)


# The bot is a plain discord.Client (one handler per event), so we CHAIN: run whatever is already set for
# this event first, then ours, each guarded so one failing never blocks the other.
def _install(name: str, handler) -> None:
    existing = getattr(bot, name, None)

    async def chained(*args, **kw):
        if existing is not None:
            try:
                await existing(*args, **kw)
            except Exception:                    # noqa: BLE001
                log.exception("%s (earlier handler) failed", name)
        try:
            await handler(*args, **kw)
        except Exception:                        # noqa: BLE001
            log.exception("%s (unsent log) failed", name)

    chained.__name__ = name
    setattr(bot, name, chained)


_install("on_message_delete", _on_message_delete)
