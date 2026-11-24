"""Timeline studio: one library, one film, one take composer."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import Request, UploadFile
from fastapi.responses import JSONResponse
from nicegui import app, ui

from app.config import OUTPUT_DIR
from app.models.rally import RallyPackage
from app.pipeline import board, board_gen, library

_STATIC = Path(__file__).resolve().parent / "glass"
_ROUTES = False
_TASKS: dict[str, asyncio.Task] = {}
_NOTES: dict[str, str] = {}
ASSET_V = "20"


def open_project(name: str) -> RallyPackage:
    cleaned = (name or "").strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise ValueError("Unknown project")
    root = (OUTPUT_DIR / cleaned).resolve()
    if OUTPUT_DIR.resolve() not in root.parents or not (root / "meta.json").is_file():
        raise ValueError("Unknown project")
    return RallyPackage.load(root)


def _url(name: str, rel: str) -> str:
    from urllib.parse import quote

    parts = [quote(name), *[quote(part) for part in Path(rel).parts]]
    return "/media/" + "/".join(parts)


def _busy(name: str) -> bool:
    task = _TASKS.get(name)
    return task is not None and not task.done()


def _payload(name: str) -> dict:
    pkg = open_project(name)
    assets = library.catalog(pkg)
    data = board.view_model(pkg, assets, url_for=lambda rel: _url(name, rel))
    data["busy"] = _busy(name)
    data["note"] = _NOTES.get(name) or ""
    return data


def _kick(name: str, factory) -> None:
    if _busy(name):
        raise ValueError("Already generating")

    async def run() -> None:
        try:
            await factory()
        except Exception as exc:  # noqa: BLE001
            _NOTES[name] = ""
            try:
                pkg = open_project(name)
                assets = library.catalog(pkg)
                state = board.ensure_board(pkg, assets)
                state["error"] = str(exc)
                pkg.board = state
                pkg.save()
            except Exception:
                _NOTES[name] = str(exc)
        finally:
            _TASKS.pop(name, None)

    _TASKS[name] = asyncio.get_running_loop().create_task(run())


def register() -> None:
    global _ROUTES
    if _ROUTES:
        return
    _ROUTES = True
    app.add_static_files("/glass-static", str(_STATIC))

    @app.get("/api/glass")
    async def glass_state(rally: str = "") -> JSONResponse:
        try:
            return JSONResponse(_payload(rally))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    @app.post("/api/glass")
    async def glass_op(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Bad request"}, status_code=400)
        name = str((body or {}).get("rally") or "").strip()
        op = str((body or {}).get("op") or "").strip()
        try:
            pkg = open_project(name)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        try:
            if op == "generate_still":
                prompt = str(body.get("prompt") or "")
                ref_ids = [str(ref) for ref in (body.get("refs") or [])]
                _kick(
                    name,
                    lambda: board_gen.generate_still(
                        name,
                        prompt=prompt,
                        ref_ids=ref_ids,
                        progress=lambda msg: _progress(name, msg),
                    ),
                )
            elif op == "generate_take":
                _kick(
                    name,
                    lambda: board_gen.generate_take(
                        name,
                        progress=lambda msg: _progress(name, msg),
                    ),
                )
            elif op == "edit_speak":
                from app.pipeline import speech

                await speech.place_on_lane(
                    pkg,
                    text=str(body.get("text") or ""),
                    voice=str(body.get("voice") or ""),
                    at=body.get("at"),
                )
            elif op == "generate_redo":
                source = str(body.get("asset_id") or "")
                prompt = str(body.get("prompt") or "")
                hold = str(body.get("hold") or "both")
                try:
                    length = int(body.get("duration") or 6)
                except (TypeError, ValueError):
                    length = 6
                _kick(
                    name,
                    lambda: board_gen.generate_redo(
                        name,
                        asset_id=source,
                        prompt=prompt,
                        hold=hold,
                        duration=length,
                        progress=lambda msg: _progress(name, msg),
                    ),
                )
            elif op:
                assets = {row["id"]: row for row in library.catalog(pkg)}
                board.apply_op(pkg, op, body, assets)
            else:
                raise ValueError("Missing edit")
        except ValueError as exc:
            try:
                pkg = open_project(name)
                assets = library.catalog(pkg)
                state = board.ensure_board(pkg, assets)
                state["error"] = str(exc)
                pkg.board = state
                pkg.save()
            except Exception:
                return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(_payload(name))

    @app.post("/api/glass/upload")
    async def glass_upload(request: Request) -> JSONResponse:
        form = await request.form()
        rally = str(form.get("rally") or "")
        try:
            pkg = open_project(rally)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        try:
            for item in form.getlist("file"):
                if not isinstance(item, UploadFile):
                    continue
                raw = await item.read()
                if len(raw) > 40_000_000:
                    raise ValueError(f"{item.filename} is over 40MB")
                suffix = Path(item.filename or "").suffix.lower()
                tmp = library.library_dir(pkg) / f".upload{suffix}"
                tmp.write_bytes(raw)
                try:
                    library.ingest(
                        pkg,
                        tmp,
                        name=library.pretty_name(Path(item.filename or "upload").stem),
                        move=True,
                    )
                finally:
                    if tmp.is_file():
                        tmp.unlink(missing_ok=True)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(_payload(rally))


def _progress(name: str, message: str) -> None:
    _NOTES[name] = message or ""


def render_studio(get_pkg, close) -> None:
    """Replace the editor with the timeline. Close returns to the project list."""
    pkg = get_pkg()
    ui.add_head_html(f'<script src="/glass-static/board.js?v={ASSET_V}"></script>')
    with ui.element("div").classes("gb-fill"):
        with ui.element("div").classes("gb-top"):
            ui.button(icon="arrow_back", on_click=close).props(
                "flat round dense"
            ).classes("gb-back").tooltip("Projects")
            ui.element("div").props('id="gb-top-slot"').classes("gb-top-slot")
        ui.element("div").props('id="gb-stage"').classes("gb-stage")
    ui.run_javascript(
        "window.GB_RALLY = "
        + json.dumps(pkg.name)
        + "; if (window.gbBoot) window.gbBoot();"
    )
