"""
Link control:
- Link filter: deletes any message with a link, unless the member is whitelisted (by Discord ID), an admin,
  or the owner, and posts a report in a channel you choose.
- Invite lock: stops ordinary members from creating invite links (removes "Create Invite" from @everyone).
- In-server commands for the owner/admins: /allowlink, /blocklink, /linkfilter.

Needs on the bot's role: Manage Messages (to delete links), Manage Roles (for the invite lock). The link filter
also needs the "Message Content Intent" turned on in the Developer Portal, so the bot can read messages.
"""
import asyncio
import logging
import re

import discord
from discord import app_commands

import db
import discord_bot
from discord_bot import bot, OWNER_IDS

log = logging.getLogger("linkguard")
run = asyncio.to_thread

# a practical link detector: real schemes, www., Discord invites, and bare domains with common endings
_SCHEME = ("http://", "https://", "www.", "discord.gg/", "discord.com/invite/", "discordapp.com/invite/")
_BARE = re.compile(r"\b[a-z0-9][a-z0-9-]{1,}\.(?:com|net|org|gg|io|xyz|co|me|tv|info|link|app|site|online|store|"
                   r"shop|club|live|vip|biz|ru|cn|ph|to|cc)\b", re.I)


def has_link(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    if any(s in low for s in _SCHEME):
        return True
    return bool(_BARE.search(text))


# ------------------------- settings -------------------------
def _s(name: str, gid: int) -> str:
    return f"{name}:{gid}"


def on_sync(gid: int) -> bool:
    return db.get_setting(_s("link_on", gid), "0") == "1"


def whitelist_sync(gid: int) -> set:
    raw = db.get_setting(_s("link_wl", gid), "")
    return {int(x) for x in raw.replace("\n", ",").replace(" ", ",").split(",") if x.strip().isdigit()}


def add_to_whitelist(gid: int, user_id: int) -> None:
    wl = whitelist_sync(gid)
    wl.add(user_id)
    db.set_setting(_s("link_wl", gid), ",".join(str(i) for i in sorted(wl)))


def remove_from_whitelist(gid: int, user_id: int) -> bool:
    wl = whitelist_sync(gid)
    if user_id not in wl:
        return False
    wl.discard(user_id)
    db.set_setting(_s("link_wl", gid), ",".join(str(i) for i in sorted(wl)))
    return True


def set_on(gid: int, on: bool) -> None:
    db.set_setting(_s("link_on", gid), "1" if on else "0")


def report_channel_sync(gid: int) -> int:
    import security
    v = db.get_setting(security.report_key("link", gid), "")
    return int(v) if v.isdigit() else 0


def content_ok() -> bool:
    """Can the bot actually read message text? (needs the Message Content Intent)"""
    return bool(getattr(bot._connection._intents, "message_content", False)) and not discord_bot.GATE_STATE.get("content_missing")


# ------------------------- invite lock -------------------------
def members_can_invite(guild) -> bool:
    try:
        return bool(guild.default_role.permissions.create_instant_invite)
    except Exception:                        # noqa: BLE001
        return True


async def set_invite_lock(guild, blocked: bool) -> None:
    perms = guild.default_role.permissions
    perms.update(create_instant_invite=not blocked)
    await guild.default_role.edit(permissions=perms, reason="Link control: members may not create invites"
                                  if blocked else "Link control: members may create invites again")


def status_sync(guild) -> dict:
    me = getattr(guild, "me", None) if guild else None
    perms = getattr(me, "guild_permissions", None)
    return {"content": content_ok(),
            "can_delete": bool(perms and perms.manage_messages),
            "can_roles": bool(perms and perms.manage_roles),
            "invites_open": members_can_invite(guild) if guild else True}


# ------------------------- the filter -------------------------
def _exempt(member, gid: int, cfg_wl: set) -> bool:
    if member is None or member.bot or member.id in OWNER_IDS or member.id in cfg_wl:
        return True
    perms = getattr(member, "guild_permissions", None)
    return bool(perms and (perms.administrator or perms.manage_guild or perms.manage_messages))


async def _report(message, link_on_report=True) -> None:
    gid = message.guild.id
    cid = await run(report_channel_sync, gid)
    if not cid:
        return
    ch = message.guild.get_channel(cid)
    if ch is None or ch.id == message.channel.id:
        pass
    if ch is None:
        return
    body = (message.content or "").strip()
    if len(body) > 1000:
        body = body[:1000] + "\u2026"
    e = discord.Embed(title="\U0001f517 Link blocked", color=0xB42318,
                      description=f"**{message.author.mention}** sent a link in {message.channel.mention} and it was deleted.")
    if body:
        e.add_field(name="Message", value=body, inline=False)
    e.add_field(name="Let them post links?", value=f"Add their ID in the panel, or run `/allowlink` \u2014 `{message.author.id}`",
                inline=False)
    e.set_footer(text=str(message.author))
    e.timestamp = discord.utils.utcnow()
    try:
        await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as ex:
        log.warning("Link report failed in %s: %s", gid, ex)


async def _on_message(message: discord.Message) -> None:
    if message.guild is None or message.author.bot:
        return
    gid = message.guild.id
    if gid not in await run(db.allowed_guild_ids):
        return
    if not await run(on_sync, gid):
        return
    wl = await run(whitelist_sync, gid)
    if _exempt(message.author, gid, wl):
        return
    if not has_link(message.content or ""):
        return
    try:
        await message.delete()
    except discord.NotFound:
        pass
    except discord.Forbidden:
        log.warning("Can't delete a link in %s (missing Manage Messages)", gid)
        return
    except discord.HTTPException as ex:
        log.warning("Delete link failed in %s: %s", gid, ex)
        return
    await _report(message)


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
            log.exception("%s (link filter) failed", name)

    chained.__name__ = name
    setattr(bot, name, chained)


_install("on_message", _on_message)


# ------------------------- in-server commands (owner / admins) -------------------------
def _is_mod(interaction: discord.Interaction) -> bool:
    u = interaction.user
    if u.id in OWNER_IDS:
        return True
    p = getattr(u, "guild_permissions", None)
    return bool(p and (p.administrator or p.manage_guild))


@bot.tree.command(name="allowlink", description="Let a member send links (owner/admins only)")
@app_commands.guild_only()
@app_commands.describe(user="The member who may send links")
async def allowlink_cmd(interaction: discord.Interaction, user: discord.User):
    if not _is_mod(interaction):
        await interaction.response.send_message("Only the owner or an admin can use this.", ephemeral=True)
        return
    await run(add_to_whitelist, interaction.guild_id, user.id)
    await interaction.response.send_message(f"\u2705 {user.mention} can now send links. (`{user.id}`)", ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="blocklink", description="Stop a member from sending links (owner/admins only)")
@app_commands.guild_only()
@app_commands.describe(user="The member who may no longer send links")
async def blocklink_cmd(interaction: discord.Interaction, user: discord.User):
    if not _is_mod(interaction):
        await interaction.response.send_message("Only the owner or an admin can use this.", ephemeral=True)
        return
    removed = await run(remove_from_whitelist, interaction.guild_id, user.id)
    msg = f"\U0001f6ab {user.mention}'s links will be blocked again." if removed else f"{user.mention} wasn't on the allow list."
    await interaction.response.send_message(msg, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="linkfilter", description="Turn the link filter on or off (owner/admins only)")
@app_commands.guild_only()
@app_commands.describe(state="on or off")
@app_commands.choices(state=[app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")])
async def linkfilter_cmd(interaction: discord.Interaction, state: app_commands.Choice[str]):
    if not _is_mod(interaction):
        await interaction.response.send_message("Only the owner or an admin can use this.", ephemeral=True)
        return
    on = state.value == "on"
    await run(set_on, interaction.guild_id, on)
    extra = "" if content_ok() else " \u26a0\ufe0f The Message Content Intent is off, so I can't read messages yet \u2014 turn it on in the Developer Portal."
    await interaction.response.send_message(("\u2705 Link filter is ON. Links from members who aren't allowed will be deleted."
                                             if on else "Link filter is OFF.") + extra, ephemeral=True)
