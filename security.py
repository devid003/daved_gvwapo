"""
Join gate: new members wait for approval before they can see the server.

- When someone joins an approved server with the gate ON, the bot gives them the
  "Pending approval" role. That role is denied "View Channel" on every channel,
  so they see nothing until the owner approves them in the panel.
- Every join is logged with account age, avatar and risk flags.
- While pending, any OTHER role they get (Onboarding, auto-role or verification
  bots) is taken away and remembered, because a role that allows a channel beats
  the Pending deny. Approve gives those roles back.
- The bot DMs each new member a 6-character code. They give it to the owner, who
  types it in the panel to approve them (the panel never shows the code).
- Approve = remove the role. Deny = kick. Ban = ban.
Needs: "Server Members Intent" ON in the Developer Portal, and the bot role needs
Manage Roles, Manage Channels, Kick Members and Ban Members, placed ABOVE the
"Pending approval" role.
"""
import asyncio
import datetime as dt
import logging
import secrets
import hashlib
import hmac
import os

import discord
from zoneinfo import ZoneInfo

import db
import discord
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


# ------------------------- holding extra roles while pending -------------------------
_STRIPS: dict[tuple[int, int, int], int] = {}   # (guild, user, role) -> times removed


def _held_key(gid: int, uid: int) -> str:
    return f"held:{gid}:{uid}"


def held_roles_sync(gid: int, uid: int) -> list[int]:
    v = db.get_setting(_held_key(gid, uid), "")
    return [int(x) for x in v.split(",") if x.isdigit()]


def _save_held_sync(gid: int, uid: int, ids: list[int]) -> None:
    db.set_setting(_held_key(gid, uid), ",".join(str(i) for i in dict.fromkeys(ids)))


async def strip_roles(member: discord.Member, pending: discord.Role, only=None) -> list[str]:
    """Take away every role except Pending (or just `only`), remember them for approval."""
    guild = member.guild
    top = guild.me.top_role if guild.me else None
    candidates = only if only is not None else member.roles
    removable = []
    for r in candidates:
        if r.is_default() or r.id == pending.id or r.managed or (top is not None and not r < top):
            continue
        key = (guild.id, member.id, r.id)
        _STRIPS[key] = _STRIPS.get(key, 0) + 1
        if _STRIPS[key] > 3:          # another bot keeps re-adding it: stop fighting
            log.warning("Role %s keeps coming back on %s; leaving it.", r.name, member)
            continue
        removable.append(r)
    if not removable:
        return []
    try:
        await member.remove_roles(*removable, reason="Join gate: roles held until approved")
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning("Could not hold roles for %s: %s", member, e)
        return []
    held = await run(held_roles_sync, guild.id, member.id)
    await run(_save_held_sync, guild.id, member.id, held + [r.id for r in removable])
    names = [r.name for r in removable]
    log.info("Join gate: holding roles %s from %s until approved", names, member)
    return names


async def restore_roles(member: discord.Member) -> list[str]:
    guild = member.guild
    ids = await run(held_roles_sync, guild.id, member.id)
    await run(db.set_setting, _held_key(guild.id, member.id), "")
    top = guild.me.top_role if guild.me else None
    roles = [r for r in (guild.get_role(i) for i in ids)
             if r is not None and not r.managed and (top is None or r < top)]
    if roles:
        try:
            await member.add_roles(*roles, reason="Join gate: approved, roles given back")
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("Could not give back roles to %s: %s", member, e)
            return []
    return [r.name for r in roles]


# ------------------------- verification codes -------------------------
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O or 1/I to avoid mix-ups
MAX_TRIES = 5
_CODE_KEY = (os.getenv("SECRET_KEY") or "dabid-bots-code-key").encode()


def _hash_code(code: str) -> str:
    """Codes are stored hashed, so a database dump does not reveal them."""
    return hmac.new(_CODE_KEY, code.encode(), hashlib.sha256).hexdigest()


def _code_matches(saved: str, code: str) -> bool:
    if len(saved) == 64:                      # hashed (new)
        return hmac.compare_digest(saved, _hash_code(code))
    return hmac.compare_digest(saved, code)   # plain codes saved before this update


def new_code() -> str:
    return "".join(secrets.choice(CODE_CHARS) for _ in range(6))


def code_status_sync(join_id: int) -> str:
    """'sent', 'dm_closed' or '' (no code)."""
    if not db.get_setting(f"code:{join_id}", ""):
        return ""
    return "sent" if db.get_setting(f"dm:{join_id}", "") == "ok" else "dm_closed"


def _clear_code_sync(join_id: int) -> None:
    db.set_setting(f"code:{join_id}", "")
    db.set_setting(f"dm:{join_id}", "")
    tok = db.get_setting(f"vtok_for:{join_id}", "")
    if tok:
        db.set_setting(f"vtok:{tok}", "")


# ------------------------- device check (web link) -------------------------
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
_TOKEN_TRIES: dict[str, int] = {}

_UA_OS = [("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
          ("Mac OS X", "Mac"), ("CrOS", "ChromeOS"), ("Linux", "Linux")]
_UA_BR = [("Edg/", "Edge"), ("OPR/", "Opera"), ("SamsungBrowser", "Samsung Internet"), ("CriOS", "Chrome"),
          ("FxiOS", "Firefox"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari")]


def _h(value: str) -> str:
    """One-way hash, so raw IPs and fingerprints are never stored."""
    return hmac.new(_CODE_KEY, value.encode(), hashlib.sha256).hexdigest() if value else ""


def describe_ua(ua: str) -> str:
    os_name = next((n for k, n in _UA_OS if k in ua), "Unknown device")
    browser = next((n for k, n in _UA_BR if k in ua), "browser")
    return f"{os_name}, {browser}"


def _new_token_sync(join_id: int) -> str:
    old = db.get_setting(f"vtok_for:{join_id}", "")
    if old:
        db.set_setting(f"vtok:{old}", "")
    tok = secrets.token_urlsafe(24)
    db.set_setting(f"vtok:{tok}", str(join_id))
    db.set_setting(f"vtok_for:{join_id}", tok)
    return tok


def join_for_token_sync(token: str):
    if not token or len(token) > 64:
        return None
    jid = db.get_setting(f"vtok:{token}", "")
    if not jid.isdigit():
        return None
    req = db.get_join(int(jid))
    return req if req is not None and req.status == "pending" else None


def panel_status_sync(join_id: int) -> dict:
    """What the panel shows in the Code column for a waiting member."""
    dev = db.latest_device(join_id)
    has_code = bool(db.get_setting(f"code:{join_id}", ""))
    dm = db.get_setting(f"dm:{join_id}", "")
    if dev is not None:
        state = "code_shown" if has_code else "blocked"
    elif dm == "fail":
        state = "dm_closed"
    elif dm == "ok":
        state = "link_sent" if PUBLIC_URL else ("sent" if has_code else "")
    else:
        state = ""
    return {"state": state, "device": f"{dev.device}, {dev.tz}" if dev else ""}


async def check_device(token: str, cookie_id: str, fp: dict, ip: str, ua: str) -> dict:
    """Called by the verification web page. Returns the code only for a clean device."""
    _TOKEN_TRIES[token] = _TOKEN_TRIES.get(token, 0) + 1
    if _TOKEN_TRIES[token] > 10:
        return {"ok": False, "msg": "Too many tries. Ask the server owner to send you a new link."}
    req = await run(join_for_token_sync, token)
    if req is None:
        return {"ok": False, "msg": "This link has expired or was already used. Ask the server owner for a new one."}
    keys = ("platform", "tz", "langs", "screen", "depth", "ratio", "cores", "mem", "touch", "canvas", "gpu")
    fp_h = _h("|".join(str(fp.get(k, ""))[:300] for k in keys))
    cookie_h, ip_h = _h(cookie_id or ""), _h(ip or "")
    device, tz = describe_ua(ua or ""), str(fp.get("tz", ""))[:64]
    matches = await run(db.device_matches, req.guild_id, req.user_id, cookie_h, fp_h, ip_h)
    await run(lambda: db.add_device_check(
        guild_id=req.guild_id, user_id=req.user_id, username=req.username, join_id=req.id,
        cookie_hash=cookie_h, fp_hash=fp_h, ip_hash=ip_h, device=device, tz=tz,
        screen=str(fp.get("screen", ""))[:32]))
    strong = [m for m in matches if m["rank"] >= 2]
    keep = [f for f in (req.flags or "").split(", ")
            if f and not f.startswith(("Same device as", "Same browser as", "Same network as"))]
    keep += [f"{m['kind'].capitalize()} as {m['username'] or m['user_id']}" for m in matches]
    await run(db.set_join_flags, req.id, ", ".join(keep))
    guild = bot.get_guild(req.guild_id)
    if strong:
        log.warning("Duplicate device: %s matches %s", req.username, [m["username"] for m in strong])
        if guild is not None:
            e = discord.Embed(title="\u26a0\ufe0f Possible duplicate account", color=0xB42318,
                              description=(f"**{req.username}** opened the verification link on a device "
                                           f"already used by **{strong[0]['username'] or strong[0]['user_id']}**. "
                                           "Their code was not shown."))
            e.set_footer(text="Review in the bot panel \u2192 Security")
            await notify(guild, e)
        return {"ok": False, "msg": "This device is already linked to another account in this server. "
                                    "Please contact the server owner."}
    code = new_code()
    await run(db.set_setting, f"code:{req.id}", _hash_code(code))
    return {"ok": True, "code": code, "server": guild.name if guild else ""}


async def send_code(guild: discord.Guild, member, join_id: int) -> bool:
    """DM a verification link (device check first). Falls back to a plain code without PUBLIC_URL."""
    if not PUBLIC_URL:
        return await _send_code_dm_only(guild, member, join_id)
    tok = await run(_new_token_sync, join_id)
    e = discord.Embed(
        title=f"Verify your access to {guild.name}",
        description=("To keep the server safe, please verify your device first.\n\n"
                     "**1.** Tap **Verify now** below.\n"
                     "**2.** Your 6-character code will appear on the page.\n"
                     "**3.** Send that code to the server owner.\n\n"
                     "This link works only for you. Don't share it."),
        color=0x2747C7,
    )
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Verify now", url=f"{PUBLIC_URL}/v/{tok}"))
    try:
        await member.send(embed=e, view=view)
        await run(db.set_setting, f"dm:{join_id}", "ok")
        return True
    except (discord.Forbidden, discord.HTTPException):
        await run(db.set_setting, f"dm:{join_id}", "fail")
        log.info("Could not DM verification link to %s (DMs closed)", member)
        return False


# ------------------------- verified phone (Discord setting) -------------------------
def phone_required(guild: discord.Guild) -> bool:
    return guild.verification_level == discord.VerificationLevel.highest


async def set_phone_required(guild: discord.Guild, on: bool) -> str:
    level = discord.VerificationLevel.highest if on else discord.VerificationLevel.medium
    try:
        await guild.edit(verification_level=level, reason="Set from the bot panel")
    except discord.Forbidden:
        return "The bot needs the Manage Server permission to change this."
    except discord.HTTPException as e:
        return f"Discord error: {e.status}. Try again."
    return ("Verified phone is now required. Accounts without a phone number can't chat in the server."
            if on else "Verified phone is no longer required.")


async def _send_code_dm_only(guild: discord.Guild, member, join_id: int) -> bool:
    code = new_code()
    await run(db.set_setting, f"code:{join_id}", code)
    e = discord.Embed(
        title=f"Verification code for {guild.name}",
        description=(f"Your code is **`{code}`**\n\n"
                     "Send this code to the server owner. They will enter it to let you in.\n"
                     "Don't share it with anyone else."),
        color=0x2747C7,
    )
    try:
        await member.send(embed=e)
        await run(db.set_setting, f"dm:{join_id}", "ok")
        return True
    except (discord.Forbidden, discord.HTTPException):
        await run(db.set_setting, f"dm:{join_id}", "fail")
        log.info("Could not DM code to %s (DMs closed)", member)
        return False


async def resend_code(join_id: int) -> str:
    req = await run(db.get_join, join_id)
    if req is None or req.status != "pending":
        return "That member is no longer waiting."
    guild = bot.get_guild(req.guild_id)
    member = guild.get_member(req.user_id) if guild else None
    if member is None:
        return "That member is not in the server anymore."
    ok = await send_code(guild, member, join_id)
    return (f"New code sent to {req.username} by DM." if ok else
            f"{req.username} has DMs closed. Ask them to allow DMs from server members, then resend.")


_TRIES: dict[str, int] = {"n": 0}


async def verify_code(raw: str) -> str:
    code = "".join(ch for ch in (raw or "").upper() if ch.isalnum())
    if len(code) != 6:
        return "Codes are 6 characters, like K7MQ2X."
    _TRIES["n"] += 1
    for j in await run(db.list_joins, None, True, 500):
        saved = await run(db.get_setting, f"code:{j.id}", "")
        if saved and _code_matches(saved, code):
            _TRIES["n"] = 0
            return await decide(j.id, "approve")
    if _TRIES["n"] >= MAX_TRIES:
        await asyncio.sleep(2)        # slow down repeated wrong guesses
    return "No one waiting has that code. Check it and try again."


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


# ------------------------- member nickname -------------------------
async def set_member_number_nick(guild_id: int, user_id: int, base_name: str, number: int) -> str:
    """Rename the member to 'Name ID 01'. Returns a short status for the panel."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return ""
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.HTTPException):
            return ""
    clean = (base_name or member.name).split(" ID ")[0].strip()
    nick = f"{clean} ID {number:02d}"[:32]
    try:
        await member.edit(nick=nick, reason="Registered member number")
        return f" Renamed to {nick}."
    except discord.Forbidden:
        return " (Could not rename: give the bot Manage Nicknames and put its role above theirs.)"
    except discord.HTTPException:
        return ""


# ------------------------- warning message -------------------------
MOD_NAME = os.getenv("MOD_NAME", "Syndicate Moderation")
MOD_AVATAR = os.getenv("MOD_AVATAR", "").strip()


def _ordinal(n: int) -> str:
    return f"{n}th" if 11 <= n % 100 <= 13 else f"{n}{ {1:'st',2:'nd',3:'rd'}.get(n % 10, 'th') }"


def _warn_fields(reason: str, total: int, when):
    tz = ZoneInfo(os.getenv("DISPLAY_TZ", "Asia/Manila"))
    local = when.astimezone(tz)
    return {
        "reason": reason,
        "date": local.strftime("%A, %B ") + f"{local.day}, {local.year}",
        "time": local.strftime("%I:%M %p").lstrip("0") + f" ({os.getenv('DISPLAY_TZ', 'Asia/Manila')})",
        "total": total,
    }


def _warn_embed(guild_name: str, member_name: str, f: dict) -> discord.Embed:
    e = discord.Embed(
        title=f"\u26a0\ufe0f Warning from {guild_name}",
        description="You have received a warning from the server moderators.",
        color=0xC98A00, timestamp=discord.utils.utcnow(),
    )
    e.add_field(name="Member", value=member_name, inline=False)
    e.add_field(name="Reason", value=f["reason"], inline=False)
    e.add_field(name="Date", value=f["date"], inline=True)
    e.add_field(name="Time", value=f["time"], inline=True)
    e.add_field(name="Total warnings", value=str(f["total"]), inline=True)
    e.set_footer(text=f"{MOD_NAME} \u2022 Please follow the server rules to avoid further action.")
    return e


async def send_warning(guild_id: int, user_id: int, member_name: str, reason: str, total: int, when) -> dict:
    """DM the member AND post to the mod-log webhook/channel under the moderation name. Returns what worked."""
    guild = bot.get_guild(guild_id)
    f = _warn_fields(reason, total, when)
    result = {"dm": False, "log": False}
    if guild is None:
        return result

    # 1) real DM to the member
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.HTTPException):
            member = None
    if member is not None:
        try:
            await member.send(embed=_warn_embed(guild.name, member_name, f))
            result["dm"] = True
        except (discord.Forbidden, discord.HTTPException):
            pass

    # 2) mod-log: webhook (named + avatar) if we can, else a normal message
    cid = await run(log_channel_sync, guild_id)
    channel = guild.get_channel(cid) if cid else None
    if channel is not None:
        embed = _warn_embed(guild.name, member_name, f)
        mention = member.mention if member is not None else f"<@{user_id}>"
        try:
            hook = await _mod_webhook(channel)
            if hook is not None:
                await hook.send(content=f"{mention}", embed=embed, username=MOD_NAME,
                                avatar_url=MOD_AVATAR or None,
                                allowed_mentions=discord.AllowedMentions(users=True))
            else:
                await channel.send(content=mention, embed=embed,
                                   allowed_mentions=discord.AllowedMentions(users=True))
            result["log"] = True
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("Warning mod-log failed in %s: %s", guild_id, e)
    return result


async def _mod_webhook(channel):
    """Find or create a reusable webhook named after the moderation identity."""
    try:
        hooks = await channel.webhooks()
        for h in hooks:
            if h.name == MOD_NAME and h.token:
                return h
        return await channel.create_webhook(name=MOD_NAME, reason="Moderation messages")
    except (discord.Forbidden, discord.HTTPException):
        return None


# ------------------------- approval message -------------------------
async def send_welcome(guild: discord.Guild, member: discord.Member) -> bool:
    """Tell the member they are in. Fails quietly if their DMs are closed."""
    e = discord.Embed(
        title="\u2705 You're approved",
        description=(f"Welcome to **{guild.name}**.\n\n"
                     "Your verification code was confirmed and your access has been approved. "
                     "All channels are now open to you.\n\n"
                     "Please take a moment to read the server rules before you start. "
                     "We're glad to have you here."),
        color=0x12805A, timestamp=discord.utils.utcnow(),
    )
    if guild.icon:
        e.set_thumbnail(url=guild.icon.url)
    e.set_footer(text=f"{guild.name} \u2022 Member verification")
    try:
        await member.send(embed=e)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


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
    await run(_clear_code_sync, join_id)

    try:
        if action == "approve":
            if member is None:
                await run(db.set_join_status, join_id, "left")
                return f"{name} already left the server."
            role = await find_role(guild)
            if role and role in member.roles:
                await member.remove_roles(role, reason="Join gate: approved in panel")
            back = await restore_roles(member)
            await send_welcome(guild, member)
            await run(db.set_join_status, join_id, "approved")
            await notify(guild, discord.Embed(description=f"\u2705 **{name}** was approved.", color=0x12805A))
            extra = f" Gave back: {', '.join(back)}." if back else ""
            return f"\u2705 {name} approved. They can see the server now.{extra}"

        if action == "deny":
            if member is not None:
                await member.kick(reason="Join gate: denied in panel")
            await run(db.set_setting, _held_key(guild.id, req.user_id), "")
            await run(db.set_join_status, join_id, "denied")
            await notify(guild, discord.Embed(description=f"\U0001f6ab **{name}** was denied and removed.", color=0xB42318))
            return f"\U0001f6ab {name} denied and removed from the server."

        if action == "ban":
            await guild.ban(discord.Object(id=req.user_id), reason="Join gate: banned in panel",
                            delete_message_seconds=0)
            await run(db.set_setting, _held_key(guild.id, req.user_id), "")
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
    role = None
    try:
        role = await ensure_role(guild)
        await member.add_roles(role, reason="Join gate: waiting for approval")
    except (discord.Forbidden, discord.HTTPException) as e:
        flags.append("Could not lock (check bot permissions)")
        log.warning("Could not give Pending role to %s in %s: %s", member, guild.id, e)
    if role is not None:
        await strip_roles(member, role)

    join_id = await run(db.add_join, guild.id, member.id, str(member), avatar, created,
                        ", ".join(flags), "pending")
    log.info("Join gate: %s is waiting for approval in %s (#%s)", member, guild.name, join_id)
    dm_ok = await send_code(guild, member, join_id)

    e = discord.Embed(title="\U0001f6c2 New member waiting for approval", color=0xA35C00)
    e.add_field(name="Member", value=f"{member.mention}\n`{member}`", inline=True)
    e.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
    e.add_field(name="Flags", value=", ".join(flags) if flags else "None", inline=False)
    e.add_field(name="Code", inline=False,
                value="Sent by DM. Ask them for it and enter it in the panel." if dm_ok else
                "Their DMs are closed. Use Resend code in the panel after they open DMs.")
    e.set_thumbnail(url=avatar or None)
    e.set_footer(text="Verify the code or decide in the bot panel \u2192 Security")
    await notify(guild, e)

    if role is not None:              # other bots often add roles a few seconds later
        await asyncio.sleep(4)
        fresh = guild.get_member(member.id)
        if fresh is not None and role in fresh.roles:
            await strip_roles(fresh, role)


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """If a pending member gets any new role, hold it until they are approved."""
    added = [r for r in after.roles if r not in before.roles]
    if not added:
        return
    guild = after.guild
    pending = await find_role(guild)
    if pending is None or pending not in after.roles:
        return
    if not await run(gate_on_sync, guild.id):
        return
    await strip_roles(after, pending, only=[r for r in added if r.id != pending.id])


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
