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
from app import imagine_settings
from app.models.rally import (
    PromptHistoryEntry,
    RallyPackage,
    StepKind,
    StepStatus,
    clip_label,
    clip_video_name,
    utc_now_iso,
)
from app.pipeline import graph
from app.pipeline.formula import _as_shot, compile_formula
from app.pipeline.prompts import (
    refresh_composed,
)
from app.pipeline.start_still import (
    ensure_video_start_still,
    generate_start_still,
)

ProgressCb = Callable[[str], Any] | Callable[[str], Awaitable[None]] | None

_PREVIEW_POSE_SEED = ".preview-pose-seed.png"


def _stage_preview_pose_seed(pkg: RallyPackage) -> Path | None:
    """Copy Frame 1 still before archive so Make still can image-edit it.

    Unique shot → no seed (text-to-image). Frame 1 / current Preview → seed.
    """
    import shutil

    from app.pipeline.explicit_mark import as_imagine_still, preview_source_path
    from app.pipeline.start_still import frame1_path, frame1_type

    kind = frame1_type(pkg, "Preview", "still")
    if kind == "unique":
        return None
    src = None
    if kind == "start":
        src = frame1_path(pkg, "Preview", "still")
        if src is None or not src.is_file():
            src = preview_source_path(pkg)
        src = as_imagine_still(pkg, src) or src
    if src is None or not src.is_file():
        # Frame 1 is start but nothing on disk yet — first still is text-to-image.
        return None
    dest = pkg.dir() / _PREVIEW_POSE_SEED
    shutil.copy2(src, dest)
    return dest


def _voice_ids_for(pkg: RallyPackage, step_id: str) -> list[str]:
    """Preset voice only when the clip pins one. Empty = Imagine invents it."""
    try:
        speaker = str(pkg.step(step_id).options.get("speaker") or "").strip().lower()
    except Exception:  # noqa: BLE001
        speaker = ""
    if not speaker or speaker in {"invent", "none", "auto"}:
        return []
    return [speaker]


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
    """Create a tiny looping-friendly mp4 via ffmpeg if available."""
    import shutil
    import subprocess

    path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg and still and still.is_file():
        cmd = [
            ffmpeg, "-y", "-loop", "1", "-i", str(still),
            "-c:v", "libx264", "-t", str(duration), "-pix_fmt", "yuv420p",
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-movflags", "+faststart", str(path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return
    if ffmpeg:
        cmd = [
            ffmpeg, "-y", "-f", "lavfi",
            "-i", f"color=c=0x282c48:s=720x1280:d={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(path),
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

    preview_seed: Path | None = None
    if step_id == "Preview":
        import shutil

        from app.pipeline.explicit_mark import preview_source_path

        preview_seed = _stage_preview_pose_seed(pkg)
        preview = preview_source_path(pkg)
        ref = pkg.path_for("Preview.reference.png")
        if (
            st.options.get("use_reference")
            and preview.is_file()
            and not ref.is_file()
        ):
            shutil.copy2(preview, ref)

    from app.pipeline.scenes import (
        archive_for_generate,
        cut_is_showing,
        has_timeline,
    )

    if (
        st.kind in {StepKind.VIDEO, StepKind.IDLE}
        and has_timeline(pkg, step_id)
        and not cut_is_showing(pkg, step_id)
    ):
        raise ValueError(
            "This highlighted take is a separate copy. "
            "Select the edited take to replace a scene."
        )

    prior_status = st.status
    archived: list[str] = []
    try:
        archived = archive_for_generate(pkg, step_id)
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

        surface = "still" if step_id == "Preview" else "take"
        composed = compile_formula(pkg, step_id, surface)
        if not composed:
            composed = refresh_composed(
                st,
                pkg.concept,
                bible=getattr(pkg, "skill_note", "") or "",
                template_id=getattr(pkg, "template", "character"),
            )
        st.options["last_sent_prompt"] = composed
        graph.mark_running(pkg, step_id)
        await _emit(progress, f"Starting {clip_label(step_id)} ({GENERATE_BACKEND})…")
        await _emit(
            progress,
            "Prompt: " + " ".join(composed.split())[:280],
        )

        backend = GENERATE_BACKEND
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
        st.options.pop("video_zdr_blocked", None)
        st.options.pop("video_cli_handoff", None)
        st.options.pop("shown_take", None)
        if st.kind in {StepKind.VIDEO, StepKind.IDLE}:
            st.options["work_surface"] = "take"
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
        await _emit(progress, f"{clip_label(step_id)} finished — ready for review")
        st = pkg.step(step_id)
        shot = _as_shot(st.composed_prompt or st.custom_guidance or "")
        graph.mark_needs_review(pkg, step_id, shot, warning=None)
    except Exception as exc:  # noqa: BLE001
        await _emit(progress, f"Failed: {exc}")
        restored = pkg.restore_archived_outputs(step_id, archived)
        if restored:
            st = pkg.step(step_id)
            st.status = (
                prior_status
                if prior_status != StepStatus.RUNNING
                else StepStatus.NEEDS_REVIEW
            )
            # The previous take is back, but the new one did not happen.
            st.error = str(exc)
            pkg.save()
            await _emit(
                progress,
                "Put the previous file back: " + ", ".join(restored),
            )
        else:
            graph.mark_error(pkg, step_id, str(exc))
        raise
    finally:
        if preview_seed is not None:
            preview_seed.unlink(missing_ok=True)


def _latest_history_video(pkg: RallyPackage, video_name: str) -> Path | None:
    hist = pkg.dir() / "history"
    if not hist.is_dir():
        return None
    stem = Path(video_name).stem
    suf = Path(video_name).suffix.lower()
    older = [
        p
        for p in hist.glob(f"{stem}.*")
        if p.is_file() and p.suffix.lower() == suf
    ]
    if not older:
        return None
    older.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return older[0]


async def nudge_step(
    pkg: RallyPackage,
    step_id: str,
    delta: str,
    progress: ProgressCb = None,
) -> Path:
    """Edit the current take. Delta only — do not restate the world."""
    import shutil

    st = pkg.step(step_id)
    from app.pipeline.scenes import (
        cut_is_showing,
        has_timeline,
        save_preview_attempt,
        save_scene_attempt,
        selected_id,
        shown_take_path,
        write_target,
    )

    on_cut = has_timeline(pkg, step_id) and cut_is_showing(pkg, step_id)
    previewing_copy = has_timeline(pkg, step_id) and not on_cut
    scene_id = selected_id(pkg, step_id) if on_cut else ""
    prior = st.status
    if on_cut:
        video = write_target(pkg, step_id)
        video_name = video.name
    elif previewing_copy:
        video = shown_take_path(pkg, step_id)
        if video is None:
            raise ValueError("No take to nudge — make a take first")
        video_name = video.name
    else:
        video_name = st.primary_output() or (
            st.outputs[0] if st.outputs else clip_video_name(step_id)
        )
        video = pkg.path_for(video_name)
    if not video.is_file():
        prev = _latest_history_video(pkg, video_name)
        if prev is None:
            raise ValueError("No take to nudge — make a take first")
        shutil.copy2(prev, video)
        await _emit(progress, f"Restored {video_name} from history")
    delta = (delta or "").strip()
    if not delta:
        raise ValueError("Say the one thing to change")
    tmp = video.with_name(video.stem + ".nudge-tmp.mp4")
    tmp.unlink(missing_ok=True)
    graph.mark_running(pkg, step_id)
    await _emit(progress, f"Nudging {video_name}…")
    try:
        from app.pipeline.video_gen import edit_video

        await edit_video(
            prompt=delta,
            video_path=video,
            dest=tmp,
            progress=progress,
        )
        if not tmp.is_file() or tmp.stat().st_size < 1000:
            raise RuntimeError("Nudge produced an empty take")
        st.error = None
        st.options["last_nudge"] = delta
        st.options["last_sent_prompt"] = delta
        if on_cut:
            # Leave the cut on the timeline. The nudge is a take they can use.
            rel = await asyncio.to_thread(
                save_scene_attempt, pkg, step_id, scene_id, tmp
            )
            st.options["shown_take"] = rel
            st.options["last_sent_mode"] = f"video-edit of {video_name}"
            if prior != StepStatus.RUNNING:
                st.status = prior
            else:
                st.status = StepStatus.NEEDS_REVIEW
            pkg.save()
            await _emit(progress, "Nudge saved as a take. The cut is unchanged.")
            return pkg.dir() / rel
        if previewing_copy:
            # Edit the highlighted copy. Do not overwrite it or the cut.
            rel = await asyncio.to_thread(
                save_preview_attempt, pkg, step_id, tmp
            )
            st.options["shown_take"] = rel
            st.options["last_sent_mode"] = f"video-edit of {video_name}"
            if prior != StepStatus.RUNNING:
                st.status = prior
            else:
                st.status = StepStatus.NEEDS_REVIEW
            pkg.save()
            await _emit(
                progress,
                "Nudge saved as a new take. The original and the cut are unchanged.",
            )
            return pkg.dir() / rel
        pkg.archive_outputs(step_id)
        shutil.move(str(tmp), str(video))
        st.options.pop("shown_take", None)
        st.options["last_sent_mode"] = f"video-edit of {video_name}"
        graph.mark_needs_review(
            pkg, step_id, st.composed_prompt or delta, warning=None
        )
        await _emit(progress, f"{video_name} nudged")
        return video
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        graph.mark_error(pkg, step_id, str(exc))
        raise


async def fresh_take_on_clip(
    pkg: RallyPackage,
    step_id: str,
    prompt: str,
    progress: ProgressCb = None,
) -> Path:
    """New take of the selected clip, pinned to its first and last frames.

    The render is a history take for that clip. The cut is not replaced.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("Say what this clip should do")
    from app.pipeline.scenes import (
        capture_clip_pins,
        cut_is_showing,
        has_timeline,
        save_scene_attempt,
    )

    if not has_timeline(pkg, step_id) or not cut_is_showing(pkg, step_id):
        raise ValueError("Select a clip on the edited take")
    st = pkg.step(step_id)
    prior = st.status
    info = await asyncio.to_thread(capture_clip_pins, pkg, step_id)
    scene_id = str(info["scene_id"])
    sent = clip_send_seconds(int(info["duration"]))
    label = str(info["label"])
    tmp = pkg.dir() / "history" / f".fresh-{scene_id}.mp4"
    tmp.unlink(missing_ok=True)
    graph.mark_running(pkg, step_id)
    await _emit(progress, f"Fresh take · {label}…")
    try:
        await _animate_from_keyframe(
            prompt=prompt,
            keyframe=info["first"],
            dest=tmp,
            duration=int(info["duration"]),
            aspect_ratio=pkg.aspect_ratio,
            backend=GENERATE_BACKEND,
            progress=progress,
            label=label,
            last_frame=info["last"],
            voice_ids=_voice_ids_for(pkg, step_id),
        )
        if not tmp.is_file() or tmp.stat().st_size < 1000:
            raise RuntimeError("Fresh take produced an empty clip")
        rel = await asyncio.to_thread(
            save_scene_attempt, pkg, step_id, scene_id, tmp
        )
        st.error = None
        st.options["shown_take"] = rel
        st.options["scene_id"] = scene_id
        st.options["last_fresh"] = prompt
        st.options["last_sent_prompt"] = prompt
        st.options["last_sent_mode"] = (
            f"fresh take · {label.lower()} · first frame → last frame · {sent}s"
        )
        if prior != StepStatus.RUNNING:
            st.status = prior
        else:
            st.status = StepStatus.NEEDS_REVIEW
        pkg.save()
        await _emit(
            progress,
            "Fresh take saved under this clip. The cut is unchanged.",
        )
        return pkg.dir() / rel
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        graph.mark_error(pkg, step_id, str(exc))
        raise


def _reload(pkg: RallyPackage) -> RallyPackage:
    return RallyPackage.load(pkg.dir())


def _restore_status(pkg: RallyPackage, step_id: str, prior: StepStatus) -> None:
    st = pkg.step(step_id)
    if st.status != StepStatus.RUNNING:
        return
    st.status = prior if prior != StepStatus.RUNNING else StepStatus.NEEDS_REVIEW
    st.error = None


async def editor_generate(
    pkg: RallyPackage,
    step_id: str,
    kind: str,
    prompt: str,
    *,
    start_frame: bool = True,
    pins: str = "both",
    duration: int | None = None,
    progress: ProgressCb = None,
    dest_scene_id: str = "",
    pinned: dict | None = None,
) -> dict:
    """Fill the editor placeholder for a redo, nudge, or new scene.

    The placeholder is already on the timeline. This does not mark the
    step running, so other clips can be cut while Imagine works. The shot
    prompt (composed_prompt / custom_guidance) is left alone.
    """
    prompt = (prompt or "").strip()
    dest_scene_id = str(dest_scene_id or "").strip()
    if kind not in {"redo-frames", "redo-plain", "nudge", "new-scene"}:
        raise ValueError("Unknown edit")
    if not prompt:
        raise ValueError(
            "Say the one thing to change"
            if kind == "nudge"
            else "Say what this clip should do"
        )
    if dest_scene_id and pinned is None:
        raise ValueError("Missing clip for this job")
    from app.pipeline.scenes import (
        SceneError,
        append_generated_scene,
        capture_editor_pins,
        drop_pending_scene,
        editor_target,
        fulfill_pending_scene,
    )

    try:
        target = dict(pinned) if pinned is not None else editor_target(pkg, step_id)
    except SceneError as exc:
        raise ValueError(str(exc)) from exc
    scene_id = str(target["scene_id"])
    label = str(target["label"])
    if duration is None:
        sent = clip_send_seconds(int(target["duration"]))
        exact = False
    else:
        try:
            sent = int(duration)
        except (TypeError, ValueError):
            sent = 0
        if sent not in clip_duration_choices():
            raise ValueError("Pick a length from 1 to 15 seconds")
        exact = True
    root = pkg.dir()
    hist = root / "history"
    hist.mkdir(exist_ok=True)
    tmp_id = dest_scene_id or scene_id
    tmp = hist / f".edit-{tmp_id}.mp4"
    tmp.unlink(missing_ok=True)
    hold = bool(dest_scene_id)
    if not hold:
        graph.mark_running(pkg, step_id)
    await _emit(progress, f"{label}…")
    frame_pins = ""
    pin_paths: list[Path] = []
    if pinned is not None:
        for key in ("first", "last"):
            path = target.get(key)
            if isinstance(path, Path) and ".pin-" in path.name:
                pin_paths.append(path)
    try:
        if kind == "nudge":
            await _emit(progress, f"Nudging {label}…")
            if GENERATE_BACKEND == "stub":
                await asyncio.to_thread(
                    _make_placeholder_video, tmp, None, 2.0
                )
            else:
                from app.pipeline.video_gen import edit_video

                await edit_video(
                    prompt=prompt,
                    video_path=target["path"],
                    dest=tmp,
                    progress=progress,
                )
        elif kind == "new-scene":
            last = target.get("last") if pinned is not None else None
            if pinned is None and start_frame:
                info = await asyncio.to_thread(
                    capture_editor_pins, pkg, step_id, which="last"
                )
                last = info["last"]
                scene_id = str(info["scene_id"])
                target = info
                label = str(target["label"])
            await _emit(progress, f"New scene after {label}…")
            await _animate_from_keyframe(
                prompt=prompt,
                keyframe=last if start_frame else None,
                dest=tmp,
                duration=sent,
                aspect_ratio=pkg.aspect_ratio,
                backend=GENERATE_BACKEND,
                progress=progress,
                label=label,
                voice_ids=_voice_ids_for(pkg, step_id),
                exact_duration=exact,
                resolution=_editor_resolution(pkg, step_id),
            )
        else:
            frame_pins = (
                pins if pins in {"first", "last", "both"} else "both"
            ) if kind == "redo-frames" else ""
            if pinned is None and frame_pins:
                info = await asyncio.to_thread(
                    capture_editor_pins, pkg, step_id, which=frame_pins
                )
                scene_id = str(info["scene_id"])
                target = info
                label = str(target["label"])
            await _emit(progress, f"Regenerate · {label}…")
            await _animate_from_keyframe(
                prompt=prompt,
                keyframe=target.get("first") if frame_pins in {"first", "both"} else None,
                dest=tmp,
                duration=sent,
                aspect_ratio=pkg.aspect_ratio,
                backend=GENERATE_BACKEND,
                progress=progress,
                label=label,
                last_frame=target.get("last") if frame_pins in {"last", "both"} else None,
                voice_ids=_voice_ids_for(pkg, step_id),
                exact_duration=exact,
                resolution=_editor_resolution(pkg, step_id),
            )
        if not tmp.is_file() or tmp.stat().st_size < 1000:
            raise RuntimeError("That produced an empty clip")
        if kind == "new-scene":
            mode = (
                f"new scene · after {label.lower()} · "
                + ("last frame" if start_frame else "no frames")
                + f" · {sent}s"
            )
            note = "Added on the timeline after this clip."
        elif kind == "nudge":
            mode = f"nudge · {label.lower()}"
            note = "On the timeline, after this clip."
        elif kind == "redo-frames":
            if frame_pins == "first":
                pin_mode = "first frame"
            elif frame_pins == "last":
                pin_mode = "last frame"
            else:
                pin_mode = "first frame → last frame"
            mode = f"redo · {label.lower()} · {pin_mode} · {sent}s"
            note = "On the timeline, after this clip."
        else:
            mode = f"redo · {label.lower()} · no frames · {sent}s"
            note = "On the timeline, after this clip."
        if hold:
            options = {
                "last_sent_prompt": prompt,
                "last_sent_mode": mode,
            }
            if kind == "nudge":
                options["last_nudge"] = prompt
            elif kind != "new-scene":
                options["last_fresh"] = prompt
            placed = await asyncio.to_thread(
                fulfill_pending_scene,
                pkg,
                step_id,
                dest_scene_id,
                tmp,
                options,
            )
            return {
                "timeline_changed": bool(placed),
                "file": "",
                "scene_id": dest_scene_id,
                "note": note if placed else "That clip was removed.",
                "mode": mode,
            }
        await asyncio.to_thread(append_generated_scene, pkg, step_id, tmp)
        pkg = _reload(pkg)
        st = pkg.step(step_id)
        st.error = None
        st.options.pop("shown_take", None)
        if kind == "nudge":
            st.options["last_nudge"] = prompt
        elif kind != "new-scene":
            st.options["last_fresh"] = prompt
        st.options["last_sent_prompt"] = prompt
        st.options["last_sent_mode"] = mode
        pkg.save()
        return {
            "timeline_changed": True,
            "file": "",
            "scene_id": scene_id,
            "note": note,
            "mode": mode,
        }
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        if hold:
            try:
                failed = _reload(pkg)
                await asyncio.to_thread(
                    drop_pending_scene, failed, step_id, dest_scene_id
                )
            except Exception:  # noqa: BLE001
                pass
            raise
        try:
            failed = _reload(pkg)
        except Exception:  # noqa: BLE001
            failed = pkg
        graph.mark_error(failed, step_id, str(exc))
        raise
    finally:
        for path in pin_paths:
            path.unlink(missing_ok=True)


async def tweak_still(
    pkg: RallyPackage,
    step_id: str,
    delta: str,
    progress: ProgressCb = None,
) -> Path:
    """Image-edit the current still. Delta only — keep the rest."""
    import shutil

    from app.pipeline.explicit_mark import (
        finish_preview_generate,
        preview_source_path,
    )
    from app.pipeline.prompts import staged_still_name
    from app.pipeline.start_still import archive_still_file

    delta = (delta or "").strip()
    if not delta:
        raise ValueError("Say the one thing to change")

    st = pkg.step(step_id)
    if step_id == "Preview":
        dest = pkg.path_for("Preview.png")
        # Same file the stage shows, unless the E badge is on — then the
        # unbadged clean still, which is that photo without the letter.
        src = preview_source_path(pkg)
    elif step_id == "Idle":
        dest = pkg.path_for("IdleKeyframe.png")
        src = dest if dest.is_file() else preview_source_path(pkg)
    else:
        dest = pkg.path_for(staged_still_name(step_id))
        src = dest if dest.is_file() else preview_source_path(pkg)
    if not src.is_file():
        raise ValueError("No still to tweak — make one first")

    prompt = (
        "The provided image is the source still. "
        "Keep everything that is not mentioned.\n"
        f"Apply only this change:\n{delta}\n\n"
        "Same identity, outfit, lighting, and framing except where "
        "the change requires it. Single still. No captions, logos, "
        "or extra people."
    )
    seed = dest.with_name(dest.stem + ".tweak-seed.png")
    tmp = dest.with_name(dest.stem + ".tweak-tmp.png")
    shutil.copy2(src, seed)
    tmp.unlink(missing_ok=True)
    graph.mark_running(pkg, step_id)
    await _emit(progress, f"Tweaking {dest.name}…")
    try:
        from app.pipeline import imagine

        await imagine.generate_image(
            prompt=prompt,
            dest=tmp,
            aspect_ratio=pkg.aspect_ratio,
            image_path=seed,
        )
        if not tmp.is_file() or tmp.stat().st_size < 100:
            raise RuntimeError("Tweak produced an empty still")
        if step_id == "Preview":
            pkg.archive_outputs(step_id)
        else:
            archive_still_file(pkg, dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), str(dest))
        st.error = None
        st.options.pop("shown_take", None)
        st.options["last_tweak"] = delta
        st.options["last_sent_prompt"] = prompt
        st.options["last_sent_mode"] = f"image-edit of {src.name}"
        st.options["work_surface"] = "still"
        if step_id == "Preview":
            try:
                await _emit(progress, finish_preview_generate(pkg))
            except Exception as mark_exc:  # noqa: BLE001
                await _emit(progress, f"Tweak saved; E overlay failed: {mark_exc}")
        graph.mark_needs_review(
            pkg, step_id, st.composed_prompt or prompt, warning=None
        )
        await _emit(progress, f"{dest.name} tweaked")
        return dest
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        graph.mark_error(pkg, step_id, str(exc))
        raise
    finally:
        seed.unlink(missing_ok=True)


async def generate_still(
    pkg: RallyPackage,
    step_id: str,
    progress: ProgressCb = None,
) -> Path:
    """Generate or replace the frame-1 still without running video.

    Preview: same as generate_step (the step *is* a still).
    Idle: rebuilds IdleKeyframe.png from Preview.
    Video clips: image-edits the chosen seed into {Step}.start.png.
    """
    ok, reason = graph.can_generate(pkg, step_id)
    if not ok:
        raise ValueError(reason)

    st = pkg.step(step_id)
    backend = GENERATE_BACKEND
    await _emit(progress, f"Generating still for {clip_label(step_id)} ({backend})…")

    if step_id == "Preview":
        await generate_step(pkg, step_id, progress=progress)
        return pkg.path_for("Preview.png")

    if step_id == "Idle":
        dest = await generate_start_still(
            pkg,
            step_id,
            backend=backend,
            progress=progress,
            emit=_emit,
        )
        await _emit(progress, f"Idle keyframe ready → {dest.name}")
        return dest

    dest = await generate_start_still(
        pkg,
        step_id,
        backend=backend,
        progress=progress,
        emit=_emit,
    )
    await _emit(progress, f"Frame 1 ready → {dest.name}")
    return dest


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
    still_prompt = compile_formula(pkg, "Idle", "still")
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


def _look_ref_paths(pkg: RallyPackage, step_id: str) -> list[Path]:
    """Stills sent as Imagine reference_images — not the opening frame."""
    from app.pipeline.explicit_mark import preview_source_path

    raw = pkg.step(step_id).options.get("look_refs")
    if not isinstance(raw, list):
        raw = (getattr(pkg, "kit", None) or {}).get("look_refs") or []
    out: list[Path] = []
    seen: set[str] = set()
    for name in raw:
        key = str(name or "").strip()
        if not key:
            continue
        if key.lower() in {"preview", "preview.png", "preview.clean.png"}:
            path = preview_source_path(pkg)
        else:
            path = pkg.path_for(key)
        if not path.is_file():
            continue
        try:
            ident = str(path.resolve())
        except OSError:
            ident = str(path)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(path)
        if len(out) >= 7:
            break
    return out


def _prompt_with_look_refs(prompt: str, refs: list[Path]) -> str:
    if not refs:
        return prompt
    if "<IMAGE_" in (prompt or ""):
        return prompt
    tags = ", ".join(f"<IMAGE_{i}>" for i in range(len(refs)))
    extra = (
        f"Use {tags} for faces, bodies, clothing, and gear. "
        "Invent a new pose and camera."
    )
    return f"{prompt.rstrip()}\n\n{extra}"


def _video_output_name(pkg: RallyPackage, step_id: str) -> str:
    """Always the mp4 slot — never a start still that was prepended to outputs."""
    st = pkg.step(step_id)
    for name in st.outputs:
        if name.lower().endswith((".mp4", ".webm", ".mov")):
            return name
    return clip_video_name(step_id)


def clip_duration_choices() -> list[int]:
    """Lengths the current video model will generate, one second at a time."""
    return imagine_settings.take_duration_choices()


def clip_duration_choice(seconds: int | float | None, *, default: int = 6) -> int:
    """Nearest length the form can select. Out of range falls back to default."""
    choices = clip_duration_choices()
    try:
        dur = int(round(float(seconds)))
    except (TypeError, ValueError):
        dur = default
    if dur in choices:
        return dur
    return default if default in choices else choices[0]


def clip_send_seconds(
    duration: int | float,
    *,
    reference: bool = False,
) -> int:
    """Seconds Imagine is asked for. Always one of the seconds it accepts."""
    choices = imagine_settings.take_duration_choices(reference=reference)
    try:
        dur = int(duration)
    except (TypeError, ValueError):
        dur = 6
    picked = imagine_settings.nearest_choice(dur, choices, 6)
    return int(picked)


def clip_length_note(
    seconds: float,
    *,
    kind: str,
    pins: str = "both",
    choose: bool = False,
) -> str:
    """What the editor dialog says about length.

    ``choose`` is the regenerate / new-scene form, where the length control
    lists every available second. The form adds “Asked for Ns.”
    """
    try:
        asked = int(round(float(seconds)))
    except (TypeError, ValueError):
        asked = 6
    asked = max(1, min(15, asked))
    sent = asked if choose else clip_send_seconds(asked)
    snap = ""
    if not choose and sent != asked:
        snap = f" This request can only be {sent}s, so that is what is sent."
    shown = f"{float(seconds):.1f}s"
    if kind == "new-scene":
        if choose:
            return (
                "Adds a clip after this one. It starts on this clip’s last "
                "frame unless you turn that off."
            )
        return (
            f"Adds a clip after this one. This clip is {shown}, so the new "
            f"clip is asked for {sent}s.{snap} It starts on this clip’s last "
            "frame unless you turn that off."
        )
    if kind == "redo-plain":
        if choose:
            return (
                f"This clip is {shown}. The new take does not pin a first or "
                "last frame. It is added on the timeline after this clip."
            )
        return (
            f"This clip is {shown}. The new take is asked for {sent}s with "
            f"no first or last frame.{snap}"
        )
    if kind == "nudge":
        return "Changes this clip only. The cut stays until you Use on clip."
    pins = pins if pins in {"first", "last", "both"} else "both"
    if pins == "first":
        how = "starts on the first frame and does not pin the last"
    elif pins == "last":
        how = "ends on the last frame and does not pin the first"
    else:
        how = "starts on the first frame and ends on the last"
    if choose:
        return (
            f"This clip is {shown}. The new take {how}. It is added on the "
            "timeline after this clip."
        )
    return (
        f"This clip is {shown}. The new take {how}, and is asked for {sent}s."
        f"{snap}"
    )


def _editor_resolution(pkg: RallyPackage, step_id: str) -> str:
    """Quality a clip redo uses: the take's pick, capped like that shot."""
    from app.pipeline.start_still import frame1_type

    reference = frame1_type(pkg, step_id, "take") == "look"
    return step_video_resolution(pkg, step_id, reference=reference)


def step_video_resolution(
    pkg: RallyPackage,
    step_id: str,
    *,
    reference: bool = False,
) -> str:
    """Quality this take will ask for. Step pick, else the gear default."""
    choices = imagine_settings.take_resolution_choices(reference=reference)
    raw = str(pkg.step(step_id).options.get("video_resolution") or "").strip()
    fallback = imagine_settings.video_resolution()
    if raw in choices:
        return raw
    if fallback in choices:
        return fallback
    return choices[-1]


def _step_duration(pkg: RallyPackage, step_id: str) -> int:
    """Per-step clip length, falling back to Imagine defaults."""
    raw = pkg.step(step_id).options.get("duration")
    if raw is not None and str(raw).strip() != "":
        try:
            return max(1, min(15, int(raw)))
        except (TypeError, ValueError):
            pass
    return (
        imagine_settings.video_duration()
        or pkg.duration_default
        or VIDEO_DURATION_DEFAULT
    )


async def _animate_from_keyframe(
    *,
    prompt: str,
    keyframe: Path | None,
    dest: Path,
    duration: int | float,
    aspect_ratio: str,
    backend: str,
    progress: ProgressCb,
    label: str,
    last_frame: Path | None = None,
    voice_ids: list[str] | None = None,
    reference_paths: list[Path] | None = None,
    exact_duration: bool = False,
    resolution: str | None = None,
) -> None:
    """image→video, or reference-to-video when keyframe is omitted."""
    if backend == "stub":
        await asyncio.to_thread(_make_placeholder_video, dest, keyframe, 2.0)
        return

    if backend not in ("cli", "api"):
        raise ValueError(f"Unknown backend for video: {backend}")

    from app.pipeline.video_gen import generate_video_from_image

    reference = bool(reference_paths)
    dur = (
        clip_duration_choice(duration)
        if exact_duration
        else clip_send_seconds(duration, reference=reference)
    )
    if resolution:
        allowed = imagine_settings.take_resolution_choices(reference=reference)
        if resolution not in allowed:
            resolution = str(
                imagine_settings.nearest_choice(
                    resolution,
                    allowed,
                    allowed[-1],
                )
            )

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
        resolution=resolution,
        progress=on_progress,
        last_frame_path=last_frame,
        voice_ids=voice_ids,
        reference_paths=reference_paths,
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

    duration = _step_duration(pkg, "Idle")
    need_still = want_png or want_mp4
    still: Path | None = None
    if need_still:
        from app.pipeline.joins import (
            apply_message_last_to_idle,
            start_join,
            start_path,
        )

        sj = start_join(pkg, "Idle")
        if sj == "message-last":
            pinned = apply_message_last_to_idle(pkg)
            if pinned is None or not pinned.is_file():
                raise FileNotFoundError(
                    "Generate Message first — Idle is set to open on "
                    "Message’s last frame"
                )
            still = pinned
            await _emit(progress, f"Idle frame 1 ← Message last ({still.name})")
        else:
            rest = start_path(pkg, "Idle")
            dest_kf = pkg.path_for("IdleKeyframe.png")
            if rest is not None and rest.is_file():
                if rest.resolve() != dest_kf.resolve():
                    import shutil

                    shutil.copy2(rest, dest_kf)
                still = dest_kf
                await _emit(progress, f"Idle rest from {rest.name}")
            else:
                still = await _idle_still(
                    pkg, composed, backend=backend, progress=progress
                )
                await _emit(progress, f"Keyframe ready → {still.name}")

    st.options.pop("video_zdr_blocked", None)
    st.options.pop("video_cli_handoff", None)

    if want_mp4:
        assert still is not None
        from app.pipeline.scenes import has_timeline, restitch, write_target

        scene_write = has_timeline(pkg, "Idle")
        video = (
            write_target(pkg, "Idle")
            if scene_write
            else pkg.path_for("Idle.mp4")
        )
        idle_duration = int(duration or 6)
        await _emit(
            progress,
            f"Animating Idle.mp4 from keyframe ({backend}, ~{idle_duration}s)… "
            "this can take several minutes",
        )
        pin_loop = bool(st.options.get("imagine_loop"))
        from app.pipeline.start_still import active_look_refs

        refs = active_look_refs(pkg, "Idle", "take")
        refs = [
            p
            for p in refs
            if still is None or p.resolve() != still.resolve()
        ]
        sent = _prompt_with_look_refs(composed, refs)
        st.options["last_sent_prompt"] = sent
        st.options["last_sent_mode"] = (
            f"image-to-video from {still.name}"
            + (" · Imagine loop pin" if pin_loop else "")
            + (f" · look refs {', '.join(p.name for p in refs)}" if refs else "")
        )
        pkg.save()
        await _animate_from_keyframe(
            prompt=sent,
            keyframe=still,
            dest=video,
            duration=idle_duration,
            aspect_ratio=pkg.aspect_ratio,
            backend=backend,
            progress=progress,
            label="Idle.mp4",
            last_frame=still if pin_loop else None,
            voice_ids=_voice_ids_for(pkg, "Idle"),
            reference_paths=refs,
            resolution=step_video_resolution(
                pkg, "Idle", reference=bool(refs)
            ),
        )

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
        if scene_write:
            await asyncio.to_thread(restitch, pkg, "Idle")
            video = pkg.path_for("Idle.mp4")
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
    """Direct Imagine path (no Grok CLI agent / Terminal windows)."""
    from app.pipeline import imagine
    from app.pipeline.auth_token import resolve_bearer_token

    st = pkg.step(step_id)
    aspect = pkg.aspect_ratio
    duration = _step_duration(pkg, step_id)
    _token, auth_src = resolve_bearer_token()
    await _emit(progress, f"Imagine auth: {auth_src} (direct API, no CLI agent)")

    if st.kind == StepKind.IMAGE:
        out = pkg.path_for(st.outputs[0])
        prompt = composed
        seed = pkg.dir() / _PREVIEW_POSE_SEED
        image_path = seed if seed.is_file() else None
        if image_path is None:
            ref = pkg.path_for("Preview.reference.png")
            if st.options.get("use_reference") and ref.is_file():
                image_path = ref
        from app.pipeline.start_still import active_look_refs

        refs = [
            p
            for p in active_look_refs(pkg, step_id, "still")
            if p.is_file()
            and (
                image_path is None
                or p.resolve() != image_path.resolve()
            )
        ]
        if image_path is not None:
            mode = f"image-edit from {image_path.name}"
            await _emit(
                progress,
                f"Imagine image-edit from {image_path.name} "
                f"({image_path.stat().st_size // 1024} KB)…",
            )
        elif refs:
            mode = "look-refs (no pose seed)"
            await _emit(progress, "Imagine image from look refs…")
        else:
            mode = "text-to-image"
            await _emit(progress, "Imagine image (text-to-image)…")
        if refs:
            mode += " + refs " + ", ".join(p.name for p in refs)
        st.options["last_sent_prompt"] = prompt
        st.options["last_sent_mode"] = mode
        pkg.save()
        await imagine.generate_image(
            prompt=prompt,
            dest=out,
            aspect_ratio=aspect,
            image_path=image_path,
            reference_paths=refs or None,
        )
        return

    if st.kind == StepKind.IDLE:
        await _generate_idle(pkg, composed, backend=backend, progress=progress)
        return

    if st.kind == StepKind.VIDEO:
        from app.pipeline.joins import (
            end_join,
            end_path,
            mint_idle_rest_from_message,
            start_join,
            start_path,
            take_is_unique,
        )
        from app.pipeline.frames import FrameError, extract_last_frame

        video_name = _video_output_name(pkg, step_id)
        video = pkg.path_for(video_name)
        from app.pipeline.scenes import (
            has_timeline,
            previous_last_frame,
            restitch,
            scene_index,
            write_target,
        )

        scene_write = has_timeline(pkg, step_id)
        if scene_write:
            video = write_target(pkg, step_id)
            video_name = f"scene {scene_index(pkg, step_id) + 1}"
        from app.pipeline.start_still import active_look_refs

        refs = active_look_refs(pkg, step_id, "take")
        sj = start_join(pkg, step_id)
        from app.pipeline.prompts import staged_still_name as _staged_name

        staged = pkg.path_for(
            "IdleKeyframe.png"
            if step_id == "Idle"
            else _staged_name(step_id)
        )
        if take_is_unique(pkg, step_id) or sj == "cut":
            keyframe = None
        elif sj == "still":
            keyframe = (
                staged
                if staged.is_file()
                else start_path(pkg, step_id)
            )
            if keyframe is None or not keyframe.is_file():
                raise FileNotFoundError(
                    f"Make still first — {clip_label(step_id)} is set to open on this still"
                )
        else:
            keyframe = start_path(pkg, step_id)
            if keyframe is None or not keyframe.is_file():
                keyframe = await ensure_video_start_still(
                    pkg,
                    step_id,
                    backend=backend,
                    progress=progress,
                    emit=_emit,
                )
            if keyframe is None or not keyframe.is_file():
                raise FileNotFoundError(
                    f"Need a start still for {clip_label(step_id)}, or Unique shot"
                )
        if scene_write and scene_index(pkg, step_id) > 0:
            from app.pipeline.start_still import frame1_type

            if frame1_type(pkg, step_id, "take") == "start":
                prev = previous_last_frame(pkg, step_id)
                if prev is None or not prev.is_file():
                    raise FileNotFoundError(
                        "Could not read the previous scene’s last frame"
                    )
                keyframe = prev
        if keyframe is not None:
            refs = [
                p
                for p in refs
                if p.resolve() != keyframe.resolve()
            ]
        last = end_path(pkg, step_id)
        voices = _voice_ids_for(pkg, step_id)
        sent = _prompt_with_look_refs(composed, refs)
        mode = (
            f"image-to-video from {keyframe.name}"
            if keyframe is not None
            else "reference-to-video (no frame-1 pin)"
        )
        if refs:
            mode += " · look refs " + ", ".join(p.name for p in refs)
        if last is not None and last.is_file():
            mode += f" → {last.name}"
        reference = bool(refs)
        sent_dur = clip_send_seconds(duration, reference=reference)
        resolution = step_video_resolution(
            pkg, step_id, reference=reference
        )
        await _emit(
            progress,
            f"Animating {video_name} · {sent_dur}s · {resolution} · {mode}…",
        )
        st.options["last_sent_prompt"] = sent
        st.options["last_sent_mode"] = mode
        pkg.save()
        await _animate_from_keyframe(
            prompt=sent,
            keyframe=keyframe,
            dest=video,
            duration=sent_dur,
            aspect_ratio=aspect,
            backend=backend,
            progress=progress,
            label=video_name,
            last_frame=last if last is not None and last.is_file() else None,
            voice_ids=voices,
            reference_paths=refs,
            exact_duration=True,
            resolution=resolution,
        )
        if scene_write:
            await asyncio.to_thread(restitch, pkg, step_id)
            video = pkg.path_for(_video_output_name(pkg, step_id))
        if step_id == "Message" and video.is_file():
            dest_last = pkg.path_for("Message.last.png")
            try:
                await asyncio.to_thread(extract_last_frame, video, dest_last)
                await _emit(progress, "Message.last.png extracted")
            except FrameError as exc:
                await _emit(progress, f"Could not extract Message last: {exc}")
            if end_join(pkg, "Message") == "idle-rest":
                from app.pipeline.joins import apply_message_last_to_idle

                minted = apply_message_last_to_idle(pkg)
                if minted is not None:
                    await _emit(
                        progress,
                        f"Idle will open on Message last → {minted.name}",
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
        from app.pipeline.scenes import has_timeline, restitch, write_target

        scene_write = has_timeline(pkg, step_id)
        video_name = _video_output_name(pkg, step_id)
        video = (
            write_target(pkg, step_id)
            if scene_write
            else pkg.path_for(video_name)
        )
        keyframe = await ensure_video_start_still(
            pkg,
            step_id,
            backend="stub",
            progress=progress,
            emit=_emit,
        )
        if not keyframe.is_file():
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
        if scene_write:
            await asyncio.to_thread(restitch, pkg, step_id)
        return

    raise ValueError(f"Unsupported step kind: {st.kind}")
