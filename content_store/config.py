"""Env config for the content-store API."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load rally-builder .env when present (repo root = parent of content_store/)
_REPO = Path(__file__).resolve().parent.parent
load_dotenv(_REPO / ".env")

def _bucket_from_env() -> str:
    """Accept CONTENT_S3_BUCKET, RALLY_S3_BUCKET, or AWS_S3_BUCKET_ARN."""
    for key in ("CONTENT_S3_BUCKET", "RALLY_S3_BUCKET"):
        raw = (os.getenv(key) or "").strip().strip('"').strip("'")
        if raw:
            return raw
    arn = (os.getenv("AWS_S3_BUCKET_ARN") or "").strip().strip('"').strip("'")
    if arn.startswith("arn:aws:s3:::"):
        return arn.split(":::", 1)[1].split("/", 1)[0]
    if arn.startswith("s3://"):
        return arn[5:].split("/", 1)[0]
    return arn  # bare bucket name if someone put it in the ARN var


CONTENT_S3_BUCKET = _bucket_from_env()
CONTENT_S3_PREFIX = (
    os.getenv("CONTENT_S3_PREFIX") or os.getenv("RALLY_S3_PREFIX") or "staging/"
)
CONTENT_S3_REGION = os.getenv("CONTENT_S3_REGION") or os.getenv("AWS_REGION") or "us-east-1"
# Prefer explicit content profile; else AWS_PROFILE.
# If AWS_SSO_COMMAND mentions --profile X, that is a hint only (not auto-applied).
AWS_PROFILE = (
    os.getenv("CONTENT_AWS_PROFILE")
    or os.getenv("AWS_PROFILE")
    or ""
)

# Presigned URL lifetime (seconds). Keep short — throwaway staging.
CONTENT_URL_TTL = int(os.getenv("CONTENT_URL_TTL", "1800"))  # 30 min

# Local bind
CONTENT_HOST = os.getenv("CONTENT_HOST", "127.0.0.1")
CONTENT_PORT = int(os.getenv("CONTENT_PORT", "8787"))

# Optional shared secret. Empty = open on bind host (ok for localhost).
CONTENT_API_TOKEN = os.getenv("CONTENT_API_TOKEN", "")


def require_bucket() -> str:
    if not CONTENT_S3_BUCKET:
        raise RuntimeError(
            "CONTENT_S3_BUCKET is not set. Add it to .env, e.g.\n"
            "  CONTENT_S3_BUCKET=your-bucket-name\n"
            "  CONTENT_S3_PREFIX=staging/\n"
            "  AWS_REGION=us-east-1"
        )
    return CONTENT_S3_BUCKET


def normalize_prefix(prefix: str | None = None) -> str:
    p = (prefix if prefix is not None else CONTENT_S3_PREFIX).strip()
    if p and not p.endswith("/"):
        p += "/"
    # reject absolute / traversal
    p = p.lstrip("/")
    if ".." in p.split("/"):
        raise ValueError("invalid prefix")
    return p
