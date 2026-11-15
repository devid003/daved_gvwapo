"""
One slash command per prompt niche (/ghibli, /cooking, ...).
Picks a random active prompt from the Neon DB, renders a watermarked card, sends it.
"""
import asyncio
import io
import logging

import discord

import db
import security
from discord_bot import bot, OWNER_IDS
from prompts import PROMPT_NICHES, render_txt

log = logging.getLogger("prompts")
run = asyncio.to_thread


async def _is_cleared(guild_id: int, user_id: int) -> bool:
    """Passed the join gate (or gate off, or owner)."""
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
            "You're not registered yet. Run **/register** first, then wait for the owner to approve you.",
            ephemeral=True)
        return False
    if reg.status == "pending":
        await interaction.followup.send(
            f"Your registration (ID{reg.number:04d}) is still waiting for the owner's approval.", ephemeral=True)
        return False
    if reg.status != "approved":
        await interaction.followup.send("Your access was declined. Contact the server owner.", ephemeral=True)
        return False
    return True


@bot.tree.command(name="register", description="Register to use the bot")
@discord.app_commands.guild_only()
@discord.app_commands.describe(access="What do you want to use?")
@discord.app_commands.choices(access=[
    discord.app_commands.Choice(name="Image generation (/imagine)", value="image"),
    discord.app_commands.Choice(name="Prompts (/ghibli, /cooking, ...)", value="prompt"),
])
async def register_cmd(interaction: discord.Interaction, access: discord.app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    gid, user = interaction.guild_id, interaction.user
    if not await _is_cleared(gid, user.id):
        await interaction.followup.send(
            "You need to be a verified member first. Please finish the server verification, then run /register.",
            ephemeral=True)
        return

    if access.value == "image":
        status = await run(db.member_status, user.id)
        if user.id in OWNER_IDS or status == "approved":
            await run(db.set_member, user.id, "approved", str(user))
            await interaction.followup.send("You're approved for **image generation**. Use /imagine.", ephemeral=True)
            return
        if status == "denied":
            await interaction.followup.send("Your image access was declined. Contact the server owner.", ephemeral=True)
            return
        await run(db.request_access, user.id, str(user), gid)
        await interaction.followup.send(
            "Registered for **image generation**. The owner will approve you shortly, then you can use /imagine.",
            ephemeral=True)
        return

    # prompts
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
        f"Registered for **prompts** as **{user} {num}**. The owner will approve you shortly, then you can use the prompt commands.",
        ephemeral=True)


def _make_command(key: str, label: str):
    async def cmd(interaction: discord.Interaction):
        await interaction.response.defer()
        if not await _can_use_prompts(interaction):
            return
        p = await run(db.random_prompt, key)
        if p is None:
            await interaction.followup.send(
                f"No {label} prompts are available yet. The owner can add some in the dashboard.",
                ephemeral=True)
            return
        rows = await run(db.list_prompts, key, True)
        total = len(rows)
        index = next((i + 1 for i, r in enumerate(rows) if r.id == p.id), 0)
        data = await run(render_txt, key, p.text, index, total)
        await run(db.log_prompt_use, interaction.guild_id, interaction.user.id, key)
        file = discord.File(io.BytesIO(data), filename=f"{key}-prompt.txt")
        await interaction.followup.send(content=f"Here's a **{label}** prompt:", file=file)
    return cmd


def _register():
    for key, (label, _emoji, _accent) in PROMPT_NICHES.items():
        bot.tree.add_command(discord.app_commands.Command(
            name=key, description=f"Get a {label} prompt", callback=_make_command(key, label)))
    log.info("Registered %d prompt commands.", len(PROMPT_NICHES))


_register()
