"""App configuration from environment."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Repo root = parent of app/
REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

DEFAULTS_DIR = REPO_ROOT / "defaults"
STEP_PROMPTS_PATH = DEFAULTS_DIR / "step_prompts.yaml"
SKILL_PATH = REPO_ROOT / "SKILL.md"
COMPRESS_SCRIPT = REPO_ROOT / "cmprssRally.sh"
COMPRESS_SCRIPT_IOS = REPO_ROOT / "cmprssRally-iOS.sh"


def _bucket_from_arn(raw: str) -> str:
    raw = (raw or "").strip().strip('"').strip("'")
    if raw.startswith("arn:aws:s3:::"):
        return raw.split(":::", 1)[1].split("/", 1)[0]
    if raw.startswith("s3://"):
        return raw[5:].split("/", 1)[0]
    return raw


def _path(env_key: str, default: Path) -> Path:
    raw = os.getenv(env_key)
    if not raw:
        return default
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (REPO_ROOT / p).resolve()
    return p


OUTPUT_DIR = _path("RALLY_OUTPUT_DIR", REPO_ROOT / "rallies")
HOST = os.getenv("RALLY_HOST", "127.0.0.1")
# 8070 leaves 8080 free for another local app.
PORT = int(os.getenv("RALLY_PORT", "8070"))
# stub | cli (subscription via `grok login`) | api (XAI_API_KEY billing)
GENERATE_BACKEND = os.getenv("RALLY_GENERATE_BACKEND", "cli").lower()

XAI_API_KEY = os.getenv("XAI_API_KEY", "")
S3_BUCKET = os.getenv("RALLY_S3_BUCKET", "")
S3_PREFIX = os.getenv("RALLY_S3_PREFIX", "rallies/")
AWS_PROFILE = os.getenv("AWS_PROFILE", "")
AWS_REGION = os.getenv("AWS_REGION", "")

# DutyCall app drive (free / premium / private rallies)
DUTYCALL_S3_BUCKET = _bucket_from_arn(
    os.getenv("AWS_DUTYCALL_S3_BUCKET_ARN")
    or os.getenv("DUTYCALL_S3_BUCKET")
    or ""
)
# Cognito-style identity folder under private/
DUTYCALL_PRIVATE_FOLDER = (
    os.getenv("AWS_DUTYCALL_PRIVATE_FOLDER")
    or os.getenv("DUTYCALL_PRIVATE_FOLDER")
    or ""
).strip().strip('"').strip("'")

# Imagine models / quality (API backend)
IMAGE_MODEL = os.getenv("RALLY_IMAGE_MODEL", "grok-imagine-image-quality")
VIDEO_MODEL = os.getenv("RALLY_VIDEO_MODEL", "grok-imagine-video-1.5")
IMAGE_RESOLUTION = os.getenv("RALLY_IMAGE_RESOLUTION", "1k")  # 1k | 2k
VIDEO_RESOLUTION = os.getenv("RALLY_VIDEO_RESOLUTION", "720p")  # 480p | 720p
VIDEO_DURATION_DEFAULT = int(os.getenv("RALLY_VIDEO_DURATION", "6"))
# Grok chat model for concept → per-step beats (not Imagine)
TEXT_MODEL = os.getenv("RALLY_TEXT_MODEL", "grok-4.6")

# Grok CLI backend (uses ~/.grok/auth.json session)
GROK_CLI_BIN = os.getenv("RALLY_GROK_BIN", "")  # empty → PATH lookup
GROK_CLI_MAX_TURNS = int(os.getenv("RALLY_GROK_MAX_TURNS", "12"))
GROK_CLI_TIMEOUT_IMAGE = float(os.getenv("RALLY_GROK_TIMEOUT_IMAGE", "300"))
GROK_CLI_TIMEOUT_VIDEO = float(os.getenv("RALLY_GROK_TIMEOUT_VIDEO", "600"))

# Video always uses Imagine REST + S3 staging (see app/pipeline/video_gen.py).
# Auth: XAI_API_KEY if set, else grok login session.
