"""Tiny client for local rally-builder code to call the content-store API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from content_store.config import CONTENT_API_TOKEN, CONTENT_HOST, CONTENT_PORT


def base_url(host: str | None = None, port: int | None = None) -> str:
    h = host or CONTENT_HOST
    p = port if port is not None else CONTENT_PORT
    return f"http://{h}:{p}"


def _headers() -> dict[str, str]:
    if not CONTENT_API_TOKEN:
        return {}
    return {"Authorization": f"Bearer {CONTENT_API_TOKEN}"}


def mint_upload(
    *,
    content_type: str = "video/mp4",
    ext: str = "mp4",
    prefix: str | None = None,
    name: str | None = None,
    ttl: int | None = None,
    base: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """POST /v1/uploads → { put_url, get_url, key, ... }."""
    payload: dict[str, Any] = {
        "content_type": content_type,
        "ext": ext,
    }
    if prefix is not None:
        payload["prefix"] = prefix
    if name is not None:
        payload["name"] = name
    if ttl is not None:
        payload["ttl"] = ttl

    url = (base or base_url()) + "/v1/uploads"
    r = httpx.post(url, json=payload, headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r.json()


def download_to(
    key: str,
    dest: Path,
    *,
    base: str | None = None,
    timeout: float = 300.0,
) -> Path:
    """Stream object through the local API into dest."""
    from urllib.parse import quote

    encoded = "/".join(quote(part, safe="") for part in key.split("/"))
    url = (base or base_url()) + f"/v1/files/{encoded}?proxy=1"
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, headers=_headers(), timeout=timeout) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    return dest


def health(*, base: str | None = None, timeout: float = 10.0) -> dict[str, Any]:
    r = httpx.get((base or base_url()) + "/health", timeout=timeout)
    return r.json()
