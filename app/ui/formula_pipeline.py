"""Left-to-right formula: Frame 1 + prompt pieces + Frame end + compiled."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from nicegui import ui

from app.models.rally import RallyPackage
from app.pipeline.formula import (
    LOOK_ASPECTS,
    build_formula,
    is_muted,
    look_aspects,
    persist_formula_text,
    toggle_look_aspect,
    toggle_mute,
)
from app.pipeline.start_still import (
    cycle_end_slot_type,
    cycle_frame1_type,
    end_slot_type,
    frame1_path,
    frame1_type,
    list_clip_stills,
    look_ref_paths,
    set_end_still,
    set_frame1_still,
)

STACK_STILL = ("concept", "shot")
STACK_TAKE = ("concept", "sound", "voice", "shot")

START_LAB = {"unique": "Unique", "start": "Frame 1", "look": "Ref"}
END_LAB = {"none": "Nothing", "pin": "Frame end", "loop": "Loop"}


def _mute_btn(muted: bool, on_click) -> None:
    el = ui.element("div").classes(
        "rb-pipe-mute" + (" is-off" if muted else "")
    )
    with el:
        ui.label("+" if muted else "−")
    el.on("click.stop", on_click)


def render_formula_row(
    pkg: RallyPackage,
    step_id: str,
    surface: str,
    *,
    persist_formula: Callable[[], None],
    refresh_all: Callable[[], None],
    pkg_fn: Callable[[], RallyPackage],
    media_url: Callable[[RallyPackage, str, Path], str],
    speaker: str,
    voices: list,
    kit: dict,
    imagine_settings,
    render_post: Callable[[], None] | None = None,
) -> dict:
    """Render Frame 1, prompt stack, Frame end (take), compiled pane.

    Still/Take lives at the top of the Frame 1 column (not Preview).
    ``render_post`` draws post-process extras under compiled (takes, E, Idle).
    Returns ``comp_els`` for highlight (sid → element).
    """
    from app.pipeline.joins import end_path

    comp_els: dict = {}
    stack_ids = STACK_STILL if surface == "still" else STACK_TAKE
    sections = {
        s.id: s
        for s in build_formula(pkg, step_id, surface)
        if s.id in stack_ids or s.extra
    }

    def _refresh() -> None:
        persist_formula()
        refresh_all()

    def _toggle(sid: str) -> None:
        persist_formula()
        toggle_mute(pkg_fn(), step_id, surface, sid)
        refresh_all()

    def _open_picker(which: str) -> None:
        persist_formula()
        catalog = list_clip_stills(pkg_fn(), step_id)
        with ui.dialog() as dlg, ui.card().classes("rb-still-picker"):
            ui.label(
                "Frame 1 or Ref" if which == "start" else "Frame end"
            ).classes("text-subtitle2")
            with ui.element("div").classes("rb-still-picker-grid"):
                none = ui.element("div").classes("rb-still-pick is-none")
                with none:
                    ui.label("Nothing")

                def _pick_none(_e=None, w=which) -> None:
                    p = pkg_fn()
                    if w == "start":
                        set_frame1_still(p, step_id, surface, None)
                    else:
                        set_end_still(p, step_id, None)
                    dlg.close()
                    refresh_all()

                none.on("click", _pick_none)
                for label, path in catalog:
                    show = path
                    if path.name == "Preview.clean.png":
                        disp = pkg.path_for("Preview.png")
                        if disp.is_file():
                            show = disp
                    card = ui.element("div").classes("rb-still-pick")
                    with card:
                        ui.html(
                            f'<img src="{media_url(pkg, show.name, show)}" alt="{label}" />',
                            sanitize=False,
                        )
                        ui.label(label).classes("cap")

                    def _pick(_e=None, path_=path, w=which) -> None:
                        p = pkg_fn()
                        if w == "start":
                            set_frame1_still(p, step_id, surface, path_)
                        else:
                            set_end_still(p, step_id, path_)
                        dlg.close()
                        refresh_all()

                    card.on("click", _pick)
            ui.button("Close", on_click=dlg.close).props("flat no-caps")
        dlg.open()

    def _frame_slot(which: str) -> None:
        muted = is_muted(pkg, step_id, surface, which)
        if which == "start":
            kind = frame1_type(pkg, step_id, surface)
            path = frame1_path(pkg, step_id, surface)
            type_lab = START_LAB.get(kind, "Type")
            empty = (
                "Unique shot"
                if kind == "unique"
                else "Frame 1 or Ref"
            )
        else:
            kind = end_slot_type(pkg, step_id)
            path = end_path(pkg, step_id) if kind != "none" else None
            if kind == "loop":
                path = None
            type_lab = END_LAB.get(kind, "Type")
            empty = (
                "Loop to frame 1"
                if kind == "loop"
                else ("Nothing" if kind == "none" else "Frame end")
            )
        show = path
        if show is not None and show.name == "Preview.clean.png":
            disp = pkg.path_for("Preview.png")
            if disp.is_file():
                show = disp
        has = show is not None and show.is_file() and kind not in {"unique", "none", "loop"}
        slot = ui.element("div").classes(
            "rb-pipe-slot"
            + (" is-mute" if muted else "")
            + (" is-look" if which == "start" and kind == "look" else "")
            + (" is-on" if has or kind == "loop" else "")
        )
        with slot:
            _mute_btn(muted, lambda w=which: _toggle(w))
            glass = ui.element("div").classes("rb-pipe-glass")
            with glass:
                if has and show is not None:
                    ui.html(
                        f'<img src="{media_url(pkg, show.name, show)}" alt="" />',
                        sanitize=False,
                    )
                else:
                    ui.label(empty).classes("rb-pipe-empty")
            glass.on("click", lambda w=which: _open_picker(w))

            def _cycle(_e=None, w=which) -> None:
                persist_formula()
                p = pkg_fn()
                if w == "start":
                    cycle_frame1_type(p, step_id, surface)
                else:
                    cycle_end_slot_type(p, step_id)
                refresh_all()

            ui.button(type_lab, on_click=_cycle).props(
                "outline dense no-caps"
            ).classes("rb-pipe-type")

    # Frame 1 sits under Still/Take so the toggle keeps the first column.
    with ui.element("div").classes("rb-pipe-col rb-pipe-frame"):
        if step_id != "Preview":
            with ui.element("div").classes("rb-surface"):
                for key, lab in (("still", "Still"), ("take", "Take")):
                    on = surface == key
                    pill = ui.element("div").classes(
                        "rb-surface-btn" + (" is-on" if on else "")
                    )
                    with pill:
                        ui.label(lab)

                    def _set_surf(_e=None, key_=key) -> None:
                        persist_formula()
                        s = pkg_fn().step(step_id)
                        s.options["work_surface"] = key_
                        pkg_fn().save()
                        refresh_all()

                    pill.on("click", _set_surf)
        _frame_slot("start")

    # Prompt stack. Controls sit on the label row. Muted pieces collapse.
    with ui.element("div").classes("rb-pipe-col rb-pipe-stack"):
        reference = (
            surface == "take"
            and step_id != "Preview"
            and frame1_type(pkg, step_id, surface) == "look"
        )

        start_kind = frame1_type(pkg, step_id, surface)
        if step_id != "Preview" and start_kind == "look":
            refs_muted = is_muted(pkg, step_id, surface, "refs")
            ref_piece = ui.element("div").classes(
                "rb-pipe-piece rb-pipe-ref"
                + (" is-mute" if refs_muted else "")
            )
            with ref_piece:
                with ui.element("div").classes("rb-pipe-head"):
                    ui.label("Ref").classes("rb-pipe-k")
                    ctl = ui.element("div").classes("rb-pipe-head-ctl")
                    _mute_btn(refs_muted, lambda: _toggle("refs"))
                if not refs_muted:
                    chosen = look_aspects(pkg, step_id, surface)
                    with ctl:
                        for aspect in LOOK_ASPECTS:
                            on = aspect in chosen
                            chip = ui.element("div").classes(
                                "rb-chip" + (" is-on" if on else "")
                            )
                            with chip:
                                ui.label(aspect)

                            def _toggle_asp(_e=None, a=aspect) -> None:
                                persist_formula()
                                toggle_look_aspect(
                                    pkg_fn(), step_id, surface, a
                                )
                                refresh_all()

                            chip.on("click.stop", _toggle_asp)
                    paths = look_ref_paths(pkg, step_id)
                    if paths:
                        with ui.element("div").classes("rb-pipe-ref-row"):
                            for i, path in enumerate(paths):
                                with ui.element("div").classes(
                                    "rb-pipe-ref-still"
                                ):
                                    ui.html(
                                        f'<img src="{media_url(pkg, path.name, path)}" '
                                        f'alt="" />',
                                        sanitize=False,
                                    )
                                    ui.label(f"IMAGE_{i}").classes("cap")

            def _flash_refs(_e=None) -> None:
                for k, el in comp_els.items():
                    if k == "refs":
                        el.classes(add="is-flash")
                    else:
                        el.classes(remove="is-flash")

            ref_piece.on("click", _flash_refs)

        def _duration_row() -> None:
            """Seconds and quality this shot can actually send, on one row."""
            if surface != "take" or step_id == "Preview":
                return
            dur_choices = imagine_settings.take_duration_choices(
                reference=reference
            )
            res_choices = imagine_settings.take_resolution_choices(
                reference=reference
            )
            stored_dur = pkg.step(step_id).options.get("duration")
            cur_dur = int(
                imagine_settings.nearest_choice(
                    stored_dur
                    if stored_dur not in (None, "")
                    else imagine_settings.video_duration(),
                    dur_choices,
                    dur_choices[min(5, len(dur_choices) - 1)],
                )
            )
            stored_res = str(
                pkg.step(step_id).options.get("video_resolution") or ""
            ).strip()
            gear_res = imagine_settings.video_resolution()
            cur_res = (
                stored_res
                if stored_res in res_choices
                else gear_res
                if gear_res in res_choices
                else res_choices[-1]
            )
            with ui.element("div").classes("rb-pipe-piece rb-pipe-dur"):
                with ui.element("div").classes("rb-pipe-head"):
                    ui.label("Duration").classes("rb-pipe-k")
                    slider = (
                        ui.slider(
                            min=dur_choices[0],
                            max=dur_choices[-1],
                            step=1,
                            value=cur_dur,
                        )
                        .props("dense snap color=teal-4")
                        .classes("rb-dur-slider")
                    )
                    dur_val = ui.label(f"{cur_dur}s").classes("rb-pipe-dur-val")
                    with ui.element("div").classes("rb-pipe-q"):
                        for q in res_choices:
                            chip = ui.element("div").classes(
                                "rb-chip"
                                + (" is-on" if q == cur_res else "")
                            )
                            with chip:
                                ui.label(q)

                            def _pick_res(_e=None, q_=q) -> None:
                                persist_formula()
                                s = pkg_fn().step(step_id)
                                if str(s.options.get("video_resolution") or "") == q_:
                                    return
                                s.options["video_resolution"] = q_
                                pkg_fn().save()
                                refresh_all()

                            chip.on("click.stop", _pick_res)

            def _commit_dur(e=None) -> None:
                raw = getattr(e, "args", None)
                if raw in (None, ""):
                    raw = slider.value
                try:
                    val = int(round(float(raw)))
                except (TypeError, ValueError):
                    return
                if val not in dur_choices or val == cur_dur:
                    return
                persist_formula()
                s = pkg_fn().step(step_id)
                s.options["duration"] = val
                pkg_fn().save()
                refresh_all()

            # Paint the second locally while dragging. Save on release only.
            slider.on(
                "update:model-value",
                js_handler=(
                    "(value) => {"
                    f" const lab = document.getElementById({dur_val.html_id!r});"
                    " const n = Math.round(Number(value));"
                    " if (lab && Number.isFinite(n)) lab.textContent = n + 's';"
                    "}"
                ),
            )
            slider.on("change", _commit_dur)

        for sid in stack_ids:
            if sid == "shot":
                _duration_row()
            sec = sections.get(sid)
            if sec is None:
                continue
            muted = is_muted(pkg, step_id, surface, sid)
            title = "Prompt" if sid == "shot" else sec.label
            piece = ui.element("div").classes(
                "rb-pipe-piece"
                + (" is-prompt" if sid == "shot" else "")
                + (" is-mute" if muted else "")
            )
            with piece:
                with ui.element("div").classes("rb-pipe-head"):
                    ui.label(title).classes("rb-pipe-k")
                    ctl = ui.element("div").classes("rb-pipe-head-ctl")
                    _mute_btn(muted, lambda s=sid: _toggle(s))
                if not muted and sid == "shot":
                    with ui.element("div").classes("rb-pipe-body"):
                        ta = (
                            ui.textarea(
                                value=sec.text,
                                placeholder=sec.placeholder
                                or "What happens in this shot.",
                            )
                            .classes("w-full rb-pipe-ta")
                            .props(
                                "outlined dense "
                                "autocapitalize=none autocomplete=off "
                                "autocorrect=off spellcheck=false"
                            )
                        )

                        def _on_shot(e=None, field=ta) -> None:
                            persist_formula_text(
                                pkg_fn(),
                                step_id,
                                surface,
                                {"shot": field.value or ""},
                            )

                        ta.on_value_change(_on_shot)
                        ta.on("blur", lambda e=None: persist_formula())
                elif not muted and sid == "sound":
                    with ctl:
                        for s_tag in kit.get("sounds") or []:
                            chip = ui.element("div").classes("rb-chip")
                            with chip:
                                ui.label(str(s_tag))
                                ui.label("×").classes("rb-sound-x")

                            def _rm(_e=None, tag=str(s_tag)) -> None:
                                persist_formula()
                                p = pkg_fn()
                                p.kit["sounds"] = [
                                    s
                                    for s in (p.kit.get("sounds") or [])
                                    if str(s) != tag
                                ]
                                p.save()
                                refresh_all()

                            chip.on("click.stop", _rm)
                    with ui.element("div").classes("rb-pipe-body"):
                        with ui.element("div").classes("rb-add-row"):
                            sound_in = (
                                ui.input(placeholder="add tag")
                                .props("dense outlined")
                                .classes("w-full")
                            )

                            def _add_sound() -> None:
                                tag = (sound_in.value or "").strip()
                                if not tag:
                                    return
                                persist_formula()
                                p = pkg_fn()
                                sounds = list(p.kit.get("sounds") or [])
                                if tag not in sounds:
                                    sounds.append(tag)
                                p.kit["sounds"] = sounds
                                p.save()
                                refresh_all()

                            ui.button("Add", on_click=_add_sound).props(
                                "flat dense no-caps"
                            ).classes("rb-add-btn")
                elif not muted and sid == "voice":
                    with ctl:
                        inv = ui.element("div").classes(
                            "rb-chip" + (" is-on" if not speaker else "")
                        )
                        with inv:
                            ui.label("Invent")

                        def _invent() -> None:
                            persist_formula()
                            s = pkg_fn().step(step_id)
                            if not str(s.options.get("speaker") or "").strip():
                                return
                            s.options["speaker"] = ""
                            pkg_fn().save()
                            refresh_all()

                        inv.on("click.stop", _invent)
                        for v in voices:
                            vid = str(v.get("voice_id") or "")
                            role = str(v.get("role") or vid)
                            on = vid.lower() == speaker
                            chip = ui.element("div").classes(
                                "rb-chip" + (" is-on" if on else "")
                            )
                            with chip:
                                ui.label(f"{role} · {vid}")

                            def _cast(_e=None, vid_=vid) -> None:
                                persist_formula()
                                s = pkg_fn().step(step_id)
                                if str(s.options.get("speaker") or "") == vid_:
                                    return
                                s.options["speaker"] = vid_
                                pkg_fn().save()
                                refresh_all()

                            chip.on("click.stop", _cast)
                elif not muted:
                    with ui.element("div").classes("rb-pipe-body"):
                        ui.label(sec.text or "—").classes(
                            "rb-pipe-v rb-pipe-clamp"
                        ).tooltip(sec.text or "")

            def _flash(_e=None, sid_=sid) -> None:
                for k, el in comp_els.items():
                    if k == sid_:
                        el.classes(add="is-flash")
                    else:
                        el.classes(remove="is-flash")

            piece.on("click", _flash)

    if surface == "take" and step_id != "Preview":
        with ui.element("div").classes("rb-pipe-col rb-pipe-frame"):
            _frame_slot("end")

    # Compiled prompt (newlines, no pluses) + post-process pane
    with ui.element("div").classes("rb-pipe-col rb-pipe-compile-col"):
        with ui.element("div").classes("rb-pipe-compiled"):
            ui.label("Compiled prompt").classes("rb-pipe-k")
            compiled = ui.element("div").classes("rb-pipe-raw")
            with compiled:
                shown = [
                    s
                    for s in build_formula(pkg, step_id, surface)
                    if (s.text or "").strip() and not s.muted
                ]
                if not shown:
                    ui.label("Nothing to send.").classes(
                        "text-caption text-grey-5"
                    )
                for sec in shown:
                    line = ui.element("div").classes("rb-comp-line")
                    with line:
                        ui.label(sec.text).classes("rb-comp-text")
                    flash_id = (
                        "start"
                        if sec.id == "seed"
                        else ("end" if sec.id == "end" else sec.id)
                    )
                    comp_els[flash_id] = line
                    comp_els[sec.id] = line

                    def _from_raw(_e=None, sid_=flash_id) -> None:
                        for k, el in comp_els.items():
                            if k == sid_:
                                el.classes(add="is-flash")
                            else:
                                el.classes(remove="is-flash")

                    line.on("click", _from_raw)

        if render_post is not None:
            with ui.element("div").classes("rb-pipe-post"):
                render_post()

    return comp_els
