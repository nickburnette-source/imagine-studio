"""Timeline composer.

A take is a storyboard of stills plus prompt lines on a clock.
The clock is the duration. Stills split it evenly. Each line covers
a span. A line may start with ``voice eve:`` or ``sound:``. The
compiled prompt still labels those spans Scene, Voice, and Sound.
"""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

from app.models.rally import RallyPackage
from app.pipeline import library
from app.pipeline.edit import normalize_edit, present as present_edit

ASPECTS = ("16:9", "9:16", "1:1", "3:2", "2:3", "4:3", "3:4")
KINDS = ("scene", "voice", "sound")
VOICES = ("eve", "ara", "leo", "rex", "sal", "gork")
_MIN_SPAN = 0.04


def _nid() -> str:
    return secrets.token_hex(4)


def _stable(data: dict) -> str:
    return json.dumps(data, sort_keys=True, default=str)


def boundaries(count: int, duration: float) -> list[float]:
    """Even still edges, first and last pinned to the clip."""
    if count <= 0:
        return []
    edges = [round(i / count * duration, 1) for i in range(count + 1)]
    edges[0] = 0.0
    edges[-1] = float(round(duration, 1))
    for i in range(1, len(edges)):
        if edges[i] < edges[i - 1]:
            edges[i] = edges[i - 1]
    return edges


def exact_edges(count: int, duration: float) -> list[float]:
    if count <= 0:
        return []
    return [i / count * duration for i in range(count + 1)]


def frac_seconds(frac: float, duration: float) -> float:
    frac = max(0.0, min(1.0, float(frac)))
    return round(frac * duration, 1)


def clamp_span(start: float, end: float, *, min_width: float = _MIN_SPAN) -> tuple[float, float]:
    start = max(0.0, min(1.0, float(start)))
    end = max(0.0, min(1.0, float(end)))
    if end < start:
        start, end = end, start
    if end - start < min_width:
        end = min(1.0, start + min_width)
        if end - start < min_width:
            start = max(0.0, end - min_width)
    return round(start, 4), round(end, 4)


def snap_keyframe(t: float, duration: float, used: list[float]) -> float | None:
    """Interior frame on the 1/3s grid, strictly inside the clip."""
    step = 1.0 / 3.0
    if duration <= step * 2:
        return None
    snapped = round(t / step) * step
    lo = step
    hi = duration - step
    snapped = min(max(snapped, lo), hi)
    for other in used:
        if abs(snapped - other) < step - 1e-6:
            return None
    return round(snapped, 5)


def _empty(raw: dict | None = None) -> dict:
    raw = raw or {}
    try:
        duration = int(raw.get("duration") or 6)
    except (TypeError, ValueError):
        duration = 6
    pending = raw.get("pending")
    return {
        "v": int(raw.get("v") or 0),
        "duration": max(1, min(15, duration)),
        "stills": [],
        "clips": [],
        "pins": [],
        "sequence": [],
        "pending": str(pending) if pending else None,
        "error": str(raw.get("error") or ""),
        "flash": str(raw.get("flash") or ""),
    }


def normalize(raw: dict | None) -> dict:
    board = _empty(raw if isinstance(raw, dict) else {})
    src = raw if isinstance(raw, dict) else {}
    for item in src.get("stills") or []:
        if isinstance(item, dict) and item.get("id") and item.get("asset_id"):
            board["stills"].append(
                {"id": str(item["id"]), "asset_id": str(item["asset_id"])}
            )
    for item in src.get("clips") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        kind = str(item.get("kind") or "")
        if kind not in KINDS:
            continue
        start, end = clamp_span(item.get("start") or 0, item.get("end") or 1)
        voice = str(item.get("voice") or "eve").strip().lower()
        if voice not in VOICES:
            voice = "eve"
        refs = [
            str(ref)
            for ref in (item.get("refs") or [])
            if str(ref).strip()
        ]
        board["clips"].append(
            {
                "id": str(item["id"]),
                "kind": kind,
                "text": str(item.get("text") or "")[:4000],
                "start": start,
                "end": end,
                "voice": voice,
                "refs": refs,
            }
        )
    for item in src.get("pins") or []:
        if not isinstance(item, dict) or not item.get("id") or not item.get("asset_id"):
            continue
        try:
            at = float(item.get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        at = round(max(0.0, min(1.0, at)), 4)
        clip_id = str(item.get("clip_id") or "")
        if clip_id not in {clip["id"] for clip in board["clips"]}:
            clip_id = _line_at(board, at)
        board["pins"].append(
            {
                "id": str(item["id"]),
                "asset_id": str(item["asset_id"]),
                "at": at,
                "clip_id": clip_id,
            }
        )
        if clip_id:
            clip = next(row for row in board["clips"] if row["id"] == clip_id)
            asset_id = str(item["asset_id"])
            if asset_id not in clip["refs"]:
                clip["refs"].append(asset_id)
    for item in src.get("sequence") or []:
        if isinstance(item, dict) and item.get("id") and item.get("asset_id"):
            board["sequence"].append(
                {"id": str(item["id"]), "asset_id": str(item["asset_id"])}
            )
    session = normalize_edit(src.get("edit"))
    if session:
        board["edit"] = session
    return board


def _find_rel(assets: list[dict], rel: str) -> dict | None:
    for row in assets:
        if row.get("rel") == rel:
            return row
    return None


def _seed(pkg: RallyPackage, board: dict, assets: list[dict]) -> None:
    """First open: the current still and take show up on the new board."""
    if not board["stills"]:
        still = (
            _find_rel(assets, "start.png")
            or _find_rel(assets, "Preview.png")
            or _find_rel(assets, "IdleKeyframe.png")
        )
        if still and still.get("kind") == "still":
            board["stills"].append({"id": _nid(), "asset_id": still["id"]})
    if not board["sequence"]:
        take = _find_rel(assets, "take.mp4")
        if take and take.get("kind") == "video":
            board["sequence"].append({"id": _nid(), "asset_id": take["id"]})
    concept = (pkg.concept or "").strip()
    if concept and not board["clips"]:
        board["clips"].append(
            {
                "id": _nid(),
                "kind": "scene",
                "text": concept[:4000],
                "start": 0.0,
                "end": 1.0,
                "voice": "eve",
                "refs": [],
            }
        )
    try:
        duration = int(pkg.duration_default or 6)
    except (TypeError, ValueError):
        duration = 6
    board["duration"] = max(1, min(15, duration))
    board["v"] = 1


def ensure_board(pkg: RallyPackage, assets: list[dict] | None = None) -> dict:
    raw = pkg.board if isinstance(pkg.board, dict) else {}
    # Empty string when this project has never stored a board, so the
    # seed always persists. A stored board saves again only when repair
    # or seeding changed the canonical shape.
    before = _stable(raw) if raw.get("v") else ""
    board = normalize(raw)
    if not raw.get("v"):
        if assets is None:
            assets = library.catalog(pkg)
        _seed(pkg, board, assets)
    if _stable(board) != before:
        pkg.board = board
        pkg.save()
    else:
        pkg.board = board
    return board


_LINE_VOICE = re.compile(
    r"^\s*voice(?:\s+([A-Za-z]+))?\s*:\s*(.*)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_LINE_SOUND = re.compile(
    r"^\s*sound\s*:\s*(.*)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def split_line(text: str) -> tuple[str, str, str] | None:
    """Parse ``voice eve:`` or ``sound:`` from the start of a line.

    A known preset is that voice. ``voice:`` with no name is eve.
    An unknown name is not a voice line, so the words stay scene text.
    """
    raw = str(text or "")
    voice = _LINE_VOICE.match(raw)
    if voice:
        token = (voice.group(1) or "").strip().lower()
        if token and token not in VOICES:
            return None
        return "voice", (token or "eve"), voice.group(2).strip()
    sound = _LINE_SOUND.match(raw)
    if sound:
        return "sound", "eve", sound.group(1).strip()
    return None


def clip_parts(clip: dict) -> tuple[str, str, str]:
    """Prefix wins. A stored kind still compiles when the body has none."""
    parsed = split_line(str(clip.get("text") or ""))
    if parsed:
        return parsed
    kind = str(clip.get("kind") or "scene")
    if kind not in KINDS:
        kind = "scene"
    voice = str(clip.get("voice") or "eve").strip().lower()
    if voice not in VOICES:
        voice = "eve"
    return kind, voice, str(clip.get("text") or "")


def line_from_editor(text: str) -> tuple[str, str, str]:
    """The typed line decides kind, voice, and body."""
    parsed = split_line(text)
    if parsed:
        return parsed
    return "scene", "eve", str(text or "").strip()


def _image(asset: dict | None) -> Path | None:
    if not asset or asset.get("kind") not in {"still", "image"}:
        return None
    path = asset.get("path")
    if isinstance(path, Path) and path.is_file():
        return path
    return None


def plan_media(board: dict, assets: dict[str, dict]) -> dict:
    """Frames, references, and voices the video call should send."""
    duration = int(board["duration"])
    stills = list(board.get("stills") or [])
    n = len(stills)
    edges = exact_edges(n, duration)
    shown = boundaries(n, duration)
    windows: list[dict] = []
    first: Path | None = None
    last: Path | None = None
    keyframes: list[tuple[Path, float]] = []
    used_times: list[float] = []
    for index, still in enumerate(stills):
        asset = assets.get(still["asset_id"])
        path = _image(asset)
        role = "only" if n == 1 else "opening" if index == 0 else "closing" if index == n - 1 else "interior"
        pin_at = None
        pinned = False
        if path is not None and role in {"only", "opening"} and first is None:
            first = path
            pinned = True
        elif path is not None and role == "closing":
            last = path
            pinned = True
        elif path is not None and role == "interior":
            stamp = snap_keyframe(edges[index], duration, used_times)
            if stamp is not None and len(keyframes) < 4:
                keyframes.append((path, stamp))
                used_times.append(stamp)
                pin_at = stamp
                pinned = True
        windows.append(
            {
                "index": index + 1,
                "asset_id": still["asset_id"],
                "name": (asset or {}).get("name") or f"Still {index + 1}",
                "t0": shown[index] if shown else 0.0,
                "t1": shown[index + 1] if shown else float(duration),
                "role": role,
                "pinned": pinned,
                "pin_at": pin_at,
            }
        )

    ref_ids: list[str] = []

    def _add_ref(asset_id: str) -> None:
        if not asset_id or asset_id in ref_ids:
            return
        asset = assets.get(asset_id)
        if _image(asset) is None:
            return
        if len(ref_ids) >= 7:
            return
        ref_ids.append(asset_id)

    for pin in sorted(board.get("pins") or [], key=lambda row: row.get("at") or 0):
        _add_ref(str(pin.get("asset_id") or ""))
    for clip in sorted(board.get("clips") or [], key=lambda row: row.get("start") or 0):
        for ref in clip.get("refs") or []:
            _add_ref(str(ref))
        for pin in board.get("pins") or []:
            if pin.get("clip_id") == clip.get("id"):
                _add_ref(str(pin.get("asset_id") or ""))

    image_tags = {asset_id: f"IMAGE_{i}" for i, asset_id in enumerate(ref_ids)}
    voices: list[str] = []
    for clip in sorted(board.get("clips") or [], key=lambda row: row.get("start") or 0):
        kind, voice, text = clip_parts(clip)
        if kind != "voice" or not " ".join(text.split()):
            continue
        if voice not in voices and len(voices) < 3:
            voices.append(voice)
    audio_tags = {voice: f"AUDIO_{i}" for i, voice in enumerate(voices)}
    return {
        "duration": duration,
        "windows": windows,
        "first": first,
        "last": last,
        "keyframes": keyframes,
        "ref_ids": ref_ids,
        "references": [assets[i]["path"] for i in ref_ids],
        "voices": voices,
        "image_tags": image_tags,
        "audio_tags": audio_tags,
    }


def _rng(t0: float, t1: float) -> str:
    return f"{t0:.1f}–{t1:.1f}s"


def _clip_refs(board: dict, clip: dict) -> list[str]:
    ids = [str(ref) for ref in (clip.get("refs") or [])]
    for pin in board.get("pins") or []:
        if pin.get("clip_id") == clip.get("id"):
            asset_id = str(pin.get("asset_id") or "")
            if asset_id and asset_id not in ids:
                ids.append(asset_id)
    return ids


def compile_prompt(
    board: dict,
    assets: dict[str, dict],
    *,
    aspect: str,
    concept: str = "",
) -> str:
    """Chronological prompt. Timestamps come from the duration."""
    plan = plan_media(board, assets)
    duration = int(board["duration"])
    lines: list[str] = []
    concept = (concept or "").strip()
    if concept:
        lines.append(concept)
    lines.append(f"A {duration} second clip, {aspect}.")
    windows = plan["windows"]
    if windows:
        lines.append("")
        lines.append("Storyboard, in order:")
        for window in windows:
            line = f"{_rng(window['t0'], window['t1'])}. Still {window['index']}."
            if window["role"] in {"opening", "only"} and window["pinned"]:
                line += " This is the opening frame."
            elif window["role"] == "closing" and window["pinned"]:
                line += " This is the closing frame."
            elif window["pinned"] and window["pin_at"] is not None:
                line += f" This frame lands at {window['pin_at']:.1f}s."
            lines.append(line)
    else:
        lines.append("Invent the picture from the lines below.")

    def _with_refs(line: str, ref_bits: list[str]) -> str:
        if ref_bits:
            if not line.endswith("."):
                line += "."
            return line + " Reference " + ", ".join(ref_bits) + "."
        if not line.endswith("."):
            line += "."
        return line

    timed: list[str] = []
    sounds: list[str] = []
    ordered = sorted(
        board.get("clips") or [],
        key=lambda row: (row.get("start") or 0, clip_parts(row)[0]),
    )
    for clip in ordered:
        kind, voice, text = clip_parts(clip)
        text = " ".join(text.split())
        if not text:
            continue
        t0 = frac_seconds(clip.get("start") or 0, duration)
        t1 = frac_seconds(clip.get("end") or 1, duration)
        if t1 < t0:
            t0, t1 = t1, t0
        if t1 == t0 and t1 < duration:
            t1 = round(min(duration, t0 + 0.1), 1)
        span = _rng(t0, t1)
        ref_bits: list[str] = []
        for asset_id in _clip_refs(board, clip):
            tag = plan["image_tags"].get(asset_id)
            if not tag:
                continue
            name = (assets.get(asset_id) or {}).get("name") or "reference"
            ref_bits.append(f"<{tag}> ({name})")
        if kind == "sound":
            sounds.append(_with_refs(f"{span}. Sound: {text}", ref_bits))
            continue
        if kind == "voice":
            tag = plan["audio_tags"].get(voice)
            heard = f" (<{tag}>)" if tag else ""
            line = f"{span}. Voice {voice}{heard}: “{text}”"
        else:
            line = f"{span}. Scene: {text}"
        timed.append(_with_refs(line, ref_bits))
    if timed:
        lines.append("")
        lines.extend(timed)
    if sounds:
        lines.append("")
        lines.extend(sounds)

    loose: list[str] = []
    attached = {
        str(pin.get("asset_id"))
        for pin in board.get("pins") or []
        if pin.get("clip_id")
    }
    for pin in sorted(board.get("pins") or [], key=lambda row: row.get("at") or 0):
        if pin.get("clip_id"):
            continue
        asset_id = str(pin.get("asset_id") or "")
        if asset_id in attached:
            continue
        tag = plan["image_tags"].get(asset_id)
        if not tag:
            continue
        name = (assets.get(asset_id) or {}).get("name") or "reference"
        at = frac_seconds(pin.get("at") or 0, duration)
        loose.append(f"At {at:.1f}s, reference <{tag}> ({name}).")
    if loose:
        lines.append("")
        lines.extend(loose)
    return "\n".join(lines).strip() + "\n"


def _asset_ok(assets: dict[str, dict], asset_id: str, *kinds: str) -> dict:
    row = assets.get(asset_id)
    if row is None:
        raise ValueError("That file is not in the library")
    if kinds and row.get("kind") not in kinds:
        raise ValueError("That file is the wrong kind for this spot")
    return row


def _line_at(board: dict, at: float) -> str:
    """The prompt line under ``at``, or the nearest line."""
    clips = list(board.get("clips") or [])
    if not clips:
        return ""
    hits = []
    for clip in clips:
        start = float(clip.get("start") or 0)
        end = float(clip.get("end") or 0)
        if start - 0.001 <= at <= end + 0.001:
            hits.append(clip)
    starting = [
        clip for clip in hits if abs(float(clip.get("start") or 0) - at) <= 0.02
    ]
    if starting:
        return str(starting[0]["id"])
    if hits:
        return str(hits[0]["id"])
    nearest = min(
        clips,
        key=lambda clip: abs(((float(clip.get("start") or 0) + float(clip.get("end") or 0)) / 2) - at),
    )
    return str(nearest["id"])


def _retie_pin(board: dict, pin: dict) -> None:
    """Keep a moved pin on the line under it, and on that line's references."""
    new_id = _line_at(board, float(pin.get("at") or 0))
    old_id = str(pin.get("clip_id") or "")
    if not new_id or new_id == old_id:
        return
    pin["clip_id"] = new_id
    asset_id = str(pin.get("asset_id") or "")
    clip = next((row for row in board["clips"] if row["id"] == new_id), None)
    if clip is not None and asset_id and asset_id not in (clip.get("refs") or []):
        clip.setdefault("refs", []).append(asset_id)
    if not old_id:
        return
    still = any(
        row["asset_id"] == asset_id and row.get("clip_id") == old_id
        for row in board["pins"]
    )
    old = next((row for row in board["clips"] if row["id"] == old_id), None)
    if old is not None and not still:
        old["refs"] = [ref for ref in old.get("refs") or [] if ref != asset_id]


def apply_op(pkg: RallyPackage, op: str, payload: dict, assets: dict[str, dict]) -> dict:
    """Mutate the board and save. ``assets`` is the current catalog by id."""
    board = ensure_board(pkg, list(assets.values()))
    board["error"] = ""
    board["flash"] = ""

    if op == "set_duration":
        try:
            duration = int(payload.get("duration"))
        except (TypeError, ValueError):
            raise ValueError("Duration needs a number of seconds") from None
        if duration < 1 or duration > 15:
            raise ValueError("Duration is between 1 and 15 seconds")
        board["duration"] = duration
    elif op == "set_aspect":
        aspect = str(payload.get("aspect") or "").strip()
        if aspect not in ASPECTS:
            raise ValueError("Aspect must be 16:9, 9:16, or 1:1")
        pkg.aspect_ratio = aspect
    elif op == "add_still":
        row = _asset_ok(assets, str(payload.get("asset_id") or ""), "still")
        try:
            index = int(payload.get("index"))
        except (TypeError, ValueError):
            index = len(board["stills"])
        index = max(0, min(len(board["stills"]), index))
        board["stills"].insert(index, {"id": _nid(), "asset_id": row["id"]})
        board["flash"] = row["id"]
    elif op == "reorder_stills":
        wanted = [str(i) for i in (payload.get("ids") or [])]
        by = {row["id"]: row for row in board["stills"]}
        if sorted(wanted) != sorted(by):
            raise ValueError("Still order is missing a frame")
        board["stills"] = [by[i] for i in wanted]
    elif op == "add_clip":
        kind = str(payload.get("kind") or "")
        if kind not in KINDS:
            raise ValueError("A clip is scene, voice, or sound")
        start, end = clamp_span(payload.get("start") or 0, payload.get("end") or 1)
        text = str(payload.get("text") or "")
        voice = str(payload.get("voice") or "eve").strip().lower()
        if voice not in VOICES:
            voice = "eve"
        parsed = split_line(text)
        if parsed:
            kind, voice, text = parsed
        elif kind == "scene" and not text.strip() and not any(
            clip["kind"] == "scene" for clip in board["clips"]
        ):
            text = (pkg.concept or "").strip()
        if kind == "sound" and not any(clip["kind"] == "sound" for clip in board["clips"]):
            start, end = 0.0, 1.0
        board["clips"].append(
            {
                "id": _nid(),
                "kind": kind,
                "text": text[:4000],
                "start": start,
                "end": end,
                "voice": voice,
                "refs": [],
            }
        )
    elif op == "patch_clip":
        ident = str(payload.get("id") or "")
        clip = next((row for row in board["clips"] if row["id"] == ident), None)
        if clip is None:
            raise ValueError("That clip is gone")
        if "start" in payload or "end" in payload:
            start = payload.get("start", clip["start"])
            end = payload.get("end", clip["end"])
            clip["start"], clip["end"] = clamp_span(start, end)
        if "kind" in payload:
            kind = str(payload.get("kind") or "")
            if kind not in KINDS:
                raise ValueError("A line is scene, voice, or sound")
            voice = str(payload.get("voice") or clip.get("voice") or "eve").strip().lower()
            if voice not in VOICES:
                voice = "eve"
            text = str(payload["text"]) if "text" in payload else str(clip.get("text") or "")
            parsed = split_line(text)
            if parsed:
                kind, voice, text = parsed
            clip["kind"] = kind
            clip["voice"] = voice
            clip["text"] = text[:4000]
        elif "text" in payload:
            kind, voice, body = line_from_editor(str(payload.get("text") or ""))
            clip["kind"] = kind
            clip["voice"] = voice
            clip["text"] = body[:4000]
        elif "voice" in payload:
            voice = str(payload.get("voice") or "").strip().lower()
            if voice in VOICES:
                clip["voice"] = voice
        if "refs" in payload:
            refs = []
            for ref in payload.get("refs") or []:
                _asset_ok(assets, str(ref), "still")
                if str(ref) not in refs:
                    refs.append(str(ref))
            clip["refs"] = refs[:7]
    elif op == "add_pin":
        row = _asset_ok(assets, str(payload.get("asset_id") or ""), "still")
        try:
            at = float(payload.get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        at = round(max(0.0, min(1.0, at)), 4)
        clip_id = str(payload.get("clip_id") or "")
        if clip_id and not any(clip["id"] == clip_id for clip in board["clips"]):
            clip_id = ""
        if not clip_id:
            clip_id = _line_at(board, at)
        if not clip_id:
            raise ValueError("Add a line, then drop the still on it")
        already = any(
            pin["asset_id"] == row["id"] and pin.get("clip_id") == clip_id
            for pin in board["pins"]
        )
        if not already:
            board["pins"].append(
                {
                    "id": _nid(),
                    "asset_id": row["id"],
                    "at": at,
                    "clip_id": clip_id,
                }
            )
        clip = next(row_ for row_ in board["clips"] if row_["id"] == clip_id)
        if row["id"] not in clip["refs"]:
            clip["refs"].append(row["id"])
        board["flash"] = row["id"]
    elif op == "patch_pin":
        ident = str(payload.get("id") or "")
        pin = next((row for row in board["pins"] if row["id"] == ident), None)
        if pin is None:
            raise ValueError("That reference is gone")
        try:
            at = float(payload.get("at"))
        except (TypeError, ValueError):
            raise ValueError("Reference needs a time") from None
        pin["at"] = round(max(0.0, min(1.0, at)), 4)
        _retie_pin(board, pin)
    elif op == "remove":
        kind = str(payload.get("kind") or "")
        ident = str(payload.get("id") or "")
        if kind == "still":
            board["stills"] = [row for row in board["stills"] if row["id"] != ident]
        elif kind == "clip":
            board["clips"] = [row for row in board["clips"] if row["id"] != ident]
            board["pins"] = [row for row in board["pins"] if row.get("clip_id") != ident]
        elif kind == "ref":
            clip_id = str(payload.get("clip_id") or "")
            asset_id = str(payload.get("id") or "")
            board["pins"] = [
                row for row in board["pins"]
                if not (row.get("clip_id") == clip_id and row.get("asset_id") == asset_id)
            ]
            clip = next((row for row in board["clips"] if row["id"] == clip_id), None)
            if clip is not None:
                clip["refs"] = [ref for ref in clip.get("refs") or [] if ref != asset_id]
        elif kind == "pin":
            gone = next((row for row in board["pins"] if row["id"] == ident), None)
            board["pins"] = [row for row in board["pins"] if row["id"] != ident]
            if gone and gone.get("clip_id"):
                still = any(
                    row["asset_id"] == gone["asset_id"] and row.get("clip_id") == gone["clip_id"]
                    for row in board["pins"]
                )
                clip = next((row for row in board["clips"] if row["id"] == gone["clip_id"]), None)
                if clip and not still:
                    clip["refs"] = [
                        ref for ref in clip.get("refs") or [] if ref != gone["asset_id"]
                    ]
        elif kind == "sequence":
            board["sequence"] = [row for row in board["sequence"] if row["id"] != ident]
        else:
            raise ValueError("Nothing to remove")
    elif op == "add_sequence":
        row = _asset_ok(assets, str(payload.get("asset_id") or ""), "video")
        if not any(item["asset_id"] == row["id"] for item in board["sequence"]):
            board["sequence"].append({"id": _nid(), "asset_id": row["id"]})
        if board.get("pending") == row["id"]:
            board["pending"] = None
    elif op == "keep":
        pending = board.get("pending")
        if not pending:
            raise ValueError("Generate a take before keeping one")
        _asset_ok(assets, pending, "video")
        if not any(item["asset_id"] == pending for item in board["sequence"]):
            board["sequence"].append({"id": _nid(), "asset_id": pending})
        board["pending"] = None
    elif op == "extract":
        from app.pipeline.frames import extract_first_frame, extract_last_frame

        row = _asset_ok(assets, str(payload.get("asset_id") or ""), "video")
        which = "last" if str(payload.get("which") or "") == "last" else "first"
        dest_name = f"{row['name']} {which} frame"
        tmp = library.library_dir(pkg) / f".grab-{_nid()}.png"
        try:
            if which == "last":
                extract_last_frame(row["path"], tmp)
            else:
                extract_first_frame(row["path"], tmp)
            made = library.ingest(
                pkg,
                tmp,
                name=library.next_name(list(assets.values()), "still", dest_name),
                kind="still",
                move=True,
            )
        finally:
            if tmp.is_file():
                tmp.unlink(missing_ok=True)
        board["flash"] = made["id"]
        place = str(payload.get("place") or "")
        if place == "start":
            board["stills"] = [
                row_
                for row_ in board["stills"]
                if row_["asset_id"] != made["id"]
            ]
            board["stills"].insert(0, {"id": _nid(), "asset_id": made["id"]})
        elif place == "pin":
            board["pins"].append(
                {
                    "id": _nid(),
                    "asset_id": made["id"],
                    "at": 0.0,
                    "clip_id": "",
                }
            )
        assets[made["id"]] = made
    elif op == "dismiss":
        pass
    elif op == "edit_open":
        from app.pipeline.edit import open_edit

        open_edit(board, assets, payload)
    elif op == "edit_cut":
        from app.pipeline.edit import cut_at

        session = board.get("edit")
        if not isinstance(session, dict):
            raise ValueError("Nothing to edit")
        cut_at(session, payload.get("at"))
    elif op == "edit_join":
        from app.pipeline.edit import join_next

        session = board.get("edit")
        if not isinstance(session, dict):
            raise ValueError("Nothing to edit")
        join_next(session, str(payload.get("id") or ""))
    elif op == "edit_delete":
        from app.pipeline.edit import delete_clip

        session = board.get("edit")
        if not isinstance(session, dict):
            raise ValueError("Nothing to edit")
        delete_clip(session, str(payload.get("id") or ""))
        if not session.get("clips") and not session.get("sounds") and not session.get("undo"):
            board.pop("edit", None)
    elif op == "edit_reorder":
        from app.pipeline.edit import reorder

        session = board.get("edit")
        if not isinstance(session, dict):
            raise ValueError("Nothing to edit")
        reorder(session, list(payload.get("ids") or []))
    elif op == "edit_undo":
        from app.pipeline.edit import undo

        session = board.get("edit")
        if not isinstance(session, dict):
            raise ValueError("Nothing to edit")
        undo(session)
    elif op == "edit_discard":
        from app.pipeline.edit import discard

        discard(board)
    elif op == "edit_keep":
        from app.pipeline.edit import keep_cut

        keep_cut(pkg, board, assets, focus=str(payload.get("id") or ""))
    elif op == "edit_yank":
        from app.pipeline.edit import yank_audio

        session = board.get("edit")
        if not isinstance(session, dict):
            raise ValueError("Nothing to edit")
        yank_audio(session, str(payload.get("id") or ""), assets)
    elif op == "edit_sound":
        from app.pipeline.edit import add_sound

        session = board.get("edit")
        if not isinstance(session, dict):
            raise ValueError("Nothing to edit")
        add_sound(session, str(payload.get("asset_id") or ""), payload.get("at"), assets)
    elif op == "edit_move_sound":
        from app.pipeline.edit import move_sound

        session = board.get("edit")
        if not isinstance(session, dict):
            raise ValueError("Nothing to edit")
        move_sound(session, str(payload.get("id") or ""), payload.get("at"))
    elif op == "edit_trim":
        from app.pipeline.edit import trim_clip

        session = board.get("edit")
        if not isinstance(session, dict):
            raise ValueError("Nothing to edit")
        trim_clip(
            session,
            str(payload.get("id") or ""),
            str(payload.get("edge") or ""),
            payload.get("at"),
            assets,
        )
    elif op == "edit_trim_sound":
        from app.pipeline.edit import trim_sound

        session = board.get("edit")
        if not isinstance(session, dict):
            raise ValueError("Nothing to edit")
        trim_sound(
            session,
            str(payload.get("id") or ""),
            str(payload.get("edge") or ""),
            payload.get("at"),
            assets,
        )
    elif op == "edit_delete_sound":
        from app.pipeline.edit import delete_sound

        session = board.get("edit")
        if not isinstance(session, dict):
            raise ValueError("Nothing to edit")
        delete_sound(session, str(payload.get("id") or ""))
    else:
        raise ValueError("Unknown edit")

    pkg.board = board
    pkg.save()
    return board


def view_model(pkg: RallyPackage, assets: list[dict], *, url_for) -> dict:
    """Client payload. Timestamps are already calculated."""
    by = {row["id"]: row for row in assets}
    board = ensure_board(pkg, assets)
    duration = int(board["duration"])
    shown = boundaries(len(board["stills"]), duration)

    def url(row: dict | None) -> str:
        if not row:
            return ""
        rel = row.get("rel") or ""
        path = row.get("path")
        stamp = ""
        if isinstance(path, Path) and path.is_file():
            try:
                stamp = str(int(path.stat().st_mtime))
            except OSError:
                stamp = ""
        base = url_for(rel)
        return f"{base}?t={stamp}" if stamp else base

    stills = []
    for index, still in enumerate(board["stills"]):
        asset = by.get(still["asset_id"])
        stills.append(
            {
                "id": still["id"],
                "asset_id": still["asset_id"],
                "name": (asset or {}).get("name") or "Missing",
                "url": url(asset),
                "t0": shown[index] if shown else 0.0,
                "t1": shown[index + 1] if shown else float(duration),
            }
        )
    clips = []
    for clip in board["clips"]:
        ref_rows = []
        for ref in _clip_refs(board, clip):
            asset = by.get(ref)
            if asset:
                ref_rows.append(
                    {"id": ref, "name": asset.get("name") or "Reference", "url": url(asset)}
                )
        clips.append(
            {
                "id": clip["id"],
                "kind": clip["kind"],
                "text": clip["text"],
                "start": clip["start"],
                "end": clip["end"],
                "voice": clip["voice"],
                "refs": ref_rows,
                "t0": frac_seconds(clip["start"], duration),
                "t1": frac_seconds(clip["end"], duration),
            }
        )
    pins = []
    for pin in board["pins"]:
        asset = by.get(pin["asset_id"])
        pins.append(
            {
                "id": pin["id"],
                "asset_id": pin["asset_id"],
                "name": (asset or {}).get("name") or "Reference",
                "url": url(asset),
                "at": pin["at"],
                "clip_id": pin.get("clip_id") or "",
            }
        )
    sequence = []
    for item in board["sequence"]:
        asset = by.get(item["asset_id"])
        sequence.append(
            {
                "id": item["id"],
                "asset_id": item["asset_id"],
                "name": (asset or {}).get("name") or "Take",
                "url": url(asset),
                "pending": False,
            }
        )
    pending = None
    if board.get("pending"):
        asset = by.get(board["pending"])
        if asset:
            pending = {
                "asset_id": asset["id"],
                "name": asset.get("name") or "New take",
                "url": url(asset),
            }
    library_rows = [
        {
            "id": row["id"],
            "kind": row["kind"],
            "name": row["name"],
            "url": url(row),
        }
        for row in assets
    ]
    prompt = compile_prompt(
        board,
        by,
        aspect=pkg.aspect_ratio or "9:16",
        concept=pkg.concept or "",
    )
    return {
        "name": pkg.name,
        "title": pkg.rally_title(),
        "concept": (pkg.concept or "").strip(),
        "aspect": pkg.aspect_ratio or "9:16",
        "aspects": ["16:9", "9:16", "1:1"],
        "duration": duration,
        "durations": list(range(1, 16)),
        "voices": list(VOICES),
        "library": library_rows,
        "stills": stills,
        "clips": clips,
        "pins": pins,
        "sequence": sequence,
        "pending": pending,
        "prompt": prompt,
        "error": board.get("error") or "",
        "flash": board.get("flash") or "",
        "edit": present_edit(board.get("edit"), by, url),
    }
