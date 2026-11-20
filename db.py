"""Database layer. Uses SQLite locally and Postgres when DATABASE_URL is set."""
import datetime as dt
import os
import time

from dotenv import load_dotenv
from sqlalchemy import (
    BigInteger, DateTime, Float, Integer, String, Text,
    case, create_engine, delete, func, select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

load_dotenv()


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


_url = os.getenv("DATABASE_URL", "sqlite:///data.db")
if _url.startswith("postgres://"):
    _url = "postgresql://" + _url[len("postgres://"):]
if _url.startswith("postgresql://"):
    _url = "postgresql+psycopg2://" + _url[len("postgresql://"):]
    
IS_SQLITE = _url.startswith("sqlite")

engine = create_engine(
    _url,
    pool_pre_ping=True,
    pool_recycle=300,
    **({"connect_args": {"check_same_thread": False}} if IS_SQLITE else {}),
)
Session = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Server(Base):
    __tablename__ = "servers"
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(200), default="")
    added_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Member(Base):
    __tablename__ = "members"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|denied
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    requested_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class UsageLog(Base):
    __tablename__ = "usage_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str] = mapped_column(String(200), default="")
    guild_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    guild_name: Mapped[str] = mapped_column(String(200), default="")
    command: Mapped[str] = mapped_column(String(50), default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok|error|denied
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    prompt: Mapped[str] = mapped_column(Text, default="")
    interval_s: Mapped[int] = mapped_column(Integer, default=600)
    next_run: Mapped[float] = mapped_column(Float, default=0.0)
    created_by: Mapped[int] = mapped_column(BigInteger, default=0)


class JoinRequest(Base):
    """A member who joined an approved server (join gate log + approvals)."""
    __tablename__ = "join_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str] = mapped_column(String(200), default="")
    avatar_url: Mapped[str] = mapped_column(String(500), default="")
    account_created: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    joined_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|denied|banned|left|allowed
    flags: Mapped[str] = mapped_column(String(300), default="")
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class Registration(Base):
    """A member who registered to use prompt commands. Gets a sequential number."""
    __tablename__ = "registrations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str] = mapped_column(String(200), default="")
    number: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|denied
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class PromptUse(Base):
    """One prompt command use, for counting per niche."""
    __tablename__ = "prompt_uses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    niche: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class WebUser(Base):
    """A member's website account (self-registered with a Discord code). For the videos page."""
    __tablename__ = "web_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200), default="")
    discord_user_id: Mapped[int] = mapped_column(BigInteger, index=True, default=0)
    discord_name: Mapped[str] = mapped_column(String(200), default="")
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Video(Base):
    """A tutorial video (Google Drive). Managed on the dashboard, no redeploy."""
    __tablename__ = "videos"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    drive_id: Mapped[str] = mapped_column(String(100), default="")
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Resource(Base):
    """A tool/link shown in the Resources pop-up on the tutorials page (e.g. CapCut)."""
    __tablename__ = "resources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(80), default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    description: Mapped[str] = mapped_column(String(300), default="")
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class VideoView(Base):
    """One viewing session of a tutorial by a member (kept alive by pings from the page)."""
    __tablename__ = "video_views"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(40), index=True)
    discord_name: Mapped[str] = mapped_column(String(200), default="")
    video_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    started_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    seconds: Mapped[int] = mapped_column(Integer, default=0)


class MemberDevice(Base):
    """The one device a website member account is locked to (anti account-sharing)."""
    __tablename__ = "member_devices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, default=0)
    token_hash: Mapped[str] = mapped_column(String(64), default="")
    label: Mapped[str] = mapped_column(String(80), default="")
    linked_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    blocked: Mapped[int] = mapped_column(Integer, default=0)
    blocked_label: Mapped[str] = mapped_column(String(80), default="")
    blocked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class LessonFeedback(Base):
    """A member's feedback on one tutorial lesson (one per member per lesson)."""
    __tablename__ = "lesson_feedback"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(40), index=True)
    video_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    rating: Mapped[int] = mapped_column(Integer, default=0)
    comment: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ImageGen(Base):
    """One image a member made with the tutorials page image generator."""
    __tablename__ = "image_gens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(40), index=True)
    prompt: Mapped[str] = mapped_column(String(600), default="")
    ratio: Mapped[str] = mapped_column(String(8), default="")
    model: Mapped[str] = mapped_column(String(40), default="")
    ok: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Niche(Base):
    """A prompt niche (slash dropdown option). Created/edited on the dashboard, no restart."""
    __tablename__ = "niches"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    label: Mapped[str] = mapped_column(String(60), default="")
    emoji: Mapped[str] = mapped_column(String(16), default="")
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Prompt(Base):
    """A prompt for a niche slash command (e.g. /ghibli). Edited on the dashboard."""
    __tablename__ = "prompts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    niche: Mapped[str] = mapped_column(String(40), index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Warning(Base):
    """A warning an admin gave a member, with a timestamp and reason."""
    __tablename__ = "warnings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str] = mapped_column(String(200), default="")
    reason: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    active: Mapped[bool] = mapped_column(default=True)


class DeviceCheck(Base):
    """One device check from the verification web page (hashed, no raw IP)."""
    __tablename__ = "device_checks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str] = mapped_column(String(200), default="")
    join_id: Mapped[int] = mapped_column(Integer, index=True)
    cookie_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    fp_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    ip_hash: Mapped[str] = mapped_column(String(64), index=True, default="")
    device: Mapped[str] = mapped_column(String(200), default="")
    tz: Mapped[str] = mapped_column(String(64), default="")
    screen: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class UploadChannel(Base):
    """Where upload-time / exchange-rate reports go, per approved server."""
    __tablename__ = "upload_channels"
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    default_ch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    yt_ch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fb_ch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    schedule_ch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fx_ch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class Setting(Base):
    """Small key/value store for bot settings (pause, intervals...)."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), default="")


class DenyReason(Base):
    """Why a join request was denied, in full (no length limit), and whether the member got it by DM."""
    __tablename__ = "deny_reasons"
    join_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    dm: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


def init() -> None:
    Base.metadata.create_all(engine)


# ---------- servers ----------
def allowed_guild_ids() -> set[int]:
    with Session() as s:
        return set(s.scalars(select(Server.guild_id)))


def add_server(guild_id: int, name: str = "") -> None:
    with Session.begin() as s:
        if s.get(Server, guild_id) is None:
            s.add(Server(guild_id=guild_id, name=name))


def remove_server(guild_id: int) -> None:
    with Session.begin() as s:
        s.execute(delete(Server).where(Server.guild_id == guild_id))
        s.execute(delete(Job).where(Job.guild_id == guild_id))
        s.execute(delete(UploadChannel).where(UploadChannel.guild_id == guild_id))


def set_guild_name(guild_id: int, name: str) -> None:
    with Session.begin() as s:
        sv = s.get(Server, guild_id)
        if sv:
            sv.name = name


# ---------- members ----------
def member_status(user_id: int) -> str | None:
    with Session() as s:
        m = s.get(Member, user_id)
        return m.status if m else None


def request_access(user_id: int, username: str, guild_id: int | None) -> str:
    with Session.begin() as s:
        m = s.get(Member, user_id)
        if m is None:
            s.add(Member(user_id=user_id, username=username, status="pending", guild_id=guild_id))
            return "pending"
        m.username = username
        return m.status


def set_member(user_id: int, status: str, username: str | None = None) -> None:
    with Session.begin() as s:
        m = s.get(Member, user_id)
        if status == "remove":
            if m:
                s.delete(m)
            return
        if m is None:
            m = Member(user_id=user_id, username=username or "")
            s.add(m)
        m.status = status
        m.decided_at = None if status == "pending" else utcnow()
        if username:
            m.username = username


# ---------- usage ----------
def log_usage(user_id, username, guild_id, guild_name, command, prompt, status, duration_ms, error=""):
    with Session.begin() as s:
        s.add(UsageLog(
            user_id=user_id, username=username, guild_id=guild_id, guild_name=guild_name,
            command=command, prompt=(prompt or "")[:500], status=status,
            duration_ms=int(duration_ms), error=(error or "")[:300],
        ))
        m = s.get(Member, user_id)
        if m is not None and username:
            m.username = username


def count_recent_ok(user_id: int, hours: int = 24) -> int:
    since = utcnow() - dt.timedelta(hours=hours)
    with Session() as s:
        return s.scalar(
            select(func.count()).select_from(UsageLog).where(
                UsageLog.user_id == user_id, UsageLog.status == "ok", UsageLog.ts >= since
            )
        ) or 0


def usage_summary(since: dt.datetime | None) -> list[dict]:
    ok = func.sum(case((UsageLog.status == "ok", 1), else_=0))
    err = func.sum(case((UsageLog.status == "error", 1), else_=0))
    den = func.sum(case((UsageLog.status == "denied", 1), else_=0))
    avg_ok = func.avg(case((UsageLog.status == "ok", UsageLog.duration_ms)))
    q = (
        select(
            UsageLog.user_id,
            func.max(UsageLog.username).label("username"),
            func.count().label("total"),
            ok.label("ok"), err.label("errors"), den.label("denied"),
            avg_ok.label("avg_ms"),
            func.min(UsageLog.ts).label("first"),
            func.max(UsageLog.ts).label("last"),
        )
        .group_by(UsageLog.user_id)
        .order_by(func.count().desc())
    )
    if since:
        q = q.where(UsageLog.ts >= since)
    with Session() as s:
        return [dict(r._mapping) for r in s.execute(q)]


def recent_logs(since: dt.datetime | None, user_id: int | None = None, limit: int = 200) -> list[UsageLog]:
    q = select(UsageLog).order_by(UsageLog.ts.desc()).limit(limit)
    if since:
        q = q.where(UsageLog.ts >= since)
    if user_id:
        q = q.where(UsageLog.user_id == user_id)
    with Session() as s:
        return list(s.scalars(q))


# ---------- automations ----------
def upsert_job(guild_id, channel_id, prompt, interval_s, created_by) -> None:
    with Session.begin() as s:
        j = s.scalar(select(Job).where(Job.guild_id == guild_id, Job.channel_id == channel_id))
        if j is None:
            j = Job(guild_id=guild_id, channel_id=channel_id)
            s.add(j)
        j.prompt, j.interval_s, j.created_by = prompt, interval_s, created_by
        j.next_run = time.time() + interval_s


def remove_job(guild_id: int, channel_id: int) -> bool:
    with Session.begin() as s:
        res = s.execute(delete(Job).where(Job.guild_id == guild_id, Job.channel_id == channel_id))
        return res.rowcount > 0


def list_jobs(guild_id: int) -> list[Job]:
    with Session() as s:
        return list(s.scalars(select(Job).where(Job.guild_id == guild_id)))


def due_jobs(now: float) -> list[Job]:
    with Session() as s:
        return list(s.scalars(select(Job).where(Job.next_run <= now)))


def bump_job(job_id: int, next_run: float) -> None:
    with Session.begin() as s:
        j = s.get(Job, job_id)
        if j:
            j.next_run = next_run


# ---------- device checks ----------
def add_device_check(**kw) -> None:
    with Session.begin() as s:
        s.add(DeviceCheck(**kw))


def latest_device(join_id: int):
    with Session() as s:
        return s.scalars(select(DeviceCheck).where(DeviceCheck.join_id == join_id)
                         .order_by(DeviceCheck.created_at.desc()).limit(1)).first()


def device_matches(guild_id: int, user_id: int, cookie_h: str, fp_h: str, ip_h: str) -> list[dict]:
    """Other accounts in this server seen on the same browser, device or network."""
    with Session() as s:
        rows = s.scalars(select(DeviceCheck).where(
            DeviceCheck.guild_id == guild_id, DeviceCheck.user_id != user_id)).all()
    found = {}
    for r in rows:
        if cookie_h and r.cookie_hash == cookie_h:
            kind, rank = "same browser", 3
        elif fp_h and ip_h and r.fp_hash == fp_h and r.ip_hash == ip_h:
            kind, rank = "same device", 2
        elif ip_h and r.ip_hash == ip_h:
            kind, rank = "same network", 1
        else:
            continue
        if r.user_id not in found or found[r.user_id]["rank"] < rank:
            found[r.user_id] = {"user_id": r.user_id, "username": r.username, "kind": kind, "rank": rank}
    return sorted(found.values(), key=lambda x: -x["rank"])


def set_join_flags(join_id: int, flags: str) -> None:
    with Session.begin() as s:
        row = s.get(JoinRequest, join_id)
        if row:
            row.flags = (flags or "")[:300]


# ---------- registrations ----------
def get_registration(guild_id: int, user_id: int):
    with Session() as s:
        return s.scalars(select(Registration).where(
            Registration.guild_id == guild_id, Registration.user_id == user_id)).first()


def register_user(guild_id: int, user_id: int, username: str) -> tuple:
    """Return (registration, created). Assigns the next number on first register."""
    with Session.begin() as s:
        row = s.scalars(select(Registration).where(
            Registration.guild_id == guild_id, Registration.user_id == user_id)).first()
        if row is not None:
            if username:
                row.username = username[:200]
            s.expunge(row)
            return row, False
        nxt = (s.scalar(select(func.max(Registration.number)).where(Registration.guild_id == guild_id)) or 0) + 1
        row = Registration(guild_id=guild_id, user_id=user_id, username=(username or "")[:200], number=nxt)
        s.add(row)
        s.flush()
        s.expunge(row)
        return row, True


def get_registration_by_id(reg_id: int):
    with Session() as s:
        r = s.get(Registration, reg_id)
        if r is not None:
            s.expunge(r)
        return r


def set_registration_status(reg_id: int, status: str) -> None:
    with Session.begin() as s:
        row = s.get(Registration, reg_id)
        if row:
            row.status = status
            row.decided_at = utcnow()


def list_registrations(guild_id: int | None = None, status: str | None = None, limit: int = 500) -> list:
    with Session() as s:
        q = select(Registration)
        if guild_id is not None:
            q = q.where(Registration.guild_id == guild_id)
        if status is not None:
            q = q.where(Registration.status == status)
        return list(s.scalars(q.order_by(Registration.number)))


def is_registered_approved(guild_id: int, user_id: int) -> bool:
    with Session() as s:
        return s.scalar(select(func.count()).select_from(Registration).where(
            Registration.guild_id == guild_id, Registration.user_id == user_id,
            Registration.status == "approved")) > 0


# ---------- prompt usage ----------
def log_prompt_use(guild_id: int, user_id: int, niche: str) -> None:
    with Session.begin() as s:
        s.add(PromptUse(guild_id=guild_id, user_id=user_id, niche=niche))


def prompt_use_counts() -> dict:
    with Session() as s:
        rows = s.execute(select(PromptUse.niche, func.count()).group_by(PromptUse.niche)).all()
    return {n: c for n, c in rows}


def prompt_use_total() -> int:
    with Session() as s:
        return s.scalar(select(func.count()).select_from(PromptUse)) or 0


# ---------- web users (members) + videos ----------
def create_web_user(username: str, password_hash: str, discord_user_id: int, discord_name: str) -> bool:
    with Session.begin() as s:
        exists = s.scalar(select(func.count()).select_from(WebUser).where(WebUser.username == username))
        if exists:
            return False
        s.add(WebUser(username=username[:40], password_hash=password_hash,
                      discord_user_id=discord_user_id, discord_name=(discord_name or "")[:200]))
        return True


def get_web_user(username: str):
    with Session() as s:
        u = s.scalars(select(WebUser).where(WebUser.username == username)).first()
        if u is not None:
            s.expunge(u)
        return u


def web_user_exists_for_discord(discord_user_id: int) -> bool:
    with Session() as s:
        return (s.scalar(select(func.count()).select_from(WebUser)
                .where(WebUser.discord_user_id == discord_user_id)) or 0) > 0


def list_web_users(limit: int = 500) -> list:
    with Session() as s:
        return list(s.scalars(select(WebUser).order_by(WebUser.created_at.desc()).limit(limit)))


def delete_web_user(uid: int) -> None:
    with Session.begin() as s:
        u = s.get(WebUser, uid)
        if u:
            s.delete(u)


def list_videos(active_only: bool = False) -> list:
    with Session() as s:
        q = select(Video)
        if active_only:
            q = q.where(Video.active.is_(True))
        return list(s.scalars(q.order_by(Video.created_at.desc())))


def add_video(title: str, description: str, drive_id: str) -> int:
    with Session.begin() as s:
        v = Video(title=title[:200], description=(description or "")[:2000], drive_id=drive_id[:100])
        s.add(v)
        s.flush()
        return v.id


def update_video(vid: int, title: str, description: str, drive_id: str) -> None:
    with Session.begin() as s:
        v = s.get(Video, vid)
        if v:
            v.title = title[:200]
            v.description = (description or "")[:2000]
            v.drive_id = drive_id[:100]


def toggle_video(vid: int) -> None:
    with Session.begin() as s:
        v = s.get(Video, vid)
        if v:
            v.active = not v.active


def delete_video(vid: int) -> None:
    with Session.begin() as s:
        v = s.get(Video, vid)
        if v:
            s.delete(v)


# ---------- resources (tutorials page pop-up) ----------
def list_resources(active_only: bool = False) -> list:
    with Session() as s:
        q = select(Resource)
        if active_only:
            q = q.where(Resource.active.is_(True))
        return list(s.scalars(q.order_by(Resource.created_at)))


def add_resource(name: str, url: str, description: str) -> int:
    with Session.begin() as s:
        r = Resource(name=name[:80], url=url[:500], description=(description or "")[:300])
        s.add(r)
        s.flush()
        return r.id


def update_resource(rid: int, name: str, url: str, description: str) -> None:
    with Session.begin() as s:
        r = s.get(Resource, rid)
        if r:
            r.name, r.url, r.description = name[:80], url[:500], (description or "")[:300]


def toggle_resource(rid: int) -> None:
    with Session.begin() as s:
        r = s.get(Resource, rid)
        if r:
            r.active = not r.active


def delete_resource(rid: int) -> None:
    with Session.begin() as s:
        r = s.get(Resource, rid)
        if r:
            s.delete(r)


# ---------- tutorial viewing (who is watching) ----------
def record_view_ping(username: str, discord_name: str, video_id: int, title: str, gap: int = 90) -> None:
    """Extend the member's open session for this video, or start a new one."""
    now = utcnow()
    with Session.begin() as s:
        row = s.scalars(select(VideoView).where(
            VideoView.username == username, VideoView.video_id == video_id,
            VideoView.last_seen >= now - dt.timedelta(seconds=gap))
            .order_by(VideoView.last_seen.desc()).limit(1)).first()
        if row is None:
            s.add(VideoView(username=username[:40], discord_name=(discord_name or "")[:200],
                            video_id=video_id, title=(title or "")[:200], started_at=now, last_seen=now))
            return
        row.seconds = (row.seconds or 0) + min(30, max(0, int((now - row.last_seen).total_seconds())))
        row.last_seen = now


def watching_now(window: int = 60) -> list:
    """Sessions that pinged within the last `window` seconds (one row per member)."""
    since = utcnow() - dt.timedelta(seconds=window)
    with Session() as s:
        rows = list(s.scalars(select(VideoView).where(VideoView.last_seen >= since)
                              .order_by(VideoView.last_seen.desc())))
    seen, out = set(), []
    for r in rows:
        if r.username not in seen:
            seen.add(r.username)
            out.append(r)
    return out


def recent_views(limit: int = 15) -> list:
    with Session() as s:
        return list(s.scalars(select(VideoView).order_by(VideoView.last_seen.desc()).limit(limit)))


def view_stats() -> dict:
    """Per-video session counts and unique viewers, plus totals."""
    with Session() as s:
        per = s.execute(select(VideoView.video_id, func.count(), func.count(func.distinct(VideoView.username)))
                        .group_by(VideoView.video_id)).all()
        total_seconds = s.scalar(select(func.coalesce(func.sum(VideoView.seconds), 0))) or 0
        viewers = s.scalar(select(func.count(func.distinct(VideoView.username)))) or 0
    return {"per_video": {v: {"views": c, "viewers": u} for v, c, u in per},
            "views": sum(c for _, c, _ in per), "viewers": viewers, "minutes": int(total_seconds // 60)}


# ---------- one device per member account ----------
def device_check(username: str, user_id: int, token_hash: str, label: str, mode: str,
                 user_created=None) -> str:
    """mode 'login': link this device if the account has none.  mode 'watch': link only if never linked.
    Returns 'ok' (same device), 'bound' (just linked), 'other' (locked to another device), 'reset' (owner reset it)."""
    import hmac as _hmac
    now = utcnow()
    with Session.begin() as s:
        d = s.scalars(select(MemberDevice).where(MemberDevice.username == username)).first()
        if d is not None and (d.user_id != user_id or (user_created and d.linked_at < user_created)):
            # leftover from a deleted account with the same name
            s.delete(d)
            s.flush()
            d = None
        if d is None or not d.token_hash:
            if d is not None and mode != "login":
                return "reset"
            if d is None:
                d = MemberDevice(username=username, user_id=user_id)
                s.add(d)
            d.token_hash, d.label, d.linked_at, d.last_seen = token_hash, (label or "")[:80], now, now
            return "bound"
        if _hmac.compare_digest(d.token_hash, token_hash):
            if (now - d.last_seen).total_seconds() > 60:
                d.last_seen = now
            return "ok"
        d.blocked = (d.blocked or 0) + 1
        d.blocked_label, d.blocked_at = (label or "")[:80], now
        return "other"


def device_reset(username: str) -> None:
    with Session.begin() as s:
        d = s.scalars(select(MemberDevice).where(MemberDevice.username == username)).first()
        if d is not None:
            d.token_hash, d.label, d.blocked, d.blocked_label, d.blocked_at = "", "", 0, "", None


def device_map() -> dict:
    with Session() as s:
        return {d.username: {"user_id": d.user_id, "linked": bool(d.token_hash), "label": d.label,
                             "linked_at": d.linked_at, "last_seen": d.last_seen, "blocked": d.blocked or 0,
                             "blocked_label": d.blocked_label, "blocked_at": d.blocked_at}
                for d in s.scalars(select(MemberDevice))}


def get_web_user_by_discord(discord_user_id: int):
    with Session() as s:
        return s.scalars(select(WebUser).where(WebUser.discord_user_id == discord_user_id)
                         .order_by(WebUser.created_at)).first()


# ---------- lesson feedback ----------
def save_feedback(username: str, video_id: int, title: str, rating: int, comment: str | None) -> None:
    """rating: 1 helpful, -1 not helpful. comment None keeps the old comment."""
    now = utcnow()
    with Session.begin() as s:
        f = s.scalars(select(LessonFeedback).where(LessonFeedback.username == username,
                                                   LessonFeedback.video_id == video_id)).first()
        if f is None:
            f = LessonFeedback(username=username[:40], video_id=video_id, created_at=now)
            s.add(f)
        f.title, f.rating, f.updated_at = (title or "")[:200], rating, now
        if comment is not None:
            f.comment = comment[:500]


def get_feedback(username: str, video_id: int):
    with Session() as s:
        return s.scalars(select(LessonFeedback).where(LessonFeedback.username == username,
                                                      LessonFeedback.video_id == video_id)).first()


def list_feedback(limit: int = 60) -> list:
    with Session() as s:
        return list(s.scalars(select(LessonFeedback).order_by(LessonFeedback.updated_at.desc()).limit(limit)))


def feedback_counts() -> dict:
    with Session() as s:
        rows = s.execute(select(LessonFeedback.video_id, LessonFeedback.rating, func.count())
                         .group_by(LessonFeedback.video_id, LessonFeedback.rating)).all()
    out: dict = {}
    for vid, rating, n in rows:
        d = out.setdefault(vid, {"up": 0, "down": 0})
        d["up" if rating > 0 else "down"] += n
    return out


def delete_feedback(fid: int) -> None:
    with Session.begin() as s:
        f = s.get(LessonFeedback, fid)
        if f:
            s.delete(f)


# ---------- image generator (tutorials page) ----------
def log_image_gen(username: str, prompt: str, ratio: str, model: str, ok: bool) -> None:
    now = utcnow()
    with Session.begin() as s:
        s.add(ImageGen(username=username[:40], prompt=prompt[:600], ratio=ratio[:8], model=model[:40], ok=ok, created_at=now))
        s.execute(delete(ImageGen).where(ImageGen.created_at < now - dt.timedelta(days=90)))


def image_gens_since(username: str, hours: int = 24) -> int:
    """Images this member made (successfully) in the last N hours."""
    since = utcnow() - dt.timedelta(hours=hours)
    with Session() as s:
        return s.scalar(select(func.count()).select_from(ImageGen).where(
            ImageGen.username == username, ImageGen.ok.is_(True), ImageGen.created_at >= since)) or 0


def image_gen_stats() -> dict:
    now = utcnow()
    with Session() as s:
        def n(hours):
            return s.scalar(select(func.count()).select_from(ImageGen).where(
                ImageGen.ok.is_(True), ImageGen.created_at >= now - dt.timedelta(hours=hours))) or 0
        people = s.scalar(select(func.count(func.distinct(ImageGen.username))).where(
            ImageGen.ok.is_(True), ImageGen.created_at >= now - dt.timedelta(days=7))) or 0
        return {"day": n(24), "week": n(24 * 7), "people": people}


def recent_image_gens(limit: int = 15) -> list:
    with Session() as s:
        return list(s.scalars(select(ImageGen).order_by(ImageGen.created_at.desc()).limit(limit)))



# ---------- niches (dynamic, dashboard-managed) ----------
import re as _re


def _slug(label: str) -> str:
    s = _re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return (s or "niche")[:40]


def seed_niches(defaults: dict) -> None:
    """On first run, fill the niches table from the built-in defaults."""
    with Session.begin() as s:
        if s.scalar(select(func.count()).select_from(Niche)):
            return
        for key, (label, emoji, *_rest) in defaults.items():
            s.add(Niche(key=key, label=label, emoji=emoji or ""))


def list_niches(active_only: bool = False) -> list:
    with Session() as s:
        q = select(Niche)
        if active_only:
            q = q.where(Niche.active.is_(True))
        return list(s.scalars(q.order_by(func.lower(Niche.label), Niche.key)))   # A to Z


def get_niche(key: str):
    with Session() as s:
        n = s.get(Niche, key)
        if n is not None:
            s.expunge(n)
        return n


def add_niche(label: str, emoji: str = "") -> str:
    base = _slug(label)
    with Session.begin() as s:
        key, i = base, 2
        while s.get(Niche, key) is not None:
            key = f"{base}-{i}"[:40]
            i += 1
        s.add(Niche(key=key, label=(label or key)[:60], emoji=(emoji or "")[:16]))
        return key


def update_niche(key: str, label: str = None, emoji: str = None, active: bool = None) -> None:
    with Session.begin() as s:
        n = s.get(Niche, key)
        if not n:
            return
        if label is not None:
            n.label = label[:60]
        if emoji is not None:
            n.emoji = emoji[:16]
        if active is not None:
            n.active = active


def delete_niche(key: str) -> None:
    with Session.begin() as s:
        n = s.get(Niche, key)
        if n:
            s.delete(n)
        for p in s.scalars(select(Prompt).where(Prompt.niche == key)):
            s.delete(p)


# ---------- prompts ----------
def list_prompts(niche: str, active_only: bool = False) -> list:
    with Session() as s:
        q = select(Prompt).where(Prompt.niche == niche)
        if active_only:
            q = q.where(Prompt.active.is_(True))
        return list(s.scalars(q.order_by(Prompt.id.desc())))


def prompt_counts() -> dict:
    with Session() as s:
        rows = s.execute(select(Prompt.niche, func.count(), func.sum(func.cast(Prompt.active, Integer)))
                         .group_by(Prompt.niche)).all()
    return {n: {"total": t, "active": int(a or 0)} for n, t, a in rows}


def add_prompt(niche: str, text: str) -> int:
    with Session.begin() as s:
        p = Prompt(niche=niche, text=text.strip())  # no length limit
        s.add(p)
        s.flush()
        return p.id


def update_prompt(prompt_id: int, text: str) -> None:
    with Session.begin() as s:
        p = s.get(Prompt, prompt_id)
        if p:
            p.text = text.strip()  # no length limit


def toggle_prompt(prompt_id: int) -> None:
    with Session.begin() as s:
        p = s.get(Prompt, prompt_id)
        if p:
            p.active = not p.active


def delete_prompt(prompt_id: int) -> None:
    with Session.begin() as s:
        p = s.get(Prompt, prompt_id)
        if p:
            s.delete(p)


def random_prompt(niche: str):
    import random
    rows = list_prompts(niche, active_only=True)
    return random.choice(rows) if rows else None


# ---------- warnings & member history ----------
def add_warning(guild_id: int, user_id: int, username: str, reason: str) -> int:
    with Session.begin() as s:
        w = Warning(guild_id=guild_id, user_id=user_id, username=(username or "")[:200], reason=(reason or "")[:500])
        s.add(w)
        s.flush()
        return w.id


def remove_warning(warn_id: int) -> None:
    with Session.begin() as s:
        w = s.get(Warning, warn_id)
        if w:
            w.active = False


def list_warnings(guild_id: int, user_id: int) -> list:
    with Session() as s:
        return list(s.scalars(select(Warning).where(
            Warning.guild_id == guild_id, Warning.user_id == user_id)
            .order_by(Warning.created_at.desc())))


def warning_count(guild_id: int, user_id: int) -> int:
    with Session() as s:
        return s.scalar(select(func.count()).select_from(Warning).where(
            Warning.guild_id == guild_id, Warning.user_id == user_id, Warning.active.is_(True))) or 0


def member_joins(guild_id: int, user_id: int) -> list:
    with Session() as s:
        return list(s.scalars(select(JoinRequest).where(
            JoinRequest.guild_id == guild_id, JoinRequest.user_id == user_id)
            .order_by(JoinRequest.joined_at.desc())))


def member_devices(guild_id: int, user_id: int) -> list:
    with Session() as s:
        return list(s.scalars(select(DeviceCheck).where(
            DeviceCheck.guild_id == guild_id, DeviceCheck.user_id == user_id)
            .order_by(DeviceCheck.created_at.desc())))


def find_members(guild_id: int, query: str, limit: int = 40) -> list[dict]:
    """Distinct members seen in join records, newest first, optionally filtered by name or id."""
    q = (query or "").strip().lower()
    with Session() as s:
        rows = s.scalars(select(JoinRequest).where(JoinRequest.guild_id == guild_id)
                         .order_by(JoinRequest.joined_at.desc())).all()
    out, seen = [], set()
    for r in rows:
        if r.user_id in seen:
            continue
        if q and q not in (r.username or "").lower() and q not in str(r.user_id):
            continue
        seen.add(r.user_id)
        out.append({"user_id": r.user_id, "username": r.username, "joined_at": r.joined_at, "status": r.status})
        if len(out) >= limit:
            break
    return out


# ---------- join gate ----------
def add_join(guild_id: int, user_id: int, username: str, avatar_url: str,
             account_created, flags: str, status: str) -> int:
    with Session.begin() as s:
        row = JoinRequest(guild_id=guild_id, user_id=user_id, username=(username or "")[:200],
                          avatar_url=(avatar_url or "")[:500], account_created=account_created,
                          flags=(flags or "")[:300], status=status)
        s.add(row)
        s.flush()
        return row.id


def get_join(join_id: int):
    with Session() as s:
        return s.get(JoinRequest, join_id)


def set_join_status(join_id: int, status: str) -> None:
    with Session.begin() as s:
        row = s.get(JoinRequest, join_id)
        if row:
            row.status = status
            row.decided_at = utcnow()


def save_deny_reason(join_id: int, reason: str, dm: bool) -> None:
    with Session.begin() as s:
        row = s.get(DenyReason, join_id)
        if row is None:
            row = DenyReason(join_id=join_id)
            s.add(row)
        row.reason, row.dm = reason or "", 1 if dm else 0


def deny_reasons(ids) -> dict:
    """{join_id: {"text": reason, "dm": told them by DM}} for these join requests."""
    ids = list(ids)
    if not ids:
        return {}
    with Session() as s:
        rows = s.scalars(select(DenyReason).where(DenyReason.join_id.in_(ids)))
        return {r.join_id: {"text": r.reason or "", "dm": bool(r.dm)} for r in rows}


def mark_join_left(guild_id: int, user_id: int) -> None:
    with Session.begin() as s:
        rows = s.scalars(select(JoinRequest).where(
            JoinRequest.guild_id == guild_id, JoinRequest.user_id == user_id,
            JoinRequest.status == "pending"))
        for row in rows:
            row.status = "left"
            row.decided_at = utcnow()


def was_approved_before(guild_id: int, user_id: int) -> bool:
    with Session() as s:
        return s.scalar(select(func.count()).select_from(JoinRequest).where(
            JoinRequest.guild_id == guild_id, JoinRequest.user_id == user_id,
            JoinRequest.status == "approved")) > 0


def list_joins(guild_id: int | None = None, pending: bool = False, limit: int = 100) -> list:
    with Session() as s:
        q = select(JoinRequest)
        if guild_id is not None:
            q = q.where(JoinRequest.guild_id == guild_id)
        if pending:
            q = q.where(JoinRequest.status == "pending")
        q = q.order_by(JoinRequest.joined_at.desc()).limit(limit)
        return list(s.scalars(q))


# ---------- upload reports ----------
UPLOAD_KINDS = ("default", "yt", "fb", "schedule", "fx")


def get_upload_channels(guild_id: int) -> dict:
    with Session() as s:
        row = s.get(UploadChannel, guild_id)
        return {k: (getattr(row, f"{k}_ch") if row else None) for k in UPLOAD_KINDS}


def set_upload_channels(guild_id: int, channels: dict) -> None:
    with Session.begin() as s:
        row = s.get(UploadChannel, guild_id)
        if row is None:
            row = UploadChannel(guild_id=guild_id)
            s.add(row)
        for k in UPLOAD_KINDS:
            v = channels.get(k)
            setattr(row, f"{k}_ch", int(v) if v else None)


def get_setting(key: str, default: str = "") -> str:
    with Session() as s:
        row = s.get(Setting, key)
        return row.value if row else default


def set_setting(key: str, value) -> None:
    with Session.begin() as s:
        row = s.get(Setting, key)
        if row is None:
            s.add(Setting(key=key, value=str(value)))
        else:
            row.value = str(value)


# ---------- dashboard ----------
def public_stats() -> dict:
    """Safe aggregate numbers for the public landing page (no names or IDs)."""
    with Session() as s:
        servers = s.scalar(select(func.count()).select_from(Server)) or 0
        members = s.scalar(select(func.count(func.distinct(JoinRequest.user_id)))) or 0
        banned = s.scalar(select(func.count(func.distinct(JoinRequest.user_id)))
                          .where(JoinRequest.status == "banned")) or 0
        approved = s.scalar(select(func.count()).select_from(Registration)
                            .where(Registration.status == "approved")) or 0
        prompts_available = s.scalar(select(func.count()).select_from(Prompt)
                                     .where(Prompt.active.is_(True))) or 0
        prompts_sent = s.scalar(select(func.count()).select_from(PromptUse)) or 0
    return {"servers": servers, "members": members, "banned": banned, "approved": approved,
            "prompts_available": prompts_available, "prompts_sent": prompts_sent}


def overview() -> dict:
    since = utcnow() - dt.timedelta(hours=24)
    with Session() as s:
        servers = list(s.scalars(select(Server).order_by(Server.added_at)))
        members = list(s.scalars(select(Member).order_by(Member.requested_at.desc())))
        gen24 = s.scalar(
            select(func.count()).select_from(UsageLog).where(UsageLog.ts >= since, UsageLog.status == "ok")
        ) or 0
        jobs = s.scalar(select(func.count()).select_from(Job)) or 0
        regs_total = s.scalar(select(func.count()).select_from(Registration).where(Registration.status == "approved")) or 0
        regs_pending = s.scalar(select(func.count()).select_from(Registration).where(Registration.status == "pending")) or 0
    return {"servers": servers, "members": members, "gen24": gen24, "jobs": jobs,
            "regs_total": regs_total, "regs_pending": regs_pending}
