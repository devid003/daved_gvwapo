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
import io
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
TICK_SECONDS = 5
DEFAULT_MINS = 360          # 6 hours, until the owner sets their own
_first_tick = True          # force one post right after the bot starts (shows it's back up)
SAMPLE_SECONDS = 120        # how often to record a ping sample for the graph
GRAPH_HOURS = 6             # how much history the graph shows
_sample_count = 0


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
    """Current health, with planned maintenance taking precedence over gateway health."""
    maintenance = db.get_setting("site_maintenance", "0") == "1"
    ready = discord_bot.bot.is_ready() and not discord_bot.bot.is_closed()
    if maintenance:
        return {"color": "maintenance", "label": "Under Maintenance", "ping": ping_ms() if ready else None,
                "ready": ready, "uptime": int(time.time() - _BOOT), "maintenance": True}
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


_COLORS = {"green": 0x1F9D4D, "yellow": 0xE0A800, "red": 0xB42318, "maintenance": 0x5865F2}
_DOTS = {"green": "\U0001f7e2", "yellow": "\U0001f7e1", "red": "\U0001f534", "maintenance": "\U0001f6e0"}
_NOTES = {
    "green": "Everything is working \u2014 prompts and the website are OK.",
    "yellow": "The server is slow right now \u2014 prompts or the website may lag.",
    "red": "Down \u2014 prompts can't be used and the website is unavailable.",
}


def build_embed(st: dict, *, offline: bool = False, footer: str | None = None) -> discord.Embed:
    color = "red" if offline else st["color"]
    label = "Offline (restarting)" if offline else st["label"]
    now = discord.utils.utcnow()
    ts = int(now.timestamp())
    e = discord.Embed(title=f"{_DOTS[color]} Bot status: {label}", color=_COLORS[color])
    e.add_field(name="Ping", value=(f"{st['ping']} ms" if st["ping"] is not None else "\u2014"), inline=True)
    e.add_field(name="Up since", value=f"<t:{int(_BOOT)}:R>", inline=True)
    e.add_field(name="Checked", value=f"<t:{ts}:F>\n<t:{ts}:R>", inline=False)
    if color == "maintenance" and not offline:
        e.add_field(name="Maintenance notice", value=("**Subject: Temporary Server Maintenance and Service Interruption**\\n\\nDear Users,\\n\\nWe would like to formally inform everyone that our server is currently undergoing scheduled maintenance to implement system improvements, perform technical updates, and ensure the overall stability, security, and reliability of our platform.\\n\\nDuring this maintenance period, certain website features and services may be temporarily unavailable or experience interruptions.\\n\\nOur technical team is working to complete the necessary procedures as efficiently as possible. Full access will be restored once all maintenance activities and system verification have been completed.\\n\\nWe sincerely apologize for any inconvenience this temporary interruption may cause. We appreciate your patience, cooperation, and continued support.\\n\\nA separate announcement will be provided once the maintenance has been completed and the server is fully operational.\\n\\nThank you for your understanding.\\n\\n**Respectfully,**\\n**David_5522**\\n\\nAttention: @Hustler"), inline=False)
    elif offline:
        e.add_field(name="Status", value=_NOTES["red"], inline=False)
    else:
        note = _NOTES.get(color)
        if note:
            e.add_field(name="Status", value=note, inline=False)
    if footer:
        e.set_footer(text=footer)
    e.timestamp = now
    return e


# ------------------------- graph -------------------------
def _chart_png(samples):
    """A compact ping-over-time line chart as PNG bytes (Discord dark theme). None if not enough data."""
    try:
        from PIL import Image, ImageDraw
    except Exception:                            # noqa: BLE001
        return None
    vals = [p for _, p in samples if p is not None]
    if len(vals) < 2:
        return None
    W, H, PAD = 640, 180, 16
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi = lo + 1
    img = Image.new("RGBA", (W, H), (43, 45, 49, 255))
    d = ImageDraw.Draw(img)
    for i in range(1, 4):
        y = PAD + (H - 2 * PAD) * i / 4
        d.line([(PAD, y), (W - PAD, y)], fill=(255, 255, 255, 22))
    n = len(vals)
    xs = [PAD + (W - 2 * PAD) * i / (n - 1) for i in range(n)]
    ys = [H - PAD - (H - 2 * PAD) * (v - lo) / (hi - lo) for v in vals]
    line = list(zip(xs, ys))
    d.polygon(line + [(xs[-1], H - PAD), (xs[0], H - PAD)], fill=(88, 101, 242, 60))
    d.line(line, fill=(88, 101, 242, 255), width=3, joint="curve")
    d.ellipse([xs[-1] - 4, ys[-1] - 4, xs[-1] + 4, ys[-1] + 4], fill=(255, 255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _graph_bytes():
    try:
        return _chart_png(db.ping_samples(hours=GRAPH_HOURS))
    except Exception:                            # noqa: BLE001
        log.exception("ping chart failed")
        return None


# ------------------------- posting -------------------------
async def _upsert(gid: int, ch, *, offline: bool = False) -> None:
    st = state()
    footer = ("Offline. Updates will resume when the bot is back."
              if offline else
              f"Refreshes every {interval_text(interval_mins(gid))}. If the time above gets old, the bot is down.")
    embed = build_embed(st, offline=offline, footer=footer)
    img = None if offline else await run(_graph_bytes)
    if img:
        embed.set_image(url="attachment://ping.png")
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
            files = [discord.File(io.BytesIO(img), "ping.png")] if img else []
            await msg.edit(embed=embed, attachments=files)
            return
        except discord.HTTPException:
            pass
    try:
        if img:
            sent = await ch.send(embed=embed, file=discord.File(io.BytesIO(img), "ping.png"))
        else:
            sent = await ch.send(embed=embed)
        await run(db.set_setting, _s("health_msg", gid), str(sent.id))
    except discord.HTTPException as ex:
        log.warning("Status post failed in %s: %s", gid, ex)


async def _targets():
    out = []
    # Maintenance status must still be published to the configured status
    # channel even if the regular health monitor has been switched off.
    maintenance = await run(db.get_setting, "site_maintenance", "0") == "1"
    for gid in await run(db.allowed_guild_ids):
        if not maintenance and not await run(on_sync, gid):
            continue
        cid = await run(channel_sync, gid)
        if not cid:
            log.warning("No status channel configured for approved guild %s", gid)
            continue
        guild = bot.get_guild(gid)
        ch = guild.get_channel(cid) if guild else None
        if ch is None:
            log.warning("Configured status channel %s is unavailable in guild %s", cid, gid)
            continue
        out.append((gid, ch))
    if not out:
        log.warning("No Discord status targets found (maintenance=%s). Check approved server and Status channel ID.", maintenance)
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
    _first_tick = False
    current = state()
    current_state = current.get("color", "red")
    targets = await _targets()
    for gid, ch in targets:
        try:
            # Force an immediate edit/post on every heartbeat so the maintenance
            # state cannot remain stale after the dashboard toggle.
            await _upsert(gid, ch)
            await run(db.set_setting, _s("health_last", gid), str(time.time()))
            await run(db.set_setting, _s("health_last_state", gid), current_state)
        except Exception:
            log.exception("Could not refresh status message for guild %s (state=%s)", gid, current_state)


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


@tasks.loop(seconds=SAMPLE_SECONDS, reconnect=True)
async def sampler():
    global _sample_count
    try:
        await run(db.add_ping_sample, ping_ms())
        _sample_count += 1
        if _sample_count % 30 == 1:              # ~hourly cleanup
            await run(db.prune_ping_samples, 48)
    except Exception:                            # noqa: BLE001
        log.exception("Ping sampler failed (will retry)")


@sampler.before_loop
async def _wait_sampler():
    await bot.wait_until_ready()


def _start():
    if not heartbeat.is_running():
        heartbeat.start()
    if not sampler.is_running():
        sampler.start()


discord_bot.EXTRA_SETUP.append(_start)


# ------------------------- on-demand command -------------------------
@bot.tree.command(name="status", description="Check if the bot is up (ping, uptime, status)")
@app_commands.guild_only()
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=build_embed(state(), footer="Checked just now."), ephemeral=True)
