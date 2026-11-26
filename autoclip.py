"""Free member Auto Clip beta: split a source video into clean, watermark-free MP4 clips."""
import asyncio
import hashlib
import json
import tempfile
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
MAX_UPLOAD = 1024 * 1024 * 1024
MAX_SOURCE_SECONDS = 600
ALLOWED = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def _user_dir(username: str) -> Path:
    key = hashlib.sha256(username.encode("utf-8")).hexdigest()[:24]
    path = ROOT / key
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_path(folder: Path, job_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("Invalid job ID.")
    return folder / f"job_{job_id}.json"


def _save_job(folder: Path, job_id: str, data: dict) -> None:
    path = _job_path(folder, job_id)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temp, path)


def _storage_used(folder: Path | None = None) -> int:
    base = folder if folder is not None else ROOT
    if not base.exists():
        return 0
    total = 0
    for p in base.rglob("*"):
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


def _highlight_starts(source: Path, duration: float, clip_seconds: int) -> list:
    """Choose up to three energetic scene-change windows using FFmpeg only.

    This is lightweight activity-based selection, not semantic AI understanding.
    """
    scan = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(source), "-an",
         "-vf", "select='gt(scene,0.22)',showinfo", "-vsync", "vfr",
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=150
    )
    cuts = []
    for match in re.finditer(r"pts_time:([0-9]+(?:\\.[0-9]+)?)", scan.stderr or ""):
        try:
            cuts.append(float(match.group(1)))
        except ValueError:
            pass

    length = min(float(clip_seconds), duration)
    if duration <= length:
        return [0.0]
    step = max(2.0, min(5.0, length / 4.0))
    candidates = []
    start = 0.0
    while start + length <= duration + 0.01:
        end = start + length
        # Give more weight to cuts near the middle, while counting all cuts in the window.
        score = sum(1.0 + max(0.0, 1.0 - abs(t - (start + length / 2)) / (length / 2)) for t in cuts if start <= t <= end)
        candidates.append((score, start))
        start += step

    chosen = []
    for score, start in sorted(candidates, key=lambda item: (item[0], -item[1]), reverse=True):
        if all(abs(start - picked) >= length * 0.8 for picked in chosen):
            chosen.append(start)
        if len(chosen) == 3:
            break
    if len(chosen) < min(3, len(candidates)):
        for _, start in sorted(candidates, key=lambda item: item[1]):
            if all(abs(start - picked) >= length * 0.8 for picked in chosen):
                chosen.append(start)
            if len(chosen) == 3:
                break
    if not chosen:
        chosen = [max(0.0, (duration - length) / 2)]
    return sorted(chosen)


def _process(source: Path, folder: Path, orientation: str, clip_seconds: int, progress=None) -> list:
    duration = _duration(source)
    if duration <= 0 or duration > MAX_SOURCE_SECONDS:
        raise ValueError("Hanggang 10 minutes lang ang source video sa free beta.")
    width, height = (1080, 1920) if orientation == "portrait" else (1920, 1080)
    starts = _highlight_starts(source, duration, clip_seconds)
    outputs = []
    total = len(starts)
    for index, start in enumerate(starts, 1):
        length = min(float(clip_seconds), duration - start)
        if length < 3:
            continue
        name = f"clip_{uuid.uuid4().hex[:12]}.mp4"
        target = folder / name
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
              f"crop={width}:{height},fps=60,format=yuv420p")
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{length:.3f}",
               "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-threads", "2", "-c:a", "aac", "-b:a", "160k",
               "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(target)]
        if progress:
            progress({"stage": "processing", "current": index, "total": total, "percent": int(((index - 1) / total) * 90) + 5})
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as errfile:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errfile,
                                    text=True, bufsize=1)
            try:
                for line in proc.stdout:
                    if not progress:
                        continue
                    key, sep, value = line.strip().partition("=")
                    if sep and key == "out_time_ms":
                        try:
                            elapsed = int(value) / 1000000
                            clip_pct = min(99.0, max(0.0, elapsed / max(length, 0.1) * 100))
                            overall = 5 + ((index - 1 + clip_pct / 100) / total) * 90
                            progress({"stage": "processing", "current": index, "total": total,
                                      "percent": round(overall, 1)})
                        except ValueError:
                            pass
                code = proc.wait(timeout=600)
            except Exception:
                proc.kill()
                proc.wait()
                raise
            errfile.seek(0)
            error_text = errfile.read()
        if code != 0 or not target.exists() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)
            raise RuntimeError((error_text or "FFmpeg export failed")[-700:])
        outputs.append({"name": name, "url": f"/watch/autoclip/file/{name}",
                        "seconds": round(length, 1), "size": target.stat().st_size,
                        "start_seconds": round(start, 1)})
        if progress:
            progress({"stage": "processing", "current": index, "total": total,
                      "percent": round(5 + index / total * 90, 1)})
        if _storage_used(folder) > MAX_STORAGE:
            for item in outputs:
                (folder / item["name"]).unlink(missing_ok=True)
            raise ValueError("Naabot na ang 1 GB storage cap. I-download at burahin muna ang lumang clips.")
    return outputs


def _run_job(job_id: str, source: Path, folder: Path, orientation: str, clip_seconds: int):
    try:
        def update(values):
            try:
                current = json.loads(_job_path(folder, job_id).read_text(encoding="utf-8"))
                current.update(values)
                _save_job(folder, job_id, current)
            except Exception:
                pass
        update({"stage": "processing", "percent": 1})
        clips = _process(source, folder, orientation, clip_seconds, progress=update)
        update({"ok": True, "done": True, "stage": "done", "percent": 100,
                "clips": clips,
                "message": f"Tapos na! {len(clips)} clip(s) ang nagawa. Walang watermark."})
    except Exception as exc:
        try:
            update({"ok": False, "done": True, "stage": "error",
                    "message": f"Processing failed: {str(exc)[:400]}"})
        except Exception:
            pass
    finally:
        source.unlink(missing_ok=True)


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
    if _storage_used(_user_dir(username)) >= MAX_STORAGE:
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
                    raise ValueError("Maximum upload size ay 1 GB kahit free beta.")
                if _storage_used(folder) + len(chunk) > MAX_STORAGE:
                    raise ValueError("Kulang na ang available space sa 1 GB storage cap.")
                out.write(chunk)
        if size == 0:
            raise ValueError("Empty o invalid ang video file.")
        job_id = uuid.uuid4().hex
        _save_job(folder, job_id, {"done": False, "ok": True, "stage": "queued",
                                   "current": 0, "total": 0, "percent": 0})
        asyncio.create_task(asyncio.to_thread(_run_job, job_id, source, folder, orientation, clip_seconds))
        return JSONResponse({"ok": True, "job_id": job_id, "message": "Upload complete. Processing started."})
    except ValueError as exc:
        source.unlink(missing_ok=True)
        return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)
    except Exception as exc:
        source.unlink(missing_ok=True)
        return JSONResponse({"ok": False, "message": f"Upload failed: {str(exc)[:400]}"}, status_code=500)


async def job_status(request):
    username = request.session.get("member")
    if not username:
        return JSONResponse({"ok": False, "message": "Mag-login muna."}, status_code=401)
    job_id = request.path_params.get("job_id", "")
    try:
        path = _job_path(_user_dir(username), job_id)
        job = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError, json.JSONDecodeError):
        return JSONResponse({"ok": False, "message": "Processing job not found. Pakisubukang muli."}, status_code=404)
    return JSONResponse(job)


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
    import hmac
    expected = request.session.get("csrf", "")
    supplied = request.headers.get("x-csrf-token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        return JSONResponse({"ok": False, "message": "Bad CSRF token."}, status_code=400)
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
        Route("/watch/autoclip/status/{job_id}", job_status),
        Route("/watch/autoclip/delete/{filename}", delete_clip, methods=["POST"]),
    ]
