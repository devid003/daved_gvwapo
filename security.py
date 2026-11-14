"""
Join gate: new members wait for approval before they can see the server.

- When someone joins an approved server with the gate ON, the bot gives them the
  "Pending approval" role. That role is denied "View Channel" on every channel,
  so they see nothing until the owner approves them in the panel.
- Every join is logged with account age, avatar and risk flags.
- Approve = remove the role. Deny = kick. Ban = ban.
Needs: "Server Members Intent" ON in the Developer Portal, and the bot role needs
Manage Roles, Manage Channels, Kick Members and Ban Members, placed ABOVE the
"Pending approval" role.
"""
import asyncio
import datetime as dt
import logging

import discord

import db
import discord_bot
from discord_bot import bot

log = logging.getLogger("security")
run = asyncio.to_thread

ROLE_NAME = "Pending approval"


# ------------------------- settings -------------------------
def _key(name: str, gid: int) -> str:
    return f"{name}:{gid}"


def gate_on_sync(gid: int) -> bool:
    return db.get_setting(_key("gate", gid), "0") == "1"


def log_channel_sync(gid: int) -> int:
    v = db.get_setting(_key("gate_log", gid), "")
    return int(v) if v.isdigit() else 0


def members_intent_ok() -> bool:
    return bot._connection._intents.members and not discord_bot.GATE_STATE["intent_missing"]


# ------------------------- helpers -------------------------
def risk_flags(member: discord.abc.User) -> list[str]:
    flags = []
    age_days = (discord.utils.utcnow() - member.created_at).days
    if age_days < 7:
        flags.append(f"New account ({age_days}d old)")
    elif age_days < 30:
        flags.append(f"Young account ({age_days}d old)")
    if member.avatar is None:
        flags.append("No profile picture")
    if member.bot:
        flags.append("Bot")
    return flags


def _naive_utc(t: dt.datetime) -> dt.datetime:
    return t.astimezone(dt.timezone.utc).replace(tzinfo=None)


async def find_role(guild: discord.Guild) -> discord.Role | None:
    rid = await run(db.get_setting, _key("gate_role", guild.id), "")
    role = guild.get_role(int(rid)) if rid.isdigit() else None
    return role or discord.utils.get(guild.roles, name=ROLE_NAME)


async def ensure_role(guild: discord.Guild) -> discord.Role:
    role = await find_role(guild)
    if role is None:
        role = await guild.create_role(name=ROLE_NAME, permissions=discord.Permissions.none(),
                                       reason="Join gate: members waiting for approval")
    await run(db.set_setting, _key("gate_role", guild.id), role.id)
    return role


async def hide_channel(channel, role: discord.Role) -> bool:
    if channel.overwrites_for(role).view_channel is False:
        return True
    try:
        await channel.set_permissions(role, view_channel=False,
                                      reason="Join gate: hide from members waiting for approval")
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def notify(guild: discord.Guild, embed: discord.Embed):
    cid = await run(log_channel_sync, guild.id)
    if not cid:
        return
    ch = guild.get_channel(cid)
    if ch is None:
        return
    try:
        await ch.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning("Join gate log message failed in %s: %s", guild.id, e)


# ------------------------- turning the gate on/off -------------------------
async def enable_gate(guild: discord.Guild) -> str:
    """Create the role, hide every channel from it, switch the gate on. Returns a short report."""
    role = await ensure_role(guild)
    ok = failed = 0
    for ch in guild.channels:
        if await hide_channel(ch, role):
            ok += 1
        else:
            failed += 1
        await asyncio.sleep(0.25)
    await run(db.set_setting, _key("gate", guild.id), "1")
    msg = f"Join gate is ON for {guild.name}. Hidden {ok} channel(s) from new members."
    if failed:
        msg += f" Could not change {failed} channel(s): give the bot Manage Channels there."
    log.info(msg)
    return msg


async def disable_gate(guild_id: int):
    await run(db.set_setting, _key("gate", guild_id), "0")


# ------------------------- decisions -------------------------
async def decide(join_id: int, action: str) -> str:
    """action: approve | deny | ban. Returns a message for the panel."""
    req = await run(db.get_join, join_id)
    if req is None:
        return "That request no longer exists."
    if req.status != "pending":
        return f"Already handled ({req.status})."
    guild = bot.get_guild(req.guild_id)
    if guild is None:
        return "The bot is not connected to that server right now."
    member = guild.get_member(req.user_id)
    if member is None:
        try:
            member = await guild.fetch_member(req.user_id)
        except discord.NotFound:
            member = None
        except discord.HTTPException:
            return "Discord did not answer. Try again in a moment."
    name = req.username or str(req.user_id)

    try:
        if action == "approve":
            if member is None:
                await run(db.set_join_status, join_id, "left")
                return f"{name} already left the server."
            role = await find_role(guild)
            if role and role in member.roles:
                await member.remove_roles(role, reason="Join gate: approved in panel")
            await run(db.set_join_status, join_id, "approved")
            await notify(guild, discord.Embed(description=f"\u2705 **{name}** was approved.", color=0x12805A))
            return f"\u2705 {name} approved. They can see the server now."

        if action == "deny":
            if member is not None:
                await member.kick(reason="Join gate: denied in panel")
            await run(db.set_join_status, join_id, "denied")
            await notify(guild, discord.Embed(description=f"\U0001f6ab **{name}** was denied and removed.", color=0xB42318))
            return f"\U0001f6ab {name} denied and removed from the server."

        if action == "ban":
            await guild.ban(discord.Object(id=req.user_id), reason="Join gate: banned in panel",
                            delete_message_seconds=0)
            await run(db.set_join_status, join_id, "banned")
            await notify(guild, discord.Embed(description=f"\u26d4 **{name}** was banned.", color=0xB42318))
            return f"\u26d4 {name} banned."
    except discord.Forbidden:
        return ("The bot is missing permission. Give its role Manage Roles, Kick Members and "
                "Ban Members, and drag it above the \u201cPending approval\u201d role.")
    except discord.HTTPException as e:
        return f"Discord error: {e.status}. Try again."
    return "Unknown action."


# ------------------------- Discord events -------------------------
@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    if guild.id not in await run(db.allowed_guild_ids):
        return
    flags = risk_flags(member)
    created = _naive_utc(member.created_at)
    avatar = member.display_avatar.url if member.display_avatar else ""
    gate = await run(gate_on_sync, guild.id)

    if not gate or member.bot:
        await run(db.add_join, guild.id, member.id, str(member), avatar, created,
                  ", ".join(flags), "allowed")
        log.info("Join logged (gate %s): %s in %s", "off" if not gate else "bot", member, guild.name)
        return

    if await run(db.was_approved_before, guild.id, member.id):
        flags.append("Approved before")
    try:
        role = await ensure_role(guild)
        await member.add_roles(role, reason="Join gate: waiting for approval")
    except (discord.Forbidden, discord.HTTPException) as e:
        flags.append("Could not lock (check bot permissions)")
        log.warning("Could not give Pending role to %s in %s: %s", member, guild.id, e)

    join_id = await run(db.add_join, guild.id, member.id, str(member), avatar, created,
                        ", ".join(flags), "pending")
    log.info("Join gate: %s is waiting for approval in %s (#%s)", member, guild.name, join_id)

    e = discord.Embed(title="\U0001f6c2 New member waiting for approval", color=0xA35C00)
    e.add_field(name="Member", value=f"{member.mention}\n`{member}`", inline=True)
    e.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
    e.add_field(name="Flags", value=", ".join(flags) if flags else "None", inline=False)
    e.set_thumbnail(url=avatar or None)
    e.set_footer(text="Approve or deny in the bot panel \u2192 Security")
    await notify(guild, e)


@bot.event
async def on_member_remove(member: discord.Member):
    await run(db.mark_join_left, member.guild.id, member.id)


@bot.event
async def on_guild_channel_create(channel):
    """Keep new channels hidden from pending members too."""
    guild = channel.guild
    if not await run(gate_on_sync, guild.id):
        return
    role = await find_role(guild)
    if role:
        await hide_channel(channel, role)
