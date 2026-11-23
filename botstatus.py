"""
Bot status / health monitor.

- Keeps ONE status message in a channel you pick and refreshes it on the interval YOU set (in hours or days),
  with a color:
    GREEN  = online, everything working (prompts + website OK)
    YELLOW = slow (high ping) -- prompts/website may lag
    RED    = down -- prompts can't be used and the website is unavailable
- Shows the ping (ms), uptime, and the date/time it was last checked (with the year).
- `/status` lets anyone check on demand.

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

# how often the loop wakes up to see if any server is due for a refresh
TICK_SECONDS = 60
DEFAULT_MINS = 360          # 6 hours, until the owner sets their own
_first_tick = True          # force one post right after the bot starts (shows it's back up)


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


def interval_mins(gid: int) -> int:
    v = db.get_setting(_s("health_mins", gid), str(DEFAULT_MINS))
    try:
        return max(60, int(v))
    except ValueError:
        return DEFAULT_MINS


def interval_fields(gid: int):
    """Return (count, unit) for the form, e.g. (6, 'hours') or (1, 'days')."""
    mins = interval_mins(gid)
    if mins % 1440 == 0:
        return mins // 1440, "days"
    return max(1, mins // 60), "hours"


def interval_text(mins: int) -> str:
    if mins % 1440 == 0:
        d = mins // 1440
        return f"{d} day" + ("s" if d != 1 else "")
    h = max(1, mins // 60)
    return f"{h} hour" + ("s" if h != 1 else "")


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


def build_embed(st: dict, *, offline: bool = False, footer: str | None = None) -> discord.Embed:
    color = "red" if offline else st["color"]
    label = "Offline (restarting)" if offline else st["label"]
    now = discord.utils.utcnow()
    ts = int(now.timestamp())
    e = discord.Embed(title=f"{_DOTS[color]} Bot status: {label}", color=_COLORS[color])
    e.add_field(name="Ping", value=(f"{st['ping']} ms" if st["ping"] is not None else "\u2014"), inline=True)
    e.add_field(name="Uptime", value=uptime_text(st["uptime"]), inline=True)
    e.add_field(name="Checked", value=f"<t:{ts}:F>\n<t:{ts}:R>", inline=False)
    e.add_field(name="\u200b", value=(_NOTES["red"] if offline else _NOTES[color]), inline=False)
    if footer:
        e.set_footer(text=footer)
    e.timestamp = now
    return e


# ------------------------- posting -------------------------
async def _upsert(gid: int, ch, *, offline: bool = False) -> None:
    st = state()
    footer = ("Offline. Babalik ang updates pagkabalik ng bot."
              if offline else
              f"Refreshes every {interval_text(interval_mins(gid))}. Kung luma na ang oras sa itaas, down ang bot.")
    embed = build_embed(st, offline=offline, footer=footer)
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


def _due(gid: int) -> bool:
    raw = db.get_setting(_s("health_last", gid), "")
    try:
        last = float(raw)
    except ValueError:
        last = 0.0
    return (time.time() - last) >= interval_mins(gid) * 60


async def tick() -> None:
    global _first_tick
    force = _first_tick
    _first_tick = False
    for gid, ch in await _targets():
        if force or await run(_due, gid):
            await _upsert(gid, ch)
            await run(db.set_setting, _s("health_last", gid), str(time.time()))


async def post_offline() -> None:
    """Best-effort RED post on a planned shutdown/restart, before the gateway closes."""
    try:
        for gid, ch in await _targets():
            await _upsert(gid, ch, offline=True)
    except Exception:                            # noqa: BLE001
        log.exception("post_offline failed")


# ------------------------- loop -------------------------
@tasks.loop(seconds=TICK_SECONDS, reconnect=True)
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


# ------------------------- on-demand command -------------------------
@bot.tree.command(name="status", description="Check if the bot is up (ping, uptime, status)")
@app_commands.guild_only()
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=build_embed(state(), footer="Checked just now."), ephemeral=True)
