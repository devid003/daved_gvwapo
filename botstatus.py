"""
Bot status / health monitor.

- Keeps ONE status message in a channel you pick and updates it every couple of minutes with a color:
    GREEN  = online, everything working (prompts + website OK)
    YELLOW = slow (high ping) -- prompts/website may lag
    RED    = down -- prompts can't be used and the website is unavailable
- Shows the ping (ms), uptime, and the date/time it was last checked (with the year).
- `/status` and `/ping` let anyone check on demand.

Honest limit: a bot that is TOTALLY dead can't post anything, so on a hard crash the Discord message just stops
updating -- an old "Checked" time means it's down. The website Status page always shows the true current color,
and on a planned restart/deploy the bot posts RED before it goes offline.
"""
import asyncio
import logging
import math
import time

import discord
from discord import app_commands
from discord.ext import tasks

import db
import discord_bot
from discord_bot import bot

log = logging.getLogger("health")
run = asyncio.to_thread

_BOOT = time.time()

# ping thresholds, in milliseconds
GREEN_MS = 400
YELLOW_MS = 1500


# ------------------------- settings -------------------------
def _s(name: str, gid: int) -> str:
    return f"{name}:{gid}"


def on_sync(gid: int) -> bool:
    return db.get_setting(_s("health_on", gid), "0") == "1"


def set_on(gid: int, on: bool) -> None:
    db.set_setting(_s("health_on", gid), "1" if on else "0")


def channel_sync(gid: int) -> int:
    v = db.get_setting(_s("health_ch", gid), "")
    return int(v) if v.isdigit() else 0


# ------------------------- state -------------------------
def ping_ms():
    lat = discord_bot.bot.latency
    if lat is None or math.isnan(lat) or math.isinf(lat):
        return None
    return round(lat * 1000)


def uptime_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m or not parts:
        parts.append(f"{m}m")
    return " ".join(parts)


def state() -> dict:
    """Current health: color (green/yellow/red), label, ping, ready, uptime."""
    ready = discord_bot.bot.is_ready() and not discord_bot.bot.is_closed()
    ms = ping_ms()
    if not ready or ms is None:
        color, label = "red", "Down"
    elif ms <= GREEN_MS:
        color, label = "green", "Online"
    elif ms <= YELLOW_MS:
        color, label = "yellow", "Slow"
    else:
        color, label = "red", "Down"
    return {"color": color, "label": label, "ping": ms, "ready": ready,
            "uptime": int(time.time() - _BOOT)}


_COLORS = {"green": 0x1F9D4D, "yellow": 0xE0A800, "red": 0xB42318}
_DOTS = {"green": "\U0001f7e2", "yellow": "\U0001f7e1", "red": "\U0001f534"}
_NOTES = {
    "green": "Gumagana lahat \u2014 OK ang prompt at ang website.",
    "yellow": "Mabagal ang server ngayon \u2014 baka maantala ang prompt o ang website.",
    "red": "Down \u2014 hindi magagamit ang prompt, at hindi available ang website.",
}


def build_embed(st: dict, *, offline: bool = False) -> discord.Embed:
    color = "red" if offline else st["color"]
    label = "Offline (restarting)" if offline else st["label"]
    now = discord.utils.utcnow()
    ts = int(now.timestamp())
    e = discord.Embed(title=f"{_DOTS[color]} Bot status: {label}", color=_COLORS[color])
    e.add_field(name="Ping", value=(f"{st['ping']} ms" if st["ping"] is not None else "\u2014"), inline=True)
    e.add_field(name="Uptime", value=uptime_text(st["uptime"]), inline=True)
    e.add_field(name="Checked", value=f"<t:{ts}:F>\n<t:{ts}:R>", inline=False)
    e.add_field(name="\u200b", value=(_NOTES["red"] if offline else _NOTES[color]), inline=False)
    e.set_footer(text="Updates every ~2 min. Kung luma na ang oras sa itaas, down ang bot.")
    e.timestamp = now
    return e


# ------------------------- posting -------------------------
async def _upsert(gid: int, ch, *, offline: bool = False) -> None:
    st = state()
    embed = build_embed(st, offline=offline)
    mid = await run(db.get_setting, _s("health_msg", gid), "")
    msg = None
    if mid.isdigit():
        try:
            msg = await ch.fetch_message(int(mid))
        except (discord.NotFound, discord.Forbidden):
            msg = None
        except discord.HTTPException:
            return
    if msg is not None:
        try:
            await msg.edit(embed=embed)
            return
        except discord.HTTPException:
            pass
    try:
        sent = await ch.send(embed=embed)
        await run(db.set_setting, _s("health_msg", gid), str(sent.id))
    except discord.HTTPException as ex:
        log.warning("Status post failed in %s: %s", gid, ex)


async def _targets():
    out = []
    for gid in await run(db.allowed_guild_ids):
        if not await run(on_sync, gid):
            continue
        cid = await run(channel_sync, gid)
        if not cid:
            continue
        guild = bot.get_guild(gid)
        ch = guild.get_channel(cid) if guild else None
        if ch is not None:
            out.append((gid, ch))
    return out


async def tick() -> None:
    for gid, ch in await _targets():
        await _upsert(gid, ch)


async def post_offline() -> None:
    """Best-effort RED post on a planned shutdown/restart, before the gateway closes."""
    try:
        for gid, ch in await _targets():
            await _upsert(gid, ch, offline=True)
    except Exception:                            # noqa: BLE001
        log.exception("post_offline failed")


# ------------------------- loop -------------------------
@tasks.loop(seconds=120, reconnect=True)
async def heartbeat():
    try:
        await tick()
    except Exception:                            # noqa: BLE001
        log.exception("Status heartbeat failed (will retry)")


@heartbeat.before_loop
async def _wait():
    await bot.wait_until_ready()


def _start():
    if not heartbeat.is_running():
        heartbeat.start()


discord_bot.EXTRA_SETUP.append(_start)


# ------------------------- on-demand commands -------------------------
async def _reply_status(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(embed=build_embed(state()), ephemeral=True)


@bot.tree.command(name="status", description="Check if the bot is up (ping, uptime, status)")
@app_commands.guild_only()
async def status_cmd(interaction: discord.Interaction):
    await _reply_status(interaction)


@bot.tree.command(name="ping", description="Show the bot's ping / latency")
@app_commands.guild_only()
async def ping_cmd(interaction: discord.Interaction):
    await _reply_status(interaction)
