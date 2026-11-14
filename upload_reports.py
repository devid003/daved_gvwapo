"""
YTA + FBA best-upload-time reports (US audience, plus Philippine audience for
Pinoy niches) and live USD <-> PHP rate.

Runs inside the same Discord bot as the image generator:
- every N minutes: a separate BEST TIME TO UPLOAD report per niche
- every hour: live USD <-> PHP exchange rate
- once a day: today's best upload times for all niches
- slash commands: /report, /today, /rate, /niche, /hashtags
Only approved servers get reports. Commands also go through the bot's
server + member approval check.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

import db
import discord_bot
from discord_bot import bot
from niches import YOUTUBE, FACEBOOK

log = logging.getLogger("uploads")
run = asyncio.to_thread

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")
MT = ZoneInfo("America/Denver")
PT = ZoneInfo("America/Los_Angeles")
PH = ZoneInfo("Asia/Manila")


def _env_int(name: str, default: int = 0) -> int:
    v = os.getenv(name, "").strip()
    return int(v) if v.isdigit() else default


# Optional .env fallbacks (dashboard settings win over these)
ENV_CHANNELS = {
    "default": _env_int("CHANNEL_ID"),
    "yt": _env_int("YT_CHANNEL_ID"),
    "fb": _env_int("FB_CHANNEL_ID"),
    "schedule": _env_int("SCHEDULE_CHANNEL_ID"),
    "fx": _env_int("FX_CHANNEL_ID"),
}
SCHEDULE_HOUR_ET = _env_int("SCHEDULE_HOUR_ET", 0)
DEFAULT_INTERVAL = _env_int("INTERVAL_MINUTES", 10) or 10
DEFAULT_FX_INTERVAL = _env_int("FX_INTERVAL_MINUTES", 60) or 60

ALL_NICHES = {**{f"yt:{k}": v for k, v in YOUTUBE.items()},
              **{f"fb:{k}": v for k, v in FACEBOOK.items()}}
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# Live state shown on the dashboard
STATE = {
    "interval": DEFAULT_INTERVAL, "fx_interval": DEFAULT_FX_INTERVAL, "paused": False,
    "last_fx_rate": None, "last_fx_source": "", "last_fx_at": None,
    "last_report_at": None, "last_schedule_date": None,
}


# ------------------------- time helpers -------------------------
def fmt(t: datetime, tz: ZoneInfo, with_day: bool = False) -> str:
    d = t.astimezone(tz)
    s = d.strftime("%I:%M %p").lstrip("0")
    return f"{d.strftime('%a')} {s}" if with_day else s


def us_times(t: datetime, with_day: bool = False) -> str:
    return f"{fmt(t, ET, with_day)} ET • {fmt(t, CT)} CT • {fmt(t, MT)} MT • {fmt(t, PT)} PT"


# Each niche has an audience. Its "days" windows are written in that audience's local time.
AUDIENCES = {
    "US": {"tz": ET, "flag": "🇺🇸", "label": "US audience", "abbr": "ET"},
    "PH": {"tz": PH, "flag": "🇵🇭", "label": "Philippine audience", "abbr": "PH time"},
}


def aud(niche: dict) -> dict:
    return AUDIENCES.get(niche.get("audience", "US"), AUDIENCES["US"])


def local_times(niche: dict, t: datetime, with_day: bool = False) -> str:
    """'Sat 7:00 PM ET • 6:00 PM CT • ...' for US niches, 'Sat 7:00 PM PH time' for PH niches."""
    if aud(niche)["tz"] is ET:
        return us_times(t, with_day)
    return f"{fmt(t, aud(niche)['tz'], with_day)} {aud(niche)['abbr']}"


def windows_for(niche: dict, day) -> list:
    return sorted(niche["days"][day.weekday()])


def window_dt(day, w, tz: ZoneInfo = ET):
    sh, sm, eh, em = w
    return (datetime.combine(day, time(sh, sm), tzinfo=tz),
            datetime.combine(day, time(eh, em), tzinfo=tz))


def find_window(niche: dict, now: datetime):
    """(start, end, is_live) of the current or next upload window, in the niche's audience time."""
    tz = aud(niche)["tz"]
    now = now.astimezone(tz)
    for offset in range(8):
        day = (now + timedelta(days=offset)).date()
        for w in windows_for(niche, day):
            start, end = window_dt(day, w, tz)
            if start <= now < end:
                return start, end, True
            if start > now:
                return start, end, False
    return None, None, False


def countdown(delta: timedelta) -> str:
    mins = max(0, int(delta.total_seconds() // 60))
    h, m = divmod(mins, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def _window_line(niche: dict, s: datetime, e: datetime) -> str:
    a = aud(niche)
    if a["tz"] is ET:
        return (f"**{fmt(s, ET)}–{fmt(e, ET)} ET**\n"
                f"　{fmt(s, CT)}–{fmt(e, CT)} CT • {fmt(s, MT)}–{fmt(e, MT)} MT • {fmt(s, PT)}–{fmt(e, PT)} PT")
    return f"**{fmt(s, a['tz'])}–{fmt(e, a['tz'])} {a['abbr']}**"


# ------------------------- embeds -------------------------
def niche_embed(platform: str, niche: dict, now: datetime) -> discord.Embed:
    is_yt = platform == "youtube"
    a = aud(niche)
    now = now.astimezone(a["tz"])
    start, end, live = find_window(niche, now)
    if live:
        status = (f"🟢 **UPLOAD NOW!** Best time until **{fmt(end, a['tz'])} {a['abbr']}**\n"
                  f"({countdown(end - now)} left)")
    else:
        status = f"⏳ **Next best time:** {local_times(niche, start, True)}\nStarts in **{countdown(start - now)}**"

    lines = []
    for w in windows_for(niche, now.date()):
        s, e = window_dt(now.date(), w, a["tz"])
        mark = "✅" if e <= now else ("🟢" if s <= now < e else "🕐")
        lines.append(f"{mark} {_window_line(niche, s, e)}")

    tag = "▶️ YTA" if is_yt else "📘 FBA"
    where = "US" if a["tz"] is ET else "PH"
    embed = discord.Embed(
        title=f"{tag} • {niche['emoji']} {niche['name']} — BEST TIME TO UPLOAD {a['flag']}",
        description=f"🕒 **Current {where} time:** {local_times(niche, now, True)}",
        color=(0x00C853 if live else (0xFF0000 if is_yt else 0x1877F2)),
        timestamp=now,
    )
    embed.add_field(name="⏱️ Status", value=status, inline=False)
    embed.add_field(name=f"📅 Best Time to Upload Today ({DAY_NAMES[now.weekday()]}, {where})",
                    value="\n".join(lines), inline=False)
    embed.add_field(name="🏷️ Hashtags", value=" ".join(niche["hashtags"]), inline=False)
    embed.set_footer(text=f"{'YouTube Automation' if is_yt else 'Facebook Automation'} • "
                          f"Target: {a['label']} • Report every {STATE['interval']} mins")
    return embed


def build_reports(now: datetime | None = None) -> list[tuple[str, discord.Embed]]:
    now = now or datetime.now(ET)
    out = [("yt", niche_embed("youtube", n, now)) for n in YOUTUBE.values()]
    out += [("fb", niche_embed("facebook", n, now)) for n in FACEBOOK.values()]
    return out


def schedule_rows(niches: dict, now: datetime) -> list[dict]:
    """Today's schedule as plain data (each niche in its own audience time)."""
    rows = []
    for n in niches.values():
        a = aud(n)
        local_now = now.astimezone(a["tz"])
        start, end, live = find_window(n, local_now)
        wins = [window_dt(local_now.date(), w, a["tz"]) for w in windows_for(n, local_now.date())]
        rows.append({
            "emoji": n["emoji"], "name": n["name"], "live": live, "abbr": a["abbr"], "flag": a["flag"],
            "windows": " | ".join(f"{fmt(s, a['tz'])}–{fmt(e, a['tz'])}" for s, e in wins),
            "next": (f"until {fmt(end, a['tz'])} {a['abbr']}" if live else
                     f"{fmt(start, a['tz'], True)} {a['abbr']} (in {countdown(start - local_now)})"),
        })
    return rows


def timeline_rows(niches: dict, now: datetime, display_tz: ZoneInfo = PH) -> list[dict]:
    """
    Today's windows on a 24-hour track shown in the OWNER's time (Philippine time by default).
    The windows themselves stay in each niche's audience time (US niches = US Eastern),
    they are only converted for display. A window can cross midnight, so it may be cut.
    """
    local_now = now.astimezone(display_tz)
    day_start = datetime.combine(local_now.date(), time(0, 0), tzinfo=display_tz)
    day_end = day_start + timedelta(days=1)
    day_len = (day_end - day_start).total_seconds()
    rows = []
    for n in niches.values():
        a = aud(n)
        bands = []
        first_day = day_start.astimezone(a["tz"]).date() - timedelta(days=1)
        for k in range(4):
            d = first_day + timedelta(days=k)
            for w in windows_for(n, d):
                s, e = window_dt(d, w, a["tz"])
                cs, ce = max(s, day_start), min(e, day_end)
                if cs >= ce:
                    continue
                label = f"{fmt(s, display_tz)}–{fmt(e, display_tz)} PH"
                if a["tz"] is not display_tz:
                    label += f" ({fmt(s, a['tz'])}–{fmt(e, a['tz'])} {a['abbr']})"
                bands.append({
                    "left": round((cs - day_start).total_seconds() / day_len * 100, 2),
                    "width": round((ce - cs).total_seconds() / day_len * 100, 2),
                    "label": label,
                    "state": "live" if s <= now < e else ("past" if e <= now else "next"),
                })
        bands.sort(key=lambda x: x["left"])
        start, end, live = find_window(n, now)
        if live:
            status = f"Live until {fmt(end, display_tz)} PH"
        else:
            other_day = start.astimezone(display_tz).date() != local_now.date()
            status = f"Next {fmt(start, display_tz, other_day)} PH, in {countdown(start - now)}"
        if a["tz"] is not display_tz and start:
            status += f" ({fmt(end if live else start, a['tz'])} {a['abbr']})"
        rows.append({
            "emoji": n["emoji"], "name": n["name"], "live": live, "bands": bands,
            "audience": a["label"], "local": a["tz"] is display_tz,
            "now_pct": round((local_now - day_start).total_seconds() / day_len * 100, 2),
            "status": status,
        })
    return rows


def schedule_embed(now: datetime | None = None) -> discord.Embed:
    now = (now or datetime.now(ET)).astimezone(ET)
    embed = discord.Embed(
        title=f"📅 TODAY'S BEST UPLOAD TIMES — {now.strftime('%A, %B')} {now.day}",
        color=0xFFB300, timestamp=now,
    )
    for label, niches in (("▶️ YTA", YOUTUBE), ("📘 FBA", FACEBOOK)):
        embed.add_field(name=label, inline=False, value="\n".join(
            f"{r['emoji']} **{r['name']}:** {r['windows']} {r['abbr']}" for r in schedule_rows(niches, now)))
    embed.set_footer(text="Daily schedule • US niches in ET, Pinoy niches in PH time")
    return embed


def niche_detail_embed(key: str) -> discord.Embed:
    niche = ALL_NICHES[key]
    a = aud(niche)
    now = datetime.now(a["tz"])
    start, end, live = find_window(niche, now)
    embed = discord.Embed(title=f"{niche['emoji']} {niche['name']} — Weekly Best Upload Times {a['flag']}",
                          color=0xFF0000 if key.startswith("yt:") else 0x1877F2)
    for wd in range(7):
        day = now.date() + timedelta(days=(wd - now.weekday()))
        if a["tz"] is ET:
            wins = "\n".join(f"**{fmt(s, ET)}–{fmt(e, ET)} ET** • {fmt(s, PT)}–{fmt(e, PT)} PT"
                             for s, e in (window_dt(day, w, ET) for w in windows_for(niche, day)))
        else:
            wins = "\n".join(f"**{fmt(s, a['tz'])}–{fmt(e, a['tz'])} {a['abbr']}**"
                             for s, e in (window_dt(day, w, a["tz"]) for w in windows_for(niche, day)))
        embed.add_field(name=DAY_NAMES[wd], value=wins, inline=True)
    embed.add_field(name="⏱️ Status", inline=False,
                    value="🟢 Upload now!" if live else
                    f"Next: {fmt(start, a['tz'], True)} {a['abbr']} (in {countdown(start - now)})")
    embed.add_field(name="🏷️ Hashtags", value=" ".join(niche["hashtags"]), inline=False)
    embed.set_footer(text=f"Target: {a['label']}")
    return embed


# ------------------------- USD <-> PHP -------------------------
FX_SOURCES = [
    ("Coinbase", "https://api.coinbase.com/v2/exchange-rates?currency=USD",
     lambda j: float(j["data"]["rates"]["PHP"])),
    ("ExchangeRate-API", "https://open.er-api.com/v6/latest/USD",
     lambda j: float(j["rates"]["PHP"])),
]


async def fetch_usd_php() -> tuple[float, str]:
    last_err = None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        for name, url, parse in FX_SOURCES:
            try:
                async with s.get(url) as r:
                    r.raise_for_status()
                    rate = parse(await r.json(content_type=None))
                    if rate > 0:
                        return rate, name
            except Exception as e:
                last_err = e
                log.warning("FX source %s failed: %s", name, e)
    raise RuntimeError(f"All FX sources failed: {last_err}")


def fx_embed(rate: float, source: str, prev: float | None) -> discord.Embed:
    now = datetime.now(timezone.utc)
    inv = 1 / rate
    if prev:
        diff = rate - prev
        arrow = "🟢 ▲" if diff > 0 else ("🔴 ▼" if diff < 0 else "⚪ ▬")
        change = f"{arrow} {diff:+.4f} PHP ({diff / prev * 100:+.2f}%) vs last report"
    else:
        change = "⚪ First report — change will show next time"
    embed = discord.Embed(
        title="💱 USD ⇄ PHP EXCHANGE RATE — LIVE",
        description=f"🕒 **Updated:** {fmt(now, ET, True)} ET • {fmt(now, PH, True)} PH",
        color=0x2E7D32, timestamp=now,
    )
    embed.add_field(name="🇺🇸 1 USD =", value=f"**₱{rate:,.4f}**", inline=True)
    embed.add_field(name="🇵🇭 1 PHP =", value=f"**${inv:,.6f}**", inline=True)
    embed.add_field(name="📊 Change", value=change, inline=False)
    embed.add_field(name="💵 USD → PHP", inline=True,
                    value="\n".join(f"${a:,} = ₱{a * rate:,.2f}" for a in (1, 10, 50, 100, 500, 1000)))
    embed.add_field(name="🪙 PHP → USD", inline=True,
                    value="\n".join(f"₱{a:,} = ${a * inv:,.2f}" for a in (100, 500, 1000, 5000, 10000, 50000)))
    embed.set_footer(text=f"Source: {source} • Mid-market rate • Updates every {STATE['fx_interval']} mins")
    return embed


# ------------------------- sending -------------------------
async def channel_for(guild: discord.Guild, kind: str) -> int:
    chans = await run(db.get_upload_channels, guild.id)
    cid = chans.get(kind) or chans.get("default")
    if cid:
        return int(cid)
    for env_cid in (ENV_CHANNELS.get(kind), ENV_CHANNELS["default"]):
        ch = bot.get_channel(env_cid) if env_cid else None
        if ch is not None and getattr(ch, "guild", None) and ch.guild.id == guild.id:
            return env_cid
    return 0


async def approved_guilds() -> list[discord.Guild]:
    allowed = await run(db.allowed_guild_ids)
    return [g for g in bot.guilds if g.id in allowed]


async def send_embed(guild: discord.Guild, channel_id: int, embed: discord.Embed):
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    if getattr(channel, "guild", None) is None or channel.guild.id != guild.id:
        raise PermissionError("Channel is not in this server")
    await channel.send(embed=embed)


# ------------------------- live "UPLOAD NOW" alerts -------------------------
def live_now(now: datetime | None = None) -> list[tuple]:
    """Niches whose best upload window is open right now: (kind, platform, key, niche, start, end)."""
    now = now or datetime.now(ET)
    out = []
    for kind, platform, niches in (("yt", "youtube", YOUTUBE), ("fb", "facebook", FACEBOOK)):
        for key, n in niches.items():
            start, end, live = find_window(n, now)
            if live:
                out.append((kind, platform, key, n, start, end))
    return out


def live_embed(platform: str, niche: dict, end: datetime, now: datetime) -> discord.Embed:
    is_yt = platform == "youtube"
    tag = "YTA" if is_yt else "FBA"
    e = discord.Embed(
        title=f"\U0001f7e2 UPLOAD NOW \u2014 {tag} \u2022 {niche['emoji']} {niche['name']}",
        description=(f"It's the best time to upload.\n"
                     f"Window closes in **{countdown(end - now)}** ({fmt(end, PH)} PH)."),
        color=0x00C853, timestamp=now,
    )
    e.add_field(name="\U0001f3f7\ufe0f Hashtags", value=" ".join(niche["hashtags"]), inline=False)
    e.set_footer(text=f"{'YouTube' if is_yt else 'Facebook'} Automation \u2022 {aud(niche)['label']}")
    return e


async def send_live_alerts(force: bool = False, include_schedule: bool = False) -> int:
    """Post one alert per niche when its window opens. force=True re-sends every open window."""
    now = datetime.now(ET)
    todo = []
    for kind, platform, key, n, start, end in live_now(now):
        mark = start.isoformat()
        if force or await run(db.get_setting, f"live_sent:{key}", "") != mark:
            todo.append((kind, platform, key, n, end, mark))
    guilds = await approved_guilds()
    sent = 0
    for g in guilds:
        if include_schedule and (cid := await channel_for(g, "schedule")):
            try:
                await send_embed(g, cid, schedule_embed(now))
            except Exception:
                log.exception("Schedule failed in %s", g.id)
            await asyncio.sleep(1.5)
        for kind, platform, key, n, end, mark in todo:
            cid = await channel_for(g, kind)
            if not cid:
                continue
            try:
                await send_embed(g, cid, live_embed(platform, n, end, now))
                sent += 1
            except Exception:
                log.exception("Live alert failed in %s: %s", g.id, n["name"])
            await asyncio.sleep(1.5)
    for kind, platform, key, n, end, mark in todo:
        await run(db.set_setting, f"live_sent:{key}", mark)
    if sent:
        STATE["last_report_at"] = datetime.now(timezone.utc)
        log.info("Sent %d UPLOAD NOW alert(s).", sent)
    return sent


async def send_upload_reports(include_schedule: bool = False) -> int:
    """Dashboard 'send now': post every niche that is live right now."""
    return await send_live_alerts(force=True, include_schedule=include_schedule)


async def _old_send_all_reports(include_schedule: bool = False) -> int:
    now = datetime.now(ET)
    reports = build_reports(now)
    sent = 0
    for g in await approved_guilds():
        if include_schedule and (cid := await channel_for(g, "schedule")):
            try:
                await send_embed(g, cid, schedule_embed(now))
            except Exception:
                log.exception("Schedule failed in %s", g.id)
            await asyncio.sleep(1.5)
        for kind, embed in reports:
            cid = await channel_for(g, kind)
            if not cid:
                continue
            try:
                await send_embed(g, cid, embed)
                sent += 1
            except Exception:
                log.exception("Report failed in %s: %s", g.id, embed.title)
            await asyncio.sleep(1.5)
    STATE["last_report_at"] = datetime.now(timezone.utc)
    log.info("Sent %d upload-time reports.", sent)
    return sent


async def send_fx_reports() -> float:
    rate, source = await fetch_usd_php()
    embed = fx_embed(rate, source, STATE["last_fx_rate"])
    for g in await approved_guilds():
        if cid := await channel_for(g, "fx"):
            try:
                await send_embed(g, cid, embed)
            except Exception:
                log.exception("FX failed in %s", g.id)
            await asyncio.sleep(1.5)
    STATE.update(last_fx_rate=rate, last_fx_source=source, last_fx_at=datetime.now(timezone.utc))
    log.info("FX sent: 1 USD = %.4f PHP (%s)", rate, source)
    return rate


# ------------------------- loops -------------------------
@tasks.loop(minutes=DEFAULT_INTERVAL, reconnect=True)
async def report_loop():
    if STATE["paused"]:
        return
    now = datetime.now(ET)
    daily = now.hour == SCHEDULE_HOUR_ET and STATE["last_schedule_date"] != now.date()
    if daily:
        STATE["last_schedule_date"] = now.date()
    try:
        await send_live_alerts(include_schedule=daily)
    except Exception:
        log.exception("Report cycle failed (retry next cycle)")


@tasks.loop(minutes=DEFAULT_FX_INTERVAL, reconnect=True)
async def fx_loop():
    if STATE["paused"]:
        return
    try:
        await send_fx_reports()
    except Exception:
        log.exception("FX cycle failed (retry next cycle)")


@report_loop.before_loop
async def _wait_reports():
    await bot.wait_until_ready()


@fx_loop.before_loop
async def _wait_fx():
    await bot.wait_until_ready()


def apply_settings(interval: int, fx_interval: int, paused: bool):
    STATE.update(interval=interval, fx_interval=fx_interval, paused=paused)
    report_loop.change_interval(minutes=1)   # check every minute for windows that just opened
    fx_loop.change_interval(minutes=fx_interval)


def load_settings_sync():
    """Read saved settings from the database (called at start-up)."""
    interval = int(db.get_setting("upload_interval", str(DEFAULT_INTERVAL)) or DEFAULT_INTERVAL)
    fx_interval = int(db.get_setting("fx_interval", str(DEFAULT_FX_INTERVAL)) or DEFAULT_FX_INTERVAL)
    paused = db.get_setting("upload_paused", "0") == "1"
    apply_settings(interval, fx_interval, paused)


async def save_settings(interval: int, fx_interval: int, paused: bool):
    interval, fx_interval = max(1, min(interval, 1440)), max(5, min(fx_interval, 1440))
    await run(db.set_setting, "upload_interval", interval)
    await run(db.set_setting, "fx_interval", fx_interval)
    await run(db.set_setting, "upload_paused", "1" if paused else "0")
    apply_settings(interval, fx_interval, paused)


def _start_loops():
    if not report_loop.is_running():
        report_loop.start()
    if not fx_loop.is_running():
        fx_loop.start()


discord_bot.EXTRA_SETUP.append(_start_loops)


# ------------------------- slash commands -------------------------
NICHE_CHOICES = [app_commands.Choice(name=f"{'YT' if k.startswith('yt') else 'FB'} • {v['name']}", value=k)
                 for k, v in ALL_NICHES.items()]


@bot.tree.command(name="report", description="Show all best-time-to-upload reports now")
@app_commands.guild_only()
async def report_cmd(interaction: discord.Interaction):
    reports = build_reports()
    await interaction.response.send_message(embed=reports[0][1])
    for _, embed in reports[1:]:
        await interaction.followup.send(embed=embed)
        await asyncio.sleep(1)


@bot.tree.command(name="today", description="Today's best upload times for all niches")
@app_commands.guild_only()
async def today_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(embed=schedule_embed())


@bot.tree.command(name="rate", description="Live USD ⇄ PHP exchange rate")
@app_commands.guild_only()
async def rate_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    try:
        rate, source = await fetch_usd_php()
        await interaction.followup.send(embed=fx_embed(rate, source, STATE["last_fx_rate"]))
    except Exception:
        await interaction.followup.send("⚠️ Exchange rate service is unavailable right now. Try again in a minute.")


@bot.tree.command(name="niche", description="Weekly best upload times and hashtags for one niche")
@app_commands.guild_only()
@app_commands.choices(niche=NICHE_CHOICES)
async def niche_cmd(interaction: discord.Interaction, niche: app_commands.Choice[str]):
    await interaction.response.send_message(embed=niche_detail_embed(niche.value))


@bot.tree.command(name="hashtags", description="Copy-paste ready hashtags for one niche")
@app_commands.guild_only()
@app_commands.choices(niche=NICHE_CHOICES)
async def hashtags_cmd(interaction: discord.Interaction, niche: app_commands.Choice[str]):
    tags = " ".join(ALL_NICHES[niche.value]["hashtags"])
    await interaction.response.send_message(f"**{niche.name}**\n```{tags}```", ephemeral=True)
