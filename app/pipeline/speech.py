"""Speak a line into the cutter.

Imagine's video models bake a soundtrack into the picture. Yank lifts
that track onto the lane. A separate file — a line you can drop while
editing — comes from the speech API (POST /v1/tts). There is no music
or room-tone endpoint. Storyboard ``sound:`` lines stay prompt text.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from app.config import GENERATE_BACKEND
from app.models.rally import RallyPackage
from app.pipeline import board, edit, library
from app.pipeline.auth_token import resolve_bearer_token
from app.pipeline.board import VOICES

API_URL = "https://api.x.ai/v1/tts"
_MAX_CHARS = 1500

# Tests park a stand-in here. Production leaves it empty.
_SYNTHESIZER: Callable[[str, str], bytes] | None = None


def silent_wav(text: str) -> bytes:
    """A tiny PCM file so a stub line still has a duration."""
    import struct

    words = max(1, len((text or "").split()))
    seconds = min(8.0, max(0.45, words * 0.32))
    rate = 16000
    count = int(rate * seconds)
    payload = b"\x00\x00" * count
    header = b"RIFF" + struct.pack("<I", 36 + len(payload)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(payload))
    return header + payload


def _request(text: str, voice: str, at) -> tuple[str, str, float]:
    line = " ".join((text or "").split())
    if not line:
        raise ValueError("Write a line to speak")
    if len(line) > _MAX_CHARS:
        raise ValueError("Keep a spoken line under 1500 characters")
    voice_id = (voice or "eve").strip().lower()
    if voice_id not in VOICES:
        raise ValueError("Pick eve, ara, leo, rex, sal, or gork")
    try:
        at_f = float(at or 0)
    except (TypeError, ValueError):
        raise ValueError("Speech needs a time") from None
    return line, voice_id, max(0.0, at_f)


def _clip_name(text: str, voice: str) -> str:
    short = text.strip()
    if len(short) > 32:
        short = short[:32].rstrip() + "…"
    return f"{voice} · {short}"[:80]


def _speech_error(response: httpx.Response) -> str:
    message = ""
    try:
        data = response.json()
    except Exception:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or err.get("code") or "")
        elif isinstance(err, str):
            message = err
    message = " ".join(message.split())[:180]
    if message:
        return message
    return f"Speech failed ({response.status_code})"


async def synthesize(text: str, voice_id: str) -> bytes:
    """MP3 bytes from the speech API, or a silent wav in stub mode."""
    if _SYNTHESIZER is not None:
        return _SYNTHESIZER(text, voice_id)
    if GENERATE_BACKEND == "stub":
        return silent_wav(text)
    try:
        token, _source = resolve_bearer_token()
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    timeout = httpx.Timeout(60.0, connect=20.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            response = await http.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "text": text,
                    "voice_id": voice_id,
                    "language": "auto",
                    "output_format": {
                        "codec": "mp3",
                        "sample_rate": 24000,
                        "bit_rate": 128000,
                    },
                },
            )
    except httpx.HTTPError as exc:
        raise ValueError("Speech did not answer") from exc
    if response.status_code >= 400 or response.content[:1] == b"{":
        raise ValueError(_speech_error(response))
    if len(response.content) < 64:
        raise ValueError("Speech came back empty")
    return response.content


async def place_on_lane(pkg: RallyPackage, *, text: str, voice: str, at) -> None:
    """Synthesize ``text`` and drop the file on the open cut at ``at``."""
    line, voice_id, at_f = _request(text, voice, at)
    state = board.ensure_board(pkg, library.catalog(pkg))
    session = state.get("edit")
    if not isinstance(session, dict) or not session.get("clips"):
        raise ValueError("Open a cut before speaking")
    audio = await synthesize(line, voice_id)
    suffix = ".wav" if audio[:4] == b"RIFF" else ".mp3"
    tmp = library.library_dir(pkg) / f".speak-tmp{suffix}"
    tmp.write_bytes(audio)
    try:
        rows = library.catalog(pkg)
        made = library.ingest(
            pkg,
            tmp,
            name=library.next_name(rows, "audio", _clip_name(line, voice_id)),
            kind="audio",
            move=True,
        )
    finally:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
    assets = library.by_id(pkg)
    state = board.ensure_board(pkg, list(assets.values()))
    session = state.get("edit")
    if not isinstance(session, dict):
        raise ValueError("Open a cut before speaking")
    edit.add_sound(session, made["id"], at_f, assets)
    state["flash"] = made["id"]
    state["error"] = ""
    pkg.board = state
    pkg.save()
