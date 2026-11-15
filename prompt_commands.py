"""
One slash command per prompt niche (/ghibli, /cooking, ...).
Picks a random active prompt from the Neon DB, renders a watermarked card, sends it.
"""
import asyncio
import io
import logging

import discord

import db
from discord_bot import bot
from prompts import PROMPT_NICHES, render_txt

log = logging.getLogger("prompts")
run = asyncio.to_thread


def _make_command(key: str, label: str):
    async def cmd(interaction: discord.Interaction):
        await interaction.response.defer()
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
        file = discord.File(io.BytesIO(data), filename=f"{key}-prompt.txt")
        await interaction.followup.send(content=f"Here's a **{label}** prompt:", file=file)
    return cmd


def _register():
    for key, (label, _emoji, _accent) in PROMPT_NICHES.items():
        bot.tree.add_command(discord.app_commands.Command(
            name=key, description=f"Get a {label} prompt", callback=_make_command(key, label)))
    log.info("Registered %d prompt commands.", len(PROMPT_NICHES))


_register()
