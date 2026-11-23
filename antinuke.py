"""
Anti-nuke: stop someone from wrecking the server fast.

It watches for bursts of destructive admin actions in a short time \u2014 deleting or
creating many channels, deleting roles, and mass bans or kicks (and webhook spam)
\u2014 works out WHO did it from the audit log, and, depending on your choice, takes
away their roles, kicks or bans them, then alerts you. The server owner, the bot
itself, and anyone you whitelist are never touched.

How it decides: for each kind of action it counts how many that one person did in
the last few seconds. Once they pass the limit (default 3 in 15 seconds) it acts
on them once, then alerts you.

Needs on the bot's role:
- "View Audit Log"  -> to see who did the action.
- Ban Members / Kick Members / Manage Roles -> to actually stop them.
- The bot role placed ABOVE the attacker's roles (near the top).
It cannot act on the server owner or on roles higher than its own, and it cannot
un-delete channels or roles. It stops the attacker; it does not undo the damage,
so the real safety net is to keep the number of people with admin powers small and
to whitelist only the ones you trust.
"""
import asyncio
import logging
import time

import discord

import db
import discord_bot
from discord_bot import bot

log = logging.getLogger("antinuke")
run = asyncio.to_thread

DEFAULTS = {"an_on": "0", "an_limit": "3", "an_window": "15", "an_punish": "strip"}

PUNISH = [("strip", "Take away their roles (they lose power; you can give the roles back)"),
          ("kick", "Kick them out of the server"),
          ("ban", "Ban them from the server"),
          ("alert", "Just alert me, don't act")]
PUNISH_LABEL = dict(PUNISH)

# category -> (what they did, the audit-log action that proves who did it)
CATS = {
    "channel_delete": ("deleting channels", discord.AuditLogAction.channel_delete),
    "channel_create": ("creating channels", discord.AuditLogAction.channel_create),
    "role_delete": ("deleting roles", discord.AuditLogAction.role_delete),
    "ban": ("banning members", discord.AuditLogAction.ban),
    "kick": ("kicking members", discord.AuditLogAction.kick),
    "webhook": ("making webhooks", discord.AuditLogAction.webhook_create),
}

PUNISH_COOLDOWN = 60          # act on the same person at most once a minute
_hits: dict = {}             # (guild, user, category) -> [monotonic times]
_punished: dict = {}         # (guild, user) -> monotonic time we last acted
_no_audit: set = set()       # guilds where the bot lacks "View Audit Log"
_cache: dict = {}            # guild -> (monotonic time, settings) so a burst doesn't hammer the DB


# ------------------------- settings -------------------------
def _s(name: str, gid: int) -> str:
    return f"{name}:{gid}"


def on_sync(gid: int) -> bool:
    return db.get_setting(_s("an_on", gid), "0") == "1"


def whitelist_sync(gid: int) -> set:
    raw = db.get_setting(_s("an_wl", gid), "")
    return {int(x) for x in raw.replace("\n", ",").replace(" ", ",").split(",") if x.strip().isdigit()}


def log_channel_sync(gid: int) -> int:
    v = db.get_setting(_s("an_log", gid), "") or db.get_setting(f"gate_log:{gid}", "")
    return int(v) if v.isdigit() else 0


def _int(name: str, gid: int, default: int, lo: int, hi: int) -> int:
    v = db.get_setting(_s(name, gid), str(default))
    n = int(v) if v.lstrip("-").isdigit() else default
    return max(lo, min(hi, n))


def cfg_sync(gid: int) -> dict:
    return {"on": on_sync(gid),
            "limit": _int("an_limit", gid, 3, 2, 50),
            "window": _int("an_window", gid, 15, 3, 600),
            "punish": db.get_setting(_s("an_punish", gid), "strip") if db.get_setting(_s("an_punish", gid), "strip") in PUNISH_LABEL else "strip",
            "whitelist": whitelist_sync(gid),
            "log": log_channel_sync(gid)}


async def _cfg(gid: int) -> dict:
    """Settings for this guild, remembered for a few seconds so a nuke burst doesn't hit the DB every event."""
    ts, c = _cache.get(gid, (0.0, None))
    if c is not None and time.monotonic() - ts < 3:
        return c
    c = await run(cfg_sync, gid)
    _cache[gid] = (time.monotonic(), c)
    return c


def forget_cache(gid: int) -> None:
    _cache.pop(gid, None)


def status_sync(guild) -> dict:
    """For the admin page: can the bot actually protect this server right now?"""
    me = getattr(guild, "me", None) if guild else None
    perms = getattr(me, "guild_permissions", None)
    roles = getattr(guild, "roles", None) or []
    my_top = getattr(me, "top_role", None)
    top = bool(my_top and getattr(my_top, "position", 0) >= (max((getattr(r, "position", 0) for r in roles), default=0) - 1))
    return {"audit": bool(perms and perms.view_audit_log),
            "can_act": bool(perms and (perms.ban_members or perms.kick_members or perms.manage_roles)),
            "high": top,
            "blind_seen": (guild.id in _no_audit) if guild else False}


# ------------------------- who did it -------------------------
async def _culprit(guild, action, target_id=None, within=20):
    """The member who just did this action (from the audit log), or None."""
    try:
        async for e in guild.audit_logs(limit=6, action=action):
            if e.created_at and (discord.utils.utcnow() - e.created_at).total_seconds() > within:
                break
            if target_id is None or (e.target is not None and getattr(e.target, "id", None) == target_id):
                _no_audit.discard(guild.id)
                return e.user
    except discord.Forbidden:
        _no_audit.add(guild.id)
        log.warning("Anti-nuke can't read the audit log in %s (missing View Audit Log)", guild.id)
    except discord.HTTPException as e:
        log.warning("Anti-nuke audit log error in %s: %s", guild.id, e)
    return None


def _bump(gid: int, uid: int, cat: str, limit: int, window: int) -> bool:
    """Count this action; True once the person passes the limit inside the window."""
    now = time.monotonic()
    k = (gid, uid, cat)
    hits = [t for t in _hits.get(k, []) if now - t < window]
    hits.append(now)
    _hits[k] = hits
    return len(hits) >= limit


# ------------------------- acting on an attacker -------------------------
async def _trip(guild, user, cat: str, cfg: dict) -> None:
    if user is None or user.id == bot.user.id:
        return
    if user.id == guild.owner_id or user.id in cfg["whitelist"]:
        return
    if not _bump(guild.id, user.id, cat, cfg["limit"], cfg["window"]):
        return
    pk = (guild.id, user.id)
    now = time.monotonic()
    if now - _punished.get(pk, -1e9) < PUNISH_COOLDOWN:
        return
    _punished[pk] = now
    await _act(guild, user, cat, cfg)


async def _act(guild, user, cat: str, cfg: dict) -> None:
    member = guild.get_member(user.id)
    me = guild.me
    punish = cfg["punish"]
    nice = CATS[cat][0]
    reason = f"Anti-nuke: {nice} too fast"
    done, problem = "", ""

    if punish == "alert":
        pass
    elif member is None and punish == "ban":
        try:
            await guild.ban(discord.Object(id=user.id), reason=reason, delete_message_days=0)
            done = "Banned"
        except discord.HTTPException as e:
            problem = f"couldn't ban them (Discord error {getattr(e, 'status', '')})"
    elif member is None:
        problem = "they already left, so there was nothing to take away"
    elif me is None or me.top_role <= member.top_role:
        problem = "their top role is higher than the bot's, so the bot can't touch them \u2014 move the bot role up"
    else:
        try:
            if punish == "strip":
                keep = [r for r in member.roles if r.is_default() or r.managed or r >= me.top_role]
                removed = len(member.roles) - len(keep)
                if removed <= 0:
                    problem = "their roles are all above the bot's, so none could be removed \u2014 move the bot role up"
                else:
                    await member.edit(roles=keep, reason=reason)
                    done = f"Took away {removed} role(s) from"
            elif punish == "kick":
                await member.kick(reason=reason)
                done = "Kicked"
            elif punish == "ban":
                await member.ban(reason=reason, delete_message_days=0)
                done = "Banned"
        except discord.Forbidden:
            problem = "the bot is missing the permission to do that"
        except discord.HTTPException as e:
            problem = f"Discord error {getattr(e, 'status', '')}"

    await _alert(guild, user, cat, cfg, done, problem)
    log.warning("Anti-nuke tripped in %s: %s was %s -> %s %s",
                guild.id, user, nice, done or ("alert only" if punish == "alert" else "could not act"), problem)


async def _alert(guild, user, cat: str, cfg: dict, done: str, problem: str) -> None:
    nice = CATS[cat][0]
    e = discord.Embed(title="\U0001f6e1\ufe0f Anti-nuke tripped", color=0xCC2222,
                      description=f"**{user}** (`{user.id}`) was **{nice}** very fast.")
    if done:
        e.add_field(name="What I did", value=f"{done} them.", inline=False)
    elif cfg["punish"] == "alert":
        e.add_field(name="What I did", value="Nothing \u2014 you set Anti-nuke to alert only.", inline=False)
    else:
        e.add_field(name="What I did", value="I tried to stop them but couldn't.", inline=False)
    if problem:
        e.add_field(name="Heads up", value=problem.capitalize() + ".", inline=False)
    e.add_field(name="Important", value="Deleted channels and roles can't be brought back. Open **Server Settings \u2192 "
                "Audit Log** to see everything they did, and remove their admin powers if they still have any.", inline=False)
    e.set_thumbnail(url=(user.display_avatar.url if getattr(user, "display_avatar", None) else None))

    cid = cfg["log"]
    ch = guild.get_channel(cid) if cid else None
    if ch is not None:
        try:
            await ch.send(content="@here", embed=e, allowed_mentions=discord.AllowedMentions(everyone=True))
        except discord.HTTPException as err:
            log.warning("Anti-nuke alert to channel failed in %s: %s", guild.id, err)
    try:
        owner = guild.owner or await guild.fetch_member(guild.owner_id)
        if owner is not None:
            await owner.send(embed=e)
    except discord.HTTPException:
        pass


# ------------------------- Discord events (added alongside the join-gate ones) -------------------------
async def _on_channel_delete(channel):
    guild = getattr(channel, "guild", None)
    if guild is None:
        return
    cfg = await _cfg(guild.id)
    if not cfg["on"]:
        return
    await _trip(guild, await _culprit(guild, CATS["channel_delete"][1], channel.id), "channel_delete", cfg)


async def _on_channel_create(channel):
    guild = getattr(channel, "guild", None)
    if guild is None:
        return
    cfg = await _cfg(guild.id)
    if not cfg["on"]:
        return
    await _trip(guild, await _culprit(guild, CATS["channel_create"][1], channel.id), "channel_create", cfg)


async def _on_role_delete(role):
    guild = role.guild
    cfg = await _cfg(guild.id)
    if not cfg["on"]:
        return
    await _trip(guild, await _culprit(guild, CATS["role_delete"][1], role.id), "role_delete", cfg)


async def _on_ban(guild, user):
    cfg = await _cfg(guild.id)
    if not cfg["on"]:
        return
    await _trip(guild, await _culprit(guild, CATS["ban"][1], user.id), "ban", cfg)


async def _on_remove(member):
    guild = member.guild
    cfg = await _cfg(guild.id)
    if not cfg["on"]:
        return
    who = await _culprit(guild, CATS["kick"][1], member.id, within=8)   # None = a normal leave, not a kick
    if who is not None:
        await _trip(guild, who, "kick", cfg)


async def _on_webhooks(channel):
    guild = getattr(channel, "guild", None)
    if guild is None:
        return
    cfg = await _cfg(guild.id)
    if not cfg["on"]:
        return
    await _trip(guild, await _culprit(guild, CATS["webhook"][1]), "webhook", cfg)


# The bot is a plain discord.Client, which allows only ONE handler per event. The join gate (security.py)
# already set some of these, so we CHAIN: run the join-gate handler first, then ours, each guarded so one
# failing never blocks the other.
def _install(name: str, handler) -> None:
    existing = getattr(bot, name, None)

    async def chained(*args, **kw):
        if existing is not None:
            try:
                await existing(*args, **kw)
            except Exception:                 # noqa: BLE001  the other handler must still run
                log.exception("%s (join gate) failed", name)
        try:
            await handler(*args, **kw)
        except Exception:                     # noqa: BLE001
            log.exception("%s (anti-nuke) failed", name)

    chained.__name__ = name
    setattr(bot, name, chained)


for _fn, _name in [
    (_on_channel_delete, "on_guild_channel_delete"),
    (_on_channel_create, "on_guild_channel_create"),
    (_on_role_delete, "on_guild_role_delete"),
    (_on_ban, "on_member_ban"),
    (_on_remove, "on_member_remove"),
    (_on_webhooks, "on_webhooks_update"),
]:
    _install(_name, _fn)
