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
from starlette.routing import Route
from starlette.templating import Jinja2Templates

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("panel")

import db  # noqa: E402
import discord_bot  # noqa: E402
import upload_reports  # noqa: E402  (adds upload-time reports + commands to the bot)
import security  # noqa: E402  (join gate: new members need approval)
import prompt_commands  # noqa: E402,F401  (adds /ghibli, /cooking, ... commands)
import prompts as promptlib  # noqa: E402

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
_PUBLIC = ("/v/", "/health", "/api/public/", "/static/")
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
           "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
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
    flash(request, "Prompt added.")
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
        flash(request, "Prompt updated.")
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
    flash(request, f"Niche '{label}' added. It now shows in /prompt.")
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
    return redirect(f"/prompts?n={key}")


async def niche_delete(request):
    form, resp = await guarded_post(request)
    if resp:
        return resp
    key = str(form.get("key", ""))
    if await run(db.get_niche, key) is not None:
        await run(db.delete_niche, key)
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
    }


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
        Route("/api/prompts/live", prompts_live),
        Route("/api/overview/live", overview_live),
        Route("/api/security/live", security_live),
        Route("/api/uploads/live", uploads_live),
        Route("/uploads/channels", uploads_channels, methods=["POST"]),
        Route("/uploads/settings", uploads_settings, methods=["POST"]),
        Route("/uploads/send", uploads_send, methods=["POST"]),
        Route("/health", health),
        Route("/api/public/stats", public_stats_api),
        Route("/site", site_page),
        Route("/site/save", site_save, methods=["POST"]),
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
