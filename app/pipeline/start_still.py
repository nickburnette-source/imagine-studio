"""Stage a clip's frame-1 still when the opening pose is not Preview/Idle."""

from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models.rally import RallyPackage
from app.pipeline.frames import resolve_start_still
from app.pipeline.prompts import (
    still_seed_filename,
    staged_still_name,
)

ProgressCb = Any


def seed_path_for(pkg: RallyPackage, step_id: str, seed_key: str) -> Path:
    """On-disk still that Generate still image-edits from."""
    from app.pipeline.explicit_mark import as_imagine_still, preview_source_path

    name = still_seed_filename(seed_key, step_id)
    path = pkg.path_for(name)
    if path.is_file():
        remapped = as_imagine_still(pkg, path)
        return remapped if remapped is not None else path
    preview = preview_source_path(pkg)
    if preview.is_file():
        return preview
    idle = pkg.path_for("IdleKeyframe.png")
    if idle.is_file():
        return idle
    return path


def look_ref_paths(pkg: RallyPackage, step_id: str) -> list[Path]:
    """Character/look stills (Preview) for still edits and video refs."""
    from app.pipeline.explicit_mark import preview_source_path

    raw = pkg.step(step_id).options.get("look_refs")
    if not isinstance(raw, list):
        raw = []
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
        if len(out) >= 5:
            break
    return out


def preview_ref_on(pkg: RallyPackage, step_id: str) -> bool:
    raw = pkg.step(step_id).options.get("look_refs")
    if not isinstance(raw, list):
        return False
    return any(
        str(x).lower() in {"preview", "preview.png", "preview.clean.png"}
        for x in raw
    )


def toggle_preview_ref(pkg: RallyPackage, step_id: str) -> bool:
    st = pkg.step(step_id)
    on = not preview_ref_on(pkg, step_id)
    cur = [
        x
        for x in (st.options.get("look_refs") or [])
        if str(x).lower()
        not in {"preview", "preview.png", "preview.clean.png"}
    ]
    if on:
        cur.append("Preview.png")
    st.options["look_refs"] = cur
    pkg.save()
    return on


def still_seed_kind(pkg: RallyPackage, step_id: str) -> str:
    st = pkg.step(step_id)
    if str(st.options.get("still_seed") or "").lower() in {
        "none",
        "off",
        "prompt",
    }:
        return "none"
    path = still_seed_path(pkg, step_id)
    if not path.is_file():
        return "none"
    if path.name in {"Preview.png", "Preview.clean.png"}:
        return "preview"
    return "self"


def _same_still(a: Path | None, b: Path | None) -> bool:
    if a is None or b is None:
        return False
    try:
        if a.is_file() and b.is_file() and a.resolve() == b.resolve():
            return True
    except OSError:
        pass
    names = {a.name, b.name}
    return names <= {"Preview.png", "Preview.clean.png"}


def _rel_still(pkg: RallyPackage, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(pkg.dir().resolve()))
    except ValueError:
        return path.name


def _look_ref_key(pkg: RallyPackage, path: Path) -> str:
    if path.name in {"Preview.png", "Preview.clean.png"}:
        return "Preview.png"
    return _rel_still(pkg, path)


def _look_ref_alias_set(key: str) -> set[str]:
    k = str(key or "").strip().lower()
    if k in {"preview", "preview.png", "preview.clean.png"}:
        return {"preview", "preview.png", "preview.clean.png"}
    name = Path(str(key)).name.lower()
    return {str(key).strip(), str(key).strip().lower(), name}


def _drop_look_ref(cur: list, key: str) -> list:
    aliases = _look_ref_alias_set(key)
    out: list = []
    for item in cur:
        raw = str(item or "").strip()
        if not raw:
            continue
        if raw.lower() in aliases or Path(raw).name.lower() in aliases:
            continue
        out.append(item)
    return out


def input_slot_path(pkg: RallyPackage, step_id: str) -> Path | None:
    st = pkg.step(step_id)
    rel = str(st.options.get("input_slot") or "").strip()
    if rel:
        p = pkg.path_for(rel)
        if p.is_file():
            return p
    seed = still_seed_path(pkg, step_id)
    if seed.is_file():
        return seed
    from app.pipeline.joins import start_path

    sp = start_path(pkg, step_id)
    return sp if sp is not None and sp.is_file() else None


def input_role(
    pkg: RallyPackage, step_id: str, path: Path, *, surface: str
) -> str:
    """'start' | 'ref' | 'off' for a catalog still."""
    if preview_ref_on(pkg, step_id) and path.name in {
        "Preview.png",
        "Preview.clean.png",
    }:
        # Preview can be ref even when also start
        start = False
        if surface == "still":
            start = still_seed_kind(pkg, step_id) == "preview"
        else:
            from app.pipeline.joins import start_join, start_path

            sp = start_path(pkg, step_id)
            start = _same_still(sp, path)
        if start:
            return "start"
        return "ref"
    if surface == "still":
        seed = still_seed_path(pkg, step_id)
        if seed.is_file() and _same_still(seed, path):
            if still_seed_kind(pkg, step_id) != "none":
                return "start"
        refs = look_ref_paths(pkg, step_id)
        if any(_same_still(r, path) for r in refs):
            return "ref"
        return "off"
    from app.pipeline.joins import start_path

    sp = start_path(pkg, step_id)
    if _same_still(sp, path):
        return "start"
    refs = look_ref_paths(pkg, step_id)
    if any(_same_still(r, path) for r in refs):
        return "ref"
    return "off"


def apply_input_as_start(
    pkg: RallyPackage, step_id: str, path: Path, *, surface: str
) -> None:
    from app.pipeline.joins import JOIN_START, start_join

    st = pkg.step(step_id)
    rel = _rel_still(pkg, path)
    st.options["input_slot"] = rel
    name = path.name
    if surface == "still":
        st.options.pop("still_seed", None)
        st.options["still_seed_file"] = (
            "Preview.png"
            if name in {"Preview.png", "Preview.clean.png"}
            else rel
        )
    else:
        opts = JOIN_START.get(step_id) or []
        if name in {"Preview.png", "Preview.clean.png"} and "preview" in opts:
            st.options["start_join"] = "preview"
            st.options.pop("unique_shot", None)
            st.options.pop("start_file", None)
        elif name == "IdleKeyframe.png" and "idle-rest" in opts:
            st.options["start_join"] = "idle-rest"
            st.options.pop("unique_shot", None)
        elif name == "Idle.last.png" and "idle-last" in opts:
            st.options["start_join"] = "idle-last"
            st.options.pop("unique_shot", None)
        elif name == "Message.last.png" and "message-last" in opts:
            st.options["start_join"] = "message-last"
            st.options.pop("unique_shot", None)
        elif "still" in opts:
            st.options["start_join"] = "still"
            st.options.pop("unique_shot", None)
            st.options["start_file"] = rel
    # If this still is now frame 1, drop it from look-refs to keep the formula lean
    if name in {"Preview.png", "Preview.clean.png"}:
        cur = [
            x
            for x in (st.options.get("look_refs") or [])
            if str(x).lower()
            not in {"preview", "preview.png", "preview.clean.png"}
        ]
        st.options["look_refs"] = cur
    pkg.save()


def apply_input_as_ref(
    pkg: RallyPackage, step_id: str, path: Path, *, surface: str
) -> None:
    st = pkg.step(step_id)
    rel = _rel_still(pkg, path)
    st.options["input_slot"] = rel
    if surface == "still":
        if still_seed_kind(pkg, step_id) != "none" and _same_still(
            still_seed_path(pkg, step_id), path
        ):
            st.options["still_seed"] = "none"
            st.options["still_seed_file"] = ""
    else:
        from app.pipeline.joins import start_path

        sp = start_path(pkg, step_id)
        if _same_still(sp, path):
            st.options["start_join"] = "cut"
            st.options["unique_shot"] = True
            st.options.pop("start_file", None)
    key = _look_ref_key(pkg, path)
    cur = _drop_look_ref(list(st.options.get("look_refs") or []), key)
    cur.insert(0, key)
    st.options["look_refs"] = cur[:5]
    pkg.save()


def cycle_input_role(
    pkg: RallyPackage, step_id: str, path: Path, *, surface: str
) -> str:
    role = input_role(pkg, step_id, path, surface=surface)
    if role == "start":
        apply_input_as_ref(pkg, step_id, path, surface=surface)
        return "ref"
    apply_input_as_start(pkg, step_id, path, surface=surface)
    return "start"


def list_clip_stills(
    pkg: RallyPackage, step_id: str
) -> list[tuple[str, Path]]:
    """Catalog of stills that can drop into Frame 1 / Frame end."""
    from app.pipeline.prompts import staged_still_name

    out: list[tuple[str, Path]] = []
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
        out.append((label, path))

    add("Preview", pkg.path_for("Preview.png"))
    add("Idle", pkg.path_for("IdleKeyframe.png"))
    add("Idle last", pkg.path_for("Idle.last.png"))
    add("Msg last", pkg.path_for("Message.last.png"))
    this = pkg.path_for(
        "IdleKeyframe.png" if step_id == "Idle" else staged_still_name(step_id)
    )
    add("This clip", this)
    for sid in ("Rally", "Message", "Accept", "Decline", "Adjust"):
        if sid == step_id:
            continue
        other = pkg.path_for(
            "Reschedule.start.png" if sid == "Adjust" else f"{sid}.start.png"
        )
        add(sid, other)
    return out


def _frame1_surf(surface: str, step_id: str) -> str:
    if step_id == "Preview":
        return "still"
    return "still" if (surface or "").strip().lower() == "still" else "take"


def _frame1_kind_map(st) -> dict[str, str]:
    raw = st.options.get("frame1_kind")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, val in raw.items():
        k = str(key).strip().lower()
        v = str(val).strip().lower()
        if k in {"still", "take"} and v in {"unique", "start", "look"}:
            out[k] = v
    return out


def set_frame1_kind(
    pkg: RallyPackage, step_id: str, surface: str, kind: str
) -> None:
    if kind not in {"unique", "start", "look"}:
        return
    st = pkg.step(step_id)
    store = _frame1_kind_map(st)
    store[_frame1_surf(surface, step_id)] = kind
    st.options["frame1_kind"] = store


def _unpin_frame1(pkg: RallyPackage, step_id: str, surface: str) -> None:
    """Drop the pose pin on this surface only. Keep look_refs for Ref."""
    st = pkg.step(step_id)
    if _frame1_surf(surface, step_id) == "still":
        st.options["still_seed"] = "none"
        st.options["still_seed_file"] = ""
        return
    st.options["start_join"] = "cut"
    st.options["unique_shot"] = True
    st.options.pop("start_file", None)


def frame1_type(pkg: RallyPackage, step_id: str, surface: str) -> str:
    """unique | start | look — stored per still/take so leftover refs
    don't pin the Type button on Ref."""
    stored = _frame1_kind_map(pkg.step(step_id)).get(
        _frame1_surf(surface, step_id)
    )
    if stored is not None:
        return stored
    if surface == "still" or step_id == "Preview":
        if still_seed_kind(pkg, step_id) == "none":
            return "look" if look_ref_paths(pkg, step_id) else "unique"
        return "start"
    from app.pipeline.joins import take_is_unique

    if take_is_unique(pkg, step_id):
        return "look" if look_ref_paths(pkg, step_id) else "unique"
    return "start"


def active_look_refs(
    pkg: RallyPackage, step_id: str, surface: str
) -> list[Path]:
    """Look-lock stills only when Frame 1 Type is Ref."""
    if frame1_type(pkg, step_id, surface) != "look":
        return []
    return look_ref_paths(pkg, step_id)


def frame1_path(
    pkg: RallyPackage, step_id: str, surface: str
) -> Path | None:
    kind = frame1_type(pkg, step_id, surface)
    if kind == "start":
        if surface == "still" or step_id == "Preview":
            p = still_seed_path(pkg, step_id)
            return p if p.is_file() else None
        from app.pipeline.joins import start_path

        return start_path(pkg, step_id)
    if kind == "look":
        refs = look_ref_paths(pkg, step_id)
        rel = str(pkg.step(step_id).options.get("input_slot") or "").strip()
        if rel:
            picked = pkg.path_for(rel)
            if picked.is_file():
                for r in refs:
                    if _same_still(picked, r):
                        return picked
        return refs[0] if refs else None
    return None


def set_frame1_still(
    pkg: RallyPackage,
    step_id: str,
    surface: str,
    path: Path | None,
) -> None:
    if path is None or not path.is_file():
        _unpin_frame1(pkg, step_id, surface)
        set_frame1_kind(pkg, step_id, surface, "unique")
        pkg.save()
        return
    kind = frame1_type(pkg, step_id, surface)
    if kind == "look":
        apply_input_as_ref(pkg, step_id, path, surface=surface)
        set_frame1_kind(pkg, step_id, surface, "look")
    else:
        apply_input_as_start(pkg, step_id, path, surface=surface)
        set_frame1_kind(pkg, step_id, surface, "start")
    pkg.save()


def _enter_look(
    pkg: RallyPackage,
    step_id: str,
    surface: str,
    held: Path | None,
) -> None:
    _unpin_frame1(pkg, step_id, surface)
    if not look_ref_paths(pkg, step_id):
        if held is not None and held.is_file():
            apply_input_as_ref(pkg, step_id, held, surface=surface)
        elif pkg.path_for("Preview.png").is_file():
            pkg.step(step_id).options["look_refs"] = ["Preview.png"]
    set_frame1_kind(pkg, step_id, surface, "look")
    pkg.save()


def cycle_frame1_type(
    pkg: RallyPackage, step_id: str, surface: str
) -> str:
    order = ["unique", "start", "look"]
    cur = frame1_type(pkg, step_id, surface)
    nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "unique"
    catalog = list_clip_stills(pkg, step_id)
    fallback = catalog[0][1] if catalog else None
    held = frame1_path(pkg, step_id, surface)
    if held is None:
        this = pkg.path_for(
            "IdleKeyframe.png"
            if step_id == "Idle"
            else staged_still_name(step_id)
        )
        held = this if this.is_file() else fallback
    if nxt == "unique":
        _unpin_frame1(pkg, step_id, surface)
        set_frame1_kind(pkg, step_id, surface, "unique")
        pkg.save()
        return "unique"
    if nxt == "look":
        _enter_look(pkg, step_id, surface, held)
        return "look"
    if held is None:
        _unpin_frame1(pkg, step_id, surface)
        set_frame1_kind(pkg, step_id, surface, "unique")
        pkg.save()
        return "unique"
    apply_input_as_start(pkg, step_id, held, surface=surface)
    set_frame1_kind(pkg, step_id, surface, "start")
    pkg.save()
    return "start"


def end_slot_type(pkg: RallyPackage, step_id: str) -> str:
    from app.pipeline.joins import end_join

    if step_id == "Preview":
        return "none"
    st = pkg.step(step_id)
    if st.options.get("imagine_loop") or end_join(pkg, step_id) == "loop":
        return "loop"
    if end_join(pkg, step_id) == "cut" and not str(
        st.options.get("end_file") or ""
    ).strip():
        return "none"
    from app.pipeline.joins import end_path

    if end_path(pkg, step_id) is not None or str(
        st.options.get("end_file") or ""
    ).strip():
        return "pin"
    return "none"


def cycle_end_slot_type(pkg: RallyPackage, step_id: str) -> str:
    order = ["none", "pin", "loop"]
    cur = end_slot_type(pkg, step_id)
    nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "none"
    st = pkg.step(step_id)
    if nxt == "none":
        st.options["end_join"] = "cut"
        st.options.pop("end_file", None)
        st.options["imagine_loop"] = False
    elif nxt == "loop":
        st.options["end_join"] = "loop"
        st.options["imagine_loop"] = True
    else:
        st.options["imagine_loop"] = False
        catalog = list_clip_stills(pkg, step_id)
        idle = pkg.path_for("IdleKeyframe.png")
        held = idle if idle.is_file() else (catalog[0][1] if catalog else None)
        if held is not None:
            set_end_still(pkg, step_id, held)
            return "pin"
        st.options["end_join"] = "cut"
        return "none"
    pkg.save()
    return nxt


def set_end_still(
    pkg: RallyPackage, step_id: str, path: Path | None
) -> None:
    st = pkg.step(step_id)
    if path is None or not path.is_file():
        st.options["end_join"] = "cut"
        st.options.pop("end_file", None)
        st.options["imagine_loop"] = False
        pkg.save()
        return
    rel = _rel_still(pkg, path)
    st.options["end_file"] = rel
    st.options["imagine_loop"] = False
    if path.name == "IdleKeyframe.png":
        st.options["end_join"] = "idle-rest"
    else:
        st.options["end_join"] = "still"
    pkg.save()


def select_input_still(
    pkg: RallyPackage, step_id: str, path: Path, *, surface: str
) -> None:
    """Select the still beside the phone. Role is the overlay, not this click."""
    st = pkg.step(step_id)
    st.options["input_slot"] = _rel_still(pkg, path)
    pkg.save()


def cycle_still_seed(pkg: RallyPackage, step_id: str) -> str:
    staged = pkg.path_for(
        "IdleKeyframe.png"
        if step_id == "Idle"
        else staged_still_name(step_id)
    )
    kinds = ["preview"]
    if staged.is_file():
        kinds.append("self")
    kinds.append("none")
    cur = still_seed_kind(pkg, step_id)
    nxt = kinds[(kinds.index(cur) + 1) % len(kinds)] if cur in kinds else kinds[0]
    st = pkg.step(step_id)
    if nxt == "preview":
        st.options.pop("still_seed", None)
        st.options["still_seed_file"] = "Preview.png"
    elif nxt == "self":
        st.options["still_seed"] = "self"
        try:
            st.options["still_seed_file"] = str(staged.relative_to(pkg.dir()))
        except ValueError:
            st.options["still_seed_file"] = staged.name
    else:
        st.options["still_seed"] = "none"
        st.options["still_seed_file"] = ""
    pkg.save()
    return nxt


def still_seed_path(pkg: RallyPackage, step_id: str) -> Path:
    """Still Imagine image-edits from for Make still.

    Honors a picked ``still_seed_file``. ``still_seed=none`` wins even if a
    leftover file is set — that still is a look-lock, not frame 1.
    """
    from app.pipeline.explicit_mark import as_imagine_still, preview_source_path

    st = pkg.step(step_id)
    seed_key = str(st.options.get("still_seed") or "").strip().lower()
    if seed_key in {"none", "off", "prompt"}:
        return Path()
    rel = str(st.options.get("still_seed_file") or "").strip()
    if rel:
        picked = pkg.path_for(rel)
        if picked.is_file():
            remapped = as_imagine_still(pkg, picked)
            return remapped if remapped is not None else picked
    if not seed_key:
        return Path()
    if seed_key in {"self", "staged"}:
        dest = pkg.path_for(
            "IdleKeyframe.png"
            if step_id == "Idle"
            else staged_still_name(step_id)
        )
        return dest if dest.is_file() else Path()
    if seed_key == "preview":
        preview = preview_source_path(pkg)
        return preview if preview.is_file() else Path()
    return seed_path_for(pkg, step_id, seed_key)


def archive_still_file(pkg: RallyPackage, path: Path) -> Path | None:
    if not path.is_file():
        return None
    hist = pkg.dir() / "history"
    hist.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = hist / f"{path.stem}.{stamp}{path.suffix}"
    shutil.move(str(path), str(dest))
    return dest


async def generate_start_still(
    pkg: RallyPackage,
    step_id: str,
    *,
    backend: str,
    progress: ProgressCb,
    emit,
) -> Path:
    """Always write a new staged still (archives the previous one)."""
    st = pkg.step(step_id)
    dest = pkg.path_for(
        "IdleKeyframe.png"
        if step_id == "Idle"
        else staged_still_name(step_id)
    )
    if dest.name in {"Preview.png", "Preview.clean.png"}:
        raise RuntimeError("Clip stills cannot overwrite the Preview look lock")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if still_seed_kind(pkg, step_id) == "none":
        seed = Path()
    else:
        seed = still_seed_path(pkg, step_id)
    refs = active_look_refs(pkg, step_id, "still")
    refs = [
        p
        for p in refs
        if not seed.is_file() or p.resolve() != seed.resolve()
    ]
    from app.pipeline.formula import compile_formula

    stage = str(st.options.get("start_still_prompt") or "").strip()
    if not stage:
        stage = (st.composed_prompt or st.custom_guidance or "").strip()
    still_prompt = compile_formula(pkg, step_id, "still")

    seed_tmp: Path | None = None
    if seed.is_file() and seed.resolve() == dest.resolve():
        seed_tmp = dest.with_name(dest.stem + ".seed.png")
        shutil.copy2(seed, seed_tmp)
        seed = seed_tmp

    archive_still_file(pkg, dest)
    src_note = seed.name if seed.is_file() else "prompt"
    if refs:
        src_note += " + refs " + ", ".join(p.name for p in refs)
    await emit(progress, f"Staging {dest.name} from {src_note}…")

    if backend in ("cli", "api"):
        from app.pipeline import imagine

        await imagine.generate_image(
            prompt=still_prompt,
            dest=dest,
            aspect_ratio=pkg.aspect_ratio,
            image_path=seed if seed.is_file() else None,
            reference_paths=refs,
        )
    else:
        if seed.is_file():
            shutil.copy2(seed, dest)
        else:
            from PIL import Image, ImageDraw

            w, h = (720, 720) if pkg.aspect_ratio == "1:1" else (720, 1280)
            img = Image.new("RGB", (w, h), (50, 55, 70))
            ImageDraw.Draw(img).text(
                (40, h // 2), f"{step_id} start", fill=(230, 230, 240)
            )
            img.save(dest, format="PNG")

    if seed_tmp is not None:
        seed_tmp.unlink(missing_ok=True)

    st.options["start_mode"] = "idle" if step_id == "Idle" else "staged"
    if step_id != "Idle":
        # Don't pin the next take to this still if Unique shot / Reference
        # is on — that was forcing image-to-video and locking the pose.
        unique = bool(st.options.get("unique_shot")) or str(
            st.options.get("start_join") or ""
        ).strip().lower() == "cut"
        if not unique:
            st.options["start_join"] = "still"
            st.options.pop("unique_shot", None)
    st.options["work_surface"] = "still"
    st.options["start_still_prompt_used"] = stage
    st.options["last_sent_prompt"] = still_prompt
    st.options["last_sent_mode"] = (
        f"still identity plate + refs ({src_note})"
        if not seed.is_file() and refs
        else f"still from {src_note}"
    )
    try:
        st.options["start_file"] = str(dest.relative_to(pkg.dir()))
    except ValueError:
        st.options["start_file"] = dest.name
    pkg.save()
    if dest.is_file() and dest.stat().st_size > 100:
        return dest
    return resolve_start_still(pkg, step_id, extract=True)


async def ensure_video_start_still(
    pkg: RallyPackage,
    step_id: str,
    *,
    backend: str,
    progress: ProgressCb,
    emit,
) -> Path:
    """Use the still the user picked. Never silently invent a new one."""
    _ = backend, progress, emit
    return resolve_start_still(pkg, step_id, extract=True)
