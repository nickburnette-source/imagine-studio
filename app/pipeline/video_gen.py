"""
Image→video for ZDR: mint S3 put_url, call Imagine REST, download result.

Auth precedence:
  1. XAI_API_KEY (API billing)
  2. grok login session token (~/.grok/auth.json) — subscription path
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from app.config import VIDEO_DURATION_DEFAULT
from app import imagine_settings
from app.pipeline.auth_token import resolve_bearer_token

API_BASE = "https://api.x.ai/v1"

ProgressFn = Callable[[str], Any] | None


def _clamp_duration(seconds: int | float | None) -> int:
    d = int(seconds if seconds is not None else VIDEO_DURATION_DEFAULT)
    return max(1, min(15, d))


def _normalize_aspect(aspect: str | None) -> str:
    a = (aspect or "9:16").strip()
    allowed = {"1:1", "3:4", "4:3", "9:16", "16:9", "2:3", "3:2"}
    return a if a in allowed else "9:16"


# Video 1.5 cannot edit/extend an existing take. Classic can.
EDIT_VIDEO_MODEL = "grok-imagine-video"
EDIT_MAX_SECONDS = 8.7


def _path_to_data_url(path: Path) -> str:
    raw = path.read_bytes()
    mime, _ = mimetypes.guess_type(str(path))
    if not mime:
        ext = path.suffix.lower()
        if ext == ".png":
            mime = "image/png"
        elif ext in {".mp4", ".m4v", ".mov"}:
            mime = "video/mp4"
        elif ext == ".webm":
            mime = "video/webm"
        else:
            mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _video_duration_seconds(video: Path) -> float:
    import shutil
    import subprocess

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(video),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float((result.stdout or "").strip() or "0")
    except ValueError:
        return 0.0


def _trim_for_edit(video: Path) -> Path:
    """Imagine edits cap input at 8.7s. Copy/trim to a sibling if needed."""
    import shutil
    import subprocess

    dur = _video_duration_seconds(video)
    if dur <= EDIT_MAX_SECONDS + 0.05:
        return video
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError(
            f"{video.name} is {dur:.1f}s — an edit needs ≤ {EDIT_MAX_SECONDS}s "
            "(and ffmpeg to trim)"
        )
    dest = video.with_name(video.stem + ".edit-src.mp4")
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video),
            "-t",
            f"{EDIT_MAX_SECONDS:.2f}",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(dest),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not dest.is_file() or dest.stat().st_size < 1000:
        err = (result.stderr or result.stdout or "")[-400:]
        raise RuntimeError(f"Could not trim {video.name} for edit:\n{err}")
    return dest


def content_bucket_configured() -> bool:
    try:
        from content_store.config import CONTENT_S3_BUCKET

        return bool(CONTENT_S3_BUCKET)
    except Exception:  # noqa: BLE001
        return False


def require_content_bucket() -> str:
    from content_store.config import require_bucket

    return require_bucket()


async def generate_video_from_image(
    *,
    prompt: str,
    image_path: Path | None = None,
    dest: Path,
    duration: int | float | None = None,
    aspect_ratio: str = "9:16",
    model: str | None = None,
    resolution: str | None = None,
    progress: ProgressFn = None,
    keep_staging: bool = False,
    last_frame_path: Path | None = None,
    voice_ids: list[str] | None = None,
    reference_paths: list[Path] | None = None,
    keyframes: list[tuple[Path, float]] | None = None,
) -> Path:
    """
    Animate via Imagine + S3 staging (ZDR-safe).

    ``image_path`` pins frame 1 (image-to-video). ``reference_paths`` are
    character/look refs and do not lock the opening unless ``image_path``
    is also set.
    """
    refs = [p for p in (reference_paths or []) if p.is_file()][:7]
    if image_path is not None and not image_path.is_file():
        raise FileNotFoundError(f"Keyframe image missing: {image_path}")
    if not content_bucket_configured():
        raise RuntimeError(
            "CONTENT_S3_BUCKET (or AWS_S3_BUCKET_ARN) is required for video "
            "under ZDR. Set it in .env — see content_store/README.md."
        )

    from content_store import s3 as s3lib

    async def emit(msg: str) -> None:
        if not progress:
            return
        result = progress(msg)
        if asyncio.iscoroutine(result):
            await result

    token, auth_src = resolve_bearer_token()
    await emit(f"Video auth: {auth_src}")

    from content_store.config import normalize_prefix

    # Nest under video/ for easier lifecycle browsing
    key = s3lib.make_key(ext="mp4", prefix=normalize_prefix() + "video/")
    put_url = s3lib.presign_put(key, content_type="video/mp4")
    await emit(f"Staging put_url → s3://…/{key}")

    chosen = model or imagine_settings.video_model()
    res = resolution or imagine_settings.video_resolution()
    if refs and res == "1080p":
        res = "720p"
        await emit("Look refs cap resolution at 720p")
    body: dict = {
        "model": chosen,
        "prompt": prompt,
        "duration": _clamp_duration(duration),
        "aspect_ratio": _normalize_aspect(aspect_ratio),
        "resolution": res,
        "output": {"upload_url": put_url},
    }
    if image_path is not None and image_path.is_file():
        body["image"] = {"url": _path_to_data_url(image_path)}
    if refs:
        if "1.5" not in str(chosen):
            raise RuntimeError(
                "Character refs need grok-imagine-video-1.5 "
                "(gear → Video model)."
            )
        body["reference_images"] = [
            {"url": _path_to_data_url(p)} for p in refs
        ]
        await emit(
            "Look refs (not frame 1): "
            + ", ".join(p.name for p in refs)
        )
    if last_frame_path is not None and last_frame_path.is_file():
        body["last_frame"] = {"url": _path_to_data_url(last_frame_path)}
    if keyframes:
        body["keyframes"] = [
            {
                "image": {"url": _path_to_data_url(path)},
                "timestamp_s": float(stamp),
            }
            for path, stamp in keyframes
            if Path(path).is_file()
        ]
    if voice_ids:
        body["reference_audios"] = [
            {"voice_id": v} for v in voice_ids if v
        ]
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    timeout = httpx.Timeout(120.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        await emit("Starting Imagine video generation…")
        start = await http.post(
            f"{API_BASE}/videos/generations",
            headers=headers,
            json=body,
        )
        if start.status_code >= 400:
            detail = start.text[:800]
            raise RuntimeError(
                f"Video start failed HTTP {start.status_code}: {detail}"
            )
        request_id = start.json().get("request_id")
        if not request_id:
            raise RuntimeError(f"Video start missing request_id: {start.text[:400]}")

        await emit(f"request_id={request_id} — polling…")
        deadline = time.monotonic() + 600  # 10 min
        last_progress = -1
        while time.monotonic() < deadline:
            poll = await http.get(
                f"{API_BASE}/videos/{request_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if poll.status_code >= 400:
                raise RuntimeError(
                    f"Video poll failed HTTP {poll.status_code}: {poll.text[:500]}"
                )
            data = poll.json()
            status = data.get("status")
            prog = data.get("progress")
            if prog is not None and prog != last_progress:
                last_progress = prog
                await emit(f"Video progress {prog}%")

            if status == "done":
                await emit("Imagine done — downloading from S3…")
                break
            if status in ("failed", "expired"):
                err = data.get("error") or data
                raise RuntimeError(f"Video generation {status}: {err}")
            await asyncio.sleep(4)
        else:
            raise TimeoutError(f"Video generation timed out (request_id={request_id})")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # boto3 download is sync
    await asyncio.to_thread(s3lib.download, key, dest)
    if not dest.is_file() or dest.stat().st_size < 1000:
        raise RuntimeError(f"Downloaded video looks empty: {dest}")

    await emit(f"Saved {dest.name} ({dest.stat().st_size // 1024} KB)")

    if not keep_staging:
        try:
            await asyncio.to_thread(s3lib.delete, key)
        except Exception:  # noqa: BLE001 — cleanup best-effort
            pass

    return dest


async def edit_video(
    *,
    prompt: str,
    video_path: Path,
    dest: Path,
    progress: ProgressFn = None,
    keep_staging: bool = False,
    model: str | None = None,
) -> Path:
    """Delta-edit an existing take. Prompt is what changes, not a rewrite.

    Imagine video edits only work on ``grok-imagine-video`` (Classic), not 1.5.
    Input is capped at 8.7s; longer takes are trimmed for the request.
    """
    if not video_path.is_file():
        raise FileNotFoundError(f"Take missing: {video_path}")
    if not content_bucket_configured():
        raise RuntimeError(
            "CONTENT_S3_BUCKET is required for video under ZDR."
        )
    from content_store import s3 as s3lib
    from content_store.config import normalize_prefix

    async def emit(msg: str) -> None:
        if not progress:
            return
        result = progress(msg)
        if asyncio.iscoroutine(result):
            await result

    # Video 1.5 has no source-video input. Always edit on Classic.
    chosen = EDIT_VIDEO_MODEL
    if model and "1.5" in model:
        await emit(
            "Edit uses grok-imagine-video (Classic) — "
            "Video 1.5 cannot edit an existing take"
        )
    elif "1.5" in imagine_settings.video_model():
        await emit(
            "Edit uses grok-imagine-video (Classic) — "
            "Video 1.5 cannot edit an existing take"
        )

    src = await asyncio.to_thread(_trim_for_edit, video_path)
    if src.resolve() != video_path.resolve():
        await emit(
            f"{video_path.name} is longer than {EDIT_MAX_SECONDS}s — "
            "sending the first 8.7s to Imagine"
        )

    token, auth_src = resolve_bearer_token()
    await emit(f"Video auth: {auth_src}")
    key = s3lib.make_key(ext="mp4", prefix=normalize_prefix() + "video/")
    put_url = s3lib.presign_put(key, content_type="video/mp4")
    body = {
        "model": chosen,
        "prompt": prompt,
        "video": {"url": _path_to_data_url(src)},
        "output": {"upload_url": put_url},
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    timeout = httpx.Timeout(180.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        await emit(f"Starting Imagine video edit ({chosen})…")
        start = await http.post(
            f"{API_BASE}/videos/edits",
            headers=headers,
            json=body,
        )
        if start.status_code >= 400:
            detail = start.text[:800]
            low = detail.lower()
            if "not support" in low:
                raise RuntimeError(
                    "Imagine refused the edit. Edits only work with "
                    "grok-imagine-video (Classic), clips ≤ 8.7s. "
                    f"API: {detail}"
                )
            raise RuntimeError(
                f"Video edit failed HTTP {start.status_code}: {detail}"
            )
        request_id = start.json().get("request_id")
        if not request_id:
            raise RuntimeError(f"Edit missing request_id: {start.text[:400]}")
        await emit(f"request_id={request_id} — polling…")
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            poll = await http.get(
                f"{API_BASE}/videos/{request_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            if poll.status_code >= 400:
                raise RuntimeError(
                    f"Edit poll failed HTTP {poll.status_code}: {poll.text[:500]}"
                )
            data = poll.json()
            status = data.get("status")
            if status == "done":
                break
            if status in ("failed", "expired"):
                raise RuntimeError(
                    f"Video edit {status}: {data.get('error') or data}"
                )
            await asyncio.sleep(4)
        else:
            raise TimeoutError(f"Video edit timed out (request_id={request_id})")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(s3lib.download, key, dest)
    if not dest.is_file() or dest.stat().st_size < 1000:
        raise RuntimeError(f"Edited video looks empty: {dest}")
    await emit(f"Saved {dest.name} ({dest.stat().st_size // 1024} KB)")
    if src.resolve() != video_path.resolve():
        src.unlink(missing_ok=True)
    if not keep_staging:
        try:
            await asyncio.to_thread(s3lib.delete, key)
        except Exception:  # noqa: BLE001
            pass
    return dest

