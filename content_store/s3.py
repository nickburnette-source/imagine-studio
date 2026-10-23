"""S3 helpers: presign PUT/GET, download, delete, head."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import boto3
from botocore.client import BaseClient
from botocore.config import Config

from content_store.config import (
    AWS_PROFILE,
    CONTENT_S3_REGION,
    CONTENT_URL_TTL,
    normalize_prefix,
    require_bucket,
)


def _sso_hint() -> str:
    profile = AWS_PROFILE or "default"
    return (
        f"AWS SSO token expired for profile '{profile}'. "
        f"In a terminal run:\n"
        f"  aws sso login --profile {profile}\n"
        f"  aws sts get-caller-identity --profile {profile}\n"
        "Then try the take again."
    )


def _reraise_aws(exc: BaseException) -> None:
    text = str(exc)
    if (
        "Token has expired" in text
        or "refresh failed" in text
        or "SSO" in text
        or "sso" in text
    ):
        raise RuntimeError(_sso_hint()) from exc
    raise


def _client() -> BaseClient:
    """Fresh Session each call so a new `aws sso login` is picked up after restart."""
    session_kwargs: dict[str, Any] = {}
    if AWS_PROFILE:
        session_kwargs["profile_name"] = AWS_PROFILE
    try:
        session = boto3.Session(**session_kwargs)
        return session.client(
            "s3",
            region_name=CONTENT_S3_REGION,
            config=Config(signature_version="s3v4"),
        )
    except Exception as exc:  # noqa: BLE001
        _reraise_aws(exc)
        raise  # unreachable


def make_key(*, ext: str = "mp4", prefix: str | None = None, name: str | None = None) -> str:
    """Build a unique object key under the staging prefix."""
    p = normalize_prefix(prefix)
    stem = name or uuid.uuid4().hex
    stem = stem.strip("/").replace("..", "")
    e = ext.lstrip(".") or "bin"
    return f"{p}{stem}.{e}"


def presign_put(
    key: str,
    *,
    content_type: str = "video/mp4",
    expires_in: int | None = None,
) -> str:
    bucket = require_bucket()
    ttl = expires_in if expires_in is not None else CONTENT_URL_TTL
    try:
        return _client().generate_presigned_url(
            "put_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=ttl,
            HttpMethod="PUT",
        )
    except Exception as exc:  # noqa: BLE001
        _reraise_aws(exc)
        raise


def presign_get(key: str, *, expires_in: int | None = None) -> str:
    bucket = require_bucket()
    ttl = expires_in if expires_in is not None else CONTENT_URL_TTL
    try:
        return _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=ttl,
            HttpMethod="GET",
        )
    except Exception as exc:  # noqa: BLE001
        _reraise_aws(exc)
        raise


def head(key: str) -> dict[str, Any] | None:
    from botocore.exceptions import ClientError

    bucket = require_bucket()
    try:
        return _client().head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        http = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ("404", "NoSuchKey", "NotFound") or http == 404:
            return None
        raise


def download(key: str, dest: Path) -> Path:
    bucket = require_bucket()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        _client().download_file(bucket, key, str(dest))
    except Exception as exc:  # noqa: BLE001
        _reraise_aws(exc)
        raise
    return dest


def get_bytes(key: str) -> tuple[bytes, str]:
    bucket = require_bucket()
    resp = _client().get_object(Bucket=bucket, Key=key)
    body = resp["Body"].read()
    ctype = resp.get("ContentType") or "application/octet-stream"
    return body, ctype


def delete(key: str) -> None:
    bucket = require_bucket()
    _client().delete_object(Bucket=bucket, Key=key)


def ensure_bucket_access() -> dict[str, Any]:
    """Cheap connectivity check (HeadBucket)."""
    bucket = require_bucket()
    client = _client()
    client.head_bucket(Bucket=bucket)
    return {
        "bucket": bucket,
        "region": CONTENT_S3_REGION,
        "profile": AWS_PROFILE or "(default chain)",
    }
