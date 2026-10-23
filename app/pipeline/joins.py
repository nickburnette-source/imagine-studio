"""Clip in/out joins — continuity without forcing every clip onto Idle."""

from __future__ import annotations

from pathlib import Path

from app.models.rally import RallyPackage

JOIN_START: dict[str, list[str]] = {
    "Rally": ["preview", "still", "cut"],
    "Message": ["preview", "still", "cut", "idle-rest"],
    "Idle": ["message-last", "idle-rest"],
    "Accept": ["idle-last", "idle-rest", "still", "cut", "preview"],
    "Decline": ["idle-last", "idle-rest", "still", "cut", "preview"],
    "Adjust": ["idle-last", "idle-rest", "still", "cut", "preview"],
}
JOIN_END: dict[str, list[str]] = {
    "Rally": ["cut", "idle-rest", "still", "loop"],
    "Message": ["idle-rest", "cut", "still"],
    "Idle": ["loop", "cut"],
    "Accept": ["idle-rest", "cut", "still"],
    "Decline": ["idle-rest", "cut", "still"],
    "Adjust": ["idle-rest", "cut", "still"],
}
JOIN_START_DEFAULT: dict[str, str] = {
    "Rally": "preview",
    "Message": "preview",
    "Idle": "idle-rest",
    "Accept": "idle-last",
    "Decline": "idle-last",
    "Adjust": "idle-last",
}
JOIN_END_DEFAULT: dict[str, str] = {
    "Rally": "cut",
    "Message": "idle-rest",
    "Idle": "loop",
    "Accept": "idle-rest",
    "Decline": "idle-rest",
    "Adjust": "idle-rest",
}

_LABELS = {
    ("in", "still"): (
        "Opens on this still",
        "Frame 1 = the still you staged.",
    ),
    ("in", "cut"): (
        "Unique shot",
        "No frame 1 pin. Prompt opens a new shot.",
    ),
    ("in", "preview"): ("Opens on Preview", "Look lock is frame 1."),
    ("in", "idle-rest"): ("Opens on Idle rest", "Same pose Idle loops on."),
    ("in", "idle-last"): ("Opens on Idle last", "Replies usually open here."),
    ("in", "message-last"): (
        "Opens on Message last",
        "Frame 1 = last frame of Message.",
    ),
    ("out", "cut"): ("Unique end", "Does not have to land on Idle."),
    ("out", "idle-rest"): (
        "Lands on Idle rest",
        "Last frame = Idle’s first.",
    ),
    ("out", "loop"): ("Loops · first = last", "Idle is a closed loop."),
}


def staged_still_path(pkg: RallyPackage, step_id: str) -> Path:
    from app.models.rally import opening_still_name

    return pkg.path_for(opening_still_name(step_id))


def start_join(pkg: RallyPackage, step_id: str) -> str:
    st = pkg.step(step_id)
    raw = str(st.options.get("start_join") or "").strip()
    opts = JOIN_START.get(step_id) or ["preview"]
    if raw in opts:
        return raw
    return JOIN_START_DEFAULT.get(step_id, "preview")


def take_is_unique(pkg: RallyPackage, step_id: str) -> bool:
    """True when the take must not send a frame-1 image (reference-to-video)."""
    st = pkg.step(step_id)
    if bool(st.options.get("unique_shot")):
        return True
    return str(st.options.get("start_join") or "").strip().lower() == "cut"


def end_join(pkg: RallyPackage, step_id: str) -> str:
    st = pkg.step(step_id)
    raw = str(st.options.get("end_join") or "").strip()
    opts = JOIN_END.get(step_id) or ["cut"]
    if raw in opts or raw in {"cut", "still", "loop", "idle-rest"}:
        return raw
    return JOIN_END_DEFAULT.get(step_id, "cut")


def join_label(kind: str, key: str) -> tuple[str, str]:
    return _LABELS.get((kind, key), (key, ""))


def idle_rest_path(pkg: RallyPackage) -> Path | None:
    for name in ("IdleKeyframe.png", "Message.last.png"):
        p = pkg.path_for(name)
        if p.is_file():
            return p
    return None


def idle_last_path(pkg: RallyPackage) -> Path | None:
    last = pkg.path_for("Idle.last.png")
    if last.is_file():
        return last
    return idle_rest_path(pkg)


def start_path(pkg: RallyPackage, step_id: str) -> Path | None:
    from app.pipeline.explicit_mark import as_imagine_still, preview_source_path

    if step_id == "Preview":
        p = pkg.path_for("Preview.png")
        return p if p.is_file() else None
    key = start_join(pkg, step_id)
    custom = str(pkg.step(step_id).options.get("start_file") or "").strip()
    if key == "still":
        if custom:
            p = pkg.path_for(custom)
            if p.is_file():
                remapped = as_imagine_still(pkg, p)
                return remapped if remapped is not None else p
        staged = staged_still_path(pkg, step_id)
        return staged if staged.is_file() else None
    if key == "preview":
        p = preview_source_path(pkg)
        return p if p.is_file() else None
    if key == "idle-rest":
        return idle_rest_path(pkg)
    if key == "idle-last":
        return idle_last_path(pkg)
    if key == "message-last":
        last = pkg.path_for("Message.last.png")
        return last if last.is_file() else None
    if key == "cut":
        return None
    return None


def end_path(pkg: RallyPackage, step_id: str) -> Path | None:
    key = end_join(pkg, step_id)
    custom = str(pkg.step(step_id).options.get("end_file") or "").strip()
    if key == "cut":
        return None
    if key == "loop":
        return start_path(pkg, step_id)
    if custom:
        p = pkg.path_for(custom)
        if p.is_file():
            from app.pipeline.explicit_mark import as_imagine_still

            remapped = as_imagine_still(pkg, p)
            return remapped if remapped is not None else p
    if key == "idle-rest":
        rest = idle_rest_path(pkg)
        if rest is not None:
            return rest
        if step_id == "Message":
            last = pkg.path_for("Message.last.png")
            return last if last.is_file() else None
        return None
    return None


def cycle_join(pkg: RallyPackage, step_id: str, which: str) -> str:
    opts = JOIN_START[step_id] if which == "in" else JOIN_END[step_id]
    cur = start_join(pkg, step_id) if which == "in" else end_join(pkg, step_id)
    nxt = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
    st = pkg.step(step_id)
    if which == "in":
        st.options["start_join"] = nxt
        if nxt == "cut":
            st.options["unique_shot"] = True
            st.options.pop("start_file", None)
        else:
            st.options.pop("unique_shot", None)
            if nxt != "still":
                st.options.pop("start_file", None)
    else:
        st.options["end_join"] = nxt
    pkg.save()
    return nxt


def take_formula(
    pkg: RallyPackage, step_id: str
) -> list[tuple[str, str, str]]:
    """Recipe for the next take: (label, summary, detail)."""
    st = pkg.step(step_id)
    sj = start_join(pkg, step_id)
    sp = start_path(pkg, step_id)
    if sj == "cut":
        frame = "Unique shot — no frame 1 pin"
        frame_d = (
            "Imagine text-to-video (or reference-to-video if look refs are "
            "on). The opening is invented from the prompt, not from a still. "
            "Cycle the left join chip if you meant to start on a still."
        )
    elif sj == "still":
        if sp is not None and sp.is_file():
            frame = f"This clip’s still · {sp.name}"
            frame_d = (
                "Imagine image-to-video. The take begins on the still you "
                "staged for this clip (Make still / Tweak). That pose is "
                "frame 1; the prompt describes what happens next."
            )
        else:
            frame = "This clip’s still — missing"
            frame_d = (
                "Join is “Opens on this still” but there is no staged still "
                "on disk. Switch to Still and Make still, or cycle the join "
                "to Unique shot / Preview."
            )
    elif sj == "preview":
        frame = "Preview (look lock)"
        frame_d = (
            "Imagine image-to-video from the approved Preview. The look lock "
            "is frame 1 of this take. Cycle the join to “Opens on this still” "
            "to use the clip still you staged instead."
        )
    elif sj == "idle-rest":
        if sp is not None and sp.is_file():
            frame = f"Idle rest · {sp.name}"
            frame_d = (
                "Frame 1 is the Idle rest pose (the loop’s first/last still). "
                "The take should open already sitting on that rest."
            )
        else:
            frame = "Idle rest — missing"
            frame_d = (
                "No Idle rest still yet. Generate Idle, or approve Message "
                "with “Lands on Idle rest” to mint one."
            )
    elif sj == "message-last":
        if sp is not None and sp.is_file():
            frame = f"Message last · {sp.name}"
            frame_d = (
                "Idle opens on the last frame of Message so the loop "
                "continues the same vantage. Captured from Message.mp4 "
                "after that take finishes."
            )
        else:
            frame = "Message last — missing"
            frame_d = (
                "Generate a Message take first. The last frame is captured "
                "automatically, then Idle can open on it."
            )
    elif sj == "idle-last":
        if sp is not None and sp.is_file():
            frame = f"Idle last · {sp.name}"
            frame_d = (
                "Frame 1 is Idle’s last frame so replies match-cut off the "
                "loop. Replies usually open here."
            )
        else:
            frame = "Idle last — missing"
            frame_d = "Need an Idle take (or rest still) before this join works."
    else:
        frame, frame_d = sj, ""
    rows: list[tuple[str, str, str]] = [("Frame 1", frame, frame_d)]

    raw_refs = st.options.get("look_refs")
    ref_names: list[str] = []
    if isinstance(raw_refs, list):
        for x in raw_refs:
            key = str(x or "").strip()
            if not key:
                continue
            if key.lower() in {"preview", "preview.png", "preview.clean.png"}:
                ref_names.append("Preview")
            else:
                ref_names.append(Path(key).name)
    if ref_names:
        rows.append(
            (
                "Look refs",
                ", ".join(ref_names),
                "Character/look only — not frame 1 unless the join above "
                "also pins that same still. Imagine is told to match these "
                "subjects while playing out the prompt.",
            )
        )
    else:
        rows.append(
            (
                "Look refs",
                "None",
                "No extra character stills. Identity comes from frame 1 "
                "(if pinned) and the prompt. Check “Preview still — look "
                "only” under Character ref to add the look lock without "
                "making it frame 1.",
            )
        )

    ej = end_join(pkg, step_id)
    ep = end_path(pkg, step_id)
    if ej == "loop":
        rows.append(
            (
                "Last frame",
                "Loop — last = first",
                "Idle is a closed loop. Imagine is asked to end on the "
                "same pose it started on (or boomerang is applied after).",
            )
        )
    elif ej == "idle-rest":
        if ep is not None and ep.is_file():
            rows.append(
                (
                    "Last frame",
                    f"Idle rest · {ep.name}",
                    "Pinned last frame so this take can land on Idle. "
                    "The rest pose is sent as last_frame.",
                )
            )
        else:
            rows.append(
                (
                    "Last frame",
                    "Idle rest — will mint on approve",
                    "No rest still yet. Approving Message with this join "
                    "copies Message’s last frame into Idle rest.",
                )
            )
    else:
        rows.append(
            (
                "Last frame",
                "Unique — no last-frame pin",
                "The take can end anywhere. It does not have to match Idle.",
            )
        )

    spk = str(st.options.get("speaker") or "").strip()
    rows.append(
        (
            "Voice",
            spk or "Invent",
            "Invent lets Imagine pick a voice from the line. A named "
            "cast chip pins that speaker for this take.",
        )
    )
    dur = st.options.get("duration")
    if dur not in (None, ""):
        rows.append(
            (
                "Duration",
                f"{dur}s",
                "Clip length sent to Imagine for this step only.",
            )
        )
    return rows


def ensure_message_last_frame(pkg: RallyPackage) -> Path | None:
    """Return Message.last.png, extracting from Message.mp4 if needed."""
    dest = pkg.path_for("Message.last.png")
    if dest.is_file() and dest.stat().st_size > 1000:
        return dest
    video = pkg.path_for("Message.mp4")
    if not video.is_file():
        return dest if dest.is_file() else None
    from app.pipeline.frames import extract_last_frame

    try:
        extract_last_frame(video, dest)
    except Exception:  # noqa: BLE001
        return dest if dest.is_file() else None
    return dest if dest.is_file() and dest.stat().st_size > 1000 else None


def apply_message_last_to_idle(pkg: RallyPackage) -> Path | None:
    """Point Idle at Message’s last frame and copy it to IdleKeyframe."""
    import shutil

    from app.pipeline.start_still import archive_still_file

    last = ensure_message_last_frame(pkg)
    if last is None:
        return None
    idle = pkg.step("Idle")
    idle.options["start_join"] = "message-last"
    idle.options.pop("unique_shot", None)
    idle.options["generate_png"] = True
    if not pkg.path_for("Idle.mp4").is_file():
        idle.options["generate_mp4"] = False
    dest = pkg.path_for("IdleKeyframe.png")
    try:
        same = dest.is_file() and dest.resolve() == last.resolve()
    except OSError:
        same = False
    if not same:
        if dest.is_file():
            archive_still_file(pkg, dest)
        shutil.copy2(last, dest)
    pkg.save()
    return dest


def mint_idle_rest_from_message(pkg: RallyPackage) -> Path | None:
    """If Message lands on Idle and Idle has no rest, copy Message.last → keyframe."""
    last = pkg.path_for("Message.last.png")
    dest = pkg.path_for("IdleKeyframe.png")
    if not last.is_file():
        return dest if dest.is_file() else None
    if dest.is_file() and dest.stat().st_size > 1000:
        return dest
    import shutil

    shutil.copy2(last, dest)
    return dest
