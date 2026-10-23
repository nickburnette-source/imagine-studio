"""Extract frames from rally videos for clip-to-clip continuity."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.models.rally import RallyPackage
from app.pipeline.prompts import keyframe_source_for_step, staged_still_name


class FrameError(RuntimeError):
    pass


def extract_first_frame(video: Path, dest: Path) -> Path:
    """Write the first video frame to ``dest`` (png)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FrameError("ffmpeg not found on PATH")
    video = Path(video)
    if not video.is_file():
        raise FrameError(f"Video missing: {video}")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.png")
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video),
            "-vframes",
            "1",
            str(tmp),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
        tmp.replace(dest)
        return dest
    if tmp.is_file():
        tmp.unlink(missing_ok=True)
    err = (result.stderr or result.stdout or "").strip()
    raise FrameError(
        f"Could not extract first frame from {video.name}: {err[-400:]}"
    )


def extract_last_frame(video: Path, dest: Path) -> Path:
    """Write the last video frame to ``dest`` (png)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FrameError("ffmpeg not found on PATH")
    video = Path(video)
    if not video.is_file():
        raise FrameError(f"Video missing: {video}")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.png")
    attempts = [
        [
            ffmpeg,
            "-y",
            "-sseof",
            "-0.05",
            "-i",
            str(video),
            "-vframes",
            "1",
            str(tmp),
        ],
        [
            ffmpeg,
            "-y",
            "-sseof",
            "-1",
            "-i",
            str(video),
            "-vframes",
            "1",
            str(tmp),
        ],
    ]
    last_err = ""
    for cmd in attempts:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
            tmp.replace(dest)
            return dest
        last_err = (result.stderr or result.stdout or "").strip()
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
    raise FrameError(f"Could not extract last frame from {video.name}: {last_err[-400:]}")


def resolve_start_still(
    pkg: RallyPackage,
    step_id: str,
    *,
    extract: bool = True,
) -> Path:
    """
    Still that seeds image-to-video for ``step_id``.

    ``idle_last`` prefers Idle.last.png, then extracts it from Idle.mp4
    (when ``extract``), then IdleKeyframe.png, then Preview.png.
    """
    tid = getattr(pkg, "template", "character") or "character"
    opts = {}
    try:
        opts = pkg.step(step_id).options or {}
    except Exception:  # noqa: BLE001
        opts = {}
    from app.pipeline.explicit_mark import as_imagine_still, preview_source_path
    from app.pipeline.joins import start_join

    custom = str(opts.get("start_file") or "").strip()
    join = start_join(pkg, step_id)
    if join == "still":
        if custom:
            chosen = pkg.path_for(custom)
            if chosen.is_file():
                remapped = as_imagine_still(pkg, chosen)
                return remapped if remapped is not None else chosen
        staged = pkg.path_for(staged_still_name(step_id))
        if staged.is_file():
            return staged
        return preview_source_path(pkg)
    if join == "cut":
        return preview_source_path(pkg)
    src = keyframe_source_for_step(step_id, tid, opts)
    preview = preview_source_path(pkg)
    idle_kf = pkg.path_for("IdleKeyframe.png")
    idle_last = pkg.path_for("Idle.last.png")
    idle_mp4 = pkg.path_for("Idle.mp4")

    if src == "staged":
        staged = pkg.path_for(staged_still_name(step_id))
        if staged.is_file():
            return staged
        return preview
    if src == "preview":
        return preview
    if src == "idle":
        return idle_kf if idle_kf.is_file() else preview
    if src == "idle_last":
        if extract and not idle_last.is_file() and idle_mp4.is_file():
            try:
                extract_last_frame(idle_mp4, idle_last)
            except FrameError:
                pass
        if idle_last.is_file():
            return idle_last
        if idle_kf.is_file():
            return idle_kf
        return preview
    return preview
