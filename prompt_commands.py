"""
/prompt command with a live niche dropdown (autocomplete from the database).
New niches added on the dashboard appear here immediately, no restart.
Only registered + approved members can use it.
"""
import asyncio
import io
import logging

import discord
from discord import app_commands

import db
import security
from discord_bot import bot, OWNER_IDS
from prompts import render_txt

log = logging.getLogger("prompts")
run = asyncio.to_thread
import os as _os


def _int_env(name: str, default: int) -> int:
    """Read a number from an env var; ignores extra text, falls back to default."""
    import re as _re
    m = _re.search(r"\d+", _os.getenv(name, "") or "")
    return int(m.group()) if m else default


PROMPT_DELETE_AFTER = _int_env("PROMPT_DELETE_SECONDS", 600)  # 0 = keep forever
_DELETE_TASKS: set = set()


async def _delete_later(msg, delay: int) -> None:
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


# Replies to panel buttons vanish after 5 minutes so channels stay tidy (0 = keep).
# Discord only lets the bot delete these private replies for 15 minutes, so keep it under 900.
PANEL_DELETE_AFTER = min(_int_env("PANEL_DELETE_SECONDS", 300), 840)


def _delete_reply_later(msg) -> None:
    if PANEL_DELETE_AFTER and msg is not None:
        t = asyncio.create_task(_delete_later(msg, PANEL_DELETE_AFTER))
        _DELETE_TASKS.add(t)
        t.add_done_callback(_DELETE_TASKS.discard)


async def _is_cleared(guild_id: int, user_id: int) -> bool:
    if user_id in OWNER_IDS:
        return True
    if not await run(security.gate_on_sync, guild_id):
        return True
    joins = await run(db.member_joins, guild_id, user_id)
    return any(j.status in ("approved", "allowed") for j in joins)


async def _can_use_prompts(interaction: discord.Interaction) -> bool:
    gid, uid = interaction.guild_id, interaction.user.id
    if uid in OWNER_IDS:
        return True
    reg = await run(db.get_registration, gid, uid)
    if reg is None:
        await interaction.followup.send(
            "You're not registered for prompts yet. Run **/register** and choose Prompts, "
            "then wait for the owner to approve you.", ephemeral=True)
        return False
    if reg.status == "pending":
        await interaction.followup.send(
            f"Your prompt registration (ID{reg.number:04d}) is still waiting for approval.", ephemeral=True)
        return False
    if reg.status != "approved":
        await interaction.followup.send("Your prompt access was declined. Contact the server owner.", ephemeral=True)
        return False
    return True


def _tap_view(label: str, url: str) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label=label, url=url))
    return v


async def _register_reply(gid: int, user) -> dict:
    """What /register (or the panel button) answers. Returns kwargs for followup.send."""
    if not await _is_cleared(gid, user.id):
        return {"content": "You need to be a verified member first. Finish the server verification, then try again."}
    reg, created = await run(db.register_user, gid, user.id, str(user))
    num = f"ID{reg.number:04d}"
    if reg.status == "approved":
        return {"content": f"You already have prompt access as **{user} {num}**. Tap a niche on the prompt panel."}
    if reg.status == "denied":
        return {"content": "Your prompt access was declined. Contact the server owner."}
    if user.id in OWNER_IDS:
        await run(db.set_registration_status, reg.id, "approved")
        return {"content": f"You're the owner, so you're approved as **{user} {num}**."}
    if not security.PUBLIC_URL:
        return {"content": f"Registered for **prompts** as **{user} {num}**. The owner will approve you, then you can get prompts from the prompt panel."}
    tok = await run(security.new_register_token_sync, reg.id)
    e = discord.Embed(
        title="Tap to verify",
        description=(f"You're **{user} {num}**. Tap the button, check your device once, and you can use "
                     "get prompts right away.\n\nThe link works for 24 hours and only for you. Don't share it."),
        color=0x2747C7)
    return {"embed": e, "view": _tap_view("Tap to verify", f"{security.PUBLIC_URL}/v/r/{tok}")}


async def _weblogin_reply(gid: int, user) -> dict:
    """What /weblogin (or the panel button) answers."""
    if user.id not in OWNER_IDS and not await run(db.is_registered_approved, gid, user.id):
        return {"content": "Only members with prompt access can watch the tutorials. Get prompt access first."}
    if not security.PUBLIC_URL:
        code = await run(security.new_web_signup_code_sync, user.id, str(user))
        return {"content": f"Your signup code: **{code}**. Use it on the website's /join page."}
    tok = await run(security.new_web_login_token_sync, user.id, str(user))
    e = discord.Embed(
        title="Open the tutorials",
        description=("Tap the button to open the tutorials. You'll be logged in right away, no password needed.\n\n"
                     "Use the device you'll watch on: your account stays on that one device. "
                     "The button works once, for 15 minutes. Don't share it."),
        color=0x2747C7)
    return {"embed": e, "view": _tap_view("Open tutorials", f"{security.PUBLIC_URL}/watch/go/{tok}")}


async def _verify_reply(gid: int, user) -> dict:
    """Panel 'Verify' button: the same personal device-check link the DM has."""
    if not await run(security.gate_on_sync, gid):
        return {"content": "You don't need to verify in this server. Welcome!"}
    pending = [j for j in await run(db.member_joins, gid, user.id) if j.status == "pending"]
    if not pending:
        if await _is_cleared(gid, user.id):
            return {"content": "You're already verified. Welcome!"}
        return {"content": "I can't find your join request. Leave and rejoin the server, or contact the owner."}
    j = pending[-1]
    if not security.PUBLIC_URL:
        return {"content": "Check your DMs from the bot for your verification code."}
    tok = await run(security._new_token_sync, j.id)
    await run(db.set_setting, f"dm:{j.id}", "ok")
    e = discord.Embed(
        title="Verify your access",
        description=("**1.** Tap **Verify now** below.\n"
                     "**2.** Your 6-character code will appear on the page.\n"
                     "**3.** Tap **Send code to owner** on that page. Done!\n\n"
                     "This link works only for you. Don't share it."),
        color=0x2747C7)
    return {"embed": e, "view": _tap_view("Verify now", f"{security.PUBLIC_URL}/v/{tok}")}


@bot.tree.command(name="register", description="Get access to the prompt commands")
@app_commands.guild_only()
async def register_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(ephemeral=True, **await _register_reply(interaction.guild_id, interaction.user))


@bot.tree.command(name="weblogin", description="Open the tutorials website (one tap, no password)")
@app_commands.guild_only()
async def weblogin_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(ephemeral=True, **await _weblogin_reply(interaction.guild_id, interaction.user))


# ---------- verification panel: buttons in a channel the owner picks ----------
PANEL_BUTTONS = {
    "verify": ("Verify", discord.ButtonStyle.success, "**New here?** Tap **Verify** to check your device and get approved."),
    "register": ("Get prompt access", discord.ButtonStyle.primary, "**Want the prompts?** Tap **Get prompt access**."),
    "weblogin": ("Open tutorials", discord.ButtonStyle.secondary, "**Tutorials?** Tap **Open tutorials** to watch on the website."),
}


def panel_message(guild_name: str, keys: list) -> tuple:
    """The embed and buttons for the verification panel."""
    e = discord.Embed(title=f"Welcome to {guild_name}",
                      description="\n".join(PANEL_BUTTONS[k][2] for k in keys) +
                                  "\n\nOnly you can see the bot's answer.",
                      color=0x2747C7)
    v = discord.ui.View(timeout=None)
    for k in keys:
        label, style, _ = PANEL_BUTTONS[k]
        v.add_item(discord.ui.Button(label=label, style=style, custom_id=f"syn:{k}"))
    return e, v


@bot.event
async def on_interaction(interaction: discord.Interaction):
    _cid = str((interaction.data or {}).get("custom_id", ""))
    if _cid.startswith("tkt:"):
        import tickets
        await tickets.on_component(interaction, _cid)
        return
    if interaction.type != discord.InteractionType.component:
        return
    cid = str((interaction.data or {}).get("custom_id", ""))
    if cid.startswith("synp:"):
        await interaction.response.send_message("Prompt generation has moved to the website. Open the Prompt Library from your member dashboard.", ephemeral=True)
        return
    if not cid.startswith("syn:") or cid[4:] not in PANEL_BUTTONS:
        return
    if interaction.guild_id is None or interaction.guild_id not in await run(db.allowed_guild_ids):
        await interaction.response.send_message("This bot isn't available in this server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    make = {"verify": _verify_reply, "register": _register_reply, "weblogin": _weblogin_reply}[cid[4:]]
    try:
        reply = await make(interaction.guild_id, interaction.user)
    except Exception:
        log.exception("Panel button %s failed", cid)
        reply = {"content": "Something went wrong. Try again in a moment."}
    _delete_reply_later(await interaction.followup.send(ephemeral=True, wait=True, **reply))


@bot.tree.command(name="prompt", description="Get a random prompt for a niche")
@app_commands.guild_only()
@app_commands.describe(niche="Pick a niche")
async def prompt_cmd(interaction: discord.Interaction, niche: str):
    await interaction.response.defer()
    if not await _can_use_prompts(interaction):
        return
    n = await run(db.get_niche, niche)
    if n is None or not n.active:
        await interaction.followup.send("That niche isn't available. Pick one from the list.", ephemeral=True)
        return
    p = await run(db.random_prompt, niche)
    if p is None:
        await interaction.followup.send(
            f"No **{n.label}** prompts are available yet. The owner can add some in the dashboard.", ephemeral=True)
        return
    rows = await run(db.list_prompts, niche, True)
    total = len(rows)
    index = next((i + 1 for i, r in enumerate(rows) if r.id == p.id), 0)
    data = await run(render_txt, niche, p.text, index, total, n.label)
    await run(db.log_prompt_use, interaction.guild_id, interaction.user.id, niche)
    file = discord.File(io.BytesIO(data), filename=f"{niche}-prompt.txt")
    note = f"\n-# This message will be removed in {PROMPT_DELETE_AFTER // 60} min." if PROMPT_DELETE_AFTER else ""
    msg = await interaction.followup.send(content=f"Here's a **{n.label}** prompt:{note}", file=file, wait=True)
    if PROMPT_DELETE_AFTER and msg is not None:
        t = asyncio.create_task(_delete_later(msg, PROMPT_DELETE_AFTER))
        _DELETE_TASKS.add(t)
        t.add_done_callback(_DELETE_TASKS.discard)


@prompt_cmd.autocomplete("niche")
async def _niche_autocomplete(interaction: discord.Interaction, current: str):
    rows = await run(db.list_niches, True)
    cur = (current or "").lower()
    out = []
    for n in rows:
        if cur in n.label.lower() or cur in n.key:
            name = f"{n.emoji} {n.label}".strip()
            out.append(app_commands.Choice(name=name[:100], value=n.key))
        if len(out) >= 25:
            break
    return out


async def _notify_prompt(gid: int, user, niche_label: str, prompt_text: str) -> None:
    """Post a note in the chosen channel each time a member gets a prompt (who + which niche + the prompt)."""
    try:
        raw = await run(db.get_setting, security.report_key("prompt", gid), "")
        if not raw.isdigit():
            return
        guild = bot.get_guild(gid)
        ch = guild.get_channel(int(raw)) if guild else None
        if ch is None:
            return
        who = getattr(user, "mention", None) or str(user)
        e = discord.Embed(title="\U0001f4dd Prompt generated", color=0x2747C7,
                          description=f"**{who}** got a **{niche_label}** prompt.")
        e.set_footer(text=str(user))
        e.timestamp = discord.utils.utcnow()
        await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as ex:
        log.warning("Prompt notify failed in %s: %s", gid, ex)
    except Exception:                        # noqa: BLE001  a bad notify must never break the member's prompt
        log.exception("Prompt notify error in %s", gid)


# ---------- prompt panel: one button per niche, no slash command needed ----------
BUTTONS_PER_MESSAGE = 25        # Discord's limit for one message


def _access_view() -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Get prompt access", style=discord.ButtonStyle.secondary, custom_id="syn:register"))
    return v


def prompt_panel_messages(niches: list) -> list:
    """The prompt panel as [(content, embed, view), ...]: one button per niche, A to Z, 25 per message
    (a long list continues in the next message), and "Get prompt access" last."""
    if niches:
        text = "Tap a niche to get a random prompt. Only you can see it."
    else:
        text = "No niches yet. They show up here as soon as the owner adds them."
    e = discord.Embed(title="Get a prompt", description=text + "\n\nNo access yet? Tap **Get prompt access** first.",
                      color=0x2747C7)
    order = sorted(niches, key=lambda n: ((n.label or n.key).casefold(), n.key))
    buttons = [discord.ui.Button(label=(f"{n.emoji} {n.label}".strip() or n.key)[:80],
                                 style=discord.ButtonStyle.primary, custom_id=f"synp:{n.key}"[:100]) for n in order]
    chunks = [buttons[i:i + BUTTONS_PER_MESSAGE] for i in range(0, len(buttons), BUTTONS_PER_MESSAGE)] or [[]]
    if len(chunks[-1]) == BUTTONS_PER_MESSAGE:
        chunks.append([])
    row = (len(chunks[-1]) + 4) // 5                    # "Get prompt access" on its own row when there's room
    chunks[-1].append(discord.ui.Button(label="Get prompt access", style=discord.ButtonStyle.secondary,
                                        custom_id="syn:register", row=row if row <= 4 else None))
    out = []
    for i, chunk in enumerate(chunks):
        v = discord.ui.View(timeout=None)
        for btn in chunk:
            v.add_item(btn)
        out.append((None, e, v) if i == 0 else ("**More niches**", None, v))
    return out


async def _prompt_reply(gid: int, user, niche_key: str) -> dict:
    """What a niche button answers (only the member who tapped sees it)."""
    if await run(db.get_setting, "site_maintenance", "0") == "1":
        return {"content": "🛠️ The website is currently under maintenance. Prompt downloads are temporarily disabled. Please try again later."}
    if user.id not in OWNER_IDS:     # prompt access = Get prompt access (device check), approved on the Prompts page
        reg = await run(db.get_registration, gid, user.id)
        if reg is None:
            return {"content": "You don't have prompt access yet. Tap **Get prompt access** below, then try again.",
                    "view": _access_view()}
        if reg.status == "pending":
            return {"content": f"Your prompt access (ID{reg.number:04d}) is still waiting. Tap **Get prompt access** "
                               "to finish the device check.", "view": _access_view()}
        if reg.status != "approved":
            return {"content": "Your prompt access was declined. Contact the server owner."}
    n = await run(db.get_niche, niche_key)
    if n is None or not n.active:
        return {"content": "That niche isn't available anymore."}
    p = await run(db.random_prompt, niche_key)
    if p is None:
        return {"content": f"No **{n.label}** prompts yet. Check back soon."}
    rows = await run(db.list_prompts, niche_key, True)
    index = next((i + 1 for i, r in enumerate(rows) if r.id == p.id), 0)
    data = await run(render_txt, niche_key, p.text, index, len(rows), n.label)
    await run(db.log_prompt, gid, user.id, str(user), niche_key, n.label, p.id, p.text)
    asyncio.create_task(_notify_prompt(gid, user, n.label, p.text))
    note = (f"\n-# Download it now. This message disappears in {PANEL_DELETE_AFTER // 60} min."
            if PANEL_DELETE_AFTER >= 60 else "")
    return {"content": f"Here's a **{n.label}** prompt:{note}",
            "file": discord.File(io.BytesIO(data), filename=f"{niche_key}-prompt.txt")}


async def _prompt_button(interaction: discord.Interaction, niche_key: str) -> None:
    if interaction.guild_id is None or interaction.guild_id not in await run(db.allowed_guild_ids):
        await interaction.response.send_message("This bot isn't available in this server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        reply = await _prompt_reply(interaction.guild_id, interaction.user, niche_key)
    except Exception:
        log.exception("Prompt button %s failed", niche_key)
        reply = {"content": "Something went wrong. Try again in a moment."}
    _delete_reply_later(await interaction.followup.send(ephemeral=True, wait=True, **reply))


# Buttons replace the slash commands: members tap the welcome panel and the prompt panel instead.
for _name in ("prompt", "register", "weblogin"):
    bot.tree.remove_command(_name)


async def remove_legacy_prompt_panels() -> None:
    """Delete previously posted Discord niche panels; prompt generation now lives on the website."""
    try:
        guild_ids = await run(db.allowed_guild_ids)
        for gid in guild_ids:
            raw = await run(db.get_setting, f"ppanel_msg:{gid}", "")
            ch_raw, _, mids_raw = (raw or "").partition("|")
            channel_id = int(ch_raw) if ch_raw.isdigit() else 0
            channel = bot.get_channel(channel_id) if channel_id else None
            if channel is None and channel_id and bot.is_ready():
                try:
                    channel = await bot.fetch_channel(channel_id)
                except Exception:
                    channel = None
            if channel is not None:
                for mid in (x for x in mids_raw.split(",") if x.isdigit()):
                    try:
                        msg = await channel.fetch_message(int(mid))
                        await msg.delete()
                    except discord.NotFound:
                        pass
                    except Exception:
                        log.warning("Could not delete legacy prompt panel message %s in guild %s", mid, gid)
            await run(db.set_setting, f"ppanel_msg:{gid}", "")
            await run(db.set_setting, f"ppanel_ch:{gid}", "")
        log.info("Legacy Discord prompt panels cleanup completed; prompt generation is website-only.")
    except Exception:
        log.exception("Legacy prompt panel cleanup failed")


async def notify_website_prompt(guild_id: int, discord_user_id: int, username: str,
                                niche_label: str, prompt_text: str) -> None:
    """Send website prompt-generation activity to the configured Discord prompt log channel."""
    try:
        raw = await run(db.get_setting, security.report_key("prompt", guild_id), "")
        if not raw.isdigit():
            log.info("No prompt log channel configured for guild %s", guild_id)
            return
        guild = bot.get_guild(guild_id)
        channel = guild.get_channel(int(raw)) if guild else None
        if channel is None:
            log.warning("Prompt log channel %s is unavailable for guild %s", raw, guild_id)
            return
        user = bot.get_user(discord_user_id) if discord_user_id else None
        who = user.mention if user else f"{username} (Discord ID: {discord_user_id})"
        embed = discord.Embed(
            title="Website prompt generated",
            description=f"**Member:** {who}\n**Niche:** {niche_label}\n\n**Prompt:**\n{prompt_text[:3000]}",
            color=0x5865F2)
        embed.set_footer(text=f"Website Prompt Library • {username}")
        embed.timestamp = discord.utils.utcnow()
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        log.exception("Website prompt log notification failed for guild %s", guild_id)

