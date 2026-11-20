"""Auto edit: a member picks a niche (and, if they want, a concept, their own script or their own clips) and gets
a whole video: a YouTube Short / Facebook Reel (30 to 60 seconds) or a long video (5 to 10 minutes).

The server does the light work. The story comes from the owner's Pollinations text model (free). The voice,
the pictures and the music come either from the member's own Monid key (monid.ai: ElevenLabs voices, MiniMax or
GPT Image pictures, MiniMax music; the member pays) or from the owner's Pollinations key, as the owner chooses.
The member's browser then puts it all together into an MP4 (static/autoedit.js), so nothing heavy runs on the
Render server and the bot stays fast.

Only members the owner gives access to can use it, each in their own time window, one video at a time.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

import db

log = logging.getLogger("autoedit")
TZ = ZoneInfo(os.getenv("DISPLAY_TZ", "Asia/Manila"))
ROOT = Path(os.getenv("AUTOEDIT_DIR", "/tmp/autoedit"))
KEEP_HOURS = 24                    # files are kept this long, then the member can make them again
WORDS_PER_MIN = 140                # narration speed
SCENE_WORDS = 28                   # words per scene (about 11 seconds, one picture each)
BATCH = 8                          # scenes written per request to the text model
LENGTHS = (5, 7, 10)               # long videos, minutes
SHORTS = (30, 45, 60)              # YouTube Shorts / Facebook Reels, seconds (9:16)
SHORT_SCENE_WORDS = 10             # Shorts change picture about every 4 seconds
RATIOS = ("16:9", "9:16")
SIZES = {"16:9": [(1920, 1088), (1344, 768)], "9:16": [(1088, 1920), (768, 1344)]}
LANGS = ["English", "Tagalog", "Taglish", "Cebuano", "Ilocano", "Spanish", "Portuguese", "French", "German",
         "Italian", "Hindi", "Indonesian", "Malay", "Vietnamese", "Thai", "Arabic", "Turkish", "Russian",
         "Japanese", "Korean", "Chinese"]
VOICES = [("onyx", "Deep male"), ("echo", "Male"), ("fable", "Storyteller"), ("alloy", "Neutral"),
          ("nova", "Female"), ("shimmer", "Soft female")]
ACTIVE = ("queued", "writing", "voicing", "drawing", "music")
STEP_LABEL = {"queued": "Waiting in line", "writing": "Writing the story", "voicing": "Recording the voice",
              "drawing": "Making the pictures", "music": "Making the music", "ready": "Ready",
              "failed": "Didn't finish", "canceled": "Canceled", "expired": "Files cleared"}
DEFAULTS = {"ae_on": "1", "ae_limit": "2", "ae_text_model": "openai", "ae_voice_model": "elevenflash",
            "ae_image_model": "flux", "ae_music": "1", "ae_music_model": "elevenmusic",
            "ae_provider": "monid", "ae_monid_image": "minimax", "ae_monid_voice": "eleven_flash_v2_5"}
PROVIDERS = [("monid", "Each member's own Monid key (they pay)"), ("pollinations", "Your Pollinations key (you pay)")]
MONID_IMAGES = [("minimax", "MiniMax image-01: good, about $0.004 a picture"),
                ("gpt-image-2", "GPT Image 2: best, costs more")]
MONID_VOICES = [("eleven_flash_v2_5", "ElevenLabs Flash: fast, half the price"),
                ("eleven_multilingual_v2", "ElevenLabs Multilingual: best quality")]
ELEVEN_VOICE = {"onyx": "nPczCjzI2devNBz1zQrb", "echo": "pNInz6obpgDQGcFmaJgB", "fable": "JBFqnCBsd6RMkjVDRZzb",
                "alloy": "TX3LPaxmHKxFdv7VOQHJ", "nova": "21m00Tcm4TlvDq8ikWAM", "shimmer": "EXAVITQu4vr4xnSDxMKL"}


# ------------------------- database -------------------------
class AutoAccess(db.Base):
    """A member the owner allowed to use Auto edit, and the hours they may start a video (Manila time)."""
    __tablename__ = "auto_access"
    username: Mapped[str] = mapped_column(String(40), primary_key=True)
    start: Mapped[str] = mapped_column(String(5), default="")      # "14:00"; empty = any time
    end: Mapped[str] = mapped_column(String(5), default="")
    added_at: Mapped[dt.datetime] = mapped_column(DateTime, default=db.utcnow)


class AutoJob(db.Base):
    """One Auto edit video."""
    __tablename__ = "auto_jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(40), index=True)
    niche: Mapped[str] = mapped_column(String(40), default="")
    niche_label: Mapped[str] = mapped_column(String(60), default="")
    concept: Mapped[str] = mapped_column(Text, default="")
    script_in: Mapped[str] = mapped_column(Text, default="")
    language: Mapped[str] = mapped_column(String(40), default="English")
    minutes: Mapped[int] = mapped_column(Integer, default=5)
    ratio: Mapped[str] = mapped_column(String(8), default="16:9")
    voice: Mapped[str] = mapped_column(String(20), default="onyx")
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    step_done: Mapped[int] = mapped_column(Integer, default=0)
    step_total: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(200), default="")
    data: Mapped[str] = mapped_column(Text, default="")             # JSON: style, characters, music, scenes
    msg: Mapped[str] = mapped_column(String(400), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=db.utcnow, index=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


def setting(key: str) -> str:
    return db.get_setting(key, DEFAULTS.get(key, "")) or DEFAULTS.get(key, "")


def access_all() -> list:
    with db.Session() as s:
        return list(s.scalars(select(AutoAccess).order_by(AutoAccess.username)))


def access_get(username: str):
    with db.Session() as s:
        return s.get(AutoAccess, username)


def access_set(username: str, start: str, end: str) -> None:
    with db.Session.begin() as s:
        row = s.get(AutoAccess, username)
        if row is None:
            row = AutoAccess(username=username)
            s.add(row)
        row.start, row.end = start, end


def access_remove(username: str) -> None:
    with db.Session.begin() as s:
        row = s.get(AutoAccess, username)
        if row is not None:
            s.delete(row)


def job_add(**fields) -> int:
    with db.Session.begin() as s:
        job = AutoJob(**fields)
        s.add(job)
        s.flush()
        return job.id


def job_get(job_id: int):
    with db.Session() as s:
        return s.get(AutoJob, job_id)


def job_set(job_id: int, **fields) -> None:
    with db.Session.begin() as s:
        job = s.get(AutoJob, job_id)
        if job is not None:
            for k, v in fields.items():
                setattr(job, k, v)


def job_step(job_id: int, **fields) -> bool:
    """Update a video only while it is still being made (a cancel always wins). False = canceled."""
    with db.Session.begin() as s:
        job = s.get(AutoJob, job_id)
        if job is None or job.status not in ACTIVE:
            return False
        for k, v in fields.items():
            setattr(job, k, v)
        return True


def jobs_for(username: str, limit: int = 6) -> list:
    with db.Session() as s:
        return list(s.scalars(select(AutoJob).where(AutoJob.username == username)
                              .order_by(AutoJob.id.desc()).limit(limit)))


def jobs_recent(limit: int = 30) -> list:
    with db.Session() as s:
        return list(s.scalars(select(AutoJob).order_by(AutoJob.id.desc()).limit(limit)))


def next_queued():
    with db.Session() as s:
        return s.scalars(select(AutoJob).where(AutoJob.status.in_(ACTIVE)).order_by(AutoJob.id)).first()


def queue_position(job_id: int) -> int:
    """1 = being made now (or next)."""
    with db.Session() as s:
        return s.scalar(select(func.count()).select_from(AutoJob).where(
            AutoJob.status.in_(ACTIVE), AutoJob.id <= job_id)) or 0


def made_today(username: str) -> int:
    """Videos started in the last 24 hours that count toward the daily limit (failed and canceled don't)."""
    since = db.utcnow() - dt.timedelta(hours=24)
    with db.Session() as s:
        return s.scalar(select(func.count()).select_from(AutoJob).where(
            AutoJob.username == username, AutoJob.created_at >= since,
            AutoJob.status.not_in(("failed", "canceled")))) or 0


def _data(job) -> dict:
    try:
        return json.loads(job.data or "{}")
    except ValueError:
        return {}


# ------------------------- access and time windows -------------------------
def _hm(text: str) -> int | None:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (text or "").strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def window_label(start: str, end: str) -> str:
    a, b = _hm(start), _hm(end)
    if a is None or b is None or a == b:
        return "Any time"

    def fmt(m):
        h, mm = divmod(m, 60)
        return f"{(h % 12) or 12}:{mm:02d} {'AM' if h < 12 else 'PM'}"
    return f"{fmt(a)} to {fmt(b)}"


def in_window(start: str, end: str, now: dt.datetime | None = None) -> bool:
    a, b = _hm(start), _hm(end)
    if a is None or b is None or a == b:
        return True
    now = now or dt.datetime.now(TZ)
    m = now.hour * 60 + now.minute
    return a <= m < b if a < b else (m >= a or m < b)        # windows past midnight work too


def member_info(username: str) -> dict:
    """For the tutorials page: show the Auto edit tab? What may this member do right now?"""
    if not username or setting("ae_on") == "0" or not os.getenv("POLLINATIONS_KEY", "").strip():
        return {"show": False}
    acc = access_get(username)
    if acc is None:
        return {"show": False}
    try:
        limit = max(1, int(setting("ae_limit")))
    except ValueError:
        limit = 2
    used = made_today(username)
    niches = [{"key": n.key, "label": n.label or n.key} for n in db.list_niches(active_only=True)]
    return {"show": True, "limit": limit, "left": max(0, limit - used), "window": window_label(acc.start, acc.end),
            "open": in_window(acc.start, acc.end), "niches": niches, "langs": LANGS, "voices": VOICES,
            "lengths": LENGTHS, "shorts": SHORTS, "monid": setting("ae_provider") == "monid"}


# ------------------------- Monid (the member's own key) -------------------------
_KEYS: dict = {}                   # job id -> the member's Monid key, kept in memory only while the video is made


class MemberStop(Exception):
    """A problem with the member's own key or balance: shown to the member, not to the owner."""


def _monid_base() -> str:
    return os.getenv("MONID_URL", "https://api.monid.ai").rstrip("/")


def good_key(key: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_\-.]{16,300}", key or ""))


async def monid_call(key: str, method: str, path: str, body: dict | None = None, timeout: float = 300) -> tuple:
    """(status, json or None, text)."""
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.request(method, _monid_base() + path, json=body,
                                 headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}) as r:
                raw = await r.read()
                try:
                    return r.status, json.loads(raw), ""
                except ValueError:
                    return r.status, None, raw[:300].decode("utf-8", "replace")
    except asyncio.TimeoutError:
        return 0, None, "timeout"
    except aiohttp.ClientError as e:
        return 0, None, f"could not reach Monid ({type(e).__name__})"


def _monid_why(status: int, j, text: str, what: str) -> str:
    msg = (j or {}).get("message") if isinstance(j, dict) else ""
    if status == 401:
        return "Your Monid key isn't working. Check it, save it again, then press Make it again."
    if status == 402:
        return "Your Monid balance ran out. Top up at monid.ai, then press Make it again."
    if status == 403:
        return "Your Monid key can't use this. Check your Monid workspace."
    if status == 0:
        return f"Couldn't reach Monid while {what}. Try again in a few minutes."
    return f"Monid couldn't finish {what} (HTTP {status}){': ' + str(msg)[:200] if msg else ''}."


async def monid_run(key: str, provider: str, endpoint: str, inp: dict, what: str, wait: float = 900) -> dict:
    """Run one Monid endpoint and wait for it. Returns the finished run (with "output")."""
    status, j, text = await monid_call(key, "POST", "/v1/run", {"provider": provider, "endpoint": endpoint, "input": inp})
    if status in (401, 402, 403):
        raise MemberStop(_monid_why(status, j, text, what))
    if status not in (200, 201, 202) or not isinstance(j, dict):
        raise Stop(_monid_why(status, j, text, what))
    run, start = j, time.monotonic()
    while str(run.get("status", "")).upper() in ("READY", "QUEUED", "PENDING", "RUNNING", "IN_PROGRESS", "STARTED") \
            and run.get("runId") and time.monotonic() - start < wait:
        await asyncio.sleep(4)
        st, r2, t2 = await monid_call(key, "GET", f"/v1/runs/{run['runId']}", timeout=60)
        if st in (401, 402, 403):
            raise MemberStop(_monid_why(st, r2, t2, what))
        if st == 200 and isinstance(r2, dict):
            run = r2
    state = str(run.get("status", "")).upper()
    pr = run.get("providerResponse") or {}
    code = pr.get("httpStatus") if isinstance(pr, dict) else None
    if state not in ("COMPLETED", "SUCCEEDED", "SUCCESS", "DONE", "") or (isinstance(code, int) and code >= 400) \
            or run.get("output") is None:
        err = pr.get("error") if isinstance(pr, dict) else None
        detail = (err.get("message") if isinstance(err, dict) else err) or run.get("reason") or state.lower() or "no result"
        if isinstance(code, int) and code == 402:
            raise MemberStop("Your Monid balance ran out. Top up at monid.ai, then press Make it again.")
        raise Stop(f"Monid couldn't finish {what}: {str(detail)[:200]}")
    return run


def _media_spots(obj, depth=0):
    """Every place in a Monid output that could hold the file: (kind, value)."""
    if depth > 8:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            lk = str(k).lower()
            if isinstance(v, str):
                if v.startswith(("http://", "https://")) and any(w in lk for w in ("url", "link", "uri", "href")):
                    yield "url", v
                elif ("b64" in lk or "base64" in lk) and len(v) > 200:
                    yield "b64", v
                elif lk in ("audio", "data", "image") and len(v) > 400 and re.fullmatch(r"[0-9a-fA-F]+", v[:400]):
                    yield "hex", v
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, str) and x.startswith(("http://", "https://")) and \
                            any(w in lk for w in ("url", "link", "uri")):
                        yield "url", x
                    else:
                        yield from _media_spots(x, depth + 1)
            else:
                yield from _media_spots(v, depth + 1)
    elif isinstance(obj, list):
        for x in obj:
            yield from _media_spots(x, depth + 1)


async def monid_media(run: dict, want: str) -> bytes:
    """The picture or sound a Monid run made (from a link, base64 or hex). want: "image" or "audio"."""
    import aiohttp
    import base64
    seen = 0
    todo = list(_media_spots(run.get("output")))
    while todo and seen < 6:
        kind, val = todo.pop(0)
        seen += 1
        if kind == "b64":
            try:
                return base64.b64decode(val.split(",", 1)[-1])
            except ValueError:
                continue
        if kind == "hex":
            try:
                return bytes.fromhex(val)
            except ValueError:
                continue
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as s:
                async with s.get(val) as r:
                    data = await r.read()
                    ctype = r.headers.get("Content-Type", "").lower()
        except (asyncio.TimeoutError, aiohttp.ClientError):
            continue
        if r.status != 200:
            continue
        if "json" in ctype:                        # a big result is handed over as a JSON file first
            try:
                todo = list(_media_spots(json.loads(data))) + todo
            except ValueError:
                pass
            continue
        if want == "image" and (ctype.startswith("image/") or data[:3] == b"\xff\xd8\xff" or data[:4] == b"\x89PNG"):
            return data
        if want == "audio" and (ctype.startswith("audio/") or _is_audio(ctype, data) or "octet" in ctype):
            return data
    raise Stop("Monid finished, but the file couldn't be read from its answer.")


async def monid_speech(key: str, text: str, voice: str) -> bytes:
    inp = {"text": text[:4900], "model_id": setting("ae_monid_voice"), "voice_id": ELEVEN_VOICE.get(voice, ELEVEN_VOICE["onyx"])}
    run = await monid_run(key, "elevenlabs", "/text-to-speech", inp, "recording the voice")
    return await monid_media(run, "audio")


async def monid_picture(key: str, prompt: str, ratio: str) -> bytes | None:
    import app as web
    w, h = (1080, 1920) if ratio == "9:16" else (1920, 1080)
    if setting("ae_monid_image") == "gpt-image-2":
        size = "1088x1920" if ratio == "9:16" else "1920x1088"
        run = await monid_run(key, "openai", "/v1/image/gpt-image-2",
                              {"prompt": prompt[:3800], "size": size, "quality": "medium"}, "making the pictures")
    else:
        try:
            run = await monid_run(key, "minimax", "/v1/image_generation",
                                  {"model": "image-01", "prompt": prompt[:1400], "width": w, "height": h,
                                   "response_format": "url", "n": 1}, "making the pictures")
        except Stop:
            run = await monid_run(key, "minimax", "/v1/image_generation",
                                  {"model": "image-01", "prompt": prompt[:1400], "aspect_ratio": ratio,
                                   "response_format": "url", "n": 1}, "making the pictures")
    data = await monid_media(run, "image")
    final, _why = await asyncio.to_thread(web._img_finish, data, ratio)
    return final


async def monid_music(key: str, mood: str) -> bytes | None:
    """Background music from MiniMax. Any problem: no music (the video is still made)."""
    tries = [{"model": "music-2.0", "prompt": f"{mood}, instrumental", "is_instrumental": True, "output_format": "url"},
             {"model": "music-1.5", "prompt": f"{mood}, instrumental", "lyrics": "[Instrumental]\n[Instrumental]",
              "output_format": "url"}]
    for inp in tries:
        try:
            run = await monid_run(key, "minimax", "/v1/music_generation", inp, "making the music")
            return await monid_media(run, "audio")
        except MemberStop:
            raise
        except Stop as e:
            log.info("Monid music try failed: %s", e)
    return None


async def monid_check(key: str) -> dict:
    """Is this key good, and how much is left? For the key box on the Auto edit tab."""
    status, j, text = await monid_call(key, "GET", "/v1/auth/whoami", timeout=20)
    if status == 401:
        return {"ok": False, "msg": "That key doesn't work. Copy it again from monid.ai (it starts with monid_)."}
    if status != 200:
        return {"ok": False, "msg": _monid_why(status, j, text, "checking your key")}
    bal = ""
    st, b, _ = await monid_call(key, "GET", "/v1/wallet/balance", timeout=20)
    if st == 200 and isinstance(b, dict):
        for spot in (b, b.get("balance") if isinstance(b.get("balance"), dict) else None, b.get("data")):
            if not isinstance(spot, dict):
                continue
            for k in ("available", "balance", "value", "amount", "usd"):
                v = spot.get(k)
                if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v)):
                    n = float(v)
                    if str(spot.get("unit", spot.get("currency", ""))).upper() in ("MICRO_DOLLAR", "MICRO_USD"):
                        n /= 1_000_000
                    bal = f"${n:,.2f}"
                    break
            if bal:
                break
    return {"ok": True, "balance": bal}


# ------------------------- Pollinations -------------------------
def _base() -> str:
    return os.getenv("POLLINATIONS_URL", "https://gen.pollinations.ai").rstrip("/")


def _key() -> str:
    return os.getenv("POLLINATIONS_KEY", "").strip()


class Stop(Exception):
    """A problem that ends the job; the text is shown to the member."""


def problem(msg: str) -> None:
    log.warning("Auto edit: %s", msg)
    db.set_setting("ae_err", f"{msg[:300]}|{time.time():.0f}")


async def _post(path: str, body: dict, timeout: float = 180) -> tuple:
    """(status, content_type, bytes). status 0 = could not reach."""
    import aiohttp
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as s:
            async with s.post(_base() + path, json=body, headers={"Authorization": f"Bearer {_key()}"}) as r:
                return r.status, r.headers.get("Content-Type", "").split(";")[0].strip().lower(), await r.read()
    except asyncio.TimeoutError:
        return 0, "", b"timeout"
    except aiohttp.ClientError as e:
        return 0, "", f"could not reach Pollinations ({type(e).__name__})".encode()


def _explain(what: str, status: int, body: bytes) -> str:
    text = body[:300].decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
    if status in (401, 403):
        return f"Pollinations refused the key while {what} (HTTP {status}). Check POLLINATIONS_KEY on Render."
    if status == 402:
        return f"Out of Pollinations credits (pollen) while {what}. Top up at enter.pollinations.ai."
    if status == 0:
        return f"Couldn't reach Pollinations while {what}: {text}"
    return f"Pollinations error while {what} (HTTP {status}): {text}"


_NO_JSON_MODE: set = set()        # text models that refused JSON mode once (asked without it from then on)


async def chat_json(prompt: str, system: str, model: str, tries: int = 3) -> dict:
    """Ask the text model for a JSON object."""
    last, json_mode, attempt = "", model not in _NO_JSON_MODE, 0
    while attempt < tries:
        body = {"model": model, "messages": [{"role": "system", "content": system},
                                              {"role": "user", "content": prompt}], "temperature": 0.9}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        status, _, raw = await _post("/v1/chat/completions", body)
        if status == 400 and json_mode:
            json_mode = False                      # this model doesn't take JSON mode: ask again without it
            _NO_JSON_MODE.add(model)
            continue
        attempt += 1
        if status == 200:
            try:
                text = json.loads(raw)["choices"][0]["message"]["content"] or ""
                a, b = text.find("{"), text.rfind("}")
                return json.loads(text[a:b + 1])
            except (ValueError, KeyError, IndexError, TypeError):
                last = "The story model's answer couldn't be read. Try again, or set another story model."
                continue
        last = _explain("writing the story", status, raw)
        if status in (401, 402, 403):
            break
        await asyncio.sleep(3 + 4 * attempt)
    raise Stop(last or "Couldn't write the story.")


def _is_audio(ctype: str, data: bytes) -> bool:
    return ctype.startswith("audio/") or data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")


async def speech(text: str, voice: str, model: str, tries: int = 3) -> bytes:
    last = ""
    for attempt in range(tries):
        status, ctype, data = await _post("/v1/audio/speech", {"model": model, "input": text, "voice": voice,
                                                              "response_format": "mp3"}, timeout=120)
        if status == 200 and _is_audio(ctype, data) and len(data) > 500:
            return data
        last = _explain("recording the voice", status, data) if status != 200 else \
            f"Pollinations sent something that isn't sound while recording the voice ({ctype or 'unknown'})."
        if status in (401, 402, 403):
            break
        await asyncio.sleep(3 + 5 * attempt)
    raise Stop(last)


async def music(prompt: str, model: str, seconds: int = 90) -> bytes | None:
    status, ctype, data = await _post("/v1/audio/speech", {"model": model, "input": prompt, "duration": seconds,
                                                          "response_format": "mp3"}, timeout=240)
    if status == 200 and _is_audio(ctype, data) and len(data) > 2000:
        return data
    problem(_explain("making the music", status, data) + " The video was made without music.")
    return None


async def picture(prompt: str, ratio: str, model: str, seed: int) -> bytes | None:
    """One finished JPEG the exact size of the video, or None."""
    import app as web                                   # the Create images helpers
    for i, (w, h) in enumerate(SIZES[ratio]):
        status, _, out, _info = await web._pollinations(prompt, w, h, model, seed)
        if status == 200 and isinstance(out, (bytes, bytearray)):
            final, _why = await asyncio.to_thread(web._img_finish, bytes(out), ratio)
            return final
        if status in (401, 402, 403):
            raise Stop(_explain("making the pictures", status, str(out).encode()))
        low = str(out).lower()
        if not (status == 400 and i == 0 and any(k in low for k in ("size", "width", "height", "pixel"))):
            break
    return None


# ------------------------- writing the story -------------------------
SYSTEM = ("You write narration scripts for faceless YouTube and Facebook videos: a voice tells a story or explains "
          "a topic while pictures show each moment. You always answer with one JSON object and nothing else.")


def _clean(text: str, n: int = 4000) -> str:
    return " ".join(str(text or "").split())[:n]


def split_script(text: str, words: int = SCENE_WORDS, cap: int = 120) -> list:
    """The member's own script, cut into scenes of about `words` words at sentence ends."""
    sentences = re.findall(r"[^.!?\u3002\uff01\uff1f\n]+[.!?\u3002\uff01\uff1f]*", text or "")
    scenes, cur = [], ""
    for s in (" ".join(x.split()) for x in sentences):
        if not s:
            continue
        if cur and len((cur + " " + s).split()) > words * 1.25:
            scenes.append(cur)
            cur = s
        else:
            cur = (cur + " " + s).strip()
    if cur:
        scenes.append(cur)
    return scenes[:cap]


def seconds_of(job) -> int:
    """A Short's length in seconds (0 for a long video)."""
    return int(_data(job).get("seconds") or 0) if not job.minutes else 0


async def write_story(job, model: str, samples: list) -> dict:
    secs = seconds_of(job)
    scene_words = SHORT_SCENE_WORDS if secs else SCENE_WORDS
    words = round(secs * 1.85) if secs else job.minutes * WORDS_PER_MIN       # Shorts leave room to stay under the limit
    own = split_script(job.script_in, scene_words, 30 if secs else 120) if job.script_in.strip() else []
    n = len(own) if own else max(4 if secs else 8, round(words / scene_words))
    length = (f"a vertical {secs}-second YouTube Short / Facebook Reel ({words} words of narration at most)"
              if secs else f"about {job.minutes} minutes ({words} words of narration)")
    pace = ("It's a Short: the first line must hook people in under 3 seconds, keep it fast with no filler, and end "
            "with a punchy line or a question that makes people comment." if secs else
            "Start with a strong hook and end with a satisfying ending.")
    looks = "\n".join(f"- {_clean(p, 400)}" for p in samples[:3]) or "- (none)"
    base = (f"Niche: {job.niche_label}\nPictures that fit this niche look like:\n{looks}\n")
    if own:
        plan_q = (base + f"Here is the creator's own narration (keep it as it is):\n\"\"\"{_clean(job.script_in, 6000)}\"\"\"\n\n"
                  f"It has {n} scenes. Give the video a title in {job.language}, one English line for the picture "
                  "style, an English description of how each main character looks (face, age, hair, clothes) so "
                  "every picture shows them the same way, and an English line for the background music mood.\n"
                  'JSON: {"title": "", "style": "", "characters": "", "music": ""}')
    else:
        idea = _clean(job.concept, 3000) or "None given. Invent a fresh, gripping idea that fits the niche."
        plan_q = (base + f"Creator's concept: {idea}\nNarration language: {job.language}\n"
                  f"Length: {length} in exactly {n} scenes.\n\n"
                  "Plan the video. Give a title in the narration language, one English line for the picture style, "
                  "an English description of how each main character looks (face, age, hair, clothes) so every "
                  "picture shows them the same way, an English line for the background music mood, and exactly "
                  f"{n} beats: one short English sentence per scene saying what happens. {pace}\n"
                  'JSON: {"title": "", "style": "", "characters": "", "music": "", "beats": ["", ""]}')
    plan = await chat_json(plan_q, SYSTEM, model)
    beats = [str(b) for b in plan.get("beats") or []] if not own else own
    if not own:
        if len(beats) < n:
            beats += [""] * (n - len(beats))
        beats = beats[:n]
    title = _clean(plan.get("title") or job.niche_label, 190)
    style = _clean(plan.get("style"), 400)
    chars = _clean(plan.get("characters"), 900)
    scenes: list = []
    prev = ""
    for a in range(0, n, BATCH):
        part = beats[a:a + BATCH]
        k = len(part)
        if own:
            q = (f"Video: \"{title}\". Picture style: {style}\nCharacters: {chars}\n\n"
                 f"For each of these {k} narration lines, write one English picture prompt that shows that moment: "
                 "one clear scene, the characters' look, the style, cinematic, no text or letters in the picture.\n"
                 + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(part))
                 + f'\nJSON: {{"images": ["", ""]}} with exactly {k} items.')
            got = await chat_json(q, SYSTEM, model)
            imgs = [str(x) for x in got.get("images") or []]
            for i, t in enumerate(part):
                scenes.append({"text": t, "image": _clean(imgs[i] if i < len(imgs) else f"{t}. {style}", 900)})
            continue
        q = (f"Video: \"{title}\" ({job.niche_label}). Picture style: {style}\nCharacters: {chars}\n"
             f"All beats: {json.dumps(beats, ensure_ascii=False)[:5000]}\n"
             + (f"The narration so far ends with: \"{prev[-400:]}\"\n" if prev else "This is the start of the video.\n")
             + f"\nWrite scenes {a + 1} to {a + k}. For each: the narration in {job.language} (about {scene_words} "
             "words, natural spoken style, flowing on from the last scene, no scene numbers, no stage directions, "
             "no emojis) and one English picture prompt showing that moment (one clear scene, the characters' look, "
             "the style, cinematic, no text or letters in the picture).\n"
             f'JSON: {{"scenes": [{{"text": "", "image": ""}}]}} with exactly {k} items.')
        got = await chat_json(q, SYSTEM, model)
        items = [x for x in got.get("scenes") or [] if isinstance(x, dict) and _clean(x.get("text"))]
        for i in range(k):
            x = items[i] if i < len(items) else {"text": part[i] or "...", "image": part[i]}
            scenes.append({"text": _clean(x.get("text"), 900), "image": _clean(x.get("image") or part[i], 900)})
        prev = " ".join(s["text"] for s in scenes[-3:])
    scenes = [s for s in scenes if s["text"] and s["text"] != "..."]
    if len(scenes) < 3:
        raise Stop("The story came back too short. Try again.")
    return {"title": title, "style": style, "characters": chars, "music": _clean(plan.get("music"), 300),
            "scenes": scenes}


# ------------------------- the worker (one video at a time) -------------------------
_WORKER = {"task": None}


def job_dir(job_id: int) -> Path:
    return ROOT / str(int(job_id))


def kick() -> None:
    """Start the worker if it isn't running (called when a video is asked for and when pages check in)."""
    t = _WORKER["task"]
    if t is None or t.done():
        try:
            _WORKER["task"] = asyncio.get_running_loop().create_task(_worker())
        except RuntimeError:
            pass


def _wanted(job_id: int) -> bool:
    job = job_get(job_id)
    return job is not None and job.status in ACTIVE


async def _step(job_id: int, **fields) -> None:
    if not await asyncio.to_thread(job_step, job_id, **fields):
        raise _Canceled()


class _Canceled(Exception):
    pass


async def _save(job_id: int, path: Path, data: bytes) -> bool:
    """Write one file of a video that is still wanted. False = it was canceled meanwhile, stop."""
    if not await asyncio.to_thread(_wanted, job_id):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


async def _worker() -> None:
    await asyncio.to_thread(cleanup)
    while True:
        job = await asyncio.to_thread(next_queued)
        if job is None:
            return
        try:
            await run_job(job.id)
        except _Canceled:
            continue
        except Exception as e:  # noqa: BLE001
            if not await asyncio.to_thread(_wanted, job.id):
                continue                                   # canceled while it was being made
            if isinstance(e, MemberStop):                  # their key or their balance: only they need to know
                await asyncio.to_thread(job_set, job.id, status="failed", msg=str(e)[:390], finished_at=db.utcnow())
            elif isinstance(e, Stop):
                await asyncio.to_thread(problem, str(e))
                await asyncio.to_thread(job_set, job.id, status="failed", msg=str(e)[:390], finished_at=db.utcnow())
            else:
                log.exception("Auto edit job %s failed", job.id)
                await asyncio.to_thread(problem, f"Unexpected error: {type(e).__name__}: {e}")
                await asyncio.to_thread(job_set, job.id, status="failed", finished_at=db.utcnow(),
                                        msg="Something went wrong on the server. Try again.")
        finally:
            j = await asyncio.to_thread(job_get, job.id)
            if j is None or j.status not in ACTIVE:
                _KEYS.pop(job.id, None)                    # the key is forgotten as soon as the video is done


async def run_job(job_id: int) -> None:
    job = await asyncio.to_thread(job_get, job_id)
    d = _data(job)
    folder = job_dir(job_id)
    folder.mkdir(parents=True, exist_ok=True)
    monid = setting("ae_provider") == "monid"
    key = _KEYS.get(job_id, "")
    if monid and not key:
        raise MemberStop("The server restarted, so your Monid key is needed again. Press Make it again.")
    if not d.get("scenes"):
        await _step(job_id, status="writing", step_done=0, step_total=0, msg="")
        samples = [p.text for p in await asyncio.to_thread(db.list_prompts, job.niche, True)][:3]
        d = {**d, **await write_story(job, setting("ae_text_model"), samples)}
        d["seed"] = secrets.randbelow(1_000_000_000) + 1
        await _step(job_id, data=json.dumps(d, ensure_ascii=False), title=d["title"])
    scenes = d["scenes"]
    n = len(scenes)

    voice_model = setting("ae_voice_model")
    await _step(job_id, status="voicing", step_done=0, step_total=n)
    for i, sc in enumerate(scenes):
        f = folder / f"v{i:03d}.mp3"
        if not f.exists():
            if not await asyncio.to_thread(_wanted, job_id):
                return
            sound = await (monid_speech(key, sc["text"], job.voice) if monid else speech(sc["text"], job.voice, voice_model))
            if not await _save(job_id, f, sound):
                return
        await _step(job_id, step_done=i + 1)

    img_model = setting("ae_image_model")
    await _step(job_id, status="drawing", step_done=0, step_total=n)
    style, chars, misses = d.get("style", ""), d.get("characters", ""), 0
    for i, sc in enumerate(scenes):
        f = folder / f"p{i:03d}.jpg"
        if not f.exists():
            if not await asyncio.to_thread(_wanted, job_id):
                return
            prompt = _clean(f"{sc['image']}. {style}. {chars}", 1800)
            data = None
            for attempt in range(3):
                try:
                    data = await (monid_picture(key, prompt, job.ratio) if monid else
                                  picture(prompt, job.ratio, img_model, d.get("seed", 1) + i * 7 + attempt))
                except Stop as e:                              # one bad picture: try again, then reuse the last one
                    log.info("Picture %s of job %s: %s", i, job_id, e)
                    data = None
                if data:
                    break
                await asyncio.sleep(3)
            if data:
                if not await _save(job_id, f, data):
                    return
            elif i > 0 and (folder / f"p{i - 1:03d}.jpg").exists():
                shutil.copyfile(folder / f"p{i - 1:03d}.jpg", f)        # keep going with the last picture
                misses += 1
            else:
                misses += 1
                if i == 0:
                    raise Stop("Couldn't make the pictures. Try again in a few minutes.")
        await _step(job_id, step_done=i + 1)
    if misses > max(3, n // 5):
        raise Stop(f"{misses} of {n} pictures couldn't be made. Try again in a few minutes.")

    d["music_file"] = False
    if setting("ae_music") != "0":
        await _step(job_id, status="music", step_done=0, step_total=1)
        f = folder / "music.mp3"
        if not f.exists():
            mood = d.get("music") or f"background music for a {job.niche_label} video"
            if monid:
                data = await monid_music(key, f"{mood}. Steady, loopable background music, no vocals")
            else:
                data = await music(f"{mood}. Instrumental only, no vocals, steady, loopable background music.",
                                   setting("ae_music_model"))
            if data and not await _save(job_id, f, data):
                return
        d["music_file"] = f.exists()
    if not await asyncio.to_thread(_wanted, job_id):
        return
    await _step(job_id, data=json.dumps(d, ensure_ascii=False), status="ready",
                            step_done=1, step_total=1, finished_at=db.utcnow(),
                            msg=("" if not misses else f"{misses} picture(s) reused the one before."))


def cleanup() -> None:
    """Delete files older than KEEP_HOURS; those videos can be made again from their saved story."""
    if not ROOT.exists():
        return
    limit = time.time() - KEEP_HOURS * 3600
    for d in ROOT.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < limit and d.name.isdigit():
                job = job_get(int(d.name))
                if job is None or job.status not in ACTIVE:
                    shutil.rmtree(d, ignore_errors=True)
                    if job is not None and job.status == "ready":
                        job_set(job.id, status="expired")
        except OSError:
            pass


# ------------------------- what the pages see -------------------------
def _ago(t) -> str:
    if t is None:
        return ""
    s = int((db.utcnow() - t).total_seconds())
    return "just now" if s < 60 else f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h ago" if s < 86400 else f"{s // 86400}d ago"


def job_view(job, files: bool = False) -> dict:
    d = _data(job)
    ready = job.status == "ready"
    if ready and not (job_dir(job.id) / "p000.jpg").exists():      # server restarted: files are gone
        job_set(job.id, status="expired")
        job.status, ready = "expired", False
    out = {"id": job.id, "status": job.status, "label": STEP_LABEL.get(job.status, job.status),
           "done": job.step_done, "total": job.step_total, "title": job.title or job.niche_label,
           "niche": job.niche_label, "minutes": job.minutes, "ratio": job.ratio, "language": job.language,
           "shorts": bool(seconds_of(job)), "length": f"{seconds_of(job)} s" if seconds_of(job) else f"{job.minutes} min",
           "msg": job.msg, "when": _ago(job.created_at), "active": job.status in ACTIVE,
           "line": queue_position(job.id) if job.status == "queued" else 0,
           "again": job.status in ("expired", "failed", "canceled") and bool(d.get("scenes"))}
    if ready and files:
        out["scenes"] = [{"text": s["text"]} for s in d.get("scenes", [])]
        out["music"] = bool(d.get("music_file"))
    return out


def admin_info() -> dict:
    err, _, when = setting("ae_err").partition("|")
    try:
        err_when = _ago(dt.datetime.fromtimestamp(float(when), dt.timezone.utc).replace(tzinfo=None)) if err else ""
    except ValueError:
        err_when = ""
    acc = access_all()
    names = {a.username for a in acc}
    rows = []
    for a in acc:
        rows.append({"user": a.username, "start": a.start, "end": a.end, "window": window_label(a.start, a.end),
                     "today": made_today(a.username)})
    others = [u.username for u in db.list_web_users() if u.username not in names and u.active]
    jobs = [{**job_view(j), "user": j.username} for j in jobs_recent(30)]
    return {"provider": setting("ae_provider"), "providers": PROVIDERS, "monid_image": setting("ae_monid_image"),
            "monid_images": MONID_IMAGES, "monid_voice": setting("ae_monid_voice"), "monid_voices": MONID_VOICES,
            "on": setting("ae_on") != "0", "limit": setting("ae_limit"), "text_model": setting("ae_text_model"),
            "voice_model": setting("ae_voice_model"), "image_model": setting("ae_image_model"),
            "music": setting("ae_music") != "0", "music_model": setting("ae_music_model"),
            "key": bool(_key()), "err": err, "err_when": err_when, "rows": rows, "others": sorted(others),
            "jobs": jobs, "voices": VOICES, "langs": LANGS}


# ------------------------- web routes -------------------------
def _web():
    import app as web
    return web


async def _member(request):
    """The logged-in member allowed to use Auto edit, or (None, error response)."""
    from starlette.responses import JSONResponse
    web = _web()
    if not web.member_authed(request):
        return None, JSONResponse({"ok": False}, status_code=401)
    user = request.session.get("member")
    if await web._member_access(user):
        return None, JSONResponse({"ok": False}, status_code=401)
    acc = await asyncio.to_thread(access_get, user)
    if acc is None or setting("ae_on") == "0":
        return None, JSONResponse({"ok": False, "msg": "Auto edit isn't open for your account."}, status_code=403)
    return user, None


async def member_start(request):
    """POST JSON: make a new video."""
    from starlette.responses import JSONResponse
    web = _web()
    user, bad = await _member(request)
    if bad:
        return bad
    if not web._same_site_json(request):
        return JSONResponse({"ok": False, "msg": "Bad request."}, status_code=400)
    raw = await request.body()
    try:
        f = json.loads(raw) if len(raw) < 120_000 else None
        if not isinstance(f, dict):
            raise ValueError
    except ValueError:
        return JSONResponse({"ok": False, "msg": "That's too much text. Shorten the script and try again."},
                            status_code=400)

    def fail(msg, code=400):
        return JSONResponse({"ok": False, "msg": msg}, status_code=code)
    if not _key():
        return fail("Auto edit is turned off right now.", 503)
    acc = await asyncio.to_thread(access_get, user)
    if not in_window(acc.start, acc.end):
        return fail(f"You can start videos from {window_label(acc.start, acc.end)} (Manila time). Come back then.", 403)
    if any(j.status in ACTIVE for j in await asyncio.to_thread(jobs_for, user, 5)):
        return fail("Your last video is still being made. Wait for it to finish first.", 429)
    info = await asyncio.to_thread(member_info, user)
    if info.get("left", 0) <= 0:
        return fail(f"You've made {info.get('limit')} videos today, the most allowed. Come back tomorrow.", 429)
    niches = {n["key"]: n["label"] for n in info["niches"]}
    niche = str(f.get("niche", ""))
    if niche not in niches:
        return fail("Pick a niche first.")
    shorts = f.get("format") == "shorts"
    minutes = int(f.get("minutes") or 5) if str(f.get("minutes", "")).isdigit() else 5
    seconds = int(f.get("seconds") or 60) if str(f.get("seconds", "")).isdigit() else 60
    ratio = "9:16" if shorts else (f.get("ratio") if f.get("ratio") in RATIOS else "16:9")
    key = str(f.get("key", "")).strip()
    if setting("ae_provider") == "monid" and not good_key(key):
        return fail("Put your Monid key in the box at the top first.")
    lang = str(f.get("language", "English"))[:40]
    if lang not in LANGS:
        lang = "English"
    voice = f.get("voice") if f.get("voice") in dict(VOICES) else "onyx"
    concept = str(f.get("concept", ""))[:3000].strip()
    script = str(f.get("script", ""))[:20000].strip()
    if script and len(script.split()) < 30:
        return fail("Your script is very short. Write at least a few sentences, or leave it empty.")
    if web._too_many("ae:" + user, limit=6, window=600):
        return fail("Too many tries. Wait a few minutes.", 429)
    job_id = await asyncio.to_thread(job_add, username=user, niche=niche, niche_label=niches[niche], concept=concept,
                                     script_in=script, language=lang, voice=voice, ratio=ratio,
                                     minutes=0 if shorts else (minutes if minutes in LENGTHS else 5),
                                     data=json.dumps({"seconds": seconds if seconds in SHORTS else 60}) if shorts else "")
    if key:
        _KEYS[job_id] = key
    kick()
    job = await asyncio.to_thread(job_get, job_id)
    return JSONResponse({"ok": True, "job": await asyncio.to_thread(job_view, job)})


async def member_key(request):
    """POST JSON {key}: check a member's Monid key and show their balance. The key isn't saved on the server."""
    from starlette.responses import JSONResponse
    web = _web()
    user, bad = await _member(request)
    if bad:
        return bad
    if not web._same_site_json(request):
        return JSONResponse({"ok": False}, status_code=400)
    try:
        key = str(json.loads(await request.body()).get("key", "")).strip()
    except (ValueError, AttributeError):
        return JSONResponse({"ok": False}, status_code=400)
    if not good_key(key):
        return JSONResponse({"ok": False, "msg": "That doesn't look like a Monid key. Copy it again from monid.ai."})
    if web._too_many("aekey:" + user, limit=12, window=600):
        return JSONResponse({"ok": False, "msg": "Too many tries. Wait a few minutes."}, status_code=429)
    return JSONResponse(await monid_check(key))


async def member_jobs(request):
    """GET: this member's latest videos (and the one asked for with ?id=, with its scenes when ready)."""
    from starlette.responses import JSONResponse
    user, bad = await _member(request)
    if bad:
        return bad
    kick()
    want = request.query_params.get("id", "")
    jobs = await asyncio.to_thread(jobs_for, user, 6)
    out = [await asyncio.to_thread(job_view, j, j.id == int(want) if want.isdigit() else False) for j in jobs]
    info = await asyncio.to_thread(member_info, user)
    return JSONResponse({"ok": True, "jobs": out, "left": info.get("left", 0), "limit": info.get("limit", 0),
                         "open": info.get("open", False), "window": info.get("window", "")},
                        headers={"Cache-Control": "no-store"})


async def member_file(request):
    """GET a voice clip, picture or the music of one of this member's ready videos."""
    from starlette.responses import FileResponse, JSONResponse
    user, bad = await _member(request)
    if bad:
        return bad
    jid = request.path_params.get("jid", "")
    name = request.path_params.get("name", "")
    if not jid.isdigit() or not re.fullmatch(r"(v\d{3}\.mp3|p\d{3}\.jpg|music\.mp3)", name):
        return JSONResponse({"ok": False}, status_code=404)
    job = await asyncio.to_thread(job_get, int(jid))
    if job is None or job.username != user or job.status != "ready":
        return JSONResponse({"ok": False}, status_code=404)
    path = job_dir(job.id) / name
    if not path.exists():
        return JSONResponse({"ok": False}, status_code=404)
    return FileResponse(path, media_type="audio/mpeg" if name.endswith(".mp3") else "image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


async def member_action(request):
    """POST JSON {id, action: cancel | again}."""
    from starlette.responses import JSONResponse
    web = _web()
    user, bad = await _member(request)
    if bad:
        return bad
    if not web._same_site_json(request):
        return JSONResponse({"ok": False}, status_code=400)
    try:
        f = json.loads(await request.body())
        jid = int(f.get("id"))
    except (ValueError, TypeError, AttributeError):
        return JSONResponse({"ok": False}, status_code=400)
    job = await asyncio.to_thread(job_get, jid)
    if job is None or job.username != user:
        return JSONResponse({"ok": False}, status_code=404)
    if f.get("action") == "cancel" and job.status in ACTIVE:
        await asyncio.to_thread(job_set, jid, status="canceled", finished_at=db.utcnow())
        shutil.rmtree(job_dir(jid), ignore_errors=True)
    elif f.get("action") == "again" and job.status in ("expired", "failed", "canceled"):
        key = str(f.get("key", "")).strip()
        if setting("ae_provider") == "monid":
            if not good_key(key):
                return JSONResponse({"ok": False, "msg": "Put your Monid key in the box at the top first."}, status_code=400)
            _KEYS[jid] = key
        acc = await asyncio.to_thread(access_get, user)
        if not in_window(acc.start, acc.end):
            return JSONResponse({"ok": False, "msg": f"You can make videos from {window_label(acc.start, acc.end)} "
                                                     "(Manila time)."}, status_code=403)
        if any(j.status in ACTIVE for j in await asyncio.to_thread(jobs_for, user, 5)):
            return JSONResponse({"ok": False, "msg": "Your last video is still being made."}, status_code=429)
        info = await asyncio.to_thread(member_info, user)
        if job.status != "expired" and info.get("left", 0) <= 0:
            return JSONResponse({"ok": False, "msg": "No videos left today. Come back tomorrow."}, status_code=429)
        await asyncio.to_thread(job_set, jid, status="queued", msg="", step_done=0, step_total=0,
                                created_at=db.utcnow() if job.status != "expired" else job.created_at)
        kick()
    job = await asyncio.to_thread(job_get, jid)
    return JSONResponse({"ok": True, "job": await asyncio.to_thread(job_view, job)})


async def admin_page(request):
    web = _web()
    if not web.authed(request):
        return web.redirect("/login")
    kick()
    return web.templates.TemplateResponse(request=request, name="autoedit_admin.html", context={
        "csrf": web.csrf_token(request), "flash": request.session.pop("flash", None),
        "ae": await asyncio.to_thread(admin_info), "img_models": web.IMG_MODELS, "tz": web.TZ_LABEL})


async def admin_save(request):
    web = _web()
    form, resp = await web.guarded_post(request)
    if resp:
        return resp
    what = str(form.get("what", ""))
    if what == "settings":
        limit = web.parse_int(form.get("limit")) or 2
        img = str(form.get("image_model", "flux"))
        prov, mimg, mvoice = str(form.get("provider", "")), str(form.get("monid_image", "")), str(form.get("monid_voice", ""))
        vals = {"ae_provider": prov if prov in dict(PROVIDERS) else "monid",
                "ae_monid_image": mimg if mimg in dict(MONID_IMAGES) else "minimax",
                "ae_monid_voice": mvoice if mvoice in dict(MONID_VOICES) else "eleven_flash_v2_5",
                "ae_on": "1" if form.get("on") else "0", "ae_limit": str(max(1, min(50, limit))),
                "ae_text_model": _clean(form.get("text_model"), 60) or DEFAULTS["ae_text_model"],
                "ae_voice_model": _clean(form.get("voice_model"), 60) or DEFAULTS["ae_voice_model"],
                "ae_image_model": img if img in dict(web.IMG_MODELS) else "flux",
                "ae_music": "1" if form.get("music") else "0",
                "ae_music_model": _clean(form.get("music_model"), 60) or DEFAULTS["ae_music_model"]}
        for k, v in vals.items():
            await asyncio.to_thread(db.set_setting, k, v)
        web.flash(request, "Auto edit saved." if _key() else "Saved. Add POLLINATIONS_KEY on Render so it can work.")
    elif what in ("access", "add"):
        user = _clean(form.get("username"), 40)
        start, end = str(form.get("start", "")).strip(), str(form.get("end", "")).strip()
        if _hm(start) is None or _hm(end) is None:
            start = end = ""
        if user and await asyncio.to_thread(db.get_web_user, user):
            await asyncio.to_thread(access_set, user, start, end)
            web.flash(request, f"{user} can use Auto edit: {window_label(start, end).lower()}.")
        else:
            web.flash(request, "Pick a member first.")
    elif what == "remove":
        user = _clean(form.get("username"), 40)
        await asyncio.to_thread(access_remove, user)
        web.flash(request, f"{user} can't use Auto edit anymore.")
    elif what == "cancel":
        jid = web.parse_int(form.get("id"))
        job = await asyncio.to_thread(job_get, jid) if jid else None
        if job is not None and job.status in ACTIVE:
            await asyncio.to_thread(job_set, jid, status="canceled", finished_at=db.utcnow(),
                                    msg="Canceled by the owner.")
            shutil.rmtree(job_dir(jid), ignore_errors=True)
            web.flash(request, "Video canceled.")
    elif what == "clear-error":
        await asyncio.to_thread(db.set_setting, "ae_err", "")
    return web.redirect("/autoedit")


async def admin_test_voice(request):
    """POST JSON {text, voice}: a short sample so the owner can hear the voice model before members use it."""
    from starlette.responses import JSONResponse, Response
    web = _web()
    if not web.authed(request):
        return JSONResponse({"ok": False}, status_code=401)
    if not web._same_site_json(request):
        return JSONResponse({"ok": False}, status_code=400)
    try:
        f = json.loads(await request.body())
    except ValueError:
        return JSONResponse({"ok": False}, status_code=400)
    text = _clean(f.get("text"), 300) or "Hello! This is how the narration will sound."
    voice = f.get("voice") if f.get("voice") in dict(VOICES) else "onyx"
    if not _key():
        return JSONResponse({"ok": False, "msg": "Add POLLINATIONS_KEY on Render first."})
    try:
        data = await speech(text, voice, setting("ae_voice_model"), tries=1)
    except Stop as e:
        return JSONResponse({"ok": False, "msg": str(e)})
    return Response(content=data, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


def routes() -> list:
    from starlette.routing import Route
    return [
        Route("/watch/autoedit/start", member_start, methods=["POST"]),
        Route("/watch/autoedit/jobs", member_jobs),
        Route("/watch/autoedit/act", member_action, methods=["POST"]),
        Route("/watch/autoedit/key", member_key, methods=["POST"]),
        Route("/watch/autoedit/file/{jid}/{name}", member_file),
        Route("/autoedit", admin_page),
        Route("/autoedit/save", admin_save, methods=["POST"]),
        Route("/autoedit/test-voice", admin_test_voice, methods=["POST"]),
    ]
