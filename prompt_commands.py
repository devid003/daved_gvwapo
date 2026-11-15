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
def _int_env(name, default):
    import re
    m = re.match(r"\s*(\d+)", _os.getenv(name, "") or "")
    return int(m.group(1)) if m else default
PROMPT_DELETE_AFTER = _int_env("PROMPT_DELETE_SECONDS", 600)


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


@bot.tree.command(name="register", description="Register to use the prompt commands")
@app_commands.guild_only()
async def register_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    gid, user = interaction.guild_id, interaction.user
    if not await _is_cleared(gid, user.id):
        await interaction.followup.send(
            "You need to be a verified member first. Finish the server verification, then run /register.",
            ephemeral=True)
        return

    reg, created = await run(db.register_user, gid, user.id, str(user))
    num = f"ID{reg.number:04d}"
    if not created:
        state = {"pending": "still waiting for approval", "approved": "already approved",
                 "denied": "was declined"}.get(reg.status, reg.status)
        await interaction.followup.send(f"You're already registered for **prompts** as **{user} {num}** ({state}).", ephemeral=True)
        return
    if user.id in OWNER_IDS:
        await run(db.set_registration_status, reg.id, "approved")
    await interaction.followup.send(
        f"Registered for **prompts** as **{user} {num}**. The owner will approve you, then you can use /prompt.",
        ephemeral=True)


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
        async def _later(m=msg):
            await asyncio.sleep(PROMPT_DELETE_AFTER)
            try:
                await m.delete()
            except Exception:
                pass
        asyncio.create_task(_later())


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
