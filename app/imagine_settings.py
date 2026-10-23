"""User-overridable Imagine model / resolution. Defaults match .env."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import (
    IMAGE_MODEL,
    IMAGE_RESOLUTION,
    OUTPUT_DIR,
    VIDEO_DURATION_DEFAULT,
    VIDEO_MODEL,
    VIDEO_RESOLUTION,
)

SETTINGS_PATH = OUTPUT_DIR / ".imagine-settings.json"

IMAGE_MODELS = {
    "grok-imagine-image-quality": "Quality",
    "grok-imagine-image-2.0": "Image 2.0",
    "grok-imagine-image": "Fast",
}
IMAGE_RESOLUTIONS = {"1k": "1k", "2k": "2k"}
VIDEO_MODELS = {
    "grok-imagine-video-1.5": "Video 1.5",
    "grok-imagine-video": "Classic",
}
VIDEO_RESOLUTIONS = {"480p": "480p", "720p": "720p", "1080p": "1080p"}
VIDEO_DURATIONS = {
    "4": "4s",
    "6": "6s",
    "8": "8s",
    "10": "10s",
    "15": "15s",
}

_KEYS = (
    "image_model",
    "image_resolution",
    "video_model",
    "video_resolution",
    "video_duration",
)


def _defaults() -> dict[str, str]:
    return {
        "image_model": IMAGE_MODEL or "grok-imagine-image-quality",
        "image_resolution": (IMAGE_RESOLUTION or "1k").lower(),
        "video_model": VIDEO_MODEL or "grok-imagine-video-1.5",
        "video_resolution": (VIDEO_RESOLUTION or "720p").lower(),
        "video_duration": str(int(VIDEO_DURATION_DEFAULT or 6)),
    }


def load() -> dict[str, str]:
    data = _defaults()
    if SETTINGS_PATH.is_file():
        try:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict):
            for key in _KEYS:
                val = str(raw.get(key) or "").strip()
                if val:
                    data[key] = val
    data["image_model"] = (
        data["image_model"]
        if data["image_model"] in IMAGE_MODELS
        else _defaults()["image_model"]
    )
    data["image_resolution"] = (
        data["image_resolution"]
        if data["image_resolution"] in IMAGE_RESOLUTIONS
        else "1k"
    )
    data["video_model"] = (
        data["video_model"]
        if data["video_model"] in VIDEO_MODELS
        else _defaults()["video_model"]
    )
    data["video_resolution"] = (
        data["video_resolution"]
        if data["video_resolution"] in VIDEO_RESOLUTIONS
        else "720p"
    )
    data["video_duration"] = (
        data["video_duration"]
        if data["video_duration"] in VIDEO_DURATIONS
        else "6"
    )
    return data


def save(updates: dict[str, Any]) -> dict[str, str]:
    data = load()
    for key in _KEYS:
        if key in updates and updates[key] is not None:
            data[key] = str(updates[key]).strip()
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Re-validate via load after write
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    payload = {k: data[k] for k in _KEYS}
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(SETTINGS_PATH)
    return load()


def image_model() -> str:
    return load()["image_model"]


def image_resolution() -> str:
    return load()["image_resolution"]


def video_model() -> str:
    return load()["video_model"]


def video_resolution() -> str:
    return load()["video_resolution"]


def video_duration() -> int:
    try:
        d = int(load()["video_duration"])
    except (KeyError, TypeError, ValueError):
        d = 6
    return max(1, min(15, d))


def video_model_is_classic(model: str | None = None) -> bool:
    """Classic ``grok-imagine-video`` edits and extends. 1.5 does not."""
    return "1.5" not in (model or video_model())


def take_duration_choices(
    *,
    model: str | None = None,
    reference: bool = False,
) -> list[int]:
    """Whole seconds this Imagine video request can actually use.

    Video 1.5 and Classic both take 1–15. Classic reference-to-video stops
    at 10 seconds.
    """
    if video_model_is_classic(model) and reference:
        return list(range(1, 11))
    return list(range(1, 16))


def take_resolution_choices(
    *,
    model: str | None = None,
    reference: bool = False,
) -> list[str]:
    """Resolutions this request can use.

    Video 1.5 text and image-to-video can be 1080p. Reference images, and
    Classic entirely, stop at 720p.
    """
    if video_model_is_classic(model) or reference:
        return ["480p", "720p"]
    return ["480p", "720p", "1080p"]


def nearest_choice(value: object, choices: list, default: object) -> object:
    """Return ``value`` when it is offered, otherwise the nearest choice."""
    if value in choices:
        return value
    try:
        num = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default if default in choices else choices[0]
    if all(isinstance(c, int) for c in choices):
        return min(choices, key=lambda c: (abs(int(c) - num), -int(c)))
    return default if default in choices else choices[0]
