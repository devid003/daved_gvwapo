"""Admin dashboard + Discord bot in ONE process (so one Render service runs both)."""
import asyncio
import csv
import datetime as dt
import hmac
import io
import logging
import os
import json
from html import escape as html_escape
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.templating import Jinja2Templates
from starlette.staticfiles import StaticFiles

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("panel")

import db  # noqa: E402
import discord_bot  # noqa: E402
import upload_reports  # noqa: E402  (adds upload-time reports + commands to the bot)
import security  # noqa: E402  (join gate: new members need approval)
import prompt_commands  # noqa: E402,F401  (adds /ghibli, /cooking, ... commands)
import prompts as promptlib  # noqa: E402
import daily_word  # noqa: E402  (motivation + Bible verse at 6 AM, 3 PM, 10 PM)

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    raise SystemExit("Set ADMIN_PASSWORD in your .env file (this is the dashboard login password).")
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)
if not os.getenv("SECRET_KEY"):
    log.warning("SECRET_KEY not set: dashboard logins will reset every restart.")
if os.getenv("RENDER") and db.IS_SQLITE:
    log.warning("DATABASE_URL is not set: data will be LOST on every restart/deploy on Render!")

try:
    TZ = ZoneInfo(os.getenv("DISPLAY_TZ", "Asia/Manila"))
except Exception:
    TZ = dt.timezone.utc

run = asyncio.to_thread


def shell_context(request) -> dict:
    """Bot status shown in the sidebar of every page."""
    b = discord_bot.bot
    ready = b.is_ready()
    return {
        "nav_bot_online": ready,
        "nav_bot_name": str(b.user) if b.user else "Not connected",
        "nav_bot_ping": round(b.latency * 1000) if ready and b.latency == b.latency else None,
        "nav_path": request.url.path,
    }


templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"),
                            context_processors=[shell_context])


def local(v):
    if v is None:
        return ""
    return v.replace(tzinfo=dt.timezone.utc).astimezone(TZ).strftime("%Y-%m-%d %H:%M:%S")


def secs(ms):
    return "" if ms is None else f"{float(ms) / 1000:.1f}s"


templates.env.filters["local"] = local
templates.env.filters["secs"] = secs
TZ_LABEL = os.getenv("DISPLAY_TZ", "Asia/Manila")


# ---------- helpers ----------
def authed(request) -> bool:
    return request.session.get("auth") is True


def csrf_token(request) -> str:
    if "csrf" not in request.session:
        request.session["csrf"] = secrets.token_urlsafe(16)
    return request.session["csrf"]


def redirect(url: str):
    return RedirectResponse(url, status_code=303)


def flash(request, msg: str):
    request.session["flash"] = msg


async def guarded_post(request):
    """Returns (form, None) if allowed, else (None, response)."""
    if not authed(request):
        return None, redirect("/login")
    form = await request.form()
    if not hmac.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "")):
        return None, PlainTextResponse("Bad CSRF token. Go back and reload the page.", status_code=400)
    return form, None


async def public_post(request):
    """For member-facing forms: CSRF check only (no admin login needed)."""
    form = await request.form()
    if not hmac.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "")):
        return None, PlainTextResponse("Bad CSRF token. Go back and reload the page.", status_code=400)
    return form, None


_MEMBER_TRIES: dict[str, list] = {}


def _too_many(key: str, limit: int = 10, window: int = 300) -> bool:
    """True when this key (IP) made too many attempts in the last few minutes."""
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    hits = [t for t in _MEMBER_TRIES.get(key, []) if now - t < window]
    hits.append(now)
    _MEMBER_TRIES[key] = hits
    return len(hits) > limit


def _client_ip(request) -> str:
    return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "")


def parse_int(value) -> int | None:
    try:
        v = int(str(value))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def parse_id(value) -> int | None:
    s = str(value or "").strip()
    return int(s) if s.isdigit() and 15 <= len(s) <= 20 else None


PERIODS = [("24h", "Last 24h"), ("7d", "7 days"), ("30d", "30 days"), ("all", "All time")]
PERIOD_DELTA = {"24h": dt.timedelta(hours=24), "7d": dt.timedelta(days=7), "30d": dt.timedelta(days=30), "all": None}


def since_for(period: str):
    delta = PERIOD_DELTA.get(period, PERIOD_DELTA["7d"])
    return db.utcnow() - delta if delta else None


def csv_safe(v) -> str:
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


# ---------- auth ----------
FAILS: list[float] = []


async def login(request):
    error = None
    status = 200
    if request.method == "POST":
        form = await request.form()
        now = time.time()
        FAILS[:] = [t for t in FAILS if now - t < 300]
        if len(FAILS) >= 10:
            error, status = "Too many attempts. Wait 5 minutes.", 429
        elif hmac.compare_digest(str(form.get("password", "")).encode(), ADMIN_PASSWORD.encode()):
            request.session.clear()
            request.session["auth"] = True
            csrf_token(request)
            return redirect("/")
        else:
            FAILS.append(now)
            error, status = "Wrong password.", 401
    return templates.TemplateResponse(request=request, name="login.html", context={"error": error}, status_code=status)


async def logout(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    request.session.clear()
    return redirect("/login")


# ---------- pages ----------
async def index(request):
    if not authed(request):
        return redirect("/login")
    data = await run(db.overview)
    b = discord_bot.bot
    ready = b.is_ready()
    bot_info = {
        "online": ready,
        "name": str(b.user) if b.user else "not connected",
        "guilds": len(b.guilds) if ready else 0,
        "ping": round(b.latency * 1000) if ready and b.latency == b.latency else None,
    }
    members = data["members"]
    ctx = {
        "csrf": csrf_token(request),
        "flash": request.session.pop("flash", None),
        "bot": bot_info,
        "servers": data["servers"],
        "members": members,
        "pending": [m for m in members if m.status == "pending"],
        "approved_count": sum(1 for m in members if m.status == "approved"),
        "gen24": data["gen24"],
        "jobs": data["jobs"],
        "tz": TZ_LABEL,
        "regs_total": data.get("regs_total", 0),
        "regs_pending": data.get("regs_pending", 0),
        "prompts_total": await run(db.prompt_use_total),
    }
    return templates.TemplateResponse(request=request, name="index.html", context=ctx)


async def usage(request):
    if not authed(request):
        return redirect("/login")
    period = request.query_params.get("period", "7d")
    if period not in PERIOD_DELTA:
        period = "7d"
    user = parse_id(request.query_params.get("user"))
    since = since_for(period)
    summary = await run(db.usage_summary, since)
    logs = await run(db.recent_logs, since, user, 200)
    ctx = {
        "csrf": csrf_token(request), "periods": PERIODS, "period": period, "user": user,
        "summary": summary, "logs": logs, "tz": TZ_LABEL,
    }
    return templates.TemplateResponse(request=request, name="usage.html", context=ctx)


async def usage_csv(request):
    if not authed(request):
        return redirect("/login")
    period = request.query_params.get("period", "7d")
    user = parse_id(request.query_params.get("user"))
    logs = await run(db.recent_logs, since_for(period), user, 5000)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "user_id", "username", "server", "command", "status", "duration_s", "prompt", "error"])
    for r in logs:
        w.writerow([
            local(r.ts), r.user_id, csv_safe(r.username), csv_safe(r.guild_name), r.command, r.status,
            f"{r.duration_ms / 1000:.1f}", csv_safe(r.prompt), csv_safe(r.error),
        ])
    return Response(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="usage-log.csv"'},
    )


# ---------- actions ----------
async def servers_add(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    gid = parse_id(form.get("guild_id"))
    if gid is None:
        flash(request, "That doesn't look like a server ID (it's a long number, 17 to 20 digits).")
    else:
        await run(db.add_server, gid, str(form.get("name", "")).strip()[:200])
        flash(request, f"Server {gid} approved. Invite the bot to it (or, if it's already there, wait ~30 seconds).")
    return redirect("/")


async def servers_remove(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    gid = parse_id(form.get("guild_id"))
    if gid:
        await run(db.remove_server, gid)
        flash(request, f"Server {gid} removed. The bot will leave it within ~30 seconds.")
    return redirect("/")


async def members_set(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    uid = parse_id(form.get("user_id"))
    status = str(form.get("status", ""))
    if uid is None or status not in ("approved", "denied", "pending", "remove"):
        flash(request, "That doesn't look like a valid user ID.")
    else:
        await run(db.set_member, uid, status, str(form.get("username", "")).strip()[:200] or None)
        flash(request, f"User {uid}: {status}.")
    return redirect("/")


async def image_toggle(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    enabled = str(form.get("enabled", "")) == "1"
    discord_bot.IMAGE_STATE["enabled"] = enabled
    await run(db.set_setting, "image_enabled", "1" if enabled else "0")
    flash(request, "🟢 Image generation turned ON." if enabled else "🔴 Image generation turned OFF.")
    return redirect("/")


# ---------- upload-time reports ----------
UPLOAD_KINDS = [("default", "Default channel"), ("yt", "YTA reports"), ("fb", "FBA reports"),
                ("schedule", "Daily schedule"), ("fx", "USD ⇄ PHP rate")]


def _utc_local(v):
    return local(v.astimezone(dt.timezone.utc).replace(tzinfo=None)) if v else "—"


async def uploads(request):
    if not authed(request):
        return redirect("/login")
    b = discord_bot.bot
    data = await run(db.overview)
    servers = []
    for s in data["servers"]:
        guild = b.get_guild(s.guild_id) if b.is_ready() else None
        chans = await run(db.get_upload_channels, s.guild_id)
        options = []
        if guild:
            me = guild.me
            options = [(c.id, c.name) for c in guild.text_channels
                       if me is None or c.permissions_for(me).send_messages]
        servers.append({"id": s.guild_id, "name": (guild.name if guild else s.name) or "—",
                        "joined": guild is not None, "chans": chans, "options": options})
    now = upload_reports.datetime.now(upload_reports.ET)
    ph_now = now.astimezone(upload_reports.PH)
    st = upload_reports.STATE
    ctx = {
        "csrf": csrf_token(request),
        "flash": request.session.pop("flash", None),
        "online": b.is_ready(),
        "state": st,
        "fx_rate": f"₱{st['last_fx_rate']:,.4f}" if st["last_fx_rate"] else "—",
        "fx_at": _utc_local(st["last_fx_at"]),
        "report_at": _utc_local(st["last_report_at"]),
        "servers": servers,
        "kinds": UPLOAD_KINDS,
        "ph_now": upload_reports.fmt(ph_now, upload_reports.PH, True),
        "us_now": f"{upload_reports.fmt(now, upload_reports.ET, True)} ET",
        "today": ph_now.strftime("%A, %B ") + str(ph_now.day),
        "timeline": [("YouTube automation", "yt", upload_reports.timeline_rows(upload_reports.YOUTUBE, now)),
                     ("Facebook automation", "fb", upload_reports.timeline_rows(upload_reports.FACEBOOK, now))],
        "now_pct": round((ph_now.hour * 60 + ph_now.minute) / 1440 * 100, 2),
        "now_label": upload_reports.fmt(ph_now, upload_reports.PH),
        "tz": TZ_LABEL,
    }
    return templates.TemplateResponse(request=request, name="uploads.html", context=ctx)


async def uploads_channels(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    gid = parse_id(form.get("guild_id"))
    if gid is None or gid not in await run(db.allowed_guild_ids):
        flash(request, "That server is not approved.")
        return redirect("/uploads")
    chans = {}
    for k, _ in UPLOAD_KINDS:
        v = str(form.get(k, "")).strip()
        chans[k] = int(v) if v.isdigit() else None
    await run(db.set_upload_channels, gid, chans)
    flash(request, "💾 Channels saved.")
    return redirect("/uploads")


async def uploads_settings(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    st = upload_reports.STATE
    action = str(form.get("action", "save"))
    try:
        interval = int(form.get("interval", st["interval"]))
        fx_interval = int(form.get("fx_interval", st["fx_interval"]))
    except ValueError:
        flash(request, "Please enter whole numbers for the intervals.")
        return redirect("/uploads")
    paused = {"pause": True, "resume": False}.get(action, st["paused"])
    await upload_reports.save_settings(interval, fx_interval, paused)
    flash(request, {"pause": "⏸️ Reports paused.", "resume": "▶️ Reports resumed."}.get(action, "💾 Settings saved."))
    return redirect("/uploads")


async def uploads_send(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    if not discord_bot.bot.is_ready():
        flash(request, "The bot is not connected to Discord yet, so nothing can be sent.")
        return redirect("/uploads")
    what = str(form.get("what", ""))
    if what == "fx":
        asyncio.create_task(upload_reports.send_fx_reports())
        flash(request, "📤 Sending the exchange rate now…")
    else:
        live_n = len(upload_reports.live_now())
        asyncio.create_task(upload_reports.send_upload_reports(include_schedule=(what == "all")))
        if not live_n and what != "all":
            flash(request, "No niche is in its best time right now, so nothing was sent.")
            return redirect("/uploads")
        flash(request, "📤 Sending upload-time reports now…")
    return redirect("/uploads")


async def uploads_live(request):
    """Fresh timeline + status for the Upload times page (polled every 30 s)."""
    if not authed(request):
        return JSONResponse({"error": "login"}, status_code=401)
    now = upload_reports.datetime.now(upload_reports.ET)
    ph_now = now.astimezone(upload_reports.PH)
    st = upload_reports.STATE
    html = templates.env.get_template("_timeline.html").render(
        timeline=[("YouTube automation", "yt", upload_reports.timeline_rows(upload_reports.YOUTUBE, now)),
                  ("Facebook automation", "fb", upload_reports.timeline_rows(upload_reports.FACEBOOK, now))],
        now_pct=round((ph_now.hour * 60 + ph_now.minute) / 1440 * 100, 2),
        now_label=upload_reports.fmt(ph_now, upload_reports.PH),
    )
    return JSONResponse({
        "timeline_html": html,
        "today": ph_now.strftime("%A, %B ") + str(ph_now.day),
        "online": discord_bot.bot.is_ready(),
        "paused": st["paused"],
        "report_at": _utc_local(st["last_report_at"]),
        "fx_rate": f"\u20b1{st['last_fx_rate']:,.4f}" if st["last_fx_rate"] else "",
        "fx_source": st["last_fx_source"],
        "fx_at": _utc_local(st["last_fx_at"]),
        "fx_interval": st["fx_interval"],
    }, headers={"Cache-Control": "no-store"})


# ---------- security (join gate) ----------
async def security_page(request):
    if not authed(request):
        return redirect("/login")
    b = discord_bot.bot
    data = await run(db.overview)
    servers = []
    for s in data["servers"]:
        guild = b.get_guild(s.guild_id) if b.is_ready() else None
        role = await security.find_role(guild) if guild else None
        me = guild.me if guild else None
        servers.append({
            "id": s.guild_id,
            "name": (guild.name if guild else s.name) or "Unnamed server",
            "joined": guild is not None,
            "gate": await run(security.gate_on_sync, s.guild_id),
            "log": await run(security.log_channel_sync, s.guild_id),
            "role": role.name if role else None,
            "role_ok": bool(role and me and me.top_role > role),
            "phone": security.phone_required(guild) if guild else False,
            "options": [(c.id, c.name) for c in guild.text_channels] if guild else [],
        })
    pending = await run(db.list_joins, None, True, 200)
    history = [j for j in await run(db.list_joins, None, False, 80) if j.status != "pending"][:50]
    ctx = {
        "csrf": csrf_token(request),
        "flash": request.session.pop("flash", None),
        "servers": servers,
        "names": {s["id"]: s["name"] for s in servers},
        "pending": pending,
        "history": history,
        "intent_ok": security.members_intent_ok(),
        "online": b.is_ready(),
        "tz": TZ_LABEL,
    }
    return templates.TemplateResponse(request=request, name="security.html", context=ctx)


async def _approved_guild(form):
    gid = parse_id(form.get("guild_id"))
    if gid is None or gid not in await run(db.allowed_guild_ids):
        return None, None
    return gid, discord_bot.bot.get_guild(gid)


async def security_gate(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    gid, guild = await _approved_guild(form)
    if gid is None:
        flash(request, "That server is not approved.")
        return redirect("/security")
    if str(form.get("on", "")) == "1":
        if guild is None:
            flash(request, "The bot must be online and inside the server to turn the gate on.")
            return redirect("/security")
        try:
            flash(request, await security.enable_gate(guild))
        except Exception as e:  # noqa: BLE001
            log.exception("enable_gate failed")
            flash(request, f"Could not turn on the gate: {type(e).__name__}. Check the bot's permissions.")
    else:
        await security.disable_gate(gid)
        flash(request, "Join gate is OFF. New members can see the server right away.")
    return redirect("/security")


async def security_log(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    gid, _ = await _approved_guild(form)
    if gid is None:
        flash(request, "That server is not approved.")
        return redirect("/security")
    ch = str(form.get("channel", "")).strip()
    await run(db.set_setting, f"gate_log:{gid}", ch if ch.isdigit() else "")
    flash(request, "Notification channel saved.")
    return redirect("/security")


async def security_decide(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    raw = str(form.get("id", "")).strip()
    jid = int(raw) if raw.isdigit() else None
    action = str(form.get("action", ""))
    if jid is None or action not in ("deny", "ban"):   # approving needs the member's code
        flash(request, "Invalid request.")
        return redirect("/security")
    if not discord_bot.bot.is_ready():
        flash(request, "The bot is not connected to Discord yet. Try again in a moment.")
        return redirect("/security")
    flash(request, await security.decide(jid, action))
    return redirect("/security")


# ---------- security v2: codes + live ----------
def _join_stats(days: int = 14) -> dict:
    """Numbers and a day-by-day chart for the Security insights panel (local dates)."""
    now_local = dt.datetime.now(TZ)
    today = now_local.date()
    first = today - dt.timedelta(days=days - 1)
    prev_first = first - dt.timedelta(days=days)
    rows = db.list_joins(None, False, 5000)

    def day_of(t):
        return t.replace(tzinfo=dt.timezone.utc).astimezone(TZ).date() if t else None

    buckets = {first + dt.timedelta(days=i): {"approved": 0, "rejected": 0, "pending": 0, "left": 0, "open": 0}
               for i in range(days)}
    cur = {"joins": 0, "codes": 0, "approved": 0, "rejected": 0}
    prev = {"joins": 0, "codes": 0, "approved": 0, "rejected": 0}
    waits = []
    for j in rows:
        d = day_of(j.joined_at)
        if d is None or d < prev_first:
            continue
        side = cur if d >= first else prev
        side["joins"] += 1
        if j.status != "allowed":
            side["codes"] += 1
        if j.status == "approved":
            side["approved"] += 1
        if j.status in ("denied", "banned"):
            side["rejected"] += 1
        if d >= first:
            key = {"approved": "approved", "denied": "rejected", "banned": "rejected",
                   "pending": "pending", "left": "left"}.get(j.status, "open")
            buckets[d][key] += 1
            if j.status == "approved" and j.decided_at and j.joined_at:
                waits.append((j.decided_at - j.joined_at).total_seconds() / 60)

    def delta(k):
        a, b = cur[k], prev[k]
        if b == 0:
            return "new" if a else ""
        pct = round((a - b) / b * 100)
        return f"{'+' if pct >= 0 else ''}{pct}%"

    decided = cur["approved"] + cur["rejected"]
    waits.sort()
    med = waits[len(waits) // 2] if waits else None
    if med is None:
        med_txt = "\u2014"
    elif med < 60:
        med_txt = f"{max(1, round(med))}m"
    else:
        med_txt = f"{med / 60:.1f}h"

    top = max([sum(v.values()) for v in buckets.values()] + [1])
    step = 1 if top <= 5 else (2 if top <= 10 else (5 if top <= 25 else 10))
    ymax = ((top + step - 1) // step) * step
    chart, x = [], 0
    for d, v in buckets.items():
        y = 0
        segs = []
        for k in ("approved", "rejected", "pending", "left", "open"):
            if v[k]:
                segs.append({"k": k, "y": y, "h": v[k]})
                y += v[k]
        chart.append({"label": d.strftime("%b %d"), "short": d.strftime("%d"), "total": y,
                      "segs": segs, "x": x, "today": d == today, "v": v})
        x += 1
    return {
        "days": days, "cur": cur, "pending_now": sum(1 for j in rows if j.status == "pending"),
        "rate": f"{round(cur['approved'] / decided * 100)}%" if decided else "\u2014",
        "median": med_txt, "d_joins": delta("joins"), "d_codes": delta("codes"),
        "d_approved": delta("approved"), "d_rejected": delta("rejected"),
        "chart": chart, "ymax": ymax, "ticks": list(range(0, ymax + 1, step)),
        "totals": {k: sum(v[k] for v in buckets.values()) for k in ("approved", "rejected", "pending", "left", "open")},
    }


async def _security_lists(request) -> dict:
    b = discord_bot.bot
    data = await run(db.overview)
    names = {}
    for s in data["servers"]:
        g = b.get_guild(s.guild_id) if b.is_ready() else None
        names[s.guild_id] = (g.name if g else s.name) or "Unnamed server"
    pending = await run(db.list_joins, None, True, 200)
    history = [j for j in await run(db.list_joins, None, False, 80) if j.status != "pending"][:50]
    codes = {j.id: await run(security.panel_status_sync, j.id) for j in pending}
    stats = await run(_join_stats)
    return {"csrf": csrf_token(request), "pending": pending, "history": history, "codes": codes,
            "names": names, "multi": len(names) > 1, "tz": TZ_LABEL, "stats": stats}


async def security_page_v2(request):
    if not authed(request):
        return redirect("/login")
    b = discord_bot.bot
    data = await run(db.overview)
    servers = []
    for s in data["servers"]:
        guild = b.get_guild(s.guild_id) if b.is_ready() else None
        role = await security.find_role(guild) if guild else None
        me = guild.me if guild else None
        servers.append({
            "id": s.guild_id,
            "name": (guild.name if guild else s.name) or "Unnamed server",
            "joined": guild is not None,
            "gate": await run(security.gate_on_sync, s.guild_id),
            "log": await run(security.log_channel_sync, s.guild_id),
            "role": role.name if role else None,
            "role_ok": bool(role and me and me.top_role > role),
            "phone": security.phone_required(guild) if guild else False,
            "options": [(c.id, c.name) for c in guild.text_channels] if guild else [],
        })
    ctx = await _security_lists(request)
    ctx.update({
        "flash": request.session.pop("flash", None),
        "servers": servers,
        "intent_ok": security.members_intent_ok(),
        "online": b.is_ready(),
    })
    return templates.TemplateResponse(request=request, name="security.html", context=ctx)


async def security_live(request):
    if not authed(request):
        return JSONResponse({"error": "login"}, status_code=401)
    ctx = await _security_lists(request)
    env = templates.env
    return JSONResponse({
        "pending_html": env.get_template("_sec_pending.html").render(**ctx),
        "history_html": env.get_template("_sec_history.html").render(**ctx),
        "pending_count": len(ctx["pending"]),
        "stats_html": env.get_template("_sec_stats.html").render(**ctx),
    }, headers={"Cache-Control": "no-store"})


async def security_verify(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    if not discord_bot.bot.is_ready():
        flash(request, "The bot is not connected to Discord yet. Try again in a moment.")
        return redirect("/security")
    flash(request, await security.verify_code(str(form.get("code", ""))))
    return redirect("/security")


async def security_resend(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    raw = str(form.get("id", "")).strip()
    if not raw.isdigit():
        flash(request, "Invalid request.")
        return redirect("/security")
    if not discord_bot.bot.is_ready():
        flash(request, "The bot is not connected to Discord yet. Try again in a moment.")
        return redirect("/security")
    flash(request, await security.resend_code(int(raw)))
    return redirect("/security")


# ---------- hide the admin panel behind a secret path ----------
ADMIN_PATH = os.getenv("ADMIN_PATH", "").strip().strip("/")
ADMIN_HOSTS = {h.strip().lower() for h in os.getenv("ADMIN_HOSTS", "").split(",") if h.strip()}
SITE_NAME = os.getenv("SITE_NAME", "Syndicate")
_PUBLIC = ("/v/", "/health", "/api/public/", "/static/", "/manifest", "/join", "/members/", "/watch")
_PLAIN_CSS = ("body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0f131a;color:#e6e9ef;"
              "font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}"
              ".c{text-align:center;padding:24px;max-width:420px}h1{font-size:28px;margin:0 0 8px}p{color:#9aa3b2;margin:0}")


def _plain_page(title: str, text: str) -> bytes:
    t = html_escape(title)
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width, initial-scale=1'><meta name='robots' content='noindex'>"
            f"<title>{t}</title><style>{_PLAIN_CSS}</style></head><body><div class='c'><h1>{t}</h1>"
            f"<p>{html_escape(text)}</p></div></body></html>").encode()


class AdminPath:
    """With ADMIN_PATH set, the panel only answers at /<ADMIN_PATH>/...; the site root is a plain page."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not ADMIN_PATH:
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path.startswith(_PUBLIC):
            return await self.app(scope, receive, send)
        pre = "/" + ADMIN_PATH
        host = dict(scope.get("headers") or []).get(b"host", b"").decode().split(":")[0].lower()
        if (path == pre or path.startswith(pre + "/")) and (not ADMIN_HOSTS or host in ADMIN_HOSTS):
            inner = path[len(pre):] or "/"
            scope = dict(scope, path=inner, raw_path=inner.encode())
            return await self.app(scope, receive, self._rewriter(send, pre.encode()))
        if path == "/":
            html = await _render_landing()
            return await self._send(send, 200, html.encode())
        return await self._send(send, 404, _plain_page("Not found", "This page doesn't exist."))

    @staticmethod
    async def _send(send, status, body):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"text/html; charset=utf-8"),
                                (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _rewriter(send, pre: bytes):
        state = {"start": None, "chunks": [], "rewrite": False}
        swaps = [(b'href="/', b'href="' + pre + b"/"), (b'action="/', b'action="' + pre + b"/"),
                 (b"fetch('/", b"fetch('" + pre + b"/"), (b"location.href = '/", b"location.href = '" + pre + b"/"),
                 (b'href=\\"/', b'href=\\"' + pre + b"/"), (b'action=\\"/', b'action=\\"' + pre + b"/")]

        def fix_headers(headers):
            out = []
            for k, v in headers:
                if k.lower() == b"location" and v.startswith(b"/") and not v.startswith(pre + b"/") and v != pre:
                    v = pre + v
                out.append((k, v))
            return out

        async def wrapped(message):
            if message["type"] == "http.response.start":
                headers = fix_headers(list(message.get("headers") or []))
                ctype = dict((k.lower(), v) for k, v in headers).get(b"content-type", b"")
                state["rewrite"] = ctype.startswith(b"text/html") or ctype.startswith(b"application/json")
                message = dict(message, headers=headers)
                if not state["rewrite"]:
                    return await send(message)
                state["start"] = message
                return
            if message["type"] == "http.response.body" and state["rewrite"]:
                state["chunks"].append(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = b"".join(state["chunks"])
                for old, new in swaps:
                    body = body.replace(old, new)
                start = state["start"]
                headers = [(k, v) for k, v in start["headers"] if k.lower() != b"content-length"]
                headers.append((b"content-length", str(len(body)).encode()))
                await send(dict(start, headers=headers))
                return await send({"type": "http.response.body", "body": body})
            await send(message)

        return wrapped


class SecurityHeaders:
    """Browser security headers on every response: no framing, no sniffing, strict CSP."""
    CSP = ("default-src 'self'; img-src 'self' data: https://cdn.discordapp.com; "
           "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
           "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src https://drive.google.com; frame-ancestors 'none'; "
           "base-uri 'none'; form-action 'self'; object-src 'none'")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                extra = {
                    b"x-frame-options": b"DENY",
                    b"x-content-type-options": b"nosniff",
                    b"referrer-policy": b"no-referrer",
                    b"permissions-policy": b"camera=(), microphone=(), geolocation=()",
                    b"content-security-policy": self.CSP.encode(),
                }
                if os.getenv("RENDER"):
                    extra[b"strict-transport-security"] = b"max-age=31536000"
                have = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in extra.items() if k not in have]
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ---------- device verification page (public link from the DM) ----------
async def verify_page(request):
    token = request.path_params.get("token", "")
    req = await run(security.join_for_token_sync, token)
    guild = discord_bot.bot.get_guild(req.guild_id) if req else None
    resp = templates.TemplateResponse(request=request, name="verify.html", context={
        "valid": req is not None, "server": guild.name if guild else "the server",
    })
    if not request.cookies.get("dbv"):
        resp.set_cookie("dbv", secrets.token_urlsafe(24), max_age=60 * 60 * 24 * 730,
                        httponly=True, secure=bool(os.getenv("RENDER")), samesite="lax", path="/v/")
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def verify_submit(request):
    body = await request.body()
    if len(body) > 8000:
        return JSONResponse({"ok": False, "msg": "Request too large."}, status_code=413)
    try:
        fp = json.loads(body or b"{}")
        if not isinstance(fp, dict):
            raise ValueError
    except ValueError:
        return JSONResponse({"ok": False, "msg": "Bad request."}, status_code=400)
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "")
    result = await security.check_device(request.path_params.get("token", ""), request.cookies.get("dbv", ""),
                                         fp, ip, request.headers.get("user-agent", "")[:400])
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


async def security_phone(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    gid, guild = await _approved_guild(form)
    if gid is None or guild is None:
        flash(request, "The bot must be online and inside that approved server.")
        return redirect("/security")
    flash(request, await security.set_phone_required(guild, str(form.get("on", "")) == "1"))
    return redirect("/security")


# ---------- members directory + history ----------
def _one_guild_id():
    ids = sorted(db.allowed_guild_ids())
    return ids[0] if ids else 0


async def members_page(request):
    if not authed(request):
        return redirect("/login")
    gid = parse_id(request.query_params.get("g")) or _one_guild_id()
    q = request.query_params.get("q", "")
    rows = await run(db.find_members, gid, q) if gid else []
    today = dt.datetime.now(dt.timezone.utc)
    people = []
    for m in rows:
        joins = await run(db.member_joins, gid, m["user_id"])
        first = min((j.joined_at for j in joins), default=m["joined_at"])
        days = (today - first.replace(tzinfo=dt.timezone.utc)).days if first else 0
        people.append({**m, "days": days, "warns": await run(db.warning_count, gid, m["user_id"])})
    guild = discord_bot.bot.get_guild(gid) if discord_bot.bot.is_ready() else None
    return templates.TemplateResponse(request=request, name="members.html", context={
        "csrf": csrf_token(request), "flash": request.session.pop("flash", None),
        "people": people, "q": q, "gid": gid, "tz": TZ_LABEL,
        "server": (guild.name if guild else "") or "your server",
    })


async def member_history(request):
    if not authed(request):
        return redirect("/login")
    gid = parse_id(request.query_params.get("g")) or _one_guild_id()
    uid = parse_id(request.query_params.get("u"))
    if not uid:
        return redirect("/members")
    joins = await run(db.member_joins, gid, uid)
    warns = await run(db.list_warnings, gid, uid)
    devices = await run(db.member_devices, gid, uid)
    guild = discord_bot.bot.get_guild(gid) if discord_bot.bot.is_ready() else None
    member = guild.get_member(uid) if guild else None
    name = (joins[0].username if joins else None) or (str(member) if member else str(uid))
    first_join = min((j.joined_at for j in joins), default=None)
    today = dt.datetime.now(dt.timezone.utc)
    days = (today - first_join.replace(tzinfo=dt.timezone.utc)).days if first_join else None
    status = joins[0].status if joins else "unknown"
    in_server = member is not None
    return templates.TemplateResponse(request=request, name="member.html", context={
        "csrf": csrf_token(request), "flash": request.session.pop("flash", None),
        "uid": uid, "gid": gid, "name": name, "joins": joins, "warns": warns, "devices": devices,
        "active_warns": sum(1 for w in warns if w.active), "days": days, "status": status,
        "in_server": in_server, "server": (guild.name if guild else "") or "your server",
        "first_join": first_join, "tz": TZ_LABEL, "now": today,
    })


async def member_warn(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    gid = parse_id(form.get("gid"))
    uid = parse_id(form.get("uid"))
    reason = str(form.get("reason", "")).strip()[:500]
    if gid is None or uid is None or gid not in await run(db.allowed_guild_ids):
        flash(request, "Invalid request.")
        return redirect("/members")
    if not reason:
        flash(request, "Please type a reason for the warning.")
        return redirect(f"/member?u={uid}&g={gid}")
    name = ""
    joins = await run(db.member_joins, gid, uid)
    if joins:
        name = joins[0].username
    await run(db.add_warning, gid, uid, name, reason)
    total = await run(db.warning_count, gid, uid)
    msg = "Warning added."
    if discord_bot.bot.is_ready():
        res = await security.send_warning(gid, uid, name or str(uid), reason, total, dt.datetime.now(dt.timezone.utc))
        if res["dm"] and res["log"]:
            msg = "Warning added. The member was notified by DM and in the mod-log."
        elif res["dm"]:
            msg = "Warning added and sent to the member by DM. (Set a mod-log channel on the Security page to also post it there.)"
        elif res["log"]:
            msg = "Warning added and posted to the mod-log. Their DMs are closed, so no direct message was sent."
        else:
            msg = "Warning added, but the member could not be messaged (DMs closed) and no mod-log channel is set."
    flash(request, msg)
    return redirect(f"/member?u={uid}&g={gid}")


async def member_unwarn(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    wid = parse_id(form.get("warn_id"))
    uid = parse_id(form.get("uid"))
    gid = parse_id(form.get("gid"))
    if wid is None:
        flash(request, "Invalid request.")
        return redirect("/members")
    await run(db.remove_warning, wid)
    flash(request, "Warning removed.")
    return redirect(f"/member?u={uid}&g={gid}")


async def member_export(request):
    if not authed(request):
        return redirect("/login")
    gid = parse_id(request.query_params.get("g")) or _one_guild_id()
    uid = parse_id(request.query_params.get("u"))
    if not uid:
        return redirect("/members")
    joins = await run(db.member_joins, gid, uid)
    warns = await run(db.list_warnings, gid, uid)
    devices = await run(db.member_devices, gid, uid)
    name = (joins[0].username if joins else None) or str(uid)
    first = min((j.joined_at for j in joins), default=None)
    days = (dt.datetime.now(dt.timezone.utc) - first.replace(tzinfo=dt.timezone.utc)).days if first else "-"
    L = [f"MEMBER HISTORY  -  {name}", f"User ID: {uid}", f"Days in server: {days}",
         f"Current status: {joins[0].status if joins else 'unknown'}",
         f"Generated: {local(dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))} {TZ_LABEL}", "",
         f"WARNINGS ({sum(1 for w in warns if w.active)} active / {len(warns)} total)"]
    L += [f"  [{local(w.created_at)}] {'ACTIVE ' if w.active else 'removed'} - {w.reason}" for w in warns] if warns else ["  none"]
    L += ["", "JOIN HISTORY"]
    L += [f"  [{local(j.joined_at)}] {j.status}" + (f" (decided {local(j.decided_at)})" if j.decided_at else "")
          + (f" flags: {j.flags}" if j.flags else "") for j in joins] if joins else ["  none"]
    L += ["", "DEVICES SEEN"]
    L += [f"  [{local(d.created_at)}] {d.device} | {d.tz} | {d.screen}" for d in devices] if devices else ["  none"]
    body = "\n".join(L).encode()
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_") or "member"
    return Response(body, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="history-{safe}-{uid}.txt"'})


# ---------- prompts (niche slash commands) ----------
async def prompts_page(request):
    if not authed(request):
        return redirect("/login")
    stats = await _prompt_stats()
    sel = request.query_params.get("n", "")
    keys = [x["key"] for x in stats["niches"]]
    if sel not in keys:
        sel = keys[0] if keys else ""
    sel_obj = next((x for x in stats["niches"] if x["key"] == sel), None)
    rows = await run(db.list_prompts, sel) if sel else []
    ctx = {"csrf": csrf_token(request), "flash": request.session.pop("flash", None),
           "sel": sel, "sel_label": (sel_obj["label"] if sel_obj else ""),
           "sel_emoji": (sel_obj["emoji"] if sel_obj else ""), "rows": rows}
    ctx.update(stats)
    return templates.TemplateResponse(request=request, name="prompts.html", context=ctx)


async def _prompt_stats() -> dict:
    counts = await run(db.prompt_counts)
    uses = await run(db.prompt_use_counts)
    db_niches = await run(db.list_niches)
    niches = [{"key": x.key, "label": x.label, "emoji": x.emoji, "active_niche": x.active,
               "total": counts.get(x.key, {}).get("total", 0),
               "active": counts.get(x.key, {}).get("active", 0),
               "uses": uses.get(x.key, 0)} for x in db_niches]
    available = sum(x["active"] for x in niches)
    unavailable = sum(x["total"] - x["active"] for x in niches)
    regs = await run(db.list_registrations, _one_guild_id())
    approved_users = sum(1 for r in regs if r.status == "approved")
    cmax = max([x["uses"] for x in niches] + [1])
    chart = [{"label": x["label"], "emoji": x["emoji"], "uses": x["uses"],
              "pct": round(x["uses"] / cmax * 100)} for x in niches]
    chart.sort(key=lambda c: -c["uses"])
    return {"niches": niches, "regs": regs, "uses_total": sum(uses.values()),
            "available": available, "unavailable": unavailable, "approved_users": approved_users,
            "chart": chart}


async def prompt_add(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    niche = str(form.get("niche", ""))
    text = str(form.get("text", "")).strip()
    if await run(db.get_niche, niche) is None:
        flash(request, "Unknown niche.")
        return redirect("/prompts")
    if not text:
        flash(request, "Please type the prompt.")
        return redirect(f"/prompts?n={niche}")
    await run(db.add_prompt, niche, text)
    flash(request, f"Prompt added: {len(text):,} characters, nothing cut.")
    return redirect(f"/prompts?n={niche}")


async def prompt_edit(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    pid = parse_int(form.get("id"))
    niche = str(form.get("niche", ""))
    text = str(form.get("text", "")).strip()
    if pid and text:
        await run(db.update_prompt, pid, text)
        flash(request, f"Prompt updated: {len(text):,} characters, nothing cut.")
    return redirect(f"/prompts?n={niche}")


async def prompt_toggle(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    pid = parse_int(form.get("id"))
    niche = str(form.get("niche", ""))
    if pid:
        await run(db.toggle_prompt, pid)
    return redirect(f"/prompts?n={niche}")


async def prompt_delete(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    pid = parse_int(form.get("id"))
    niche = str(form.get("niche", ""))
    if pid:
        await run(db.delete_prompt, pid)
        flash(request, "Prompt deleted.")
    return redirect(f"/prompts?n={niche}")


async def reg_decide(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    rid = parse_int(form.get("id"))
    action = str(form.get("action", ""))
    if rid and action in ("approve", "deny"):
        await run(db.set_registration_status, rid, "approved" if action == "approve" else "denied")
        extra = ""
        if action == "approve" and discord_bot.bot.is_ready():
            reg = await run(db.get_registration_by_id, rid)
            if reg is not None:
                base = (reg.username or "").split("#")[0].split(" ID")[0] or str(reg.user_id)
                extra = await security.set_member_number_nick(reg.guild_id, reg.user_id, base, reg.number)
        flash(request, ("Registration approved." + extra) if action == "approve" else "Registration declined.")
    return redirect("/prompts")


async def niche_add(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    label = str(form.get("label", "")).strip()
    emoji = str(form.get("emoji", "")).strip()
    if not label:
        flash(request, "Type a niche name.")
        return redirect("/prompts")
    key = await run(db.add_niche, label, emoji)
    await refresh_prompt_panel()
    flash(request, f"Niche '{label}' added. It now shows on the prompt panel.")
    return redirect(f"/prompts?n={key}")


async def niche_edit(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    key = str(form.get("key", ""))
    label = str(form.get("label", "")).strip()
    emoji = str(form.get("emoji", "")).strip()
    if await run(db.get_niche, key) is not None and label:
        await run(db.update_niche, key, label, emoji, None)
        await refresh_prompt_panel()
        flash(request, "Niche updated.")
    return redirect(f"/prompts?n={key}")


async def niche_toggle(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    key = str(form.get("key", ""))
    n = await run(db.get_niche, key)
    if n is not None:
        await run(db.update_niche, key, None, None, not n.active)
        await refresh_prompt_panel()
    return redirect(f"/prompts?n={key}")


async def niche_delete(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    key = str(form.get("key", ""))
    if await run(db.get_niche, key) is not None:
        await run(db.delete_niche, key)
        await refresh_prompt_panel()
        flash(request, "Niche and its prompts deleted.")
    return redirect("/prompts")


async def prompts_live(request):
    if not authed(request):
        return JSONResponse({"error": "login"}, status_code=401)
    stats = await _prompt_stats()
    stats["csrf"] = csrf_token(request)
    env = templates.env
    return JSONResponse({
        "stats_html": env.get_template("_prompts_stats.html").render(**stats),
        "regs_html": env.get_template("_prompts_regs.html").render(**stats),
    }, headers={"Cache-Control": "no-store"})


async def _watching_live() -> dict:
    w = await _watch_ctx()
    return {"watching_now": len(w["watching"]),
            "watching_html": templates.env.get_template("_watching.html").render(**w)}


async def overview_live(request):
    if not authed(request):
        return JSONResponse({"error": "login"}, status_code=401)
    data = await run(db.overview)
    b = discord_bot.bot
    ready = b.is_ready()
    return JSONResponse({
        "online": ready,
        "guilds": len(b.guilds) if ready else 0,
        "ping": round(b.latency * 1000) if ready and b.latency == b.latency else None,
        "servers": len(data["servers"]),
        "regs_total": data.get("regs_total", 0),
        "regs_pending": data.get("regs_pending", 0),
        "prompts_total": await run(db.prompt_use_total),
    }, headers={"Cache-Control": "no-store"})


SITE_KEYS = ["site_title", "site_desc", "link_discord", "link_tiktok", "link_youtube", "link_facebook"]


async def _site_ctx() -> dict:
    g = {k: await run(db.get_setting, k, "") for k in SITE_KEYS}
    stats = await run(db.public_stats)
    return {
        "title": g["site_title"] or SITE_NAME,
        "desc": g["site_desc"] or "A free creator community. Join us and start creating.",
        "links": [("Discord", g["link_discord"], "#5865F2"), ("TikTok", g["link_tiktok"], "#000000"),
                  ("YouTube", g["link_youtube"], "#FF0000"), ("Facebook", g["link_facebook"], "#1877F2")],
        "stats": stats,
        "contact": await run(contact_info),
    }


# ---------- landing page: Contact us (filled in on the Site page) ----------
import re as _re_c

CONTACT_KEYS = ["contact_note", "contact_messenger", "contact_email", "contact_telegram",
                "contact_discord", "contact_other_label", "contact_other_url"]
_EMAIL_RX = _re_c.compile(r"^[^@\s<>\"']{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}$")


def contact_info() -> dict:
    """The Contact us rows for the public page, and why a filled-in field is hidden (for the Site page)."""
    from urllib.parse import urlsplit
    v = {k: db.get_setting(k, "") for k in CONTACT_KEYS}
    rows, problems = [], {}
    if v["contact_messenger"]:
        u = _clean_url(v["contact_messenger"])
        if u:
            rows.append({"kind": "messenger", "label": "Messenger", "value": "Send us a message", "href": u})
        else:
            problems["contact_messenger"] = "Hidden: use a full link like https://m.me/yourpage"
    if v["contact_email"]:
        e = v["contact_email"].strip()
        if _EMAIL_RX.match(e) and len(e) <= 254:
            rows.append({"kind": "email", "label": "Email", "value": e, "href": "mailto:" + e, "clip": e})
        else:
            problems["contact_email"] = "Hidden: this doesn't look like an email address"
    if v["contact_telegram"]:
        tg = _re_c.sub(r"^(https?://)?(www\.)?(t\.me|telegram\.me)/", "", v["contact_telegram"].strip(), flags=_re_c.I)
        tg = tg.strip("/").lstrip("@")
        if _re_c.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", tg):
            rows.append({"kind": "telegram", "label": "Telegram", "value": "@" + tg, "href": "https://t.me/" + tg})
        else:
            problems["contact_telegram"] = "Hidden: use your Telegram username, like @yourname"
    if v["contact_discord"]:
        dc = v["contact_discord"].strip().lstrip("@")
        if _re_c.fullmatch(r"[A-Za-z0-9_.]{2,32}", dc):
            rows.append({"kind": "discord", "label": "Discord", "value": dc, "href": "", "clip": dc})
        else:
            problems["contact_discord"] = "Hidden: use your Discord username, like yourname"
    if v["contact_other_url"]:
        u = _clean_url(v["contact_other_url"])
        if u:
            sp = urlsplit(u)
            host = (sp.hostname or "").lower()
            host = host[4:] if host.startswith("www.") else host
            rows.append({"kind": "link", "label": (v["contact_other_label"].strip() or host)[:30],
                         "value": (host + sp.path).rstrip("/")[:60], "href": u})
        else:
            problems["contact_other_url"] = "Hidden: use a full link like https://instagram.com/yourname"
    note = " ".join(v["contact_note"].split())[:200]
    return {"vals": v, "rows": rows, "problems": problems,
            "note": note or "Have a question or need help with your access? Reach the owner here."}


templates.env.globals["contact_info"] = contact_info


async def site_contact_save(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    for k in CONTACT_KEYS:
        await run(db.set_setting, k, " ".join(str(form.get(k, "")).split())[:300])
    info = await run(contact_info)
    n = len(info["rows"])
    if info["problems"]:
        flash(request, "Saved, but some contacts are hidden. Check the red notes under them.")
    elif n:
        flash(request, f"Contact saved. Your public page now shows {n} way{'s' if n != 1 else ''} to reach you.")
    else:
        flash(request, "Contact saved. Everything is blank, so Contact us is hidden on your public page.")
    return redirect("/site#contact")


async def _render_landing() -> str:
    ctx = await _site_ctx()
    return templates.env.get_template("landing.html").render(**ctx)


async def landing_root(request):
    # Used when ADMIN_PATH is not set (dev). In production AdminPath serves this.
    return HTMLResponse(await _render_landing())


async def public_stats_api(request):
    return JSONResponse(await run(db.public_stats), headers={"Cache-Control": "no-store"})


async def site_page(request):
    if not authed(request):
        return redirect("/login")
    g = {k: await run(db.get_setting, k, "") for k in SITE_KEYS}
    return templates.TemplateResponse(request=request, name="site.html", context={
        "csrf": csrf_token(request), "flash": request.session.pop("flash", None),
        "g": g, "default_title": SITE_NAME,
    })


async def site_save(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    for k in SITE_KEYS:
        await run(db.set_setting, k, str(form.get(k, "")).strip()[:500])
    flash(request, "Landing page saved.")
    return redirect("/site")


_MANIFEST_ADMIN = {
    "name": "Bot Panel", "short_name": "Panel", "start_url": ".", "scope": ".",
    "display": "standalone", "background_color": "#0f131a", "theme_color": "#161B26",
    "icons": [{"src": "/static/app-icon-admin.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
}
_MANIFEST_SITE = {
    "name": SITE_NAME, "short_name": SITE_NAME[:12], "start_url": "/", "scope": "/",
    "display": "standalone", "background_color": "#0b0e14", "theme_color": "#0b0e14",
    "icons": [{"src": "/static/app-icon.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
}


async def manifest_admin(request):
    return JSONResponse(_MANIFEST_ADMIN, media_type="application/manifest+json")


async def manifest_site(request):
    m = dict(_MANIFEST_SITE)
    title = await run(db.get_setting, "site_title", "")
    if title:
        m["name"] = title
        m["short_name"] = title[:12]
    return JSONResponse(m, media_type="application/manifest+json")


# ---------- member accounts (tutorials) ----------
import hashlib as _hashlib


def _hash_pw(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = _hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120000)
    return f"pbkdf2${salt}${dk.hex()}"


def _check_pw(stored: str, password: str) -> bool:
    try:
        algo, salt, want = stored.split("$", 2)
        if algo != "pbkdf2":
            return False
        dk = _hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120000)
        return hmac.compare_digest(dk.hex(), want)
    except Exception:
        return False


def member_authed(request) -> bool:
    return bool(request.session.get("member"))


def _valid_username(u: str) -> bool:
    return 3 <= len(u) <= 40 and all(c.isalnum() or c in "_-." for c in u)


async def join_page(request):
    return templates.TemplateResponse(request=request, name="join.html", context={
        "csrf": csrf_token(request), "flash": request.session.pop("flash", None),
        "public_name": await run(db.get_setting, "site_title", "") or SITE_NAME,
    })


async def join_submit(request):
    form, resp = await public_post(request)
    if resp:
        return resp
    if _too_many("join:" + _client_ip(request)):
        flash(request, "Too many tries. Wait a few minutes and try again.")
        return redirect("/join")
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    code = str(form.get("code", "")).strip()
    if not _valid_username(username):
        flash(request, "Username must be 3-40 characters: letters, numbers, _ - . only.")
        return redirect("/join")
    if len(password) < 6:
        flash(request, "Password must be at least 6 characters.")
        return redirect("/join")
    if await run(db.get_web_user, username) is not None:
        flash(request, "That username is taken. Try another one.")
        return redirect("/join")
    info = await run(security.consume_web_signup_code_sync, code)
    if info is None:
        flash(request, "Invalid or used signup code. Tap Open tutorials in Discord to get a new one.")
        return redirect("/join")
    uid, dname = info
    ok = await run(db.create_web_user, username, _hash_pw(password), uid, dname)
    if not ok:
        flash(request, "That username is taken. Tap Open tutorials again for a new code and try another name.")
        return redirect("/join")
    request.session["member"] = username
    return redirect("/watch")


async def member_login_page(request):
    if member_authed(request):
        return redirect("/watch")
    return templates.TemplateResponse(request=request, name="member_login.html", context={
        "csrf": csrf_token(request), "flash": request.session.pop("flash", None),
        "public_name": await run(db.get_setting, "site_title", "") or SITE_NAME,
    })


async def member_login_submit(request):
    form, resp = await public_post(request)
    if resp:
        return resp
    if _too_many("login:" + _client_ip(request)):
        flash(request, "Too many tries. Wait a few minutes and try again.")
        return redirect("/members/login")
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    u = await run(db.get_web_user, username)
    if u is not None and u.active and _check_pw(u.password_hash, password):
        request.session["member"] = username
        return redirect("/watch")
    flash(request, "Wrong username or password.")
    return redirect("/members/login")


async def member_logout(request):
    form, resp = await public_post(request)
    if resp:
        return resp
    request.session.pop("member", None)
    return redirect("/members/login")


async def watch_page(request):
    if not member_authed(request):
        return redirect("/members/login")
    vids = await run(db.list_videos, True)
    return templates.TemplateResponse(request=request, name="watch.html", context={
        "csrf": csrf_token(request), "videos": vids, "username": request.session.get("member"),
        "public_name": await run(db.get_setting, "site_title", "") or SITE_NAME,
        "watermark": await run(db.get_setting, "video_watermark", "") or "David_5522",
        "resources": [{"id": r.id, "name": r.name, "url": r.url, "description": r.description,
                       "host": (r.url.split("//", 1)[-1].split("/", 1)[0].replace("www.", ""))}
                      for r in await run(db.list_resources, True)],
    })


# ---------- admin: videos management ----------
def _drive_id(url: str) -> str:
    """Extract the Google Drive file id from a share link or raw id."""
    u = (url or "").strip()
    import re as _re
    m = _re.search(r"/d/([a-zA-Z0-9_-]{20,})", u) or _re.search(r"[?&]id=([a-zA-Z0-9_-]{20,})", u)
    if m:
        return m.group(1)
    if _re.fullmatch(r"[a-zA-Z0-9_-]{20,}", u):
        return u
    return ""


async def videos_page(request):
    if not authed(request):
        return redirect("/login")
    return templates.TemplateResponse(request=request, name="videos.html", context={
        "devices": await run(db.device_map),
        "one_device": (await run(db.get_setting, "member_one_device", "1")) != "0",
        "announce": await _announce_options(),
        "announce_video": await run(db.get_setting, "announce_video", ""),
        "announce_resource": await run(db.get_setting, "announce_resource", ""),
        "resources": await run(db.list_resources),
        "vstats": await run(db.view_stats),
        "csrf": csrf_token(request), "flash": request.session.pop("flash", None),
        "videos": await run(db.list_videos), "members": await run(db.list_web_users),
        "watermark": await run(db.get_setting, "video_watermark", "") or "David_5522",
    })


async def video_add(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    did = _drive_id(str(form.get("link", "")))
    title = str(form.get("title", "")).strip()
    if not did:
        flash(request, "Could not read that Google Drive link. Use the file's Share link.")
        return redirect("/videos")
    if not title:
        flash(request, "Please add a title.")
        return redirect("/videos")
    await run(db.add_video, title, str(form.get("description", "")).strip(), did)
    flash(request, "Video added.")
    return redirect("/videos")


async def video_edit(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    vid = parse_int(form.get("id"))
    did = _drive_id(str(form.get("link", "")))
    if vid and did:
        await run(db.update_video, vid, str(form.get("title", "")).strip(),
                  str(form.get("description", "")).strip(), did)
        flash(request, "Video updated.")
    return redirect("/videos")


async def video_toggle(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    vid = parse_int(form.get("id"))
    if vid:
        await run(db.toggle_video, vid)
    return redirect("/videos")


async def video_delete(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    vid = parse_int(form.get("id"))
    if vid:
        await run(db.delete_video, vid)
        flash(request, "Video deleted.")
    return redirect("/videos")


async def video_wm_save(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    await run(db.set_setting, "video_watermark", str(form.get("watermark", "")).strip()[:60])
    flash(request, "Watermark saved.")
    return redirect("/videos")


async def video_strict_toggle(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    on = (await run(db.get_setting, "video_strict", "1")) != "0"
    await run(db.set_setting, "video_strict", "0" if on else "1")
    flash(request, "Strict mode is now " + ("OFF." if on else "ON."))
    return redirect("/videos")


def _ago(t) -> str:
    """'just now', '3m ago', '2h ago', '4d ago' for a naive-UTC datetime."""
    if t is None:
        return ""
    s = int((dt.datetime.now(dt.timezone.utc) - t.replace(tzinfo=dt.timezone.utc)).total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _dur(seconds: int) -> str:
    s = int(seconds or 0)
    if s < 60:
        return "<1m"
    m = s // 60
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


async def _watch_ctx() -> dict:
    """Live viewers + recent views for the Overview panel."""
    now_rows = await run(db.watching_now)
    recent = await run(db.recent_views, 12)
    per = (await run(db.view_stats))["per_video"]
    live = {}
    for r in now_rows:
        live[r.video_id] = live.get(r.video_id, 0) + 1
    vids = list(reversed(await run(db.list_videos, True)))  # oldest first, same order as the tutorials page
    top = max([per.get(v.id, {}).get("views", 0) for v in vids] + [1])
    lessons = [{"n": i + 1, "id": v.id, "title": v.title, "now": live.get(v.id, 0),
                "views": per.get(v.id, {}).get("views", 0), "members": per.get(v.id, {}).get("viewers", 0),
                "pct": round(100 * per.get(v.id, {}).get("views", 0) / top)} for i, v in enumerate(vids)]
    return {
        "lessons": lessons,
        "watching": [{"user": r.username, "discord": r.discord_name, "title": r.title,
                      "for": _dur(r.seconds), "seen": _ago(r.last_seen)} for r in now_rows],
        "recent_views": [{"user": r.username, "discord": r.discord_name, "title": r.title,
                          "dur": _dur(r.seconds), "when": _ago(r.last_seen)} for r in recent],
    }


async def watch_ping(request):
    """Heartbeat from the tutorials page: marks the member as watching a video."""
    if not member_authed(request):
        return JSONResponse({"ok": False}, status_code=401)
    form, resp = await public_post(request)
    if resp:
        return JSONResponse({"ok": False}, status_code=400)
    vid = parse_int(form.get("vid"))
    if not vid:
        return JSONResponse({"ok": False}, status_code=400)
    v = next((x for x in await run(db.list_videos, True) if x.id == vid), None)
    if v is None:
        return JSONResponse({"ok": False}, status_code=404)
    username = request.session.get("member")
    u = await run(db.get_web_user, username)
    if u is None or not u.active:
        request.session.pop("member", None)
        return JSONResponse({"ok": False}, status_code=401)
    await run(db.record_view_ping, username, u.discord_name, v.id, v.title)
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


def _clean_url(u: str) -> str:
    """Only plain web links (https://site.com/...). Anything else returns ''."""
    from urllib.parse import urlsplit
    u = (u or "").strip()
    if u and "://" not in u:
        u = "https://" + u
    try:
        p = urlsplit(u)
    except ValueError:
        return ""
    host = p.hostname or ""
    if p.scheme not in ("http", "https") or "." not in host or len(u) > 500:
        return ""
    if any(c in u for c in ' "\'<>'):
        return ""
    return u


async def resource_add(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    name = str(form.get("name", "")).strip()
    url = _clean_url(str(form.get("url", "")))
    if not name or not url:
        flash(request, "Add a name and a valid link (https://...).")
        return redirect("/videos")
    await run(db.add_resource, name, url, str(form.get("description", "")).strip())
    flash(request, f"Resource '{name}' added. Members see it under Resources.")
    return redirect("/videos")


async def resource_edit(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    rid = parse_int(form.get("id"))
    name = str(form.get("name", "")).strip()
    url = _clean_url(str(form.get("url", "")))
    if rid and name and url:
        await run(db.update_resource, rid, name, url, str(form.get("description", "")).strip())
        flash(request, "Resource updated.")
    else:
        flash(request, "Add a name and a valid link (https://...).")
    return redirect("/videos")


async def resource_toggle(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    rid = parse_int(form.get("id"))
    if rid:
        await run(db.toggle_resource, rid)
    return redirect("/videos")


async def resource_delete(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    rid = parse_int(form.get("id"))
    if rid:
        await run(db.delete_resource, rid)
        flash(request, "Resource deleted.")
    return redirect("/videos")


async def _viewers_ctx() -> dict:
    ctx = await _watch_ctx()
    ctx["totals"] = await run(db.view_stats)
    return ctx


async def viewers_page(request):
    """Admin page: who is watching the tutorials, per lesson."""
    if not authed(request):
        return redirect("/login")
    ctx = await _viewers_ctx()
    ctx["csrf"] = csrf_token(request)
    return templates.TemplateResponse(request=request, name="viewers.html", context=ctx)


async def viewers_live(request):
    if not authed(request):
        return JSONResponse({"error": "login"}, status_code=401)
    ctx = await _viewers_ctx()
    return JSONResponse({"watching_now": len(ctx["watching"]),
                         "html": templates.env.get_template("_watching.html").render(**ctx)},
                        headers={"Cache-Control": "no-store"})
# ---------- Discord posts for new videos / resources ----------
async def _announce_options() -> list:
    """Text channels the bot can post in, grouped by category, for the 'Post to Discord' dropdowns."""
    import discord
    b = discord_bot.bot
    if not b.is_ready():
        return []
    out = []
    for gid in sorted(await run(db.allowed_guild_ids)):
        g = b.get_guild(gid)
        if g is None:
            continue
        groups = []
        for cat, chans in g.by_category():
            ok = []
            for c in chans:
                if getattr(c, "type", None) not in (discord.ChannelType.text, discord.ChannelType.news):
                    continue
                perms = c.permissions_for(g.me)
                if perms.view_channel and perms.send_messages and perms.embed_links:
                    ok.append((str(c.id), c.name))
            if ok:
                groups.append((cat.name if cat else "No category", ok))
        if groups:
            out.append({"server": g.name, "groups": groups})
    return out


_IMG_SIGS = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"), (b"GIF87a", "gif"), (b"GIF89a", "gif"))
_IMG_MAX = 8 * 1024 * 1024


async def _read_image(form) -> tuple:
    """The picture chosen for the Discord post: ((bytes, filename) or None, problem text)."""
    f = form.get("image")
    if f is None or isinstance(f, str) or not getattr(f, "filename", ""):
        return None, ""
    data = await f.read(_IMG_MAX + 1)
    if not data:
        return None, ""
    if len(data) > _IMG_MAX:
        return None, "the picture is bigger than 8 MB"
    ext = next((e for sig, e in _IMG_SIGS if data.startswith(sig)), None)
    if ext is None and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = "webp"
    if ext is None:
        return None, "it must be a PNG, JPG, GIF or WEBP picture"
    return (data, f"cover.{ext}"), ""


async def _announce(channel_id: str, title: str, body: str, buttons: list, image=None) -> str:
    """Post an embed with link buttons (and an optional big picture).
    Returns the channel name, or raises ValueError with a short reason."""
    import discord
    import io
    b = discord_bot.bot
    if not b.is_ready():
        raise ValueError("the bot is offline")
    ch = b.get_channel(int(channel_id)) if channel_id.isdigit() else None
    if ch is None or getattr(ch, "guild", None) is None or ch.guild.id not in await run(db.allowed_guild_ids):
        raise ValueError("that channel was not found")
    name = await run(db.get_setting, "site_title", "") or SITE_NAME
    e = discord.Embed(title=title[:250], description=body[:3800], color=0x5865F2)
    e.set_footer(text=f"{name} tutorials")
    view = discord.ui.View(timeout=None)
    for label, url in buttons:
        if url and url.startswith(("https://", "http://")):
            view.add_item(discord.ui.Button(label=label[:80], url=url))
    extra = {}
    if image:
        e.set_image(url=f"attachment://{image[1]}")
        extra["file"] = discord.File(io.BytesIO(image[0]), filename=image[1])
    try:
        await ch.send(embed=e, view=view if view.children else None, **extra)
    except discord.Forbidden:
        raise ValueError(f"the bot can't post in #{ch.name}")
    except discord.HTTPException as ex:
        if ex.status == 413:
            raise ValueError("the picture is too big for Discord")
        raise ValueError(f"Discord said no ({ex.status})")
    return ch.name


def _watch_url() -> str:
    return f"{security.PUBLIC_URL.rstrip('/')}/watch" if security.PUBLIC_URL else ""


async def _post_result(request, kind: str, channel_id: str, title: str, body: str, buttons: list,
                       image=None, problem: str = "") -> None:
    """Remember the channel choice, post, and add the outcome to the flash message."""
    await run(db.set_setting, f"announce_{kind}", channel_id)
    if not channel_id:
        return
    try:
        ch = await _announce(channel_id, title, body, buttons, image)
        note = f" Posted in #{ch}" + (" with your picture." if image else ".")
        if problem:
            note += f" Picture left out: {problem}."
    except ValueError as ex:
        note = f" Not posted to Discord: {ex}."
    request.session["flash"] = (request.session.get("flash") or "") + note


async def video_add_and_announce(request):
    """Add the video as usual, then post it to the chosen Discord channel."""
    form = await request.form()
    image, problem = await _read_image(form)
    before = {v.id for v in await run(db.list_videos, False)}
    resp = await video_add(request)
    new = [v for v in await run(db.list_videos, False) if v.id not in before]
    if new:
        v = new[0]
        body = (v.description + "\n\n" if v.description else "") + \
            "Watch it on the tutorials page. No account yet? Tap **Open tutorials** in the server."
        await _post_result(request, "video", str(form.get("announce", "")).strip(), f"New tutorial: {v.title}",
                           body, [("Watch now", _watch_url())], image, problem)
    return resp


async def resource_add_and_announce(request):
    """Add the resource as usual, then post it to the chosen Discord channel."""
    form = await request.form()
    image, problem = await _read_image(form)
    before = {r.id for r in await run(db.list_resources)}
    resp = await resource_add(request)
    new = [r for r in await run(db.list_resources) if r.id not in before]
    if new:
        r = new[0]
        body = (r.description + "\n\n" if r.description else "") + \
            "You can also find it under Resources on the tutorials page."
        await _post_result(request, "resource", str(form.get("announce", "")).strip(), f"New resource: {r.name}",
                           body, [(f"Open {r.name}", r.url), ("Tutorials page", _watch_url())], image, problem)
    return resp


# ---------- one device per member account ----------
_DEV_COOKIE = "sdev"
_LOCKED_MSG = ("This account is already used on another device. "
               "Ask the owner to reset your device, then log in again.")


def _device_label(request) -> str:
    ua = request.headers.get("user-agent", "")
    os_ = ("iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua else
           "Windows" if "Windows" in ua else "Mac" if "Macintosh" in ua else "Linux" if "Linux" in ua else "Unknown")
    br = ("App" if "; wv)" in ua else "Edge" if "Edg/" in ua else "Opera" if "OPR/" in ua else
          "Samsung Internet" if "SamsungBrowser" in ua else "Chrome" if ("Chrome/" in ua or "CriOS" in ua) else
          "Firefox" if ("Firefox/" in ua or "FxiOS" in ua) else "Safari" if "Safari/" in ua else "Browser")
    return f"{os_} - {br}"


async def _device_gate(request, username: str, mode: str) -> tuple:
    """Check this browser against the account's locked device. Returns (status, new_cookie_or_None)."""
    import hashlib
    if (await run(db.get_setting, "member_one_device", "1")) == "0":
        return "off", None
    u = await run(db.get_web_user, username)
    if u is None:
        return "ok", None
    tok, new = request.cookies.get(_DEV_COOKIE, ""), None
    if not (20 <= len(tok) <= 100):
        tok = new = secrets.token_urlsafe(32)
    st = await run(db.device_check, username, u.id, hashlib.sha256(tok.encode()).hexdigest(),
                   _device_label(request), mode, u.created_at)
    return st, new


def _set_dev_cookie(resp, tok) -> None:
    if tok:
        resp.set_cookie(_DEV_COOKIE, tok, max_age=60 * 60 * 24 * 400, httponly=True,
                        secure=bool(os.getenv("RENDER")), samesite="lax", path="/")


# ---------- tutorials: only current, verified members ----------
_ACCESS_CACHE: dict = {}
_ACCESS_MSG = {
    "gone": "Your website account was removed. Contact the server owner.",
    "off": "Your website account is turned off. Contact the server owner.",
    "noreg": "You don't have prompt access right now, so the tutorials are closed for you. "
             "Contact the server owner.",
    "left": "You're no longer in the Discord server, so the tutorials are closed for you. "
            "Rejoin and get approved to watch again.",
    "pending": "You need to finish the verification in the Discord server first, then log in again.",
}


async def _member_access_check(username: str) -> str:
    import discord
    u = await run(db.get_web_user, username)
    if u is None:
        return "gone"
    if not u.active:
        return "off"
    uid = int(u.discord_user_id or 0)
    if not uid or uid in discord_bot.OWNER_IDS:
        return ""
    gids = await run(lambda: [g for g in db.allowed_guild_ids() if db.is_registered_approved(g, uid)])
    if not gids:
        return "noreg"
    b = discord_bot.bot
    if not b.is_ready():
        return ""            # can't ask Discord right now: don't lock everyone out during a restart
    unsure = pending = False
    for gid in gids:
        g = b.get_guild(gid)
        if g is None:
            unsure = True
            continue
        m = g.get_member(uid)
        if m is None:
            try:
                m = await g.fetch_member(uid)
            except discord.NotFound:
                continue     # not in this server anymore
            except Exception:
                unsure = True
                continue
        if await run(security.gate_on_sync, gid):
            role = await security.find_role(g)
            if role is not None and role in m.roles:
                pending = True
                continue
        return ""
    if unsure:
        return ""
    return "pending" if pending else "left"


async def _member_access(username: str) -> str:
    """'' if this website member may still watch; otherwise why not (a key of _ACCESS_MSG).
    Checked again every minute: left the server, got kicked or banned, prompt access removed,
    or account deleted on the dashboard all close the tutorials."""
    now = time.monotonic()
    hit = _ACCESS_CACHE.get(username)
    if hit and hit[0] > now:
        return hit[1]
    why = await _member_access_check(username)
    if len(_ACCESS_CACHE) > 5000:
        _ACCESS_CACHE.clear()
    _ACCESS_CACHE[username] = (now + (60 if not why else 15), why)
    return why


async def _login_with_device(request, handler):
    """Run the normal login/signup, then lock the account to this device (or refuse another one)."""
    resp = await handler(request)
    user = request.session.get("member")
    if not user or not resp.headers.get("location", "").endswith("/watch"):
        return resp
    why = await _member_access(user)
    if why:
        request.session.pop("member", None)
        flash(request, _ACCESS_MSG[why])
        return redirect("/members/login")
    st, new = await _device_gate(request, user, "login")
    if st == "other":
        request.session.pop("member", None)
        flash(request, _LOCKED_MSG)
        resp = redirect("/members/login")
    _set_dev_cookie(resp, new)
    return resp


async def member_login_submit_1dev(request):
    return await _login_with_device(request, member_login_submit)


async def join_submit_1dev(request):
    return await _login_with_device(request, join_submit)


async def _watch_device_ok(request) -> tuple:
    user = request.session.get("member")
    if not user:
        return True, None
    why = await _member_access(user)
    if why:
        request.session.pop("member", None)
        flash(request, _ACCESS_MSG[why])
        return False, None
    st, new = await _device_gate(request, user, "watch")
    if st in ("other", "reset"):
        request.session.pop("member", None)
        flash(request, _LOCKED_MSG if st == "other" else
              "Your device was reset. Log in again on the device you want to use.")
        return False, None
    return True, new


async def watch_page_1dev(request):
    ok, new = await _watch_device_ok(request)
    if not ok:
        return redirect("/members/login")
    resp = await watch_page(request)
    _set_dev_cookie(resp, new)
    return resp


async def watch_ping_1dev(request):
    ok, new = await _watch_device_ok(request)
    if not ok:
        return JSONResponse({"ok": False}, status_code=401)
    resp = await watch_ping(request)
    _set_dev_cookie(resp, new)
    return resp


async def member_device_reset(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    name = str(form.get("username", "")).strip()[:40]
    if name:
        await run(db.device_reset, name)
        flash(request, f"Device reset for {name}. The next device they log in on becomes their device.")
    return redirect("/videos")


async def one_device_toggle(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    on = (await run(db.get_setting, "member_one_device", "1")) != "0"
    await run(db.set_setting, "member_one_device", "0" if on else "1")
    flash(request, "1 device per account is now off." if on else
          "1 device per account is on. Each account works only on the device it logs in on first.")
    return redirect("/videos")


# ---------- members send their verification code from the website ----------
async def _pending_by_code(code: str):
    for j in await run(db.list_joins, None, True, 500):
        saved = await run(db.get_setting, f"code:{j.id}", "")
        if saved and security._code_matches(saved, code):
            return j
    return None


async def _tell_owners(j, note: str) -> None:
    """DM the owner(s) that a waiting member sent their code. Falls back to the security log channel."""
    import discord
    b = discord_bot.bot
    if not b.is_ready():
        return
    g = b.get_guild(j.guild_id)
    e = discord.Embed(title="Verification code received",
                      description=(f"**{j.username}** sent their code from the website.\n"
                                   "Open **Security** in your bot panel and tap **Approve**."),
                      color=0x5865F2)
    e.add_field(name="Discord ID", value=str(j.user_id))
    if note:
        e.add_field(name="Their note", value=note[:200], inline=False)
    if g:
        e.set_footer(text=g.name)
    sent = 0
    for oid in discord_bot.OWNER_IDS:
        try:
            u = b.get_user(oid) or await b.fetch_user(oid)
            await u.send(embed=e)
            sent += 1
        except Exception:
            pass
    if not sent and g:
        try:
            await security.notify(g, e)
        except Exception:
            pass


async def send_code_api(request):
    """Public: a waiting member sends their 6-character code to the owner (landing page or verify page)."""
    bad = JSONResponse({"ok": False, "msg": "Something went wrong. Refresh the page and try again."}, status_code=400)
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return bad
    if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return bad
    if _too_many("sendcode:" + _client_ip(request), limit=8):
        return JSONResponse({"ok": False, "msg": "Too many tries. Wait a few minutes and try again."}, status_code=429)
    body = await request.body()
    if len(body) > 2000:
        return bad
    try:
        data = json.loads(body)
    except ValueError:
        return bad
    if not isinstance(data, dict):
        return bad
    code = "".join(ch for ch in str(data.get("code", "")).upper() if ch.isalnum())
    note = " ".join(str(data.get("name", "")).split())[:60]
    if len(code) != 6:
        return JSONResponse({"ok": False, "msg": "Codes are 6 characters, like K7MQ2X."})
    j = await _pending_by_code(code)
    if j is None:
        return JSONResponse({"ok": False, "msg": "That code doesn't match anyone waiting. Check it, or open the "
                                                 "Verify link from the bot again to see your code."})
    prev, _, old_note = (await run(db.get_setting, f"wcode:{j.id}", "")).partition("|")
    await run(db.set_setting, f"wcode:{j.id}", f"{int(time.time())}|{note or old_note}"[:480])
    if not prev.isdigit() or time.time() - int(prev) > 600:      # one DM per member every 10 minutes
        await _tell_owners(j, note)
    return JSONResponse({"ok": True, "msg": "Sent! The owner will check your code. "
                                            "You'll get a message from the bot once you're approved."})


def web_code_info(join_id: int):
    """For the Security page: did this member send their code from the website?"""
    t, _, note = db.get_setting(f"wcode:{join_id}", "").partition("|")
    if not t.isdigit():
        return None
    return {"ago": _ago(dt.datetime.fromtimestamp(int(t), dt.timezone.utc).replace(tzinfo=None)), "note": note}


templates.env.globals["web_code_info"] = web_code_info


async def security_approve_web(request):
    """Approve a member who sent a valid code from the website."""
    form, resp = await guarded_post(request)
    if resp:
        return resp
    jid = parse_int(form.get("id"))
    if not jid or not await run(db.get_setting, f"wcode:{jid}", ""):
        flash(request, "That member hasn't sent their code from the website.")
        return redirect("/security")
    if not discord_bot.bot.is_ready():
        flash(request, "The bot is not connected to Discord yet. Try again in a moment.")
        return redirect("/security")
    msg = await security.decide(jid, "approve")
    await run(db.set_setting, f"wcode:{jid}", "")
    flash(request, msg)
    return redirect("/security")


# ---------- tap to verify: /register (device check) and /weblogin (one-tap login) ----------
def _same_site_json(request) -> bool:
    return (request.headers.get("content-type", "").split(";")[0].strip().lower() == "application/json"
            and request.headers.get("sec-fetch-site", "same-origin") in ("same-origin", "none"))


async def reg_verify_page(request):
    token = request.path_params.get("token", "")
    reg = await run(security.reg_for_token_sync, token)
    guild = discord_bot.bot.get_guild(reg.guild_id) if reg else None
    resp = templates.TemplateResponse(request=request, name="register_verify.html", context={
        "valid": reg is not None, "server": guild.name if guild else "the server",
    })
    if not request.cookies.get("dbv"):
        resp.set_cookie("dbv", secrets.token_urlsafe(24), max_age=60 * 60 * 24 * 730,
                        httponly=True, secure=bool(os.getenv("RENDER")), samesite="lax", path="/v/")
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def reg_verify_submit(request):
    body = await request.body()
    if len(body) > 8000:
        return JSONResponse({"ok": False, "msg": "Request too large."}, status_code=413)
    try:
        fp = json.loads(body or b"{}")
        if not isinstance(fp, dict):
            raise ValueError
    except ValueError:
        return JSONResponse({"ok": False, "msg": "Bad request."}, status_code=400)
    if not discord_bot.bot.is_ready():
        return JSONResponse({"ok": False, "msg": "The bot is starting up. Wait a minute and tap Verify again."})
    result = await security.check_register_device(request.path_params.get("token", ""), request.cookies.get("dbv", ""),
                                                  fp, _client_ip(request), request.headers.get("user-agent", "")[:400])
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


def _web_user_for_discord_sync(uid: int, dname: str) -> str:
    """The website username for this Discord member; makes one (no password needed) the first time."""
    u = db.get_web_user_by_discord(uid)
    if u is not None:
        return u.username
    base = "".join(c for c in (dname or "").split("#")[0].split(" ID")[0].lower() if c.isalnum() or c in "_-.")[:24]
    if len(base) < 3:
        base = f"member{str(uid)[-4:]}"
    for n in range(0, 60):
        name = base if n == 0 else f"{base}{n + 1}"
        if db.create_web_user(name, _hash_pw(secrets.token_urlsafe(24)), uid, dname):
            return name
    name = f"{base}{secrets.randbelow(10 ** 6)}"
    db.create_web_user(name, _hash_pw(secrets.token_urlsafe(24)), uid, dname)
    return name


async def magic_login_page(request):
    return templates.TemplateResponse(request=request, name="member_go.html", context={
        "public_name": await run(db.get_setting, "site_title", "") or SITE_NAME, "flash": None,
    }, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


async def magic_login_submit(request):
    """One tap from /weblogin: log in on this device (device lock still applies)."""
    if not _same_site_json(request):
        return JSONResponse({"ok": False, "msg": "Bad request."}, status_code=400)
    if _too_many("magic:" + _client_ip(request), limit=20):
        return JSONResponse({"ok": False, "msg": "Too many tries. Wait a few minutes and try again."}, status_code=429)
    got = await run(security.consume_web_login_token_sync, request.path_params.get("token", ""))
    if not got:
        return JSONResponse({"ok": False, "msg": "This login link has expired or was already used. "
                                                 "Tap Open tutorials in Discord for a new one."})
    uid, dname = got
    username = await run(_web_user_for_discord_sync, uid, dname)
    u = await run(db.get_web_user, username)
    if u is None or not u.active:
        return JSONResponse({"ok": False, "msg": "Your website account is turned off. Contact the server owner."})
    why = await _member_access(username)
    if why:
        return JSONResponse({"ok": False, "msg": _ACCESS_MSG[why]})
    new = None
    gate = globals().get("_device_gate")
    if gate is not None:
        st, new = await gate(request, username, "login")
        if st == "other":
            return JSONResponse({"ok": False, "msg": _LOCKED_MSG})
    request.session["member"] = username
    resp = JSONResponse({"ok": True, "next": "/watch"}, headers={"Cache-Control": "no-store"})
    if new:
        _set_dev_cookie(resp, new)
    return resp


# ---------- verification panel (Security page) ----------
def panel_info() -> dict:
    """For the Security page: channels the bot can post in, and where the panel is now."""
    import discord
    b = discord_bot.bot
    out = {"servers": [], "current": []}
    if not b.is_ready():
        return out
    for gid in sorted(db.allowed_guild_ids()):
        g = b.get_guild(gid)
        if g is None:
            continue
        groups = []
        for cat, chans in g.by_category():
            ok = [(str(c.id), c.name) for c in chans
                  if getattr(c, "type", None) in (discord.ChannelType.text, discord.ChannelType.news)
                  and c.permissions_for(g.me).send_messages and c.permissions_for(g.me).embed_links]
            if ok:
                groups.append((cat.name if cat else "No category", ok))
        if groups:
            out["servers"].append({"name": g.name, "groups": groups})
        ch = b.get_channel(int(db.get_setting(f"vpanel_ch:{gid}", "0") or 0))
        if ch is not None:
            out["current"].append({"server": g.name, "channel": ch.name, "id": str(ch.id),
                                   "btns": db.get_setting(f"vpanel_keys:{gid}", "").split(",")})
    return out


templates.env.globals["panel_info"] = panel_info


async def _remove_panel(gid: int) -> None:
    b = discord_bot.bot
    ch_id, _, msg_id = (await run(db.get_setting, f"vpanel_msg:{gid}", "")).partition("|")
    if ch_id.isdigit() and msg_id.isdigit():
        ch = b.get_channel(int(ch_id))
        if ch is not None:
            try:
                await (await ch.fetch_message(int(msg_id))).delete()
            except Exception:
                pass
    await run(db.set_setting, f"vpanel_msg:{gid}", "")
    await run(db.set_setting, f"vpanel_ch:{gid}", "")


async def security_panel_post(request):
    """Post (or move) the verification panel to the chosen channel."""
    import discord
    import prompt_commands
    form, resp = await guarded_post(request)
    if resp:
        return resp
    b = discord_bot.bot
    if not b.is_ready():
        flash(request, "The bot is not connected to Discord yet. Try again in a moment.")
        return redirect("/security")
    cid = str(form.get("channel", "")).strip()
    keys = [k for k in ("verify", "register", "weblogin") if form.get(k)]
    ch = b.get_channel(int(cid)) if cid.isdigit() else None
    if ch is None or getattr(ch, "guild", None) is None or ch.guild.id not in await run(db.allowed_guild_ids):
        flash(request, "Pick a channel first.")
        return redirect("/security")
    if not keys:
        flash(request, "Pick at least one button for the panel.")
        return redirect("/security")
    g = ch.guild
    await _remove_panel(g.id)
    e, v = prompt_commands.panel_message(g.name, keys)
    try:
        msg = await ch.send(embed=e, view=v)
    except (discord.Forbidden, discord.HTTPException):
        flash(request, f"The bot can't post in #{ch.name}. Give it Send Messages and Embed Links there.")
        return redirect("/security")
    await run(db.set_setting, f"vpanel_msg:{g.id}", f"{ch.id}|{msg.id}")
    await run(db.set_setting, f"vpanel_ch:{g.id}", str(ch.id))
    await run(db.set_setting, f"vpanel_keys:{g.id}", ",".join(keys))
    note = ""
    if "verify" in keys and await run(security.gate_on_sync, g.id):
        role = await security.find_role(g)
        if role is not None:
            try:
                await ch.set_permissions(role, view_channel=True, read_message_history=True, send_messages=False,
                                         reason="Verification panel: new members can see it")
                note = " New members waiting for approval can see that channel."
            except (discord.Forbidden, discord.HTTPException):
                note = (" Could not show the channel to new members: give the bot Manage Channels there, "
                        "or let the Pending role view it.")
    flash(request, f"Panel posted in #{ch.name}.{note}")
    return redirect("/security")


async def security_panel_remove(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    for gid in await run(db.allowed_guild_ids):
        await _remove_panel(gid)
    flash(request, "Verification panel removed.")
    return redirect("/security")


# ---------- prompt panel (Prompts page): niche buttons in a channel the owner picks ----------
def prompt_panel_info() -> dict:
    """For the Prompts page: channels the bot can post in, and where the prompt panel is now."""
    out = panel_info()
    out["current"] = []
    b = discord_bot.bot
    if not b.is_ready():
        return out
    for gid in sorted(db.allowed_guild_ids()):
        g = b.get_guild(gid)
        ch = b.get_channel(int(db.get_setting(f"ppanel_ch:{gid}", "0") or 0)) if g is not None else None
        if ch is not None:
            out["current"].append({"server": g.name, "channel": ch.name, "id": str(ch.id)})
    return out


templates.env.globals["prompt_panel_info"] = prompt_panel_info


async def _prompt_panel_parts() -> list:
    return prompt_commands.prompt_panel_messages(await run(db.list_niches, True))


def _panel_ids(raw: str) -> tuple:
    """'channel|msg1,msg2' -> ('channel', ['msg1', 'msg2'])"""
    ch_id, _, ids = (raw or "").partition("|")
    return ch_id, [m for m in ids.split(",") if m.isdigit()]


async def _send_prompt_panel(ch) -> list:
    """Post the panel (one message per 25 buttons). Returns the message ids."""
    ids = []
    try:
        for content, e, v in await _prompt_panel_parts():
            ids.append(str((await ch.send(content=content, embed=e, view=v)).id))
    except Exception:
        for mid in ids:                                 # don't leave half a panel behind
            try:
                await (await ch.fetch_message(int(mid))).delete()
            except Exception:
                pass
        raise
    return ids


async def refresh_prompt_panel() -> None:
    """Niches changed on the dashboard: update the buttons on the posted prompt panel."""
    b = discord_bot.bot
    if not b.is_ready():
        return
    for gid in await run(db.allowed_guild_ids):
        ch_id, ids = _panel_ids(await run(db.get_setting, f"ppanel_msg:{gid}", ""))
        ch = b.get_channel(int(ch_id)) if ch_id.isdigit() else None
        if ch is None or not ids:
            continue
        try:
            parts = await _prompt_panel_parts()
            if len(parts) == len(ids):
                for (content, e, v), mid in zip(parts, ids):
                    await (await ch.fetch_message(int(mid))).edit(content=content, embed=e, view=v)
            else:                                       # it needs more or fewer messages now: post it again
                await _remove_prompt_panel(gid)
                new = await _send_prompt_panel(ch)
                await run(db.set_setting, f"ppanel_msg:{gid}", f"{ch.id}|{','.join(new)}")
                await run(db.set_setting, f"ppanel_ch:{gid}", str(ch.id))
        except Exception:
            log.warning("Could not update the prompt panel in #%s", getattr(ch, "name", ch_id))


async def _remove_prompt_panel(gid: int) -> None:
    b = discord_bot.bot
    ch_id, ids = _panel_ids(await run(db.get_setting, f"ppanel_msg:{gid}", ""))
    ch = b.get_channel(int(ch_id)) if ch_id.isdigit() else None
    if ch is not None:
        for mid in ids:
            try:
                await (await ch.fetch_message(int(mid))).delete()
            except Exception:
                pass
    await run(db.set_setting, f"ppanel_msg:{gid}", "")
    await run(db.set_setting, f"ppanel_ch:{gid}", "")


# ---------- daily word (motivation + verse at 6 AM, 3 PM, 10 PM) ----------
def _dw_guild() -> int:
    """The server whose daily word settings to show: the one with a channel set, else the first server."""
    for gid in sorted(db.allowed_guild_ids()):
        if daily_word.channel_id(gid):
            return gid
    return _one_guild_id()


def _dw_slot_now() -> str:
    h = dt.datetime.now(daily_word.TZ).hour
    return "morning" if 4 <= h < 12 else "afternoon" if 12 <= h < 19 else "night"


async def daily_page(request):
    if not authed(request):
        return redirect("/login")
    info = await run(panel_info)
    gid = await run(_dw_guild)
    ch_id = await run(daily_word.channel_id, gid) if gid else 0
    b = discord_bot.bot
    ch = b.get_channel(ch_id) if ch_id and b.is_ready() else None
    return templates.TemplateResponse(request=request, name="daily.html", context={
        "csrf": csrf_token(request), "flash": request.session.pop("flash", None), "tz": TZ_LABEL,
        "servers": info["servers"], "current": ch.name if ch else "", "current_id": str(ch_id) if ch else "",
        "on": bool(gid) and await run(daily_word.is_on, gid), "lang": await run(daily_word.language),
        "pic": await run(daily_word.picture_on),
        "times": [s["label"] for s in daily_word.SLOTS.values()], "log": await run(daily_word.history)})


async def daily_save(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    b = discord_bot.bot
    cid = str(form.get("channel", "")).strip()
    ch = b.get_channel(int(cid)) if cid.isdigit() and b.is_ready() else None
    if ch is None or getattr(ch, "guild", None) is None or ch.guild.id not in await run(db.allowed_guild_ids):
        flash(request, "Pick a channel first.")
        return redirect("/daily")
    lang = str(form.get("lang", "both"))
    on = form.get("on") == "1"
    for other in await run(db.allowed_guild_ids):          # one channel at a time
        if other != ch.guild.id:
            await run(db.set_setting, f"dw_on:{other}", "0")
    await run(db.set_setting, f"dw_ch:{ch.guild.id}", str(ch.id))
    await run(db.set_setting, f"dw_on:{ch.guild.id}", "1" if on else "0")
    await run(db.set_setting, "dw_lang", lang if lang in daily_word.TRANSLATIONS else "both")
    await run(db.set_setting, "dw_pic", "1" if form.get("pic") == "1" else "0")
    times = ", ".join(s["label"] for s in daily_word.SLOTS.values())
    flash(request, f"Saved. The daily word posts in #{ch.name} every day at {times}." if on
          else "Saved. The daily word is off.")
    return redirect("/daily")


async def daily_send(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    if not discord_bot.bot.is_ready():
        flash(request, "The bot is not connected to Discord yet. Try again in a moment.")
        return redirect("/daily")
    problem = await daily_word.post(await run(_dw_guild), _dw_slot_now(), by_hand=True)
    flash(request, problem or ("Sent. Check the channel." + (" " + daily_word.STATE["last_error"] if daily_word.STATE["last_error"] else "")))
    return redirect("/daily")


async def prompts_panel_post(request):
    """Post (or move) the prompt panel to the chosen channel."""
    import discord
    form, resp = await guarded_post(request)
    if resp:
        return resp
    b = discord_bot.bot
    if not b.is_ready():
        flash(request, "The bot is not connected to Discord yet. Try again in a moment.")
        return redirect("/prompts")
    cid = str(form.get("channel", "")).strip()
    ch = b.get_channel(int(cid)) if cid.isdigit() else None
    if ch is None or getattr(ch, "guild", None) is None or ch.guild.id not in await run(db.allowed_guild_ids):
        flash(request, "Pick a channel first.")
        return redirect("/prompts")
    g = ch.guild
    await _remove_prompt_panel(g.id)
    try:
        ids = await _send_prompt_panel(ch)
    except (discord.Forbidden, discord.HTTPException):
        flash(request, f"The bot can't post in #{ch.name}. Give it Send Messages and Embed Links there.")
        return redirect("/prompts")
    await run(db.set_setting, f"ppanel_msg:{g.id}", f"{ch.id}|{','.join(ids)}")
    await run(db.set_setting, f"ppanel_ch:{g.id}", str(ch.id))
    flash(request, f"Prompt panel posted in #{ch.name}. Members tap a niche to get a prompt.")
    return redirect("/prompts")


async def prompts_panel_remove(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    for gid in await run(db.allowed_guild_ids):
        await _remove_prompt_panel(gid)
    flash(request, "Prompt panel removed.")
    return redirect("/prompts")


# ---------- lesson feedback (tutorials page) ----------
async def watch_feedback(request):
    """GET: this member's feedback for a lesson. POST (JSON): save it."""
    if not member_authed(request):
        return JSONResponse({"ok": False}, status_code=401)
    username = request.session.get("member")
    if await _member_access(username):
        return JSONResponse({"ok": False}, status_code=401)
    if request.method == "GET":
        vid = parse_int(request.query_params.get("vid"))
        f = await run(db.get_feedback, username, vid) if vid else None
        return JSONResponse({"ok": True, "rating": f.rating if f else 0, "comment": f.comment if f else ""},
                            headers={"Cache-Control": "no-store"})
    if not _same_site_json(request):
        return JSONResponse({"ok": False, "msg": "Bad request."}, status_code=400)
    if _too_many("fb:" + username, limit=30):
        return JSONResponse({"ok": False, "msg": "Slow down a little and try again."}, status_code=429)
    body = await request.body()
    if len(body) > 4000:
        return JSONResponse({"ok": False, "msg": "That's too long."}, status_code=413)
    try:
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return JSONResponse({"ok": False, "msg": "Bad request."}, status_code=400)
    vid = parse_int(data.get("vid"))
    v = next((x for x in await run(db.list_videos, True) if x.id == vid), None) if vid else None
    rating = data.get("rating")
    if v is None or rating not in (1, -1):
        return JSONResponse({"ok": False, "msg": "Pick Yes or Not really first."}, status_code=400)
    comment = data.get("comment")
    comment = " ".join(str(comment).split())[:500] if comment is not None else None
    await run(db.save_feedback, username, v.id, v.title, rating, comment)
    return JSONResponse({"ok": True, "msg": "Thanks! Your feedback was sent." if comment else "Thanks for rating this lesson."})


def lesson_feedback() -> dict:
    """For the Viewers page."""
    rows = db.list_feedback(60)
    return {"rows": [{"id": f.id, "user": f.username, "title": f.title, "rating": f.rating, "comment": f.comment,
                      "when": _ago(f.updated_at)} for f in rows],
            "counts": db.feedback_counts()}


templates.env.globals["lesson_feedback"] = lesson_feedback
templates.env.globals["feedback_counts"] = db.feedback_counts


async def feedback_delete(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    fid = parse_int(form.get("id"))
    if fid:
        await run(db.delete_feedback, fid)
    return redirect("/viewers")


# ---------- image generator for members (Pollinations, on the tutorials page) ----------
# Needs POLLINATIONS_KEY (a secret sk_ key from enter.pollinations.ai) in the environment.
# Images made with a key have no Pollinations logo.
IMG_MODELS = [("zimage", "Z-Image Turbo: sharp, realistic (recommended)"), ("flux", "Flux: fastest, uses the fewest credits"),
              ("klein", "FLUX.2 Klein: good detail"), ("gptimage", "GPT Image: best with text, uses more credits")]
_IMG_SIZES = {"9:16": [(1088, 1920), (768, 1344)], "16:9": [(1920, 1088), (1344, 768)]}   # what we ask for
_IMG_FINAL = {"9:16": (1080, 1920), "16:9": (1920, 1080)}                                 # what members get
_IMG_SAFE = "sexual"          # block adult content only; drama scenes (fights, crying) are fine
_IMG_BUSY: set = set()
_IMG_SEM = asyncio.Semaphore(3)


def _img_key() -> str:
    return os.getenv("POLLINATIONS_KEY", "").strip()


def _img_cfg() -> dict:
    model = db.get_setting("img_model", "zimage") or "zimage"
    try:
        limit = max(1, min(500, int(db.get_setting("img_limit", "15") or 15)))
    except ValueError:
        limit = 15
    return {"on": db.get_setting("img_on", "1") != "0", "model": model, "limit": limit, "key": bool(_img_key())}


def imagine_member(username: str) -> dict:
    """For the tutorials page: show the Create images tab? How many left today?"""
    c = _img_cfg()
    used = db.image_gens_since(username, 24) if username else 0
    return {"show": c["on"] and c["key"], "limit": c["limit"], "left": max(0, c["limit"] - used)}


def imagine_admin() -> dict:
    """For the Videos page."""
    c = _img_cfg()
    err, _, when = db.get_setting("img_err", "").partition("|")
    try:
        err_when = _ago(dt.datetime.fromtimestamp(float(when), dt.timezone.utc).replace(tzinfo=None)) if err else ""
    except ValueError:
        err_when = ""
    rows = [{"user": g.username, "prompt": g.prompt, "ratio": g.ratio, "ok": g.ok, "when": _ago(g.created_at)}
            for g in db.recent_image_gens(15)]
    return {**c, "models": IMG_MODELS, "stats": db.image_gen_stats(), "rows": rows, "err": err, "err_when": err_when}


templates.env.globals["imagine_member"] = imagine_member
templates.env.globals["imagine_admin"] = imagine_admin


def _img_finish(data: bytes, ratio: str) -> tuple:
    """Check the picture and make it exactly 1080x1920 or 1920x1080 (sharp JPEG).
    Returns (jpeg bytes, "") or (None, "bad" | "blank")."""
    try:
        from PIL import Image, ImageOps, ImageStat
        with Image.open(io.BytesIO(data)) as src:
            src.load()
            im = src.convert("RGB")
    except Exception:
        return None, "bad"
    if ImageStat.Stat(im.convert("L").resize((96, 168))).stddev[0] < 1.5:
        return None, "blank"          # one flat color (all black, all gray...): a blocked or failed image
    im = ImageOps.fit(im, _IMG_FINAL[ratio], Image.LANCZOS, centering=(0.5, 0.5))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=94, subsampling=0, optimize=True)
    return buf.getvalue(), ""


async def _img_problem(msg: str) -> None:
    """Remember the last problem so the owner sees it on the Videos page."""
    log.warning("Image generator: %s", msg)
    await run(db.set_setting, "img_err", f"{msg[:300]}|{time.time():.0f}")


async def _pollinations(prompt: str, w: int, h: int, model: str, seed: int) -> tuple:
    """(status, content_type, bytes or error text, extra info). status 0 = could not reach / timed out."""
    import aiohttp
    from urllib.parse import quote
    base = os.getenv("POLLINATIONS_URL", "https://gen.pollinations.ai").rstrip("/")
    params = {"model": model, "width": str(w), "height": str(h), "seed": str(seed), "safe": _IMG_SAFE}
    if model.startswith("gptimage"):
        params["quality"] = "high"
    timeout = aiohttp.ClientTimeout(total=float(os.getenv("POLLINATIONS_TIMEOUT", "150")))
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(f"{base}/image/{quote(prompt, safe='')}", params=params,
                             headers={"Authorization": f"Bearer {_img_key()}", "Accept": "image/*"}) as r:
                ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
                info = ", ".join(f"{k}: {v[:60]}" for k, v in r.headers.items()
                                 if k.lower().startswith("x-") and "cookie" not in k.lower())[:240]
                data, cap = b"", 20 * 1024 * 1024
                async for chunk in r.content.iter_chunked(256 * 1024):   # read the WHOLE picture, not just the first part
                    data += chunk
                    if len(data) > cap:
                        break
                if r.status == 200 and ctype.startswith("image/"):
                    return 200, ctype, data, info
                return r.status, ctype, data[:600].decode("utf-8", "replace"), info
    except asyncio.TimeoutError:
        return 0, "", "timeout", ""
    except aiohttp.ClientError as e:
        return 0, "", f"could not reach Pollinations ({type(e).__name__})", ""


async def watch_imagine(request):
    """POST JSON {prompt, ratio}: make one image for a logged-in member. Returns the image itself."""
    if not member_authed(request):
        return JSONResponse({"ok": False}, status_code=401)
    username = request.session.get("member")
    check = globals().get("_member_access")
    if check and await check(username):
        return JSONResponse({"ok": False}, status_code=401)
    if not _same_site_json(request):
        return JSONResponse({"ok": False, "msg": "Bad request."}, status_code=400)
    cfg = await run(_img_cfg)
    if not (cfg["on"] and cfg["key"]):
        return JSONResponse({"ok": False, "msg": "The image generator is turned off right now."}, status_code=503)
    body = await request.body()
    try:
        data = json.loads(body) if len(body) < 12000 else None
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return JSONResponse({"ok": False, "msg": "Bad request."}, status_code=400)
    prompt = " ".join("".join(ch for ch in str(data.get("prompt", "")) if ch.isprintable() or ch == " ").split())[:2000]
    ratio = data.get("ratio") if data.get("ratio") in _IMG_SIZES else "9:16"
    if len(prompt) < 3:
        return JSONResponse({"ok": False, "msg": "Describe the image you want first."}, status_code=400)
    if username in _IMG_BUSY:
        return JSONResponse({"ok": False, "msg": "Your last image is still being made. Wait for it to finish."}, status_code=429)
    if _too_many("img:" + username, limit=8, window=120):
        return JSONResponse({"ok": False, "msg": "That's a lot of images at once. Wait a minute and try again."}, status_code=429)
    used = await run(db.image_gens_since, username, 24)
    if used >= cfg["limit"]:
        return JSONResponse({"ok": False, "msg": f"You've made {cfg['limit']} images today, the most allowed. "
                                                 "Come back tomorrow for more.", "left": 0}, status_code=429)
    _IMG_BUSY.add(username)
    try:
        async with _IMG_SEM:
            seed = secrets.randbelow(2_000_000_000) + 1
            sizes = _IMG_SIZES[ratio]
            for i, (w, h) in enumerate(sizes):
                status, ctype, out, info = await _pollinations(prompt, w, h, cfg["model"], seed)
                low = str(out).lower() if status != 200 else ""
                if status == 400 and i + 1 < len(sizes) and any(
                        k in low for k in ("size", "width", "height", "dimension", "resolution", "pixel")):
                    continue                      # this model can't do HD: try the standard size
                break
    finally:
        _IMG_BUSY.discard(username)
    final, why = None, "bad"
    if status == 200 and isinstance(out, (bytes, bytearray)) and len(out) <= 20 * 1024 * 1024:
        final, why = await run(_img_finish, bytes(out), ratio)
    ok = final is not None
    await run(db.log_image_gen, username, prompt, ratio, cfg["model"], ok)
    if ok:
        if await run(db.get_setting, "img_err", ""):
            await run(db.set_setting, "img_err", "")
        fw, fh = _IMG_FINAL[ratio]
        return Response(content=final, media_type="image/jpeg", headers={
            "Cache-Control": "no-store", "X-Seed": str(seed), "X-Size": f"{fw}x{fh}",
            "X-Left": str(max(0, cfg["limit"] - used - 1))})
    if status == 200 and why == "blank":
        await _img_problem(f"Pollinations sent back a blank image (model {cfg['model']}, {w}x{h}). {info}".strip())
        msg = ("The image came back blank, so it wasn't counted. This can happen when the prompt asks to copy "
               "a real person's face or photo. Describe the people instead, then try again.")
    elif status == 200:
        await _img_problem(f"Pollinations sent something that isn't a picture ({ctype or 'unknown type'}).")
        msg = "The image service had a problem. Try again in a moment."
    elif status in (401, 403):
        await _img_problem(f"Pollinations refused the key (HTTP {status}). Check POLLINATIONS_KEY on Render.")
        msg = "The image generator isn't working right now. The owner has been told."
    elif status == 402:
        await _img_problem("Out of Pollinations credits (pollen). They refill on Pollinations' schedule, or top up at enter.pollinations.ai.")
        msg = "The image generator is out of credits for now. Try again later."
    elif status in (429, 503):
        msg = "The image service is busy. Try again in a minute."
    elif status == 400:
        msg = "That prompt couldn't be made into an image. Try describing it differently."
    elif status == 0 and out == "timeout":
        msg = "That took too long. Try again."
    else:
        await _img_problem(f"Pollinations error (HTTP {status}): {str(out)[:160]}")
        msg = "The image service had a problem. Try again in a moment."
    return JSONResponse({"ok": False, "msg": msg}, status_code=502)


async def imagine_settings_save(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    model = str(form.get("model", "zimage"))
    if model not in dict(IMG_MODELS):
        model = "zimage"
    limit = parse_int(form.get("limit")) or 15
    await run(db.set_setting, "img_on", "1" if form.get("on") else "0")
    await run(db.set_setting, "img_model", model)
    await run(db.set_setting, "img_limit", str(max(1, min(500, limit))))
    flash(request, "Image generator saved." if _img_key() else
          "Saved. Add POLLINATIONS_KEY on Render so members can use it.")
    return redirect("/videos#imagine")


async def member_delete(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    mid = parse_int(form.get("id"))
    if mid:
        await run(db.delete_web_user, mid)
        _ACCESS_CACHE.clear()
        flash(request, "Member account removed.")
    return redirect("/videos")


async def health(request):
    return PlainTextResponse("ok")


def _env_server_ids() -> set[int]:
    raw = os.getenv("ALLOWED_GUILD_IDS", "") + "," + os.getenv("APPROVED_SERVER_IDS", "")
    return {int(x) for x in (p.strip() for p in raw.split(",")) if x.isdigit() and 15 <= len(x) <= 20}


@asynccontextmanager
async def lifespan(app):
    await run(db.init)
    await run(db.seed_niches, promptlib.PROMPT_NICHES)
    for gid in _env_server_ids():          # servers listed in .env are always approved
        await run(db.add_server, gid, "")
    await run(upload_reports.load_settings_sync)
    saved = await run(db.get_setting, "image_enabled", "")
    if saved:
        discord_bot.IMAGE_STATE["enabled"] = saved == "1"
    task = None
    if discord_bot.TOKEN:
        task = asyncio.create_task(discord_bot.start_with_backoff(discord_bot.TOKEN))
        task.add_done_callback(lambda t: t.cancelled() or (t.exception() and log.error("Bot stopped: %r", t.exception())))
    else:
        log.error("DISCORD_TOKEN is not set. The dashboard works but the bot is offline.")
    yield
    await discord_bot.bot.close()


app = Starlette(
    routes=[
        Route("/", index),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/usage", usage),
        Route("/usage.csv", usage_csv),
        Route("/servers/add", servers_add, methods=["POST"]),
        Route("/servers/remove", servers_remove, methods=["POST"]),
        Route("/members/set", members_set, methods=["POST"]),
        Route("/image/toggle", image_toggle, methods=["POST"]),
        Route("/uploads", uploads),
        Route("/security", security_page_v2),
        Route("/security/gate", security_gate, methods=["POST"]),
        Route("/security/log", security_log, methods=["POST"]),
        Route("/security/approve-web", security_approve_web, methods=["POST"]),
        Route("/security/panel", security_panel_post, methods=["POST"]),
        Route("/security/panel/remove", security_panel_remove, methods=["POST"]),
        Route("/security/decide", security_decide, methods=["POST"]),
        Route("/security/verify", security_verify, methods=["POST"]),
        Route("/security/resend", security_resend, methods=["POST"]),
        Route("/members", members_page),
        Route("/member", member_history),
        Route("/member/warn", member_warn, methods=["POST"]),
        Route("/member/unwarn", member_unwarn, methods=["POST"]),
        Route("/member/export", member_export),
        Route("/prompts", prompts_page),
        Route("/prompts/add", prompt_add, methods=["POST"]),
        Route("/prompts/edit", prompt_edit, methods=["POST"]),
        Route("/prompts/toggle", prompt_toggle, methods=["POST"]),
        Route("/prompts/delete", prompt_delete, methods=["POST"]),
        Route("/prompts/register", reg_decide, methods=["POST"]),
        Route("/prompts/niche/add", niche_add, methods=["POST"]),
        Route("/prompts/niche/edit", niche_edit, methods=["POST"]),
        Route("/prompts/niche/toggle", niche_toggle, methods=["POST"]),
        Route("/prompts/niche/delete", niche_delete, methods=["POST"]),
        Route("/prompts/panel", prompts_panel_post, methods=["POST"]),
        Route("/prompts/panel/remove", prompts_panel_remove, methods=["POST"]),
        Route("/daily", daily_page),
        Route("/daily/save", daily_save, methods=["POST"]),
        Route("/daily/send", daily_send, methods=["POST"]),
        Route("/api/prompts/live", prompts_live),
        Route("/api/overview/live", overview_live),
        Route("/api/security/live", security_live),
        Route("/api/uploads/live", uploads_live),
        Route("/uploads/channels", uploads_channels, methods=["POST"]),
        Route("/uploads/settings", uploads_settings, methods=["POST"]),
        Route("/uploads/send", uploads_send, methods=["POST"]),
        Route("/health", health),
        Route("/manifest-admin.webmanifest", manifest_admin),
        Route("/manifest.webmanifest", manifest_site),
        Mount("/static", app=StaticFiles(directory=str(Path(__file__).parent / "static")), name="static"),
        Route("/api/public/send-code", send_code_api, methods=["POST"]),
        Route("/api/public/stats", public_stats_api),
        Route("/site", site_page),
        Route("/site/contact", site_contact_save, methods=["POST"]),
        Route("/site/save", site_save, methods=["POST"]),
        Route("/join", join_page),
        Route("/join", join_submit_1dev, methods=["POST"]),
        Route("/members/login", member_login_page),
        Route("/members/login", member_login_submit_1dev, methods=["POST"]),
        Route("/members/logout", member_logout, methods=["POST"]),
        Route("/watch", watch_page_1dev),
        Route("/watch/go/{token}", magic_login_page),
        Route("/watch/go/{token}", magic_login_submit, methods=["POST"]),
        Route("/watch/feedback", watch_feedback, methods=["GET", "POST"]),
        Route("/viewers/feedback/delete", feedback_delete, methods=["POST"]),
        Route("/watch/imagine", watch_imagine, methods=["POST"]),
        Route("/videos/imagine", imagine_settings_save, methods=["POST"]),
        Route("/watch/ping", watch_ping_1dev, methods=["POST"]),
        Route("/viewers", viewers_page),
        Route("/api/viewers/live", viewers_live),
        Route("/videos", videos_page),
        Route("/videos/add", video_add_and_announce, methods=["POST"]),
        Route("/videos/edit", video_edit, methods=["POST"]),
        Route("/videos/toggle", video_toggle, methods=["POST"]),
        Route("/videos/delete", video_delete, methods=["POST"]),
        Route("/videos/watermark", video_wm_save, methods=["POST"]),
        Route("/videos/strict", video_strict_toggle, methods=["POST"]),
        Route("/videos/resource/add", resource_add_and_announce, methods=["POST"]),
        Route("/videos/resource/edit", resource_edit, methods=["POST"]),
        Route("/videos/resource/toggle", resource_toggle, methods=["POST"]),
        Route("/videos/resource/delete", resource_delete, methods=["POST"]),
        Route("/videos/member-device-reset", member_device_reset, methods=["POST"]),
        Route("/videos/one-device", one_device_toggle, methods=["POST"]),
        Route("/videos/member-delete", member_delete, methods=["POST"]),
        Route("/v/r/{token}", reg_verify_page),
        Route("/v/r/{token}", reg_verify_submit, methods=["POST"]),
        Route("/v/{token}", verify_page),
        Route("/v/{token}", verify_submit, methods=["POST"]),
        Route("/security/phone", security_phone, methods=["POST"]),
    ],
    middleware=[
        Middleware(SecurityHeaders),
        Middleware(AdminPath),
        Middleware(
            SessionMiddleware, secret_key=SECRET_KEY, same_site="lax",
            https_only=bool(os.getenv("RENDER")), max_age=60 * 60 * 12,
        )
    ],
    lifespan=lifespan,
)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
