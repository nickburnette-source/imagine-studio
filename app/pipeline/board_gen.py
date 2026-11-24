"""Generate a still or a take from the timeline and file it in the library."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path

from PIL import Image, ImageDraw

from app.config import GENERATE_BACKEND
from app.models.rally import RallyPackage
from app.pipeline import board, library
from app.pipeline.board import compile_prompt, plan_media

Progress = Callable[[str], Awaitable[None] | None]


async def _note(progress: Progress | None, message: str) -> None:
    if progress is None:
        return
    result = progress(message)
    if asyncio.iscoroutine(result):
        await result


def _size(aspect: str) -> tuple[int, int]:
    if aspect == "1:1":
        return 960, 960
    if aspect == "9:16":
        return 720, 1280
    return 1280, 720


def _placeholder_image(path: Path, *, title: str, subtitle: str, aspect: str) -> None:
    w, h = _size(aspect)
    img = Image.new("RGB", (w, h), (12, 22, 36))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((28, 28, w - 28, h - 28), radius=28, outline=(80, 190, 255), width=4)
    draw.text((48, h // 2 - 24), title[:40], fill=(190, 230, 255))
    y = h // 2 + 16
    for line in _wrap(subtitle, 42)[:6]:
        draw.text((48, y), line, fill=(140, 170, 190))
        y += 22
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")


def _wrap(text: str, width: int) -> list[str]:
    words = (text or "").split()
    lines: list[str] = []
    cur = ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if len(trial) > width and cur:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines or [""]


def _placeholder_video(path: Path, still: Path | None, duration: float, aspect: str) -> None:
    import shutil
    import subprocess

    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not on PATH, so a preview take cannot be made")
    w, h = _size(aspect)
    w -= w % 2
    h -= h % 2
    seconds = max(1.0, min(float(duration), 3.0))
    if still is not None and still.is_file():
        cmd = [
            ffmpeg, "-y", "-loop", "1", "-i", str(still),
            "-t", str(seconds),
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(path),
        ]
    else:
        cmd = [
            ffmpeg, "-y",
            "-f", "lavfi", "-i", f"color=c=0x0c1624:s={w}x{h}:d={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(path),
        ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not path.is_file():
        tail = (result.stderr or "")[-400:]
        raise RuntimeError(f"Preview take failed: {tail}")


def _open(name: str) -> RallyPackage:
    from app.config import OUTPUT_DIR

    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError("Unknown project")
    root = (OUTPUT_DIR / name).resolve()
    if OUTPUT_DIR.resolve() not in root.parents or not (root / "meta.json").is_file():
        raise ValueError("Unknown project")
    return RallyPackage.load(root)


def _remember_prompt(pkg: RallyPackage, prompt: str, rel: str) -> None:
    if "Rally" not in pkg.steps:
        return
    step = pkg.step("Rally")
    step.composed_prompt = prompt.strip()
    step.custom_guidance = prompt.strip()
    step.options["last_sent_prompt"] = prompt.strip()
    if rel not in step.outputs:
        step.outputs.append(rel)


async def generate_still(
    name: str,
    *,
    prompt: str,
    ref_ids: list[str],
    progress: Progress | None = None,
) -> None:
    prompt = " ".join((prompt or "").split())
    if not prompt:
        raise ValueError("Write a prompt for the still")
    pkg = _open(name)
    assets = {row["id"]: row for row in library.catalog(pkg)}
    refs: list[Path] = []
    for ref_id in ref_ids:
        row = assets.get(str(ref_id))
        path = (row or {}).get("path")
        if row and row.get("kind") == "still" and isinstance(path, Path) and path.is_file():
            if path not in refs:
                refs.append(path)
        if len(refs) >= 5:
            break
    aspect = pkg.aspect_ratio or "9:16"
    tmp = library.library_dir(pkg) / ".still-tmp.png"
    await _note(progress, "Generating still")
    try:
        if GENERATE_BACKEND == "stub":
            _placeholder_image(tmp, title="Still", subtitle=prompt, aspect=aspect)
        else:
            from app.pipeline.imagine import generate_image

            await generate_image(
                prompt=prompt,
                dest=tmp,
                aspect_ratio=aspect,
                reference_paths=refs or None,
            )
        rows = list(assets.values())
        made = library.ingest(
            pkg,
            tmp,
            name=library.next_name(rows, "still", "Still"),
            kind="still",
            move=True,
        )
    finally:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
    pkg = _open(name)
    fresh = {row["id"]: row for row in library.catalog(pkg)}
    state = board.ensure_board(pkg, list(fresh.values()))
    state["stills"].append({"id": secrets.token_hex(4), "asset_id": made["id"]})
    state["flash"] = made["id"]
    state["error"] = ""
    pkg.board = state
    pkg.save()
    await _note(progress, "")


async def generate_take(name: str, progress: Progress | None = None) -> None:
    pkg = _open(name)
    assets = {row["id"]: row for row in library.catalog(pkg)}
    state = board.ensure_board(pkg, list(assets.values()))
    aspect = pkg.aspect_ratio or "9:16"
    plan = plan_media(state, assets)
    prompt = compile_prompt(state, assets, aspect=aspect, concept=pkg.concept or "")
    if plan["first"] is None and not plan["references"] and not plan["voices"]:
        raise ValueError("Add a still, a reference, or a voice line first")
    from app.imagine_settings import take_duration_choices

    choices = take_duration_choices(reference=bool(plan["references"]))
    duration = int(state["duration"])
    if duration not in choices:
        duration = choices[-1]
        state["duration"] = duration
        prompt = compile_prompt(state, assets, aspect=aspect, concept=pkg.concept or "")
        await _note(progress, f"Duration set to {duration}s for this model")
    tmp = library.library_dir(pkg) / ".take-tmp.mp4"
    await _note(progress, "Generating take")
    try:
        if GENERATE_BACKEND == "stub":
            _placeholder_video(tmp, plan["first"], duration, aspect)
        else:
            from app.pipeline import video_gen

            await video_gen.generate_video_from_image(
                prompt=prompt,
                image_path=plan["first"],
                dest=tmp,
                duration=duration,
                aspect_ratio=aspect,
                last_frame_path=plan["last"],
                voice_ids=plan["voices"] or None,
                reference_paths=plan["references"] or None,
                keyframes=plan["keyframes"] or None,
                progress=progress,
            )
        rows = list(assets.values())
        made = library.ingest(
            pkg,
            tmp,
            name=library.next_name(rows, "video", "Take"),
            kind="video",
            move=True,
        )
    finally:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
    pkg = _open(name)
    fresh = {row["id"]: row for row in library.catalog(pkg)}
    state = board.ensure_board(pkg, list(fresh.values()))
    state["pending"] = made["id"]
    state["flash"] = made["id"]
    state["error"] = ""
    state["duration"] = duration
    _remember_prompt(pkg, prompt, made["rel"])
    pkg.board = state
    pkg.save()
    await _note(progress, "")


async def generate_redo(
    name: str,
    *,
    asset_id: str,
    prompt: str,
    hold: str,
    duration: int,
    progress: Progress | None = None,
) -> None:
    """A new take from one saved cut. Held frames come from that cut."""
    prompt = " ".join((prompt or "").split())
    if not prompt:
        raise ValueError("Write what this clip should become")
    if hold not in {"first", "last", "both", "none"}:
        hold = "both"
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        duration = 6
    duration = max(1, min(15, duration))
    pkg = _open(name)
    assets = {row["id"]: row for row in library.catalog(pkg)}
    asset = assets.get(str(asset_id or ""))
    path = (asset or {}).get("path")
    if not asset or asset.get("kind") != "video" or not isinstance(path, Path) or not path.is_file():
        raise ValueError("Pick a take to regenerate")
    aspect = pkg.aspect_ratio or "9:16"
    folder = library.library_dir(pkg)
    first_png = folder / ".redo-first.png"
    last_png = folder / ".redo-last.png"
    tmp = folder / ".redo-tmp.mp4"
    await _note(progress, "Regenerating take")
    try:
        from app.pipeline.frames import extract_first_frame, extract_last_frame

        image = None
        last = None
        if hold in {"first", "both"}:
            extract_first_frame(path, first_png)
            image = first_png
        if hold in {"last", "both"}:
            extract_last_frame(path, last_png)
            if image is None:
                image = last_png
            else:
                last = last_png
        if GENERATE_BACKEND == "stub":
            _placeholder_video(tmp, image, duration, aspect)
        else:
            from app.pipeline import video_gen

            await video_gen.generate_video_from_image(
                prompt=prompt,
                image_path=image,
                dest=tmp,
                duration=duration,
                aspect_ratio=aspect,
                last_frame_path=last,
                progress=progress,
            )
        rows = list(assets.values())
        made = library.ingest(
            pkg,
            tmp,
            name=library.next_name(rows, "video", "Take"),
            kind="video",
            move=True,
        )
    finally:
        for leftover in (first_png, last_png, tmp):
            if leftover.is_file():
                leftover.unlink(missing_ok=True)
    pkg = _open(name)
    fresh = {row["id"]: row for row in library.catalog(pkg)}
    state = board.ensure_board(pkg, list(fresh.values()))
    state["pending"] = made["id"]
    state["flash"] = made["id"]
    state["error"] = ""
    _remember_prompt(pkg, prompt, made["rel"])
    pkg.board = state
    pkg.save()
    await _note(progress, "")
