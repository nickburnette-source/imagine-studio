"""Per-step scene timeline.

A step starts as one virtual scene: its take. Saving a cut copies that
take into history, writes scene files under scenes/{step}/, and stitches
them back into the step's take. The clip bar belongs to that stitched
take. A highlighted history take is its own copy and shows as one uncut
block. A history file named scene-{step}-{id} is a take for that clip:
the bar stays, and the cut changes only when that take is used on the clip.
Make writes the selected scene and stitches again.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.models.rally import RallyPackage, StepKind, StepStatus

_VIRTUAL = "main"
_MAX_CLIPS = 24
_MAX_SOUNDS = 16
_MAX_AUDIO_BYTES = 25 * 1024 * 1024
_MAX_AUDIO_FRAMES = 54000
_AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".ogg"}
_AUDIO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_ATTEMPT_NAME = re.compile(
    r"^scene-(?P<step>[A-Za-z0-9]+)-(?P<sid>[0-9a-f]{8})\."
    r"(?:(?P<kind>redo|nudge)\.)?"
    r"\d{8}T\d{6}Z(?:-\d+)?\.mp4$"
)


class SceneError(RuntimeError):
    pass


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _write_lock(pkg: RallyPackage, step_id: str) -> threading.RLock:
    """One writer at a time per step. Jobs render outside this lock."""
    key = f"{pkg.name}/{step_id}"
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SceneError("ffmpeg not found on PATH")
    return ffmpeg


def _ffprobe() -> str:
    probe = shutil.which("ffprobe")
    if not probe:
        raise SceneError("ffprobe not found on PATH")
    return probe


def video_name(pkg: RallyPackage, step_id: str) -> str:
    st = pkg.step(step_id)
    for name in st.outputs:
        if name.lower().endswith((".mp4", ".webm", ".mov")):
            return name
    from app.models.rally import clip_video_name

    return clip_video_name(step_id)


def canonical_video(pkg: RallyPackage, step_id: str) -> Path:
    return pkg.path_for(video_name(pkg, step_id))


def _stored(pkg: RallyPackage, step_id: str) -> list[dict]:
    raw = pkg.step(step_id).options.get("timeline")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id") or "").strip()
        rel = str(item.get("file") or "").strip()
        if not sid or not rel:
            continue
        out.append(dict(item))
    return out


def has_timeline(pkg: RallyPackage, step_id: str) -> bool:
    """True after a cut has been saved. A single uncut take is not stored."""
    return bool(_stored(pkg, step_id))


def shown_take_path(pkg: RallyPackage, step_id: str) -> Path | None:
    """Highlighted take, if it is a file inside this pack."""
    rel = str(pkg.step(step_id).options.get("shown_take") or "")
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel or ".." in Path(rel).parts:
        return None
    try:
        path = _safe(pkg, rel)
    except SceneError:
        return None
    return path if path.is_file() else None


def cut_is_showing(pkg: RallyPackage, step_id: str) -> bool:
    """The stored cut is the take on screen.

    Highlighting a different whole-step history file shows that copy, not
    the cut. A take for one clip on this timeline still counts as the cut:
    the bar stays, and the phone is only previewing that clip.
    """
    if not _stored(pkg, step_id):
        return False
    shown = shown_take_path(pkg, step_id)
    if shown is None:
        return True
    if clip_attempt_scene(pkg, step_id, shown):
        return True
    canon = canonical_video(pkg, step_id)
    try:
        if canon.is_file() and shown.resolve() == canon.resolve():
            return True
    except OSError:
        return True
    if not canon.is_file():
        return False
    shown_fp = _fp(shown)
    return bool(shown_fp and shown_fp == _fp(canon))


def _safe(pkg: RallyPackage, rel: str) -> Path:
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel or ".." in Path(rel).parts:
        raise SceneError("Bad scene path")
    root = pkg.dir().resolve()
    path = (root / rel).resolve()
    if path != root and root not in path.parents:
        raise SceneError("Bad scene path")
    return path


def _ratio(text: str) -> float:
    raw = (text or "").strip()
    if not raw or raw == "0/0":
        return 0.0
    if "/" in raw:
        num, den = raw.split("/", 1)
        try:
            den_f = float(den)
            return float(num) / den_f if den_f else 0.0
        except ValueError:
            return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def probe(path: Path) -> dict:
    """fps, frame count, duration seconds, and whether the file has audio."""
    path = Path(path)
    if not path.is_file():
        raise SceneError(f"Missing video: {path.name}")
    result = subprocess.run(
        [
            _ffprobe(),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,nb_frames,avg_frame_rate,r_frame_rate,duration",
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
        err = (result.stderr or result.stdout or "").strip()
        raise SceneError(f"Could not read {path.name}: {err[-240:]}")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise SceneError(f"Could not read {path.name}") from exc
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise SceneError(f"{path.name} has no video track")
    fps = _ratio(str(video.get("avg_frame_rate") or "")) or _ratio(
        str(video.get("r_frame_rate") or "")
    )
    if fps <= 0:
        fps = 30.0
    duration = 0.0
    try:
        duration = float(video.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        try:
            duration = float((data.get("format") or {}).get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
    frames = 0
    raw_frames = str(video.get("nb_frames") or "")
    if raw_frames.isdigit():
        frames = int(raw_frames)
    if frames <= 0 and duration > 0:
        frames = int(round(duration * fps))
    if frames <= 0:
        raise SceneError(f"Could not count frames in {path.name}")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    return {
        "fps": fps,
        "frames": frames,
        "duration": duration if duration > 0 else frames / fps,
        "audio": has_audio,
    }


def _fmt(frames: int, fps: float) -> str:
    if fps <= 0:
        return ""
    return f"{frames / fps:.1f}s"


def _one_block(path: Path, rel: str) -> dict:
    meta = probe(path)
    return {
        "id": _VIRTUAL,
        "file": rel,
        "frames": meta["frames"],
        "fps": meta["fps"],
        "label": _fmt(meta["frames"], meta["fps"]),
        "virtual": True,
        "missing": False,
    }


def bar_items(pkg: RallyPackage, step_id: str) -> list[dict]:
    """Scenes for the step bar. Empty when the step has no take yet.

    A highlighted whole take that is not the stitch is one uncut block.
    A take for one clip on this timeline keeps the saved scenes.
    """
    st = pkg.step(step_id)
    if st.kind not in {StepKind.VIDEO, StepKind.IDLE}:
        return []
    stored = _stored(pkg, step_id)
    if stored and not cut_is_showing(pkg, step_id):
        shown = shown_take_path(pkg, step_id)
        if shown is not None and shown.is_file():
            rel = str(st.options.get("shown_take") or "").replace("\\", "/").lstrip("/")
            return [_one_block(shown, rel)]
    if stored:
        items: list[dict] = []
        for item in stored:
            path = _safe(pkg, str(item["file"]))
            frames = int(item.get("frames") or 0)
            fps = float(item.get("fps") or 0)
            if path.is_file() and frames <= 0:
                meta = probe(path)
                frames = meta["frames"]
                fps = meta["fps"]
            items.append(
                {
                    "id": str(item["id"]),
                    "file": str(item["file"]),
                    "frames": frames,
                    "fps": fps or 30.0,
                    "label": _fmt(frames, fps or 30.0),
                    "virtual": False,
                    "missing": not path.is_file(),
                    "fresh": bool(item.get("fresh")),
                    "pending": bool(item.get("pending")),
                }
            )
        return items
    video = canonical_video(pkg, step_id)
    if not video.is_file():
        return []
    return [_one_block(video, video_name(pkg, step_id))]


def selected_id(pkg: RallyPackage, step_id: str) -> str:
    items = bar_items(pkg, step_id)
    if not items:
        return ""
    want = str(pkg.step(step_id).options.get("scene_id") or "")
    if any(item["id"] == want for item in items):
        return want
    return items[0]["id"]


def select_scene(pkg: RallyPackage, step_id: str, scene_id: str) -> None:
    """Select a clip. Reloads first so a stale package cannot put an old cut back."""
    with _write_lock(pkg, step_id):
        fresh = RallyPackage.load(pkg.dir())
        _select_scene(fresh, step_id, scene_id)
        _mirror_cut(pkg, fresh, step_id)


def _mirror_cut(pkg: RallyPackage, fresh: RallyPackage, step_id: str) -> None:
    """Copy the saved cut onto ``pkg`` so a later save of that object keeps it."""
    src = fresh.step(step_id).options
    dest = pkg.step(step_id).options
    for key in ("timeline", "scene_id", "shown_take"):
        if key in src:
            dest[key] = src[key]
        else:
            dest.pop(key, None)


def _select_scene(pkg: RallyPackage, step_id: str, scene_id: str) -> None:
    if not has_timeline(pkg, step_id):
        return
    scene_id = str(scene_id or "").strip()
    st = pkg.step(step_id)
    ids = [str(item["id"]) for item in _stored(pkg, step_id)]
    if scene_id not in ids:
        return
    if str(st.options.get("scene_id") or "") == scene_id:
        return
    shown = shown_take_path(pkg, step_id)
    if shown is not None:
        attempt = clip_attempt_scene(pkg, step_id, shown)
        if attempt and attempt != scene_id:
            st.options.pop("shown_take", None)
    st.options["scene_id"] = scene_id
    pkg.save()


def reorder_scenes(pkg: RallyPackage, step_id: str, ids: list[str]) -> bool:
    with _write_lock(pkg, step_id):
        fresh = RallyPackage.load(pkg.dir())
        return _reorder_scenes(fresh, step_id, ids)


def _reorder_scenes(pkg: RallyPackage, step_id: str, ids: list[str]) -> bool:
    """Put the timeline clips in this order. The picture in each clip stays."""
    if _viewing_copy(pkg, step_id):
        raise SceneError(
            "This is a copy of the take. Go back to the edited take to edit."
        )
    stored = _stored(pkg, step_id)
    if not stored:
        raise SceneError("Nothing to reorder")
    current = [str(item["id"]) for item in stored]
    want = [str(item or "").strip() for item in ids]
    if want == current:
        return False
    if len(want) != len(current) or sorted(want) != sorted(current):
        raise SceneError("Reorder the clips that are already on the timeline")
    by_id = {str(item["id"]): item for item in stored}
    timeline = [by_id[sid] for sid in want]
    push_timeline_undo(pkg, step_id)
    committed = False
    try:
        st = pkg.step(step_id)
        st.options["timeline"] = timeline
        if str(st.options.get("scene_id") or "") not in want:
            st.options["scene_id"] = want[0]
        _touch_review(pkg, step_id)
        restitch(pkg, step_id, save=False)
        pkg.save()
        committed = True
        return True
    except Exception:
        if not committed:
            discard_latest_undo(pkg, step_id)
        raise


def scene_index(pkg: RallyPackage, step_id: str) -> int:
    want = selected_id(pkg, step_id)
    for i, item in enumerate(_stored(pkg, step_id)):
        if str(item["id"]) == want:
            return i
    return 0


def _selected_item(pkg: RallyPackage, step_id: str) -> dict | None:
    items = bar_items(pkg, step_id)
    if not items:
        return None
    want = selected_id(pkg, step_id)
    for item in items:
        if item["id"] == want:
            return item
    return items[0]


def selected_path(pkg: RallyPackage, step_id: str) -> Path | None:
    item = _selected_item(pkg, step_id)
    if item is None or item.get("missing"):
        return None
    return _safe(pkg, item["file"])


def display_media(pkg: RallyPackage, step_id: str) -> Path | None:
    """Selected scene file when the cut is on screen.

    The rally player does not use this. It plays the stitched take.
    A highlighted copy is not a scene of the cut.
    """
    if not has_timeline(pkg, step_id) or not cut_is_showing(pkg, step_id):
        return None
    return selected_path(pkg, step_id)


def write_target(pkg: RallyPackage, step_id: str) -> Path:
    """Where Make / Nudge should write when a timeline is saved."""
    item = _selected_item(pkg, step_id)
    if item is None or item.get("virtual"):
        return canonical_video(pkg, step_id)
    path = _safe(pkg, item["file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def clear_timeline(pkg: RallyPackage, step_id: str) -> None:
    """Drop saved scenes. Used when a history take replaces the step video."""
    st = pkg.step(step_id)
    had = has_timeline(pkg, step_id)
    st.options.pop("timeline", None)
    st.options.pop("scene_id", None)
    folder = pkg.dir() / "scenes" / step_id
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
    if had:
        pkg.save()


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


def _scene_rel(step_id: str, scene_id: str) -> str:
    return f"scenes/{step_id}/{scene_id}.mp4"


def _touch_review(pkg: RallyPackage, step_id: str) -> None:
    """A cut changes the take, so the step has to be reviewed again."""
    st = pkg.step(step_id)
    if st.status != StepStatus.LOCKED:
        st.status = StepStatus.NEEDS_REVIEW
        st.error = None
    pkg.final_review_approved = False
    pkg.publish_tier = None


def _drop_replaced(path: Path) -> None:
    try:
        if "scenes" in Path(path).parts:
            Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _run(cmd: list[str], label: str) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        raise SceneError(f"{label} failed: {err[-400:]}")


def _extract(src: Path, dest: Path, start: int, end: int, fps: float, audio: bool) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = f"trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS"
    cmd = [_ffmpeg(), "-y", "-i", str(src), "-vf", vf]
    if audio and fps > 0:
        # Pad to the picture. A short tail makes the next clip's sound start early.
        dur = (end - start) / fps
        # Reset timestamps before apad. apad reads the incoming clock, and a
        # trim still starts at the source offset, so padding first drops the audio.
        af = (
            f"atrim=start={start / fps:.6f}:end={end / fps:.6f},"
            "asetpts=PTS-STARTPTS,"
            f"apad=whole_dur={dur:.6f},atrim=end={dur:.6f}"
        )
        cmd += ["-af", af, "-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]
    cmd += [
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
    _run(cmd, "Cut")


def _clean_name(raw: str) -> str:
    cleaned = re.sub(r"[^\w .()-]+", "", str(raw or ""), flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()[:40]
    return cleaned or "Audio"


def _audio_info(path: Path) -> tuple[float, bool]:
    """Duration seconds and whether an audio stream exists. Video optional."""
    if not path.is_file():
        raise SceneError("Missing audio")
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
        raise SceneError(f"Could not read {path.name}")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise SceneError(f"Could not read {path.name}") from exc
    streams = data.get("streams") or []
    has = any(s.get("codec_type") == "audio" for s in streams)
    dur = 0.0
    for stream in streams:
        if stream.get("codec_type") != "audio":
            continue
        try:
            dur = float(stream.get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0.0
        if dur > 0:
            break
    if dur <= 0:
        try:
            dur = float((data.get("format") or {}).get("duration") or 0)
        except (TypeError, ValueError):
            dur = 0.0
    return dur, has


def _audio_title(path: Path) -> str:
    result = subprocess.run(
        [
            _ffprobe(),
            "-v",
            "error",
            "-show_entries",
            "format_tags=title",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    title = (result.stdout or "").strip()
    return _clean_name(title) if title else "Audio"


def _audio_file(pkg: RallyPackage, rel: str) -> Path:
    """A sound already stored under this pack's audio folder."""
    rel = str(rel or "").replace("\\", "/").lstrip("/")
    parts = Path(rel).parts
    if len(parts) != 2 or parts[0] != "audio" or not _AUDIO_NAME.match(parts[1]):
        raise SceneError("Bad audio path")
    if Path(parts[1]).suffix.lower() not in _AUDIO_EXTS:
        raise SceneError("Bad audio path")
    path = _safe(pkg, rel)
    if path.parent.resolve() != (pkg.dir() / "audio").resolve():
        raise SceneError("Bad audio path")
    if not path.is_file():
        raise SceneError("Missing audio")
    return path


def _pack_video(pkg: RallyPackage, rel: str) -> Path:
    """A clip video in this pack: the take, a scene, or a history file."""
    rel = str(rel or "").replace("\\", "/").lstrip("/")
    path = _safe(pkg, rel)
    if path.suffix.lower() not in {".mp4", ".webm", ".mov"}:
        raise SceneError("That file is not a clip")
    root = pkg.dir().resolve()
    try:
        inside = path.resolve().relative_to(root)
    except ValueError as exc:
        raise SceneError("Bad scene path") from exc
    parts = inside.parts
    ok = (
        len(parts) == 1
        or (len(parts) == 2 and parts[0] == "history")
        or (len(parts) == 3 and parts[0] == "scenes")
    )
    if not ok or not path.is_file():
        raise SceneError("That clip is not in this rally")
    return path


def _encode_audio(
    src: Path,
    dest: Path,
    *,
    title: str,
    audio_filter: str = "",
    empty: str = "That clip has no audio",
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_ffmpeg(), "-y", "-i", str(src), "-vn"]
    if audio_filter:
        cmd += ["-af", audio_filter]
    cmd += [
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-metadata",
        f"title={title}",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    try:
        _run(cmd, "Audio")
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    if not dest.is_file() or dest.stat().st_size < 32:
        dest.unlink(missing_ok=True)
        raise SceneError(empty)


def extract_audio(
    pkg: RallyPackage,
    src_rel: str,
    start: int,
    end: int,
    timeline_fps: float,
    name: str = "",
) -> dict:
    """Copy a clip's audio into pack/audio. Does not change the clip."""
    src = _pack_video(pkg, src_rel)
    meta = probe(src)
    if not meta["audio"]:
        raise SceneError("That clip has no audio")
    src_fps = float(meta["fps"])
    if timeline_fps <= 0:
        timeline_fps = src_fps
    try:
        start_i = int(start)
        end_i = int(end)
    except (TypeError, ValueError) as exc:
        raise SceneError("That range is outside the clip") from exc
    frames = int(meta["frames"])
    if start_i < 0 or end_i > frames or end_i <= start_i:
        raise SceneError("That range is outside the clip")
    if (end_i - start_i) / src_fps > 600:
        raise SceneError("That clip is too long to lift")
    sid = _new_id()
    rel = f"audio/{sid}.m4a"
    dest = _safe(pkg, rel)
    title = _clean_name(name)
    _encode_audio(
        src,
        dest,
        title=title,
        audio_filter=(
            f"atrim=start={start_i / src_fps:.6f}:end={end_i / src_fps:.6f},"
            "asetpts=PTS-STARTPTS"
        ),
    )
    seconds = (end_i - start_i) / src_fps
    return {
        "file": rel,
        "name": title,
        "frames": max(1, int(round(seconds * timeline_fps))),
    }


def store_audio_upload(
    pkg: RallyPackage,
    data: bytes,
    original_name: str,
    timeline_fps: float,
) -> dict:
    """Transcode an uploaded sound into pack/audio."""
    if timeline_fps <= 0:
        timeline_fps = 30.0
    if len(data) > _MAX_AUDIO_BYTES:
        raise SceneError("Sound file is over 25 MB")
    if not data:
        raise SceneError("Empty sound file")
    ext = Path(original_name or "").suffix.lower()
    if ext not in _AUDIO_EXTS:
        raise SceneError("Use an m4a, mp3, wav, aac, or ogg file")
    folder = pkg.dir() / "audio"
    folder.mkdir(parents=True, exist_ok=True)
    sid = _new_id()
    raw = folder / f".{sid}{ext}"
    dest = folder / f"{sid}.m4a"
    raw.write_bytes(data)
    title = _clean_name(Path(original_name or "").stem)
    try:
        _encode_audio(raw, dest, title=title, empty="That file has no audio")
    finally:
        raw.unlink(missing_ok=True)
    dur, has = _audio_info(dest)
    if not has or dur <= 0:
        dest.unlink(missing_ok=True)
        raise SceneError("That file has no audio")
    if dur > 600:
        dest.unlink(missing_ok=True)
        raise SceneError("Sound is longer than 10 minutes")
    return {
        "file": f"audio/{dest.name}",
        "name": title,
        "frames": max(1, int(round(dur * timeline_fps))),
    }


def editor_bin(
    pkg: RallyPackage,
    step_id: str,
    open_rel: str,
    timeline_fps: float,
) -> list[dict]:
    """Saved sounds, plus other scenes on this step that have audio."""
    if timeline_fps <= 0:
        timeline_fps = 30.0
    items: list[dict] = []
    folder = pkg.dir() / "audio"
    if folder.is_dir():
        for path in sorted(folder.glob("*.m4a")):
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                dur, has = _audio_info(path)
            except SceneError:
                continue
            if not has or dur <= 0:
                continue
            items.append(
                {
                    "kind": "file",
                    "file": f"audio/{path.name}",
                    "name": _audio_title(path),
                    "frames": max(1, int(round(dur * timeline_fps))),
                }
            )
    open_rel = str(open_rel or "").replace("\\", "/")
    try:
        shown = bar_items(pkg, step_id)
    except SceneError:
        shown = []
    selected = ""
    try:
        selected = selected_id(pkg, step_id)
    except SceneError:
        selected = ""
    for index, item in enumerate(shown):
        rel = str(item.get("file") or "")
        if not rel or rel == open_rel or item.get("missing"):
            continue
        try:
            path = _pack_video(pkg, rel)
            meta = probe(path)
        except SceneError:
            continue
        if not meta["audio"]:
            continue
        seconds = float(meta["duration"]) or (meta["frames"] / meta["fps"])
        name = (
            "This clip"
            if str(item.get("id") or "") == selected
            else f"Clip {index + 1}"
        )
        items.append(
            {
                "kind": "scene",
                "file": rel,
                "name": name,
                "in": 0,
                "out": int(meta["frames"]),
                "frames": max(1, int(round(seconds * timeline_fps))),
            }
        )
    return items


def _parse_audio_blocks(
    pkg: RallyPackage,
    audio: list | None,
    fps: float,
) -> list[dict]:
    if not audio:
        return []
    if not isinstance(audio, list):
        raise SceneError("Bad audio")
    if len(audio) > _MAX_SOUNDS:
        raise SceneError(f"Too many sounds (max {_MAX_SOUNDS})")
    blocks: list[dict] = []
    for block in audio:
        if not isinstance(block, dict):
            raise SceneError("Bad audio")
        path = _audio_file(pkg, str(block.get("file") or ""))
        try:
            at = int(block.get("at"))
            count = int(block.get("frames"))
            offset = int(block.get("offset") or 0)
        except (TypeError, ValueError) as exc:
            raise SceneError("Bad audio") from exc
        if at < 0 or offset < 0 or count <= 0 or count > _MAX_AUDIO_FRAMES:
            raise SceneError("Bad audio")
        dur, has = _audio_info(path)
        if not has or dur <= 0:
            raise SceneError("Missing audio")
        blocks.append(
            {
                "path": path,
                "at": at,
                "frames": count,
                "offset": offset,
                "file_frames": max(1, int(round(dur * fps))),
            }
        )
    return blocks


def _blocks_overlap(blocks: list[dict], total: int) -> bool:
    for block in blocks:
        if block["at"] < total and block["at"] + block["frames"] > 0:
            return True
    return False


def _layers_for(blocks: list[dict], seq: int, length: int) -> list[tuple[Path, int, int, int]]:
    """Overlaps as (path, offset into the sound, delay into the clip, length)."""
    layers: list[tuple[Path, int, int, int]] = []
    for block in blocks:
        start = int(block["at"])
        end = start + int(block["frames"])
        overlap_start = max(start, seq)
        overlap_end = min(end, seq + length)
        if overlap_end <= overlap_start:
            continue
        offset = overlap_start - start + int(block.get("offset") or 0)
        delay = overlap_start - seq
        span = overlap_end - overlap_start
        file_frames = int(block["file_frames"])
        if offset >= file_frames:
            continue
        span = min(span, file_frames - offset)
        if span <= 0:
            continue
        layers.append((block["path"], offset, delay, span))
    return layers


def _mix_clip(
    src: Path,
    dest: Path,
    start: int,
    end: int,
    fps: float,
    keep_source: bool,
    layers: list[tuple[Path, int, int, int]],
) -> None:
    """Video range plus lane audio. Source audio is included when keep_source."""
    if not layers:
        _extract(src, dest, start, end, fps, keep_source)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_ffmpeg(), "-y", "-i", str(src)]
    for layer in layers:
        cmd += ["-i", str(layer[0])]
    chains = [
        f"[0:v]trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS[v]"
    ]
    labels: list[str] = []
    if keep_source:
        chains.append(
            f"[0:a]atrim=start={start / fps:.6f}:end={end / fps:.6f},"
            "asetpts=PTS-STARTPTS,"
            "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
            "[srca]"
        )
        labels.append("[srca]")
    for index, layer in enumerate(layers):
        _path, offset, delay, span = layer
        delay_ms = max(0, int(round(delay / fps * 1000)))
        delay_f = f",adelay={delay_ms}|{delay_ms}" if delay_ms else ""
        label = f"b{index}"
        chains.append(
            f"[{index + 1}:a]atrim=start={offset / fps:.6f}:"
            f"end={(offset + span) / fps:.6f},asetpts=PTS-STARTPTS,"
            "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
            f"{delay_f}[{label}]"
        )
        labels.append(f"[{label}]")
    # Pad to the picture. A short lift otherwise ends the audio early, and the
    # stitch then plays every later clip's sound ahead of its picture.
    clip_s = (end - start) / fps
    fit = (
        f"asetpts=PTS-STARTPTS,apad=whole_dur={clip_s:.6f},"
        f"atrim=end={clip_s:.6f}[aout]"
    )
    if len(labels) == 1:
        chains.append(f"{labels[0]}{fit}")
    else:
        chains.append(
            "".join(labels)
            + f"amix=inputs={len(labels)}:duration=longest:"
            "dropout_transition=0:normalize=0,"
            + fit
        )
    cmd += [
        "-filter_complex",
        ";".join(chains),
        "-map",
        "[v]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    _run(cmd, "Audio")


def _with_audio(path: Path) -> Path:
    """Temp sibling that has an audio track, for a uniform stitch."""
    if probe(path)["audio"]:
        return path
    dest = path.with_name(path.stem + ".aud.mp4")
    _run(
        [
            _ffmpeg(),
            "-y",
            "-i",
            str(path),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-shortest",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(dest),
        ],
        "Audio",
    )
    return dest


def _stitch_files(paths: list[Path], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(paths) == 1:
        if paths[0].resolve() != dest.resolve():
            shutil.copy2(paths[0], dest)
        return
    temps: list[Path] = []
    try:
        ready: list[Path] = []
        audio_flags = [probe(p)["audio"] for p in paths]
        if any(audio_flags) and not all(audio_flags):
            for path, has in zip(paths, audio_flags):
                if has:
                    ready.append(path)
                else:
                    temp = _with_audio(path)
                    temps.append(temp)
                    ready.append(temp)
            use_audio = True
        else:
            ready = list(paths)
            use_audio = all(audio_flags)
        listing = dest.with_name(dest.stem + ".concat.txt")
        lines = []
        for path in ready:
            escaped = str(path.resolve()).replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temps.append(listing)
        cmd = [
            _ffmpeg(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
        ]
        if use_audio:
            cmd += ["-c:a", "aac", "-b:a", "128k"]
        else:
            cmd += ["-an"]
        cmd += ["-movflags", "+faststart", str(dest)]
        _run(cmd, "Stitch")
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)


def _refresh_meta(pkg: RallyPackage, step_id: str) -> None:
    items = _stored(pkg, step_id)
    for item in items:
        path = _safe(pkg, str(item["file"]))
        if not path.is_file():
            continue
        meta = probe(path)
        item["frames"] = meta["frames"]
        item["fps"] = meta["fps"]
    pkg.step(step_id).options["timeline"] = items


def restitch(pkg: RallyPackage, step_id: str, *, save: bool = True) -> Path:
    """Write the step take as the scenes in order."""
    items = _stored(pkg, step_id)
    if not items:
        return canonical_video(pkg, step_id)
    paths = [_safe(pkg, str(item["file"])) for item in items]
    missing = [p.name for p in paths if not p.is_file()]
    if missing:
        raise SceneError("Missing scene file: " + ", ".join(missing))
    dest = canonical_video(pkg, step_id)
    # The stitch about to be replaced is the cut the user can still pick.
    _archive_canonical(pkg, step_id)
    tmp = dest.with_name(dest.stem + ".stitch.mp4")
    tmp.unlink(missing_ok=True)
    try:
        _stitch_files(paths, tmp)
        if not tmp.is_file() or tmp.stat().st_size < 1000:
            raise SceneError("Stitch produced an empty take")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    _refresh_meta(pkg, step_id)
    if save:
        pkg.save()
    return dest


def _archive_canonical(pkg: RallyPackage, step_id: str) -> None:
    src = canonical_video(pkg, step_id)
    if not src.is_file():
        return
    fp = _fp(src)
    if fp and pkg._history_has_fingerprint(step_id, fp):
        return
    hist = pkg.dir() / "history"
    hist.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = video_name(pkg, step_id)
    # ``.cut.`` keeps an autosaved stitch out of the take strip. A real
    # generation still uses ``{stem}.{stamp}.mp4``.
    dest = hist / f"{Path(name).stem}.cut.{stamp}{Path(name).suffix}"
    shutil.copy2(src, dest)


_HIST_STAMP = re.compile(r"\.(\d{8}T\d{6})Z")


def history_is_cut_archive(pkg: RallyPackage, step_id: str, path: Path) -> bool:
    """True for a stitched cut parked in history, not a new generation.

    New archives are named ``{stem}.cut.{stamp}``. Older ones share a
    timestamp with the undo snapshot taken just before that restitch.
    """
    name = Path(path).name
    if ".cut." in name:
        return True
    match = _HIST_STAMP.search(name)
    if match is None:
        return False
    try:
        stamp = datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return False
    root = pkg.dir() / "history" / f".undo-{step_id}"
    if not root.is_dir():
        return False
    for kid in root.iterdir():
        if not kid.is_dir() or len(kid.name) < 15:
            continue
        try:
            when = datetime.strptime(kid.name[:15], "%Y%m%dT%H%M%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        if abs((when - stamp).total_seconds()) <= 2:
            return True
    return False


def _fp(path: Path) -> str:
    from app.models.rally import file_fingerprint

    return file_fingerprint(path)


def _backup_scene(pkg: RallyPackage, step_id: str) -> str | None:
    item = _selected_item(pkg, step_id)
    if item is None or item.get("virtual") or item.get("missing"):
        return None
    src = _safe(pkg, item["file"])
    if not src.is_file():
        return None
    hist = pkg.dir() / "history"
    dest = _attempt_dest(hist, step_id, str(item["id"]))
    shutil.copy2(src, dest)
    return f"history/{dest.name}"


def scene_attempt_id(step_id: str, path: Path) -> str | None:
    """Scene id encoded in a history attempt name, if this file is one."""
    match = _ATTEMPT_NAME.match(Path(path).name)
    if match is None or match.group("step") != step_id:
        return None
    return match.group("sid")


def attempt_kind(path: Path) -> str:
    """``redo`` or ``nudge`` when the file name says so. Older takes are blank."""
    match = _ATTEMPT_NAME.match(Path(path).name)
    if match is None:
        return ""
    return match.group("kind") or ""


def clip_attempt_scene(
    pkg: RallyPackage, step_id: str, path: Path | None
) -> str | None:
    """Scene id when ``path`` is a take for a clip still on this timeline."""
    if path is None:
        return None
    sid = scene_attempt_id(step_id, path)
    if not sid:
        return None
    ids = {str(item.get("id") or "") for item in _stored(pkg, step_id)}
    if sid not in ids:
        return None
    return sid


def scene_attempts(pkg: RallyPackage, step_id: str, scene_id: str) -> list[Path]:
    """History takes for one clip, newest first. Does not include the cut."""
    hist = pkg.dir() / "history"
    if not hist.is_dir():
        return []
    scene_id = str(scene_id or "").strip()
    rows = [
        p
        for p in hist.glob(f"scene-{step_id}-{scene_id}.*.mp4")
        if p.is_file() and scene_attempt_id(step_id, p) == scene_id
    ]
    rows.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return rows


def _attempt_dest(hist: Path, step_id: str, scene_id: str, kind: str = "") -> Path:
    hist.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    token = f"{kind}." if kind in {"redo", "nudge"} else ""
    dest = hist / f"scene-{step_id}-{scene_id}.{token}{stamp}.mp4"
    n = 2
    while dest.exists():
        dest = hist / f"scene-{step_id}-{scene_id}.{token}{stamp}-{n}.mp4"
        n += 1
    return dest


def save_preview_attempt(pkg: RallyPackage, step_id: str, src: Path) -> str:
    """Park a nudge of a highlighted take. That file and the cut stay."""
    src = Path(src)
    if not src.is_file() or src.stat().st_size < 1000:
        raise SceneError("Nudge produced an empty take")
    hist = pkg.dir() / "history"
    hist.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = video_name(pkg, step_id)
    stem = Path(name).stem
    suf = Path(name).suffix
    dest = hist / f"{stem}.{stamp}{suf}"
    n = 2
    while dest.exists():
        dest = hist / f"{stem}.{stamp}-{n}{suf}"
        n += 1
    shutil.move(str(src), str(dest))
    return f"history/{dest.name}"


def save_scene_attempt(
    pkg: RallyPackage,
    step_id: str,
    scene_id: str,
    src: Path,
    *,
    kind: str = "",
) -> str:
    """Park a render in history. Does not touch the cut.

    ``kind`` is ``redo`` or ``nudge`` for editor takes. Blank keeps the
    older name, used for backups and the rally-page nudge.
    """
    src = Path(src)
    if not src.is_file():
        raise SceneError("Nudge produced an empty take")
    kind = kind if kind in {"redo", "nudge"} else ""
    hist = pkg.dir() / "history"
    dest = _attempt_dest(hist, step_id, scene_id, kind)
    shutil.move(str(src), str(dest))
    return f"history/{dest.name}"


def drop_redo_attempts(
    pkg: RallyPackage,
    step_id: str,
    scene_id: str,
    *,
    keep: Path | None = None,
) -> None:
    """Remove earlier regenerates for this clip. Nudges and the cut stay."""
    keep_res = None
    if keep is not None:
        try:
            keep_res = Path(keep).resolve()
        except OSError:
            keep_res = None
    for path in scene_attempts(pkg, step_id, scene_id):
        if attempt_kind(path) != "redo":
            continue
        try:
            if keep_res is not None and path.resolve() == keep_res:
                continue
        except OSError:
            continue
        path.unlink(missing_ok=True)


def reassign_attempts(
    pkg: RallyPackage, step_id: str, old_id: str, new_id: str
) -> list[tuple[Path, Path]]:
    """Keep parked takes when a cut gives the clip a new id.

    Each pair is ``(new path, old path)`` so a failed save can rename them back.
    """
    old_id = str(old_id or "").strip()
    new_id = str(new_id or "").strip()
    if (
        not _SLOT.match(old_id)
        or not _SLOT.match(new_id)
        or old_id == new_id
    ):
        return []
    hist = pkg.dir() / "history"
    if not hist.is_dir():
        return []
    prefix = f"scene-{step_id}-{old_id}."
    moved: list[tuple[Path, Path]] = []
    for path in list(hist.glob(f"{prefix}*.mp4")):
        if scene_attempt_id(step_id, path) != old_id:
            continue
        dest = hist / path.name.replace(prefix, f"scene-{step_id}-{new_id}.", 1)
        if dest.exists():
            continue
        path.rename(dest)
        moved.append((dest, path))
    return moved


def _restore_moves(moved: list[tuple[Path, Path]]) -> None:
    for dest, src in reversed(moved):
        try:
            if dest.is_file() and not src.exists():
                dest.rename(src)
        except OSError:
            continue


def _note_moves(
    pkg: RallyPackage, step_id: str, moved: list[tuple[Path, Path]]
) -> None:
    """Remember take renames on the latest undo snapshot, so undo puts them back."""
    if not moved:
        return
    kids = _undo_dirs(pkg, step_id)
    if not kids:
        return
    path = kids[-1] / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    rows = manifest.get("moves")
    if not isinstance(rows, list):
        rows = []
        manifest["moves"] = rows
    root = pkg.dir().resolve()
    for dest, src in moved:
        try:
            rows.append(
                {
                    "dest": dest.resolve().relative_to(root).as_posix(),
                    "src": src.resolve().relative_to(root).as_posix(),
                }
            )
        except (OSError, ValueError):
            continue
    path.write_text(json.dumps(manifest), encoding="utf-8")


_UNDO_CAP = 40


def _undo_root(pkg: RallyPackage, step_id: str) -> Path:
    return pkg.dir() / "history" / f".undo-{step_id}"


def _undo_dirs(pkg: RallyPackage, step_id: str) -> list[Path]:
    root = _undo_root(pkg, step_id)
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.iterdir() if (path / "manifest.json").is_file()
    )


def undo_count(pkg: RallyPackage, step_id: str) -> int:
    return len(_undo_dirs(pkg, step_id))


def _new_undo_dir(pkg: RallyPackage, step_id: str) -> Path:
    root = _undo_root(pkg, step_id)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = root / stamp
    dest.mkdir()
    kids = _undo_dirs(pkg, step_id)
    while len(kids) > _UNDO_CAP:
        shutil.rmtree(kids.pop(0), ignore_errors=True)
    return dest


def discard_latest_undo(pkg: RallyPackage, step_id: str) -> None:
    kids = _undo_dirs(pkg, step_id)
    if kids:
        shutil.rmtree(kids[-1], ignore_errors=True)


def push_timeline_undo(pkg: RallyPackage, step_id: str) -> None:
    """Copy the timeline files so the next cut, join, or delete can be undone."""
    dest = _new_undo_dir(pkg, step_id)
    st = pkg.step(step_id)
    stored = _stored(pkg, step_id)
    rels: list[str] = []
    if stored:
        rels.extend(str(item["file"]) for item in stored)
    else:
        canon = canonical_video(pkg, step_id)
        if canon.is_file():
            rels.append(
                canon.resolve().relative_to(pkg.dir().resolve()).as_posix()
            )
    shown = str(st.options.get("shown_take") or "")
    if shown:
        rels.append(shown)
    files: list[dict] = []
    seen: set[str] = set()
    for rel in rels:
        if rel in seen:
            continue
        seen.add(rel)
        try:
            src = _safe(pkg, rel)
        except SceneError:
            continue
        if not src.is_file():
            continue
        name = f"{len(files):02d}-{Path(rel).name}"
        shutil.copy2(src, dest / name)
        files.append({"rel": rel, "name": name})
    manifest = {
        "kind": "timeline",
        "timeline": stored,
        "scene_id": str(st.options.get("scene_id") or ""),
        "shown_take": shown,
        "files": files,
    }
    (dest / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def push_attempt_undo(pkg: RallyPackage, step_id: str, rel: str) -> None:
    """Undo deletes this parked take. The cut is not part of the snapshot."""
    dest = _new_undo_dir(pkg, step_id)
    name = Path(rel).name
    token = name.split(".", 1)[1] if "." in name else name
    manifest = {
        "kind": "attempt",
        "step": step_id,
        "token": token,
        "rel": rel.replace("\\", "/"),
    }
    (dest / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _undo_attempt(pkg: RallyPackage, step_id: str, manifest: dict) -> None:
    token = str(manifest.get("token") or "")
    rel = str(manifest.get("rel") or "")
    hist = pkg.dir() / "history"
    doomed: list[Path] = []
    if rel:
        try:
            direct = _safe(pkg, rel)
        except SceneError:
            direct = None
        if direct is not None and direct.is_file():
            doomed.append(direct)
    if token and hist.is_dir():
        doomed.extend(
            path
            for path in hist.glob(f"scene-{step_id}-*.{token}")
            if path.is_file() and path not in doomed
        )
    for path in doomed:
        path.unlink(missing_ok=True)


def _undo_timeline(
    pkg: RallyPackage, step_id: str, folder: Path, manifest: dict
) -> None:
    for row in reversed(manifest.get("moves") or []):
        if not isinstance(row, dict):
            continue
        try:
            dest = _safe(pkg, str(row.get("dest") or ""))
            src = _safe(pkg, str(row.get("src") or ""))
        except SceneError:
            continue
        if dest.is_file() and not src.exists():
            dest.rename(src)
    for row in manifest.get("files") or []:
        if not isinstance(row, dict):
            continue
        src = folder / str(row.get("name") or "")
        rel = str(row.get("rel") or "")
        if not src.is_file() or not rel:
            continue
        dest = _safe(pkg, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    st = pkg.step(step_id)
    timeline = manifest.get("timeline") or []
    if timeline:
        st.options["timeline"] = timeline
        sid = str(manifest.get("scene_id") or "")
        if sid:
            st.options["scene_id"] = sid
    else:
        st.options.pop("timeline", None)
        st.options.pop("scene_id", None)
    shown = str(manifest.get("shown_take") or "")
    if shown:
        st.options["shown_take"] = shown
    else:
        st.options.pop("shown_take", None)
    keep = {
        str(row.get("rel") or "")
        for row in (manifest.get("files") or [])
        if isinstance(row, dict)
    }
    scene_dir = pkg.dir() / "scenes" / step_id
    if scene_dir.is_dir():
        for path in scene_dir.glob("*.mp4"):
            rel = f"scenes/{step_id}/{path.name}"
            if rel not in keep:
                path.unlink(missing_ok=True)
    if timeline:
        restitch(pkg, step_id, save=False)


def undo_edit(pkg: RallyPackage, step_id: str) -> None:
    with _write_lock(pkg, step_id):
        fresh = RallyPackage.load(pkg.dir())
        return _undo_edit(fresh, step_id)


def _undo_edit(pkg: RallyPackage, step_id: str) -> None:
    """Reverse the last saved cut, join, delete, or parked take."""
    kids = _undo_dirs(pkg, step_id)
    if not kids:
        raise SceneError("Nothing to undo")
    latest = kids[-1]
    manifest = json.loads((latest / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("kind") == "attempt":
        _undo_attempt(pkg, step_id, manifest)
    else:
        _undo_timeline(pkg, step_id, latest, manifest)
    shutil.rmtree(latest, ignore_errors=True)
    pkg.save()


def adopt_scene_attempt(
    pkg: RallyPackage, step_id: str, scene_id: str, src: Path
) -> None:
    """Replace one scene with a history attempt and stitch. Others stay."""
    src = Path(src)
    if not src.is_file():
        raise SceneError("That take is gone")
    stored = _stored(pkg, step_id)
    item = next((row for row in stored if str(row.get("id")) == scene_id), None)
    if item is None:
        raise SceneError("That scene is no longer on the timeline")
    current = _safe(pkg, str(item["file"]))
    if current.is_file() and current.resolve() != src.resolve():
        hist = pkg.dir() / "history"
        shutil.copy2(current, _attempt_dest(hist, step_id, scene_id))
    current.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, current)
    meta = probe(current)
    item["frames"] = meta["frames"]
    item["fps"] = meta["fps"]
    st = pkg.step(step_id)
    st.options["timeline"] = stored
    st.options["scene_id"] = scene_id
    _touch_review(pkg, step_id)
    restitch(pkg, step_id, save=False)
    pkg.save()


def delete_scene_attempt(pkg: RallyPackage, step_id: str, path: Path) -> str:
    """Remove one history take for a clip. The clip in the cut stays."""
    path = Path(path)
    sid = scene_attempt_id(step_id, path)
    if sid is None or clip_attempt_scene(pkg, step_id, path) != sid:
        raise SceneError("That is not a take for this clip")
    item = next(
        (row for row in _stored(pkg, step_id) if str(row.get("id")) == sid),
        None,
    )
    if item is None:
        raise SceneError("That clip is no longer on the timeline")
    current = _safe(pkg, str(item["file"]))
    try:
        if path.is_file() and current.is_file() and path.resolve() == current.resolve():
            raise SceneError("That clip is in the cut")
    except OSError:
        pass
    if not path.is_file():
        return "Already gone"
    fp = _fp(path)
    hist = pkg.dir() / "history"
    path.unlink(missing_ok=True)
    if fp and hist.is_dir():
        for cand in list(hist.glob(f"scene-{step_id}-{sid}.*")):
            if not cand.is_file() or scene_attempt_id(step_id, cand) != sid:
                continue
            if _fp(cand) == fp:
                cand.unlink(missing_ok=True)
    st = pkg.step(step_id)
    rel = str(st.options.get("shown_take") or "").strip()
    shown = shown_take_path(pkg, step_id)
    drop = bool(rel) and shown is None
    if shown is not None and fp:
        try:
            if not shown.is_file():
                drop = True
            elif scene_attempt_id(step_id, shown) == sid and _fp(shown) == fp:
                shown.unlink(missing_ok=True)
                drop = True
        except OSError:
            drop = True
    if drop:
        st.options.pop("shown_take", None)
    pkg.save()
    return "Deleted take for this clip"


def capture_clip_pins(pkg: RallyPackage, step_id: str) -> dict:
    """First and last frame of the clip on screen.

    Pins land beside the scene file so they are not history takes.
    A highlighted take is a real clip — it does not have to be the saved cut.
    """
    item = _selected_item(pkg, step_id)
    if item is None or item.get("missing"):
        raise SceneError("No video to edit")
    src = _safe(pkg, str(item["file"]))
    if not src.is_file():
        raise SceneError("That clip is missing")
    meta = probe(src)
    duration = max(1, min(15, int(round(float(meta["duration"])))))
    virtual = bool(item.get("virtual"))
    sid = _clip_slot(pkg, step_id) if virtual else str(item["id"])
    folder = pkg.dir() / "scenes" / step_id
    first = folder / f"{sid}.pin-first.png"
    last = folder / f"{sid}.pin-last.png"
    from app.pipeline.frames import FrameError, extract_first_frame, extract_last_frame

    try:
        extract_first_frame(src, first)
        extract_last_frame(src, last)
    except FrameError as exc:
        raise SceneError(str(exc)) from exc
    index = 0 if virtual else scene_index(pkg, step_id)
    return {
        "scene_id": sid,
        "index": index,
        "label": f"Clip {index + 1}",
        "duration": duration,
        "seconds": float(meta["duration"]),
        "first": first,
        "last": last,
    }


_SLOT = re.compile(r"^[0-9a-f]{8}$")


def _clip_slot(pkg: RallyPackage, step_id: str) -> str:
    """Stable id for takes of an uncut clip. Not a timeline scene until one is added."""
    st = pkg.step(step_id)
    raw = str(st.options.get("clip_slot") or "").strip()
    if _SLOT.match(raw):
        return raw
    raw = _new_id()
    st.options["clip_slot"] = raw
    pkg.save()
    return raw


def _viewing_copy(pkg: RallyPackage, step_id: str) -> bool:
    return has_timeline(pkg, step_id) and not cut_is_showing(pkg, step_id)


def editor_target(pkg: RallyPackage, step_id: str) -> dict:
    """Clip a redo, nudge, or new scene reads. The clip on screen, cut or not."""
    item = _selected_item(pkg, step_id)
    if item is None or item.get("missing"):
        raise SceneError("No video to edit")
    path = _safe(pkg, str(item["file"]))
    if not path.is_file():
        raise SceneError("That clip is missing")
    meta = probe(path)
    virtual = bool(item.get("virtual"))
    sid = _clip_slot(pkg, step_id) if virtual else str(item["id"])
    index = 0 if virtual else scene_index(pkg, step_id)
    duration = max(1, min(15, int(round(float(meta["duration"])))))
    return {
        "scene_id": sid,
        "virtual": virtual,
        "index": index,
        "label": f"Clip {index + 1}",
        "path": path,
        "file": str(item["file"]),
        "duration": duration,
        "seconds": float(meta["duration"]),
        "frames": int(meta["frames"]),
        "fps": float(meta["fps"]),
        "audio": bool(meta["audio"]),
    }


def capture_editor_pins(pkg: RallyPackage, step_id: str, *, which: str = "both") -> dict:
    """First and/or last frame of the clip the editor is generating from."""
    target = editor_target(pkg, step_id)
    which = which if which in {"first", "last", "both"} else "both"
    folder = pkg.dir() / "scenes" / step_id
    folder.mkdir(parents=True, exist_ok=True)
    sid = target["scene_id"]
    first = folder / f"{sid}.pin-first.png"
    last = folder / f"{sid}.pin-last.png"
    from app.pipeline.frames import FrameError, extract_first_frame, extract_last_frame

    try:
        if which in {"first", "both"}:
            extract_first_frame(target["path"], first)
        if which in {"last", "both"}:
            extract_last_frame(target["path"], last)
    except FrameError as exc:
        raise SceneError(str(exc)) from exc
    target["first"] = first if which in {"first", "both"} else None
    target["last"] = last if which in {"last", "both"} else None
    return target


def _attempt_rows(pkg: RallyPackage, step_id: str, scene_id: str, cut: Path) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    cut_fp = _fp(cut) if cut.is_file() else ""
    if cut_fp:
        seen.add(cut_fp)
    for item in _stored(pkg, step_id):
        try:
            path = _safe(pkg, str(item.get("file") or ""))
        except SceneError:
            continue
        if not path.is_file():
            continue
        fp = _fp(path)
        if fp:
            seen.add(fp)
    if not scene_id:
        return rows
    for path in scene_attempts(pkg, step_id, scene_id):
        fp = _fp(path)
        if fp and fp in seen:
            continue
        if fp:
            seen.add(fp)
        rows.append(
            {
                "file": f"history/{path.name}",
                "path": path,
                "kind": attempt_kind(path),
            }
        )
    return rows


def editor_snapshot(pkg: RallyPackage, step_id: str) -> dict:
    """What the full-screen editor is showing. Does not reserve a clip slot."""
    source = editor_source(pkg, step_id)
    items = bar_items(pkg, step_id)
    virtual = bool(
        source["scene_id"] == _VIRTUAL
        or any(item["id"] == source["scene_id"] and item.get("virtual") for item in items)
    )
    label = "Clip 1"
    seconds = source["frames"] / source["fps"] if source["fps"] else 0
    scenes: list[dict] = []
    for i, item in enumerate(items):
        on = item["id"] == source["scene_id"]
        lab = f"Clip {i + 1}"
        if on:
            label = lab
        frames = int(item.get("frames") or 0)
        fps = float(item.get("fps") or 0) or 30.0
        audio = False
        if not item.get("missing"):
            try:
                meta = probe(_safe(pkg, str(item["file"])))
            except SceneError:
                meta = None
            if meta:
                frames = int(meta["frames"])
                fps = float(meta["fps"]) or fps
                audio = bool(meta["audio"])
        scenes.append(
            {
                "id": item["id"],
                "label": lab,
                "seconds": frames / fps if frames and fps else 0,
                "on": on,
                "fresh": bool(item.get("fresh")),
                "frames": frames,
                "fps": fps,
                "audio": audio,
                "file": str(item.get("file") or ""),
                "virtual": bool(item.get("virtual")),
                "pending": bool(item.get("pending")),
                "detail": _pending_detail(pkg, step_id, str(item["id"]))
                if item.get("pending")
                else "",
            }
        )
    slot = str(pkg.step(step_id).options.get("clip_slot") or "").strip()
    if not _SLOT.match(slot):
        slot = ""
    attempt_id = slot if virtual else str(source["scene_id"])
    path = source["path"]
    attempts = _attempt_rows(pkg, step_id, attempt_id, path)
    reason = ""
    can = True
    st = pkg.step(step_id)
    return {
        "scene_id": source["scene_id"],
        "attempt_id": attempt_id,
        "file": source["file"],
        "path": path,
        "fps": source["fps"],
        "frames": source["frames"],
        "audio": bool(source["audio"]),
        "virtual": virtual,
        "label": label,
        "seconds": seconds,
        "only_scene": (not has_timeline(pkg, step_id)) or len(items) <= 1,
        "can_generate": can,
        "generate_reason": reason,
        "scenes": scenes,
        "attempts": attempts,
        "last_fresh": str(st.options.get("last_fresh") or ""),
        "last_nudge": str(st.options.get("last_nudge") or ""),
        "undo": undo_count(pkg, step_id),
        "editing": "",
    }


def focus_editor_scene(pkg: RallyPackage, step_id: str, scene_id: str) -> dict:
    scene_id = str(scene_id or "").strip()
    items = bar_items(pkg, step_id)
    if not any(item["id"] == scene_id for item in items):
        raise SceneError("That scene is not on the timeline")
    if has_timeline(pkg, step_id) and scene_id != _VIRTUAL:
        select_scene(pkg, step_id, scene_id)
    return editor_snapshot(pkg, step_id)


def join_with_next(pkg: RallyPackage, step_id: str) -> None:
    with _write_lock(pkg, step_id):
        fresh = RallyPackage.load(pkg.dir())
        return _join_with_next(fresh, step_id)


def _join_with_next(pkg: RallyPackage, step_id: str) -> None:
    """Splice the selected clip and the next one back into one clip."""
    if _viewing_copy(pkg, step_id):
        raise SceneError(
            "This is a copy of the take. Go back to the edited take to join."
        )
    stored = _stored(pkg, step_id)
    if len(stored) < 2:
        raise SceneError("Nothing to join")
    sid = selected_id(pkg, step_id)
    idx = next(
        (i for i, item in enumerate(stored) if str(item.get("id")) == sid),
        -1,
    )
    if idx < 0:
        raise SceneError("That scene is no longer on the timeline")
    if idx + 1 >= len(stored):
        raise SceneError("No clip after this one")
    left = stored[idx]
    right = stored[idx + 1]
    if left.get("pending") or right.get("pending"):
        raise SceneError("That clip is still generating")
    push_timeline_undo(pkg, step_id)
    created: Path | None = None
    moved: list[tuple[Path, Path]] = []
    committed = False
    try:
        new_id = _new_id()
        rel = _scene_rel(step_id, new_id)
        dest = _safe(pkg, rel)
        _stitch_files(
            [_safe(pkg, str(left["file"])), _safe(pkg, str(right["file"]))],
            dest,
        )
        created = dest
        meta = probe(dest)
        new_item = {
            "id": new_id,
            "file": rel,
            "frames": meta["frames"],
            "fps": meta["fps"],
        }
        timeline = stored[:idx] + [new_item] + stored[idx + 2 :]
        st = pkg.step(step_id)
        st.options["timeline"] = timeline
        st.options["scene_id"] = new_id
        moved.extend(reassign_attempts(pkg, step_id, str(left["id"]), new_id))
        moved.extend(reassign_attempts(pkg, step_id, str(right["id"]), new_id))
        _note_moves(pkg, step_id, moved)
        _touch_review(pkg, step_id)
        restitch(pkg, step_id, save=False)
        for old in (left, right):
            old_rel = str(old.get("file") or "")
            if any(str(item.get("file")) == old_rel for item in timeline):
                continue
            _drop_replaced(_safe(pkg, old_rel))
        pkg.save()
        committed = True
    except Exception:
        if not committed:
            discard_latest_undo(pkg, step_id)
            _restore_moves(moved)
            if created is not None:
                created.unlink(missing_ok=True)
        raise


def _attempt_for_scene(pkg: RallyPackage, step_id: str, rel: str) -> tuple[Path, str]:
    path = _pack_video(pkg, rel)
    sid = scene_attempt_id(step_id, path)
    if not sid:
        raise SceneError("That is not a take for this clip")
    target = editor_target(pkg, step_id)
    if sid != str(target["scene_id"]):
        raise SceneError("That take is not for this clip")
    return path, sid


def apply_attempt_edit(
    pkg: RallyPackage,
    step_id: str,
    attempt_rel: str,
    clips: list[dict],
    audio: list | None = None,
) -> str:
    """Bake a cut of a parked take into a new take. The clip in the cut stays."""
    src, sid = _attempt_for_scene(pkg, step_id, attempt_rel)
    meta = probe(src)
    source = {
        "path": src,
        "frames": int(meta["frames"]),
        "fps": float(meta["fps"]),
        "audio": bool(meta["audio"]),
    }
    parsed, blocks, frames, fps = _edit_request(source, clips, audio, pkg)
    if not parsed:
        raise SceneError("Keep at least one clip")
    if _unchanged_edit(parsed, blocks, frames):
        return ""
    with tempfile.TemporaryDirectory(prefix="rb-take-") as tmp:
        parts = _render_parts(
            src, parsed, blocks, fps, bool(meta["audio"]), Path(tmp)
        )
        out = Path(tmp) / "out.mp4"
        _stitch_files(parts, out)
        rel = save_scene_attempt(pkg, step_id, sid, out, kind="redo")
    push_attempt_undo(pkg, step_id, rel)
    pkg.save()
    return rel


def splice_clip_audio(pkg: RallyPackage, step_id: str, attempt_rel: str) -> str:
    """Mix the open clip's audio onto a parked take, from the start.

    The take keeps its own audio. The clip in the cut is not changed.
    """
    src, sid = _attempt_for_scene(pkg, step_id, attempt_rel)
    target = editor_target(pkg, step_id)
    cut = target["path"]
    cut_meta = probe(cut)
    if not cut_meta["audio"]:
        raise SceneError("This clip has no audio")
    src_meta = probe(src)
    fps = float(src_meta["fps"]) or 24.0
    frames = int(src_meta["frames"])
    if frames < 2:
        raise SceneError("That take is too short")
    with tempfile.TemporaryDirectory(prefix="rb-splice-") as tmp:
        folder = Path(tmp)
        audio = folder / "clip.m4a"
        _encode_audio(cut, audio, title="This clip")
        dur, has = _audio_info(audio)
        if not has or dur <= 0:
            raise SceneError("This clip has no audio")
        span = max(1, min(frames, int(round(dur * fps))))
        out = folder / "out.mp4"
        _mix_clip(
            src,
            out,
            0,
            frames,
            fps,
            bool(src_meta["audio"]),
            [(audio, 0, 0, span)],
        )
        rel = save_scene_attempt(pkg, step_id, sid, out, kind="redo")
    push_attempt_undo(pkg, step_id, rel)
    pkg.save()
    return rel


def attempt_view(pkg: RallyPackage, step_id: str, rel: str) -> dict:
    """Editor snapshot whose cutter is a parked take, not the clip in the cut."""
    snap = editor_snapshot(pkg, step_id)
    path, sid = _attempt_for_scene(pkg, step_id, rel)
    if sid != str(snap.get("attempt_id") or "") and sid != str(snap["scene_id"]):
        raise SceneError("That take is not for this clip")
    meta = probe(path)
    file_rel = str(rel).replace("\\", "/").lstrip("/")
    snap["path"] = path
    snap["file"] = file_rel
    snap["frames"] = int(meta["frames"])
    snap["fps"] = float(meta["fps"])
    snap["audio"] = bool(meta["audio"])
    snap["seconds"] = (
        int(meta["frames"]) / float(meta["fps"]) if meta["fps"] else 0
    )
    snap["editing"] = file_rel
    snap["can_generate"] = False
    snap["generate_reason"] = "Back to the clip in the cut before generating."
    snap["bin"] = editor_bin(pkg, step_id, file_rel, float(meta["fps"] or 30))
    return snap


def adopt_editor_attempt(pkg: RallyPackage, step_id: str, src: Path) -> None:
    """Put a parked take on the open clip. Does not highlight it on the phone."""
    src = Path(src)
    if not src.is_file():
        raise SceneError("That take is gone")
    sid = scene_attempt_id(step_id, src)
    if not sid:
        raise SceneError("That is not a take for this clip")
    if _viewing_copy(pkg, step_id):
        raise SceneError(
            "This is a copy of the take. Go back to the edited take to generate."
        )
    stored = _stored(pkg, step_id)
    if stored:
        if not any(str(item.get("id")) == sid for item in stored):
            raise SceneError("That take is not for this clip")
        adopt_scene_attempt(pkg, step_id, sid, src)
        pkg.step(step_id).options.pop("shown_take", None)
        pkg.save()
        return
    slot = str(pkg.step(step_id).options.get("clip_slot") or "")
    if sid != slot:
        raise SceneError("That take is not for this clip")
    canon = canonical_video(pkg, step_id)
    if not canon.is_file():
        raise SceneError("No take to replace")
    _archive_canonical(pkg, step_id)
    shutil.copy2(src, canon)
    _touch_review(pkg, step_id)
    pkg.step(step_id).options.pop("shown_take", None)
    pkg.save()


def delete_editor_attempt(pkg: RallyPackage, step_id: str, path: Path) -> str:
    """Remove one parked take. The clip in the cut stays."""
    path = Path(path)
    sid = scene_attempt_id(step_id, path)
    if not sid:
        raise SceneError("That is not a take for this clip")
    stored_ids = {str(item.get("id") or "") for item in _stored(pkg, step_id)}
    slot = str(pkg.step(step_id).options.get("clip_slot") or "")
    if sid in stored_ids:
        return delete_scene_attempt(pkg, step_id, path)
    if sid != slot:
        raise SceneError("That is not a take for this clip")
    if not path.is_file():
        return "Already gone"
    fp = _fp(path)
    hist = pkg.dir() / "history"
    path.unlink(missing_ok=True)
    if fp and hist.is_dir():
        for cand in list(hist.glob(f"scene-{step_id}-{sid}.*")):
            if not cand.is_file() or scene_attempt_id(step_id, cand) != sid:
                continue
            if _fp(cand) == fp:
                cand.unlink(missing_ok=True)
    st = pkg.step(step_id)
    shown = shown_take_path(pkg, step_id)
    if shown is not None:
        try:
            if not shown.is_file() or (
                scene_attempt_id(step_id, shown) == sid and _fp(shown) == fp
            ):
                st.options.pop("shown_take", None)
        except OSError:
            st.options.pop("shown_take", None)
    pkg.save()
    return "Deleted take for this clip"


def _insert_after_pending(stored: list[dict], after_id: str, new_item: dict) -> list[dict]:
    """Insert after this clip, and after placeholders already queued there."""
    ids = [str(item.get("id")) for item in stored]
    if after_id not in ids:
        return [*stored, new_item]
    idx = ids.index(after_id) + 1
    while idx < len(stored) and stored[idx].get("pending"):
        idx += 1
    return [*stored[:idx], new_item, *stored[idx:]]


def _frame_size(path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            _ffprobe(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    raw = (result.stdout or "").strip().splitlines()
    line = raw[0] if raw else ""
    if "x" in line:
        width, height = line.split("x", 1)
        if width.isdigit() and height.isdigit():
            return int(width), int(height)
    return 720, 1280


def _blank_like(src: Path, dest: Path, frames: int, fps: float) -> None:
    """A silent black clip the same size as src, so the timeline can hold a slot."""
    width, height = _frame_size(src)
    width = max(2, width - (width % 2))
    height = max(2, height - (height % 2))
    rate = f"{fps:.6f}" if fps and fps > 0 else "24"
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            _ffmpeg(),
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={width}x{height}:r={rate}",
            "-frames:v",
            str(max(2, int(frames))),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-movflags",
            "+faststart",
            str(dest),
        ],
        "Placeholder",
    )


def _pending_detail(pkg: RallyPackage, step_id: str, scene_id: str) -> str:
    try:
        from app.pipeline.editor_jobs import detail_for_scene
    except Exception:  # noqa: BLE001
        return "Queued…"
    text = detail_for_scene(pkg.name, step_id, scene_id)
    return text or "Queued…"


def append_generated_scene(
    pkg: RallyPackage,
    step_id: str,
    src: Path,
    *,
    fresh: bool = True,
    pending: bool = False,
    after_id: str | None = None,
) -> str:
    """Insert a clip after the one on screen. An uncut take becomes scene 1.

    A highlighted take is that clip. The previous cut stays in undo.
    A pending clip is a placeholder: selection stays on the clip it follows.
    """
    with _write_lock(pkg, step_id):
        fresh_pkg = RallyPackage.load(pkg.dir())
        return _append_generated_scene(
            fresh_pkg,
            step_id,
            src,
            fresh=fresh,
            pending=pending,
            after_id=after_id,
        )


def _append_generated_scene(
    pkg: RallyPackage,
    step_id: str,
    src: Path,
    *,
    fresh: bool = True,
    pending: bool = False,
    after_id: str | None = None,
) -> str:
    src = Path(src)
    if not src.is_file() or src.stat().st_size < 1000:
        raise SceneError("New scene produced an empty clip")
    meta = probe(src)
    new_id = _new_id()
    rel = _scene_rel(step_id, new_id)
    dest = _safe(pkg, rel)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    new_item = {
        "id": new_id,
        "file": rel,
        "frames": meta["frames"],
        "fps": meta["fps"],
    }
    if fresh:
        new_item["fresh"] = True
    if pending:
        new_item["pending"] = True
        new_item["detail"] = "Queued…"
    created: list[Path] = [dest]
    stitched = False
    try:
        push_timeline_undo(pkg, step_id)
        stored = _stored(pkg, step_id)
        st = pkg.step(step_id)
        if _viewing_copy(pkg, step_id):
            shown = shown_take_path(pkg, step_id)
            if shown is None or not shown.is_file():
                raise SceneError("No take to extend")
            sid1 = _new_id()
            while sid1 == new_id:
                sid1 = _new_id()
            rel1 = _scene_rel(step_id, sid1)
            dest1 = _safe(pkg, rel1)
            dest1.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(shown, dest1)
            created.append(dest1)
            meta1 = probe(dest1)
            timeline = [
                {
                    "id": sid1,
                    "file": rel1,
                    "frames": meta1["frames"],
                    "fps": meta1["fps"],
                },
                new_item,
            ]
        elif not stored:
            canon = canonical_video(pkg, step_id)
            if not canon.is_file():
                raise SceneError("No take to extend")
            sid1 = str(st.options.get("clip_slot") or "").strip()
            if not _SLOT.match(sid1):
                sid1 = _new_id()
                st.options["clip_slot"] = sid1
            while new_id == sid1:
                new_id = _new_id()
                new_item["id"] = new_id
                rel = _scene_rel(step_id, new_id)
                moved = _safe(pkg, rel)
                dest.replace(moved)
                created = [moved]
                dest = moved
                new_item["file"] = rel
            rel1 = _scene_rel(step_id, sid1)
            dest1 = _safe(pkg, rel1)
            dest1.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(canon, dest1)
            created.append(dest1)
            meta1 = probe(dest1)
            timeline = [
                {
                    "id": sid1,
                    "file": rel1,
                    "frames": meta1["frames"],
                    "fps": meta1["fps"],
                },
                new_item,
            ]
        else:
            after = str(after_id or selected_id(pkg, step_id) or "")
            timeline = _insert_after_pending(stored, after, new_item)
        st.options["timeline"] = timeline
        if pending:
            ids = [str(item.get("id")) for item in timeline]
            pos = ids.index(str(new_item["id"])) if str(new_item["id"]) in ids else 0
            stay = ""
            if after_id and after_id in ids:
                stay = after_id
            elif pos > 0:
                stay = ids[pos - 1]
            elif ids:
                stay = ids[0]
            if stay:
                st.options["scene_id"] = stay
        else:
            st.options["scene_id"] = new_item["id"]
        st.options.pop("shown_take", None)
        _touch_review(pkg, step_id)
        restitch(pkg, step_id, save=False)
        stitched = True
        pkg.save()
        return str(new_item["id"])
    except Exception:
        if not stitched:
            discard_latest_undo(pkg, step_id)
            for path in created:
                path.unlink(missing_ok=True)
        raise


def prepare_pending_job(
    pkg: RallyPackage,
    step_id: str,
    scene_id: str,
    kind: str,
    *,
    seconds: int | None,
    pins: str,
    start_frame: bool,
) -> dict:
    """Freeze the source clip and put a placeholder after it.

    The placeholder is the length asked for. Selection stays on the source
    so the next one queues behind this one.
    """
    with _write_lock(pkg, step_id):
        fresh = RallyPackage.load(pkg.dir())
        source = _source_for(fresh, step_id, scene_id)
        stored_item = next(
            (
                item
                for item in _stored(fresh, step_id)
                if str(item.get("id")) == str(source["scene_id"])
            ),
            None,
        )
        if stored_item is not None and stored_item.get("pending"):
            raise SceneError("That clip is still generating")
        meta = probe(source["path"])
        fps = float(meta["fps"] or 24)
        if kind == "nudge" or seconds is None:
            frames = int(meta["frames"])
        else:
            frames = max(2, int(round(float(seconds) * fps)))
        items = bar_items(fresh, step_id)
        index = next(
            (i for i, item in enumerate(items) if item["id"] == source["scene_id"]),
            0,
        )
        which = ""
        if kind == "redo-frames":
            which = pins if pins in {"first", "last", "both"} else "both"
        elif kind == "new-scene" and start_frame:
            which = "last"
        sid = str(source["scene_id"])
        if not _SLOT.match(sid):
            sid = _clip_slot(fresh, step_id)
        target: dict = {
            "scene_id": sid,
            "path": source["path"],
            "file": source["file"],
            "fps": fps,
            "frames": int(meta["frames"]),
            "audio": bool(meta["audio"]),
            "duration": max(1, min(15, int(round(float(meta["duration"]))))),
            "seconds": float(meta["duration"]),
            "label": f"Clip {index + 1}",
            "first": None,
            "last": None,
        }
        if which:
            folder = fresh.dir() / "scenes" / step_id
            folder.mkdir(parents=True, exist_ok=True)
            from app.pipeline.frames import FrameError, extract_first_frame, extract_last_frame

            token = _new_id()
            try:
                if which in {"first", "both"}:
                    first = folder / f"{sid}.pin-first-{token}.png"
                    extract_first_frame(source["path"], first)
                    target["first"] = first
                if which in {"last", "both"}:
                    last = folder / f"{sid}.pin-last-{token}.png"
                    extract_last_frame(source["path"], last)
                    target["last"] = last
            except FrameError as exc:
                raise SceneError(str(exc)) from exc
        blank = fresh.dir() / "history" / f".pending-{_new_id()}.mp4"
        _blank_like(source["path"], blank, frames, fps)
        try:
            dest_id = _append_generated_scene(
                fresh,
                step_id,
                blank,
                fresh=False,
                pending=True,
                after_id=str(source["scene_id"]),
            )
        finally:
            blank.unlink(missing_ok=True)
        return {"dest_id": dest_id, "target": target}


def fulfill_pending_scene(
    pkg: RallyPackage,
    step_id: str,
    scene_id: str,
    src: Path,
    options: dict | None = None,
) -> bool:
    """Replace a placeholder with the rendered clip. False if it was removed."""
    with _write_lock(pkg, step_id):
        fresh = RallyPackage.load(pkg.dir())
        return _fulfill_pending_scene(fresh, step_id, scene_id, src, options)


def _fulfill_pending_scene(
    pkg: RallyPackage,
    step_id: str,
    scene_id: str,
    src: Path,
    options: dict | None = None,
) -> bool:
    src = Path(src)
    stored = _stored(pkg, step_id)
    item = next((row for row in stored if str(row.get("id")) == str(scene_id)), None)
    if item is None:
        src.unlink(missing_ok=True)
        return False
    dest = _safe(pkg, str(item["file"]))
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.resolve() != dest.resolve():
        shutil.move(str(src), str(dest))
    meta = probe(dest)
    item["frames"] = meta["frames"]
    item["fps"] = meta["fps"]
    item.pop("pending", None)
    item.pop("detail", None)
    item["fresh"] = True
    st = pkg.step(step_id)
    st.options["timeline"] = stored
    st.error = None
    if options:
        for key, value in options.items():
            st.options[key] = value
    _touch_review(pkg, step_id)
    restitch(pkg, step_id, save=False)
    pkg.save()
    return True


def drop_pending_scene(pkg: RallyPackage, step_id: str, scene_id: str) -> None:
    """Remove a placeholder that failed. Leave it if it is the only clip."""
    with _write_lock(pkg, step_id):
        fresh = RallyPackage.load(pkg.dir())
        _drop_pending_scene(fresh, step_id, scene_id)


def _drop_pending_scene(pkg: RallyPackage, step_id: str, scene_id: str) -> None:
    stored = _stored(pkg, step_id)
    item = next((row for row in stored if str(row.get("id")) == str(scene_id)), None)
    if item is None or not item.get("pending"):
        return
    st = pkg.step(step_id)
    if len(stored) <= 1:
        item.pop("pending", None)
        item.pop("detail", None)
        st.options["timeline"] = stored
        pkg.save()
        return
    timeline = [row for row in stored if str(row.get("id")) != str(scene_id)]
    st.options["timeline"] = timeline
    if str(st.options.get("scene_id") or "") == str(scene_id):
        st.options["scene_id"] = str(timeline[0]["id"])
    path = _safe(pkg, str(item.get("file") or ""))
    _touch_review(pkg, step_id)
    restitch(pkg, step_id, save=False)
    pkg.save()
    if path.is_file():
        _drop_replaced(path)


_CLIP_EXTS = {".mp4", ".mov", ".m4v", ".webm"}
_MAX_UPLOAD_SECONDS = 120


def _normalize_upload(src: Path, dest: Path) -> None:
    """Make an uploaded clip playable in the editor. Keeps silence if it has none."""
    meta = probe(src)
    if meta["frames"] < 2:
        raise SceneError("That clip is too short")
    if meta["duration"] > _MAX_UPLOAD_SECONDS:
        raise SceneError("That clip is longer than 2 minutes")
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _ffmpeg(),
        "-y",
        "-i",
        str(src),
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
    ]
    if meta["audio"]:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-an"]
    cmd.append(str(dest))
    _run(cmd, "Import")


def import_timeline_clip(
    pkg: RallyPackage, step_id: str, data: bytes, original_name: str
) -> str:
    """Put an uploaded video on the timeline, after the selected clip."""
    ext = Path(original_name or "").suffix.lower()
    if ext not in _CLIP_EXTS:
        raise SceneError("Use an mp4, mov, or webm file")
    if len(data) < 1000:
        raise SceneError("That clip is empty")
    hist = pkg.dir() / "history"
    hist.mkdir(exist_ok=True)
    raw = hist / f".upload-src-{_new_id()}{ext}"
    norm = hist / f".upload-{_new_id()}.mp4"
    raw.write_bytes(data)
    try:
        _normalize_upload(raw, norm)
        return append_generated_scene(pkg, step_id, norm, fresh=False)
    finally:
        raw.unlink(missing_ok=True)
        norm.unlink(missing_ok=True)


def archive_for_generate(pkg: RallyPackage, step_id: str) -> list[str]:
    """Back up the file Make is about to replace. Does not move the stitch.

    Highlighting another take does not back up a scene of the hidden cut.
    """
    if has_timeline(pkg, step_id):
        if not cut_is_showing(pkg, step_id):
            return []
        rel = _backup_scene(pkg, step_id)
        return [rel] if rel else []
    return pkg.archive_outputs(step_id)


def previous_last_frame(pkg: RallyPackage, step_id: str) -> Path | None:
    """Last frame of the scene before the selected one."""
    items = _stored(pkg, step_id)
    idx = scene_index(pkg, step_id)
    if idx <= 0 or idx >= len(items):
        return None
    prev = _safe(pkg, str(items[idx - 1]["file"]))
    if not prev.is_file():
        return None
    from app.pipeline.frames import extract_last_frame

    dest = prev.with_name(prev.stem + ".last.png")
    return extract_last_frame(prev, dest)


def editor_source(pkg: RallyPackage, step_id: str) -> dict:
    """Probe info for the frame editor. Raises SceneError if there is no take."""
    item = _selected_item(pkg, step_id)
    if item is None:
        raise SceneError("No video to edit")
    path = _safe(pkg, item["file"])
    if item.get("missing") or not path.is_file():
        frames = int(item.get("frames") or 0)
        fps = float(item.get("fps") or 0) or 30.0
        return {
            "scene_id": item["id"],
            "file": item["file"],
            "path": path,
            "fps": fps,
            "frames": frames if frames >= 2 else 2,
            "audio": False,
        }
    meta = probe(path)
    if meta["frames"] < 2:
        raise SceneError("Video is too short to cut")
    return {
        "scene_id": item["id"],
        "file": item["file"],
        "path": path,
        "fps": meta["fps"],
        "frames": meta["frames"],
        "audio": meta["audio"],
    }


def _source_for(pkg: RallyPackage, step_id: str, scene_id: str) -> dict:
    """Probe one timeline clip. It does not have to be the selected one.

    A highlighted copy is still one take, so that path keeps the selected file.
    """
    scene_id = str(scene_id or "").strip()
    stored = _stored(pkg, step_id)
    if not stored or not cut_is_showing(pkg, step_id):
        source = editor_source(pkg, step_id)
        if scene_id and source["scene_id"] != scene_id:
            raise SceneError("That clip is not on the timeline")
        return source
    item = next((it for it in stored if str(it.get("id")) == scene_id), None)
    if item is None:
        raise SceneError("That clip is not on the timeline")
    path = _safe(pkg, str(item["file"]))
    if not path.is_file():
        raise SceneError("That clip is missing")
    meta = probe(path)
    if int(meta["frames"]) < 2:
        raise SceneError("Video is too short to cut")
    return {
        "scene_id": str(item["id"]),
        "file": str(item["file"]),
        "path": path,
        "fps": meta["fps"],
        "frames": meta["frames"],
        "audio": meta["audio"],
    }


def _remove_scene(pkg: RallyPackage, step_id: str, source: dict) -> None:
    """Drop a scene that was deleted in the editor. The step must keep one."""
    stored = _stored(pkg, step_id)
    if len(stored) <= 1:
        raise SceneError("Keep at least one clip")
    sid = source["scene_id"]
    timeline = [item for item in stored if str(item["id"]) != sid]
    if len(timeline) == len(stored):
        raise SceneError("That scene is no longer on the timeline")
    st = pkg.step(step_id)
    st.options["timeline"] = timeline
    st.options["scene_id"] = str(timeline[0]["id"])
    _touch_review(pkg, step_id)
    _archive_canonical(pkg, step_id)
    restitch(pkg, step_id, save=False)
    if sid != _VIRTUAL:
        _drop_replaced(source["path"])
    pkg.save()


def _edit_request(
    source: dict,
    clips: list[dict],
    audio: list | None,
    pkg: RallyPackage,
) -> tuple[list[tuple[int, int, bool]], list[dict], int, float]:
    frames = int(source["frames"])
    fps = float(source["fps"])
    parsed: list[tuple[int, int, bool]] = []
    for clip in clips:
        if not isinstance(clip, dict):
            raise SceneError("Bad clip")
        try:
            start = int(clip.get("in"))
            end = int(clip.get("out"))
        except (TypeError, ValueError) as exc:
            raise SceneError("Bad clip") from exc
        if start < 0 or end > frames or end <= start:
            raise SceneError("A clip is outside the video")
        parsed.append((start, end, bool(clip.get("mute"))))
    blocks = _parse_audio_blocks(pkg, audio, fps)
    return parsed, blocks, frames, fps


def _unchanged_edit(
    parsed: list[tuple[int, int, bool]],
    blocks: list[dict],
    frames: int,
) -> bool:
    """Full-range picture with no mute and no lane audio."""
    return (
        len(parsed) == 1
        and parsed[0][0] == 0
        and parsed[0][1] == frames
        and not parsed[0][2]
        and not _blocks_overlap(blocks, frames)
    )


def _render_parts(
    src: Path,
    parsed: list[tuple[int, int, bool]],
    blocks: list[dict],
    fps: float,
    has_audio: bool,
    folder: Path,
) -> list[Path]:
    parts: list[Path] = []
    seq = 0
    for index, (start, end, mute) in enumerate(parsed):
        length = end - start
        layers = _layers_for(blocks, seq, length)
        keep = (not mute) and has_audio
        dest = folder / f"{index}.mp4"
        if layers:
            _mix_clip(src, dest, start, end, fps, keep, layers)
        else:
            _extract(src, dest, start, end, fps, keep)
        if not dest.is_file() or dest.stat().st_size < 500:
            raise SceneError("Cut produced an empty clip")
        parts.append(dest)
        seq += length
    return parts


def _history_take_dest(pkg: RallyPackage, step_id: str) -> tuple[Path, str]:
    hist = pkg.dir() / "history"
    hist.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = Path(video_name(pkg, step_id)).stem
    dest = hist / f"{stem}.{stamp}.mp4"
    if dest.exists():
        dest = hist / f"{stem}.{stamp}.{_new_id()}.mp4"
    return dest, f"history/{dest.name}"


def _save_shown_take(
    pkg: RallyPackage,
    step_id: str,
    source: dict,
    parsed: list[tuple[int, int, bool]],
    blocks: list[dict],
    frames: int,
    fps: float,
) -> bool:
    """Cut the highlighted take into a new history file.

    The saved scene split on the other take stays where it is.
    """
    if not parsed:
        raise SceneError("Keep at least one clip")
    if len(parsed) > _MAX_CLIPS:
        raise SceneError(f"Too many clips (max {_MAX_CLIPS})")
    if _unchanged_edit(parsed, blocks, frames):
        return False
    dest, rel = _history_take_dest(pkg, step_id)
    try:
        with tempfile.TemporaryDirectory(prefix="rb-take-") as tmp:
            parts = _render_parts(
                source["path"],
                parsed,
                blocks,
                fps,
                bool(source["audio"]),
                Path(tmp),
            )
            _stitch_files(parts, dest)
    except Exception:
        dest.unlink(missing_ok=True)
        raise
    if not dest.is_file() or dest.stat().st_size < 500:
        dest.unlink(missing_ok=True)
        raise SceneError("Cut produced an empty clip")
    pkg.step(step_id).options["shown_take"] = rel
    pkg.save()
    return True


def apply_edit(
    pkg: RallyPackage,
    step_id: str,
    scene_id: str,
    clips: list[dict],
    audio: list | None = None,
) -> bool:
    with _write_lock(pkg, step_id):
        fresh = RallyPackage.load(pkg.dir())
        return _apply_edit(fresh, step_id, scene_id, clips, audio)


def _apply_edit(
    pkg: RallyPackage,
    step_id: str,
    scene_id: str,
    clips: list[dict],
    audio: list | None = None,
) -> bool:
    """Replace one scene with the ordered clips. Other scenes stay.

    Saving the first cut copies the uncut take into history first.
    Cutting or trimming the clip on screen keeps those pieces, even when
    that clip is a highlighted take. The previous cut stays in undo.
    An audio-only change to a highlighted take stays one new take.
    An empty audio lane and no mutes leaves a full-range picture unchanged.
    """
    source = _source_for(pkg, step_id, scene_id)
    parsed, blocks, frames, fps = _edit_request(source, clips, audio, pkg)
    pending_hit = any(
        str(item.get("id")) == str(source["scene_id"]) and item.get("pending")
        for item in _stored(pkg, step_id)
    )
    if pending_hit and parsed:
        raise SceneError("That clip is still generating")
    viewing_copy = bool(_stored(pkg, step_id)) and not cut_is_showing(pkg, step_id)
    picture = not (
        len(parsed) == 1 and parsed[0][0] == 0 and parsed[0][1] == frames
    )
    if viewing_copy and not picture:
        push_timeline_undo(pkg, step_id)
        try:
            changed = _save_shown_take(
                pkg, step_id, source, parsed, blocks, frames, fps
            )
        except Exception:
            discard_latest_undo(pkg, step_id)
            raise
        if not changed:
            discard_latest_undo(pkg, step_id)
        return changed
    if not parsed:
        push_timeline_undo(pkg, step_id)
        try:
            _remove_scene(pkg, step_id, source)
        except Exception:
            discard_latest_undo(pkg, step_id)
            raise
        return True
    if len(parsed) > _MAX_CLIPS:
        raise SceneError(f"Too many clips (max {_MAX_CLIPS})")
    # Same bytes as today when the lane is empty and no clip is muted.
    if (
        len(parsed) == 1
        and parsed[0][0] == 0
        and parsed[0][1] == frames
        and not parsed[0][2]
        and not _blocks_overlap(blocks, frames)
    ):
        return False

    src = source["path"]
    has_audio = bool(source["audio"])
    created: list[Path] = []
    new_items: list[dict] = []
    moved: list[tuple[Path, Path]] = []
    committed = False
    push_timeline_undo(pkg, step_id)
    try:
        seq = 0
        for start, end, mute in parsed:
            length = end - start
            layers = _layers_for(blocks, seq, length)
            keep = (not mute) and has_audio
            sid = _new_id()
            rel = _scene_rel(step_id, sid)
            dest = _safe(pkg, rel)
            if layers:
                _mix_clip(src, dest, start, end, fps, keep, layers)
            else:
                _extract(src, dest, start, end, fps, keep)
            if not dest.is_file() or dest.stat().st_size < 500:
                raise SceneError("Cut produced an empty clip")
            created.append(dest)
            meta = probe(dest)
            new_items.append(
                {
                    "id": sid,
                    "file": rel,
                    "frames": meta["frames"],
                    "fps": meta["fps"],
                }
            )
            seq += length
        _archive_canonical(pkg, step_id)
        stored = _stored(pkg, step_id)
        if viewing_copy or not stored:
            timeline = new_items
        else:
            timeline = []
            replaced = False
            for item in stored:
                if str(item["id"]) == source["scene_id"] and not replaced:
                    timeline.extend(new_items)
                    replaced = True
                else:
                    timeline.append(item)
            if not replaced:
                raise SceneError("That scene is no longer on the timeline")
        st = pkg.step(step_id)
        st.options["timeline"] = timeline
        st.options["scene_id"] = new_items[0]["id"]
        if viewing_copy:
            st.options.pop("shown_take", None)
        _touch_review(pkg, step_id)
        restitch(pkg, step_id, save=False)
        if source["scene_id"] != _VIRTUAL and new_items:
            moved.extend(
                reassign_attempts(
                    pkg, step_id, source["scene_id"], str(new_items[0]["id"])
                )
            )
            _note_moves(pkg, step_id, moved)
        pkg.save()
        committed = True
        if source["scene_id"] != _VIRTUAL:
            rel_old = str(source["file"])
            still_used = any(str(item.get("file")) == rel_old for item in timeline)
            if not still_used:
                _drop_replaced(src)
        return True
    except Exception:
        if not committed:
            discard_latest_undo(pkg, step_id)
            _restore_moves(moved)
            for path in created:
                path.unlink(missing_ok=True)
        raise
