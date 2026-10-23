"""Post-process videos into a visual boomerang (forward + reverse) loop."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.models.rally import RallyPackage


class BoomerangError(RuntimeError):
    pass


def _ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise BoomerangError("ffmpeg not found on PATH")
    return ffmpeg


def video_duration_seconds(video: Path) -> float:
    """Duration in seconds via ffprobe (falls back to ffmpeg)."""
    probe = shutil.which("ffprobe")
    video = Path(video)
    if probe:
        result = subprocess.run(
            [
                probe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(video),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            try:
                return float((result.stdout or "").strip())
            except ValueError:
                pass
    ffmpeg = _ffmpeg()
    result = subprocess.run(
        [ffmpeg, "-i", str(video)],
        capture_output=True,
        text=True,
    )
    # ffmpeg prints Duration: HH:MM:SS.xx on stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
    if not m:
        raise BoomerangError(f"Could not read duration of {video.name}")
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mi * 60 + s


def extract_forward_half(video: Path, dest: Path) -> Path:
    """First half of a ping-pong clip ≈ the original forward take."""
    ffmpeg = _ffmpeg()
    video = Path(video)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dur = video_duration_seconds(video)
    half = max(0.2, dur / 2.0)
    tmp = dest.with_suffix(".tmp.mp4")
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video),
            "-t",
            f"{half:.3f}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(tmp),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < 1000:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
        raise BoomerangError(
            f"Could not split forward half of {video.name}:\n"
            f"{(result.stderr or result.stdout or '')[-400:]}"
        )
    tmp.replace(dest)
    return dest


def make_boomerang(
    src: Path,
    dest: Path | None = None,
    *,
    strip_audio: bool = True,
    max_forward_seconds: float | None = None,
) -> Path:
    """
    Build a ping-pong loop: play forward, then reverse back to the start frame.

    Audio: reversed speech/SFX sounds wrong, so by default we **strip audio**
    (silent loop). That is the right default for DutyCall idle skins.

    If max_forward_seconds is set, only the first N seconds of the source are
    used before reversing (keeps final length ~2N instead of 2*full).
    """
    ffmpeg = _ffmpeg()

    src = src.resolve()
    if not src.is_file():
        raise BoomerangError(f"Source video missing: {src}")

    dest = (dest or src).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="rally-boom-") as tmp:
        tmp_path = Path(tmp)
        work = tmp_path / "forward.mp4"
        out_tmp = tmp_path / "boomerang.mp4"

        # Optional trim to keep final loop short
        if max_forward_seconds and max_forward_seconds > 0:
            trim = subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(src),
                    "-t",
                    str(max_forward_seconds),
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "fast",
                    "-crf",
                    "20",
                    "-pix_fmt",
                    "yuv420p",
                    str(work),
                ],
                capture_output=True,
                text=True,
            )
            if trim.returncode != 0:
                raise BoomerangError(
                    f"ffmpeg trim failed:\n{trim.stderr or trim.stdout}"
                )
            forward = work
        else:
            forward = src

        # reverse + concat video only (no audio graph)
        # [0:v]split into original stream and reverse, then concat
        filter_complex = "[0:v]split[v0][v1];[v1]reverse[vr];[v0][vr]concat=n=2:v=1:a=0[v]"
        cmd = [
            ffmpeg,
            "-y",
            "-i",
            str(forward),
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(out_tmp),
        ]
        # strip_audio is always true for the filter path above; keep flag for API clarity
        if not strip_audio:
            # Keep forward audio only for first half, silence for reverse — still awkward.
            # Prefer mute for product; fall through to silent if anything fails.
            pass

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise BoomerangError(
                f"ffmpeg boomerang failed:\n{result.stderr or result.stdout}"
            )

        # Atomic-ish replace
        if dest.exists():
            dest.unlink()
        shutil.move(str(out_tmp), str(dest))

    return dest


def _refresh_idle_last_frame(pkg: RallyPackage) -> None:
    video = pkg.path_for("Idle.mp4")
    last = pkg.path_for("Idle.last.png")
    if not video.is_file():
        return
    try:
        from app.pipeline.frames import extract_last_frame

        extract_last_frame(video, last)
    except Exception:  # noqa: BLE001 — last-frame is best-effort
        pass


def idle_boomerang_is_applied(pkg: RallyPackage) -> bool:
    st = pkg.step("Idle")
    flag = st.options.get("boomerang_applied")
    if flag is not None:
        return bool(flag)
    raw = pkg.path_for("Idle.raw.mp4")
    video = pkg.path_for("Idle.mp4")
    if raw.is_file() and video.is_file():
        return video.stat().st_size != raw.stat().st_size
    return bool(st.options.get("boomerang", False)) and video.is_file()


def apply_idle_boomerang(pkg: RallyPackage, enabled: bool) -> str:
    """
    Make live Idle.mp4 match the checkbox using Idle.raw.mp4.

    Does not regenerate. Uncheck restores the original take; check rebuilds
    the forward+reverse loop from that take.
    """
    st = pkg.step("Idle")
    video = pkg.path_for("Idle.mp4")
    raw = pkg.path_for("Idle.raw.mp4")
    applied = idle_boomerang_is_applied(pkg)

    if enabled == applied and video.is_file():
        st.options["boomerang"] = enabled
        st.options["boomerang_applied"] = enabled
        return "Idle already " + ("boomeranged" if enabled else "the original take")

    if enabled:
        if not raw.is_file():
            if not video.is_file():
                raise BoomerangError("Generate Idle.mp4 first")
            if applied:
                extract_forward_half(video, raw)
            else:
                shutil.copy2(video, raw)
        make_boomerang(raw, video, strip_audio=True)
        st.options["boomerang"] = True
        st.options["boomerang_applied"] = True
        _refresh_idle_last_frame(pkg)
        return "Boomerang on — Idle.mp4 is forward + reverse"

    if raw.is_file():
        shutil.copy2(raw, video)
    elif video.is_file() and applied:
        extract_forward_half(video, raw)
        shutil.copy2(raw, video)
    elif video.is_file():
        shutil.copy2(video, raw)
    else:
        raise BoomerangError("Generate Idle.mp4 first")
    st.options["boomerang"] = False
    st.options["boomerang_applied"] = False
    _refresh_idle_last_frame(pkg)
    return "Boomerang off — Idle.mp4 is the original take"
