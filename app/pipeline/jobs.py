"""Run one Imagine generation for a project.

The click handler marks the project running before this task starts.
A failure puts the message on the project and leaves the gallery as it was.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path

from app import imagine_settings
from app.config import GENERATE_BACKEND
from app.models import project as store

_detail: dict[str, str] = {}
_generation: dict[str, int] = {}

_FRAME = {
    "1:1": (720, 720),
    "9:16": (720, 1280),
    "16:9": (1280, 720),
    "4:3": (960, 720),
    "3:4": (720, 960),
    "3:2": (1080, 720),
    "2:3": (720, 1080),
}


def detail(slug: str) -> str:
    return _detail.get(slug, "")


def generation(slug: str) -> int:
    return _generation.get(slug, 0)


def _note(slug: str, message: str) -> None:
    _detail[slug] = message


def _frame(aspect: str) -> tuple[int, int]:
    return _FRAME.get(aspect, (720, 1280))


def _stub_image(dest: Path, aspect: str, prompt: str, *, edited: bool) -> None:
    from PIL import Image, ImageDraw

    width, height = _frame(aspect)
    color = (28, 36, 48) if edited else (18, 20, 24)
    image = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(image)
    draw.text((32, 32), (prompt or "stub")[:240], fill=(230, 230, 230))
    dest.parent.mkdir(parents=True, exist_ok=True)
    image.save(dest, format="PNG")


def _stub_video(dest: Path, aspect: str, duration: int, prompt: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for a stub video")
    width, height = _frame(aspect)
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x141820:s={width}x{height}:r=24:d={duration}",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(dest),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not dest.is_file():
        err = (result.stderr or result.stdout or "")[-400:]
        raise RuntimeError(f"stub video failed: {err}")


def _refs(proj: store.Project) -> list[Path]:
    paths: list[Path] = []
    for rel in proj.refs:
        try:
            path = proj.resolve(rel)
        except ValueError:
            continue
        if path.is_file():
            paths.append(path)
    return paths


def _video_request(proj: store.Project) -> tuple[int, str, bool]:
    """Legal duration and resolution for this shot. Refs cap both."""
    refs = _refs(proj)
    reference = bool(refs)
    durations = imagine_settings.take_duration_choices(reference=reference)
    resolutions = imagine_settings.take_resolution_choices(reference=reference)
    duration = int(
        imagine_settings.nearest_choice(proj.duration, durations, durations[-1])
    )
    resolution = str(
        imagine_settings.nearest_choice(
            proj.resolution, resolutions, resolutions[-1]
        )
    )
    return duration, resolution, reference


async def _produce(slug: str, action: str) -> None:
    proj = store.load(slug)
    prompt = (proj.prompt or "").strip()
    if not prompt:
        raise RuntimeError("Write a prompt first")
    shown = proj.shown_file()
    refs = _refs(proj)
    kind = "image"
    suffix = ".png"

    with tempfile.TemporaryDirectory(prefix="imagine-") as tmp:
        if action == "edit_video":
            if shown is None or proj.shown_kind() != "video":
                raise RuntimeError("Show a video before editing it")
            dest = Path(tmp) / "take.mp4"
            suffix = ".mp4"
            kind = "edit"
            _note(slug, "Editing this clip…")
            if GENERATE_BACKEND == "stub":
                await asyncio.to_thread(_stub_video, dest, proj.aspect_ratio, 2, prompt)
            else:
                from app.pipeline.video_gen import edit_video

                await edit_video(
                    prompt=prompt,
                    video_path=shown,
                    dest=dest,
                    progress=lambda message: _note(slug, message),
                )
        elif action == "video":
            image_path = None
            if proj.pin_still and shown is not None and proj.shown_kind() == "image":
                image_path = shown
            duration, resolution, _reference = _video_request(proj)
            dest = Path(tmp) / "take.mp4"
            suffix = ".mp4"
            kind = "video"
            label = "Generating video…"
            if image_path is not None and refs:
                label = "Generating from this still and the references…"
            elif image_path is not None:
                label = "Animating this still…"
            elif refs:
                label = "Generating from references…"
            _note(slug, label)
            if GENERATE_BACKEND == "stub":
                await asyncio.to_thread(
                    _stub_video, dest, proj.aspect_ratio, duration, prompt
                )
            else:
                from app.pipeline.video_gen import generate_video_from_image

                await generate_video_from_image(
                    prompt=prompt,
                    image_path=image_path,
                    dest=dest,
                    duration=duration,
                    aspect_ratio=proj.aspect_ratio,
                    resolution=resolution,
                    reference_paths=refs or None,
                    progress=lambda message: _note(slug, message),
                )
        else:
            image_path = None
            if action == "edit_image":
                if shown is None or proj.shown_kind() != "image":
                    raise RuntimeError("Show an image before editing it")
                image_path = shown
                kind = "edit"
                _note(slug, "Editing this image…")
            else:
                _note(slug, "Generating image…")
            dest = Path(tmp) / "take.png"
            if GENERATE_BACKEND == "stub":
                await asyncio.to_thread(
                    _stub_image,
                    dest,
                    proj.aspect_ratio,
                    prompt,
                    edited=image_path is not None,
                )
            else:
                from app.pipeline.imagine import generate_image

                await generate_image(
                    prompt=prompt,
                    dest=dest,
                    aspect_ratio=proj.aspect_ratio,
                    image_path=image_path,
                    reference_paths=refs or None,
                )
        if not dest.is_file():
            raise RuntimeError("Imagine returned no file")
        parked = Path(tmp) / f"ready{suffix}"
        if dest != parked:
            shutil.move(dest, parked)
        store.add_take(slug, parked, kind=kind, prompt=prompt)


async def run(slug: str, action: str) -> None:
    try:
        await _produce(slug, action)
    except Exception as exc:  # noqa: BLE001
        store.mark_ready(slug, str(exc))
    else:
        store.mark_ready(slug, None)
    finally:
        _detail.pop(slug, None)
        _generation[slug] = _generation.get(slug, 0) + 1
