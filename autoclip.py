"""Free member Auto Clip beta: split a source video into clean, watermark-free MP4 clips."""
import asyncio
import hashlib
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import imageio_ffmpeg
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

ROOT = Path(__file__).parent / "static" / "autoclip"
MAX_STORAGE = 1024 * 1024 * 1024
MAX_UPLOAD = 200 * 1024 * 1024
MAX_SOURCE_SECONDS = 600
ALLOWED = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def _user_dir(username: str) -> Path:
    key = hashlib.sha256(username.encode("utf-8")).hexdigest()[:24]
    path = ROOT / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def _storage_used() -> int:
    if not ROOT.exists():
        return 0
    total = 0
    for p in ROOT.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _duration(path: Path) -> float:
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", str(path)], capture_output=True,
                       text=True, timeout=45)
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr or "")
    if not match:
        raise ValueError("Hindi mabasa ang video duration o unsupported ang file.")
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def _process(source: Path, folder: Path, orientation: str, clip_seconds: int) -> list:
    duration = _duration(source)
    if duration <= 0 or duration > MAX_SOURCE_SECONDS:
        raise ValueError("Hanggang 10 minutes lang ang source video sa free beta.")
    if orientation == "portrait":
        width, height = 1080, 1920
    else:
        width, height = 1920, 1080
    outputs = []
    start = 0.0
    index = 1
    while start < duration and index <= 30:
        length = min(float(clip_seconds), duration - start)
        if length < 3:
            break
        name = f"clip_{uuid.uuid4().hex[:12]}.mp4"
        target = folder / name
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
              f"crop={width}:{height},fps=60,format=yuv420p")
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{length:.3f}",
               "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-threads", "2", "-c:a", "aac", "-b:a", "160k",
               "-movflags", "+faststart", str(target)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise RuntimeError((result.stderr or "FFmpeg export failed")[-700:])
        outputs.append({"name": name, "url": f"/watch/autoclip/file/{name}",
                        "seconds": round(length, 1), "size": target.stat().st_size})
        start += clip_seconds
        index += 1
        if _storage_used() > MAX_STORAGE:
            raise ValueError("Naabot na ang 1 GB storage cap. I-download at burahin muna ang lumang clips.")
    return outputs


async def create_clips(request):
    username = request.session.get("member")
    if not username:
        return JSONResponse({"ok": False, "message": "Mag-login muna sa member account."}, status_code=401)
    form = await request.form()
    expected = request.session.get("csrf", "")
    supplied = str(form.get("csrf", ""))
    import hmac
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        return JSONResponse({"ok": False, "message": "Session expired. Reload /watch and try again."}, status_code=400)
    upload = form.get("video")
    if not upload or not getattr(upload, "filename", ""):
        return JSONResponse({"ok": False, "message": "Pumili muna ng video."}, status_code=400)
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in ALLOWED:
        return JSONResponse({"ok": False, "message": "MP4, MOV, M4V, MKV, o WEBM lang ang supported."}, status_code=400)
    orientation = str(form.get("orientation", "portrait"))
    if orientation not in ("portrait", "landscape"):
        orientation = "portrait"
    try:
        clip_seconds = int(form.get("clip_seconds", "30"))
    except (TypeError, ValueError):
        clip_seconds = 30
    if clip_seconds not in (15, 30, 45, 60):
        clip_seconds = 30
    if _storage_used() >= MAX_STORAGE:
        return JSONResponse({"ok": False, "message": "Naabot na ang 1 GB storage limit."}, status_code=507)
    folder = _user_dir(username)
    source = folder / f"source_{uuid.uuid4().hex}{suffix}"
    size = 0
    try:
        with source.open("wb") as out:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD:
                    raise ValueError("Maximum upload size ay 200 MB sa free beta.")
                if _storage_used() + len(chunk) > MAX_STORAGE:
                    raise ValueError("Kulang na ang available space sa 1 GB storage cap.")
                out.write(chunk)
        if size == 0:
            raise ValueError("Empty o invalid ang video file.")
        clips = await asyncio.to_thread(_process, source, folder, orientation, clip_seconds)
        return JSONResponse({"ok": True, "clips": clips,
                             "message": f"Tapos na! {len(clips)} clip(s) ang nagawa. Walang watermark."})
    except ValueError as exc:
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "message": f"Processing failed: {str(exc)[:400]}"}, status_code=500)
    finally:
        source.unlink(missing_ok=True)


async def get_clip(request):
    username = request.session.get("member")
    if not username:
        return JSONResponse({"ok": False}, status_code=401)
    name = request.path_params.get("filename", "")
    if not re.fullmatch(r"clip_[a-f0-9]{12}\.mp4", name):
        return JSONResponse({"ok": False}, status_code=404)
    path = _user_dir(username) / name
    if not path.is_file():
        return JSONResponse({"ok": False, "message": "File not found."}, status_code=404)
    return FileResponse(path, media_type="video/mp4", filename=name,
                        headers={"Cache-Control": "private, no-store"})


async def delete_clip(request):
    username = request.session.get("member")
    if not username:
        return JSONResponse({"ok": False}, status_code=401)
    name = request.path_params.get("filename", "")
    if not re.fullmatch(r"clip_[a-f0-9]{12}\.mp4", name):
        return JSONResponse({"ok": False}, status_code=404)
    path = _user_dir(username) / name
    path.unlink(missing_ok=True)
    return JSONResponse({"ok": True})


def routes():
    return [
        Route("/watch/autoclip/create", create_clips, methods=["POST"]),
        Route("/watch/autoclip/file/{filename}", get_clip),
        Route("/watch/autoclip/delete/{filename}", delete_clip, methods=["POST"]),
    ]
