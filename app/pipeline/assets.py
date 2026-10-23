"""Canonical rally asset paths and import into the static media tree."""

from __future__ import annotations

import shutil
from pathlib import Path

from app.models.rally import StepKind, StepStatus, RallyPackage, utc_now_iso

# Fixed deliverable names — also the URL basename under /media/<rally>/
CANONICAL: dict[str, list[str]] = {
    "Preview": ["Preview.png"],
    "Idle": ["IdleKeyframe.png", "Idle.mp4"],
    "Rally": ["take.mp4"],
    "Message": ["Message.mp4"],
    "Accept": ["Accept.mp4"],
    "Decline": ["Decline.mp4"],
    "Adjust": ["Reschedule.mp4"],
}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v"}


def media_url(pkg: RallyPackage, filename: str) -> str:
    """Browser path served by NiceGUI static mount (supports nested favorites/)."""
    from urllib.parse import quote

    parts = [quote(pkg.name), *[quote(p) for p in Path(filename).parts]]
    return "/media/" + "/".join(parts)


def resolve_target_filename(step_id: str, source: Path) -> str:
    """Map an uploaded file to the canonical name for this step."""
    ext = source.suffix.lower()
    if step_id == "Preview":
        return "Preview.png"
    if step_id == "Idle":
        if ext in VIDEO_EXTS:
            return "Idle.mp4"
        if ext in IMAGE_EXTS:
            return "IdleKeyframe.png"
        raise ValueError(f"Idle accepts image or video, got {ext}")
    # Branch videos
    names = CANONICAL.get(step_id) or [f"{step_id}.mp4"]
    video_name = next((n for n in names if n.lower().endswith(".mp4")), names[0])
    if ext not in VIDEO_EXTS and ext not in IMAGE_EXTS:
        raise ValueError(f"Unsupported extension {ext} for {step_id}")
    # Branch steps are videos; allow image only if explicitly Idle (handled above)
    if ext in IMAGE_EXTS and step_id != "Preview":
        raise ValueError(f"{step_id} expects a video file (.mp4), got {ext}")
    return video_name


def import_file(
    pkg: RallyPackage,
    step_id: str,
    source: Path,
    *,
    mark_review: bool = True,
) -> Path:
    """
    Copy/move a user file into the rally folder under a consistent name.

    The package root is mounted at /media/<rally>/ so the browser always loads:
      /media/<rally>/Preview.png
      /media/<rally>/Idle.mp4
      …
    """
    if not source.is_file():
        raise FileNotFoundError(source)

    dest_name = resolve_target_filename(step_id, source)
    dest = pkg.path_for(dest_name)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Archive existing if present
    if dest.is_file():
        hist = pkg.dir() / "history"
        hist.mkdir(exist_ok=True)
        stamp = utc_now_iso().replace(":", "").replace("+00:00", "Z")
        archived = hist / f"{dest.stem}.{stamp}{dest.suffix}"
        shutil.move(str(dest), str(archived))

    # Normalize extension to canonical (e.g. .mov → .mp4 name; keep bytes)
    shutil.copy2(source, dest)

    st = pkg.step(step_id)
    if dest_name not in st.outputs:
        st.outputs = list(dict.fromkeys([*st.outputs, dest_name]))

    if dest_name == "Idle.mp4":
        st.error = None
        st.options.pop("video_zdr_blocked", None)
        st.options.pop("video_cli_handoff", None)
    if dest_name == "IdleKeyframe.png":
        st.options["generate_png"] = True

    if mark_review and st.status != StepStatus.LOCKED:
        st.status = StepStatus.NEEDS_REVIEW
        st.error = None

    if dest_name == "Preview.png":
        from app.pipeline.explicit_mark import (
            apply_explicit_mark,
            snapshot_clean_preview,
        )

        snapshot_clean_preview(pkg)
        apply_explicit_mark(pkg)

    pkg.save()
    return dest


def write_upload_bytes(
    pkg: RallyPackage,
    step_id: str,
    data: bytes,
    original_name: str,
) -> Path:
    """Save upload bytes via a temp file, then import under the canonical name."""
    import tempfile

    suffix = Path(original_name).suffix or ".bin"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        return import_file(pkg, step_id, tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
