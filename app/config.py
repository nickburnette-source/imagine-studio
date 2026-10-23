"""App configuration from the environment."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Repo root = parent of app/
REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


def _env(*keys: str, default: str = "") -> str:
    for key in keys:
        raw = (os.getenv(key) or "").strip()
        if raw:
            return raw
    return default


def _path(env_key: str, default: Path) -> Path:
    raw = os.getenv(env_key)
    if not raw:
        return default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    return path


OUTPUT_DIR = _path("IMAGINE_OUTPUT_DIR", REPO_ROOT / "projects")
HOST = _env("IMAGINE_HOST", default="127.0.0.1")
PORT = int(_env("IMAGINE_PORT", default="8070"))
# stub | cli (grok login session) | api (XAI_API_KEY)
GENERATE_BACKEND = _env(
    "IMAGINE_GENERATE_BACKEND", "RALLY_GENERATE_BACKEND", default="cli"
).lower()

XAI_API_KEY = os.getenv("XAI_API_KEY", "")

IMAGE_MODEL = _env(
    "IMAGINE_IMAGE_MODEL", "RALLY_IMAGE_MODEL", default="grok-imagine-image-quality"
)
VIDEO_MODEL = _env(
    "IMAGINE_VIDEO_MODEL", "RALLY_VIDEO_MODEL", default="grok-imagine-video-1.5"
)
IMAGE_RESOLUTION = _env(
    "IMAGINE_IMAGE_RESOLUTION", "RALLY_IMAGE_RESOLUTION", default="1k"
).lower()
VIDEO_RESOLUTION = _env(
    "IMAGINE_VIDEO_RESOLUTION", "RALLY_VIDEO_RESOLUTION", default="720p"
).lower()
VIDEO_DURATION_DEFAULT = int(
    _env("IMAGINE_VIDEO_DURATION", "RALLY_VIDEO_DURATION", default="6")
)
