"""Gate state machine for the human-in-the-loop pipeline."""

from __future__ import annotations

import json
from pathlib import Path

from app.models.rally import (
    BRANCH_STEPS,
    GateState,
    RallyPackage,
    StepStatus,
)
from app.pipeline.prompts import (
    default_step_states,
    refresh_composed,
    refresh_package_composed,
)


def new_rally(
    *,
    name: str,
    concept: str,
    root,
    aspect_ratio: str = "9:16",
    mode: str = "human",
    template: str = "character",
) -> RallyPackage:
    from app.models.rally import slugify

    slug = slugify(name)
    tid = (template or "character").strip().lower()
    steps = default_step_states(tid)
    # One clip, not a pack. Keep the Rally video step — that is the editor.
    if "Rally" in steps:
        steps = {"Rally": steps["Rally"]}
        # Nothing to pin frame 1 to. The clip opens as its own shot.
        rally = steps["Rally"]
        rally.options["start_join"] = "cut"
        rally.options["end_join"] = "cut"
        rally.options["unique_shot"] = True
        rally.label = "Take"
        rally.role = ""
        rally.base_prompt = ""
        rally.custom_placeholder = "What happens in this shot"
        rally.outputs = ["take.mp4"]
    else:
        only = next(iter(steps))
        steps = {only: steps[only]}
    typed = (name or "").strip()
    pkg = RallyPackage(
        name=slug,
        display_name=typed if typed and typed != slug else slug.replace("-", " ").title(),
        concept=concept.strip(),
        mode=mode,
        template=tid,
        aspect_ratio=aspect_ratio,
        steps=steps,
        root=root,
    )
    # Every clip is usable from the start — no sequential Preview→Idle locks.
    pkg.gates = {
        "preview": GateState.OPEN,
        "idle": GateState.OPEN,
        "branch": GateState.OPEN,
    }
    refresh_package_composed(pkg)
    for st in pkg.steps.values():
        st.status = StepStatus.READY
    normalize_clip(pkg)
    return pkg


# User-facing media. The step id stays Rally so scene folders keep working.
_CANON_NAMES = (
    ("Rally.start.png", "start.png"),
    ("Rally.raw.mp4", "take.raw.mp4"),
    ("Rally.mp4", "take.mp4"),
)


def _dest_media_name(name: str) -> str | None:
    """Rally.start.png → start.png, Rally.mp4 / Rally.cut.* → take.*."""
    if name.startswith("Rally.start"):
        return name[len("Rally.") :]
    if name.startswith("Rally.") and name.lower().endswith(
        (".mp4", ".webm", ".mov")
    ):
        return "take." + name[len("Rally.") :]
    return None


def _rewrite_names(text: str, pairs: list[tuple[str, str]]) -> str:
    out = text
    for old, new in pairs:
        if old and old in out:
            out = out.replace(old, new)
    return out


def _map_exact_path(value: str, pairs: dict[str, str]) -> str:
    """Rename a stored path. Leave scenes/Rally and undo blob names alone."""
    norm = value.replace("\\", "/")
    if norm.startswith("scenes/"):
        return value
    base = Path(norm).name
    new = pairs.get(base)
    if not new or new == base:
        return value
    parent = Path(norm).parent.as_posix()
    if parent in {"", "."}:
        return new
    return f"{parent}/{new}"


def _patch_undo_manifests(root: Path, pairs: dict[str, str]) -> bool:
    undo = root / "history" / ".undo-Rally"
    if not undo.is_dir():
        return False
    changed = False
    for path in undo.glob("*/manifest.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        touched = False

        def patch(row: dict, key: str) -> None:
            nonlocal touched
            val = row.get(key)
            if not isinstance(val, str):
                return
            new = _map_exact_path(val, pairs)
            if new != val:
                row[key] = new
                touched = True

        for row in data.get("files") or []:
            if isinstance(row, dict):
                patch(row, "rel")
        for row in data.get("moves") or []:
            if isinstance(row, dict):
                patch(row, "src")
                patch(row, "dest")
        rel = data.get("rel")
        if isinstance(rel, str):
            new = _map_exact_path(rel, pairs)
            if new != rel:
                data["rel"] = new
                touched = True
        shown = data.get("shown_take")
        if isinstance(shown, str):
            new = _map_exact_path(shown, pairs)
            if new != shown:
                data["shown_take"] = new
                touched = True
        if touched:
            path.write_text(json.dumps(data), encoding="utf-8")
            changed = True
    return changed


def _rename_clip_files(root: Path) -> list[tuple[str, str]]:
    """Rename root and history media. Does not move scenes/Rally."""
    renamed: list[tuple[str, str]] = []

    def take(src: Path) -> None:
        if not src.is_file():
            return
        dest_name = _dest_media_name(src.name)
        if not dest_name or dest_name == src.name:
            return
        dest = src.with_name(dest_name)
        if dest.exists():
            return
        src.rename(dest)
        renamed.append((src.name, dest_name))

    for src in list(root.iterdir()):
        take(src)
    hist = root / "history"
    if hist.is_dir():
        for src in list(hist.iterdir()):
            take(src)
    fav = root / "favorites"
    if fav.is_dir():
        for src in list(fav.rglob("*")):
            take(src)
    return renamed


def normalize_clip(pkg: RallyPackage) -> bool:
    """Drop pack filenames and the stock send prompt from the one clip.

    Idempotent. Skips a step that is generating so a live take is not
    renamed out from under the writer. Does not touch the creative
    still prompt or scenes/Rally.
    """
    if "Rally" not in pkg.steps:
        return False
    if any(st.status == StepStatus.RUNNING for st in pkg.steps.values()):
        return False
    try:
        root = pkg.dir()
    except RuntimeError:
        return False

    from app.pipeline.formula import _as_shot

    changed = False
    renamed = _rename_clip_files(root)
    if renamed:
        changed = True
    pairs: list[tuple[str, str]] = list(renamed)
    for old, new in _CANON_NAMES:
        if (root / new).exists() or not (root / old).exists():
            pairs.append((old, new))
    # Longest first so Rally.start.png is not touched by a shorter token.
    dedup: dict[str, str] = {}
    for old, new in sorted(pairs, key=lambda item: len(item[0]), reverse=True):
        dedup.setdefault(old, new)
    pairs = list(dedup.items())
    exact = dict(pairs)
    if _patch_undo_manifests(root, exact):
        changed = True

    st = pkg.step("Rally")
    if (st.label or "").strip().lower() == "rally":
        st.label = "Take"
        changed = True
    role = _as_shot(st.role or "")
    if role != (st.role or ""):
        st.role = role
        changed = True
    if (st.custom_placeholder or "").strip().lower() in {"the send", "the send."}:
        st.custom_placeholder = "What happens in this shot"
        changed = True
    for attr in ("base_prompt", "composed_prompt", "custom_guidance"):
        old = getattr(st, attr) or ""
        new = _as_shot(old)
        if new != old:
            setattr(st, attr, new)
            changed = True
    for entry in st.history:
        for attr in ("base_prompt", "composed_prompt", "custom_guidance"):
            old = getattr(entry, attr) or ""
            new = _as_shot(old)
            if new != old:
                setattr(entry, attr, new)
                changed = True
        paths = list(entry.paths or [])
        rewritten = [_rewrite_names(p, pairs) for p in paths]
        if rewritten != paths:
            entry.paths = rewritten
            changed = True

    outputs = [_rewrite_names(name, pairs) for name in st.outputs]
    if "Rally" == st.id and not any(
        name.lower().endswith((".mp4", ".webm", ".mov")) for name in outputs
    ):
        outputs.insert(0, "take.mp4")
    if outputs != list(st.outputs):
        st.outputs = outputs
        changed = True

    def walk(value):
        if isinstance(value, str):
            new = _rewrite_names(value, pairs)
            return new
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    new_options = walk(dict(st.options))
    if new_options != st.options:
        st.options = new_options
        changed = True
    if changed:
        pkg.save()
    return changed


def unlock_all_steps(pkg: RallyPackage) -> bool:
    """Drop leftover sequential locks so every clip is generatable."""
    changed = False
    for st in pkg.steps.values():
        if st.status == StepStatus.LOCKED:
            st.status = (
                StepStatus.NEEDS_REVIEW
                if pkg.review_media(st.id)
                else StepStatus.READY
            )
            changed = True
    for key in ("preview", "idle", "branch"):
        if pkg.gates.get(key) == GateState.LOCKED:
            pkg.gates[key] = GateState.OPEN
            changed = True
    if changed:
        pkg.save()
    return changed


def can_generate(pkg: RallyPackage, step_id: str) -> tuple[bool, str]:
    st = pkg.step(step_id)
    if st.status == StepStatus.RUNNING:
        return False, "Step is already running"
    return True, ""


def can_approve(pkg: RallyPackage, step_id: str) -> tuple[bool, str]:
    st = pkg.step(step_id)
    if st.status == StepStatus.RUNNING:
        return False, "Wait for generation to finish"
    if st.status == StepStatus.APPROVED:
        return False, "Already approved"
    if not pkg.review_media(step_id):
        return False, "Add a still or take before approving"
    return True, ""


def approve_step(pkg: RallyPackage, step_id: str) -> None:
    ok, reason = can_approve(pkg, step_id)
    if not ok:
        raise ValueError(reason)

    st = pkg.step(step_id)
    st.status = StepStatus.APPROVED
    st.error = None

    if step_id == "Preview":
        from app.pipeline.explicit_mark import snapshot_clean_preview

        # Pin the approved pixels as the look lock other clips Edit-from.
        # When E is on, clean stays unstamped for Imagine; display is Preview.png.
        if not bool(st.options.get("explicit_e")):
            snapshot_clean_preview(pkg)
        pkg.gates["preview"] = GateState.APPROVED
        pkg.gates["idle"] = GateState.OPEN
        pkg.gates["branch"] = GateState.OPEN
        for sid in ("Idle", "Rally", "Message"):
            s = pkg.step(sid)
            if s.status == StepStatus.LOCKED:
                s.status = StepStatus.READY

    elif step_id == "Message":
        from app.pipeline.joins import apply_message_last_to_idle, end_join

        if end_join(pkg, "Message") == "idle-rest":
            apply_message_last_to_idle(pkg)

    elif step_id == "Idle":
        from app.pipeline.joins import apply_message_last_to_idle, start_join

        if start_join(pkg, "Idle") == "message-last" or (
            not pkg.path_for("IdleKeyframe.png").is_file()
            and pkg.path_for("Message.last.png").is_file()
        ):
            apply_message_last_to_idle(pkg)
        st.options["generate_png"] = True
        if not pkg.path_for("Idle.mp4").is_file():
            st.options["generate_mp4"] = False
        pkg.gates["idle"] = GateState.APPROVED
        pkg.gates["branch"] = GateState.OPEN
        for bid in ("Accept", "Decline", "Adjust"):
            b = pkg.step(bid)
            if b.status == StepStatus.LOCKED:
                b.status = StepStatus.READY

    elif step_id in BRANCH_STEPS:
        # Individual branch file approved; package may still be incomplete
        pass

    # Any step re-approval cycle resets final review / publish
    if not pkg.all_steps_approved():
        pkg.final_review_approved = False

    pkg.save()


def approve_final_review(pkg: RallyPackage) -> None:
    if not pkg.all_steps_approved():
        raise ValueError("Approve every pipeline step before final review")
    if not pkg.is_package_complete():
        raise ValueError("All deliverable files must exist on disk for final review")
    pkg.final_review_approved = True
    pkg.save()


def reset_final_review(pkg: RallyPackage) -> None:
    pkg.final_review_approved = False
    pkg.publish_tier = None
    pkg.save()


def mark_running(pkg: RallyPackage, step_id: str) -> None:
    st = pkg.step(step_id)
    st.status = StepStatus.RUNNING
    st.error = None
    pkg.save()


def mark_needs_review(
    pkg: RallyPackage,
    step_id: str,
    composed: str,
    *,
    warning: str | None = None,
) -> None:
    st = pkg.step(step_id)
    st.status = StepStatus.NEEDS_REVIEW
    # composed_prompt is the take/still shot store. last_sent_prompt holds
    # the full compile. Writing the compile here stacked the Frame 1 seed
    # line into the prompt box on every new take.
    from app.pipeline.formula import _as_shot

    shot = _as_shot(composed)
    if shot:
        st.composed_prompt = shot
    elif composed and not (st.composed_prompt or "").strip():
        st.composed_prompt = composed
    # warning stays visible in the UI without marking the step as ERROR
    st.error = warning
    if step_id == "Preview":
        pkg.gates["preview"] = GateState.NEEDS_REVIEW
    elif step_id == "Idle":
        pkg.gates["idle"] = GateState.NEEDS_REVIEW
    pkg.save()


def mark_error(pkg: RallyPackage, step_id: str, message: str) -> None:
    st = pkg.step(step_id)
    st.status = StepStatus.ERROR
    st.error = message
    pkg.save()


def invalidate_downstream_from_preview(pkg: RallyPackage) -> None:
    """Preview changed — it needs re-approval. Other clips stay usable."""
    pkg.gates["preview"] = GateState.OPEN
    if pkg.step("Preview").status != StepStatus.RUNNING:
        pkg.step("Preview").status = StepStatus.NEEDS_REVIEW
    pkg.final_review_approved = False
    pkg.publish_tier = None
    pkg.save()


def invalidate_branch_from_idle(pkg: RallyPackage) -> None:
    """Idle changed — it needs re-approval. Branch clips stay usable."""
    pkg.gates["idle"] = GateState.OPEN
    if pkg.step("Idle").status != StepStatus.RUNNING:
        pkg.step("Idle").status = StepStatus.NEEDS_REVIEW
    pkg.final_review_approved = False
    pkg.publish_tier = None
    pkg.save()


def status_emoji(status: StepStatus | str) -> str:
    s = status.value if isinstance(status, StepStatus) else status
    return {
        "locked": "🔒",
        "ready": "○",
        "running": "◐",
        "needs_review": "◉",
        "approved": "✓",
        "error": "✗",
    }.get(s, "·")
