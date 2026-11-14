"""Admin dashboard + Discord bot in ONE process (so one Render service runs both)."""
import asyncio
import csv
import datetime as dt
import hmac
import io
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route
from starlette.templating import Jinja2Templates

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("panel")

import db  # noqa: E402
import discord_bot  # noqa: E402
import upload_reports  # noqa: E402  (adds upload-time reports + commands to the bot)
import security  # noqa: E402  (join gate: new members need approval)

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
        "image_enabled": discord_bot.IMAGE_STATE["enabled"],
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
        asyncio.create_task(upload_reports.send_upload_reports(include_schedule=(what == "all")))
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
    if jid is None or action not in ("approve", "deny", "ban"):
        flash(request, "Invalid request.")
        return redirect("/security")
    if not discord_bot.bot.is_ready():
        flash(request, "The bot is not connected to Discord yet. Try again in a moment.")
        return redirect("/security")
    flash(request, await security.decide(jid, action))
    return redirect("/security")

# ---------- security v2: codes + live ----------
async def _security_lists(request) -> dict:
    b = discord_bot.bot
    data = await run(db.overview)
    names = {}
    for s in data["servers"]:
        g = b.get_guild(s.guild_id) if b.is_ready() else None
        names[s.guild_id] = (g.name if g else s.name) or "Unnamed server"
    pending = await run(db.list_joins, None, True, 200)
    history = [j for j in await run(db.list_joins, None, False, 80) if j.status != "pending"][:50]
    codes = {j.id: await run(security.code_status_sync, j.id) for j in pending}
    return {"csrf": csrf_token(request), "pending": pending, "history": history, "codes": codes,
            "names": names, "multi": len(names) > 1, "tz": TZ_LABEL}


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

async def health(request):
    return PlainTextResponse("ok")


def _env_server_ids() -> set[int]:
    raw = os.getenv("ALLOWED_GUILD_IDS", "") + "," + os.getenv("APPROVED_SERVER_IDS", "")
    return {int(x) for x in (p.strip() for p in raw.split(",")) if x.isdigit() and 15 <= len(x) <= 20}


@asynccontextmanager
async def lifespan(app):
    await run(db.init)
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
        Route("/api/security/live", security_live),
        Route("/api/uploads/live", uploads_live),
        Route("/uploads/channels", uploads_channels, methods=["POST"]),
        Route("/uploads/settings", uploads_settings, methods=["POST"]),
        Route("/uploads/send", uploads_send, methods=["POST"]),
        Route("/health", health),
    ],
    middleware=[
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
