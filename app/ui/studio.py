"""Imagine studio: one project, a prompt, and a gallery of takes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import quote

from fastapi.responses import FileResponse, Response
from nicegui import app, ui

from app import imagine_settings
from app.models import project as store
from app.pipeline import doctor, jobs

_STYLE = """
<style>
  body { background: #0b0b0c; color: #f2f2f2; }
  .im-app { min-height: 100vh; display: flex; flex-direction: column; }
  .im-bar {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 8px; border-bottom: 1px solid rgba(255,255,255,0.08);
  }
  .im-title { font-weight: 650; letter-spacing: 0.01em; }
  .im-job { display: flex; align-items: center; gap: 6px; font-size: 0.78rem; color: #c8c8c8; min-width: 0; }
  .im-job.is-off { visibility: hidden; }
  .im-job.is-err { color: #ff8d8d; }
  .im-stage {
    flex: 1; min-height: 38vh; display: flex; align-items: center; justify-content: center;
    background: #000;
  }
  .im-stage-media { max-height: 62vh; max-width: 100%; object-fit: contain; }
  .im-empty { color: #8a8a8a; }
  .im-history { display: flex; gap: 8px; overflow-x: auto; padding: 8px 12px 4px; }
  .im-hit {
    width: 72px; height: 96px; border-radius: 8px; overflow: hidden; position: relative;
    flex: 0 0 auto; border: 1px solid transparent; background: #161616; cursor: pointer;
  }
  .im-hit.is-on { border-color: #8ab4f8; }
  .im-hit img, .im-hit video { width: 100%; height: 100%; object-fit: cover; display: block; }
  .im-hit .x {
    position: absolute; top: 2px; right: 2px; width: 18px; height: 18px;
    border: 0; border-radius: 99px; background: rgba(0,0,0,0.65); color: #fff;
    font-size: 12px; line-height: 18px; cursor: pointer;
  }
  .im-composer { display: flex; flex-direction: column; gap: 8px; padding: 8px 12px 18px; }
  .im-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .im-label { font-size: 0.68rem; letter-spacing: 0.08em; color: #9a9a9a; width: 4.5rem; }
  .im-chip { }
  .im-prompt textarea { min-height: 5.5rem; }
  .im-ref {
    width: 64px; height: 64px; border-radius: 6px; overflow: hidden; position: relative;
    background: #161616; flex: 0 0 auto;
  }
  .im-ref img { width: 100%; height: 100%; object-fit: contain; }
  .im-ref .cap {
    position: absolute; left: 0; right: 0; bottom: 0; font-size: 9px; text-align: center;
    background: rgba(0,0,0,0.6); color: #ddd;
  }
  .im-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 12px; padding: 12px;
  }
  .im-card {
    background: #141416; border-radius: 12px; overflow: hidden; cursor: pointer;
    border: 1px solid rgba(255,255,255,0.06); position: relative;
  }
  .im-card img, .im-card video { width: 100%; height: 220px; object-fit: cover; display: block; background: #000; }
  .im-card .ph { height: 220px; display: flex; align-items: center; justify-content: center; color: #666; }
  .im-card .meta { padding: 8px 10px 10px; }
  .im-card .name { font-weight: 600; }
  .im-card .sub { color: #8d8d8d; font-size: 0.75rem; }
  .im-card .del { position: absolute; top: 6px; right: 6px; }
  .im-banner {
    background: #3a3014; color: #ffe7a3; border-radius: 8px; padding: 8px 10px;
    display: flex; gap: 8px; align-items: center; justify-content: space-between;
  }
  .im-note { color: #8d8d8d; font-size: 0.75rem; }
  @media (max-width: 640px) {
    .im-stage { min-height: 32vh; }
    .im-stage-media { max-height: 48vh; }
  }
</style>
"""


def _theme() -> None:
    ui.dark_mode().enable()
    ui.colors(
        primary="#8ab4f8",
        secondary="#8ab4f8",
        accent="#8ab4f8",
        dark="#0b0b0c",
        positive="#8ab4f8",
        negative="#ff6b6b",
    )
    ui.add_head_html(_STYLE)


def _alive(el: object | None) -> bool:
    if el is None or getattr(el, "is_deleted", False):
        return False
    try:
        _ = el.client  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False
    return True


def _media_url(slug: str, rel: str, path: Path) -> str:
    stamp = int(path.stat().st_mtime)
    return f"/media/{quote(slug)}/{quote(rel, safe='/')}?v={stamp}"


def _open_doctor() -> None:
    with ui.dialog() as dialog, ui.card().classes("w-full").style("max-width: 36rem;"):
        ui.label("Doctor").classes("text-h6")
        for check in doctor.run_doctor():
            color = "#9ddead" if check.ok else "#ff8d8d"
            with ui.row().classes("w-full items-start no-wrap"):
                ui.icon("check_circle" if check.ok else "error", color=color)
                with ui.column().classes("gap-0"):
                    ui.label(check.name).classes("text-weight-medium")
                    ui.label(check.detail).classes("text-caption text-grey-5")
        ui.button("Close", on_click=dialog.close).props("flat")
    dialog.open()


def _register_media() -> None:
    @app.get("/media/{slug}/{rel:path}")
    def media(slug: str, rel: str):
        try:
            proj = store.load(slug)
            path = proj.resolve(rel)
        except (FileNotFoundError, ValueError, OSError):
            return Response(status_code=404)
        if path.name == "meta.json" or not path.is_file():
            return Response(status_code=404)
        return FileResponse(path)


def mount() -> None:
    _register_media()

    @ui.page("/")
    def home() -> None:
        _theme()
        with ui.element("div").classes("im-app"):
            with ui.element("div").classes("im-bar"):
                ui.label("Imagine").classes("im-title")
                ui.label("Image and video").classes("text-caption text-grey-6")
                ui.space()
                ui.button(icon="health_and_safety", on_click=_open_doctor).props(
                    "flat dense round size=sm"
                ).tooltip("Doctor")

            with ui.element("div").classes("im-row").style("padding: 12px;"):
                name = ui.input(placeholder="New project").props("dense outlined").classes(
                    "col"
                )

                def make() -> None:
                    title = (name.value or "").strip()
                    if not title:
                        ui.notify("Name the project", type="warning")
                        return
                    proj = store.create(title)
                    ui.navigate.to(f"/p/{proj.name}")

                name.on("keydown.enter", make)
                ui.button("Create", on_click=make).props("unelevated")

            projects = store.list_projects()
            if not projects:
                ui.label("No projects yet.").classes("im-note").style("padding: 0 12px;")
            with ui.element("div").classes("im-grid"):
                for proj in projects:
                    _project_card(proj)

    @ui.page("/p/{slug}")
    def studio(slug: str) -> None:
        _theme()
        try:
            store.load(slug)
        except FileNotFoundError:
            with ui.column().classes("q-pa-lg"):
                ui.label("That project is gone.")
                ui.button("Home", on_click=lambda: ui.navigate.to("/"))
            return

        shell = ui.element("div").classes("im-app")
        seen = {"n": jobs.generation(slug)}
        chip: dict[str, object] = {}

        def refresh() -> None:
            if not _alive(shell):
                return
            shell.clear()
            with shell:
                _build(slug, refresh, chip)

        def poll() -> None:
            if not _alive(shell):
                return
            current = jobs.generation(slug)
            if current != seen["n"]:
                seen["n"] = current
                refresh()
                return
            _paint_chip(slug, chip)

        refresh()
        ui.timer(0.4, poll)


def _project_card(proj: store.Project) -> None:
    def open_proj() -> None:
        ui.navigate.to(f"/p/{proj.name}")

    def ask_delete() -> None:
        with ui.dialog() as dialog, ui.card():
            ui.label(f"Delete {proj.title}?")
            ui.label("Takes and references go with it.").classes("text-caption")
            with ui.row():
                ui.button("Cancel", on_click=dialog.close).props("flat")

                def confirm() -> None:
                    store.delete(proj.name)
                    dialog.close()
                    ui.navigate.to("/")

                ui.button("Delete", on_click=confirm).props("unelevated color=negative")
        dialog.open()

    with ui.element("div").classes("im-card").on("click", open_proj):
        shown = proj.shown_file()
        if shown is None:
            with ui.element("div").classes("ph"):
                ui.icon("image", size="32px")
        elif proj.shown_kind() == "video":
            url = _media_url(proj.name, proj.shown or "", shown)
            ui.html(
                f'<video src="{url}" muted preload="metadata"></video>',
                sanitize=False,
            )
        else:
            url = _media_url(proj.name, proj.shown or "", shown)
            ui.html(f'<img src="{url}" alt="">', sanitize=False)
        ui.button(icon="delete_outline").props(
            "flat dense round size=sm color=grey"
        ).classes("del").on("click.stop", ask_delete)
        with ui.element("div").classes("meta"):
            ui.label(proj.title).classes("name")
            kind = proj.shown_kind()
            sub = f"{len(proj.takes)} take{'s' if len(proj.takes) != 1 else ''}"
            if kind != "none":
                sub = f"{kind} · {sub}"
            ui.label(sub).classes("sub")


def _paint_chip(slug: str, chip: dict[str, object]) -> None:
    box = chip.get("box")
    lab = chip.get("lab")
    spin = chip.get("spin")
    if not _alive(box) or not _alive(lab):
        return
    try:
        proj = store.load(slug)
    except FileNotFoundError:
        return
    running = proj.status == "running"
    err = (proj.error or "").strip()
    if running:
        box.classes(remove="is-off is-err")  # type: ignore[attr-defined]
        text = jobs.detail(slug) or "Generating…"
        lab.set_text(text[:140])  # type: ignore[attr-defined]
        if _alive(spin):
            spin.set_visibility(True)  # type: ignore[attr-defined]
    elif err:
        box.classes(remove="is-off")  # type: ignore[attr-defined]
        box.classes(add="is-err")  # type: ignore[attr-defined]
        lab.set_text(f"Failed · {err[:140]}")  # type: ignore[attr-defined]
        if _alive(spin):
            spin.set_visibility(False)  # type: ignore[attr-defined]
    else:
        box.classes(add="is-off")  # type: ignore[attr-defined]
        box.classes(remove="is-err")  # type: ignore[attr-defined]
        lab.set_text("")  # type: ignore[attr-defined]
        if _alive(spin):
            spin.set_visibility(False)  # type: ignore[attr-defined]


def _build(slug: str, refresh, chip: dict[str, object]) -> None:
    proj = store.load(slug)
    running = proj.status == "running"
    reference = bool(proj.refs)
    durations = imagine_settings.take_duration_choices(reference=reference)
    resolutions = imagine_settings.take_resolution_choices(reference=reference)
    duration = int(imagine_settings.nearest_choice(proj.duration, durations, 6))
    resolution = str(
        imagine_settings.nearest_choice(proj.resolution, resolutions, "720p")
    )

    with ui.element("div").classes("im-bar"):
        ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/")).props(
            "flat dense round size=sm"
        ).tooltip("Projects")
        title = ui.input(value=proj.title).props("dense borderless").classes("col")

        def save_title() -> None:
            store.update(slug, title=title.value or proj.title)

        title.on("blur", save_title)
        box = ui.element("div").classes("im-job is-off")
        with box:
            spin = ui.spinner("dots", size="1em")
            lab = ui.label("")
        chip["box"] = box
        chip["spin"] = spin
        chip["lab"] = lab
        ui.button(icon="health_and_safety", on_click=_open_doctor).props(
            "flat dense round size=sm"
        ).tooltip("Doctor")
    _paint_chip(slug, chip)

    shown = proj.shown_file()
    with ui.element("div").classes("im-stage"):
        if shown is None:
            ui.label("Nothing generated yet.").classes("im-empty")
        elif proj.shown_kind() == "video":
            url = _media_url(slug, proj.shown or "", shown)
            ui.html(
                f'<video class="im-stage-media" src="{url}" controls playsinline></video>',
                sanitize=False,
            )
        else:
            url = _media_url(slug, proj.shown or "", shown)
            ui.html(
                f'<img class="im-stage-media" src="{url}" alt="">',
                sanitize=False,
            )

    if proj.takes:
        with ui.element("div").classes("im-history"):
            for take in proj.takes:
                try:
                    path = proj.resolve(take.rel)
                except ValueError:
                    continue
                if not path.is_file():
                    continue
                _take_thumb(slug, proj, take, path, refresh, running)

    prompt = ui.textarea(value=proj.prompt, placeholder="Describe the shot").classes(
        "im-prompt w-full"
    ).props("outlined autogrow")

    def save_prompt() -> None:
        store.update(slug, prompt=prompt.value or "")

    prompt.on("blur", save_prompt)

    with ui.element("div").classes("im-composer"):
        err = (proj.error or "").strip()
        if err and not running:
            with ui.element("div").classes("im-banner"):
                ui.label(err).style("white-space: pre-wrap;")

                def dismiss() -> None:
                    store.update(slug, error=None)
                    refresh()

                ui.button("Dismiss", on_click=dismiss).props(
                    "flat dense no-caps color=white"
                )

        with ui.element("div").classes("im-row"):
            ui.label("MODE").classes("im-label")

            def set_mode(mode: str) -> None:
                store.update(slug, mode=mode, prompt=prompt.value or "")
                refresh()

            for mode, label in (("image", "Image"), ("video", "Video")):
                props = "unelevated dense no-caps" if proj.mode == mode else "outline dense no-caps"
                ui.button(label, on_click=lambda m=mode: set_mode(m)).props(props)

        with ui.element("div").classes("im-row"):
            ui.label("FRAME").classes("im-label")

            def set_aspect(aspect: str) -> None:
                store.update(slug, aspect_ratio=aspect, prompt=prompt.value or "")
                refresh()

            for aspect in ("9:16", "16:9", "1:1", "4:3", "3:4", "3:2", "2:3"):
                props = (
                    "unelevated dense no-caps"
                    if proj.aspect_ratio == aspect
                    else "outline dense no-caps"
                )
                ui.button(aspect, on_click=lambda a=aspect: set_aspect(a)).props(props)

        with ui.element("div").classes("im-row"):
            ui.label("REFS").classes("im-label")
            for index, rel in enumerate(proj.refs):
                try:
                    path = proj.resolve(rel)
                except ValueError:
                    continue
                if not path.is_file():
                    continue
                url = _media_url(slug, rel, path)
                with ui.element("div").classes("im-ref"):
                    ui.html(f'<img src="{url}" alt="">', sanitize=False)
                    ui.label(f"IMAGE_{index}").classes("cap")

                    def drop(rel=rel) -> None:
                        store.remove_ref(slug, rel)
                        refresh()

                    ui.button(icon="close", on_click=drop).props(
                        "flat dense round size=xs color=white"
                    ).style(
                        "position:absolute; top:0; right:0;"
                    )

            def on_upload(event) -> None:
                content = event.content
                raw = content.read() if hasattr(content, "read") else content
                suffix = Path(getattr(event, "name", "") or "ref.png").suffix
                try:
                    store.add_ref(slug, raw, suffix or ".png")
                except Exception as exc:  # noqa: BLE001
                    ui.notify(str(exc), type="negative")
                    return
                refresh()

            if len(proj.refs) < 7 and not running:
                ui.upload(
                    label="Add",
                    auto_upload=True,
                    on_upload=on_upload,
                ).props("accept=image/* flat dense")

        if proj.mode == "video":
            with ui.element("div").classes("im-row"):
                ui.label("LENGTH").classes("im-label")
                readout = ui.label(f"{duration}s").classes("text-weight-medium")
                slider = ui.slider(
                    min=durations[0], max=durations[-1], step=1, value=duration
                ).classes("col").props("label-always=false")

                def _slider_value(event) -> int | None:
                    args = getattr(event, "args", None)
                    if isinstance(args, (list, tuple)):
                        args = args[0] if args else None
                    try:
                        return int(args)
                    except (TypeError, ValueError):
                        return None

                def on_move(event) -> None:
                    chosen = _slider_value(event)
                    if chosen is not None:
                        readout.set_text(f"{chosen}s")

                def on_commit(event) -> None:
                    chosen = _slider_value(event)
                    if chosen is None or chosen not in durations:
                        return
                    store.update(slug, duration=chosen)

                slider.on("update:model-value", on_move)
                slider.on("change", on_commit)

            with ui.element("div").classes("im-row"):
                ui.label("QUALITY").classes("im-label")

                def set_res(value: str) -> None:
                    store.update(slug, resolution=value, prompt=prompt.value or "")
                    refresh()

                for value in resolutions:
                    props = (
                        "unelevated dense no-caps"
                        if resolution == value
                        else "outline dense no-caps"
                    )
                    ui.button(value, on_click=lambda v=value: set_res(v)).props(props)
                if reference and "1080p" not in resolutions:
                    ui.label("References cap this at 720p.").classes("im-note")

            if proj.shown_kind() == "image":
                def set_pin(event) -> None:
                    store.update(slug, pin_still=bool(event.value))

                ui.checkbox(
                    "Start from this still",
                    value=proj.pin_still,
                    on_change=set_pin,
                )

        note = _send_note(proj)
        ui.label(note).classes("im-note")

        def begin(action: str) -> None:
            store.update(
                slug,
                prompt=prompt.value or "",
                title=title.value or proj.title,
            )
            if not (prompt.value or "").strip():
                ui.notify("Write a prompt first", type="warning")
                return
            if not store.try_mark_running(slug):
                ui.notify("Already generating", type="warning")
                return
            asyncio.create_task(jobs.run(slug, action))
            refresh()

        with ui.element("div").classes("im-row"):
            if proj.mode == "image":
                ui.button(
                    "Generate image",
                    on_click=lambda: begin("image"),
                ).props("unelevated no-caps").set_enabled(not running)
                if proj.shown_kind() == "image":
                    ui.button(
                        "Edit this image",
                        on_click=lambda: begin("edit_image"),
                    ).props("outline no-caps").set_enabled(not running)
            else:
                ui.button(
                    "Generate video",
                    on_click=lambda: begin("video"),
                ).props("unelevated no-caps").set_enabled(not running)
                if proj.shown_kind() == "video":
                    ui.button(
                        "Edit this clip",
                        on_click=lambda: begin("edit_video"),
                    ).props("outline no-caps").set_enabled(not running)


def _take_thumb(slug, proj, take, path: Path, refresh, running: bool) -> None:
    on = proj.shown == take.rel
    classes = "im-hit is-on" if on else "im-hit"
    url = _media_url(slug, take.rel, path)

    def show() -> None:
        if running:
            return
        store.show_take(slug, take.rel)
        refresh()

    with ui.element("div").classes(classes).on("click", show):
        if path.suffix.lower() in {".mp4", ".webm", ".mov"}:
            ui.html(
                f'<video src="{url}" muted preload="metadata"></video>',
                sanitize=False,
            )
        else:
            ui.html(f'<img src="{url}" alt="">', sanitize=False)
        if not running:

            def drop(rel=take.rel) -> None:
                store.delete_take(slug, rel)
                refresh()

            ui.button("×").props("flat dense round size=xs color=white").on(
                "click.stop", drop
            )


def _send_note(proj: store.Project) -> str:
    refs = len(proj.refs)
    kind = proj.shown_kind()
    if proj.mode == "image":
        if kind == "image":
            return (
                "Generate image uses the references as look, not as the pose. "
                "Edit this image changes the still on stage."
            )
        if refs:
            return "Generate image uses the references as look, not as the pose."
        return "Generate image is text to image."
    if kind == "video":
        extra = " Edit this clip sends the prompt as the change, not a new scene."
    else:
        extra = ""
    if proj.pin_still and kind == "image" and refs:
        return "Generate video starts on this still and uses the references." + extra
    if proj.pin_still and kind == "image":
        return "Generate video animates this still." + extra
    if refs:
        return "Generate video uses the references and invents the pose and camera." + extra
    return "Generate video is text to video." + extra
