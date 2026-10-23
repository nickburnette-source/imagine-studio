"""Imagine Studio editor."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from nicegui import app, ui

from app.config import OUTPUT_DIR
from app import imagine_settings
from app.models.rally import (
    FINAL_REVIEW_STEPS,
    PIPELINE_STEPS,
    GateState,
    RallyPackage,
    StepStatus,
    clip_label,
    delete_rally,
    file_fingerprint,
    list_rallies,
    slugify,
)
from app.pipeline import graph
from app.pipeline.assets import media_url, write_upload_bytes
from app.pipeline.compress import compress_rally_ios, fmt_bytes
from app.pipeline.doctor import run_doctor
from app.pipeline.generate import (
    generate_step,
    generate_still,
    nudge_step,
    tweak_still,
)
from app.pipeline.expand import (
    RallyBrief,
    apply_concept_pack,
    clear_brief_store,
    expand_concept,
    load_brief_store,
    parse_start_lock,
    persist_brief,
)
from app.pipeline.prompts import (
    STEP_ROLES,
    connection_label,
    default_step_states,
    refresh_composed,
    refresh_package_composed,
    staged_still_name,
)
from app.pipeline.upload import sanitize_display_name, upload_rally
from app.ui.scenes_ui import (
    open_scene_editor,
    register_routes,
    render_scene_bar,
    set_notice_hook,
    set_saved_hook,
)

# Session globals (single-user local app)
_current: RallyPackage | None = None
_brief: RallyBrief | None = None
_brief_open: bool = False
_selected_step: str = "Rally"
# Background generate jobs — keyed by "{rally}/{step}" so work survives
# leaving a rally and can be shown in a site-wide queue.
_gen_tasks: dict[str, asyncio.Task] = {}
_gen_detail: dict[str, str] = {}
# Force edit UI even when all steps approved (escape hatch from final review)
_force_edit: bool = False
# Fullscreen of the one take. Not a gate and not a publish step.
_view_final: bool = False

_MAX_NOTICES = 40


@dataclass
class _Notice:
    kind: str  # success | error | info
    title: str
    detail: str = ""
    rally: str | None = None
    step: str | None = None
    at: float = field(default_factory=time.time)
    unread: bool = True


# Newest-first activity feed (top-right panel)
_notices: list[_Notice] = []
# Last stage file that was allowed to autoplay. Settings refreshes reuse it.
_stage_played: dict[str, str] = {}


def _stage_should_autoplay(pkg: RallyPackage, step_id: str, primary: Path) -> bool:
    """Play only when the shown file changes, not on mute or other settings."""
    if _is_generating(pkg.name, step_id):
        return False
    try:
        stt = primary.stat()
        sig = f"{stt.st_mtime_ns}-{stt.st_size}"
    except OSError:
        sig = "missing"
    try:
        rel = str(primary.resolve().relative_to(pkg.dir().resolve()))
    except ValueError:
        rel = primary.name
    key = f"{pkg.name}\0{step_id}\0{rel}\0{sig}"
    same = key == _stage_played.get("key")
    _stage_played["key"] = key
    return not same


def _job_key(rally: str, step: str) -> str:
    return f"{rally}/{step}"


def _parse_job_key(key: str) -> tuple[str, str]:
    rally, _, step = key.partition("/")
    return rally, step


def _task_running(rally: str, step: str) -> bool:
    task = _gen_tasks.get(_job_key(rally, step))
    return task is not None and not task.done()


def _job_detail(key: str) -> str:
    text = _gen_detail.get(key) or ""
    if text:
        return text
    from app.pipeline.editor_jobs import detail_for

    return detail_for(key)


def _active_jobs() -> list[tuple[str, str, str]]:
    """Return [(key, rally, step), ...] for jobs still running."""
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for key, task in list(_gen_tasks.items()):
        if task.done():
            continue
        rally, step = _parse_job_key(key)
        out.append((key, rally, step))
        seen.add(key)
    from app.pipeline.editor_jobs import running_jobs

    for key, rally, step in running_jobs():
        if key not in seen:
            out.append((key, rally, step))
    return out


def _is_generating(rally: str, step: str) -> bool:
    if _task_running(rally, step):
        return True
    from app.pipeline.editor_jobs import local_busy

    return local_busy(rally, step)


def _sync_current_from_disk(rally: str, root: Path | None = None) -> None:
    """Reload _current from disk after a background job wrote meta/assets."""
    global _current
    if _current is None or _current.name != rally:
        return
    path = root if root is not None else _current.dir()
    try:
        _current = RallyPackage.load(path)
    except Exception:  # noqa: BLE001
        pass


def _push_notice(
    kind: str,
    title: str,
    *,
    detail: str = "",
    rally: str | None = None,
    step: str | None = None,
) -> None:
    """Record an activity item for the top-right panel (no toast)."""
    _notices.insert(
        0,
        _Notice(
            kind=kind,
            title=title,
            detail=detail,
            rally=rally,
            step=step,
        ),
    )
    del _notices[_MAX_NOTICES:]


def _step_label(step: str) -> str:
    return clip_label(step)


def _fmt_notice_age(at: float) -> str:
    secs = max(0, int(time.time() - at))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h ago"


def _el_alive(el: object | None) -> bool:
    """True if a NiceGUI element still exists (not cleared by refresh/navigate)."""
    if el is None:
        return False
    try:
        return not bool(getattr(el, "is_deleted", True))
    except Exception:  # noqa: BLE001
        return False


def _recover_stale_running(pkg: RallyPackage) -> list[str]:
    """Clear steps left as ``running`` after a crash/restart (no live job).

    ``mark_running`` persists to meta.json; in-memory tasks do not survive a
    server restart, so the UI would otherwise look forever-generating.
    """
    fixed: list[str] = []
    for sid, st in pkg.steps.items():
        if st.status != StepStatus.RUNNING:
            continue
        if _is_generating(pkg.name, sid):
            continue
        if pkg.existing_outputs(sid):
            graph.mark_needs_review(
                pkg,
                sid,
                st.composed_prompt or "",
                warning=(
                    "Recovered after interrupted generate "
                    "(no live job — review or regenerate)."
                ),
            )
        else:
            # Generation archives prior outputs first; if nothing is on disk,
            # leave the step actionable rather than fake-running.
            st.status = StepStatus.READY
            st.error = (
                "Generation interrupted before output was written "
                "(server restart or crash)."
            )
            pkg.save()
        fixed.append(sid)
    return fixed

_STEP_ICONS: dict[str, str] = {
    "Preview": "image",
    "Idle": "hourglass_empty",
    "Rally": "campaign",
    "Message": "notifications",
    "Accept": "check_circle",
    "Decline": "cancel",
    "Adjust": "schedule",
}

_STILL_MS = 2500  # how long to show stills in final review


def _pkg() -> RallyPackage:
    if _current is None:
        raise RuntimeError("No rally loaded")
    return _current


def _status_color(status: StepStatus) -> str:
    return {
        StepStatus.LOCKED: "grey",
        StepStatus.READY: "blue",
        StepStatus.RUNNING: "orange",
        StepStatus.NEEDS_REVIEW: "purple",
        StepStatus.APPROVED: "positive",
        StepStatus.ERROR: "negative",
    }.get(status, "grey")


def _sole_step(pkg: RallyPackage) -> str:
    if "Rally" in pkg.steps:
        return "Rally"
    return next(iter(pkg.steps))


def _studio_phase(pkg: RallyPackage) -> str:
    """edit | final. Approval does not change the screen."""
    _ = pkg
    if _view_final:
        return "final"
    return "edit"


def mount() -> None:
    """Register routes / build root page."""

    def _on_scenes_saved(name: str) -> None:
        _sync_current_from_disk(name)

    set_saved_hook(_on_scenes_saved)
    set_notice_hook(_push_notice)
    from app.pipeline.editor_jobs import set_studio_busy

    set_studio_busy(_task_running)
    register_routes()

    @ui.page("/")
    def index() -> None:
        # DutyCall palette (lib/themes.dart)
        ui.dark_mode().enable()
        ui.colors(
            primary="#127987",
            secondary="#37BD39",
            accent="#37BD39",
            dark="#121212",
            positive="#37BD39",
            negative="#FF3B30",
        )
        ui.add_head_html(_HEAD_CSS)
        ui.run_javascript(
            "window.Quasar && Quasar.Notify.setDefaults({position:'top-right'});"
        )

        global _current, _brief, _brief_open
        if _brief is None:
            stored = load_brief_store()
            if stored is not None:
                _brief = stored.brief
                _brief_open = stored.open
        if _current is not None:
            ui.run_javascript(
                "document.body.classList.add('rb-studio-active');"
            )
            _studio_view()
            return
        # Home / brief must scroll
        ui.run_javascript(
            "document.body.classList.remove('rb-studio-active');"
            "document.getElementById('rb-scene-shade')?.remove();"
        )
        with ui.header().classes(
            "items-center justify-between px-4 dc-header"
        ):
            with ui.row().classes("items-center gap-2"):
                ui.label("Imagine Studio").classes(
                    "text-h6 text-weight-bold"
                ).style("color: #37BD39; letter-spacing: 0.02em;")
            with ui.row().classes("items-center gap-2"):
                _activity_center()
                ui.button(
                    icon="health_and_safety", on_click=_open_doctor
                ).props("flat dense round").tooltip("Doctor")
        # One clip. The per-step scene brief is not a path in this app.
        _home_view()

    def _open_rally(path: Path, step: str | None = None) -> None:
        """Load a rally into the studio, optionally selecting a step."""
        global _current, _selected_step, _force_edit
        _current = RallyPackage.load(path)
        graph.normalize_clip(_current)
        graph.unlock_all_steps(_current)
        recovered = _recover_stale_running(_current)
        if recovered:
            _push_notice(
                "info",
                f"{_current.name}: cleared stuck generating",
                detail=(
                    "Steps were left as running after a restart: "
                    + ", ".join(_step_label(s) for s in recovered)
                ),
                rally=_current.name,
                step=recovered[0],
            )
            # Prefer landing on a recovered step if none requested
            if not step:
                step = recovered[0]
        if step and step in _current.steps:
            _selected_step = step
        else:
            _selected_step = _sole_step(_current)
        _force_edit = False
        global _view_final
        _view_final = False
        ui.navigate.to("/")

    def _activity_center() -> None:
        """Top-right queue + notices panel (home header and studio chrome)."""
        host = ui.element("div").classes("rb-activity")
        state: dict = {"sig": None, "details": {}}

        def _open_job(rally: str, step: str) -> None:
            root = OUTPUT_DIR / rally
            if not (root / "meta.json").exists():
                _push_notice(
                    "error",
                    f"Project “{rally}” not found",
                    rally=rally,
                    step=step,
                )
                return
            _open_rally(root, step)

        def _mark_read() -> None:
            for n in _notices:
                n.unread = False

        def _clear_notices() -> None:
            _notices.clear()
            state["sig"] = None
            paint()

        def _structure_sig(
            active: list[tuple[str, str, str]],
        ) -> tuple:
            """Rebuild only when jobs/notices set changes — not every progress tick."""
            return (
                tuple(k for k, _, _ in active),
                tuple(
                    (n.kind, n.title, n.detail, n.at, n.rally, n.step)
                    for n in _notices[:12]
                ),
            )

        def _refresh_badge(active_n: int) -> None:
            badge = state.get("badge")
            if badge is None:
                return
            unread = sum(1 for n in _notices if n.unread)
            badge_n = active_n + unread
            try:
                if badge_n:
                    badge.set_visibility(True)
                    badge.set_text(str(badge_n) if badge_n < 10 else "9+")
                else:
                    badge.set_visibility(False)
            except Exception:  # noqa: BLE001
                pass

        def paint() -> None:
            active = _active_jobs()
            unread = sum(1 for n in _notices if n.unread)
            badge_n = len(active) + unread
            sig = _structure_sig(active)

            # Soft-update details + badge without closing an open menu
            if sig == state["sig"]:
                dead: list[str] = []
                for key, lbl in list(state["details"].items()):
                    if not _el_alive(lbl):
                        dead.append(key)
                        continue
                    try:
                        lbl.text = (_job_detail(key) or "Working…")[:90]
                    except Exception:  # noqa: BLE001
                        dead.append(key)
                for key in dead:
                    state["details"].pop(key, None)
                if state.get("badge") is not None and not _el_alive(
                    state["badge"]
                ):
                    state["badge"] = None
                    state["sig"] = None
                    return
                _refresh_badge(len(active))
                return

            state["sig"] = sig
            state["details"] = {}
            state["badge"] = None
            host.clear()
            with host:
                icon = "hourglass_top" if active else "notifications"
                props = "flat dense round"
                if active:
                    props += " color=orange"
                elif unread:
                    props += " color=primary"
                with ui.button(
                    icon=icon, on_click=lambda: _mark_read()
                ).props(props).classes("rb-activity-btn") as btn:
                    btn.tooltip("Queue & notifications")
                    badge = (
                        ui.badge(
                            str(badge_n) if badge_n < 10 else "9+"
                        )
                        .props("floating color=orange rounded")
                        .classes("rb-activity-badge")
                    )
                    badge.set_visibility(badge_n > 0)
                    state["badge"] = badge
                    with ui.menu().classes("rb-activity-menu").props(
                        "anchor=top end self=top end"
                    ):
                        with ui.row().classes(
                            "items-center justify-between w-full "
                            "q-px-md q-pt-sm q-pb-xs"
                        ):
                            ui.label("Activity").classes(
                                "text-subtitle2 text-weight-medium"
                            )
                            if _notices:
                                ui.button(
                                    "Clear",
                                    on_click=_clear_notices,
                                ).props("flat dense no-caps size=sm")

                        if active:
                            ui.label("In progress").classes(
                                "text-caption text-grey-5 q-px-md q-pt-xs"
                            )
                            for key, rally, step in active:
                                detail = (_job_detail(key) or "Working…")[:90]
                                label = f"{rally} · {_step_label(step)}"

                                def go(
                                    r: str = rally,
                                    s: str = step,
                                ) -> None:
                                    _open_job(r, s)

                                with ui.menu_item(on_click=go):
                                    with ui.row().classes(
                                        "items-start gap-2 no-wrap"
                                    ):
                                        ui.spinner(
                                            size="16px", color="orange"
                                        ).classes("q-mt-xs")
                                        with ui.column().classes(
                                            "gap-0"
                                        ).style("min-width: 0;"):
                                            ui.label(label).classes(
                                                "text-weight-medium"
                                            )
                                            detail_lbl = ui.label(
                                                detail
                                            ).classes(
                                                "text-caption text-grey-6"
                                            ).style(
                                                "max-width: 18rem; "
                                                "overflow: hidden; "
                                                "text-overflow: ellipsis; "
                                                "white-space: nowrap;"
                                            )
                                            state["details"][key] = (
                                                detail_lbl
                                            )

                        if active and _notices:
                            ui.separator().classes("q-my-xs")

                        if _notices:
                            ui.label(
                                "Notifications"
                                if active
                                else "Recent"
                            ).classes(
                                "text-caption text-grey-5 q-px-md"
                                + (" q-pt-xs" if not active else "")
                            )
                            for notice in _notices[:12]:
                                icon_map = {
                                    "success": (
                                        "check_circle",
                                        "positive",
                                    ),
                                    "error": ("error", "negative"),
                                    "info": ("info", "primary"),
                                }
                                ic, col = icon_map.get(
                                    notice.kind, ("info", "grey")
                                )
                                can_open = bool(
                                    notice.rally and notice.step
                                )

                                def go_notice(
                                    n: _Notice = notice,
                                ) -> None:
                                    if n.rally and n.step:
                                        _open_job(n.rally, n.step)

                                item = ui.menu_item(
                                    on_click=go_notice
                                    if can_open
                                    else None
                                )
                                with item:
                                    with ui.row().classes(
                                        "items-start gap-2 no-wrap"
                                    ):
                                        ui.icon(
                                            ic, color=col, size="18px"
                                        ).classes("q-mt-xs")
                                        with ui.column().classes(
                                            "gap-0"
                                        ).style("min-width: 0;"):
                                            with ui.row().classes(
                                                "items-center gap-2 "
                                                "no-wrap justify-between"
                                            ).style("width: 100%;"):
                                                ui.label(
                                                    notice.title
                                                ).classes(
                                                    "text-weight-medium"
                                                ).style(
                                                    "max-width: 14rem; "
                                                    "overflow: hidden; "
                                                    "text-overflow: ellipsis; "
                                                    "white-space: nowrap;"
                                                )
                                                ui.label(
                                                    _fmt_notice_age(
                                                        notice.at
                                                    )
                                                ).classes(
                                                    "text-caption text-grey-7"
                                                )
                                            if notice.detail:
                                                ui.label(
                                                    notice.detail[:100]
                                                ).classes(
                                                    "text-caption text-grey-6"
                                                ).style(
                                                    "max-width: 18rem; "
                                                    "overflow: hidden; "
                                                    "text-overflow: ellipsis; "
                                                    "white-space: nowrap;"
                                                )
                        elif not active:
                            with ui.column().classes(
                                "q-pa-md items-center gap-1"
                            ):
                                ui.icon(
                                    "notifications_none", size="28px"
                                ).classes("text-grey-7")
                                ui.label("No activity yet").classes(
                                    "text-caption text-grey-6"
                                )

        paint()
        ui.timer(1.0, paint)

    def _normalize_template(raw: str) -> str:
        _ = raw
        return "rally"

    def _home_view() -> None:
        """Landing: concept → Grok invents tone and a scene per clip."""
        global _brief
        form = {
            "name": _brief.name if _brief else "",
            "template": "rally",
            "concept": _brief.concept if _brief else "",
            "aspect": _brief.aspect_ratio if _brief else "9:16",
        }

        with ui.column().classes(
            "w-full max-w-4xl mx-auto q-pa-md gap-5 rb-home"
        ):
            # ── Create card ──────────────────────────────────────
            with ui.element("div").classes("dc-card w-full q-pa-md"):
                ui.label("New project").classes(
                    "text-h6 text-weight-medium q-mb-md"
                )

                ui.label("Name").classes("dc-field-title")
                name_in = (
                    ui.input(value=form["name"])
                    .classes("w-full dc-input")
                    .props(
                        'outlined dense dark '
                        'placeholder="Folder name on disk"'
                    )
                )
                name_in.bind_value(form, "name")

                ui.label("Concept").classes("dc-field-title q-mt-md")
                concept_in = (
                    ui.textarea(value=form["concept"])
                    .classes("w-full dc-input")
                    .props(
                        "outlined dense dark autogrow "
                        'placeholder="What this is, and the vibe"'
                    )
                )
                concept_in.bind_value(form, "concept")
                ui.label(
                    "Opens the editor with this concept."
                ).classes("dc-hint")

                ui.label("Aspect").classes("dc-field-title q-mt-md")
                aspect = (
                    ui.select(["9:16", "1:1"], value=form["aspect"])
                    .classes("w-40 dc-input")
                    .props("outlined dense dark")
                )
                aspect.bind_value(form, "aspect")
                ui.label(
                    "9:16 is portrait · 1:1 is square"
                ).classes("dc-hint")

                def open_pipeline() -> None:
                    global _current, _selected_step, _force_edit, _brief_open, _brief
                    concept = (form.get("concept") or "").strip()
                    if not concept:
                        ui.notify(
                            "Type a concept first",
                            type="warning",
                        )
                        return
                    typed = (
                        form.get("name") or concept[:48] or "project"
                    ).strip()
                    name = slugify(typed)
                    root = OUTPUT_DIR / name
                    if root.exists() and (root / "meta.json").exists():
                        ui.notify(
                            f"“{name}” already exists — pick another name",
                            type="warning",
                        )
                        return
                    root.mkdir(parents=True, exist_ok=True)
                    pkg = graph.new_rally(
                        name=typed,
                        concept=concept,
                        root=root,
                        aspect_ratio=str(form.get("aspect") or "9:16"),
                        template="rally",
                    )
                    pkg.save()
                    _current = pkg
                    _selected_step = _sole_step(pkg)
                    _force_edit = False
                    global _view_final
                    _view_final = False
                    _brief_open = False
                    _brief = None
                    clear_brief_store()
                    ui.notify(f"Ready · {name}", type="positive")
                    ui.navigate.to("/")

                with ui.column().classes("w-full q-mt-lg gap-2"):
                    ui.button(
                        "Open editor",
                        icon="play_arrow",
                        on_click=open_pipeline,
                    ).classes("w-full dc-cta").props(
                        "unelevated color=secondary no-caps"
                    )

            # ── Recent grid (newest first, Preview thumbnails) ───
            ui.label("Recent").classes("text-h6 text-weight-medium")
            rallies = list_rallies(OUTPUT_DIR)
            if not rallies:
                ui.label("No projects yet — create one above.").classes(
                    "text-grey-6"
                )
            else:
                with ui.element("div").classes("dc-rally-grid"):
                    for path in rallies:
                        _recent_card(path)

    def _brief_view() -> None:
        """Review Grok's per-step scenes. Pipeline has not started yet."""
        global _brief, _brief_open, _current, _selected_step, _force_edit
        brief = _brief
        if brief is None:
            _brief_open = False
            _home_view()
            return

        form = {
            "name": (getattr(brief, "display_name", None) or brief.name),
            "template": "rally",
            "concept": brief.concept,
            "aspect": brief.aspect_ratio,
            "tone": brief.tone,
            "look": brief.look,
        }
        beats = {
            sid: brief.beats.get(sid, "") for sid in PIPELINE_STEPS
        }
        starts = {
            sid: brief.starts.get(sid, "")
            for sid in ("Rally", "Message", "Accept", "Decline", "Adjust")
        }
        defaults = default_step_states("rally")

        with ui.column().classes(
            "w-full max-w-4xl mx-auto q-pa-md gap-4 rb-brief"
        ):
            ui.label("Scene brief").classes("text-h6 text-weight-medium")
            ui.label(
                "Tone plus a scene for each clip. Edit anything. "
                "The pipeline starts only when you say so."
            ).classes("text-body2 text-grey-5")

            with ui.element("div").classes("dc-card w-full q-pa-md"):
                ui.label("Name").classes("dc-field-title")
                name_in = (
                    ui.input(value=form["name"])
                    .classes("w-full dc-input")
                    .props("outlined dense dark")
                )
                name_in.bind_value(form, "name")

                ui.label("Concept").classes("dc-field-title q-mt-md")
                concept_in = (
                    ui.textarea(value=form["concept"])
                    .classes("w-full dc-input")
                    .props("outlined dense dark autogrow")
                )
                concept_in.bind_value(form, "concept")

                ui.label("Tone").classes("dc-field-title q-mt-md")
                tone_in = (
                    ui.textarea(value=form["tone"])
                    .classes("w-full dc-input")
                    .props("outlined dense dark autogrow")
                )
                tone_in.bind_value(form, "tone")
                ui.label(
                    "Register — serious LARP, joke, roast. Carried into every clip."
                ).classes("dc-hint")

                ui.label("World").classes("dc-field-title q-mt-md")
                look_in = (
                    ui.textarea(value=form["look"])
                    .classes("w-full dc-input")
                    .props("outlined dense dark autogrow")
                )
                look_in.bind_value(form, "look")
                ui.label(
                    "Visual through-line for stills. Clip prompts carry the pictures."
                ).classes("dc-hint")

            for sid in PIPELINE_STEPS:
                st = defaults.get(sid)
                title = "Reschedule" if sid == "Adjust" else sid
                role = STEP_ROLES.get(sid) or (st.role if st else "") or ""
                start_raw = starts.get(sid, "")
                mode, _staged = parse_start_lock(start_raw, sid)
                conn = connection_label(
                    sid, "rally", options={"start_mode": mode}
                )
                with ui.element("div").classes("rb-brief-step w-full"):
                    with ui.row().classes(
                        "w-full items-baseline justify-between no-wrap"
                    ):
                        ui.label(title).classes("text-weight-medium")
                        if role:
                            ui.label(role).classes("text-caption text-grey-6")
                    ui.label(conn).classes("text-caption text-teal-4 q-mb-xs")
                    if sid in starts:
                        ui.label("Opening").classes("text-caption text-grey-5")
                        start_area = (
                            ui.textarea(value=starts.get(sid, ""))
                            .classes("w-full dc-input")
                            .props(
                                "outlined dense dark autogrow "
                                'placeholder="preview, idle, or a still of a '
                                'different opening"'
                            )
                        )
                        start_area.bind_value(starts, sid)
                    ui.label("Beat").classes("text-caption text-grey-5")
                    area = (
                        ui.textarea(value=beats.get(sid, ""))
                        .classes("w-full dc-input")
                        .props("outlined dense dark autogrow")
                    )
                    area.bind_value(beats, sid)

            def _sync_brief(*, open: bool | None = None) -> None:
                assert _brief is not None
                raw_name = (form.get("name") or "").strip() or (
                    _brief.concept[:48] or "rally"
                )
                _brief.display_name = raw_name
                _brief.name = slugify(raw_name)
                _brief.template = "rally"
                _brief.concept = (form.get("concept") or "").strip()
                _brief.tone = (form.get("tone") or "").strip()
                _brief.look = (form.get("look") or "").strip()
                _brief.aspect_ratio = str(form.get("aspect") or "9:16")
                _brief.beats = dict(beats)
                _brief.starts = dict(starts)
                persist_brief(
                    _brief,
                    open=_brief_open if open is None else open,
                )

            def back() -> None:
                global _brief_open
                _brief_open = False
                _sync_brief(open=False)
                ui.navigate.to("/")

            async def recompose() -> None:
                _sync_brief()
                concept = (_brief.concept if _brief else "").strip()
                if not concept:
                    ui.notify("Concept is empty", type="warning")
                    return
                notes = (notes_in.value or "").strip()
                recompose_btn.props("loading")
                try:
                    previous = None
                    if notes and _brief is not None:
                        previous = _brief.to_pack()
                    pack = await expand_concept(
                        concept,
                        aspect_ratio=_brief.aspect_ratio,
                        notes=notes,
                        previous=previous,
                    )
                    _brief.apply_pack(pack)
                    persist_brief(_brief, open=True)
                    notes_in.value = ""
                    ui.notify(
                        "Scenes revised from your notes"
                        if notes
                        else "Scenes rewritten from concept",
                        type="positive",
                    )
                    ui.navigate.to("/")
                except Exception as exc:  # noqa: BLE001
                    ui.notify(str(exc), type="negative")
                finally:
                    recompose_btn.props(remove="loading")

            def start_pipeline() -> None:
                global _current, _selected_step, _force_edit, _brief_open, _brief
                global _view_final
                _sync_brief()
                b = _brief
                assert b is not None
                if not b.concept:
                    ui.notify("Concept is empty", type="warning")
                    return
                typed = (
                    (getattr(b, "display_name", None) or b.name or b.concept[:48] or "rally")
                ).strip()
                name = slugify(typed)
                root = OUTPUT_DIR / name
                if root.exists() and (root / "meta.json").exists():
                    ui.notify(
                        f"“{name}” already exists — pick another name",
                        type="warning",
                    )
                    return
                root.mkdir(parents=True, exist_ok=True)
                pkg = graph.new_rally(
                    name=typed,
                    concept=b.concept,
                    root=root,
                    aspect_ratio=b.aspect_ratio,
                    template="rally",
                )
                b.apply_to_package(pkg)
                pkg.save()
                _current = pkg
                _selected_step = _sole_step(pkg)
                _force_edit = False
                _view_final = False
                _brief_open = False
                _brief = None
                clear_brief_store()
                ui.notify(f"Ready · {name}", type="positive")
                ui.navigate.to("/")

            with ui.element("div").classes("rb-brief-sticky w-full"):
                notes_in = (
                    ui.textarea(
                        placeholder=(
                            "What to change — e.g. Preview too busy, Idle seated "
                            "not standing, Decline meaner…"
                        )
                    )
                    .classes("w-full dc-input")
                    .props("outlined dense dark autogrow")
                )
                ui.label(
                    "Read the draft, then tell Grok what to fix. "
                    "Leave blank to invent from scratch."
                ).classes("text-caption text-grey-5 q-mb-sm")
                with ui.row().classes("w-full gap-2 no-wrap"):
                    ui.button("Back", icon="arrow_back", on_click=back).props(
                        "flat no-caps"
                    )
                    recompose_btn = (
                        ui.button(
                            "Revise",
                            icon="refresh",
                            on_click=recompose,
                        ).props("outline no-caps")
                    )
                    ui.button(
                        "Open editor",
                        icon="play_arrow",
                        on_click=start_pipeline,
                    ).classes("col dc-cta").props(
                        "unelevated color=secondary no-caps"
                    )

            ui.timer(1.5, lambda: _sync_brief(open=True))

    def _recent_card(path: Path) -> None:
        """DutyCall-style skin tile: opening still if present, else placeholder."""
        try:
            pkg = RallyPackage.load(path)
            title = pkg.rally_title()
            subtitle = (pkg.concept or "").strip()
        except Exception:  # noqa: BLE001
            title = path.name
            subtitle = ""

        from urllib.parse import quote

        thumb: Path | None = None
        for name in ("start.png", "Rally.start.png", "Preview.png"):
            cand = path / name
            if cand.is_file():
                thumb = cand
                break
        if thumb is None:
            pngs = sorted(p for p in path.glob("*.png") if p.is_file())
            thumb = pngs[0] if pngs else None
        bust = int(thumb.stat().st_mtime * 1000) if thumb is not None else 0
        preview_url = (
            f"/media/{quote(path.name)}/{quote(thumb.name)}?t={bust}"
            if thumb is not None
            else ""
        )

        def open_pkg(p: Path = path) -> None:
            _open_rally(p)

        def ask_delete(p: Path = path, title_: str = title) -> None:
            with ui.dialog().props("position=top").classes(
                "rb-confirm"
            ) as dlg, ui.card().classes("q-pa-md"):
                ui.label(f"Delete “{title_}”?").classes("text-subtitle1")
                ui.label(
                    "Removes the folder on disk. This cannot be undone."
                ).classes("text-caption text-grey-5")
                with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                    ui.button("Cancel", on_click=dlg.close).props(
                        "flat no-caps"
                    )

                    def confirm() -> None:
                        global _current
                        try:
                            name = p.name
                            for key in [
                                k
                                for k, t in list(_gen_tasks.items())
                                if k.startswith(f"{name}/")
                            ]:
                                t = _gen_tasks.pop(key, None)
                                if t is not None:
                                    t.cancel()
                            if _current is not None and _current.name == name:
                                _current = None
                            delete_rally(p)
                            dlg.close()
                            ui.notify(f"Deleted {title_}", type="info")
                            ui.navigate.to("/")
                        except Exception as exc:  # noqa: BLE001
                            ui.notify(str(exc), type="negative")

                    ui.button("Delete", on_click=confirm).props(
                        "unelevated no-caps color=negative"
                    )
            dlg.open()

        with ui.element("div").classes("dc-rally-card").on(
            "click", open_pkg
        ):
            if preview_url:
                ui.html(
                    f'<img class="dc-rally-thumb" src="{preview_url}" '
                    f'alt="{title}" loading="lazy" />',
                    sanitize=False,
                )
            else:
                with ui.element("div").classes("dc-rally-thumb dc-rally-empty"):
                    ui.icon("image_not_supported", size="32px").classes(
                        "text-grey-7"
                    )
            del_btn = (
                ui.button(icon="delete_outline")
                .props("flat dense round size=sm color=grey")
                .classes("dc-rally-del")
                .tooltip("Delete project")
            )
            del_btn.on("click.stop", ask_delete)
            with ui.element("div").classes("dc-rally-meta"):
                ui.label(title).classes("dc-rally-title")
                if subtitle:
                    ui.label(subtitle).classes("dc-rally-sub")

    def _studio_view() -> None:
        shell = ui.element("div").classes("rb-shell")

        def refresh_all() -> None:
            if not _el_alive(shell):
                return
            shell.clear()
            with shell:
                _build_studio(refresh_all)

        refresh_all()

    def _close_rally() -> None:
        global _current, _force_edit, _view_final
        # Keep background jobs running — top-right Activity tracks them.
        n_run = sum(1 for t in _gen_tasks.values() if not t.done())
        _current = None
        _force_edit = False
        _view_final = False
        ui.run_javascript(
            "document.body.classList.remove('rb-studio-active');"
            "document.getElementById('rb-scene-shade')?.remove();"
        )
        if n_run:
            _push_notice(
                "info",
                f"{n_run} generation{'s' if n_run != 1 else ''} still running",
                detail="Open Activity (top right) to watch progress",
            )
        ui.navigate.to("/")

    def _build_studio(refresh_all) -> None:
        pkg = _pkg()
        before_title = pkg.display_name
        pkg.ensure_display_name()
        if pkg.display_name != before_title:
            pkg.save()
        graph.unlock_all_steps(pkg)
        kit = getattr(pkg, "kit", None) or {}
        if not kit.get("voices"):
            pkg.kit = {
                "voices": [
                    {"voice_id": "eve", "role": "Voice 1"},
                    {"voice_id": "leo", "role": "Voice 2"},
                ],
                "sounds": list(kit.get("sounds") or []),
            }
            pkg.save()
        phase = _studio_phase(pkg)
        keep_editor = json.dumps(
            f"{pkg.name}:{_selected_step}" if phase == "edit" else ""
        )
        ui.run_javascript(
            "(() => {"
            f" const key = {keep_editor};"
            " const shade = document.getElementById('rb-scene-shade');"
            " if (!shade) return;"
            " if (!key || shade.dataset.key !== key) shade.remove();"
            "})();"
        )

        # Chrome
        with ui.row().classes(
            "w-full items-center no-wrap q-px-xs gap-1 rb-chrome"
        ).style(
            "border-bottom: 1px solid rgba(255,255,255,0.08); background: #0d0e14;"
        ):
            ui.button(icon="arrow_back", on_click=_close_rally).props(
                "flat dense round size=sm"
            ).tooltip("Close")
            with ui.column().classes("gap-0").style(
                "flex-shrink: 1; min-width: 8rem; max-width: 18rem;"
            ):
                title = pkg.ensure_display_name()
                ui.label(title).classes("text-weight-bold text-caption").style(
                    "overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
                )
                if phase != "edit":
                    concept_bit = (pkg.concept or "").strip().replace("\n", " ")
                    if concept_bit:
                        ui.label(concept_bit).classes(
                            "text-caption text-grey-6"
                        ).style(
                            "font-size: 0.65rem; line-height: 1.2; "
                            "overflow: hidden; text-overflow: ellipsis; "
                            "white-space: nowrap;"
                        )

            job_chip = ui.element("div").classes("rb-chrome-job is-off")
            with job_chip:
                job_spin = ui.spinner("dots", size="1em").classes("rb-chrome-job-spin")
                job_lab = ui.label("").classes("rb-chrome-job-text")

            def _poll_chrome_job() -> None:
                if not _el_alive(job_chip):
                    return
                p = _pkg()
                sid = _selected_step
                key = _job_key(p.name, sid)
                generating = _is_generating(p.name, sid)
                err = ""
                try:
                    err = p.step(sid).error or ""
                except Exception:  # noqa: BLE001
                    err = ""
                if generating:
                    job_chip.classes(remove="is-off")
                    job_chip.classes(remove="is-err")
                    detail = _job_detail(key)
                    msg = f"Generating {_step_label(sid)}"
                    if detail:
                        msg += " · " + " ".join(detail.split())[:90]
                    job_lab.set_text(msg)
                    if _el_alive(job_spin):
                        job_spin.set_visibility(True)
                elif err:
                    job_chip.classes(remove="is-off")
                    job_chip.classes(add="is-err")
                    flat = " ".join(err.split())
                    job_lab.set_text(f"{_step_label(sid)} failed · {flat[:140]}")
                    if _el_alive(job_spin):
                        job_spin.set_visibility(False)
                else:
                    job_chip.classes(add="is-off")
                    job_chip.classes(remove="is-err")
                    if _el_alive(job_lab):
                        job_lab.set_text("")
                    if _el_alive(job_spin):
                        job_spin.set_visibility(False)

            if phase == "edit":
                ui.timer(0.45, _poll_chrome_job)

            def _show_final() -> None:
                global _view_final
                _view_final = True
                refresh_all()

            def _download_take() -> None:
                p = _pkg()
                sid = (
                    _selected_step if _selected_step in p.steps else _sole_step(p)
                )
                path = p.primary_media_for_step(sid)
                if path is None or not path.is_file():
                    ui.notify("No take to download yet", type="warning")
                    return
                ui.download(path, filename=path.name)

            with ui.row().classes("items-center gap-1 no-wrap rb-chrome-right"):
                if phase == "edit":
                    ui.button(
                        "Final", icon="fullscreen", on_click=_show_final
                    ).props("outline dense no-caps")
                elif phase == "final":
                    ui.button(
                        "Download", icon="download", on_click=_download_take
                    ).props("outline dense no-caps")
                    ui.button(
                        "Edit",
                        icon="edit",
                        on_click=lambda: _back_to_edit(refresh_all),
                    ).props("flat dense no-caps")
                _activity_center()
                ui.button(icon="health_and_safety", on_click=_open_doctor).props(
                    "flat dense round size=sm"
                ).tooltip("Doctor")

        if phase == "edit":
            def _scenes_refresh(saved: bool = False, note: str = "") -> None:
                if saved:
                    _sync_current_from_disk(pkg.name)
                    if note:
                        ui.notify(note, type="positive")
                refresh_all()

            def _open_scenes() -> None:
                p = _pkg()
                open_scene_editor(
                    p,
                    _selected_step,
                    lambda path: _media_url(p, path.name, path),
                )

            render_scene_bar(
                pkg,
                _selected_step,
                refresh=_scenes_refresh,
                open_editor=_open_scenes,
            )

        body = ui.element("div").classes("rb-body w-full")
        with body:
            if phase == "final":
                _render_final(refresh_all)
            else:
                _render_edit(refresh_all)

    # ── Final review: fullscreen sequential autoplay ─────────────
    def _render_final_review(refresh_all) -> None:
        pkg = _pkg()
        # Product order: Preview → Rally → Message → Idle → Accept → Decline → Adjust
        items: list[dict] = []
        for sid in FINAL_REVIEW_STEPS:
            path = pkg.primary_media_for_step(sid)
            if path is None:
                continue
            url = _media_url(pkg, path.name, path)
            kind = (
                "video"
                if path.suffix.lower() in {".mp4", ".webm", ".mov"}
                else "image"
            )
            items.append(
                {
                    "id": sid,
                    "label": (
                        "Reschedule" if sid == "Adjust" else pkg.step(sid).label
                    ),
                    "url": url,
                    "kind": kind,
                }
            )

        if not items:
            with ui.column().classes("w-full h-full items-center justify-center gap-3"):
                ui.label("No media for final review").classes("text-h6")
                ui.button(
                    "Back to edit",
                    on_click=lambda: _back_to_edit(refresh_all),
                )
            return

        playlist_json = json.dumps(items)
        idle_index = next(
            (idx for idx, it in enumerate(items) if it["id"] == "Idle"), 0
        )

        # Full-bleed stage under main chrome (stepper lives in chrome, not here)
        with ui.element("div").classes("rb-review").props('id="rb-review-root"'):
            ui.html(
                """
                <div id="rb-review-stage" class="rb-review-stage">
                  <img id="rb-review-img" class="rb-review-media" alt="" />
                  <video id="rb-review-vid" class="rb-review-media"
                    playsinline controls></video>
                </div>
                """,
                sanitize=False,
            )

        js = f"""
        (() => {{
          const items = {playlist_json};
          const stillMs = {_STILL_MS};
          const idleIndex = {idle_index};
          let i = 0;
          let timer = null;
          let started = false;
          // After Reschedule (Adjust), hold on Idle and loop that clip forever
          let holdOnIdle = false;

          function els() {{
            return {{
              img: document.getElementById('rb-review-img'),
              vid: document.getElementById('rb-review-vid'),
              stage: document.getElementById('rb-review-stage'),
            }};
          }}

          function unmuteVideo(vid) {{
            vid.muted = false;
            vid.defaultMuted = false;
            vid.volume = 1;
            vid.removeAttribute('muted');
          }}

          function muteVideo(vid) {{
            vid.muted = true;
            vid.defaultMuted = true;
            vid.volume = 0;
            vid.setAttribute('muted', '');
          }}

          function showEl(el, on) {{
            if (!el) return;
            el.style.display = on ? 'block' : 'none';
            el.style.visibility = on ? 'visible' : 'hidden';
            el.style.opacity = on ? '1' : '0';
            el.classList.toggle('rb-review-active', !!on);
          }}

          function highlightStepper() {{
            const it = items[i];
            if (!it) return;
            document.querySelectorAll('.rb-stepper-btn').forEach((btn) => {{
              const sidClass = [...btn.classList].find((c) => c.startsWith('rb-review-sid-'));
              const sid = sidClass ? sidClass.slice('rb-review-sid-'.length) : '';
              const on = sid === it.id;
              btn.classList.toggle('rb-stepper-current', on);
              if (on) {{
                btn.classList.add('q-btn--unelevated');
              }} else {{
                btn.classList.remove('q-btn--unelevated');
              }}
            }});
          }}

          function clearTimer() {{
            if (timer) {{ clearTimeout(timer); timer = null; }}
          }}

          function showNext() {{
            const cur = items[i];
            // After final step (Adjust / Reschedule) → Idle and stay looping
            if (cur && (cur.id === 'Adjust' || i >= items.length - 1)) {{
              holdOnIdle = true;
              i = idleIndex;
              playCurrent();
              return;
            }}
            i = i + 1;
            if (i >= items.length) {{
              holdOnIdle = true;
              i = idleIndex;
            }}
            playCurrent();
          }}

          function playCurrent() {{
            const {{ img, vid }} = els();
            if (!img || !vid) return;
            clearTimer();
            const it = items[i];
            if (!it) return;
            highlightStepper();
            const loopIdle = holdOnIdle && it.id === 'Idle';

            if (it.kind === 'video') {{
              showEl(img, false);
              img.removeAttribute('src');
              showEl(vid, true);
              vid.onended = null;
              vid.loop = !!loopIdle;
              vid.onerror = function() {{
                console.error('review video error', it.url);
                if (!loopIdle) showNext();
              }};
              unmuteVideo(vid);
              // Only reload src if changed (smoother when re-entering Idle loop)
              if (vid.dataset.rbSrc !== it.url) {{
                vid.src = it.url;
                vid.dataset.rbSrc = it.url;
                vid.load();
              }}
              unmuteVideo(vid);
              const p = vid.play();
              if (p && p.catch) {{
                p.catch((e) => {{
                  console.warn('unmuted play blocked, trying muted', e);
                  muteVideo(vid);
                  vid.play().catch(() => {{ if (!loopIdle) showNext(); }});
                }});
              }}
              if (!loopIdle) {{
                vid.onended = function() {{ showNext(); }};
              }}
            }} else {{
              try {{ vid.pause(); }} catch (e) {{}}
              vid.removeAttribute('src');
              delete vid.dataset.rbSrc;
              vid.loop = false;
              showEl(vid, false);
              showEl(img, true);
              img.onerror = function() {{
                console.error('review image error', it.url);
                showNext();
              }};
              img.src = it.url;
              // Stills never "hold" — even if somehow on Idle as image
              timer = setTimeout(function() {{
                if (loopIdle) {{
                  // re-show still loop
                  playCurrent();
                }} else {{
                  showNext();
                }}
              }}, stillMs);
            }}
          }}

          window.__rbReviewReplay = function() {{
            holdOnIdle = false;
            i = 0;
            playCurrent();
          }};
          window.__rbReviewJump = function(stepId) {{
            const idx = items.findIndex((x) => x.id === stepId);
            if (idx < 0) return;
            // Manual jump: leave hold mode unless jumping to Idle after finishing
            holdOnIdle = false;
            i = idx;
            playCurrent();
          }};
          window.__rbReviewStop = function() {{
            clearTimer();
            const {{ vid }} = els();
            try {{ if (vid) {{ vid.pause(); vid.loop = false; }} }} catch (e) {{}}
          }};

          function boot(attempt) {{
            const {{ img, vid, stage }} = els();
            if (img && vid && stage) {{
              const r = stage.getBoundingClientRect();
              if (r.height < 40 && attempt < 50) {{
                setTimeout(function() {{ boot(attempt + 1); }}, 40);
                return;
              }}
              if (!started) {{
                started = true;
                holdOnIdle = false;
                i = 0;
                playCurrent();
              }}
              return;
            }}
            if (attempt < 50) {{
              setTimeout(function() {{ boot(attempt + 1); }}, 40);
            }} else {{
              console.error('final review: media elements never appeared');
            }}
          }}
          boot(0);
        }})();
        """
        ui.timer(0.08, lambda: ui.run_javascript(js), once=True)

    def _render_final(refresh_all) -> None:
        """Fullscreen playback of the one take."""
        from html import escape

        pkg = _pkg()
        sid = _selected_step if _selected_step in pkg.steps else _sole_step(pkg)
        path = pkg.primary_media_for_step(sid)
        if path is None or not path.is_file():
            with ui.column().classes(
                "w-full h-full items-center justify-center gap-3"
            ):
                ui.label("No take yet").classes("text-h6")
                ui.button(
                    "Back to edit",
                    on_click=lambda: _back_to_edit(refresh_all),
                )
            return
        url = escape(_media_url(pkg, path.name, path), quote=True)
        if path.suffix.lower() in {".mp4", ".webm", ".mov"}:
            media = (
                f'<video class="rb-review-media rb-review-active" src="{url}" '
                "playsinline controls autoplay></video>"
            )
        else:
            media = (
                f'<img class="rb-review-media rb-review-active" alt="" src="{url}" />'
            )
        with ui.element("div").classes("rb-review"):
            ui.html(
                f'<div class="rb-review-stage">{media}</div>',
                sanitize=False,
            )

    def _back_to_edit(refresh_all) -> None:
        global _force_edit, _view_final
        _force_edit = True
        _view_final = False
        ui.run_javascript("window.__rbReviewStop && window.__rbReviewStop()")
        refresh_all()

    # ── Publish: Free / Premium / Private ────────────────────────
    def _render_publish(refresh_all) -> None:
        pkg = _pkg()
        default_name = pkg.display_name or pkg.name.replace("-", " ").title()
        # Holder always tracks the typed name (NiceGUI .value can lag in async handlers)
        name_state = {"value": default_name}

        with ui.column().classes(
            "w-full h-full items-stretch gap-3 q-pa-md"
        ).style("overflow-y: auto; max-width: 720px; margin: 0 auto;"):
            ui.label("Publish package").classes("text-h5")
            ui.label(
                "Name the rally as players will see it, then compress (iOS H.264) "
                "and upload to Free, Premium, or Private."
            ).classes("text-body2 text-grey-5")
            ui.label(
                f"Local folder: {pkg.name} · final review approved"
            ).classes("text-caption text-grey-6")

            name_in = ui.input(
                "Public display name",
                value=default_name,
                placeholder="e.g. DC Knight",
            ).classes("w-full").props("outlined dense")

            def on_name_change(e=None) -> None:
                # Event args may be the string or an object with .args / .value
                raw = name_in.value
                if e is not None:
                    args = getattr(e, "args", None)
                    if isinstance(args, str):
                        raw = args
                    elif args is not None and not isinstance(args, (list, dict)):
                        raw = str(args)
                    elif hasattr(e, "value") and e.value is not None:
                        raw = e.value
                name_state["value"] = str(raw or "")
                # Persist immediately so rebuilds keep the typed name
                p = _pkg()
                cleaned = sanitize_display_name(name_state["value"])
                if cleaned:
                    p.display_name = cleaned
                    p.save()

            name_in.on("update:model-value", on_name_change)
            name_in.on("blur", on_name_change)
            name_in.on("change", on_name_change)

            ui.label(
                "Becomes the folder under free|premium/rallies/… "
                "(or the zip name for private)."
            ).classes("text-caption text-grey-6")

            # Live log + size table
            log_box = (
                ui.log(max_lines=80)
                .classes("w-full")
                .style(
                    "height: 160px; font-family: ui-monospace, monospace; "
                    "font-size: 0.75rem; background: #111; border-radius: 8px;"
                )
            )
            size_host = ui.column().classes("w-full gap-1")

            status = ui.label("").classes("text-body2 text-weight-medium")
            busy = {"on": False}

            def render_size_table(rows, before_total: int, after_total: int) -> None:
                size_host.clear()
                with size_host:
                    ui.label("Compression results").classes("text-subtitle2")
                    cols = [
                        {"name": "file", "label": "File", "field": "file", "align": "left"},
                        {
                            "name": "before",
                            "label": "Before",
                            "field": "before",
                            "align": "right",
                        },
                        {
                            "name": "after",
                            "label": "After",
                            "field": "after",
                            "align": "right",
                        },
                        {
                            "name": "saved",
                            "label": "Saved",
                            "field": "saved",
                            "align": "right",
                        },
                    ]
                    data = [
                        {
                            "file": r.name,
                            "before": fmt_bytes(r.before),
                            "after": fmt_bytes(r.after),
                            "saved": (
                                f"{fmt_bytes(r.saved)} ({r.pct:.0f}%)"
                                if r.before != r.after
                                else "—"
                            ),
                        }
                        for r in rows
                    ]
                    data.append(
                        {
                            "file": "TOTAL",
                            "before": fmt_bytes(before_total),
                            "after": fmt_bytes(after_total),
                            "saved": (
                                f"{fmt_bytes(max(0, before_total - after_total))} "
                                f"({(100 * (1 - after_total / before_total)) if before_total else 0:.0f}%)"
                            ),
                        }
                    )
                    ui.table(columns=cols, rows=data, row_key="file").classes(
                        "w-full"
                    ).props("dense flat bordered")

            def resolve_display() -> str:
                # Prefer live typed state, then input widget, then saved meta
                candidates = [
                    name_state.get("value"),
                    name_in.value,
                    _pkg().display_name,
                ]
                for c in candidates:
                    d = sanitize_display_name(str(c or ""))
                    if d and d.lower() not in ("", "untitled rally"):
                        # Avoid silently falling back to local folder title-case
                        # only when user never typed anything useful.
                        return d
                # Last resort: typed empty → use folder title
                return sanitize_display_name(
                    _pkg().display_name or _pkg().name.replace("-", " ").title()
                )

            async def publish(tier: str) -> None:
                if busy["on"]:
                    ui.notify("Publish already running", type="warning")
                    return
                p = _pkg()
                if not p.final_review_approved or not p.is_package_complete():
                    ui.notify("Complete final review first", type="warning")
                    return
                # Sync from widget once more before resolve
                name_state["value"] = str(name_in.value or name_state.get("value") or "")
                display = resolve_display()
                if not display:
                    ui.notify("Enter a public display name", type="warning")
                    return
                name_in.value = display
                name_state["value"] = display
                p.display_name = display
                p.save()

                busy["on"] = True
                for b in (btn_free, btn_prem, btn_priv):
                    b.set_enabled(False)
                log_box.clear()
                size_host.clear()
                status.text = f"Starting {tier} publish for “{display}”…"
                log_box.push(status.text)
                log_box.push(f"Display name locked: {display}")

                async def on_progress(msg: str) -> None:
                    if _el_alive(log_box):
                        log_box.push(msg)
                    if _el_alive(status):
                        status.text = msg
                    await asyncio.sleep(0)

                try:
                    # Re-read from meta so long jobs always use the saved name
                    p2 = RallyPackage.load(p.dir())
                    p2.root = p.dir()
                    display2 = sanitize_display_name(
                        p2.display_name or display
                    )
                    result = await compress_rally_ios(
                        p2.dir(),
                        display_name=display2,
                        progress=on_progress,
                    )
                    render_size_table(
                        result.rows, result.before_total, result.after_total
                    )
                    await on_progress(
                        f"Uploading {tier} as “{display2}” → DutyCall drive…"
                    )
                    uri = await asyncio.to_thread(
                        upload_rally,
                        result.output_dir,
                        p2,
                        tier=tier,
                        display_name=display2,
                    )
                    # Keep session package in sync
                    global _current
                    _current = RallyPackage.load(p.dir())
                    await on_progress(f"Uploaded → {uri}")
                    status.text = f"Done ({tier}): {uri}"
                    name_in.value = display2
                    name_state["value"] = display2
                    ui.notify(f"Published “{display2}” to {tier}", type="positive")
                except Exception as exc:  # noqa: BLE001
                    msg = str(exc)[:500]
                    log_box.push(f"ERROR: {msg}")
                    status.text = msg
                    ui.notify(msg, type="negative")
                finally:
                    busy["on"] = False
                    for b in (btn_free, btn_prem, btn_priv):
                        b.set_enabled(True)

            with ui.row().classes("gap-3 flex-wrap q-mt-sm"):
                btn_free = ui.button(
                    "Free",
                    icon="public",
                    on_click=lambda: publish("free"),
                ).props("color=primary unelevated")
                btn_prem = ui.button(
                    "Premium",
                    icon="workspace_premium",
                    on_click=lambda: publish("premium"),
                ).props("color=secondary unelevated")
                btn_priv = ui.button(
                    "Private",
                    icon="lock",
                    on_click=lambda: publish("private"),
                ).props("outline")

            ui.label(
                "Free/Premium → folder under {tier}/rallies/{name}/ · "
                "Private → zip to private/{your id}/rallies/{name}.zip"
            ).classes("text-caption text-grey-6")

            last = (pkg.s3 or {}).get("last_upload")
            if last:
                ui.label(
                    f"Last: {last.get('tier', '?')} · "
                    f"{last.get('display_name', '')} · "
                    f"{last.get('uri', '')}"
                ).classes("text-caption text-grey-6")

            with ui.row().classes("gap-2 q-mt-md"):
                ui.button(
                    "Replay final review",
                    icon="movie",
                    on_click=lambda: _reopen_review(refresh_all),
                ).props("outline dense")
                ui.button(
                    "Back to edit",
                    icon="edit",
                    on_click=lambda: _back_to_edit(refresh_all),
                ).props("flat dense")

    def _reopen_review(refresh_all) -> None:
        p = _pkg()
        p.final_review_approved = False
        p.save()
        global _force_edit
        _force_edit = False
        refresh_all()

    async def _apply_explicit_mark(refresh_all) -> None:
        from app.pipeline.explicit_mark import apply_explicit_mark

        p = _pkg()
        preview = p.path_for("Preview.png")
        clean = p.path_for("Preview.clean.png")
        if not preview.is_file() and not clean.is_file():
            ui.notify(
                "Saved — will stamp on next Preview generate", type="info"
            )
            return
        try:
            msg = await asyncio.to_thread(apply_explicit_mark, p)
            p.save()
            ui.notify(msg, type="positive")
            refresh_all()
        except Exception as exc:  # noqa: BLE001
            ui.notify(str(exc), type="negative")

    # ── Edit mode ────────────────────────────────────────────────
    def _render_edit(refresh_all) -> None:
        pkg = _pkg()
        step_id = _selected_step
        st = pkg.step(step_id)
        if step_id == "Idle":
            st.options.setdefault("generate_mp4", True)
            st.options.setdefault("generate_png", False)
            st.options.setdefault("boomerang", False)
            st.options.setdefault("imagine_loop", False)
        if step_id == "Preview":
            st.options.setdefault("explicit_e", False)
            st.options.setdefault("explicit_e_style", "black")
            st.options.setdefault("explicit_e_hex", "")
        else:
            if pkg.path_for("Preview.png").is_file():
                st.options.setdefault("look_refs", ["Preview.png"])
        if step_id != "Preview" and (
            "duration" not in st.options or st.options.get("duration") in (
                None,
                "",
            )
        ):
            st.options.setdefault(
                "duration", imagine_settings.video_duration()
            )

        from app.pipeline.formula import (
            compile_formula,
            persist_formula_text,
        )
        from app.ui.formula_pipeline import render_formula_row
        from app.pipeline.joins import (
            cycle_join,
            end_join,
            end_path,
            idle_rest_path,
            join_label,
            start_join,
            start_path,
        )

        kit = dict(getattr(pkg, "kit", None) or {})
        kit.setdefault(
            "voices",
            [
                {"voice_id": "eve", "role": "Voice 1"},
                {"voice_id": "leo", "role": "Voice 2"},
            ],
        )
        kit.setdefault("sounds", [])
        pkg.kit = kit

        has_take = bool(
            any(
                pkg.path_for(n).is_file()
                for n in st.outputs
                if n.lower().endswith((".mp4", ".webm", ".mov", ".png"))
            )
        )
        if step_id == "Preview":
            has_take = pkg.path_for("Preview.png").is_file()

        speaker = str(st.options.get("speaker") or "").strip().lower()
        if speaker in {"invent", "none", "auto"}:
            speaker = ""
        voices = kit.get("voices") or []

        video_on_disk = any(
            pkg.path_for(n).is_file()
            for n in st.outputs
            if n.lower().endswith((".mp4", ".webm", ".mov"))
        )
        if step_id == "Preview":
            surface = "still"
        else:
            raw_surf = str(st.options.get("work_surface") or "").strip().lower()
            if raw_surf in {"still", "take"}:
                surface = raw_surf
            else:
                surface = "take" if video_on_disk else "still"

        formula_state: dict[str, str] = {}
        formula_fields: dict = {}

        def _formula_value(field, e=None) -> str:
            if isinstance(e, str):
                return e
            if e is not None:
                val = getattr(e, "value", None)
                if val is None:
                    args = getattr(e, "args", None)
                    if isinstance(args, str):
                        val = args
                    elif isinstance(args, (list, tuple)) and args:
                        val = args[0]
                if val is not None:
                    return str(val)
            try:
                return str(field.value or "")
            except Exception:  # noqa: BLE001
                return ""

        def persist_formula() -> None:
            values = dict(formula_state)
            for sid, field in list(formula_fields.items()):
                values[sid] = _formula_value(field)
            formula_state.update(values)
            persist_formula_text(_pkg(), step_id, surface, values)

        with ui.element("div").classes(
            f"rb-direct w-full surface-{surface}"
        ):
            with ui.element("div").classes("rb-direct-body"):
                take_items: list[tuple[Path, bool, str | None, str]] = []
                seen_fp: set[str] = set()
                still_ext = {".png", ".jpg", ".jpeg", ".webp"}
                vid_ext = {".mp4", ".webm", ".mov"}
                want_ext = still_ext if surface == "still" else vid_ext

                def add_take(
                    path: Path,
                    *,
                    hearted: bool = False,
                    fav_id: str | None = None,
                ) -> None:
                    if not path.is_file():
                        return
                    if path.suffix.lower() not in want_ext:
                        return
                    fp = file_fingerprint(path)
                    if not fp or fp in seen_fp:
                        return
                    seen_fp.add(fp)
                    take_items.append((path, hearted, fav_id, fp))

                if surface == "still":
                    if step_id == "Preview":
                        primary = pkg.path_for("Preview.png")
                    elif step_id == "Idle":
                        primary = pkg.path_for("IdleKeyframe.png")
                    else:
                        from app.pipeline.prompts import (
                            staged_still_name as _stem_name,
                        )

                        primary = pkg.path_for(_stem_name(step_id))
                    if primary is None or not primary.is_file():
                        primary = start_path(pkg, step_id)
                else:
                    primary = pkg.primary_media_for_step(step_id)
                live_fp = (
                    file_fingerprint(primary)
                    if primary is not None and primary.is_file()
                    else ""
                )
                hist = pkg.dir() / "history"
                # Live file first, so a history copy of the same bytes
                # is not the thumb Delete would remove.
                if primary is not None:
                    add_take(primary)
                if hist.is_dir():
                    if surface == "still":
                        if primary is not None:
                            stems = [Path(primary.name).stem]
                        elif step_id == "Idle":
                            stems = ["IdleKeyframe"]
                        elif step_id == "Preview":
                            stems = ["Preview"]
                        else:
                            from app.pipeline.prompts import (
                                staged_still_name as _stem_name2,
                            )

                            stems = [Path(_stem_name2(step_id)).stem]
                    else:
                        stems = [
                            Path(n).stem
                            for n in (st.outputs or [step_id])
                        ]
                    older: list[Path] = []
                    for stem in stems:
                        older.extend(
                            p
                            for p in hist.glob(f"{stem}.*")
                            if p.suffix.lower()
                            in {".mp4", ".webm", ".mov", ".png"}
                        )
                    older.sort(
                        key=lambda p: p.stat().st_mtime, reverse=True
                    )
                    from app.pipeline.explicit_mark import (
                        is_clean_preview_name,
                        preview_display_history_name,
                    )

                    from app.pipeline.scenes import history_is_cut_archive

                    for pth in older:
                        if ".nudge-" in pth.name or pth.stem.endswith(".stitch"):
                            continue
                        if pth.suffix.lower() in {
                            ".mp4",
                            ".webm",
                            ".mov",
                        } and history_is_cut_archive(pkg, step_id, pth):
                            continue
                        if is_clean_preview_name(pth.name):
                            disp = preview_display_history_name(pth.name)
                            sibling = hist / disp if disp else None
                            if sibling is not None and sibling.is_file():
                                continue
                        add_take(pth)

                def show_raw_prompt() -> None:
                    persist_formula()
                    s = _pkg().step(step_id)
                    raw = str(s.options.get("last_sent_prompt") or "").strip()
                    mode = str(s.options.get("last_sent_mode") or "")
                    if not raw:
                        raw = (
                            "(Not sent yet. Compiled formula:)\n\n"
                            + compile_formula(_pkg(), step_id, surface)
                        )
                    with ui.dialog() as dlg, ui.card().classes(
                        "rb-prompt-sheet"
                    ):
                        ui.label("Raw prompt sent to Imagine").classes(
                            "text-subtitle2"
                        )
                        if mode:
                            ui.label(mode).classes(
                                "text-caption text-grey-5"
                            )
                        ui.textarea(value=raw).classes(
                            "w-full rb-prompt-sheet-ta"
                        ).props("outlined readonly autogrow")
                        ui.button("Close", on_click=dlg.close).props(
                            "flat no-caps"
                        )
                    dlg.open()

                def _take_rel(path: Path) -> str:
                    root = pkg.dir().resolve()
                    try:
                        return path.resolve().relative_to(root).as_posix()
                    except ValueError:
                        return path.name

                def _take_inside(rel: str) -> Path | None:
                    rel = (rel or "").replace("\\", "/").lstrip("/")
                    if not rel or ".." in Path(rel).parts:
                        return None
                    root = pkg.dir().resolve()
                    path = (root / rel).resolve()
                    if path != root and root not in path.parents:
                        return None
                    return path if path.is_file() else None

                def _install_take(path: Path) -> None:
                    """Put a highlighted take on the step. Clears a stale approval."""
                    from app.pipeline.scenes import (
                        adopt_scene_attempt,
                        has_timeline,
                        scene_attempt_id,
                    )

                    p = _pkg()
                    sid = scene_attempt_id(step_id, path)
                    on_timeline = False
                    if sid and has_timeline(p, step_id):
                        rows = p.step(step_id).options.get("timeline")
                        on_timeline = isinstance(rows, list) and any(
                            isinstance(row, dict) and str(row.get("id")) == sid
                            for row in rows
                        )
                    if sid and on_timeline:
                        adopt_scene_attempt(p, step_id, sid, path)
                    else:
                        p.restore_history_take(step_id, path)
                    p = _pkg()
                    p.step(step_id).options.pop("shown_take", None)
                    p.save()

                shown = _take_inside(str(st.options.get("shown_take") or ""))
                shown_hits = bool(
                    shown is not None
                    and any(tp.resolve() == shown.resolve() for tp, *_ in take_items)
                )
                live_path = (
                    primary
                    if primary is not None and primary.is_file()
                    else None
                )

                def _render_post_pane() -> None:
                    ui.label("Post").classes("rb-pipe-k")
                    chosen: dict[str, Path | None] = {"path": None}
                    if take_items:
                        with ui.element("div").classes(
                            "rb-takes rb-pipe-post-takes"
                        ):
                            for tp, hearted, fav_id, fp in take_items[:24]:
                                is_live = bool(
                                    live_path is not None
                                    and tp.resolve() == live_path.resolve()
                                )
                                is_on = (
                                    shown is not None
                                    and tp.resolve() == shown.resolve()
                                ) or (not shown_hits and is_live)
                                if is_on and chosen["path"] is None:
                                    chosen["path"] = tp
                                url = _media_url(pkg, tp.name, tp)
                                cls = "rb-take"
                                if is_on:
                                    cls += " is-on"
                                if is_live:
                                    cls += " is-live"
                                approved_path = (
                                    pkg.primary_media_for_step(step_id)
                                    if st.status == StepStatus.APPROVED
                                    else None
                                )
                                is_approved = bool(
                                    approved_path is not None
                                    and approved_path.is_file()
                                    and tp.resolve() == approved_path.resolve()
                                )
                                if is_approved:
                                    cls += " is-approved"
                                cell = ui.element("div").classes(cls)
                                with cell:
                                    if tp.suffix.lower() in {
                                        ".png",
                                        ".jpg",
                                        ".jpeg",
                                        ".webp",
                                    }:
                                        ui.html(
                                            f'<img src="{url}" alt="" />',
                                            sanitize=False,
                                        )
                                    else:
                                        ui.html(
                                            f'<video src="{url}#t=0.1" muted '
                                            f"playsinline></video>",
                                            sanitize=False,
                                        )
                                    if is_approved:
                                        ui.html(
                                            '<span class="rb-take-ok" title="Approved">✓</span>',
                                            sanitize=False,
                                        )

                                def make_select(path: Path, live: bool):
                                    def _select(_e=None) -> None:
                                        p = _pkg()
                                        s = p.step(step_id)
                                        if live:
                                            s.options.pop("shown_take", None)
                                        else:
                                            s.options["shown_take"] = _take_rel(path)
                                        p.save()
                                        refresh_all()

                                    return _select

                                cell.on("click", make_select(tp, is_live))
                                cell.tooltip(
                                    "Click to highlight. Delete removes that take."
                                )

                        picked = chosen["path"]

                        def on_use() -> None:
                            path = picked
                            if path is None:
                                return
                            if (
                                live_path is not None
                                and path.resolve() == live_path.resolve()
                            ):
                                return
                            try:
                                _install_take(path)
                                refresh_all()
                            except Exception as exc:  # noqa: BLE001
                                ui.notify(str(exc), type="negative")

                        def on_delete() -> None:
                            path = picked
                            if path is None:
                                return
                            paths = [row[0] for row in take_items[:24]]
                            try:
                                idx = next(
                                    i
                                    for i, item in enumerate(paths)
                                    if item.resolve() == path.resolve()
                                )
                            except StopIteration:
                                idx = 0
                            neighbor = None
                            if idx + 1 < len(paths):
                                neighbor = paths[idx + 1]
                            elif idx > 0:
                                neighbor = paths[idx - 1]
                            was_live = bool(
                                live_path is not None
                                and path.resolve() == live_path.resolve()
                            )

                            def go() -> None:
                                try:
                                    msg = _pkg().delete_take(step_id, path)
                                except Exception as exc:  # noqa: BLE001
                                    ui.notify(str(exc), type="negative")
                                    return
                                p = _pkg()
                                s = p.step(step_id)
                                if (
                                    was_live
                                    or neighbor is None
                                    or not neighbor.is_file()
                                ):
                                    s.options.pop("shown_take", None)
                                elif (
                                    live_path is not None
                                    and neighbor.resolve() == live_path.resolve()
                                ):
                                    s.options.pop("shown_take", None)
                                else:
                                    s.options["shown_take"] = _take_rel(neighbor)
                                p.save()
                                ui.notify(msg, type="info")
                                refresh_all()

                            from app.pipeline.scenes import has_timeline

                            if was_live and has_timeline(_pkg(), step_id):
                                with ui.dialog().props("position=top").classes(
                                    "rb-confirm"
                                ) as dlg, ui.card().classes("q-pa-md"):
                                    ui.label(
                                        "Delete the cut on the timeline?"
                                    ).classes("text-subtitle2")
                                    ui.label(
                                        "This removes the current take and "
                                        "brings back the previous one."
                                    ).classes("text-caption").style(
                                        "color:#c8c9d1"
                                    )
                                    with ui.row().classes(
                                        "w-full justify-end gap-2 q-mt-md"
                                    ):
                                        ui.button(
                                            "Cancel", on_click=dlg.close
                                        ).props("flat no-caps")

                                        def confirm() -> None:
                                            dlg.close()
                                            go()

                                        ui.button(
                                            "Delete cut", on_click=confirm
                                        ).props(
                                            "unelevated no-caps color=negative"
                                        )
                                dlg.open()
                                return
                            go()

                        with ui.element("div").classes("rb-pipe-post-btns"):
                            use_btn = ui.button("Use", on_click=on_use).props(
                                "outline dense no-caps"
                            ).classes("rb-ghost")
                            already_live = bool(
                                picked is not None
                                and live_path is not None
                                and picked.resolve() == live_path.resolve()
                            )
                            use_btn.set_enabled(picked is not None and not already_live)
                            use_btn.tooltip(
                                "Puts the highlighted take on this step"
                            )
                            del_btn = ui.button(
                                "Delete", on_click=on_delete
                            ).props("outline dense no-caps color=negative")
                            del_btn.set_enabled(picked is not None)
                            del_btn.tooltip("Deletes the highlighted take")

                    if step_id == "Idle":
                        has_msg_last = pkg.path_for(
                            "Message.last.png"
                        ).is_file() or pkg.path_for("Message.mp4").is_file()
                        ui.label("Idle start").classes("rb-direct-lbl")
                        if has_msg_last:

                            def _use_msg_last() -> None:
                                from app.pipeline.joins import (
                                    apply_message_last_to_idle,
                                )

                                p = _pkg()
                                dest = apply_message_last_to_idle(p)
                                if dest is None:
                                    ui.notify(
                                        "Generate Message first",
                                        type="warning",
                                    )
                                    return
                                ui.notify(
                                    f"Idle will open on Message last → {dest.name}",
                                    type="positive",
                                )
                                refresh_all()

                            ui.button(
                                "Use Message last frame",
                                on_click=_use_msg_last,
                            ).props("outline dense no-caps").classes(
                                "rb-ghost"
                            )
                        else:
                            ui.label(
                                "Generate Message to capture its last "
                                "frame for Idle."
                            ).classes("text-caption text-grey-5")

                    if step_id == "Idle" and surface == "take":
                        ui.label("Idle").classes("rb-direct-lbl")
                        chk_png = ui.checkbox(
                            "Still only (PNG — no Idle.mp4)",
                            value=bool(st.options.get("generate_png"))
                            and not bool(st.options.get("generate_mp4", True)),
                        )

                        def on_png_only() -> None:
                            s = _pkg().step("Idle")
                            only = bool(chk_png.value)
                            s.options["generate_png"] = True if only else bool(
                                s.options.get("generate_png", False)
                            )
                            s.options["generate_mp4"] = not only
                            if only:
                                s.options["generate_png"] = True
                            _pkg().save()

                        chk_png.on("update:model-value", on_png_only)
                        chk_loop = ui.checkbox(
                            "Ask Imagine to loop (first = last)",
                            value=bool(st.options.get("imagine_loop", False)),
                        )
                        chk_boom = ui.checkbox(
                            "Boomerang (forward + reverse, mute)",
                            value=bool(st.options.get("boomerang", False)),
                        )

                        def on_loop() -> None:
                            persist_formula()
                            s = _pkg().step("Idle")
                            s.options["imagine_loop"] = bool(chk_loop.value)
                            _pkg().save()
                            refresh_all()

                        async def on_boom(_e=None) -> None:
                            p = _pkg()
                            s = p.step("Idle")
                            want = bool(chk_boom.value)
                            prev = bool(s.options.get("boomerang", False))
                            video = p.path_for("Idle.mp4")
                            raw = p.path_for("Idle.raw.mp4")
                            if not video.is_file() and not raw.is_file():
                                s.options["boomerang"] = want
                                p.save()
                                ui.notify(
                                    "Saved — will apply on next Idle generate",
                                    type="info",
                                )
                                return
                            try:
                                from app.pipeline.boomerang import (
                                    apply_idle_boomerang,
                                )
                                msg = await asyncio.to_thread(
                                    apply_idle_boomerang, p, want
                                )
                                p.save()
                                ui.notify(msg, type="positive")
                                refresh_all()
                            except Exception as exc:
                                s.options["boomerang"] = prev
                                chk_boom.value = prev
                                p.save()
                                ui.notify(str(exc), type="negative")

                        chk_loop.on("update:model-value", on_loop)
                        chk_boom.on("update:model-value", on_boom)

                    e_open = None
                    if step_id == "Preview":
                        from app.pipeline.explicit_mark import parse_hex_color

                        with ui.dialog() as e_dlg, ui.card().classes(
                            "rb-nudge-sheet"
                        ):
                            ui.label("Explicit").classes("text-subtitle2")
                            chk_e = ui.checkbox(
                                "E overlay (top-left)",
                                value=bool(st.options.get("explicit_e")),
                            )
                            e_style = str(
                                st.options.get("explicit_e_style") or "black"
                            ).lower()
                            e_hex = str(st.options.get("explicit_e_hex") or "")
                            hex_on = parse_hex_color(e_hex) is not None
                            with ui.element("div").classes("rb-chip-row"):
                                for style_key, style_lab in (
                                    ("white", "White"),
                                    ("black", "Black"),
                                ):
                                    on = (not hex_on) and e_style == style_key
                                    chip = ui.element("div").classes(
                                        "rb-chip" + (" is-on" if on else "")
                                    )
                                    with chip:
                                        ui.element("div").classes(
                                            "av rb-e-" + style_key
                                        )
                                        ui.label(style_lab)

                                    async def _pick_style(
                                        _e=None, key=style_key
                                    ) -> None:
                                        s = _pkg().step("Preview")
                                        s.options["explicit_e"] = True
                                        s.options["explicit_e_style"] = key
                                        s.options["explicit_e_hex"] = ""
                                        _pkg().save()
                                        await _apply_explicit_mark(refresh_all)

                                    chip.on("click", _pick_style)
                            hex_in = (
                                ui.input(
                                    placeholder="#hex  ·  Enter",
                                    value=e_hex,
                                )
                                .props("dense outlined")
                                .classes("w-full")
                            )
                            ui.label(
                                "After-effect on Preview.png only. Videos "
                                "generate from the clean still."
                            ).classes("text-caption text-grey-5")

                            async def on_e_toggle(_e=None) -> None:
                                p = _pkg()
                                s = p.step("Preview")
                                s.options["explicit_e"] = bool(chk_e.value)
                                p.save()
                                await _apply_explicit_mark(refresh_all)

                            chk_e.on("update:model-value", on_e_toggle)

                            async def on_hex(_e=None) -> None:
                                raw = str(hex_in.value or "").strip()
                                s = _pkg().step("Preview")
                                if raw and parse_hex_color(raw) is None:
                                    ui.notify(
                                        "Use #RGB or #RRGGBB", type="warning"
                                    )
                                    return
                                s.options["explicit_e_hex"] = raw
                                if raw:
                                    s.options["explicit_e"] = True
                                _pkg().save()
                                await _apply_explicit_mark(refresh_all)

                            hex_in.on("keydown.enter", on_hex)

                        e_open = e_dlg.open

                    with ui.element("div").classes("rb-pipe-post-btns"):
                        ui.button(
                            "Last sent prompt",
                            icon="bug_report",
                            on_click=show_raw_prompt,
                        ).props("outline dense no-caps").classes("rb-ghost")
                        if e_open is not None:
                            ui.button("E", on_click=e_open).props(
                                "outline dense no-caps"
                            ).classes("rb-ghost")


                if (st.error or "").strip():
                    banner = ui.element("div").classes(
                        "rb-callout rb-callout-warn w-full rb-callout-dismiss"
                    )
                    with banner:
                        ui.label(st.error).classes(
                            "text-caption rb-callout-msg"
                        ).style("white-space: pre-wrap")

                        def _dismiss_err() -> None:
                            p = _pkg()
                            s = p.step(step_id)
                            s.error = None
                            p.save()
                            banner.set_visibility(False)

                        ui.button(
                            "Dismiss", on_click=_dismiss_err
                        ).props(
                            "flat dense no-caps size=sm color=white"
                        ).classes("rb-callout-x")

                with ui.element("div").classes("rb-pipe-row"):
                    render_formula_row(
                        pkg,
                        step_id,
                        surface,
                        persist_formula=persist_formula,
                        refresh_all=refresh_all,
                        pkg_fn=_pkg,
                        media_url=_media_url,
                        speaker=speaker,
                        voices=voices,
                        kit=kit,
                        imagine_settings=imagine_settings,
                        render_post=_render_post_pane,
                    )
                    with ui.element("div").classes("rb-pipe-out"):
                      with ui.element("div").classes("rb-out-col"):
                        with ui.element("div").classes("rb-direct-phone"):
                          with ui.element("div").classes("rb-stage"):
                            _media_stage(
                                pkg,
                                step_id,
                                refresh_all,
                                prefer_still=(surface == "still"),
                            )


                        def save_line() -> None:
                            persist_formula()

                        def launch_job(kind: str, delta: str = "") -> None:
                            if kind not in {"nudge", "tweak"}:
                                persist_formula()
                            sid = step_id
                            job_pkg = _pkg()
                            rally = job_pkg.name
                            key = _job_key(rally, sid)
                            if _is_generating(rally, sid):
                                ui.notify("Already generating this step", type="warning")
                                return
                            ok, reason = graph.can_generate(job_pkg, sid)
                            if not ok:
                                ui.notify(reason, type="warning")
                                return
                            delta = (delta or "").strip()
                            if kind in {"nudge", "tweak"} and not delta:
                                ui.notify(
                                    "Say the one thing to change",
                                    type="warning",
                                )
                                return
                            if kind == "take":
                                from app.pipeline.scenes import (
                                    cut_is_showing as _showing_cut,
                                    has_timeline as _has_cut,
                                )

                                if _has_cut(job_pkg, sid) and not _showing_cut(
                                    job_pkg, sid
                                ):
                                    ui.notify(
                                        "Select the edited take to replace a scene. "
                                        "This copy is uncut.",
                                        type="warning",
                                    )
                                    return

                            async def job() -> None:
                                async def on_progress(msg: str) -> None:
                                    _gen_detail[key] = msg
                                    await asyncio.sleep(0)

                                try:
                                    if kind == "nudge":
                                        from app.pipeline.scenes import (
                                            cut_is_showing as _showing_cut,
                                            has_timeline as _kept_cut,
                                        )

                                        kept_cut = _kept_cut(job_pkg, sid)
                                        showing_cut = _showing_cut(job_pkg, sid)
                                        await nudge_step(
                                            job_pkg, sid, delta, progress=on_progress
                                        )
                                        if kept_cut and showing_cut:
                                            nudge_detail = (
                                                "Cut kept — nudge is under this clip"
                                            )
                                        elif kept_cut:
                                            nudge_detail = (
                                                "Original kept — nudge is a new take. "
                                                "The cut is unchanged."
                                            )
                                        else:
                                            nudge_detail = ""
                                        _push_notice(
                                            "success",
                                            f"{rally} · {_step_label(sid)} nudged",
                                            detail=nudge_detail,
                                            rally=rally,
                                            step=sid,
                                        )
                                    elif kind == "tweak":
                                        dest = await tweak_still(
                                            job_pkg,
                                            sid,
                                            delta,
                                            progress=on_progress,
                                        )
                                        _push_notice(
                                            "success",
                                            f"{rally} · {_step_label(sid)} "
                                            f"tweaked ({dest.name})",
                                            rally=rally,
                                            step=sid,
                                        )
                                    elif kind == "still":
                                        dest = await generate_still(
                                            job_pkg, sid, progress=on_progress
                                        )
                                        _push_notice(
                                            "success",
                                            f"{rally} · {_step_label(sid)} still "
                                            f"ready ({dest.name})",
                                            rally=rally,
                                            step=sid,
                                        )
                                    else:
                                        await generate_step(
                                            job_pkg, sid, progress=on_progress
                                        )
                                        _push_notice(
                                            "success",
                                            f"{rally} · {_step_label(sid)} ready",
                                            rally=rally,
                                            step=sid,
                                        )
                                except asyncio.CancelledError:
                                    pass
                                except Exception as exc:  # noqa: BLE001
                                    _push_notice(
                                        "error",
                                        f"{rally} · {_step_label(sid)} failed",
                                        detail=str(exc)[:200],
                                        rally=rally,
                                        step=sid,
                                    )
                                finally:
                                    _gen_tasks.pop(key, None)
                                    _gen_detail.pop(key, None)
                                    if (
                                        _current is not None
                                        and _current.name == rally
                                    ):
                                        if _current is not job_pkg:
                                            _sync_current_from_disk(
                                                rally, job_pkg.dir()
                                            )
                                        try:
                                            refresh_all()
                                        except Exception:  # noqa: BLE001
                                            pass

                            _gen_tasks[key] = asyncio.create_task(job())
                            _gen_detail[key] = {
                                "nudge": "Nudging…",
                                "still": "Staging still…",
                                "tweak": "Tweaking still…",
                            }.get(kind, "Starting…")
                            verb = {
                                "nudge": "Nudge",
                                "still": "Still",
                                "tweak": "Tweak",
                            }.get(kind, "Generating")
                            _push_notice(
                                "info",
                                f"{verb} {rally} · {_step_label(sid)}",
                                rally=rally,
                                step=sid,
                            )
                            refresh_all()

                        already = _is_generating(pkg.name, step_id)
                        can_a, _ = graph.can_approve(pkg, step_id)
                        video_exists = any(
                            pkg.path_for(n).is_file()
                            for n in st.outputs
                            if n.lower().endswith((".mp4", ".webm", ".mov"))
                        )
                        if not video_exists:
                            hist = pkg.dir() / "history"
                            if hist.is_dir():
                                for n in st.outputs:
                                    if not n.lower().endswith(
                                        (".mp4", ".webm", ".mov")
                                    ):
                                        continue
                                    stem = Path(n).stem
                                    suf = Path(n).suffix.lower()
                                    if any(
                                        p.suffix.lower() == suf
                                        for p in hist.glob(f"{stem}.*")
                                    ):
                                        video_exists = True
                                        break

                        opening_still = None
                        if step_id == "Preview":
                            opening_still = pkg.path_for("Preview.png")
                        elif step_id == "Idle":
                            opening_still = pkg.path_for("IdleKeyframe.png")
                        else:
                            from app.pipeline.prompts import staged_still_name as _ssn

                            opening_still = pkg.path_for(_ssn(step_id))
                        has_opening = bool(
                            opening_still is not None and opening_still.is_file()
                        )

                        show_tweak = surface == "still" and (
                            step_id == "Preview" or has_opening
                        )
                        show_nudge = surface == "take" and video_exists

                        with ui.element("div").classes("rb-pipe-acts"):
                            if show_tweak:
                                with ui.dialog() as tweak_dlg, ui.card().classes(
                                    "rb-nudge-sheet"
                                ):
                                    ui.label("Tweak this still").classes(
                                        "text-subtitle2"
                                    )
                                    ui.label(
                                        "Imagine image-edits the still on the stage. "
                                        "Name only what to change — the rest stays."
                                    ).classes("text-caption text-grey-5")
                                    tweak_ta = (
                                        ui.textarea(
                                            placeholder=(
                                                "e.g. fix the extra fingers, "
                                                "keep everything else"
                                            )
                                        )
                                        .classes("w-full rb-nudge-sheet-ta")
                                        .props(
                                            "outlined autogrow autocapitalize=none "
                                            "autocomplete=off autocorrect=off "
                                            "spellcheck=false"
                                        )
                                    )
                                    with ui.row().classes(
                                        "w-full justify-end gap-2 q-mt-sm"
                                    ):
                                        ui.button(
                                            "Cancel",
                                            on_click=tweak_dlg.close,
                                        ).props("flat no-caps")

                                        def apply_tweak() -> None:
                                            text = (tweak_ta.value or "").strip()
                                            if not text:
                                                ui.notify(
                                                    "Say the one thing to change",
                                                    type="warning",
                                                )
                                                return
                                            tweak_dlg.close()
                                            launch_job("tweak", text)

                                        ui.button(
                                            "Apply tweak",
                                            on_click=apply_tweak,
                                        ).props(
                                            "unelevated no-caps color=secondary"
                                        )

                                def on_tweak() -> None:
                                    tweak_ta.set_value("")
                                    tweak_dlg.open()

                            if show_nudge:
                                with ui.dialog() as nudge_dlg, ui.card().classes(
                                    "rb-nudge-sheet"
                                ):
                                    from app.pipeline.scenes import (
                                        bar_items as _scene_items,
                                        has_timeline as _has_scenes,
                                    )

                                    _scene_only = (
                                        _has_scenes(pkg, step_id)
                                        and len(_scene_items(pkg, step_id)) > 1
                                    )
                                    ui.label(
                                        "Nudge this scene"
                                        if _scene_only
                                        else "Nudge this take"
                                    ).classes("text-subtitle2")
                                    ui.label(
                                        "Imagine edits this scene only. "
                                        "The other scenes stay. "
                                        "Uses Classic video (1.5 cannot edit takes)."
                                        if _scene_only
                                        else (
                                            "Imagine edits the current video. "
                                            "One change — don’t rewrite the shot. "
                                            "Uses Classic video (1.5 cannot edit takes)."
                                        )
                                    ).classes("text-caption text-grey-5")
                                    nudge_ta = (
                                        ui.textarea(
                                            placeholder=(
                                                "e.g. rotors only — don’t recast "
                                                "the pilot"
                                            )
                                        )
                                        .classes("w-full rb-nudge-sheet-ta")
                                        .props(
                                            "outlined autogrow autocapitalize=none "
                                            "autocomplete=off autocorrect=off "
                                            "spellcheck=false"
                                        )
                                    )
                                    with ui.row().classes(
                                        "w-full justify-end gap-2 q-mt-sm"
                                    ):
                                        ui.button(
                                            "Cancel",
                                            on_click=nudge_dlg.close,
                                        ).props("flat no-caps")

                                        def apply_nudge() -> None:
                                            text = (nudge_ta.value or "").strip()
                                            if not text:
                                                ui.notify(
                                                    "Say the one thing to change",
                                                    type="warning",
                                                )
                                                return
                                            nudge_dlg.close()
                                            launch_job("nudge", text)

                                        ui.button(
                                            "Apply nudge",
                                            on_click=apply_nudge,
                                        ).props(
                                            "unelevated no-caps color=secondary"
                                        )

                                def on_nudge() -> None:
                                    nudge_ta.set_value(
                                        str(st.options.get("last_nudge") or "")
                                    )
                                    nudge_dlg.open()

                            if step_id != "Preview" and surface == "still":
                                sb = (
                                    ui.button(
                                        "Make still",
                                        on_click=lambda: launch_job("still"),
                                    )
                                    .props("unelevated no-caps color=secondary")
                                    .classes("rb-go rb-still-only")
                                )
                                sb.set_enabled(not already)
                                sb.tooltip(
                                    "Image-edit the selected Edit from still "
                                    "into this clip’s opening."
                                )
                            elif step_id == "Preview" or surface == "take":
                                primary_lab = (
                                    "Make still"
                                    if step_id == "Preview"
                                    else (
                                        "New take" if video_exists else "Make take"
                                    )
                                )
                                pb = ui.button(
                                    primary_lab,
                                    on_click=lambda: launch_job("take"),
                                ).props(
                                    "unelevated no-caps color=secondary"
                                ).classes("rb-go")
                                pb.set_enabled(not already)
                                from app.pipeline.scenes import (
                                    has_timeline as _step_has_scenes,
                                )

                                if step_id != "Preview" and _step_has_scenes(
                                    pkg, step_id
                                ):
                                    from app.pipeline.scenes import (
                                        cut_is_showing as _showing_cut,
                                    )

                                    pb.tooltip(
                                        "Replaces the selected scene"
                                        if _showing_cut(pkg, step_id)
                                        else (
                                            "Select the edited take to replace "
                                            "a scene"
                                        )
                                    )

                            def on_approve() -> None:
                                save_line()
                                try:
                                    p = _pkg()
                                    s = p.step(step_id)
                                    shown_path = _take_inside(
                                        str(s.options.get("shown_take") or "")
                                    )
                                    live_now = (
                                        primary
                                        if primary is not None and primary.is_file()
                                        else None
                                    )
                                    from app.pipeline.scenes import (
                                        clip_attempt_scene as _clip_attempt,
                                    )

                                    previewing_clip = (
                                        shown_path is not None
                                        and _clip_attempt(p, step_id, shown_path)
                                        is not None
                                    )
                                    if (
                                        shown_path is not None
                                        and shown_path.is_file()
                                        and not previewing_clip
                                        and (
                                            live_now is None
                                            or shown_path.resolve()
                                            != live_now.resolve()
                                        )
                                    ):
                                        _install_take(shown_path)
                                        p = _pkg()
                                    graph.approve_step(p, step_id)
                                    ui.notify(
                                        f"{_step_label(step_id)} approved",
                                        type="positive",
                                    )
                                except Exception as exc:  # noqa: BLE001
                                    ui.notify(str(exc), type="negative")
                                refresh_all()

                            with ui.element("div").classes("rb-pipe-acts-row"):
                                if show_tweak:
                                    tb = ui.button(
                                        "Tweak", on_click=on_tweak
                                    ).props("outline no-caps").classes(
                                        "rb-ghost rb-still-only"
                                    )
                                    tb.set_enabled(not already)
                                    tb.tooltip(
                                        "Edit this still — keep 90%, change one thing"
                                    )
                                elif show_nudge:
                                    nb = ui.button(
                                        "Nudge", on_click=on_nudge
                                    ).props("outline no-caps").classes("rb-ghost")
                                    nb.set_enabled(not already)
                                    nb.tooltip(
                                        "Edit this scene — one change"
                                        if _scene_only
                                        else "Edit this take — one change"
                                    )
                                ab = ui.button(
                                    "Approve", on_click=on_approve
                                ).props("outline no-caps").classes("rb-ok")
                                other = _take_inside(
                                    str(st.options.get("shown_take") or "")
                                )
                                live_ok = (
                                    primary is not None and primary.is_file()
                                )
                                approving_other = False
                                from app.pipeline.scenes import (
                                    clip_attempt_scene as _clip_attempt,
                                )

                                previewing_clip = (
                                    other is not None
                                    and _clip_attempt(pkg, step_id, other) is not None
                                )
                                if (
                                    other is not None
                                    and other.is_file()
                                    and not previewing_clip
                                ):
                                    if (
                                        not live_ok
                                        or other.resolve() != primary.resolve()
                                    ):
                                        approving_other = (
                                            not live_ok
                                            or file_fingerprint(other)
                                            != file_fingerprint(primary)
                                        )
                                ab.set_enabled(
                                    (can_a or approving_other) and not already
                                )
                                if previewing_clip:
                                    ab.tooltip(
                                        "Approves the cut on the timeline, "
                                        "not this preview"
                                    )
                                elif approving_other:
                                    ab.tooltip("Approves the highlighted take")
                                else:
                                    from app.pipeline.scenes import (
                                        has_timeline as _approve_cut,
                                    )

                                    if _approve_cut(pkg, step_id):
                                        ab.tooltip(
                                            "Approves the combined take. "
                                            "The clips in the bar are already in it."
                                        )

        def _enter_final_review(refresh_all) -> None:
            global _force_edit
            _force_edit = False
            p = _pkg()
            if p.final_review_approved:
                p.final_review_approved = False
                p.save()
            refresh_all()

    # ── Media helpers ────────────────────────────────────────────
    def _media_url(pkg: RallyPackage, filename: str, path: Path) -> str:
        if path.is_file():
            st = path.stat()
            bust = f"{int(st.st_mtime * 1000)}-{st.st_size}"
        else:
            bust = "0"
        try:
            rel = str(path.resolve().relative_to(pkg.dir().resolve()))
        except ValueError:
            rel = filename
        return f"{media_url(pkg, rel)}?t={bust}"

    def _rel_in_pkg(pkg: RallyPackage, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(pkg.dir().resolve()))
        except ValueError:
            return path.name

    def _start_file_path(pkg: RallyPackage, step_id: str) -> Path:
        """Still-surface media: the clip's Make-still file, not take join."""
        from app.pipeline.prompts import staged_still_name

        if step_id == "Preview":
            return pkg.path_for("Preview.png")
        if step_id == "Idle":
            p = pkg.path_for("IdleKeyframe.png")
            if p.is_file():
                return p
        else:
            p = pkg.path_for(staged_still_name(step_id))
            if p.is_file():
                return p
            custom = str(
                pkg.step(step_id).options.get("start_file") or ""
            ).strip()
            if custom:
                c = pkg.path_for(custom)
                if c.is_file():
                    return c
        from app.pipeline.joins import start_path as join_start

        sp = join_start(pkg, step_id)
        if sp is not None:
            return sp
        return pkg.path_for("Preview.png")

    def _base_still_choices(
        pkg: RallyPackage, step_id: str
    ) -> list[tuple[str, Path]]:
        """Label + path for stills the user can pick as the video's first frame."""
        rows: list[tuple[str, Path]] = []
        seen: set[str] = set()

        def add(label: str, path: Path) -> None:
            if not path.is_file():
                return
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen:
                return
            seen.add(key)
            rows.append((label, path))

        from app.pipeline.explicit_mark import preview_source_path

        preview_disp = pkg.path_for("Preview.png")
        add(
            "Preview",
            preview_disp if preview_disp.is_file() else preview_source_path(pkg),
        )
        add("Idle", pkg.path_for("IdleKeyframe.png"))
        if step_id not in {"Preview", "Idle"}:
            add("Idle last", pkg.path_for("Idle.last.png"))
            add("This clip", pkg.path_for(staged_still_name(step_id)))
            hist = pkg.dir() / "history"
            stem = Path(staged_still_name(step_id)).stem
            if hist.is_dir():
                older = sorted(
                    hist.glob(f"{stem}.*.png"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                for i, path in enumerate(older[:4], start=1):
                    add(f"Earlier {i}", path)
        elif step_id == "Idle":
            hist = pkg.dir() / "history"
            if hist.is_dir():
                older = sorted(
                    hist.glob("IdleKeyframe.*.png"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                for i, path in enumerate(older[:4], start=1):
                    add(f"Earlier {i}", path)
        return rows

    def _mode_for_still(pkg: RallyPackage, step_id: str, path: Path) -> str:
        name = path.name
        if name in {"Preview.png", "Preview.clean.png"}:
            return "preview"
        if name == "IdleKeyframe.png":
            return "idle"
        if name == "Idle.last.png":
            return "idle_last"
        return "staged"

    def _render_favorites_panel(
        pkg: RallyPackage, step_id: str, refresh_all
    ) -> None:
        """List hearted takes with restore / delete."""
        st = pkg.step(step_id)
        favs = list(st.favorites)
        with ui.element("div").classes("rb-field"):
            with ui.row().classes(
                "w-full items-center justify-between no-wrap"
            ):
                with ui.column().classes("gap-0"):
                    ui.label("Favorites").classes("rb-field-label")
                    ui.label(
                        "Heart a take to keep it while you hunt for better"
                    ).classes("rb-field-hint")
                if favs:
                    ui.badge(str(len(favs))).props("color=pink dense")

            if not favs:
                ui.label("No hearted takes yet.").classes(
                    "text-caption text-grey-6"
                )
                return

            for fav in favs:
                primary_rel = fav.primary_path()
                primary = (
                    pkg.path_for(primary_rel) if primary_rel else None
                )

                def restore(fid: str = fav.id) -> None:
                    try:
                        pkg2 = _pkg()
                        pkg2.restore_favorite(step_id, fid)
                        ui.notify(
                            "Restored favorite — ready to review",
                            type="positive",
                        )
                        refresh_all()
                    except Exception as exc:  # noqa: BLE001
                        ui.notify(str(exc), type="negative")

                def remove(fid: str = fav.id) -> None:
                    try:
                        _pkg().delete_favorite(step_id, fid)
                        ui.notify("Favorite removed", type="info")
                        refresh_all()
                    except Exception as exc:  # noqa: BLE001
                        ui.notify(str(exc), type="negative")

                with ui.element("div").classes("rb-fav-card"):
                    with ui.row().classes(
                        "w-full items-center gap-3 no-wrap"
                    ):
                        if primary is not None and primary.is_file():
                            url = _media_url(
                                pkg, primary.name, primary
                            )
                            suf = primary.suffix.lower()
                            style = (
                                "height:56px;width:32px;object-fit:cover;"
                                "border-radius:6px;background:#111;"
                            )
                            if suf in {
                                ".png",
                                ".jpg",
                                ".jpeg",
                                ".webp",
                                ".gif",
                            }:
                                ui.html(
                                    f'<img src="{url}" alt="" '
                                    f'style="{style}" />',
                                    sanitize=False,
                                )
                            else:
                                ui.html(
                                    f'<video src="{url}" style="{style}" '
                                    f"muted playsinline></video>",
                                    sanitize=False,
                                )
                        else:
                            ui.icon("broken_image", size="28px").classes(
                                "text-grey-7"
                            )

                        with ui.column().classes("gap-0 col").style(
                            "min-width: 0;"
                        ):
                            label = fav.note or fav.id
                            ui.label(label).classes(
                                "text-caption text-weight-medium"
                            ).style(
                                "overflow: hidden; text-overflow: ellipsis; "
                                "white-space: nowrap;"
                            )
                            when = (fav.at or "")[:16].replace("T", " ")
                            ui.label(when).classes(
                                "text-caption text-grey-6"
                            )

                        ui.button(
                            icon="replay", on_click=restore
                        ).props(
                            "flat dense round color=primary size=sm"
                        ).tooltip("Restore as current")
                        ui.button(
                            icon="delete_outline", on_click=remove
                        ).props(
                            "flat dense round color=grey size=sm"
                        ).tooltip("Remove favorite")

    def _render_media_el(
        url: str,
        path: Path,
        *,
        primary: bool = False,
        thumb_style: str | None = None,
        autoplay: bool = False,
    ) -> None:
        suf = path.suffix.lower()
        if primary:
            cls = "rb-stage-media"
            if suf in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                ui.html(
                    f'<img class="{cls}" src="{url}" alt="{path.name}" loading="eager" />',
                    sanitize=False,
                )
            elif suf in {".mp4", ".webm", ".mov"}:
                play = 'data-rb-autoplay="1" ' if autoplay else ""
                ui.html(
                    f'<video class="rally-stage-video {cls}" src="{url}" '
                    f"controls {play}loop playsinline muted></video>",
                    sanitize=False,
                )
            else:
                ui.label(path.name).classes("text-negative")
        else:
            style = thumb_style or (
                "height:48px;width:27px;object-fit:cover;border-radius:4px;"
            )
            if suf in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                ui.html(
                    f'<img src="{url}" alt="" style="{style}" />', sanitize=False
                )
            else:
                ui.html(
                    f'<video src="{url}" style="{style}" muted playsinline></video>',
                    sanitize=False,
                )

    def _apply_mute_memory() -> None:
        ui.run_javascript(
            """
            (() => {
              const KEY = 'rallyBuilder.videoMuted';
              const prefer = localStorage.getItem(KEY);
              const muted = prefer === null ? true : prefer === '1';
              const editorOpen = !!document.getElementById('rb-scene-shade');
              if (!window.__rbStageWatch) {
                window.__rbStageWatch = true;
                window.__rbStageMemo = null;
                const remember = (v) => {
                  if (!v || !v.classList || !v.classList.contains('rally-stage-video')) return;
                  window.__rbStageMemo = {
                    src: v.getAttribute('src') || '',
                    t: v.currentTime || 0,
                    paused: v.paused,
                  };
                };
                document.addEventListener('timeupdate', (e) => remember(e.target), true);
                document.addEventListener('pause', (e) => remember(e.target), true);
                document.addEventListener('play', (e) => remember(e.target), true);
                document.addEventListener('seeking', (e) => remember(e.target), true);
              }
              document.querySelectorAll('video, audio').forEach((el) => {
                if (!editorOpen) return;
                if (el.closest('#rb-scene-shade')) return;
                try { el.pause(); el.autoplay = false; } catch (e) {}
              });
              document.querySelectorAll('video.rally-stage-video').forEach((v) => {
                v.muted = muted;
                if (!v.dataset.rbMuteBound) {
                  v.dataset.rbMuteBound = '1';
                  v.addEventListener('volumechange', () => {
                    localStorage.setItem(
                      KEY,
                      (v.muted || v.volume === 0) ? '1' : '0'
                    );
                  });
                }
                const memo = window.__rbStageMemo;
                const same = memo && memo.src && memo.src === (v.getAttribute('src') || '');
                if (editorOpen) {
                  v.pause();
                } else if (v.dataset.rbAutoplay === '1') {
                  v.play().catch(() => {});
                } else if (same) {
                  const resumeAt = memo.t || 0;
                  const park = () => {
                    if (resumeAt > 0.05) {
                      try { v.currentTime = resumeAt; } catch (e) {}
                    }
                    if (!memo.paused) v.play().catch(() => {});
                  };
                  if (v.readyState >= 1) park();
                  else v.addEventListener('loadedmetadata', park, { once: true });
                }
              });
            })();
            """
        )

    def _media_stage(
        pkg: RallyPackage,
        step_id: str,
        refresh_all=None,
        *,
        prefer_still: bool = False,
    ) -> None:
        st = pkg.step(step_id)
        paths = pkg.existing_outputs(step_id)
        if not paths and step_id == "Preview":
            p = pkg.path_for("Preview.png")
            if p.is_file():
                paths = [p]
        if not paths:
            for name in st.outputs:
                p = pkg.path_for(name)
                if p.is_file():
                    paths.append(p)
        if step_id != "Preview":
            still = _start_file_path(pkg, step_id)
            if still.is_file():
                existing = {str(p.resolve()) for p in paths if p.is_file()}
                if str(still.resolve()) not in existing:
                    paths.append(still)

        with ui.element("div").classes("rb-stage-inner"):
            if not paths:
                with ui.column().classes("items-center justify-center gap-2").style(
                    "border:2px dashed rgba(255,255,255,0.12);border-radius:16px;"
                    "padding:1.5rem;"
                ):
                    ui.icon("smartphone", size="36px").classes("text-grey-7")
                    ui.label("No media yet").classes("text-body1 text-grey-5")
                return

            primary = paths[0]
            if prefer_still:
                still = _start_file_path(pkg, step_id)
                if still.is_file():
                    primary = still
                else:
                    for p in paths:
                        if p.suffix.lower() in {
                            ".png",
                            ".jpg",
                            ".jpeg",
                            ".webp",
                        }:
                            primary = p
                            break
            else:
                for p in paths:
                    if p.suffix.lower() in {".mp4", ".webm", ".mov"}:
                        primary = p
                        break
            secondaries = [x for x in paths if x != primary]
            # The phone plays the stitched take. A highlighted history take previews instead.
            shown_rel = str(st.options.get("shown_take") or "").replace("\\", "/").lstrip("/")
            if shown_rel and ".." not in Path(shown_rel).parts:
                root = pkg.dir().resolve()
                shown_path = (root / shown_rel).resolve()
                if (
                    shown_path.is_file()
                    and root in shown_path.parents
                    and shown_path != primary.resolve()
                ):
                    primary = shown_path
                    secondaries = []

            with ui.element("div").classes("rb-primary-slot"):
                _render_media_el(
                    _media_url(pkg, primary.name, primary),
                    primary,
                    primary=True,
                    autoplay=_stage_should_autoplay(pkg, step_id, primary),
                )

            if secondaries:
                with ui.row().classes("gap-2 flex-wrap justify-center rb-thumbs"):
                    for path in secondaries:
                        _render_media_el(
                            _media_url(pkg, path.name, path),
                            path,
                            primary=False,
                        )
        _apply_mute_memory()

    def _open_doctor() -> None:
        checks = run_doctor()
        with ui.dialog() as dialog, ui.card().classes("q-pa-md").style(
            "min-width: 420px"
        ):
            ui.label("Doctor").classes("text-h6")
            for c in checks:
                icon = "check_circle" if c.ok else "cancel"
                color = "positive" if c.ok else "negative"
                if "S3" in c.name and not c.ok:
                    icon, color = "warning", "warning"
                with ui.row().classes("items-center gap-2"):
                    ui.icon(icon, color=color)
                    with ui.column().classes("gap-0"):
                        ui.label(c.name).classes("text-weight-medium")
                        ui.label(c.detail).classes("text-caption text-grey")
            ui.button("Close", on_click=dialog.close).props("flat")
        dialog.open()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    app.add_static_files("/media", str(OUTPUT_DIR))


_HEAD_CSS = """
<style>
  /* Home page: normal document scroll. Studio: .rb-shell locks the viewport. */
  html, body {
    margin: 0 !important;
    min-height: 100%;
    background: #121212 !important;
  }
  body.rb-studio-active {
    overflow: hidden !important;
    height: 100% !important;
  }
  body.rb-studio-active .q-page-container,
  body.rb-studio-active .q-page,
  body.rb-studio-active .nicegui-content {
    padding: 0 !important;
    margin: 0 !important;
    min-height: 0 !important;
    height: 100% !important;
    max-height: 100% !important;
    overflow: hidden !important;
  }
  /* Home: allow page scroll; light padding from Quasar is fine */
  body:not(.rb-studio-active) .q-page {
    overflow: auto !important;
    min-height: 100%;
  }
  .rb-home {
    padding-bottom: 3rem;
  }
  .rb-brief {
    padding-bottom: 5rem;
  }
  .rb-brief-step {
    background: #1A1A1A;
    border: 1px solid #2C2C2C;
    border-radius: 10px;
    padding: 0.85rem 1rem;
  }
  .rb-brief-sticky {
    position: sticky;
    bottom: 0;
    z-index: 5;
    background: #111111;
    border-top: 1px solid #2C2C2C;
    padding: 0.75rem 0 0.5rem;
  }
  /* Home landing */
  .dc-header {
    background: #111111 !important;
    border-bottom: 1px solid #2C2C2C;
  }
  /* Top-right Activity (queue + notices) */
  .rb-activity {
    position: relative;
    display: inline-flex;
    align-items: center;
  }
  .rb-activity-btn {
    position: relative;
  }
  .rb-activity-badge {
    font-size: 0.65rem !important;
    min-height: 1rem !important;
    padding: 0 4px !important;
  }
  .rb-activity-menu {
    min-width: 20rem !important;
    max-width: min(22rem, 92vw) !important;
    background: #1A1A1A !important;
    border: 1px solid #3A3A3A;
    border-radius: 10px !important;
  }
  .rb-activity-menu .q-item {
    min-height: 2.5rem;
  }
  .dc-card {
    background: #2C2C2C;
    border: 1px solid #3A3A3A;
    border-radius: 12px;
    box-shadow: 0 8px 24px rgba(18, 121, 135, 0.18);
  }
  .dc-field-title {
    font-size: 0.95rem;
    font-weight: 600;
    color: #E8E8E8;
    margin-bottom: 6px;
  }
  .dc-hint {
    font-size: 0.75rem;
    color: #9E9E9E;
    margin-top: 4px;
    line-height: 1.35;
  }
  .dc-input .q-field__control {
    background: #1A1A1A !important;
    border-radius: 10px;
  }
  .dc-cta {
    border-radius: 12px !important;
    font-weight: 600 !important;
    min-height: 48px !important;
  }
  .dc-rally-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 14px;
    width: 100%;
  }
  .dc-rally-card {
    position: relative;
    background: #2C2C2C;
    border: 1px solid #3A3A3A;
    border-radius: 12px;
    overflow: hidden;
    cursor: pointer;
    transition: border-color 0.15s ease, box-shadow 0.15s ease, transform 0.15s ease;
  }
  .dc-rally-card:hover {
    border-color: #127987;
    box-shadow: 0 6px 20px rgba(18, 121, 135, 0.35);
    transform: translateY(-2px);
  }
  .dc-rally-thumb {
    display: block;
    width: 100%;
    aspect-ratio: 9 / 16;
    object-fit: cover;
    background: #111;
  }
  .dc-rally-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    aspect-ratio: 9 / 16;
    background: #1A1A1A;
  }
  .dc-rally-meta {
    padding: 8px 10px 10px;
  }
  .dc-rally-title {
    font-size: 0.85rem;
    font-weight: 600;
    color: #F5F5F5;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .dc-rally-sub {
    font-size: 0.7rem;
    color: #9E9E9E;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-top: 2px;
  }
  .dc-rally-del {
    position: absolute !important;
    top: 6px;
    right: 6px;
    z-index: 2;
    background: rgba(0,0,0,0.55) !important;
  }
  .dc-rally-tmpl {
    font-size: 0.65rem;
    color: #127987;
    margin-top: 4px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .rb-shell {
    position: fixed !important;
    inset: 0 !important;
    width: 100vw !important;
    height: 100vh !important;
    height: 100dvh !important;
    display: flex !important;
    flex-direction: column !important;
    overflow: hidden !important;
    background: #0d0e14;
    z-index: 10;
  }
  .rb-chrome {
    flex: 0 0 48px;
    height: 48px;
    max-height: 48px;
  }
  .rb-chrome-right {
    flex: 0 0 auto;
    margin-left: auto;
  }
  .rb-chrome-job {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1 1 auto;
    min-width: 0;
    height: 30px;
    margin: 0 8px;
    padding: 0 10px;
    border-radius: 8px;
    background: rgba(124,92,255,0.18);
    border: 1px solid rgba(124,92,255,0.45);
  }
  .rb-chrome-job.is-off {
    visibility: hidden;
    background: transparent;
    border-color: transparent;
  }
  .rb-chrome-job.is-err {
    background: rgba(200,60,60,0.2);
    border-color: rgba(220,80,80,0.55);
  }
  .rb-chrome-job-text {
    font-size: 0.72rem;
    line-height: 1.2;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
  }
  .rb-pack {
    flex: 0 0 36px;
    height: 36px;
    display: flex;
    align-items: stretch;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    overflow-x: auto;
    background: #0d0e14;
  }
  .rb-pack-clip {
    flex: 1 1 0;
    min-width: 72px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 1px;
    color: #8b8d98;
    border-right: 1px solid rgba(255,255,255,0.08);
    position: relative;
    cursor: pointer;
  }
  .rb-pack-clip .k {
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    line-height: 1.1;
  }
  .rb-pack-clip .s { font-size: 10px; line-height: 1.1; }
  .rb-pack-clip.is-on { color: #e8e8e8; background: rgba(45,212,191,0.08); }
  .rb-pack-clip.is-on::after {
    content: "";
    position: absolute;
    left: 12%;
    right: 12%;
    bottom: 0;
    height: 2px;
    background: #2dd4bf;
  }
  .rb-pack-clip.is-ok .s { color: #37BD39; }
  .rb-pack-clip.is-lock { opacity: 0.35; cursor: default; }
  .rb-pack-clip .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #333;
    margin-top: 1px;
  }
  .rb-pack-clip.is-ok .dot { background: #37BD39; }
  .rb-pack-clip.is-on .dot { background: #2dd4bf; }
  .rb-scenes {
    flex: 0 0 64px;
    height: 64px;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 0 12px;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    background: #101118;
  }
  .rb-scenes-track {
    flex: 1;
    min-width: 0;
    height: 44px;
    display: flex;
    gap: 3px;
    align-items: stretch;
    overflow-x: auto;
  }
  .rb-scene {
    min-width: 72px;
    border-radius: 8px;
    border: 1px solid rgba(255,255,255,0.08);
    background: linear-gradient(180deg, #1c3a36, #14211f);
    text-align: left;
    padding: 6px 8px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    cursor: pointer;
    user-select: none;
  }
  .rb-scene.is-on {
    border-color: #2dd4bf;
    box-shadow: inset 0 0 0 1px #2dd4bf;
  }
  .rb-scene.is-new { box-shadow: inset 3px 0 0 #2dd4bf; }
  .rb-scene .new,
  .rb-scene-ed .scenes button .new,
  .rb-scene-ed .eclip .new {
    display: inline-block;
    margin-left: 6px;
    padding: 0 5px;
    border-radius: 999px;
    background: #2dd4bf;
    color: #04221f;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: .03em;
    line-height: 16px;
    vertical-align: 1px;
  }
  .rb-scene.is-still, .rb-scene.is-miss {
    flex: 1;
    cursor: default;
    background: #16171f;
    color: #8b8d98;
  }
  .rb-scenes-note {
    flex: none;
    max-width: 88px;
    color: #8b8d98;
    font-size: 11px;
    line-height: 1.25;
  }
  @media (max-width: 720px) {
    .rb-scenes-note { display: none; }
  }
  .rb-scene .t { font-weight: 700; font-size: 12px; line-height: 1.2; }
  .rb-scene .d { color: #8b8d98; font-size: 11px; line-height: 1.2; }
  .rb-scene.is-pending .d { color: #f0a36b; }
  .rb-scene-edit {
    flex: none;
    flex-shrink: 0;
    height: 36px;
    padding: 0 14px;
    border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.08);
    background: transparent !important;
    color: #e8e8e8 !important;
    font-weight: 700;
  }
  .rb-scene-shade {
    position: fixed;
    inset: 0;
    z-index: 80;
    background: #0e0f14;
    display: block;
  }
  .rb-scene-ed {
    position: relative;
    width: 100vw;
    height: 100dvh;
    max-width: none;
    background: #12131a;
    border: none;
    border-radius: 0;
    box-shadow: none;
    display: grid;
    grid-template-rows: auto minmax(140px, 1fr) auto auto minmax(0, max-content);
    gap: 10px;
    padding: 12px 16px 12px;
    color: #e8e8e8;
    overflow: hidden;
  }
  .rb-scene-ed .transport {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .rb-scene-ed .head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  .rb-scene-ed .head-left,
  .rb-scene-ed .head-right {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }
  .rb-scene-ed .head-left {
    flex-direction: column;
    align-items: flex-start;
    gap: 2px;
  }
  .rb-scene-ed .head-titles {
    display: flex;
    align-items: baseline;
    gap: 10px;
    min-width: 0;
  }
  .rb-scene-ed .whole {
    color: #b7b9c2;
    font-size: 12px;
    line-height: 1.3;
  }
  .rb-scene-ed .whole[hidden] { display: none; }
  .rb-scene-ed .head-right { justify-content: flex-end; margin-left: auto; }
  .rb-scene-ed h2 { margin: 0; font-size: 15px; font-weight: 650; flex: none; }
  .rb-scene-ed .sub, .rb-scene-ed .hint { color: #8b8d98; }
  .rb-scene-ed .hint { font-size: 12px; line-height: 1.35; }
  .rb-scene-ed .hint:empty { display: none; }
  .rb-scene-ed button.help {
    width: 28px;
    height: 28px;
    padding: 0;
    border-radius: 50%;
    color: #8b8d98;
    font-weight: 750;
  }
  .rb-scene-ed .sheet-card p { margin: 0; font-size: 14px; line-height: 1.4; }
  .rb-scene-ed .sheet-card p b { color: #e8e8e8; }
  .rb-scene-ed .spacer { flex: 1; }
  .rb-scene-ed button {
    font: inherit;
    color: inherit;
    cursor: pointer;
    border: 1px solid #2c2c2c;
    background: none;
    border-radius: 10px;
    height: 36px;
    padding: 0 12px;
    font-weight: 700;
    flex: none;
    white-space: nowrap;
  }
  .rb-scene-ed button:disabled { opacity: .35; cursor: default; }
  .rb-scene-ed .save {
    background: #37BD39;
    color: #041;
    border-color: transparent;
    font-weight: 800;
  }
  .rb-scene-ed .cut { border-color: #f0a36b; color: #f0a36b; }
  .rb-scene-ed .yank { border-color: #c4843a; color: #f0a36b; }
  .rb-scene-ed input[type="file"] { display: none; }
  .rb-scene-ed .work { display: flex; flex-direction: column; gap: 6px; }
  .rb-scene-ed .gen {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  .rb-scene-ed .gen-clip {
    font-weight: 750;
    font-size: 13px;
    color: #2dd4bf;
    min-width: 4.2rem;
  }
  .rb-scene-ed .status {
    color: #9a9cab;
    font-size: 12px;
    line-height: 1.35;
    flex: 1 1 12rem;
  }
  .rb-scene-ed .scenes {
    display: flex;
    gap: 6px;
    align-items: center;
    overflow-x: auto;
  }
  .rb-scene-ed .scenes button.on { border-color: #2dd4bf; color: #2dd4bf; }
  .rb-scene-ed .note { color: #9a9cab; font-size: 12px; line-height: 1.35; }
  .rb-scene-ed button.danger { border-color: #a85b5b; color: #f0b4b4; }
  .rb-scene-ed .sheet {
    position: absolute;
    inset: 0;
    z-index: 5;
    background: rgba(0,0,0,.55);
    display: grid;
    place-items: center;
    padding: 16px;
  }
  .rb-scene-ed .sheet-card {
    width: min(520px, calc(100vw - 32px));
    max-height: calc(100dvh - 32px);
    overflow: auto;
    background: #171821;
    border: 1px solid rgba(255,255,255,.08);
    border-radius: 16px;
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .rb-scene-ed .sheet-card h3 { margin: 0; font-size: 16px; }
  .rb-scene-ed textarea {
    width: 100%;
    min-height: 88px;
    resize: vertical;
    background: #101118;
    color: inherit;
    border: 1px solid #2c2c2c;
    border-radius: 10px;
    padding: 8px 10px;
    font: inherit;
  }
  .rb-scene-ed .pins { display: flex; gap: 10px; }
  .rb-scene-ed .pins figure { flex: 1; margin: 0; }
  .rb-scene-ed .pins img {
    width: 100%;
    aspect-ratio: 9 / 16;
    object-fit: cover;
    border-radius: 8px;
    max-height: 220px;
    background: #1c1d26;
  }
  .rb-scene-ed .pins figcaption {
    color: #8b8d98;
    font-size: 11px;
    margin-top: 4px;
  }
  .rb-scene-ed .check {
    display: flex;
    gap: 8px;
    align-items: center;
    font-size: 13px;
  }
  .rb-scene-ed .pin-choice { display: flex; gap: 6px; }
  .rb-scene-ed .pin-choice button { flex: 1; }
  .rb-scene-ed .pin-choice button.on { border-color: #2dd4bf; color: #2dd4bf; }
  .rb-scene-ed .dur-choice {
    --dur-thumb: 16px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .rb-scene-ed .dur-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
  }
  .rb-scene-ed .dur-k { font-size: 12px; color: #8b8d98; }
  .rb-scene-ed .dur-v {
    font-size: 13px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: #2dd4bf;
  }
  .rb-scene-ed .dur-choice input[type="range"] {
    --p: 0;
    --fill: calc(var(--dur-thumb) / 2 + (100% - var(--dur-thumb)) * var(--p));
    width: 100%;
    height: 22px;
    margin: 2px 0 0;
    appearance: none;
    background: transparent;
    cursor: pointer;
  }
  .rb-scene-ed .dur-choice input[type="range"]:focus { outline: none; }
  .rb-scene-ed .dur-choice input[type="range"]:focus-visible {
    outline: 2px solid #2dd4bf;
    outline-offset: 3px;
    border-radius: 8px;
  }
  .rb-scene-ed .dur-choice input[type="range"]::-webkit-slider-runnable-track {
    height: 4px;
    border-radius: 999px;
    background: linear-gradient(to right, #2dd4bf var(--fill), #2a2b36 var(--fill));
  }
  .rb-scene-ed .dur-choice input[type="range"]::-webkit-slider-thumb {
    appearance: none;
    width: var(--dur-thumb);
    height: var(--dur-thumb);
    margin-top: -6px;
    border: none;
    border-radius: 50%;
    background: #2dd4bf;
  }
  .rb-scene-ed .dur-choice input[type="range"]::-moz-range-track {
    height: 4px;
    border: none;
    border-radius: 999px;
    background: linear-gradient(to right, #2dd4bf var(--fill), #2a2b36 var(--fill));
  }
  .rb-scene-ed .dur-choice input[type="range"]::-moz-range-thumb {
    width: var(--dur-thumb);
    height: var(--dur-thumb);
    border: none;
    border-radius: 50%;
    background: #2dd4bf;
  }
  .rb-scene-ed .dur-scale {
    position: relative;
    height: 18px;
  }
  .rb-scene-ed .dur-scale button {
    position: absolute;
    top: 0;
    left: calc(var(--dur-thumb) / 2 + (100% - var(--dur-thumb)) * var(--p));
    transform: translateX(-50%);
    min-width: 0;
    height: auto;
    padding: 0;
    border: none;
    background: transparent;
    color: #8b8d98;
    font-size: 10px;
    font-weight: 500;
    font-variant-numeric: tabular-nums;
    line-height: 16px;
  }
  .rb-scene-ed .dur-scale button.on { color: #2dd4bf; font-weight: 700; }
  .rb-scene-ed .sheet-acts { display: flex; justify-content: flex-end; gap: 8px; }
  .rb-scene-ed button.primary {
    background: #37BD39;
    color: #041;
    border-color: transparent;
  }
  .rb-scene-ed [hidden] { display: none !important; }
  .rb-scene-ed .stage {
    min-height: 0;
    overflow: hidden;
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
    align-items: stretch;
    column-gap: 16px;
  }
  .rb-scene-ed .stage.has-results {
    grid-template-columns: auto minmax(0, 1fr);
  }
  .rb-scene-ed .stage.has-results .stage-gap { display: none; }
  .rb-scene-ed .beside {
    justify-self: stretch;
    align-self: stretch;
    min-width: 0;
    max-width: 100%;
    height: 100%;
    display: flex;
    align-items: stretch;
    gap: 12px;
    overflow-x: auto;
    overflow-y: hidden;
  }
  .rb-scene-ed .results {
    display: flex;
    align-items: stretch;
    gap: 12px;
    height: 100%;
    min-height: 0;
  }
  .rb-scene-ed .arrow {
    flex: none;
    color: #2dd4bf;
    display: flex;
    align-items: center;
  }
  .rb-scene-ed .arrow svg { display: block; }
  .rb-scene-ed .result {
    position: relative;
    flex: none;
    height: 100%;
    max-height: 100%;
    display: flex;
    flex-direction: row;
    align-items: stretch;
    gap: 6px;
    min-height: 0;
  }
  .rb-scene-ed .result .cap {
    position: absolute;
    top: 8px;
    left: 8px;
    z-index: 2;
    color: #fff;
    background: rgba(0,0,0,.55);
    border-radius: 8px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: .02em;
  }
  .rb-scene-ed .result .acts {
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 6px;
  }
  .rb-scene-ed .result .acts button {
    height: 28px;
    padding: 0 8px;
    font-size: 12px;
  }
  .rb-scene-ed .result.is-new .monitor { box-shadow: 0 0 0 2px #2dd4bf; }
  .rb-scene-ed .monitor {
    height: 100%;
    width: auto;
    max-height: 100%;
    max-width: none;
    aspect-ratio: 9 / 16;
    position: relative;
    border-radius: 16px;
    overflow: hidden;
    background: #14181f;
    border: 1px solid rgba(255,255,255,.08);
    flex: none;
  }
  .rb-scene-ed .stage:not(.has-results) > .monitor {
    justify-self: center;
  }
  .rb-scene-ed .result .monitor {
    height: 100%;
    max-height: 100%;
    max-width: none;
    width: auto;
  }
  .rb-scene-ed .monitor.is-preview { box-shadow: 0 0 0 2px #2dd4bf; }
  .rb-scene-ed .tl.is-locked { opacity: .45; pointer-events: none; }
  .rb-scene-ed video {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
    background: #14181f;
  }
  .rb-scene-ed .stage > .monitor > video {
    position: absolute;
    inset: 0;
  }
  .rb-scene-ed .stage > .monitor > video.standby {
    opacity: 0;
    pointer-events: none;
  }
  .rb-scene-ed .f {
    position: absolute;
    left: 10px;
    bottom: 10px;
    font-size: 16px;
    font-weight: 750;
    letter-spacing: .04em;
    color: #fff;
    background: rgba(0,0,0,.55);
    border-radius: 8px;
    padding: 2px 8px;
    pointer-events: none;
  }
  .rb-scene-ed .tc { font-variant-numeric: tabular-nums; font-weight: 650; }
  .rb-scene-ed .tl {
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-height: 0;
    overflow-x: auto;
    overflow-y: auto;
  }
  .rb-scene-ed .ruler {
    position: relative;
    height: 18px;
    color: #c8c9d1;
    font-size: 10px;
    cursor: ew-resize;
    user-select: none;
    background-image: repeating-linear-gradient(
      90deg, rgba(255,255,255,.28) 0 1px, transparent 1px 10px
    );
    background-position: left bottom;
    background-size: 100% 8px;
    background-repeat: repeat-x;
  }
  .rb-scene-ed .ruler span {
    position: absolute;
    top: 0;
    transform: translateX(-50%);
    background: #12131a;
    padding: 0 2px;
    line-height: 1;
  }
  .rb-scene-ed .ruler span.first { transform: none; }
  .rb-scene-ed .ruler span.last:not(.first) { transform: translateX(-100%); }
  .rb-scene-ed .lanes { position: relative; display: flex; flex-direction: column; gap: 4px; }
  .rb-scene-ed .lane { position: relative; height: 64px; }
  .rb-scene-ed .alane {
    position: relative;
    min-height: 32px;
    border-radius: 8px;
    background: #101218;
    border: 1px solid rgba(255,255,255,.06);
  }
  .rb-scene-ed .alane:empty::before {
    content: "Audio";
    position: absolute;
    left: 8px;
    top: 7px;
    color: #5c5e6a;
    font-size: 11px;
    font-weight: 700;
  }
  .rb-scene-ed .ablk {
    position: absolute;
    top: 4px;
    height: 24px;
    border-radius: 6px;
    border: 1px solid #c4843a;
    background: linear-gradient(180deg, #5a3a18, #2a1c0e);
    color: #f3d7b0;
    font-size: 11px;
    font-weight: 700;
    line-height: 18px;
    padding: 0 6px;
    overflow: hidden;
    white-space: nowrap;
    cursor: grab;
    user-select: none;
    touch-action: none;
    z-index: 1;
  }
  .rb-scene-ed .ablk.on { border-color: #fff; }
  .rb-scene-ed .ablk.is-emb {
    border-color: #2a6f66;
    background: linear-gradient(180deg, #1c3a36, #10211f);
    color: #b7e6df;
    cursor: pointer;
  }
  .rb-scene-ed .abin {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: center;
  }
  .rb-scene-ed .abin .bin-label,
  .rb-scene-ed .abin .bin-empty {
    color: #8b8d98;
    font-size: 11px;
    font-weight: 700;
  }
  .rb-scene-ed .abin button {
    height: 28px;
    padding: 0 8px;
    font-size: 12px;
    font-weight: 650;
  }
  .rb-scene-ed .eclips { display: flex; height: 100%; gap: 3px; }
  .rb-scene-ed .eclip {
    min-width: 48px;
    border-radius: 8px;
    border: 1px solid rgba(255,255,255,.08);
    background:
      repeating-linear-gradient(90deg, rgba(255,255,255,.06) 0 1px, transparent 1px 7px),
      linear-gradient(180deg, #243044, #171b22);
    text-align: left;
    padding: 8px;
    cursor: grab;
  }
  .rb-scene-ed .eclip.on { border-color: #2dd4bf; }
  .rb-scene-ed .eclip.is-new { box-shadow: inset 3px 0 0 #2dd4bf; }
  .rb-scene-ed .eclip.is-mute {
    background:
      repeating-linear-gradient(90deg, rgba(255,255,255,.06) 0 1px, transparent 1px 7px),
      linear-gradient(180deg, #3a3228, #1c1814);
  }
  .rb-scene-ed .eclip .t { display: block; font-weight: 700; }
  .rb-scene-ed .eclip .d { color: #8b8d98; font-size: 11px; }
  .rb-scene-ed .eclip.is-pending {
    overflow: hidden;
    background:
      repeating-linear-gradient(90deg, rgba(255,255,255,.05) 0 1px, transparent 1px 7px),
      linear-gradient(180deg, #3a2a22, #1c1612);
  }
  .rb-scene-ed .eclip.is-pending .d { color: #f0a36b; }
  .rb-scene-ed .eclip .spin {
    display: inline-block;
    width: 12px;
    height: 12px;
    margin-right: 6px;
    border: 2px solid rgba(240, 163, 107, .35);
    border-top-color: #f0a36b;
    border-radius: 50%;
    vertical-align: -1px;
    animation: rb-spin .7s linear infinite;
  }
  @keyframes rb-spin { to { transform: rotate(360deg); } }
  .rb-scene-ed .playhead {
    position: absolute;
    top: -16px;
    bottom: 0;
    width: 2px;
    margin-left: -1px;
    background: #fff;
    pointer-events: none;
    z-index: 3;
  }
  .rb-scene-ed .playhead::before {
    content: "";
    position: absolute;
    top: 0;
    left: -5px;
    border-left: 6px solid transparent;
    border-right: 6px solid transparent;
    border-top: 8px solid #fff;
  }
  @media (max-width: 720px) {
    .rb-scene-ed {
      width: 100vw;
      height: 100dvh;
      padding: 10px;
    }
    .rb-scene-ed .head { align-items: flex-start; }
    .rb-scene-ed .head-right { flex-wrap: wrap; justify-content: flex-end; }
    .rb-scene-ed .stage.has-results { column-gap: 8px; }
    .rb-scene-ed .result .acts button {
      padding: 0 6px;
      font-size: 11px;
    }
    .rb-scene-ed .arrow svg { width: 28px; height: 16px; }
  }
  .rb-body {
    flex: 1 1 auto;
    min-height: 0 !important;
    display: flex !important;
    flex-direction: column !important;
    overflow: hidden !important;
  }
  .rb-direct {
    flex: 1 1 auto;
    min-height: 0 !important;
    display: flex !important;
    flex-direction: column !important;
    overflow: hidden !important;
  }
  .rb-direct-body {
    flex: 1 1 auto;
    min-height: 0 !important;
    display: flex !important;
    flex-direction: column !important;
    gap: 0;
    overflow: hidden !important;
    padding: 10px 12px 12px;
  }
  .rb-pipe-row {
    flex: 1 1 auto;
    min-height: 0;
    display: grid !important;
    grid-template-columns: 148px minmax(180px, 1fr) minmax(200px, 0.85fr) minmax(260px, 1.25fr);
    gap: 12px;
    overflow: hidden;
  }
  .rb-direct.surface-take .rb-pipe-row {
    grid-template-columns: 148px minmax(170px, 1fr) 148px minmax(190px, 0.85fr) minmax(260px, 1.25fr);
  }
  .rb-pipe-col {
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
    overflow: auto;
  }
  .rb-pipe-compile-col {
    overflow: hidden;
    gap: 8px;
    height: 100%;
    min-height: 0;
  }
  .rb-pipe-compiled > .rb-pipe-k,
  .rb-pipe-post > .rb-pipe-k {
    flex: 0 0 auto;
  }
  .rb-pipe-post .rb-takes {
    height: auto;
    margin-top: 0;
    justify-content: flex-start;
  }
  .rb-pipe-frame {
    justify-content: flex-start;
    overflow: visible;
  }
  .rb-pipe-frame > .rb-surface {
    width: 100%;
    margin: 0;
    align-self: stretch;
    flex: 0 0 auto;
    justify-content: stretch;
  }
  .rb-pipe-frame > .rb-surface .rb-surface-btn {
    flex: 1 1 0;
    text-align: center;
    padding: 6px 8px;
  }
  .rb-pipe-slot {
    flex: 0 0 auto;
    width: 100%;
    background: #16171f;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 14px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    position: relative;
  }
  .rb-pipe-slot.is-mute { opacity: 0.38; }
  .rb-pipe-slot.is-on { border-color: rgba(45,212,191,0.4); }
  .rb-pipe-slot.is-look { border-color: rgba(232,194,122,0.4); }
  .rb-pipe-glass {
    flex: 0 0 auto;
    width: 100%;
    aspect-ratio: 9 / 16;
    height: auto;
    cursor: pointer;
    position: relative;
    background: repeating-linear-gradient(135deg, #14151c 0 7px, #191a22 7px 14px);
  }
  .rb-pipe-glass img {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .rb-pipe-empty {
    position: absolute;
    inset: 0;
    display: grid;
    place-items: center;
    text-align: center;
    padding: 12px;
    color: #6a6d78;
    font-size: 12px;
  }
  .rb-pipe-type {
    margin: 8px;
  }
  .rb-pipe-bubbles {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    padding: 8px 0 0;
  }
  .rb-pipe-bubbles .rb-chip {
    padding: 2px 7px;
    font-size: 10px;
    letter-spacing: 0.02em;
    cursor: pointer;
  }
  .rb-pipe-mute {
    position: absolute;
    top: 4px;
    right: 4px;
    z-index: 2;
    width: 16px;
    height: 16px;
    border-radius: 4px;
    display: grid;
    place-items: center;
    font-size: 13px;
    font-weight: 700;
    line-height: 1;
    color: #8b8d98;
    cursor: pointer;
    background: rgba(0,0,0,0.45);
    user-select: none;
    overflow: hidden;
  }
  .rb-pipe-mute .q-icon,
  .rb-pipe-mute .nicegui-label,
  .rb-pipe-mute > * {
    font-size: 13px;
    line-height: 1;
    margin: 0;
  }
  .rb-pipe-mute.is-off { color: #f0a36b; }
  .rb-pipe-piece .rb-pipe-mute {
    position: static;
    flex: 0 0 auto;
    margin-left: 4px;
    background: transparent;
  }
  .rb-pipe-piece {
    background: #16171f;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 12px;
    padding: 6px 8px;
    position: relative;
  }
  .rb-pipe-piece.is-mute { opacity: 0.55; }
  .rb-pipe-head {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }
  .rb-pipe-head > .rb-pipe-k { flex: 0 0 auto; }
  .rb-pipe-head-ctl {
    display: flex;
    flex: 1 1 auto;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px;
    min-width: 0;
  }
  .rb-pipe-ref-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    padding: 0 2px 8px;
  }
  .rb-pipe-ref-still { width: 64px; }
  .rb-pipe-ref-still img {
    display: block;
    width: 64px;
    height: 64px;
    object-fit: contain;
    background: #111;
    border-radius: 6px;
    border: 1px solid rgba(255,255,255,0.12);
  }
  .rb-pipe-ref-still .cap {
    font-size: 0.58rem;
    text-align: center;
    color: #9e9e9e;
    line-height: 1.2;
    padding-top: 2px;
  }
  .rb-pipe-head-ctl .rb-chip {
    padding: 1px 8px;
    font-size: 11px;
  }
  .rb-pipe-body { margin-top: 4px; }
  .rb-add-row {
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .rb-add-row .q-field { flex: 1 1 auto; min-width: 0; }
  .rb-add-btn { flex: 0 0 auto; min-height: 28px !important; }
  .rb-dur-slider {
    flex: 1 1 72px;
    min-width: 72px;
    max-width: 148px;
  }
  .rb-dur-slider .q-slider { padding: 0; min-height: 22px; }
  .rb-pipe-dur-val {
    flex: 0 0 auto;
    font-size: 12px;
    color: #2dd4bf;
    font-variant-numeric: tabular-nums;
    min-width: 2rem;
  }
  .rb-pipe-q {
    display: flex;
    flex: 0 0 auto;
    gap: 4px;
    margin-left: auto;
  }
  .rb-pipe-q .rb-chip {
    padding: 1px 8px;
    font-size: 11px;
  }
  .rb-pipe-clamp {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    margin-top: 0;
  }
  .rb-pipe-col.rb-pipe-stack {
    overflow: hidden;
  }
  .rb-pipe-dur {
    flex: 0 0 auto;
  }
  .rb-pipe-piece.is-prompt {
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: auto;
  }
  .rb-pipe-piece.is-prompt .rb-pipe-body {
    flex: 1 1 auto;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .rb-pipe-piece.is-prompt .rb-pipe-ta,
  .rb-pipe-piece.is-prompt .q-textarea,
  .rb-pipe-piece.is-prompt .q-field,
  .rb-pipe-piece.is-prompt .q-field__inner,
  .rb-pipe-piece.is-prompt .q-field__control,
  .rb-pipe-piece.is-prompt .q-field__control-container,
  .rb-pipe-piece.is-prompt .q-field__native {
    flex: 1 1 auto;
    height: 100%;
    min-height: 0;
  }
  .rb-pipe-piece.is-prompt .q-field__native {
    height: 100% !important;
  }
  .rb-pipe-k {
    font-size: 0.65rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #8b8d98;
  }
  .rb-pipe-v { font-size: 12px; color: #d0d2da; margin-top: 4px; }
  .rb-pipe-ta .q-field__control { background: transparent !important; }
  .rb-pipe-compiled {
    flex: 65 1 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: #12131a;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 14px;
    padding: 10px 12px;
  }
  .rb-pipe-raw {
    flex: 1 1 auto;
    min-height: 0;
    overflow-x: hidden;
    overflow-y: auto;
  }
  .rb-pipe-post {
    flex: 35 1 0;
    min-height: 0;
    overflow: auto;
    background: #12131a;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 14px;
    padding: 8px 10px 10px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .rb-pipe-post-takes {
    display: flex;
    flex-wrap: nowrap;
    gap: 6px;
    overflow-x: auto;
    overflow-y: hidden;
    flex: 0 0 auto;
    padding-bottom: 2px;
  }
  .rb-pipe-post-takes .rb-take {
    flex: 0 0 auto;
    width: 40px;
    height: auto;
    aspect-ratio: 9 / 16;
  }
  .rb-pipe-post-btns {
    display: flex;
    gap: 6px;
    flex: 0 0 auto;
  }
  .rb-pipe-post-btns .q-btn {
    flex: 1 1 0;
    min-height: 30px !important;
  }
  .rb-clip-row {
    display: flex;
    flex-direction: column;
    gap: 4px;
    flex: 0 0 auto;
  }
  .rb-clip-k {
    font-size: 0.72rem;
    color: #c8c9d1;
  }
  .rb-clip-note {
    font-size: 11px;
    line-height: 1.35;
    color: #9a9cab;
  }
  .rb-fresh-pins {
    display: flex;
    gap: 10px;
  }
  .rb-fresh-pins > div {
    flex: 1 1 0;
    min-width: 0;
  }
  .rb-fresh-pins img {
    display: block;
    width: 100%;
    aspect-ratio: 9 / 16;
    object-fit: cover;
    border-radius: 8px;
    background: #1c1d26;
  }
  .rb-comp-line {
    padding: 6px 0;
    border-bottom: 1px solid rgba(255,255,255,0.04);
    cursor: pointer;
  }
  .rb-comp-text {
    font-size: 12px;
    line-height: 1.5;
    color: #cfd1d8;
    white-space: pre-wrap;
  }
  .rb-comp-line.is-flash {
    animation: rbCompFlash 0.7s ease;
  }
  @keyframes rbCompFlash {
    0% { background: rgba(45,212,191,0.35); }
    100% { background: transparent; }
  }
  .rb-pipe-out {
    min-width: 0;
    min-height: 0;
    height: 100%;
    display: flex;
    justify-content: center;
    align-items: stretch;
    overflow: hidden;
    container-type: size;
    --acts-h: 116px;
  }
  .rb-out-col {
    height: 100%;
    width: min(100cqw, calc((100cqh - var(--acts-h)) * 9 / 16));
    min-height: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .rb-pipe-out .rb-direct-phone {
    flex: 1 1 auto;
    width: 100%;
    min-height: 0;
    display: flex;
    justify-content: center;
    align-items: center;
    padding: 0;
    overflow: hidden;
    container-type: size;
  }
  .rb-pipe-out .rb-direct-phone .rb-stage {
    flex: 0 0 auto !important;
    width: min(100cqw, calc(100cqh * 9 / 16)) !important;
    height: min(100cqh, calc(100cqw * 16 / 9)) !important;
    max-width: 100% !important;
    max-height: 100% !important;
    aspect-ratio: 9 / 16;
    margin: 0 auto;
  }
  .rb-pipe-acts {
    width: 100%;
    display: flex;
    flex-direction: column;
    gap: 8px;
    flex: 0 0 auto;
  }
  .rb-pipe-acts .rb-go {
    width: 100% !important;
    min-width: 0 !important;
    font-weight: 800 !important;
    padding: 10px !important;
    border-radius: 10px !important;
    min-height: 40px !important;
  }
  .rb-pipe-acts-row {
    display: flex;
    gap: 8px;
    width: 100%;
  }
  .rb-pipe-acts-row > .q-btn {
    flex: 1 1 0 !important;
    width: auto !important;
    min-width: 0 !important;
    border-radius: 10px !important;
    padding: 9px 10px !important;
    font-weight: 650 !important;
    min-height: 38px !important;
  }
  .rb-pipe-acts-extra > .q-btn {
    min-height: 30px !important;
    padding: 4px 8px !important;
    font-weight: 550 !important;
  }
  .rb-post-sheet {
    width: min(560px, 94vw) !important;
    max-width: 94vw !important;
  }
  .rb-post-takes {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 8px 0 12px;
  }
  .rb-post-takes .rb-take {
    width: 52px;
    height: auto;
    aspect-ratio: 9 / 16;
  }
  .rb-still-picker { min-width: min(520px, 92vw); }
  .rb-still-picker-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 10px;
    margin: 12px 0;
  }
  .rb-still-pick {
    aspect-ratio: 9/16;
    border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.08);
    overflow: hidden;
    position: relative;
    cursor: pointer;
    background: #16171f;
  }
  .rb-still-pick img { width: 100%; height: 100%; object-fit: cover; }
  .rb-still-pick .cap {
    position: absolute; left: 0; right: 0; bottom: 0;
    font-size: 10px; text-align: center; padding: 4px;
    background: linear-gradient(transparent, #000d);
  }
  .rb-still-pick.is-none {
    display: grid;
    place-items: center;
    background: repeating-linear-gradient(135deg, #14151c 0 7px, #191a22 7px 14px);
    color: #8b8d98;
    font-size: 11px;
  }
  .rb-direct-rail {
    overflow: auto;
    padding: 12px 14px;
    border-right: 1px solid rgba(255,255,255,0.08);
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-height: 0;
  }
  .rb-direct-rail:last-child {
    border-right: 0;
    border-left: 1px solid rgba(255,255,255,0.08);
    overflow: hidden;
    min-height: 0;
  }
  .rb-direct-lbl {
    font-size: 0.65rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #8b8d98;
  }
  .rb-direct.surface-take .rb-still-only { display: none !important; }
  .rb-direct-rail:last-child > *:not(.rb-formula) {
    flex-shrink: 0;
  }
  .rb-formula {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding-bottom: 10px;
    margin-bottom: 6px;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    flex: 1 1 auto;
    min-height: 0;
    overflow: auto;
  }
  .rb-formula-surf {
    font-size: 0.65rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #2dd4bf;
  }
  .rb-formula-plus {
    align-self: center;
    font-size: 18px;
    font-weight: 700;
    line-height: 1;
    color: #2dd4bf;
    opacity: 0.7;
    margin: 2px 0 4px;
    user-select: none;
  }
  .rb-formula-sec {
    background: #16171f;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 10px;
    padding: 8px 8px 6px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .rb-formula-sec.is-edit {
    border-color: rgba(45,212,191,0.35);
  }
  .rb-formula-k {
    font-size: 0.65rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #8b8d98;
  }
  .rb-formula-edited {
    font-size: 0.6rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #2dd4bf;
  }
  .rb-formula-ta .q-field__control {
    background: #1a1a1a !important;
    border-radius: 8px;
  }
  .rb-formula-ta textarea,
  .rb-formula-ta .q-field__native {
    min-height: 3.2rem !important;
    max-height: 9rem !important;
    overflow-y: auto !important;
    font-size: 12px !important;
    line-height: 1.4 !important;
    text-transform: none !important;
  }
  .rb-formula-add {
    color: #2dd4bf !important;
  }
  .rb-sound-x {
    opacity: 0.55;
    font-size: 12px;
    line-height: 1;
    margin-left: 2px;
  }
  .rb-surface {
    display: flex;
    flex: 0 0 auto;
    background: #16171f;
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 11px;
    padding: 3px;
    gap: 2px;
    z-index: 4;
  }
  .rb-surface-btn {
    padding: 6px 22px;
    border-radius: 8px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: #8b8d98;
    cursor: pointer;
  }
  .rb-surface-btn.is-on {
    background: rgba(45,212,191,0.18);
    color: #e8e8e8;
  }
  .rb-direct-phone {
    min-width: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    align-items: stretch;
    justify-content: center;
    padding: 8px 10px;
    overflow: hidden;
    position: relative;
  }
  .rb-direct-stage-row {
    flex: 1 1 auto;
    min-height: 0;
    width: 100%;
    display: flex;
    flex-direction: row;
    align-items: stretch;
    justify-content: center;
    gap: 10px;
  }
  .rb-join-slot {
    flex: 1 1 0;
    min-width: 0;
    max-width: 240px;
    display: flex;
    align-items: center;
    align-self: center;
  }
  .rb-join-slot.is-in { justify-content: flex-end; }
  .rb-join-slot.is-out { justify-content: flex-start; }
  .rb-input-stack {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    max-width: 130px;
  }
  .rb-input-still {
    width: 92px;
    aspect-ratio: 9 / 16;
    border-radius: 10px;
    overflow: hidden;
    border: 2px solid rgba(255,255,255,0.14);
    background: #16171f;
    cursor: pointer;
    position: relative;
  }
  .rb-input-still img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
    pointer-events: none;
  }
  .rb-input-still.is-empty {
    background: repeating-linear-gradient(
      135deg,
      #16171f,
      #16171f 8px,
      #1c1d26 8px,
      #1c1d26 16px
    );
  }
  .rb-input-still.rb-input-hero {
    width: 110px;
    border-color: rgba(45,212,191,0.55);
  }
  .rb-input-overlay {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    height: 42%;
    background: linear-gradient(
      to top,
      rgba(8,10,16,0.92) 0%,
      rgba(8,10,16,0.55) 70%,
      transparent 100%
    );
    pointer-events: none;
  }
  .rb-input-overlay-lab {
    position: absolute;
    left: 6px;
    right: 6px;
    bottom: 8px;
    text-align: center;
    font-size: 11px;
    font-weight: 750;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: #f2f4f8;
    pointer-events: none;
    line-height: 1.2;
  }
  .rb-still-role {
    position: absolute;
    left: 3px;
    right: 3px;
    bottom: 2px;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #2dd4bf;
    text-align: center;
    text-shadow: 0 1px 2px #000;
  }
  .rb-direct-body {
    background: radial-gradient(ellipse at center, #1a1b2e 0%, #0d0e14 70%);
  }
  .rb-direct-phone .rb-stage {
    flex: 0 0 auto;
    width: min(100cqw, calc(100cqh * 9 / 16));
    height: min(100cqh, calc(100cqw * 16 / 9));
    aspect-ratio: 9 / 16;
    max-width: 100%;
    max-height: 100%;
    min-height: 0;
    border-radius: 18px;
    overflow: hidden;
    background: #111;
    box-shadow: 0 18px 50px rgba(0,0,0,0.55);
    border: 1px solid rgba(255,255,255,0.08);
  }
  .rb-direct-phone .rb-stage-inner { inset: 0; }
  .rb-direct-phone .rb-stage-media {
    width: 100% !important;
    height: 100% !important;
    max-width: none !important;
    max-height: none !important;
    object-fit: cover;
    border-radius: 0;
    box-shadow: none;
  }
  .rb-direct-phone .rb-thumbs { display: none !important; }
  .rb-chip-row { display: flex; flex-wrap: wrap; gap: 6px; }
  .rb-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: #1a1a1a;
    border: 1px solid #2c2c2c;
    border-radius: 999px;
    padding: 4px 10px;
    font-size: 12px;
    cursor: pointer;
  }
  .rb-chip.is-on { border-color: #2dd4bf; color: #2dd4bf; }
  .rb-chip .av {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    background: rgba(45,212,191,0.35);
  }
  .rb-chip .av.rb-e-white { background: #d1d1d1; }
  .rb-chip .av.rb-e-black { background: #111; box-shadow: inset 0 0 0 1px #444; }
  .rb-takes {
    display: flex;
    flex: 0 0 auto;
    gap: 6px;
    height: 58px;
    margin-top: 8px;
    justify-content: center;
    position: relative;
    z-index: 4;
  }
  .rb-take {
    width: 36px;
    height: 58px;
    border-radius: 5px;
    overflow: hidden;
    border: 2px solid transparent;
    background: #1c1d26;
    cursor: pointer;
    position: relative;
    z-index: 1;
  }
  .rb-take.is-on {
    border-color: #2dd4bf;
    box-shadow: 0 0 0 1px #2dd4bf;
  }
  .rb-take.is-live::after {
    content: "";
    position: absolute;
    left: 4px;
    right: 4px;
    bottom: 3px;
    height: 3px;
    border-radius: 2px;
    background: #2dd4bf;
    pointer-events: none;
  }
  .rb-take-ok {
    position: absolute;
    top: 1px;
    right: 2px;
    z-index: 2;
    font-size: 8px;
    line-height: 1;
    color: #37BD39;
    text-shadow: 0 0 2px rgba(0,0,0,0.9);
    pointer-events: none;
  }
  .rb-take:hover { border-color: rgba(45,212,191,0.55); }
  .rb-take-x {
    position: absolute;
    top: 1px;
    right: 1px;
    width: 16px;
    height: 16px;
    border-radius: 4px;
    background: rgba(0,0,0,0.72);
    color: #fff;
    font-size: 12px;
    line-height: 16px;
    text-align: center;
    opacity: 0;
    cursor: pointer;
    z-index: 2;
  }
  .rb-take:hover .rb-take-x { opacity: 1; }
  .rb-take-heart {
    position: absolute;
    right: 1px;
    bottom: 0;
    font-size: 9px;
    line-height: 1;
    color: #e91e63;
    text-shadow: 0 1px 2px #000;
  }
  .rb-take img, .rb-take video {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
    pointer-events: none;
  }
  .rb-direct-linebox {
    flex: 1 1 auto;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 8px;
    background: #1a1a1a;
    border: 1px solid #2c2c2c;
    border-radius: 10px;
    padding: 8px 10px;
    cursor: text;
  }
  .rb-direct-preview {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 5;
    white-space: pre-wrap;
    font-size: 12px;
    line-height: 1.35;
  }
  .rb-direct-linebox {
    width: 100%;
    flex: 0 0 auto;
  }
  .rb-direct-dock { display: none; }
  .rb-prompt-sheet {
    width: min(720px, 94vw) !important;
    max-width: 94vw !important;
  }
  .rb-prompt-sheet-ta textarea,
  .rb-prompt-sheet-ta .q-field__native {
    min-height: 42vh !important;
    font-size: 0.95rem !important;
    line-height: 1.5 !important;
    text-transform: none !important;
  }
  .rb-confirm .q-dialog__inner {
    align-items: flex-start !important;
    justify-content: flex-end !important;
    padding: 64px 16px 16px !important;
  }
  .rb-confirm .q-card {
    background: #1c1d26;
    width: min(420px, calc(100vw - 32px));
  }
  .rb-nudge-sheet {
    width: min(480px, 94vw) !important;
    max-width: 94vw !important;
  }
  .rb-nudge-sheet-ta textarea,
  .rb-nudge-sheet-ta .q-field__native {
    text-transform: none !important;
    min-height: 6.5rem !important;
    font-size: 0.95rem !important;
    line-height: 1.45 !important;
  }
  .rb-go {
    background: #37BD39 !important;
    color: #041 !important;
    font-weight: 650 !important;
    min-width: 7.5rem;
  }
  .rb-ghost {
    border: 1px solid #2c2c2c !important;
  }
  .rb-ok {
    border: 1px solid #37BD39 !important;
    color: #37BD39 !important;
  }
  .rb-join {
    display: flex;
    width: 100%;
    max-width: 220px;
    background: #16171f;
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 10px;
    padding: 6px 8px;
    cursor: pointer;
    flex: 0 1 auto;
    margin: 0;
  }
  .rb-join:hover { border-color: rgba(45,212,191,0.45); }
  .rb-join-copy { min-width: 0; }
  .rb-join-copy .text-caption {
    white-space: normal;
    line-height: 1.2;
  }
  .rb-join-sw {
    width: 22px;
    height: 36px;
    border-radius: 4px;
    background: #222;
    background-position: center;
    flex: 0 0 auto;
  }
  .rb-direct-dock {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px 12px;
    border-top: 1px solid rgba(255,255,255,0.08);
  }
  .rb-direct-line { flex: 1 1 auto; min-width: 0; }
  .rb-direct-who {
    font-size: 11px;
    color: #2dd4bf;
    background: rgba(45,212,191,0.1);
    border-radius: 6px;
    padding: 4px 8px;
    white-space: nowrap;
  }
  .rb-main-row {
    flex: 1 1 auto;
    min-height: 0 !important;
    display: flex !important;
    flex-direction: row !important;
    overflow: hidden !important;
    align-items: stretch !important;
  }
  /* Edit sidebar: roomier than the old cramped 260–360px strip */
  .rb-edit-panel {
    flex: 0 0 auto;
    width: min(30rem, 44vw);
    min-width: 22rem;
    max-width: 34rem;
    height: 100%;
    max-height: 100%;
    display: flex !important;
    flex-direction: column !important;
    min-height: 0 !important;
    overflow: hidden !important;
    border-left: 1px solid rgba(255,255,255,0.08);
    background: #12131a;
  }
  .rb-edit-actions {
    flex: 0 0 auto;
    padding: 0.85rem 1.1rem;
    display: flex;
    flex-direction: column;
    gap: 0.55rem;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    background: rgba(13,14,20,0.98);
  }
  .rb-edit-actions .q-btn {
    min-height: 2.4rem;
  }
  .rb-edit-scroll {
    flex: 1 1 auto;
    min-height: 0 !important;
    overflow-y: auto !important;
    overflow-x: hidden;
    padding: 1.1rem 1.2rem 1.75rem;
    display: flex;
    flex-direction: column;
    gap: 1.15rem;
  }
  .rb-field {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    width: 100%;
  }
  .rb-field-label {
    font-size: 0.82rem;
    font-weight: 600;
    color: #E8E8E8;
    letter-spacing: 0.01em;
  }
  .rb-field-hint {
    font-size: 0.72rem;
    color: #9E9E9E;
    line-height: 1.4;
    margin-top: -0.15rem;
  }
  .rb-prompt .q-field__control {
    background: #1A1A1A !important;
    border-radius: 10px;
  }
  .rb-prompt .q-field__native,
  .rb-prompt textarea {
    padding-top: 0.65rem !important;
    padding-bottom: 0.65rem !important;
  }
  .rb-composed .q-field__control {
    background: #15161d !important;
  }
  .rb-composed textarea {
    overflow-y: auto !important;
    white-space: pre-wrap !important;
  }
  .rb-composed-field {
    flex: 1 0 auto;
  }
  .rb-still-hero {
    width: 168px;
    aspect-ratio: 9 / 16;
    border-radius: 10px;
    overflow: hidden;
    background: #0a0a10;
    border: 1px solid rgba(255,255,255,0.12);
  }
  .rb-still-hero img {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
  }
  .rb-still-strip {
    display: flex;
    gap: 8px;
    overflow-x: auto;
    padding: 2px 0 6px;
  }
  .rb-still-card {
    flex: 0 0 auto;
    width: 72px;
    cursor: pointer;
    border: 2px solid rgba(255,255,255,0.12);
    border-radius: 8px;
    overflow: hidden;
    background: #1a1a1a;
  }
  .rb-still-card.is-on {
    border-color: #2dd4bf;
  }
  .rb-still-card img {
    display: block;
    width: 72px;
    height: 118px;
    object-fit: cover;
  }
  .rb-still-card .cap {
    font-size: 0.58rem;
    text-align: center;
    color: #9e9e9e;
    padding: 2px 2px 4px;
    line-height: 1.15;
  }
  .rb-lock-chip {
    background: rgba(45,212,191,0.08);
    border: 1px solid rgba(45,212,191,0.25);
    border-radius: 10px;
    padding: 0.55rem 0.75rem;
  }
  .rb-callout {
    border-radius: 10px;
    padding: 0.75rem 0.85rem;
  }
  .rb-callout-warn {
    background: rgba(255,180,0,0.12);
    border: 1px solid rgba(255,180,0,0.45);
  }
  .rb-callout-dismiss {
    display: flex;
    align-items: flex-start;
    gap: 8px;
  }
  .rb-callout-msg { flex: 1 1 auto; min-width: 0; }
  .rb-callout-x { flex: 0 0 auto; margin: -6px -4px -6px 0; }
  .rb-callout-ok {
    background: rgba(45,212,191,0.12);
    border: 1px solid rgba(45,212,191,0.4);
  }
  .rb-advanced {
    margin-top: 0.25rem;
  }
  @media (max-width: 1100px) {
    .rb-edit-panel {
      width: min(26rem, 48vw);
      min-width: 20rem;
    }
  }
  .rb-stepper-btn {
    min-width: 2.9rem !important;
    padding: 1px 3px !important;
  }
  .rb-stepper-btn .q-btn__content {
    flex-direction: column;
    gap: 0;
    font-size: 0.62rem;
    line-height: 1.05;
  }
  .rb-stepper-btn .q-icon { font-size: 1.05rem !important; }
  .rb-stepper-current {
    box-shadow: 0 0 0 2px #2dd4bf inset !important;
  }
  .rb-stage {
    flex: 1 1 auto;
    min-width: 0;
    min-height: 0 !important;
    position: relative;
    overflow: hidden !important;
    background: radial-gradient(ellipse at center, #1a1b2e 0%, #0d0e14 70%);
  }
  .rb-stage-inner {
    position: absolute;
    inset: 6px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 0;
    gap: 4px;
    overflow: hidden;
  }
  .rb-primary-slot {
    flex: 1 1 auto;
    min-height: 0;
    min-width: 0;
    width: 100%;
    position: relative;
    overflow: hidden;
  }
  .rb-heart-wrap {
    position: absolute;
    top: 10px;
    right: 10px;
    z-index: 5;
  }
  .rb-heart-btn {
    box-shadow: 0 4px 14px rgba(0,0,0,0.45) !important;
  }
  .rb-heart-badge {
    font-size: 0.65rem !important;
    min-height: 1rem !important;
    padding: 0 4px !important;
  }
  .rb-fav-card {
    background: #1A1A1A;
    border: 1px solid #2C2C2C;
    border-radius: 10px;
    padding: 0.55rem 0.65rem;
  }
  .rb-fav-card:hover {
    border-color: #e91e63;
  }
  .rb-stage-media {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    display: block;
    max-width: 100%;
    max-height: 100%;
    width: auto;
    height: auto;
    object-fit: contain;
    border-radius: 12px;
    background: #111;
    box-shadow: 0 8px 32px rgba(0,0,0,0.5);
  }
  .rb-thumbs { flex: 0 0 auto; max-height: 64px; }
  /* Final review fills rb-body completely (stepper is in main chrome) */
  .rb-review {
    position: absolute !important;
    inset: 0 !important;
    width: 100% !important;
    height: 100% !important;
    display: block !important;
    overflow: hidden !important;
    background: #0a0b10;
    z-index: 1;
  }
  .rb-review-stage {
    position: absolute !important;
    inset: 0 !important;
    width: auto !important;
    height: auto !important;
    overflow: hidden !important;
    background: #050508 !important;
  }
  .rb-review-media {
    position: absolute !important;
    top: 50% !important;
    left: 50% !important;
    transform: translate(-50%, -50%) !important;
    max-width: 100% !important;
    max-height: 100% !important;
    width: auto !important;
    height: auto !important;
    object-fit: contain !important;
    background: #000;
    display: none;
  }
  .rb-review-media.rb-review-active {
    display: block !important;
    visibility: visible !important;
    opacity: 1 !important;
  }
  /* rb-body must be positioning context for absolute review */
  .rb-body {
    position: relative !important;
  }
</style>
"""
