"""Cut a generated take.

Clips are ordered pieces of library videos. Cut splits the piece under
the playhead. Join removes the boundary with the next piece. Keep
writes the assembly back into the library.
"""

from __future__ import annotations

import copy
import json
import secrets
import subprocess
from pathlib import Path

from app.models.rally import RallyPackage
from app.pipeline import library
from app.pipeline.scenes import SceneError, _ffmpeg, probe

_MIN_SECONDS = 0.04


def normalize_edit(raw: dict | list | None) -> dict | None:
    """Canonical cut. ``None`` when there is nothing to edit."""
    if isinstance(raw, list):
        raw = {"clips": raw}
    if not isinstance(raw, dict):
        return None
    clips = []
    for item in raw.get("clips") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        source = item.get("parts")
        if not isinstance(source, list):
            source = [item] if item.get("asset_id") else []
        parts = []
        for part in source:
            parsed = _part(part)
            if parsed:
                parts.append(parsed)
        if parts:
            clips.append({"id": str(item["id"]), "parts": parts})
    sounds = []
    for item in raw.get("sounds") or []:
        parsed = _sound(item)
        if parsed:
            sounds.append(parsed)
    undo = []
    for snap in (raw.get("undo") or [])[-40:]:
        if isinstance(snap, list):
            body = {"clips": snap, "sounds": []}
        elif isinstance(snap, dict) and ("clips" in snap or "sounds" in snap):
            body = {"clips": snap.get("clips") or [], "sounds": snap.get("sounds") or []}
        else:
            continue
        restored = normalize_edit(body)
        if restored and (restored["clips"] or restored["sounds"]):
            undo.append({"clips": restored["clips"], "sounds": restored["sounds"]})
    if not clips and not sounds and not undo:
        return None
    return {"clips": clips, "sounds": sounds, "undo": undo}


def present(edit: dict | None, assets: dict[str, dict], url_for) -> dict | None:
    session = normalize_edit(edit)
    if not session:
        return None
    clips = []
    total = 0.0
    for clip in session["clips"]:
        parts = []
        seconds = 0.0
        for part in clip["parts"]:
            asset = assets.get(part["asset_id"])
            length = max(0.0, part["end"] - part["start"])
            seconds += length
            parts.append(
                {
                    "asset_id": part["asset_id"],
                    "name": (asset or {}).get("name") or "Take",
                    "url": url_for(asset) if asset else "",
                    "start": part["start"],
                    "end": part["end"],
                    "fps": part["fps"],
                    "mute": bool(part.get("mute")),
                    "audio": _heard(asset),
                    "media": _media_seconds(asset, part["end"]),
                }
            )
        total += seconds
        clips.append(
            {
                "id": clip["id"],
                "seconds": round(seconds, 3),
                "parts": parts,
            }
        )
    sounds = []
    for sound in session["sounds"]:
        asset = assets.get(sound["asset_id"])
        length = max(0.0, sound["end"] - sound["start"])
        total = max(total, sound["at"] + length)
        sounds.append(
            {
                "id": sound["id"],
                "asset_id": sound["asset_id"],
                "name": (asset or {}).get("name") or "Sound",
                "url": url_for(asset) if asset else "",
                "start": sound["start"],
                "end": sound["end"],
                "at": sound["at"],
                "seconds": round(length, 3),
                "media": _media_seconds(asset, sound["end"]),
            }
        )
    if not clips and not sounds:
        return {"clips": [], "sounds": [], "seconds": 0, "undo": len(session["undo"])}
    return {
        "clips": clips,
        "sounds": sounds,
        "seconds": round(total, 3),
        "undo": len(session["undo"]),
    }


def open_edit(board: dict, assets: dict[str, dict], payload: dict) -> None:
    """Resume the cut, or start one from a take or the filmstrip."""
    current = normalize_edit(board.get("edit"))
    asset_id = str(payload.get("asset_id") or "").strip()
    fresh = bool(payload.get("fresh"))
    if asset_id and _same_whole(current, asset_id, assets):
        board["edit"] = current
        return
    if asset_id and (fresh or current is None or not current["clips"]):
        clip = _whole(assets, asset_id)
        undo = list((current or {}).get("undo") or [])
        if current and (current["clips"] or current.get("sounds")):
            undo.append(
                {
                    "clips": copy.deepcopy(current["clips"]),
                    "sounds": copy.deepcopy(current.get("sounds") or []),
                }
            )
        stored = normalize_edit({"clips": [clip], "sounds": [], "undo": undo[-40:]})
        board["edit"] = stored or {"clips": [clip], "sounds": [], "undo": []}
        return
    if current:
        board["edit"] = current
        return
    ids: list[str] = []
    for item in board.get("sequence") or []:
        asset = assets.get(str(item.get("asset_id") or ""))
        if asset and asset.get("kind") == "video" and asset["id"] not in ids:
            ids.append(asset["id"])
    pending = str(board.get("pending") or "")
    if not ids and pending and assets.get(pending, {}).get("kind") == "video":
        ids.append(pending)
    if not ids:
        raise ValueError("Generate a take before editing")
    stored = normalize_edit(
        {
            "clips": [_whole(assets, asset_id_) for asset_id_ in ids],
            "sounds": [],
            "undo": [],
        }
    )
    board["edit"] = stored or {"clips": [], "sounds": [], "undo": []}


def cut_at(edit: dict, at: float) -> None:
    """Split the piece under ``at`` seconds on the assembly."""
    try:
        at = float(at)
    except (TypeError, ValueError):
        raise ValueError("Cut needs a time") from None
    session = _require(edit)
    clips = session["clips"]
    cursor = 0.0
    for index, clip in enumerate(clips):
        length = _length(clip)
        last = index == len(clips) - 1
        if at < cursor + length - 1e-4 or last:
            _split(session, index, at - cursor)
            edit.clear()
            edit.update(session)
            return
        cursor += length
    raise ValueError("Move the playhead into the clip before cutting")


def join_next(edit: dict, clip_id: str) -> None:
    session = _require(edit)
    clips = session["clips"]
    index = _index(clips, clip_id)
    if index < 0:
        raise ValueError("Select a clip to join")
    if index + 1 >= len(clips):
        raise ValueError("No clip after this one")
    _push(session)
    left = clips[index]
    right = clips[index + 1]
    clips[index : index + 2] = [
        {"id": left["id"], "parts": _coalesce(left["parts"] + right["parts"])}
    ]
    edit.clear()
    edit.update(session)


def delete_clip(edit: dict, clip_id: str) -> None:
    session = _require(edit)
    clips = session["clips"]
    index = _index(clips, clip_id)
    if index < 0:
        raise ValueError("Select a clip to delete")
    _push(session)
    del clips[index]
    edit.clear()
    edit.update(session)


def reorder(edit: dict, ids: list[str]) -> None:
    session = _require(edit)
    clips = session["clips"]
    current = [clip["id"] for clip in clips]
    want = [str(item or "").strip() for item in ids]
    if want == current:
        edit.clear()
        edit.update(session)
        return
    if len(want) != len(current) or sorted(want) != sorted(current):
        raise ValueError("Reorder the clips that are already on the cut")
    _push(session)
    by_id = {clip["id"]: clip for clip in clips}
    session["clips"] = [by_id[item] for item in want]
    edit.clear()
    edit.update(session)


def undo(edit: dict) -> None:
    session = _require(edit)
    stack = session.get("undo") or []
    if not stack:
        raise ValueError("Nothing to undo")
    snap = stack.pop()
    if isinstance(snap, dict) and "clips" in snap:
        session["clips"] = snap["clips"]
        session["sounds"] = snap.get("sounds") or []
    else:
        session["clips"] = snap
        session["sounds"] = []
    session["undo"] = stack
    edit.clear()
    edit.update(session)


def yank_audio(edit: dict, clip_id: str, assets: dict[str, dict]) -> None:
    """Lift a clip's sound onto the lane and play the picture quiet."""
    session = _require(edit)
    index = _index(session["clips"], clip_id)
    if index < 0:
        raise ValueError("Select a clip to yank")
    clip = session["clips"][index]
    offset = sum(_length(row) for row in session["clips"][:index])
    blocks = []
    cursor = offset
    heard = False
    for part in clip["parts"]:
        length = part["end"] - part["start"]
        asset = assets.get(part["asset_id"])
        if _heard(asset):
            heard = True
            if not part.get("mute"):
                blocks.append(
                    {
                        "id": secrets.token_hex(4),
                        "asset_id": part["asset_id"],
                        "start": part["start"],
                        "end": part["end"],
                        "at": round(cursor, 4),
                    }
                )
        cursor += length
    if not blocks:
        if heard:
            raise ValueError("That audio is already on the lane")
        raise ValueError("This clip has no audio")
    _push(session)
    for part in clip["parts"]:
        if _heard(assets.get(part["asset_id"])):
            part["mute"] = True
    session["sounds"].extend(blocks)
    edit.clear()
    edit.update(session)


def add_sound(edit: dict, asset_id: str, at: float, assets: dict[str, dict]) -> None:
    """Place a library sound at ``at`` seconds on the assembly."""
    session = _require(edit)
    asset = assets.get(str(asset_id or ""))
    path = (asset or {}).get("path")
    if not isinstance(path, Path):
        raise ValueError("That sound is not in the library")
    duration = _audio_duration(path)
    if duration <= 0:
        raise ValueError("That file has no audio")
    if duration > 600:
        raise ValueError("Sound is longer than 10 minutes")
    try:
        at = float(at or 0)
    except (TypeError, ValueError):
        at = 0.0
    _push(session)
    session["sounds"].append(
        {
            "id": secrets.token_hex(4),
            "asset_id": str(asset_id),
            "start": 0.0,
            "end": round(duration, 4),
            "at": round(max(0.0, at), 4),
        }
    )
    edit.clear()
    edit.update(session)


def trim_clip(edit: dict, clip_id: str, edge: str, source_at: float, assets: dict[str, dict]) -> None:
    """Move a clip's in or out point. ``source_at`` is a time in the source."""
    session = _require(edit)
    index = _index(session["clips"], str(clip_id or ""))
    if index < 0:
        raise ValueError("Select a clip to trim")
    which = str(edge or "")
    if which not in ("in", "out"):
        raise ValueError("Trim the in or the out")
    clip = session["clips"][index]
    part = clip["parts"][0] if which == "in" else clip["parts"][-1]
    try:
        source_at = float(source_at)
    except (TypeError, ValueError):
        raise ValueError("Trim needs a time") from None
    media = float(part["end"])
    asset = (assets or {}).get(part["asset_id"])
    if asset:
        try:
            spanned, _fps = _span(asset)
            media = max(media, spanned)
        except ValueError:
            media = max(media, float(part["end"]))
    room = _min_piece(part["fps"])
    if which == "in":
        start = round(min(float(part["end"]) - room, max(0.0, source_at)), 4)
        if abs(start - float(part["start"])) < 0.01:
            return
        _push(session)
        part["start"] = start
    else:
        end = round(max(float(part["start"]) + room, min(media, source_at)), 4)
        if abs(end - float(part["end"])) < 0.01:
            return
        _push(session)
        part["end"] = end
    edit.clear()
    edit.update(session)


def trim_sound(edit: dict, sound_id: str, edge: str, source_at: float, assets: dict[str, dict]) -> None:
    """Move a lane sound's in or out. The out stays put when the in moves."""
    session = _require(edit)
    sound = next((row for row in session["sounds"] if row["id"] == str(sound_id or "")), None)
    if sound is None:
        raise ValueError("That sound is gone")
    which = str(edge or "")
    if which not in ("in", "out"):
        raise ValueError("Trim the in or the out")
    try:
        source_at = float(source_at)
    except (TypeError, ValueError):
        raise ValueError("Trim needs a time") from None
    media = float(sound["end"])
    asset = (assets or {}).get(sound["asset_id"])
    path = (asset or {}).get("path")
    if isinstance(path, Path):
        probed = _audio_duration(path)
        if probed > 0:
            media = max(media, probed)
    if which == "in":
        start = round(min(float(sound["end"]) - _MIN_SECONDS, max(0.0, source_at)), 4)
        delta = start - float(sound["start"])
        if abs(delta) < 0.01:
            return
        _push(session)
        sound["start"] = start
        sound["at"] = round(max(0.0, float(sound["at"]) + delta), 4)
    else:
        end = round(max(float(sound["start"]) + _MIN_SECONDS, min(media, source_at)), 4)
        if abs(end - float(sound["end"])) < 0.01:
            return
        _push(session)
        sound["end"] = end
    edit.clear()
    edit.update(session)


def move_sound(edit: dict, sound_id: str, at: float) -> None:
    session = _require(edit)
    sound = next((row for row in session["sounds"] if row["id"] == str(sound_id or "")), None)
    if sound is None:
        raise ValueError("That sound is gone")
    try:
        at = float(at)
    except (TypeError, ValueError):
        raise ValueError("Sound needs a time") from None
    _push(session)
    sound["at"] = round(max(0.0, at), 4)
    edit.clear()
    edit.update(session)


def delete_sound(edit: dict, sound_id: str) -> None:
    session = _require(edit)
    sounds = session["sounds"]
    index = next((i for i, row in enumerate(sounds) if row["id"] == str(sound_id or "")), -1)
    if index < 0:
        raise ValueError("Select a sound to delete")
    _push(session)
    del sounds[index]
    edit.clear()
    edit.update(session)


def discard(board: dict) -> None:
    board.pop("edit", None)


def keep_cut(
    pkg: RallyPackage,
    board: dict,
    assets: dict[str, dict],
    focus: str = "",
) -> None:
    """File each cut as its own take, in order, on the filmstrip."""
    session = _require(board.get("edit") or {})
    clips = [clip for clip in session["clips"] if clip.get("parts")]
    if not clips:
        raise ValueError("Nothing to keep")
    cursor = 0.0
    saved: list[tuple[str, dict]] = []
    for clip in clips:
        length = _length(clip)
        heard = _slice_sounds(session.get("sounds") or [], cursor, cursor + length)
        made = _materialize(
            pkg,
            assets,
            clip["parts"],
            heard,
            name=_range_name(cursor, cursor + length),
        )
        assets[made["id"]] = made
        if not any(item["asset_id"] == made["id"] for item in board["sequence"]):
            board["sequence"].append({"id": secrets.token_hex(4), "asset_id": made["id"]})
        saved.append((str(clip["id"]), made))
        cursor += length
    chosen = next((row["id"] for clip_id, row in saved if clip_id == str(focus or "")), "")
    board["flash"] = chosen or saved[0][1]["id"]
    board["edit"] = session


def _require(edit: dict) -> dict:
    session = normalize_edit(edit)
    if not session:
        raise ValueError("Nothing to edit")
    return session


def _part(raw: dict) -> dict | None:
    if not isinstance(raw, dict) or not raw.get("asset_id"):
        return None
    try:
        start = float(raw.get("start") or 0)
        end = float(raw.get("end") or 0)
        fps = float(raw.get("fps") or 30)
    except (TypeError, ValueError):
        return None
    if end - start < _MIN_SECONDS / 2:
        return None
    if fps <= 0:
        fps = 30.0
    return {
        "asset_id": str(raw["asset_id"]),
        "start": round(max(0.0, start), 4),
        "end": round(end, 4),
        "fps": fps,
        "mute": bool(raw.get("mute")),
    }


def _sound(raw: dict) -> dict | None:
    if not isinstance(raw, dict) or not raw.get("id") or not raw.get("asset_id"):
        return None
    try:
        start = float(raw.get("start") or 0)
        end = float(raw.get("end") or 0)
        at = float(raw.get("at") or 0)
    except (TypeError, ValueError):
        return None
    if end - start < _MIN_SECONDS / 2:
        return None
    return {
        "id": str(raw["id"]),
        "asset_id": str(raw["asset_id"]),
        "start": round(max(0.0, start), 4),
        "end": round(end, 4),
        "at": round(max(0.0, at), 4),
    }


def _media_seconds(asset: dict | None, fallback: float) -> float:
    path = (asset or {}).get("path")
    if not isinstance(path, Path) or not path.is_file():
        return round(float(fallback), 4)
    if (asset or {}).get("kind") == "video":
        try:
            duration, _fps = _span(asset)
        except ValueError:
            return round(float(fallback), 4)
        return round(max(float(fallback), duration), 4)
    probed = _audio_duration(path)
    if probed <= 0:
        return round(float(fallback), 4)
    return round(max(float(fallback), probed), 4)


def _length(clip: dict) -> float:
    return sum(max(0.0, part["end"] - part["start"]) for part in clip["parts"])


def _index(clips: list[dict], clip_id: str) -> int:
    want = str(clip_id or "")
    for index, clip in enumerate(clips):
        if clip["id"] == want:
            return index
    return -1


def _push(session: dict) -> None:
    stack = session.setdefault("undo", [])
    stack.append(
        {
            "clips": copy.deepcopy(session["clips"]),
            "sounds": copy.deepcopy(session.get("sounds") or []),
        }
    )
    if len(stack) > 40:
        del stack[:-40]


def _min_piece(fps: float) -> float:
    if fps <= 0:
        fps = 30.0
    return max(1.0 / fps, _MIN_SECONDS)


def _split(session: dict, index: int, local: float) -> None:
    clip = session["clips"][index]
    cursor = 0.0
    parts = clip["parts"]
    for part_index, part in enumerate(parts):
        length = part["end"] - part["start"]
        if local <= cursor + length + 1e-4:
            into = local - cursor
            room = _min_piece(part["fps"])
            if into <= room or length - into <= room:
                raise ValueError("Move the playhead into the clip before cutting")
            _push(session)
            point = round(part["start"] + into, 4)
            left = dict(part)
            right = dict(part)
            left["end"] = point
            right["start"] = point
            session["clips"][index : index + 1] = [
                {"id": clip["id"], "parts": parts[:part_index] + [left]},
                {
                    "id": secrets.token_hex(4),
                    "parts": [right] + parts[part_index + 1 :],
                },
            ]
            return
        cursor += length
    raise ValueError("Move the playhead into the clip before cutting")


def _coalesce(parts: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for part in parts:
        if merged:
            prev = merged[-1]
            if (
                prev["asset_id"] == part["asset_id"]
                and abs(prev["end"] - part["start"]) < 0.05
            ):
                prev["end"] = round(max(prev["end"], part["end"]), 4)
                continue
        merged.append(dict(part))
    return merged


def _span(asset: dict) -> tuple[float, float]:
    path = asset.get("path")
    if not isinstance(path, Path) or not path.is_file():
        raise ValueError("That video is missing")
    try:
        meta = probe(path)
    except SceneError as exc:
        raise ValueError(str(exc)) from exc
    fps = float(meta["fps"] or 30) or 30.0
    duration = float(meta["duration"] or 0)
    if duration <= 0:
        duration = float(meta["frames"]) / fps
    return duration, fps


def _whole(assets: dict[str, dict], asset_id: str) -> dict:
    asset = assets.get(asset_id)
    if not asset or asset.get("kind") != "video":
        raise ValueError("That file is not a video")
    duration, fps = _span(asset)
    if duration < 0.2:
        raise ValueError("Video is too short to cut")
    return {
        "id": secrets.token_hex(4),
        "parts": [
            {
                "asset_id": asset_id,
                "start": 0.0,
                "end": round(duration, 4),
                "fps": fps,
            }
        ],
    }


def _same_whole(session: dict | None, asset_id: str, assets: dict[str, dict]) -> bool:
    if not session or len(session["clips"]) != 1:
        return False
    parts = session["clips"][0]["parts"]
    if len(parts) != 1 or parts[0]["asset_id"] != asset_id:
        return False
    asset = assets.get(asset_id)
    if not asset:
        return False
    try:
        duration, _fps = _span(asset)
    except ValueError:
        return False
    return parts[0]["start"] <= 0.02 and parts[0]["end"] >= duration - 0.05


def _range_name(start: float, end: float) -> str:
    """A short label for one saved piece, such as ``3–7s``."""

    def fmt(value: float) -> str:
        rounded = round(float(value), 1)
        if abs(rounded - round(rounded)) < 0.05:
            return str(int(round(rounded)))
        return f"{rounded:.1f}"

    return f"{fmt(start)}–{fmt(end)}s"


def _slice_sounds(sounds: list[dict], t0: float, t1: float) -> list[dict]:
    """The part of each lane sound that plays inside ``[t0, t1)``."""
    sliced = []
    for sound in sounds:
        span = float(sound["end"]) - float(sound["start"])
        begin = float(sound["at"])
        finish = begin + span
        left = max(begin, t0)
        right = min(finish, t1)
        if right - left < _MIN_SECONDS / 2:
            continue
        into = left - begin
        sliced.append(
            {
                "id": sound["id"],
                "asset_id": sound["asset_id"],
                "start": round(float(sound["start"]) + into, 4),
                "end": round(float(sound["start"]) + into + (right - left), 4),
                "at": round(left - t0, 4),
            }
        )
    return sliced


def _materialize(
    pkg: RallyPackage,
    assets: dict[str, dict],
    parts: list[dict],
    sounds: list[dict] | None = None,
    name: str = "Cut",
) -> dict:
    sounds = list(sounds or [])
    arranged = bool(sounds) or any(part.get("mute") for part in parts)
    if len(parts) == 1 and not arranged:
        asset = assets.get(parts[0]["asset_id"])
        if asset:
            try:
                duration, _fps = _span(asset)
            except ValueError:
                duration = 0
            if parts[0]["start"] <= 0.02 and duration and parts[0]["end"] >= duration - 0.05:
                return asset
    tmp = library.library_dir(pkg) / f".cut-{secrets.token_hex(4)}.mp4"
    try:
        _stitch(parts, assets, tmp, sounds)
        rows = list(assets.values())
        return library.ingest(
            pkg,
            tmp,
            name=library.next_name(rows, "video", name),
            kind="video",
            move=True,
        )
    finally:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)


def _stitch(
    parts: list[dict],
    assets: dict[str, dict],
    dest: Path,
    sounds: list[dict] | None = None,
) -> None:
    if sounds or any(part.get("mute") for part in parts):
        _stitch_mix(parts, list(sounds or []), assets, dest)
        return
    specs: list[tuple[Path, float, float, bool]] = []
    for part in parts:
        asset = assets.get(part["asset_id"])
        if not asset or not isinstance(asset.get("path"), Path):
            raise ValueError("A clip is missing its video")
        path = asset["path"]
        try:
            meta = probe(path)
        except SceneError as exc:
            raise ValueError(str(exc)) from exc
        specs.append((path, float(part["start"]), float(part["end"]), bool(meta["audio"])))
    use_audio = all(flag for _path, _start, _end, flag in specs)
    cmd = [_ffmpeg(), "-y"]
    for path, _start, _end, _audio in specs:
        cmd.extend(["-i", str(path)])
    filters: list[str] = []
    heads: list[str] = []
    for index, (_path, start, end, _audio) in enumerate(specs):
        filters.append(
            f"[{index}:v]trim=start={start:.4f}:end={end:.4f},setpts=PTS-STARTPTS[v{index}]"
        )
        if use_audio:
            filters.append(
                f"[{index}:a]atrim=start={start:.4f}:end={end:.4f},asetpts=PTS-STARTPTS[a{index}]"
            )
            heads.append(f"[v{index}][a{index}]")
        else:
            heads.append(f"[v{index}]")
    count = len(specs)
    if use_audio:
        filters.append("".join(heads) + f"concat=n={count}:v=1:a=1[v][a]")
        cmd.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
            ]
        )
    else:
        filters.append("".join(heads) + f"concat=n={count}:v=1:a=0[v]")
        cmd.extend(["-filter_complex", ";".join(filters), "-map", "[v]", "-an"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    )
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not dest.is_file() or dest.stat().st_size < 500:
        tail = (result.stderr or "")[-400:]
        raise ValueError(f"Cut failed: {tail}")


def _heard(asset: dict | None) -> bool:
    path = (asset or {}).get("path")
    if not isinstance(path, Path) or not path.is_file():
        return False
    try:
        return bool(probe(path).get("audio"))
    except SceneError:
        return _audio_duration(path) > 0


def _audio_duration(path: Path) -> float:
    from app.pipeline.scenes import _ffprobe

    if not path.is_file():
        return 0.0
    result = subprocess.run(
        [
            _ffprobe(),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,duration",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return 0.0
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return 0.0
    streams = data.get("streams") or []
    if not any(stream.get("codec_type") == "audio" for stream in streams):
        return 0.0
    duration = 0.0
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        for stream in streams:
            if stream.get("codec_type") != "audio":
                continue
            try:
                duration = float(stream.get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0.0
            if duration > 0:
                break
    return duration


def _media_path(assets: dict[str, dict], asset_id: str) -> Path:
    asset = assets.get(asset_id)
    path = (asset or {}).get("path")
    if not isinstance(path, Path) or not path.is_file():
        raise ValueError("A clip is missing its media")
    return path


def _stitch_mix(parts: list[dict], sounds: list[dict], assets: dict[str, dict], dest: Path) -> None:
    """Picture concat, with lane sounds and quiet pieces mixed in."""
    cmd = [_ffmpeg(), "-y"]
    inputs: list[Path] = []

    def add_input(path: Path) -> int:
        inputs.append(path)
        cmd.extend(["-i", str(path)])
        return len(inputs) - 1

    filters: list[str] = []
    vlabels: list[str] = []
    alabels: list[str] = []
    cursor = 0.0
    for index, part in enumerate(parts):
        path = _media_path(assets, part["asset_id"])
        src = add_input(path)
        start = float(part["start"])
        end = float(part["end"])
        filters.append(
            f"[{src}:v]trim=start={start:.4f}:end={end:.4f},setpts=PTS-STARTPTS[v{index}]"
        )
        vlabels.append(f"[v{index}]")
        if not part.get("mute") and _heard(assets.get(part["asset_id"])):
            delay = max(0, int(round(cursor * 1000)))
            filters.append(
                f"[{src}:a]atrim=start={start:.4f}:end={end:.4f},asetpts=PTS-STARTPTS,"
                f"aresample=44100,adelay={delay}|{delay}[a{index}]"
            )
            alabels.append(f"[a{index}]")
        cursor += max(0.0, end - start)
    filters.append("".join(vlabels) + f"concat=n={len(parts)}:v=1:a=0[v]")
    for index, sound in enumerate(sounds):
        path = _media_path(assets, sound["asset_id"])
        if _audio_duration(path) <= 0:
            raise ValueError("A sound on the lane has no audio")
        src = add_input(path)
        delay = max(0, int(round(float(sound["at"]) * 1000)))
        filters.append(
            f"[{src}:a]atrim=start={float(sound['start']):.4f}:end={float(sound['end']):.4f},"
            f"asetpts=PTS-STARTPTS,aresample=44100,adelay={delay}|{delay}[s{index}]"
        )
        alabels.append(f"[s{index}]")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(alabels) == 1:
        filters.append(
            f"{alabels[0]}aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a]"
        )
        tail = ["-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]", "-c:a", "aac", "-b:a", "128k", "-shortest"]
    elif alabels:
        filters.append(
            "".join(alabels)
            + f"amix=inputs={len(alabels)}:duration=longest:dropout_transition=0:normalize=0[a]"
        )
        tail = ["-filter_complex", ";".join(filters), "-map", "[v]", "-map", "[a]", "-c:a", "aac", "-b:a", "128k", "-shortest"]
    else:
        tail = ["-filter_complex", ";".join(filters), "-map", "[v]", "-an"]
    cmd.extend(tail)
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    )
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not dest.is_file() or dest.stat().st_size < 500:
        tail_err = (result.stderr or "")[-400:]
        raise ValueError(f"Cut failed: {tail_err}")
