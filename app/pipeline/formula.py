"""Transparent, editable prompt formula for stills and takes.

Sections are generic (Concept, Start frame, Reference, Shot, …) so they
apply to any pack, not just a rally. User edits live in
``step.options['formula_edits']`` and override auto-generated copy.
Compile joins non-empty section text in order — that string is what
Imagine actually gets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.models.rally import RallyPackage


# Paragraphs that used to be compiled into composed_prompt. Shot should
# only be the creative request; these live in their own sections now.
# Only drop residue the old compiler appended. Do not strip user copy
# that starts with "the provided image" / "start frame" — those are
# the pose-lock instructions people edit.
_SHOT_NOISE_PREFIXES = (
    "single still.",
    "sound:",
    "speech uses the voice",
    "loop:",
    "duration:",
    "this clip:",
    "image-to-video begins on",
    "no start-frame pin",
    "reference-to-video:",
    "reference images:",
    "last frame:",
)
_CONSTRAINT_TAIL = re.compile(r"(?:\n+)?Single still\.[^\n]*$", re.I)
# Seed is its own formula section. mark_needs_review used to write the
# full compile into composed_prompt, so this sentence stacked in the
# prompt box on every new take.
_SEED_BOILERPLATE = re.compile(
    r"(?:Image-to-video begins on [^\n]+? \(frame 1\)\.\s*"
    r"The shot describes what happens next\.\s*)+",
    re.I,
)
_NO_PIN_BOILERPLATE = re.compile(
    r"(?:No start-frame pin\.\s*"
    r"Reference-to-video: invent the opening from the shot\.\s*)+",
    re.I,
)
# Pack harness that used to be baked into the shot. Concept, start, and
# end each have their own section. The stock "we're going" beat is not
# a shot someone wrote.
_PACK_LINE_PREFIXES = (
    "concept:",
    "this clip:",
    "starts:",
    "ends:",
)
_PACK_EXACT = frozenset(
    {
        'one "we\'re going" moment from this opening.',
        'one cinematic "we\'re going" moment.',
        "send — the squad is being called; we're going",
        "animate only what is in this still.",
    }
)


def _strip_pack_copy(para: str) -> str:
    kept: list[str] = []
    for line in (para or "").split("\n"):
        low = line.strip().lower()
        if not low:
            continue
        if any(low.startswith(prefix) for prefix in _PACK_LINE_PREFIXES):
            continue
        if low in _PACK_EXACT:
            continue
        if low.startswith("then ") and "plays" in low:
            continue
        if "last frame is reused as the start of" in low:
            continue
        kept.append(line.strip())
    return "\n".join(kept).strip()


_NO_PIN_MARKERS = (
    "no start-frame",
    "no pose seed",
    "no frame 1",
    "no frame-1",
    "invent the pose",
    "invent the opening",
    "unique shot",
    "not image-to-video",
    "not a start frame",
    "no start-frame pin",
)

# Frame 1 / Ref / constraints are driven by the panes and chips, not
# text boxes. Leftover formula_edits for these used to compile as
# ghost lines with nothing to edit in the formula stack.
_DERIVED_SECTIONS = ("seed", "refs", "constraints", "look")

LOOK_ASPECTS = ("faces", "clothes", "pose", "env", "lighting")
LOOK_ASPECT_DEFAULT = ("faces", "clothes")
_LOOK_ASPECT_WORDS = {
    "faces": "faces",
    "clothes": "clothes",
    "pose": "pose",
    "env": "environment",
    "lighting": "lighting",
}


def seed_requests_unique(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return True
    return any(m in t for m in _NO_PIN_MARKERS)


@dataclass
class FormulaSection:
    id: str
    label: str
    text: str
    edited: bool = False
    extra: bool = False
    placeholder: str = ""
    resettable: bool = True
    muted: bool = False


def _edits(pkg: RallyPackage, step_id: str) -> dict:
    raw = pkg.step(step_id).options.get("formula_edits")
    return raw if isinstance(raw, dict) else {}


def _surface(surface: str | None, step_id: str = "") -> str:
    if step_id == "Preview":
        return "still"
    s = (surface or "").strip().lower()
    return s if s in {"still", "take"} else "take"


def _edit_id(surface: str, section_id: str) -> str:
    """Every section is per still/take so the two formulas don't clobber."""
    sid = str(section_id or "").strip()
    surf = _surface(surface)
    if sid.startswith(f"{surf}:"):
        return sid
    return f"{surf}:{sid}"


def _extras_key(surface: str) -> str:
    return f"formula_extra_{_surface(surface)}"


def _mute_key(surface: str, section_id: str) -> str:
    return _edit_id(surface, section_id)


def muted_ids(pkg: RallyPackage, step_id: str, surface: str) -> set[str]:
    raw = pkg.step(step_id).options.get("formula_mute")
    if not isinstance(raw, dict):
        return set()
    surf = _surface(surface, step_id)
    prefix = f"{surf}:"
    out: set[str] = set()
    for key, val in raw.items():
        if not val:
            continue
        k = str(key)
        if k.startswith(prefix):
            out.add(k[len(prefix) :])
        elif ":" not in k:
            out.add(k)
    return out


def is_muted(
    pkg: RallyPackage, step_id: str, surface: str, section_id: str
) -> bool:
    return section_id in muted_ids(pkg, step_id, surface)


def look_aspects(
    pkg: RallyPackage, step_id: str, surface: str
) -> list[str]:
    """Which traits a Ref still should lock. Default faces + clothes."""
    st = pkg.step(step_id)
    surf = _surface(surface, step_id)
    raw = st.options.get("look_aspects")
    chosen = None
    if isinstance(raw, dict):
        val = raw.get(surf)
        if isinstance(val, list):
            chosen = val
    elif isinstance(raw, list):
        chosen = raw
    if chosen is None:
        return list(LOOK_ASPECT_DEFAULT)
    names = {str(x).strip().lower() for x in chosen}
    return [a for a in LOOK_ASPECTS if a in names]


def toggle_look_aspect(
    pkg: RallyPackage, step_id: str, surface: str, aspect: str
) -> list[str]:
    aspect = str(aspect or "").strip().lower()
    if aspect not in LOOK_ASPECTS:
        return look_aspects(pkg, step_id, surface)
    cur = set(look_aspects(pkg, step_id, surface))
    if aspect in cur:
        cur.remove(aspect)
    else:
        cur.add(aspect)
    nxt = [a for a in LOOK_ASPECTS if a in cur]
    st = pkg.step(step_id)
    surf = _surface(surface, step_id)
    raw = st.options.get("look_aspects")
    store = dict(raw) if isinstance(raw, dict) else {}
    store[surf] = nxt
    st.options["look_aspects"] = store
    edits = dict(_edits(pkg, step_id))
    key = _edit_id(surf, "refs")
    if key in edits:
        edits.pop(key)
        changed_edits = True
    else:
        changed_edits = False
    if _prune_derived_edits(edits, surf):
        changed_edits = True
    if changed_edits:
        st.options["formula_edits"] = edits
    pkg.save()
    return nxt


def toggle_mute(
    pkg: RallyPackage, step_id: str, surface: str, section_id: str
) -> bool:
    st = pkg.step(step_id)
    surface = _surface(surface, step_id)
    key = _mute_key(surface, section_id)
    raw = dict(st.options.get("formula_mute") or {})
    nxt = not bool(raw.get(key))
    if nxt:
        raw[key] = True
    else:
        raw.pop(key, None)
    st.options["formula_mute"] = raw
    pkg.save()
    return nxt


def _extras(pkg: RallyPackage, step_id: str, surface: str) -> list:
    st = pkg.step(step_id)
    raw = st.options.get(_extras_key(surface))
    if isinstance(raw, list):
        return raw
    old = st.options.get("formula_extra")
    return list(old) if isinstance(old, list) else []


def save_formula_section(
    pkg: RallyPackage,
    step_id: str,
    section_id: str,
    text: str,
    *,
    surface: str | None = None,
) -> None:
    st = pkg.step(step_id)
    if surface is None:
        surface = "still" if step_id == "Preview" else str(
            st.options.get("work_surface") or "take"
        )
    persist_formula_text(pkg, step_id, surface, {section_id: text})


def reset_formula_section(
    pkg: RallyPackage,
    step_id: str,
    section_id: str,
    *,
    surface: str | None = None,
) -> None:
    st = pkg.step(step_id)
    if surface is None:
        surface = "still" if step_id == "Preview" else str(
            st.options.get("work_surface") or "take"
        )
    edits = dict(_edits(pkg, step_id))
    key = _edit_id(surface, section_id)
    if key in edits:
        edits.pop(key)
        st.options["formula_edits"] = edits
        pkg.save()


def add_formula_section(
    pkg: RallyPackage,
    step_id: str,
    label: str,
    text: str = "",
    *,
    surface: str | None = None,
) -> str:
    st = pkg.step(step_id)
    if surface is None:
        surface = "still" if step_id == "Preview" else str(
            st.options.get("work_surface") or "take"
        )
    extras = list(_extras(pkg, step_id, surface))
    sid = f"extra_{len(extras) + 1}"
    extras.append({"id": sid, "label": (label or "Note").strip() or "Note"})
    st.options[_extras_key(surface)] = extras
    edits = dict(_edits(pkg, step_id))
    edits[_edit_id(surface, sid)] = text
    st.options["formula_edits"] = edits
    pkg.save()
    return sid


def remove_formula_section(
    pkg: RallyPackage,
    step_id: str,
    section_id: str,
    *,
    surface: str | None = None,
) -> None:
    st = pkg.step(step_id)
    if surface is None:
        surface = "still" if step_id == "Preview" else str(
            st.options.get("work_surface") or "take"
        )
    extras = [
        e
        for e in _extras(pkg, step_id, surface)
        if not (isinstance(e, dict) and str(e.get("id") or "") == section_id)
    ]
    st.options[_extras_key(surface)] = extras
    edits = dict(_edits(pkg, step_id))
    key = _edit_id(surface, section_id)
    if key in edits:
        edits.pop(key)
        st.options["formula_edits"] = edits
    pkg.save()


def _prune_derived_edits(edits: dict, surface: str) -> bool:
    surf = _surface(surface)
    changed = False
    for sid in _DERIVED_SECTIONS:
        for key in (f"{surf}:{sid}", sid):
            if key in edits:
                edits.pop(key)
                changed = True
    return changed


def persist_formula_text(
    pkg: RallyPackage,
    step_id: str,
    surface: str,
    values: dict[str, str],
) -> None:
    """Write overrides. Shot is stored per still/take.

    Auto-identical text is cleared so chips (voice, sound, duration,
    start frame) keep driving those sections.
    """
    surface = _surface(surface, step_id)
    auto = {s.id: s.text for s in auto_formula(pkg, step_id, surface)}
    extras = {
        str(e.get("id"))
        for e in _extras(pkg, step_id, surface)
        if isinstance(e, dict) and e.get("id")
    }
    st = pkg.step(step_id)
    edits = dict(_edits(pkg, step_id))
    changed = _prune_derived_edits(edits, surface)
    for sid, raw in values.items():
        if sid in _DERIVED_SECTIONS:
            continue
        text = "" if raw is None else str(raw)
        key = _edit_id(surface, sid)
        if sid == "shot":
            text = _as_shot(text)
            if surface == "still" and step_id != "Preview":
                if str(st.options.get("start_still_prompt") or "") != text:
                    st.options["start_still_prompt"] = text
                    changed = True
            else:
                if st.composed_prompt != text or (
                    st.custom_guidance or ""
                ) != text.strip():
                    st.composed_prompt = text
                    st.custom_guidance = text.strip()
                    changed = True
            if "shot" in edits:
                edits.pop("shot")
                changed = True
            continue
        if sid in extras:
            if edits.get(key) != text:
                edits[key] = text
                changed = True
            continue
        auto_text = auto.get(sid, "")
        if text.strip() == (auto_text or "").strip():
            if key in edits:
                edits.pop(key)
                changed = True
        elif not text.strip() and (auto_text or "").strip() and key not in edits:
            # Empty widget with no prior edit is almost always unsynced
            # Quasar state — don't wipe auto identity/pose language.
            continue
        elif edits.get(key) != text:
            edits[key] = text
            changed = True
    if changed:
        st.options["formula_edits"] = edits
        pkg.save()


def _apply_edits(
    pkg: RallyPackage,
    step_id: str,
    sections: list[FormulaSection],
    surface: str,
) -> list[FormulaSection]:
    edits = _edits(pkg, step_id)
    muted = muted_ids(pkg, step_id, surface)
    out: list[FormulaSection] = []
    for sec in sections:
        if sec.id in _DERIVED_SECTIONS:
            sec.muted = sec.id in muted or (
                sec.id in {"seed", "refs"} and "start" in muted
            ) or (sec.id == "end" and "end" in muted)
            out.append(sec)
            continue
        key = _edit_id(surface, sec.id)
        if key in edits:
            out.append(
                FormulaSection(
                    id=sec.id,
                    label=sec.label,
                    text=str(edits[key] if edits[key] is not None else ""),
                    edited=True,
                    extra=False,
                    placeholder=sec.placeholder,
                    resettable=sec.resettable,
                    muted=sec.id in muted or (
                        sec.id in {"seed", "refs"} and "start" in muted
                    ) or (sec.id == "end" and "end" in muted),
                )
            )
        else:
            sec.muted = sec.id in muted or (
                sec.id in {"seed", "refs"} and "start" in muted
            ) or (sec.id == "end" and "end" in muted)
            out.append(sec)
    extras = _extras(pkg, step_id, surface)
    if isinstance(extras, list):
        for extra in extras:
            if not isinstance(extra, dict):
                continue
            eid = str(extra.get("id") or "").strip()
            if not eid:
                continue
            key = _edit_id(surface, eid)
            out.append(
                FormulaSection(
                    id=eid,
                    label=str(extra.get("label") or "Note"),
                    text=str(edits.get(key) or extra.get("text") or ""),
                    edited=key in edits,
                    extra=True,
                    placeholder="Anything else to compile in.",
                    resettable=False,
                    muted=eid in muted,
                )
            )
    return out


def _file_label(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    if path.name in {"Preview.png", "Preview.clean.png"}:
        return "Preview.png"
    return path.name


def _as_shot(text: str) -> str:
    """Creative shot only. Drop derived seed / sound / duration compile tails."""
    cleaned = _SEED_BOILERPLATE.sub("", text or "")
    cleaned = _NO_PIN_BOILERPLATE.sub("", cleaned)
    chunks = [p.strip() for p in cleaned.split("\n\n") if p.strip()]
    kept: list[str] = []
    for para in chunks:
        para = _SEED_BOILERPLATE.sub("", para).strip()
        para = _NO_PIN_BOILERPLATE.sub("", para).strip()
        para = _strip_pack_copy(para)
        low = para.lower()
        if not para or any(low.startswith(n) for n in _SHOT_NOISE_PREFIXES):
            continue
        para = _CONSTRAINT_TAIL.sub("", para).strip()
        if para:
            kept.append(para)
    return "\n\n".join(kept).strip()


def _ref_tags(refs: list[Path], *, start_index: int) -> str:
    return ", ".join(
        f"<IMAGE_{start_index + i}> {_file_label(p)}" for i, p in enumerate(refs)
    )


def _join_words(items: list[str], conj: str = "and") -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} {conj} {items[1]}"
    return ", ".join(items[:-1]) + f", {conj} " + items[-1]


def _ref_use_sentence(aspects: list[str]) -> str:
    """Positive-only. Unselected traits are omitted, not negated."""
    on = [_LOOK_ASPECT_WORDS[a] for a in LOOK_ASPECTS if a in aspects]
    if not on:
        return ""
    return f"Use them for {_join_words(on, 'and')}."


def _shot_source(st, surface: str = "take", step_id: str = "") -> str:
    if _surface(surface, step_id) == "still" and step_id != "Preview":
        text = str(st.options.get("start_still_prompt") or "").strip()
        if not text:
            text = (st.custom_guidance or st.composed_prompt or "").strip()
    else:
        text = (st.composed_prompt or st.custom_guidance or "").strip()
    return _as_shot(text)


def auto_formula(
    pkg: RallyPackage, step_id: str, surface: str
) -> list[FormulaSection]:
    """Auto-generated sections only (no user overrides). Order = compile order."""
    from app.pipeline.joins import (
        end_join,
        end_path,
        start_join,
        start_path,
        take_is_unique,
    )
    from app.pipeline.start_still import (
        active_look_refs,
        still_seed_kind,
        still_seed_path,
    )

    st = pkg.step(step_id)
    is_still = surface == "still" or step_id == "Preview"
    aspect = (pkg.aspect_ratio or "9:16").strip() or "9:16"
    sections: list[FormulaSection] = []

    concept = (pkg.concept or "").strip()
    sections.append(
        FormulaSection(
            "concept",
            "Concept",
            f"Concept: {concept}" if concept else "",
            placeholder="What this is",
        )
    )

    look = (getattr(pkg, "skill_note", None) or "").strip()
    sections.append(
        FormulaSection(
            "look",
            "Look",
            look,
            placeholder="Look lock, bible, identity, wardrobe",
        )
    )

    refs = (
        active_look_refs(pkg, step_id, surface)
        if step_id != "Preview"
        else []
    )

    if is_still:
        if step_id == "Preview":
            ref = pkg.path_for("Preview.reference.png")
            if ref.is_file():
                seed_text = (
                    f"The provided image ({_file_label(ref)}) is the look "
                    "lock for an image-edit. Keep the subject. "
                    f"Reframe to {aspect}. Still only."
                )
            else:
                seed_text = ""
            sections.append(
                FormulaSection(
                    "seed",
                    "Start frame",
                    seed_text,
                    placeholder="No start-frame image — text-to-image.",
                )
            )
            ref_index = 1 if ref.is_file() else 0
        else:
            kind = still_seed_kind(pkg, step_id)
            seed = still_seed_path(pkg, step_id)
            seed_name = _file_label(seed if seed.is_file() else None)
            if kind == "none" or not seed_name:
                if refs:
                    seed_text = (
                        "<IMAGE_0> is a blank plate for aspect. Invent pose, "
                        "camera, and composition from the shot."
                    )
                    ref_index = 1
                else:
                    seed_text = (
                        "No start-frame image. Text-to-image. Invent the "
                        "pose, camera, and composition from the shot."
                    )
                    ref_index = 0
            else:
                seed_text = (
                    f"The first provided image ({seed_name}) is the pose "
                    "and composition seed. Keep identity. Restage for this "
                    "shot."
                )
                refs = [
                    p
                    for p in refs
                    if not seed.is_file() or _file_label(p) != seed_name
                ]
                ref_index = 1
            sections.append(
                FormulaSection(
                    "seed",
                    "Start frame",
                    seed_text,
                    placeholder="How the opening image is used",
                )
            )

        if refs:
            ref_text = (
                "Reference images: "
                + _ref_tags(refs, start_index=ref_index)
                + ". "
                + _ref_use_sentence(look_aspects(pkg, step_id, surface))
            )
        else:
            ref_text = ""
        sections.append(
            FormulaSection(
                "refs",
                "Reference",
                ref_text,
                placeholder="Look-lock stills as <IMAGE_n>. Empty = none.",
            )
        )

        sections.append(
            FormulaSection(
                "shot",
                "Shot",
                _shot_source(st, "still", step_id),
                placeholder="What this still is.",
                resettable=False,
            )
        )
        sections.append(
            FormulaSection(
                "constraints",
                "Constraints",
                "Single still, "
                f"{aspect}. No motion blur, captions, logos, or "
                "on-image text.",
            )
        )
        return sections

    sj = start_join(pkg, step_id)
    sp = start_path(pkg, step_id)
    seed_name = _file_label(sp)
    if take_is_unique(pkg, step_id) or sj == "cut" or not seed_name:
        seed_text = (
            "No start-frame pin. Reference-to-video: invent the opening "
            "from the shot."
        )
        if sp is not None and sp.is_file():
            refs = [p for p in refs if _file_label(p) != seed_name]
    else:
        seed_text = (
            f"Image-to-video begins on {seed_name} (frame 1). "
            "The shot describes what happens next."
        )
        refs = [
            p
            for p in refs
            if sp is None or not sp.is_file() or _file_label(p) != seed_name
        ]
    sections.append(
        FormulaSection(
            "seed",
            "Start frame",
            seed_text,
            placeholder="How the opening image is used",
        )
    )

    if refs:
        ref_text = (
            "Reference images: "
            + _ref_tags(refs, start_index=0)
            + ". "
            + _ref_use_sentence(look_aspects(pkg, step_id, surface))
        )
    else:
        ref_text = ""
    sections.append(
        FormulaSection(
            "refs",
            "Reference",
            ref_text,
            placeholder="Look-lock stills as <IMAGE_n>. Empty = none.",
        )
    )

    sections.append(
        FormulaSection(
            "shot",
            "Shot",
            _shot_source(st, "take", step_id),
            placeholder="One action. Camera. What happens in this take.",
            resettable=False,
        )
    )

    kit = getattr(pkg, "kit", None) or {}
    sounds = [
        str(s).strip() for s in (kit.get("sounds") or []) if str(s).strip()
    ]
    sections.append(
        FormulaSection(
            "sound",
            "Sound",
            ("Sound: " + ", ".join(sounds) + ".") if sounds else "",
            placeholder="What you hear in this shot",
        )
    )

    speaker = str(st.options.get("speaker") or "").strip()
    voices = kit.get("voices") or []
    if speaker and speaker.lower() not in {"invent", "none", "auto", ""}:
        idx = 0
        for i, v in enumerate(voices):
            if str(v.get("voice_id") or "").lower() == speaker.lower():
                idx = i
                break
        voice_text = (
            f"Speech uses the voice from <AUDIO_{idx}> ({speaker})."
        )
    else:
        voice_text = ""
    sections.append(
        FormulaSection(
            "voice",
            "Voice",
            voice_text,
            placeholder="Empty = Imagine invents speech from the shot.",
        )
    )

    ej = end_join(pkg, step_id)
    ep = end_path(pkg, step_id)
    if ej == "loop" or st.options.get("imagine_loop"):
        end_text = "Loop: last frame matches frame 1."
    elif ep is not None and ep.is_file():
        end_text = f"Last frame: {_file_label(ep)}. Land on this still."
    else:
        end_text = ""
    sections.append(
        FormulaSection(
            "end",
            "Last frame",
            end_text,
            placeholder="Empty = Imagine chooses the ending.",
        )
    )

    dur = st.options.get("duration")
    if dur not in (None, ""):
        dur_text = f"Duration: {dur} seconds."
    else:
        dur_text = ""
    sections.append(
        FormulaSection(
            "duration",
            "Duration",
            dur_text,
            placeholder="Duration in seconds.",
        )
    )
    return sections


def build_formula(
    pkg: RallyPackage, step_id: str, surface: str
) -> list[FormulaSection]:
    """Auto sections + user overrides. Order is the compile order."""
    return _apply_edits(
        pkg, step_id, auto_formula(pkg, step_id, surface), surface
    )


def compile_formula(
    pkg: RallyPackage, step_id: str, surface: str | None = None
) -> str:
    if surface is None:
        if step_id == "Preview":
            surface = "still"
        else:
            surface = str(
                pkg.step(step_id).options.get("work_surface") or "take"
            )
    sections = build_formula(pkg, step_id, surface)
    text = "\n\n".join(
        s.text.strip()
        for s in sections
        if (s.text or "").strip() and not s.muted
    )
    if surface == "still" and step_id != "Preview":
        from app.pipeline.start_still import active_look_refs, still_seed_kind

        if still_seed_kind(pkg, step_id) == "none" and active_look_refs(
            pkg, step_id, surface
        ):
            if "<IMAGE_0>" not in text:
                text = (
                    "<IMAGE_0> is a blank plate for aspect. Invent pose, "
                    "camera, and composition from the shot.\n\n" + text
                )
    return text
