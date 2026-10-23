"""Step generation backends: stub (offline) and api (Grok Imagine)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from app.config import (
    GENERATE_BACKEND,
    VIDEO_DURATION_DEFAULT,
)
from app.models.rally import (
    PromptHistoryEntry,
    RallyPackage,
    StepKind,
    utc_now_iso,
)
from app.pipeline import graph
from app.pipeline.frames import resolve_start_still
from app.pipeline.prompts import (
    compose_idle_still_prompt,
    compose_staged_still_prompt,
    refresh_composed,
    staged_still_name,
)

ProgressCb = Callable[[str], Any] | Callable[[str], Awaitable[None]] | None


async def _emit(progress: ProgressCb, message: str) -> None:
    if not progress:
        return
    result = progress(message)
    if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
        await result  # type: ignore[arg-type]


def _font(size: int = 28):
    try:
        return ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size)
    except OSError:
        return ImageFont.load_default()


def _make_placeholder_image(
    path: Path,
    *,
    title: str,
    subtitle: str,
    aspect: str = "9:16",
    color: tuple[int, int, int] = (40, 44, 72),
) -> None:
    if aspect == "1:1":
        w, h = 720, 720
    else:
        w, h = 720, 1280
    img = Image.new("RGB", (w, h), color)
    draw = ImageDraw.Draw(img)
    title_font = _font(36)
    sub_font = _font(22)
    margin = 40

    def center_text(y: int, text: str, font, fill=(230, 230, 240)):
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        x = max(margin, (w - tw) // 2)
        draw.text((x, y), text, font=font, fill=fill)

    center_text(h // 2 - 80, title, title_font, (255, 220, 100))
    # wrap subtitle
    words = subtitle.split()
    lines: list[str] = []
    cur = ""
    for word in words:
        trial = f"{cur} {word}".strip()
        bbox = draw.textbbox((0, 0), trial, font=sub_font)
        if bbox[2] - bbox[0] > w - 2 * margin and cur:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    y = h // 2
    for line in lines[:8]:
        center_text(y, line, sub_font, (200, 205, 220))
        y += 32
    center_text(h - 80, "STUB GENERATOR", sub_font, (140, 145, 170))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")


def _make_placeholder_video(path: Path, still: Path | None, duration: float = 2.0) -> None:
    """Create a tiny looping-friendly mp4 via ffmpeg if available, else copy note file."""
    import shutil
    import subprocess

    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg and still and still.is_file():
        # Still image → short mp4
        cmd = [
            ffmpeg,
            "-y",
            "-loop",
            "1",
            "-i",
            str(still),
            "-c:v",
            "libx264",
            "-t",
            str(duration),
            "-pix_fmt",
            "yuv420p",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-movflags",
            "+faststart",
            str(path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return

    # Fallback: write a zero-byte marker is bad for players — use color source if ffmpeg exists
    if ffmpeg:
        cmd = [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x282c48:s=720x1280:d={duration}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return

    raise RuntimeError("ffmpeg not found — required for stub video generation")


async def generate_step(
    pkg: RallyPackage,
    step_id: str,
    progress: ProgressCb = None,
) -> None:
    """Generate (or regenerate) a single pipeline step."""
    ok, reason = graph.can_generate(pkg, step_id)
    if not ok:
        raise ValueError(reason)

    st = pkg.step(step_id)
    if step_id == "Idle" and not st.idle_want_mp4() and not st.idle_want_png():
        raise ValueError("Select at least one Idle output: MP4 (default) and/or PNG")

    was_preview_approved = (
        step_id == "Preview" and pkg.gates["preview"].value == "approved"
    )
    was_idle_approved = step_id == "Idle" and pkg.gates["idle"].value == "approved"

    if was_preview_approved:
        graph.invalidate_downstream_from_preview(pkg)
    elif was_idle_approved:
        graph.invalidate_branch_from_idle(pkg)

    # Before archiving Preview.png, ensure a reference still exists if the
    # user wants image-edit grounding from the current still.
    if step_id == "Preview":
        import shutil

        from app.pipeline.explicit_mark import preview_source_path

        preview = preview_source_path(pkg)
        ref = pkg.path_for("Preview.reference.png")
        if (
            st.options.get("use_reference")
            and preview.is_file()
            and not ref.is_file()
        ):
            shutil.copy2(preview, ref)

    # Archive previous outputs into history (does not touch Preview.reference.png)
    archived = pkg.archive_outputs(step_id)
    if archived or st.composed_prompt:
        st.history.append(
            PromptHistoryEntry(
                at=utc_now_iso(),
                base_prompt=st.base_prompt,
                custom_guidance=st.custom_guidance,
                composed_prompt=st.composed_prompt,
                paths=archived,
            )
        )

    composed = refresh_composed(
        st,
        pkg.concept,
        bible=getattr(pkg, "skill_note", "") or "",
        template_id=getattr(pkg, "template", "character"),
    )
    graph.mark_running(pkg, step_id)
    await _emit(progress, f"Starting {step_id} ({GENERATE_BACKEND})…")

    backend = GENERATE_BACKEND
    try:
        if backend == "stub":
            await _generate_stub(pkg, step_id, composed, progress)
        elif backend == "api":
            await _generate_api(pkg, step_id, composed, progress)
        elif backend == "cli":
            await _generate_cli(pkg, step_id, composed, progress)
        else:
            raise ValueError(
                f"Unknown backend: {backend} (use stub, cli, or api)"
            )
        st = pkg.step(step_id)
        # Clear any legacy troubleshooting flags
        st.options.pop("video_zdr_blocked", None)
        st.options.pop("video_cli_handoff", None)
        st.error = None
        if step_id == "Preview":
            from app.pipeline.explicit_mark import finish_preview_generate

            try:
                mark_msg = finish_preview_generate(pkg)
                await _emit(progress, mark_msg)
                pkg.save()
            except Exception as mark_exc:  # noqa: BLE001
                await _emit(
                    progress,
                    f"Preview generated; E overlay failed: {mark_exc}",
                )
        await _emit(progress, f"{step_id} finished — ready for review")
        graph.mark_needs_review(pkg, step_id, composed, warning=None)
    except Exception as exc:  # noqa: BLE001 — surface to UI
        await _emit(progress, f"Failed: {exc}")
        graph.mark_error(pkg, step_id, str(exc))
        raise


async def _idle_still(
    pkg: RallyPackage,
    composed: str,
    *,
    backend: str,
    progress: ProgressCb,
) -> Path:
    """Build IdleKeyframe.png from Preview when possible."""
    still = pkg.path_for("IdleKeyframe.png")
    from app.pipeline.explicit_mark import preview_source_path

    preview = preview_source_path(pkg)
    aspect = pkg.aspect_ratio
    st = pkg.step("Idle")
    tid = getattr(pkg, "template", "character") or "character"
    still_prompt = compose_idle_still_prompt(
        concept=pkg.concept,
        base_prompt=st.base_prompt,
        custom_guidance=st.custom_guidance,
        character_bible=getattr(pkg, "skill_note", "") or "",
        template_id=tid,
    )
    # Keep composed available for debugging; still uses pose-only prompt
    _ = composed

    if preview.is_file():
        await _emit(
            progress,
            "Building Idle keyframe from approved Preview (lock)…",
        )
        if backend in ("cli", "api"):
            from app.pipeline import imagine

            await imagine.generate_image(
                prompt=still_prompt,
                dest=still,
                aspect_ratio=aspect,
                image_path=preview,
            )
        else:
            _make_placeholder_image(
                still,
                title="Idle Keyframe",
                subtitle=f"from Preview · {(pkg.concept or pkg.name)[:60]}",
                aspect=aspect,
                color=(35, 70, 60),
            )
            await asyncio.sleep(0.3)
    else:
        await _emit(progress, "No Preview.png — generating Idle keyframe from text…")
        if backend in ("cli", "api"):
            from app.pipeline import imagine

            await imagine.generate_image(
                prompt=still_prompt, dest=still, aspect_ratio=aspect
            )
        else:
            _make_placeholder_image(
                still,
                title="Idle Keyframe",
                subtitle=(pkg.concept or pkg.name)[:80],
                aspect=aspect,
                color=(35, 70, 60),
            )
            await asyncio.sleep(0.3)
    return still


async def _animate_from_keyframe(
    *,
    prompt: str,
    keyframe: Path,
    dest: Path,
    duration: int | float,
    aspect_ratio: str,
    backend: str,
    progress: ProgressCb,
    label: str,
) -> None:
    """
    image→video.

    stub  — local ffmpeg placeholder
    cli / api — Imagine REST + S3 staging (ZDR-safe). CLI uses grok session
    token; api prefers XAI_API_KEY (falls back to session if unset).
    """
    if backend == "stub":
        await asyncio.to_thread(_make_placeholder_video, dest, keyframe, 2.0)
        return

    if backend not in ("cli", "api"):
        raise ValueError(f"Unknown backend for video: {backend}")

    from app.pipeline.video_gen import generate_video_from_image

    # Imagine video duration: clamp to 6 or 10 for snappy loops when near defaults
    dur = int(duration)
    if backend == "cli" and dur not in (6, 10):
        dur = 10 if dur >= 8 else 6

    async def on_progress(msg: str) -> None:
        await _emit(progress, f"{label}: {msg}")

    await _emit(
        progress,
        f"{label}: Imagine video via S3 staging ({backend})…",
    )
    await generate_video_from_image(
        prompt=prompt,
        image_path=keyframe,
        dest=dest,
        duration=dur,
        aspect_ratio=aspect_ratio,
        progress=on_progress,
    )


async def _generate_idle(
    pkg: RallyPackage,
    composed: str,
    *,
    backend: str,
    progress: ProgressCb,
) -> None:
    st = pkg.step("Idle")
    want_mp4 = st.idle_want_mp4()
    want_png = st.idle_want_png()
    if not want_mp4 and not want_png:
        raise ValueError("Select MP4 and/or PNG for Idle")

    duration = pkg.duration_default or VIDEO_DURATION_DEFAULT
    # Keyframe required whenever MP4 is requested (source frame) or PNG is selected
    need_still = want_png or want_mp4
    still: Path | None = None
    if need_still:
        still = await _idle_still(pkg, composed, backend=backend, progress=progress)
        await _emit(progress, f"Keyframe ready → {still.name}")

    st.options.pop("video_zdr_blocked", None)
    st.options.pop("video_cli_handoff", None)

    if want_mp4:
        assert still is not None
        video = pkg.path_for("Idle.mp4")
        # Prefer a short raw clip; boomerang doubles length (6s → ~12s loop)
        idle_duration = min(6, max(4, int(duration or 6)))
        await _emit(
            progress,
            f"Animating Idle.mp4 from keyframe ({backend}, ~{idle_duration}s)… "
            "this can take several minutes",
        )
        await _animate_from_keyframe(
            prompt=composed,
            keyframe=still,
            dest=video,
            duration=idle_duration,
            aspect_ratio=pkg.aspect_ratio,
            backend=backend,
            progress=progress,
            label="Idle.mp4",
        )

        # Always keep the un-boomeranged take so the checkbox can toggle live
        import shutil

        raw = pkg.path_for("Idle.raw.mp4")
        if video.is_file():
            shutil.copy2(video, raw)
        if st.idle_want_boomerang():
            await _emit(
                progress,
                "Boomerang post-process: forward + reverse, audio stripped "
                "(clean visual loop)…",
            )
            from app.pipeline.boomerang import make_boomerang

            await asyncio.to_thread(
                make_boomerang,
                raw if raw.is_file() else video,
                video,
                strip_audio=True,
                max_forward_seconds=None,
            )
            st.options["boomerang_applied"] = True
            await _emit(
                progress,
                "Idle.mp4 boomerang ready (silent loop). Raw take: Idle.raw.mp4",
            )
        else:
            st.options["boomerang_applied"] = False
            await _emit(progress, "Idle.mp4 ready (boomerang off). Raw take kept")
        from app.pipeline.frames import FrameError, extract_last_frame

        last = pkg.path_for("Idle.last.png")
        try:
            await asyncio.to_thread(extract_last_frame, video, last)
            await _emit(
                progress,
                "Idle.last.png — start frame for Accept / Decline / Reschedule",
            )
        except FrameError as exc:
            await _emit(progress, f"Could not extract Idle last frame: {exc}")
        st.error = None
        pkg.save()
    elif want_png:
        await _emit(progress, "IdleKeyframe.png ready (MP4 skipped)")


async def _generate_imagine(
    pkg: RallyPackage,
    step_id: str,
    composed: str,
    *,
    backend: str,
    progress: ProgressCb = None,
) -> None:
    """
    Direct Imagine path (no Grok CLI agent / Terminal windows).

    ``cli`` and ``api`` share the same code; only credential preference differs
    (session token vs XAI_API_KEY — see auth_token.resolve_bearer_token).
    """
    from app.pipeline import imagine
    from app.pipeline.auth_token import resolve_bearer_token

    st = pkg.step(step_id)
    aspect = pkg.aspect_ratio
    duration = pkg.duration_default or VIDEO_DURATION_DEFAULT
    _token, auth_src = resolve_bearer_token()
    await _emit(progress, f"Imagine auth: {auth_src} (direct API, no CLI agent)")

    if st.kind == StepKind.IMAGE:
        out = pkg.path_for(st.outputs[0])
        # Optional user-dropped reference (survives archive of previous Preview.png)
        ref = pkg.path_for("Preview.reference.png")
        image_path = None
        prompt = composed
        if step_id == "Preview" and ref.is_file():
            image_path = ref
            prompt = (
                f"{composed}\n\n"
                "The provided image is the look lock. Keep the subject. "
                f"Reframe to {aspect}. Still only."
            )
            await _emit(
                progress,
                f"Imagine image-edit from reference "
                f"({ref.stat().st_size // 1024} KB)…",
            )
        else:
            await _emit(progress, "Imagine image (text-to-image)…")
        await imagine.generate_image(
            prompt=prompt,
            dest=out,
            aspect_ratio=aspect,
            image_path=image_path,
        )
        return

    if st.kind == StepKind.IDLE:
        await _generate_idle(pkg, composed, backend=backend, progress=progress)
        return

    if st.kind == StepKind.VIDEO:
        video_name = st.outputs[0]
        video = pkg.path_for(video_name)
        keyframe = await _ensure_video_start_still(
            pkg, step_id, backend=backend, progress=progress
        )
        if not keyframe.is_file():
            raise FileNotFoundError(
                f"{keyframe.name} missing — need a start still for {step_id}"
            )
        await _emit(
            progress,
            f"Animating {video_name} from {keyframe.name}…",
        )
        await _animate_from_keyframe(
            prompt=composed,
            keyframe=keyframe,
            dest=video,
            duration=duration,
            aspect_ratio=aspect,
            backend=backend,
            progress=progress,
            label=video_name,
        )
        return

    raise ValueError(f"Unsupported step kind: {st.kind}")


async def _generate_cli(
    pkg: RallyPackage,
    step_id: str,
    composed: str,
    progress: ProgressCb = None,
) -> None:
    """Subscription path: same as API, credentials from grok login session."""
    await _generate_imagine(
        pkg, step_id, composed, backend="cli", progress=progress
    )


async def _generate_api(
    pkg: RallyPackage,
    step_id: str,
    composed: str,
    progress: ProgressCb = None,
) -> None:
    """API key path: same Imagine calls; prefers XAI_API_KEY when set."""
    await _generate_imagine(
        pkg, step_id, composed, backend="api", progress=progress
    )


async def _generate_stub(
    pkg: RallyPackage,
    step_id: str,
    composed: str,
    progress: ProgressCb = None,
) -> None:
    """Local placeholder media so the pipeline UX can be tested offline."""
    st = pkg.step(step_id)
    await _emit(progress, "Stub generator running…")
    await asyncio.sleep(0.5)

    concept_bit = (pkg.concept or pkg.name)[:80]
    subtitle = composed[:200].replace("\n", " ")

    if st.kind == StepKind.IMAGE:
        out = pkg.path_for(st.outputs[0])
        _make_placeholder_image(
            out,
            title=f"{step_id}",
            subtitle=f"{concept_bit} — {subtitle}",
            aspect=pkg.aspect_ratio,
            color=(45, 55, 90),
        )
        return

    if st.kind == StepKind.IDLE:
        await _generate_idle(pkg, composed, backend="stub", progress=progress)
        return

    if st.kind == StepKind.VIDEO:
        video_name = st.outputs[0]
        video = pkg.path_for(video_name)
        keyframe = resolve_start_still(pkg, step_id, extract=False)
        if not keyframe.is_file():
            # Create a step-colored still first
            tmp_still = pkg.path_for(f".stub_{step_id}.png")
            colors = {
                "Rally": (90, 40, 50),
                "Message": (40, 60, 90),
                "Accept": (30, 80, 50),
                "Decline": (80, 50, 40),
                "Adjust": (70, 60, 30),
            }
            _make_placeholder_image(
                tmp_still,
                title=step_id,
                subtitle=concept_bit,
                aspect=pkg.aspect_ratio,
                color=colors.get(step_id, (50, 50, 70)),
            )
            keyframe = tmp_still
        await asyncio.to_thread(
            _make_placeholder_video, video, keyframe, float(pkg.duration_default)
        )
        return

    raise ValueError(f"Unsupported step kind: {st.kind}")
