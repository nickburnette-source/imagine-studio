"""Shared Imagine credentials: XAI_API_KEY or grok login session."""

from __future__ import annotations

import json
from pathlib import Path

from app.config import XAI_API_KEY


def resolve_bearer_token() -> tuple[str, str]:
    """
    Return (token, source_label).

    source_label is ``api_key`` or ``grok_session``.
    """
    if XAI_API_KEY.strip():
        return XAI_API_KEY.strip(), "api_key"

    auth_path = Path.home() / ".grok" / "auth.json"
    if not auth_path.is_file():
        raise RuntimeError(
            "No Imagine credentials. Set XAI_API_KEY or run `grok login` "
            "(session stored in ~/.grok/auth.json)."
        )
    data = json.loads(auth_path.read_text())
    if not isinstance(data, dict) or not data:
        raise RuntimeError("~/.grok/auth.json is empty — run `grok login`")
    entry = next(iter(data.values()))
    if not isinstance(entry, dict):
        raise RuntimeError("Unexpected auth.json shape — run `grok login`")
    token = (entry.get("key") or "").strip()
    if not token:
        raise RuntimeError("No access token in auth.json — run `grok login`")
    return token, "grok_session"
