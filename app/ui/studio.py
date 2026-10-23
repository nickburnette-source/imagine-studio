"""Main pipeline studio UI."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from nicegui import app, ui

from app.config import GENERATE_BACKEND, OUTPUT_DIR
from app.models.rally import (
    BRANCH_STEPS,
    FINAL_REVIEW_STEPS,
    PIPELINE_STEPS,
    RallyPackage,
    StepStatus,
    list_rallies,
    slugify,
)
from app.pipeline import graph
from app.pipeline.assets import media_url, write_upload_bytes
from app.pipeline.compress import compress_rally_ios, fmt_bytes
from app.pipeline.doctor import run_doctor
from app.pipeline.generate import generate_step
from app.pipeline.expand import (
    RallyBrief,
    apply_concept_pack,
    clear_brief_store,
    expand_concept,
    load_brief_store,
    persist_brief,
)
from app.pipeline.prompts import (
    apply_template_to_package_steps,
    connection_label,
    default_step_states,
    list_templates,
    refresh_composed,
    refresh_package_composed,
    step_graph,
)
from app.pipeline.upload import sanitize_display_name, upload_rally

# Session globals (single-user local app)
_current: RallyPackage | None = None
_brief: RallyBrief | None = None
_brief_open: bool = False
_selected_step: str = "Preview"
# Background generate jobs — keyed by "{rally}/{step}" so work survives
# leaving a rally and can be shown in a site-wide queue.
_gen_tasks: dict[str, asyncio.Task] = {}
_gen_detail: dict[str, str] = {}
# Force edit UI even when all steps approved (escape hatch from final review)
_force_edit: bool = False

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


def _job_key(rally: str, step: str) -> str:
    return f"{rally}/{step}"


def _parse_job_key(key: str) -> tuple[str, str]:
    rally, _, step = key.partition("/")
    return rally, step


def _active_jobs() -> list[tuple[str, str, str]]:
    """Return [(key, rally, step), ...] for jobs still running."""
    out: list[tuple[str, str, str]] = []
    for key, task in list(_gen_tasks.items()):
        if task.done():
            continue
        rally, step = _parse_job_key(key)
        out.append((key, rally, step))
    return out


def _is_generating(rally: str, step: str) -> bool:
    key = _job_key(rally, step)
    task = _gen_tasks.get(key)
    return task is not None and not task.done()


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
    return "Reschedule" if step == "Adjust" else step


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
            prev = StepStatus.READY
            if sid == "Preview":
                prev = StepStatus.READY
            elif sid == "Idle":
                prev = (
                    StepStatus.READY
                    if pkg.gates.get("preview")
                    and pkg.gates["preview"].value == "approved"
                    else StepStatus.LOCKED
                )
            elif sid in BRANCH_STEPS:
                prev = (
                    StepStatus.READY
                    if pkg.gates.get("idle")
                    and pkg.gates["idle"].value == "approved"
                    else StepStatus.LOCKED
                )
            st.status = prev
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


def _studio_phase(pkg: RallyPackage) -> str:
    """edit | final_review | publish"""
    if pkg.final_review_approved:
        return "publish"
    if (
        not _force_edit
        and pkg.all_steps_approved()
        and pkg.is_package_complete()
    ):
        return "final_review"
    return "edit"


def mount() -> None:
    """Register routes / build root page."""

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
        )
        with ui.header().classes(
            "items-center justify-between px-4 dc-header"
        ):
            with ui.row().classes("items-center gap-2"):
                ui.label("DutyCall").classes(
                    "text-h6 text-weight-bold"
                ).style("color: #37BD39; letter-spacing: 0.02em;")
                ui.label("Rally Builder").classes(
                    "text-subtitle1 text-grey-5"
                )
            with ui.row().classes("items-center gap-2"):
                _activity_center()
                ui.badge(GENERATE_BACKEND).props(
                    "outline dense color=primary"
                )
                ui.button(
                    icon="health_and_safety", on_click=_open_doctor
                ).props("flat dense round").tooltip("Doctor")
        if _brief_open and _brief is not None:
            _brief_view()
        else:
            _home_view()

    def _open_rally(path: Path, step: str | None = None) -> None:
        """Load a rally into the studio, optionally selecting a step."""
        global _current, _selected_step, _force_edit
        _current = RallyPackage.load(path)
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
            _selected_step = "Preview"
        _force_edit = False
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
                    f"Rally “{rally}” not found",
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
                        lbl.text = (_gen_detail.get(key) or "Working…")[:90]
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
                                detail = (
                                    _gen_detail.get(key) or "Working…"
                                )[:90]
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
        raw = (raw or "").strip().lower()
        ids = {t["id"]: t for t in list_templates()}
        if raw in ids:
            return raw
        for t in ids.values():
            if str(t.get("label") or "").strip().lower() == raw:
                return t["id"]
        return "character"

    def _home_view() -> None:
        """Landing: concept + kind → Grok invents scenes (pipeline starts later)."""
        global _brief
        form = {
            "name": _brief.name if _brief else "",
            "template": (
                _brief.template
                if _brief
                else "character"
            ),
            "concept": _brief.concept if _brief else "",
            "aspect": _brief.aspect_ratio if _brief else "9:16",
        }
        if form["template"] not in ("character", "scene"):
            form["template"] = _normalize_template(form["template"])

        with ui.column().classes(
            "w-full max-w-4xl mx-auto q-pa-md gap-5 rb-home"
        ):
            # ── Create card ──────────────────────────────────────
            with ui.element("div").classes("dc-card w-full q-pa-md"):
                ui.label("New rally").classes(
                    "text-h6 text-weight-medium q-mb-md"
                )

                ui.label("Name").classes("dc-field-title")
                name_in = (
                    ui.input(value=form["name"])
                    .classes("w-full dc-input")
                    .props(
                        'outlined dense dark '
                        'placeholder="Folder name on disk (e.g. ah-6-littlebird)"'
                    )
                )
                name_in.bind_value(form, "name")

                ui.label("Kind").classes("dc-field-title q-mt-md")
                templates = list_templates()
                tmpl_options = {
                    t["id"]: t["label"] for t in templates
                } or {"character": "Character", "scene": "Scene"}
                kind_in = (
                    ui.toggle(tmpl_options, value=form["template"])
                    .classes("w-full")
                    .props("no-caps unelevated")
                )
                kind_in.bind_value(form, "template")
                tmpl_hint = ui.label("").classes("dc-hint")

                def _refresh_tmpl_hint() -> None:
                    tid = _normalize_template(
                        str(form.get("template") or "character")
                    )
                    desc = next(
                        (
                            t.get("description") or ""
                            for t in templates
                            if t["id"] == tid
                        ),
                        "",
                    )
                    tmpl_hint.text = desc or (
                        "Character = face/body · Scene = place / tableau · "
                        "Craft = photoreal vehicle"
                    )

                _refresh_tmpl_hint()
                kind_in.on(
                    "update:model-value", lambda: _refresh_tmpl_hint()
                )

                ui.label("Concept").classes("dc-field-title q-mt-md")
                concept_in = (
                    ui.textarea(value=form["concept"])
                    .classes("w-full dc-input")
                    .props(
                        'outlined dense dark autogrow '
                        'placeholder="An AH-6 Littlebird on the tarmac '
                        'gearing up for a special operations mission"'
                    )
                )
                concept_in.bind_value(form, "concept")
                ui.label(
                    "Grok invents a scene for each clip. You review and edit "
                    "those prompts before the pipeline starts."
                ).classes("dc-hint")

                ui.label("Aspect").classes("dc-field-title q-mt-md")
                aspect = (
                    ui.select(["9:16", "1:1"], value=form["aspect"])
                    .classes("w-40 dc-input")
                    .props("outlined dense dark")
                )
                aspect.bind_value(form, "aspect")
                ui.label(
                    "Phone skins use 9:16 · square for rare square packs"
                ).classes("dc-hint")

                async def invent() -> None:
                    global _brief, _brief_open
                    concept = (form.get("concept") or "").strip()
                    if not concept:
                        ui.notify(
                            "Type a concept first — that’s what Grok riffs on",
                            type="warning",
                        )
                        return
                    tid = _normalize_template(
                        str(form.get("template") or "character")
                    )
                    name = slugify(
                        form.get("name") or concept[:48] or "rally"
                    )
                    invent_btn.props("loading")
                    try:
                        ui.notify("Inventing scenes from concept…")
                        pack = await expand_concept(
                            concept,
                            template=tid,
                            aspect_ratio=str(form.get("aspect") or "9:16"),
                        )
                        _brief = RallyBrief(
                            name=name,
                            template=tid,
                            concept=concept,
                            aspect_ratio=str(form.get("aspect") or "9:16"),
                        )
                        _brief.apply_pack(pack)
                        _brief_open = True
                        persist_brief(_brief, open=True)
                        ui.navigate.to("/")
                    except Exception as exc:  # noqa: BLE001
                        ui.notify(str(exc), type="negative")
                    finally:
                        invent_btn.props(remove="loading")

                invent_btn = (
                    ui.button(
                        "Invent scenes",
                        icon="auto_awesome",
                        on_click=invent,
                    )
                    .classes("w-full q-mt-lg dc-cta")
                    .props("unelevated color=secondary no-caps")
                )

            # ── Recent grid (newest first, Preview thumbnails) ───
            ui.label("Recent").classes("text-h6 text-weight-medium")
            rallies = list_rallies(OUTPUT_DIR)
            if not rallies:
                ui.label("No rallies yet — create one above.").classes(
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
            "name": brief.name,
            "template": brief.template,
            "concept": brief.concept,
            "aspect": brief.aspect_ratio,
            "look": brief.look,
        }
        beats = {
            sid: brief.beats.get(sid, "") for sid in PIPELINE_STEPS
        }
        defaults = default_step_states(brief.template)

        with ui.column().classes(
            "w-full max-w-4xl mx-auto q-pa-md gap-4 rb-brief"
        ):
            ui.label("Scene brief").classes("text-h6 text-weight-medium")
            ui.label(
                "These are the invented scenes for each clip. Edit anything. "
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

                ui.label("Kind").classes("dc-field-title q-mt-md")
                brief_tmpl_opts = {
                    t["id"]: t["label"] for t in list_templates()
                } or {
                    "character": "Character",
                    "scene": "Scene",
                    "craft": "Craft",
                }
                kind_in = (
                    ui.toggle(
                        brief_tmpl_opts,
                        value=form["template"],
                    )
                    .props("no-caps unelevated")
                )
                kind_in.bind_value(form, "template")

                ui.label("Concept").classes("dc-field-title q-mt-md")
                concept_in = (
                    ui.textarea(value=form["concept"])
                    .classes("w-full dc-input")
                    .props("outlined dense dark autogrow")
                )
                concept_in.bind_value(form, "concept")

                ui.label("Look lock").classes("dc-field-title q-mt-md")
                look_in = (
                    ui.textarea(value=form["look"])
                    .classes("w-full dc-input")
                    .props("outlined dense dark autogrow")
                )
                look_in.bind_value(form, "look")
                ui.label(
                    "Subject, palette, lighting, camera — carried into every clip"
                ).classes("dc-hint")

            for sid in PIPELINE_STEPS:
                st = defaults.get(sid)
                title = "Reschedule" if sid == "Adjust" else sid
                role = (st.role if st else "") or ""
                conn = connection_label(sid, form["template"])
                with ui.element("div").classes("rb-brief-step w-full"):
                    with ui.row().classes(
                        "w-full items-baseline justify-between no-wrap"
                    ):
                        ui.label(title).classes("text-weight-medium")
                        if role:
                            ui.label(role).classes("text-caption text-grey-6")
                    ui.label(conn).classes("text-caption text-teal-4 q-mb-xs")
                    area = (
                        ui.textarea(value=beats.get(sid, ""))
                        .classes("w-full dc-input")
                        .props("outlined dense dark autogrow")
                    )
                    area.bind_value(beats, sid)

            def _sync_brief(*, open: bool | None = None) -> None:
                assert _brief is not None
                _brief.name = slugify(form.get("name") or _brief.concept[:48] or "rally")
                _brief.template = _normalize_template(
                    str(form.get("template") or "character")
                )
                _brief.concept = (form.get("concept") or "").strip()
                _brief.look = (form.get("look") or "").strip()
                _brief.aspect_ratio = str(form.get("aspect") or "9:16")
                _brief.beats = dict(beats)
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
                recompose_btn.props("loading")
                try:
                    pack = await expand_concept(
                        concept,
                        template=_brief.template,
                        aspect_ratio=_brief.aspect_ratio,
                    )
                    _brief.apply_pack(pack)
                    persist_brief(_brief, open=True)
                    ui.notify("Scenes rewritten from concept", type="positive")
                    ui.navigate.to("/")
                except Exception as exc:  # noqa: BLE001
                    ui.notify(str(exc), type="negative")
                finally:
                    recompose_btn.props(remove="loading")

            def start_pipeline() -> None:
                global _current, _selected_step, _force_edit, _brief_open, _brief
                _sync_brief()
                b = _brief
                assert b is not None
                if not b.concept:
                    ui.notify("Concept is empty", type="warning")
                    return
                name = slugify(b.name or b.concept[:48] or "rally")
                root = OUTPUT_DIR / name
                if root.exists() and (root / "meta.json").exists():
                    ui.notify(
                        f"“{name}” already exists — pick another name",
                        type="warning",
                    )
                    return
                root.mkdir(parents=True, exist_ok=True)
                pkg = graph.new_rally(
                    name=name,
                    concept=b.concept,
                    root=root,
                    aspect_ratio=b.aspect_ratio,
                    template=b.template,
                )
                b.apply_to_package(pkg)
                pkg.save()
                _current = pkg
                _selected_step = "Preview"
                _force_edit = False
                _brief_open = False
                _brief = None
                clear_brief_store()
                ui.notify(f"Pipeline ready · {name}", type="positive")
                ui.navigate.to("/")

            with ui.element("div").classes("rb-brief-sticky w-full"):
                with ui.row().classes("w-full gap-2 no-wrap"):
                    ui.button("Back", icon="arrow_back", on_click=back).props(
                        "flat no-caps"
                    )
                    recompose_btn = (
                        ui.button(
                            "Recompose",
                            icon="refresh",
                            on_click=recompose,
                        ).props("outline no-caps")
                    )
                    ui.button(
                        "Start pipeline",
                        icon="play_arrow",
                        on_click=start_pipeline,
                    ).classes("col dc-cta").props(
                        "unelevated color=secondary no-caps"
                    )

            ui.timer(1.5, lambda: _sync_brief(open=True))

    def _recent_card(path: Path) -> None:
        """DutyCall-style skin tile: Preview.png if present, else placeholder."""
        try:
            pkg = RallyPackage.load(path)
            title = pkg.display_name or path.name
            subtitle = (pkg.concept or "").strip()
            tmpl = getattr(pkg, "template", "character") or "character"
        except Exception:  # noqa: BLE001
            title = path.name
            subtitle = ""
            tmpl = ""

        preview = path / "Preview.png"
        bust = (
            int(preview.stat().st_mtime * 1000) if preview.is_file() else 0
        )
        from urllib.parse import quote

        preview_url = (
            f"/media/{quote(path.name)}/Preview.png?t={bust}"
            if preview.is_file()
            else ""
        )

        def open_pkg(p: Path = path) -> None:
            _open_rally(p)

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
            with ui.element("div").classes("dc-rally-meta"):
                ui.label(title).classes("dc-rally-title")
                if subtitle:
                    ui.label(subtitle).classes("dc-rally-sub")
                if tmpl:
                    ui.label(tmpl).classes("dc-rally-tmpl")

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
        global _current, _force_edit
        # Keep background jobs running — top-right Activity tracks them.
        n_run = sum(1 for t in _gen_tasks.values() if not t.done())
        _current = None
        _force_edit = False
        ui.run_javascript(
            "document.body.classList.remove('rb-studio-active');"
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
        phase = _studio_phase(pkg)

        # Chrome
        with ui.row().classes(
            "w-full items-center no-wrap q-px-xs gap-1 rb-chrome"
        ).style(
            "border-bottom: 1px solid rgba(255,255,255,0.08); background: #0d0e14;"
        ):
            ui.button(icon="arrow_back", on_click=_close_rally).props(
                "flat dense round size=sm"
            ).tooltip("Close rally")
            with ui.column().classes("gap-0").style(
                "flex-shrink: 1; min-width: 8rem; max-width: 18rem;"
            ):
                ui.label(pkg.name).classes("text-weight-bold text-caption").style(
                    "overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"
                )
                tmpl = getattr(pkg, "template", "character") or "character"
                concept_bit = (pkg.concept or "").strip().replace("\n", " ")
                sub = tmpl if not concept_bit else f"{tmpl} · {concept_bit}"
                ui.label(sub).classes("text-caption text-grey-6").style(
                    "font-size: 0.65rem; line-height: 1.2; "
                    "overflow: hidden; text-overflow: ellipsis; "
                    "white-space: nowrap;"
                )

            # Same icon stepper in edit + final review (highlight = current edit step
            # or currently playing review clip — JS updates data-review-step classes).
            if phase in ("edit", "final_review"):
                # Edit uses pipeline order; final review uses product playback order
                step_order = (
                    FINAL_REVIEW_STEPS
                    if phase == "final_review"
                    else PIPELINE_STEPS
                )
                with ui.row().classes(
                    "items-center justify-center gap-0 col no-wrap"
                ).style("flex: 1; min-width: 0; overflow-x: auto;").props(
                    'id="rb-chrome-stepper"'
                ):
                    for sid in step_order:
                        st = pkg.step(sid)
                        status = st.status
                        jkey = _job_key(pkg.name, sid)
                        if _is_generating(pkg.name, sid):
                            status = StepStatus.RUNNING
                        if phase == "edit":
                            selected = sid == _selected_step
                        else:
                            # First review item until JS highlights the playing clip
                            selected = sid == step_order[0]
                        color = _status_color(status)
                        icon = _STEP_ICONS.get(sid, "circle")
                        locked = (
                            phase == "edit" and status == StepStatus.LOCKED
                        )
                        # Product label: Adjust = Reschedule in the live app
                        display = "Reschedule" if (
                            phase == "final_review" and sid == "Adjust"
                        ) else sid

                        def make_handler(step_id: str, ph: str = phase):
                            def handler() -> None:
                                global _selected_step
                                if ph == "final_review":
                                    ui.run_javascript(
                                        f"window.__rbReviewJump && "
                                        f"window.__rbReviewJump({json.dumps(step_id)})"
                                    )
                                    return
                                if _pkg().step(step_id).status == StepStatus.LOCKED:
                                    ui.notify(
                                        f"{step_id} is locked", type="warning"
                                    )
                                    return
                                _selected_step = step_id
                                refresh_all()

                            return handler

                        props = f"flat dense size=sm color={color}"
                        if selected:
                            props += " unelevated"
                        classes = f"rb-stepper-btn rb-review-sid-{sid}"
                        if selected:
                            classes += " rb-stepper-current"
                        btn = (
                            ui.button(
                                display, icon=icon, on_click=make_handler(sid)
                            )
                            .classes(classes)
                            .props(props)
                        )
                        if locked:
                            btn.props("disable")
                        tip = f"{st.label} · {status.value}"
                        if jkey in _gen_detail:
                            tip += f" — {_gen_detail[jkey][:80]}"
                        if phase == "final_review":
                            tip = f"{st.label} · jump to this clip"
                        btn.tooltip(tip)
            else:
                with ui.row().classes("items-center justify-center col"):
                    ui.label("Publish").classes("text-subtitle2 text-weight-medium")

            if phase == "final_review":
                ui.button(
                    "Replay",
                    icon="replay",
                    on_click=lambda: ui.run_javascript(
                        "window.__rbReviewReplay && window.__rbReviewReplay()"
                    ),
                ).props("outline dense")

                def approve_review_chrome() -> None:
                    try:
                        ui.run_javascript(
                            "window.__rbReviewStop && window.__rbReviewStop()"
                        )
                        graph.approve_final_review(_pkg())
                        ui.notify(
                            "Final review approved — choose a publish tier",
                            type="positive",
                        )
                        refresh_all()
                    except Exception as exc:  # noqa: BLE001
                        ui.notify(str(exc), type="negative")

                ui.button(
                    "Approve review",
                    icon="verified",
                    on_click=approve_review_chrome,
                ).props("color=positive unelevated dense")
                ui.button(
                    "Edit",
                    icon="edit",
                    on_click=lambda: _back_to_edit(refresh_all),
                ).props("flat dense")

            _activity_center()
            backend_color = {
                "cli": "secondary",
                "api": "positive",
                "stub": "grey",
            }.get(GENERATE_BACKEND, "grey")
            ui.badge(GENERATE_BACKEND).props(
                f"outline dense color={backend_color}"
            )
            ui.button(icon="health_and_safety", on_click=_open_doctor).props(
                "flat dense round size=sm"
            ).tooltip("Doctor")

        body = ui.element("div").classes("rb-body w-full")
        with body:
            if phase == "final_review":
                _render_final_review(refresh_all)
            elif phase == "publish":
                _render_publish(refresh_all)
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

    def _back_to_edit(refresh_all) -> None:
        global _force_edit
        _force_edit = True
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

    # ── Edit mode ────────────────────────────────────────────────
    def _render_edit(refresh_all) -> None:
        pkg = _pkg()
        step_id = _selected_step
        st = pkg.step(step_id)
        if step_id == "Idle":
            st.options.setdefault("generate_mp4", True)
            st.options.setdefault("generate_png", False)
            st.options.setdefault("boomerang", True)

        # Banner: this step and/or other parallel jobs
        job_banner = (
            ui.element("div")
            .classes("w-full q-px-sm")
            .style(
                "display:none; flex: 0 0 auto; padding-top: 4px; padding-bottom: 4px; "
                "background: rgba(124,92,255,0.18); "
                "border-bottom: 1px solid rgba(124,92,255,0.45);"
            )
        )
        with job_banner:
            with ui.row().classes("items-center gap-2 no-wrap w-full"):
                job_spinner = ui.spinner("dots", size="1.2em", color="primary")
                with ui.column().classes("gap-0 col"):
                    job_title = ui.label("Working…").classes(
                        "text-caption text-weight-medium"
                    )
                    job_detail = ui.label("").classes("text-caption text-grey-4")
                job_icon = ui.icon("hourglass_top", color="primary")

        def set_job(
            *,
            visible: bool,
            title: str = "",
            detail: str = "",
            kind: str = "running",
        ) -> None:
            # refresh_all() / navigate deletes these; background jobs must no-op.
            if not _el_alive(job_banner):
                return
            base = "flex: 0 0 auto; padding-top: 4px; padding-bottom: 4px; border-bottom: 1px solid "
            if not visible:
                job_banner.style(
                    "display:none; " + base + "rgba(124,92,255,0.45); "
                    "background: rgba(124,92,255,0.18);"
                )
                return
            colors = {
                "running": ("rgba(124,92,255,0.45)", "rgba(124,92,255,0.18)"),
                "success": ("rgba(45,180,120,0.5)", "rgba(45,180,120,0.18)"),
                "error": ("rgba(220,80,80,0.55)", "rgba(200,60,60,0.2)"),
            }
            border, bg = colors.get(kind, colors["running"])
            job_banner.style(
                f"display:block; {base}{border}; background: {bg};"
            )
            if _el_alive(job_title):
                job_title.text = title
            if _el_alive(job_detail):
                job_detail.text = detail
            if _el_alive(job_spinner):
                job_spinner.set_visibility(kind == "running")
            if _el_alive(job_icon):
                job_icon.set_visibility(kind != "running")
                if kind == "success":
                    job_icon.props("name=check_circle color=positive")
                elif kind == "error":
                    job_icon.props("name=error color=negative")
                else:
                    job_icon.props("name=hourglass_top color=primary")

        # Show live status for current step or summary of parallel jobs.
        # Progress is polled from _gen_detail (not pushed from the job callback),
        # so refresh_all() deleting this banner cannot warn/use-after-free.
        cur_key = _job_key(pkg.name, step_id)
        if _is_generating(pkg.name, step_id):
            set_job(
                visible=True,
                title=f"Generating {step_id}…",
                detail=_gen_detail.get(cur_key, "Running in background"),
                kind="running",
            )

            def _poll_job_banner() -> None:
                if not _el_alive(job_banner):
                    return
                if not _is_generating(pkg.name, step_id):
                    return
                set_job(
                    visible=True,
                    title=f"Generating {step_id}…",
                    detail=_gen_detail.get(
                        cur_key, "Running in background"
                    )[:500],
                    kind="running",
                )

            ui.timer(0.5, _poll_job_banner)
        elif st.status == StepStatus.ERROR and st.error:
            set_job(
                visible=True,
                title=f"{step_id} failed",
                detail=st.error[:500],
                kind="error",
            )
        else:
            others = [
                f"{r}/{s}" if r != pkg.name else s
                for k, r, s in _active_jobs()
                if not (r == pkg.name and s == step_id)
            ]
            if others:
                set_job(
                    visible=True,
                    title=f"Also generating: {', '.join(others)}",
                    detail="You can keep working — jobs run in parallel",
                    kind="running",
                )

        with ui.element("div").classes("rb-main-row w-full"):
            with ui.element("div").classes("rb-stage"):
                _media_stage(pkg, step_id, refresh_all)

            with ui.element("div").classes("rb-edit-panel"):
                # ── Actions (fixed header — not sticky-inside-scroll) ──
                with ui.element("div").classes("rb-edit-actions"):
                    with ui.row().classes("w-full gap-2 no-wrap"):
                        gen_btn = (
                            ui.button("Generate", icon="auto_awesome")
                            .classes("col")
                            .props("unelevated color=primary no-caps")
                        )
                        approve_btn = (
                            ui.button("Approve", icon="check_circle")
                            .classes("col")
                            .props("outline no-caps")
                        )

                        def heart_from_actions() -> None:
                            try:
                                entry = _pkg().save_favorite(step_id)
                                ui.notify(
                                    f"Hearted · saved as fallback ({entry.id})",
                                    type="positive",
                                )
                                refresh_all()
                            except Exception as exc:  # noqa: BLE001
                                ui.notify(str(exc), type="negative")

                        has_media = bool(pkg.existing_outputs(step_id)) or (
                            step_id == "Preview"
                            and pkg.path_for("Preview.png").is_file()
                        )
                        heart_btn = (
                            ui.button(icon="favorite", on_click=heart_from_actions)
                            .props("unelevated color=pink round")
                            .tooltip(
                                "Heart this take as a fallback"
                                if has_media
                                else "Generate something before hearting"
                            )
                        )
                        heart_btn.set_enabled(has_media)

                    can_gen, gen_reason = graph.can_generate(pkg, step_id)
                    can_app, app_reason = graph.can_approve(pkg, step_id)
                    already = _is_generating(pkg.name, step_id)
                    gen_btn.set_enabled(can_gen and not already)
                    approve_btn.set_enabled(can_app)
                    if already:
                        gen_btn.tooltip("Already generating this step")
                    elif not can_gen and gen_reason:
                        gen_btn.tooltip(gen_reason)
                    if not can_app and app_reason:
                        approve_btn.tooltip(app_reason)

                # ── Scrollable form ──────────────────────────────────
                with ui.element("div").classes("rb-edit-scroll"):
                    if st.error and st.status == StepStatus.NEEDS_REVIEW:
                        with ui.element("div").classes("rb-callout rb-callout-warn"):
                            ui.label(st.error).classes("text-caption").style(
                                "white-space: pre-wrap"
                            )

                    if (
                        pkg.all_steps_approved()
                        and pkg.is_package_complete()
                        and not pkg.final_review_approved
                    ):
                        with ui.element("div").classes(
                            "rb-callout rb-callout-ok"
                        ):
                            ui.label("All steps approved").classes(
                                "text-caption text-weight-medium"
                            )
                            ui.button(
                                "Start final review",
                                icon="movie",
                                on_click=lambda: _enter_final_review(
                                    refresh_all
                                ),
                            ).props(
                                "color=secondary unelevated dense no-caps"
                            ).classes("w-full q-mt-sm")

                    if (pkg.concept or "").strip():
                        with ui.element("div").classes("rb-callout rb-callout-ok"):
                            ui.label("Concept").classes(
                                "text-caption text-grey-5"
                            )
                            ui.label(pkg.concept.strip()).classes(
                                "text-body2"
                            ).style("white-space: pre-wrap")

                    with ui.element("div").classes("rb-field"):
                        ui.label("Concept").classes("rb-field-label")
                        ui.label(
                            "What this rally is about — carried into every prompt"
                        ).classes("rb-field-hint")
                        concept_area = (
                            ui.textarea(value=pkg.concept)
                            .classes("w-full rb-prompt")
                            .props(
                                "outlined autogrow "
                                'input-style="min-height: 3.2rem; '
                                'max-height: 8rem; line-height: 1.45;"'
                            )
                        )

                    look_area_holder: dict = {"el": None}

                    async def compose_beats() -> None:
                        p = _pkg()
                        p.concept = (concept_area.value or "").strip()
                        look_el = look_area_holder["el"]
                        if look_el is not None:
                            p.skill_note = (look_el.value or "").strip()
                        if not p.concept:
                            ui.notify(
                                "Type a concept first", type="warning"
                            )
                            return
                        compose_btn.props("loading")
                        try:
                            pack = await expand_concept(
                                p.concept,
                                template=getattr(
                                    p, "template", "character"
                                )
                                or "character",
                                aspect_ratio=p.aspect_ratio,
                            )
                            apply_concept_pack(p, pack)
                            p.save()
                            ui.notify(
                                "Composed on-theme beats for every step",
                                type="positive",
                            )
                            refresh_all()
                        except Exception as exc:  # noqa: BLE001
                            ui.notify(str(exc), type="negative")
                        finally:
                            compose_btn.props(remove="loading")

                    compose_btn = (
                        ui.button(
                            "Compose beats",
                            icon="auto_awesome",
                            on_click=compose_beats,
                        )
                        .props(
                            "unelevated dense no-caps color=secondary"
                        )
                        .classes("w-full")
                        .tooltip(
                            "Grok fills custom guidance on all steps "
                            "from the concept (keeps start/end/cut harness)"
                        )
                    )

                    with ui.element("div").classes("rb-field"):
                        ui.label("Look lock").classes("rb-field-label")
                        ui.label(
                            "Subject, palette, lighting, camera — prepended "
                            "to every Imagine prompt"
                        ).classes("rb-field-hint")
                        look_area = (
                            ui.textarea(
                                value=pkg.skill_note or "",
                                placeholder="Filled by Compose beats…",
                            )
                            .classes("w-full rb-prompt")
                            .props(
                                "outlined autogrow "
                                'input-style="min-height: 3.2rem; '
                                'max-height: 8rem; line-height: 1.45;"'
                            )
                        )
                        look_area_holder["el"] = look_area

                    chk_mp4 = chk_png = chk_boom = None  # type: ignore[assignment]
                    if step_id == "Idle":
                        with ui.element("div").classes("rb-field"):
                            ui.label("Idle outputs").classes("rb-field-label")
                            chk_mp4 = ui.checkbox(
                                "Idle.mp4 (default)",
                                value=bool(
                                    st.options.get("generate_mp4", True)
                                ),
                            )
                            chk_png = ui.checkbox(
                                "IdleKeyframe.png",
                                value=bool(
                                    st.options.get("generate_png", False)
                                ),
                            )
                            chk_boom = ui.checkbox(
                                "Boomerang (forward + reverse, mute)",
                                value=bool(st.options.get("boomerang", True)),
                            )
                            ui.label(
                                "Toggles the current Idle.mp4 now — does not regenerate. "
                                "Off restores the original take."
                            ).classes("rb-field-hint")

                            def sync_idle_options() -> None:
                                p = _pkg()
                                s = p.step("Idle")
                                s.options["generate_mp4"] = bool(
                                    chk_mp4.value
                                )
                                s.options["generate_png"] = bool(
                                    chk_png.value
                                )
                                if (
                                    not s.options["generate_mp4"]
                                    and not s.options["generate_png"]
                                ):
                                    s.options["generate_mp4"] = True
                                    chk_mp4.value = True
                                p.save()

                            chk_mp4.on(
                                "update:model-value",
                                lambda: sync_idle_options(),
                            )
                            chk_png.on(
                                "update:model-value",
                                lambda: sync_idle_options(),
                            )

                            async def on_boomerang_toggle(_e=None) -> None:
                                p = _pkg()
                                s = p.step("Idle")
                                want = bool(chk_boom.value)
                                prev = bool(s.options.get("boomerang", True))
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
                                chk_boom.props("disable")
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
                                except Exception as exc:  # noqa: BLE001
                                    s.options["boomerang"] = prev
                                    chk_boom.value = prev
                                    p.save()
                                    ui.notify(str(exc), type="negative")
                                finally:
                                    chk_boom.props(remove="disable")

                            chk_boom.on(
                                "update:model-value",
                                on_boomerang_toggle,
                            )

                    if step_id != "Preview":
                        tid_now = (
                            getattr(pkg, "template", "character")
                            or "character"
                        )
                        g = step_graph(step_id, tid_now)
                        start_key = str(g.get("start") or "preview")
                        start_file = {
                            "preview": pkg.path_for("Preview.png"),
                            "idle": pkg.path_for("IdleKeyframe.png"),
                            "idle_last": (
                                pkg.path_for("Idle.last.png")
                                if pkg.path_for("Idle.last.png").is_file()
                                else pkg.path_for("IdleKeyframe.png")
                            ),
                        }.get(start_key)
                        if start_file is None or not start_file.is_file():
                            start_file = pkg.path_for("Preview.png")
                        with ui.row().classes(
                            "w-full items-center gap-3 no-wrap rb-lock-chip"
                        ):
                            if start_file.is_file():
                                lock_url = _media_url(
                                    pkg, start_file.name, start_file
                                )
                                ui.html(
                                    f'<img src="{lock_url}" alt="lock" '
                                    f'style="height:44px;width:26px;'
                                    f'object-fit:cover;border-radius:5px;" />',
                                    sanitize=False,
                                )
                            ui.label(
                                connection_label(step_id, tid_now)
                            ).classes("text-caption text-grey-5")

                    with ui.element("div").classes("rb-field"):
                        ui.label("Template").classes("rb-field-label")
                        with ui.row().classes(
                            "w-full items-end gap-2 no-wrap"
                        ):
                            tmpl_now = (
                                getattr(pkg, "template", "character")
                                or "character"
                            )
                            tmpl_opts = {
                                t["id"]: t["label"] for t in list_templates()
                            }
                            if tmpl_now not in tmpl_opts:
                                tmpl_opts[tmpl_now] = tmpl_now.title()
                            tmpl_sel = (
                                ui.select(tmpl_opts, value=tmpl_now)
                                .classes("col")
                                .props("outlined dense")
                            )

                            def apply_tmpl() -> None:
                                p = _pkg()
                                tid = str(tmpl_sel.value or "character")
                                apply_template_to_package_steps(
                                    p.steps, tid, overwrite_base=True
                                )
                                p.template = tid
                                refresh_package_composed(p)
                                p.save()
                                ui.notify(
                                    f"Applied “{tid}” base prompts to all steps",
                                    type="positive",
                                )
                                refresh_all()

                            ui.button(
                                "Apply bases",
                                icon="auto_fix",
                                on_click=apply_tmpl,
                            ).props("outline dense no-caps").tooltip(
                                "Overwrite all step base prompts from this "
                                "template (keeps your custom guidance)"
                            )

                    with ui.element("div").classes("rb-field"):
                        ui.label("Base prompt").classes("rb-field-label")
                        ui.label(
                            "Optional beat between start and end. "
                            "Connections are added automatically — "
                            "don't describe the product or UI."
                        ).classes("rb-field-hint")
                        base_area = (
                            ui.textarea(value=st.base_prompt)
                            .classes("w-full rb-prompt")
                            .props(
                                "outlined autogrow "
                                'input-style="min-height: 4rem; '
                                'max-height: 10rem; line-height: 1.45;"'
                            )
                        )
                        base_area.set_enabled(
                            st.status != StepStatus.LOCKED
                        )

                    with ui.element("div").classes("rb-field"):
                        ui.label("Custom guidance").classes(
                            "rb-field-label"
                        )
                        ui.label(
                            "What happens in the middle of the clip"
                        ).classes("rb-field-hint")
                        custom_area = (
                            ui.textarea(
                                value=st.custom_guidance,
                                placeholder=st.custom_placeholder
                                or "Pose, voice line, mood…",
                            )
                            .classes("w-full rb-prompt")
                            .props(
                                "outlined autogrow "
                                'input-style="min-height: 4.5rem; '
                                'max-height: 12rem; line-height: 1.45;"'
                            )
                        )
                        custom_area.set_enabled(
                            st.status != StepStatus.LOCKED
                        )

                    composed_value = refresh_composed(
                        st,
                        pkg.concept,
                        bible=pkg.skill_note or "",
                        template_id=getattr(pkg, "template", "character"),
                    )
                    with ui.element("div").classes(
                        "rb-field rb-composed-field"
                    ):
                        ui.label("Composed prompt").classes(
                            "rb-field-label"
                        )
                        ui.label(
                            "Exact text sent to the model (starts / ends / cuts)"
                        ).classes("rb-field-hint")
                        composed_preview = (
                            ui.textarea(value=composed_value)
                            .classes("w-full rb-prompt rb-composed")
                            .props(
                                "outlined readonly "
                                'input-style="min-height: 10rem; '
                                "max-height: min(40vh, 22rem); "
                                "overflow-y: auto; line-height: 1.5; "
                                "font-family: ui-monospace, SFMono-Regular, "
                                "Menlo, Monaco, Consolas, monospace; "
                                'font-size: 0.78rem;"'
                            )
                        )

                    def sync_prompts() -> None:
                        p = _pkg()
                        s = p.step(step_id)
                        s.base_prompt = base_area.value or ""
                        s.custom_guidance = custom_area.value or ""
                        p.concept = (concept_area.value or "").strip()
                        if look_area is not None:
                            p.skill_note = (look_area.value or "").strip()
                        if step_id == "Idle" and chk_mp4 is not None:
                            s.options["generate_mp4"] = bool(chk_mp4.value)
                            s.options["generate_png"] = bool(chk_png.value)
                        composed_preview.value = refresh_composed(
                            s,
                            p.concept,
                            bible=p.skill_note or "",
                            template_id=getattr(
                                p, "template", "character"
                            ),
                        )
                        p.save()

                    prompt_dirty = {"v": False}

                    def mark_prompt_dirty(_e=None) -> None:
                        prompt_dirty["v"] = True

                    def flush_prompts() -> None:
                        if not prompt_dirty["v"]:
                            return
                        prompt_dirty["v"] = False
                        sync_prompts()

                    base_area.on("blur", lambda: sync_prompts())
                    custom_area.on("blur", lambda: sync_prompts())
                    concept_area.on("blur", lambda: sync_prompts())
                    if look_area is not None:
                        look_area.on("blur", lambda: sync_prompts())
                    base_area.on("update:model-value", mark_prompt_dirty)
                    custom_area.on("update:model-value", mark_prompt_dirty)
                    concept_area.on("update:model-value", mark_prompt_dirty)
                    if look_area is not None:
                        look_area.on("update:model-value", mark_prompt_dirty)
                    ui.timer(1.0, flush_prompts)

                    async def on_upload(e) -> None:
                        """NiceGUI UploadEventArguments: e.file is a FileUpload."""
                        try:
                            f = getattr(e, "file", None)
                            if f is None:
                                raise RuntimeError(
                                    "Upload event missing file "
                                    f"(got {type(e).__name__})"
                                )
                            name = getattr(f, "name", None) or "upload.bin"
                            if hasattr(f, "read"):
                                data = await f.read()
                            else:
                                data = bytes(f)
                            if isinstance(data, str):
                                data = data.encode("utf-8")
                            dest = write_upload_bytes(
                                _pkg(), step_id, data, name
                            )
                            if step_id == "Preview" and dest.suffix.lower() in {
                                ".png",
                                ".jpg",
                                ".jpeg",
                                ".webp",
                            }:
                                import shutil

                                p = _pkg()
                                ref = p.path_for("Preview.reference.png")
                                if dest.resolve() != ref.resolve():
                                    shutil.copy2(dest, ref)
                                p.step("Preview").options[
                                    "use_reference"
                                ] = True
                                p.save()
                                ui.notify(
                                    f"Saved {dest.name} · will ground next "
                                    f"Generate via image edit",
                                    type="positive",
                                )
                            else:
                                ui.notify(
                                    f"Saved {dest.name}", type="positive"
                                )
                            refresh_all()
                        except Exception as exc:  # noqa: BLE001
                            ui.notify(str(exc), type="negative")

                    if step_id == "Preview":
                        with ui.element("div").classes("rb-field"):
                            ui.label("Reference still").classes(
                                "rb-field-label"
                            )
                            ui.label(
                                "Optional. Generate will image-edit this into "
                                "Preview.png at the rally aspect ratio."
                            ).classes("rb-field-hint")
                            ref_path = pkg.path_for("Preview.reference.png")
                            has_ref = ref_path.is_file()
                            if has_ref:
                                with ui.row().classes(
                                    "w-full items-center gap-2 no-wrap"
                                ):
                                    ui.label(
                                        f"Reference ready "
                                        f"({ref_path.stat().st_size // 1024} KB)"
                                    ).classes("text-caption text-positive")

                                    def clear_ref() -> None:
                                        p = _pkg()
                                        r = p.path_for(
                                            "Preview.reference.png"
                                        )
                                        r.unlink(missing_ok=True)
                                        p.step("Preview").options.pop(
                                            "use_reference", None
                                        )
                                        p.save()
                                        ui.notify(
                                            "Reference cleared", type="info"
                                        )
                                        refresh_all()

                                    ui.button(
                                        "Clear",
                                        icon="close",
                                        on_click=clear_ref,
                                    ).props("flat dense no-caps")
                            ui.upload(
                                label="Drop image → Preview reference",
                                on_upload=on_upload,
                                auto_upload=True,
                                max_files=1,
                            ).props(
                                "accept=image/png,image/jpeg,image/webp,"
                                ".png,.jpg,.jpeg,.webp flat dense bordered"
                            ).classes("w-full")
                    else:
                        with ui.expansion(
                            "Advanced · import file", icon="upload_file"
                        ).classes("w-full rb-advanced"):
                            canon = ", ".join(st.outputs) or step_id
                            accept = (
                                "image/*,video/*,.png,.jpg,.mp4,.webm"
                                if step_id == "Idle"
                                else "video/mp4,.mp4,.webm,.mov"
                            )
                            ui.upload(
                                label=f"Import → {canon}",
                                on_upload=on_upload,
                                auto_upload=True,
                                max_files=1,
                            ).props(
                                f"accept={accept} flat dense bordered"
                            ).classes("w-full")

                    # ── Hearted favorites (fallback takes) ─────────
                    _render_favorites_panel(pkg, step_id, refresh_all)

                def start_generate() -> None:
                    """Fire-and-forget so other steps / rallies can run in parallel."""
                    sync_prompts()
                    sid = step_id
                    # Capture package at start — do not use _pkg() later; the
                    # user may leave this rally while the job is still running.
                    job_pkg = _pkg()
                    rally = job_pkg.name
                    key = _job_key(rally, sid)
                    if _is_generating(rally, sid):
                        _push_notice(
                            "info",
                            f"{rally} · {_step_label(sid)} already generating",
                            rally=rally,
                            step=sid,
                        )
                        return
                    ok, reason = graph.can_generate(job_pkg, sid)
                    if not ok:
                        ui.notify(reason, type="warning")
                        return

                    async def job() -> None:
                        async def on_progress(msg: str) -> None:
                            # Shared state only — never touch UI from this
                            # callback. refresh_all() deletes the old banner;
                            # the mounted view polls _gen_detail instead.
                            _gen_detail[key] = msg
                            await asyncio.sleep(0)

                        try:
                            await generate_step(
                                job_pkg, sid, progress=on_progress
                            )
                            _push_notice(
                                "success",
                                f"{rally} · {_step_label(sid)} ready",
                                detail="Ready for review",
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
                            if _current is not None and _current.name == rally:
                                # Reopened rallies are a different object; sync.
                                if _current is not job_pkg:
                                    _sync_current_from_disk(
                                        rally, job_pkg.dir()
                                    )
                                try:
                                    refresh_all()
                                except Exception:  # noqa: BLE001
                                    pass

                    _gen_tasks[key] = asyncio.create_task(job())
                    _gen_detail[key] = "Starting…"
                    _push_notice(
                        "info",
                        f"Generating {rally} · {_step_label(sid)}",
                        detail="Leave anytime — progress is in Activity (top right)",
                        rally=rally,
                        step=sid,
                    )
                    refresh_all()

                def on_approve() -> None:
                    sync_prompts()
                    try:
                        graph.approve_step(_pkg(), step_id)
                        ui.notify(f"{step_id} approved", type="positive")
                        global _selected_step, _force_edit
                        p = _pkg()
                        if p.all_steps_approved() and p.is_package_complete():
                            _force_edit = False
                            ui.notify(
                                "All steps approved — starting final review",
                                type="positive",
                            )
                        elif step_id == "Preview":
                            _selected_step = "Idle"
                        elif step_id == "Idle":
                            _selected_step = "Rally"
                        elif step_id in BRANCH_STEPS:
                            for bid in BRANCH_STEPS:
                                if p.step(bid).status != StepStatus.APPROVED:
                                    _selected_step = bid
                                    break
                    except Exception as exc:  # noqa: BLE001
                        ui.notify(str(exc), type="negative")
                    refresh_all()

                gen_btn.on_click(start_generate)
                approve_btn.on_click(on_approve)

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
            rel = str(path.relative_to(pkg.dir()))
        except ValueError:
            rel = filename
        return f"{media_url(pkg, rel)}?t={bust}"

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
                ui.html(
                    f'<video class="rally-stage-video {cls}" src="{url}" '
                    f"controls autoplay loop playsinline muted></video>",
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
              });
            })();
            """
        )

    def _media_stage(
        pkg: RallyPackage, step_id: str, refresh_all=None
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
            for p in paths:
                if p.suffix.lower() in {".mp4", ".webm", ".mov"}:
                    primary = p
                    break
            secondaries = [x for x in paths if x != primary]

            with ui.element("div").classes("rb-primary-slot"):
                _render_media_el(
                    _media_url(pkg, primary.name, primary),
                    primary,
                    primary=True,
                )

                # Heart overlay — save current take as a named favorite
                n_fav = len(st.favorites)
                with ui.element("div").classes("rb-heart-wrap"):
                    def heart_current() -> None:
                        try:
                            entry = _pkg().save_favorite(step_id)
                            ui.notify(
                                f"Hearted · saved as fallback ({entry.id})",
                                type="positive",
                            )
                            if refresh_all:
                                refresh_all()
                        except Exception as exc:  # noqa: BLE001
                            ui.notify(str(exc), type="negative")

                    tip = (
                        f"Heart this take ({n_fav} saved)"
                        if n_fav
                        else "Heart this take as a fallback"
                    )
                    ui.button(
                        icon="favorite", on_click=heart_current
                    ).props(
                        "round unelevated color=pink text-color=white"
                    ).classes("rb-heart-btn").tooltip(tip)
                    if n_fav:
                        ui.badge(str(n_fav)).props(
                            "floating color=pink rounded"
                        ).classes("rb-heart-badge")

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
  /* DutyCall landing (lib/themes.dart) */
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
  .rb-body {
    flex: 1 1 auto;
    min-height: 0 !important;
    display: flex !important;
    flex-direction: column !important;
    overflow: hidden !important;
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
