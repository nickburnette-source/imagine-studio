"""iOS H.264 compression (mirrors cmprssRally-iOS.sh) with progress + size stats."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.config import COMPRESS_SCRIPT_IOS

ProgressCb = Callable[[str], Awaitable[None] | None] | None

# Canonical package files (no meta/history/smoke artifacts)
DELIVERABLES: list[str] = [
    "Preview.png",
    "Idle.mp4",
    "take.mp4",
    "Message.mp4",
    "Accept.mp4",
    "Decline.mp4",
    "Reschedule.mp4",
]

# PNG stills count when the mp4 was skipped (Idle-as-still, clip start stills).
STILL_FALLBACKS: dict[str, tuple[str, ...]] = {
    "Idle.mp4": ("IdleKeyframe.png", "Message.last.png"),
    "take.mp4": ("start.png",),
    "Message.mp4": ("Message.start.png", "Message.last.png"),
    "Accept.mp4": ("Accept.start.png",),
    "Decline.mp4": ("Decline.start.png",),
    "Reschedule.mp4": ("Reschedule.start.png",),
}


@dataclass
class FileSizeRow:
    name: str
    before: int
    after: int

    @property
    def saved(self) -> int:
        return max(0, self.before - self.after)

    @property
    def pct(self) -> float:
        if self.before <= 0:
            return 0.0
        return 100.0 * (1.0 - self.after / self.before)


@dataclass
class CompressResult:
    output_dir: Path
    rows: list[FileSizeRow] = field(default_factory=list)
    before_total: int = 0
    after_total: int = 0
    log: list[str] = field(default_factory=list)

    @property
    def saved_total(self) -> int:
        return max(0, self.before_total - self.after_total)

    @property
    def pct_total(self) -> float:
        if self.before_total <= 0:
            return 0.0
        return 100.0 * (1.0 - self.after_total / self.before_total)


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    if n < 1024**3:
        return f"{n / (1024**2):.2f} MB"
    return f"{n / (1024**3):.2f} GB"


def fmt_bytes(n: int) -> str:
    return _fmt_bytes(n)


async def _emit(progress: ProgressCb, msg: str) -> None:
    if not progress:
        return
    r = progress(msg)
    if asyncio.iscoroutine(r) or isinstance(r, Awaitable):
        await r  # type: ignore[arg-type]


def _file_size(p: Path) -> int:
    try:
        return p.stat().st_size if p.is_file() else 0
    except OSError:
        return 0


def collect_deliverable_sources(rally_dir: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        if not p.is_file():
            return
        key = str(p.resolve())
        if key in seen:
            return
        seen.add(key)
        found.append(p)

    for name in DELIVERABLES:
        p = rally_dir / name
        if p.is_file():
            add(p)
            continue
        for alt in STILL_FALLBACKS.get(name, ()):
            alt_p = rally_dir / alt
            if alt_p.is_file():
                add(alt_p)
                break
    return found


def _ffmpeg_ios(src: Path, dest: Path) -> None:
    """Same encode settings as cmprssRally-iOS.sh."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        "24",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "")[-800:]
        raise RuntimeError(f"ffmpeg failed on {src.name}:\n{err}")


async def compress_rally_ios(
    rally_dir: Path,
    *,
    display_name: str | None = None,
    progress: ProgressCb = None,
) -> CompressResult:
    """
    Build a clean deliverables folder, compress MP4s (iOS H.264), copy stills.

    Output: ``rallies/{slug} (iOS Compressed)/`` sibling of the rally dir,
    or under a display-name label for clarity.
    """
    if not rally_dir.is_dir():
        raise FileNotFoundError(f"Rally directory not found: {rally_dir}")

    sources = collect_deliverable_sources(rally_dir)
    if not sources:
        raise RuntimeError("No deliverable files found to compress")

    missing: list[str] = []
    for name in DELIVERABLES:
        if (rally_dir / name).is_file():
            continue
        alts = STILL_FALLBACKS.get(name, ())
        if any((rally_dir / alt).is_file() for alt in alts):
            continue
        missing.append(name)
    if missing:
        await _emit(progress, f"Warning: missing {', '.join(missing)}")

    parent = rally_dir.parent
    label = (display_name or rally_dir.name).strip() or rally_dir.name
    # Keep filesystem-safe sibling folder name
    safe = label.replace("/", "-").replace("\\", "-").strip() or rally_dir.name
    out_dir = parent / f"{safe} (iOS Compressed)"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    await _emit(progress, f"iOS compress → {out_dir.name}")
    await _emit(progress, f"Using settings from {COMPRESS_SCRIPT_IOS.name}")

    rows: list[FileSizeRow] = []
    log: list[str] = []
    before_total = 0
    after_total = 0

    for src in sources:
        before = _file_size(src)
        before_total += before
        dest = out_dir / src.name
        ext = src.suffix.lower()

        if ext == ".mp4":
            await _emit(
                progress,
                f"Compressing {src.name} ({_fmt_bytes(before)})…",
            )
            await asyncio.to_thread(_ffmpeg_ios, src, dest)
            after = _file_size(dest)
            after_total += after
            row = FileSizeRow(name=src.name, before=before, after=after)
            rows.append(row)
            msg = (
                f"✓ {src.name}: {_fmt_bytes(before)} → {_fmt_bytes(after)} "
                f"({row.pct:.0f}% smaller)"
                if after < before
                else f"✓ {src.name}: {_fmt_bytes(before)} → {_fmt_bytes(after)}"
            )
            log.append(msg)
            await _emit(progress, msg)
        else:
            await _emit(progress, f"Copying {src.name} ({_fmt_bytes(before)})…")
            await asyncio.to_thread(shutil.copy2, src, dest)
            after = _file_size(dest)
            after_total += after
            rows.append(FileSizeRow(name=src.name, before=before, after=after))
            msg = f"✓ {src.name}: copied {_fmt_bytes(after)}"
            log.append(msg)
            await _emit(progress, msg)

    summary = (
        f"Done: {_fmt_bytes(before_total)} → {_fmt_bytes(after_total)} "
        f"(saved {_fmt_bytes(max(0, before_total - after_total))}, "
        f"{(100 * (1 - after_total / before_total)) if before_total else 0:.0f}%)"
    )
    log.append(summary)
    await _emit(progress, summary)

    return CompressResult(
        output_dir=out_dir,
        rows=rows,
        before_total=before_total,
        after_total=after_total,
        log=log,
    )


def compress_rally(rally_dir: Path, display_name: str | None = None) -> Path:
    """Sync wrapper — returns compressed output directory path."""
    result = asyncio.run(
        compress_rally_ios(rally_dir, display_name=display_name)
    )
    return result.output_dir
