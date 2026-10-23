"""Load prompt templates and compose Imagine prompts.

The harness describes clip connections (starts / ends / cuts), not product
copy. Content lives in the concept, the provided still, and custom guidance.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config import DEFAULTS_DIR, SKILL_PATH, STEP_PROMPTS_PATH
from app.models.rally import (
    PIPELINE_STEPS,
    RallyPackage,
    StepKind,
    StepState,
    StepStatus,
)

TEMPLATES_DIR = DEFAULTS_DIR / "templates"
DEFAULT_TEMPLATE_ID = "rally"

# What each clip does in the rally. Grok sees these as roles; the brief UI too.
STEP_ROLES = {
    "Preview": "recognition still — the poster of this world at rest",
    "Idle": "waiting — the rest loop while nobody has answered",
    "Rally": "send — the squad is being called; we're going",
    "Message": "incoming — the ping arriving; attention in-world",
    "Accept": "yes — I'm in; commit to what Rally started",
    "Decline": "no — can't make it; abort, stand-down, or roast",
    "Adjust": "not now — wait-one; hold the plan without yes or no",
}

# App playback: Rally/Message play before Idle → start still is Preview.
# Accept/Decline/Reschedule play after Idle → start still is Idle's last frame.
KEYFRAME_PREVIEW_STEPS = frozenset({"Rally", "Message"})
KEYFRAME_IDLE_STEPS = frozenset({"Accept", "Decline", "Adjust"})
_VALID_STARTS = frozenset({"preview", "idle", "idle_last", "staged", "none"})

# Fallback graph if a template omits start/end/cuts_to/loop.
_DEFAULT_GRAPH: dict[str, dict[str, Any]] = {
    "Preview": {
        "start": "none",
        "end": "none",
        "cuts_to": "",
        "loop": False,
    },
    "Idle": {
        "start": "idle",
        "end": "self",
        "cuts_to": "",
        "loop": True,
    },
    "Rally": {
        "start": "preview",
        "end": "hold",
        "cuts_to": "Idle",
        "loop": False,
    },
    "Message": {
        "start": "preview",
        "end": "hold",
        "cuts_to": "Idle",
        "loop": False,
    },
    "Accept": {
        "start": "idle_last",
        "end": "hold",
        "cuts_to": "Idle",
        "loop": False,
    },
    "Decline": {
        "start": "idle_last",
        "end": "hold",
        "cuts_to": "Idle",
        "loop": False,
    },
    "Adjust": {
        "start": "idle_last",
        "end": "hold",
        "cuts_to": "Idle",
        "loop": False,
    },
}

_START_NAMES = {
    "preview": "Preview",
    "idle": "Idle rest",
    "idle_last": "Idle last frame",
    "staged": "staged still",
}
_END_NAMES = {
    "self": "same still",
    "idle": "Idle rest",
    "preview": "Preview",
    "hold": "hold",
}

# Old template copy that made Imagine draw invite cards / phone mockups.
# If a stored base still has these, compose swaps in the current template beat.
_PITFALL_MARKERS = (
    "mobile game",
    "invitation skin",
    "phone-readable",
    "phone screen",
    "game-friendly",
    "game-ready",
    "call-to-action",
    "you've been rallied",
    "game ui",
    "game invitation",
    "invite",
    "hud",
)


def has_prompt_pitfall(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _PITFALL_MARKERS)


def _beat_without_pitfalls(
    base: str, step_id: str, template_id: str
) -> str:
    text = (base or "").strip()
    if not text:
        return ""
    low = text.lower()
    if any(m in low for m in _PITFALL_MARKERS):
        if not step_id:
            return ""
        fallback = default_base_prompt(step_id, template_id)
        # If the template itself were dirty, don't loop — drop the beat.
        if any(m in fallback.lower() for m in _PITFALL_MARKERS):
            return ""
        return fallback
    return text


def list_templates() -> list[dict[str, str]]:
    """Return [{id, label, description}, …] sorted by label.

    Kind pickers are gone; Grok bakes the visual approach into beats.
    One internal skeleton is enough. Kept so old packages still load.
    """
    found: list[dict[str, str]] = []
    if not TEMPLATES_DIR.is_dir():
        return [
            {
                "id": DEFAULT_TEMPLATE_ID,
                "label": "Rally",
                "description": "Single rally skeleton",
            }
        ]
    for path in sorted(TEMPLATES_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        tid = str(data.get("id") or path.stem)
        found.append(
            {
                "id": tid,
                "label": str(data.get("label") or tid.title()),
                "description": str(data.get("description") or "").strip(),
            }
        )
    if not found:
        found.append(
            {
                "id": DEFAULT_TEMPLATE_ID,
                "label": "Rally",
                "description": "",
            }
        )
    return found


def template_path(template_id: str) -> Path:
    tid = (template_id or DEFAULT_TEMPLATE_ID).strip().lower()
    p = TEMPLATES_DIR / f"{tid}.yaml"
    if p.is_file():
        return p
    # Fallbacks — one skeleton is enough; older packs used character/scene/craft
    for candidate in (
        TEMPLATES_DIR / f"{DEFAULT_TEMPLATE_ID}.yaml",
        TEMPLATES_DIR / "character.yaml",
        STEP_PROMPTS_PATH,
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"No template '{template_id}' under {TEMPLATES_DIR}"
    )


@lru_cache(maxsize=8)
def load_template(template_id: str = DEFAULT_TEMPLATE_ID) -> dict[str, Any]:
    path = template_path(template_id)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "steps" not in data:
        raise ValueError(f"Missing 'steps' in {path}")
    data.setdefault("id", path.stem)
    data.setdefault("label", path.stem.title())
    data.setdefault("concept_label", "Concept")
    return data


def reload_templates() -> None:
    load_template.cache_clear()
    load_step_defaults.cache_clear()


@lru_cache(maxsize=1)
def load_step_defaults() -> dict[str, Any]:
    """Back-compat: character template (or shim step_prompts.yaml)."""
    return load_template(DEFAULT_TEMPLATE_ID)


def reload_step_defaults() -> dict[str, Any]:
    reload_templates()
    return load_template(DEFAULT_TEMPLATE_ID)


def default_base_prompt(
    step_id: str, template_id: str = DEFAULT_TEMPLATE_ID
) -> str:
    cfg = load_template(template_id)["steps"].get(step_id) or {}
    return str(cfg.get("base_prompt", "")).strip()


def loop_rule(template_id: str = DEFAULT_TEMPLATE_ID) -> str:
    return str(load_template(template_id).get("loop_rule", "")).strip()


def concept_label(template_id: str = DEFAULT_TEMPLATE_ID) -> str:
    _ = template_id
    return "Concept"


def _step_cfg(step_id: str, template_id: str) -> dict[str, Any]:
    try:
        return load_template(template_id)["steps"].get(step_id) or {}
    except Exception:  # noqa: BLE001
        return {}


def step_graph(
    step_id: str, template_id: str = DEFAULT_TEMPLATE_ID
) -> dict[str, Any]:
    """Return {start, end, cuts_to, loop} for a pipeline step."""
    out = dict(_DEFAULT_GRAPH.get(step_id) or {
        "start": "none",
        "end": "none",
        "cuts_to": "",
        "loop": False,
    })
    cfg = _step_cfg(step_id, template_id)
    for key in ("start", "end", "cuts_to"):
        raw = cfg.get(key)
        if raw is None or raw == "":
            continue
        out[key] = str(raw).strip()
    if "loop" in cfg and cfg["loop"] is not None:
        out["loop"] = bool(cfg["loop"])
    start = str(out.get("start") or "").strip().lower()
    if start not in _VALID_STARTS:
        # Older templates used keyframe: preview|idle
        kf = str(cfg.get("keyframe", "")).strip().lower()
        if kf in _VALID_STARTS:
            start = kf
        elif step_id in KEYFRAME_PREVIEW_STEPS:
            start = "preview"
        elif step_id in KEYFRAME_IDLE_STEPS:
            start = "idle_last"
        elif step_id == "Idle":
            start = "idle"
        else:
            start = "none"
        out["start"] = start
    else:
        out["start"] = start
    out["end"] = str(out.get("end") or "none").strip().lower()
    out["cuts_to"] = str(out.get("cuts_to") or "").strip()
    return out


def staged_still_name(step_id: str) -> str:
    from app.models.rally import opening_still_name

    return opening_still_name(step_id)


def keyframe_source_for_step(
    step_id: str,
    template_id: str = DEFAULT_TEMPLATE_ID,
    options: dict | None = None,
) -> str:
    """
    Return ``preview`` | ``idle`` | ``idle_last`` | ``staged`` | ``none``.

    Step options ``start_mode`` (from Grok) override the template default.
    """
    if step_id == "Idle":
        return "preview"
    opts = options or {}
    mode = str(opts.get("start_mode") or "").strip().lower()
    if mode in _VALID_STARTS and mode != "none":
        return mode
    cfg = _step_cfg(step_id, template_id)
    for key in ("start", "keyframe"):
        raw = str(cfg.get(key, "")).strip().lower()
        if raw in _VALID_STARTS and raw != "none":
            return raw
    graph = step_graph(step_id, template_id)
    start = str(graph.get("start") or "none")
    if start in _VALID_STARTS and start != "none":
        return start
    if step_id in KEYFRAME_PREVIEW_STEPS:
        return "preview"
    if step_id in KEYFRAME_IDLE_STEPS:
        return "idle_last"
    return "none"


def connection_label(
    step_id: str,
    template_id: str = DEFAULT_TEMPLATE_ID,
    options: dict | None = None,
) -> str:
    """Short UI line: Starts X · Ends Y · Cuts to Z."""
    g = step_graph(step_id, template_id)
    start = g.get("start") or "none"
    mode = str((options or {}).get("start_mode") or "").strip().lower()
    if mode in _VALID_STARTS and mode != "none":
        start = mode
    if start == "none":
        return "Recognition still — story clips may start here or on an invented opening"
    start_n = _START_NAMES.get(start, start)
    end = g.get("end") or "none"
    end_n = _END_NAMES.get(end, end)
    bits = [f"Starts {start_n}", f"ends {end_n}"]
    cuts = g.get("cuts_to")
    if cuts:
        bits.append(f"cuts to {cuts}")
    elif g.get("loop"):
        bits.append("loops")
    return " · ".join(bits)


def _lock_phrase(template_id: str) -> str:
    tid = (template_id or "").strip().lower()
    if tid == "scene":
        return "setting, camera, lighting"
    if tid == "craft":
        return "airframe, camera, lighting"
    return "identity, camera, lighting"


def _starts_line(start: str, lock: str) -> str:
    _ = lock
    no_spawn = "Animate only what is in this still."
    if start == "preview":
        return f"Starts: this still is frame 1. {no_spawn}"
    if start == "idle":
        return (
            "Starts: Idle rest. Camera locked. Tiny looping life only."
        )
    if start == "idle_last":
        return f"Starts: last frame of Idle. {no_spawn}"
    if start == "staged":
        return (
            "Starts: invented opening still (frame 1). "
            + no_spawn
        )
    return f"Starts: this still is frame 1. {no_spawn}"


def _ends_line(end: str) -> str:
    if end == "self":
        return "Ends: this same still — frame 1 and last frame match."
    if end == "idle":
        return (
            "Ends: locked Idle rest (Idle frame 1) — same camera and framing."
        )
    if end == "preview":
        return "Ends: the Preview still."
    if end == "hold":
        return "Ends: hold the last frame of this beat."
    return ""


def _transition_line(step_id: str, cuts_to: str, loop: bool) -> str:
    """Shot instruction only. Idle looping is a user setting, not the default."""
    _ = step_id, cuts_to, loop
    return ""


def connection_harness(
    step_id: str,
    *,
    kind: StepKind | str,
    template_id: str = DEFAULT_TEMPLATE_ID,
    start_mode: str = "",
) -> str:
    """START / END / TRANSITION block sent to Imagine."""
    kind_val = kind.value if isinstance(kind, StepKind) else kind
    if kind_val in (StepKind.IMAGE.value, "image"):
        return ""
    g = step_graph(step_id, template_id)
    lock = _lock_phrase(template_id)
    start = (start_mode or str(g.get("start") or "none")).strip().lower()
    lines = [
        _starts_line(start, lock),
        _ends_line(str(g.get("end") or "none")),
        _transition_line(
            step_id, str(g.get("cuts_to") or ""), bool(g.get("loop"))
        ),
    ]
    return "\n".join(line for line in lines if line).strip()


def load_skill_text() -> str:
    for path in (SKILL_PATH.with_name("SKILL.next.md"), SKILL_PATH):
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
    return ""


def default_step_states(
    template_id: str = DEFAULT_TEMPLATE_ID,
) -> dict[str, StepState]:
    data = load_template(template_id)
    steps_cfg = data["steps"]
    result: dict[str, StepState] = {}
    for step_id in PIPELINE_STEPS:
        cfg = steps_cfg.get(step_id) or {}
        kind = StepKind(cfg.get("kind", "video"))
        status = StepStatus.READY
        options: dict = {}
        if step_id == "Idle":
            options = {
                "generate_mp4": True,
                "generate_png": False,
                "boomerang": False,
            }
        elif step_id == "Preview":
            options = {
                "explicit_e": False,
                "explicit_e_style": "black",
                "explicit_e_hex": "",
            }
        role_raw = cfg.get("role", "")
        if isinstance(role_raw, bool):
            role = "yes" if role_raw else "no"
        elif "role" in cfg:
            role = str(role_raw or "")
        else:
            role = STEP_ROLES.get(step_id, "")
        result[step_id] = StepState(
            id=step_id,
            label=str(cfg.get("label", step_id)),
            role=role,
            kind=kind,
            status=status,
            base_prompt=str(cfg.get("base_prompt", "")).strip(),
            custom_guidance="",
            custom_placeholder=str(cfg.get("custom_placeholder", "")),
            outputs=list(cfg.get("outputs", [])),
            options=options,
        )
    return result


def compose_prompt(
    *,
    concept: str,
    base_prompt: str,
    custom_guidance: str,
    kind: StepKind | str,
    step_id: str = "",
    character_bible: str = "",
    include_skill_snippet: bool = False,
    template_id: str = DEFAULT_TEMPLATE_ID,
    start_mode: str = "",
    tone: str = "",
) -> str:
    """Build the prompt string that will be sent to Imagine.

    Stills: concept + look + still brief. No rally tone — Accept/Decline
    copy in Tone becomes on-image captions.
    Video: concept + tone + start/end harness + shot brief. The still
    already carries look — do not re-describe composition.
    """
    parts: list[str] = []
    kind_val = kind.value if isinstance(kind, StepKind) else str(kind)
    is_motion = kind_val in (
        StepKind.VIDEO.value,
        StepKind.IDLE.value,
        "video",
        "idle",
    )

    if include_skill_snippet:
        skill = load_skill_text()
        if skill:
            parts.append(skill[:1500])

    concept = concept.strip()
    if concept:
        parts.append(f"Concept: {concept}")

    # Tone is rally register (Accept/Decline meaning). It belongs on motion
    # clips, not on the recognition still — Imagine paints it as captions.
    tone_line = (tone or "").strip()
    if tone_line and is_motion:
        cleaned = imagine_tone(tone_line)
        if cleaned:
            parts.append(f"Tone: {cleaned}")

    bible = character_bible.strip()
    if bible and not is_motion:
        parts.append(bible)

    custom = custom_guidance.strip()
    if custom:
        parts.append(custom)

    base = _beat_without_pitfalls(base_prompt, step_id, template_id)
    stock = default_base_prompt(step_id, template_id)
    # Stock template lines are fallbacks. If Invent/the user already wrote a
    # beat, do not glue on "rotors at idle" (or any other skeleton default).
    # Skeleton beats ("we're going", squad send) are not the shot.
    if base and base not in custom and not _is_stock_beat(base):
        if not (custom and base == stock):
            parts.append(base)

    if not is_motion:
        no_text = (
            "Single still. No captions, speech bubbles, logos, watermarks, "
            "or on-image text."
        )
        blob = "\n".join(parts)
        if "No captions" not in blob and "on-image text" not in blob:
            parts.append(no_text)

    return "\n\n".join(parts).strip()


def refresh_composed(
    step: StepState,
    concept: str,
    bible: str = "",
    template_id: str = DEFAULT_TEMPLATE_ID,
) -> str:
    step.composed_prompt = compose_prompt(
        concept=concept,
        base_prompt=step.base_prompt,
        custom_guidance=step.custom_guidance,
        kind=step.kind,
        step_id=step.id,
        character_bible=bible,
        template_id=template_id,
        start_mode=str(step.options.get("start_mode") or ""),
        tone=str(step.options.get("tone") or ""),
    )
    return step.composed_prompt


def package_bible(pkg: RallyPackage) -> str:
    return (getattr(pkg, "skill_note", None) or "").strip()


def refresh_package_composed(pkg: RallyPackage) -> None:
    tid = getattr(pkg, "template", "character") or "character"
    bible = package_bible(pkg)
    for st in pkg.steps.values():
        refresh_composed(
            st, pkg.concept, bible=bible, template_id=tid
        )


def compose_idle_still_prompt(
    *,
    concept: str,
    base_prompt: str,
    custom_guidance: str,
    character_bible: str = "",
    template_id: str = DEFAULT_TEMPLATE_ID,
    extra_still: str = "",
) -> str:
    """Keyframe still: rest state only — no motion / loop / product language."""
    parts: list[str] = []
    if concept.strip():
        parts.append(f"Concept: {concept.strip()}")
    if character_bible.strip():
        parts.append(character_bible.strip())

    cfg = _step_cfg("Idle", template_id)
    still = str(cfg.get("still_prompt", "")).strip()
    if not still:
        base = base_prompt.strip()
        if "MOTION" in base.upper():
            still = base.split("MOTION")[0].strip()
        else:
            still = (
                "Neutral rest pose of the same subject. "
                "Same camera and lighting."
            )
    if still:
        parts.append(still)
    extra = (extra_still or "").strip()
    if extra:
        parts.append(extra)
    parts.append(
        "The provided image is the same subject and world. "
        "Keep identity, camera, lighting. Still only."
    )
    if custom_guidance.strip():
        parts.append(custom_guidance.strip())
    return "\n\n".join(parts).strip()


def compose_staged_still_prompt(
    *,
    concept: str,
    start_still_prompt: str,
    character_bible: str = "",
    template_id: str = DEFAULT_TEMPLATE_ID,
    character_refs: bool = False,
) -> str:
    """Image-edit a seed (and optional character refs) into a clip still."""
    parts: list[str] = []
    if concept.strip():
        parts.append(f"Concept: {concept.strip()}")
    if character_bible.strip():
        parts.append(character_bible.strip())
    if character_refs:
        parts.append(
            "The first provided image is the pose/composition seed if one "
            "is given. Additional images are the character/look lock "
            "(same person, outfit, identity). Restage for THIS shot — "
            "do not copy a reference’s composition unless the beat requires "
            "it. Still only. Ignore motion, speech, and sound."
        )
    else:
        parts.append(
            "The provided image is the character and look lock. "
            "Keep the same person, outfit, and identity. "
            "Restage and reframe for THIS shot's opening pose — "
            "do not copy the source composition unless the beat requires it. "
            "Still only. Ignore motion, speech, and sound."
        )
    stage = (start_still_prompt or "").strip()
    if stage:
        parts.append("Opening pose for this clip:\n" + stage)
    parts.append("Single still. No motion blur, captions, or logos.")
    return "\n\n".join(parts).strip()


def video_start_filename(
    step_id: str,
    start_mode: str,
    *,
    idle_last_exists: bool = False,
) -> str:
    """Canonical frame-1 filename for a clip given start_mode."""
    if step_id == "Idle":
        return "IdleKeyframe.png"
    mode = (start_mode or "").strip().lower()
    if mode == "idle":
        return "IdleKeyframe.png"
    if mode == "idle_last":
        return "Idle.last.png" if idle_last_exists else "IdleKeyframe.png"
    if mode == "staged":
        return staged_still_name(step_id)
    return "Preview.png"


def still_seed_filename(seed: str, step_id: str) -> str:
    """Which on-disk still Generate still image-edits from."""
    s = (seed or "preview").strip().lower()
    if s in {"idle", "idle_kf"}:
        return "IdleKeyframe.png"
    if s == "idle_last":
        return "Idle.last.png"
    if s in {"staged", "self"}:
        if step_id == "Idle":
            return "IdleKeyframe.png"
        return staged_still_name(step_id)
    return "Preview.png"


def generation_calls(
    *,
    step_id: str,
    kind: StepKind | str,
    concept: str = "",
    bible: str = "",
    base_prompt: str = "",
    custom_guidance: str = "",
    tone: str = "",
    template_id: str = DEFAULT_TEMPLATE_ID,
    start_mode: str = "",
    start_still_prompt: str = "",
    still_seed: str = "preview",
    aspect_ratio: str = "9:16",
    use_reference: bool = False,
    idle_want_mp4: bool = True,
    idle_want_png: bool = False,
    staged_still_exists: bool = False,
    idle_last_exists: bool = False,
) -> list[dict[str, str]]:
    """Exact Imagine calls Generate / Generate still will send, in order.

    Each item: kind (image|still|video), title, note, prompt.
    The UI renders this as the composed box so every field is visible.
    """
    kind_val = kind.value if isinstance(kind, StepKind) else str(kind)
    tid = template_id or DEFAULT_TEMPLATE_ID
    calls: list[dict[str, str]] = []

    video_prompt = compose_prompt(
        concept=concept,
        base_prompt=base_prompt,
        custom_guidance=custom_guidance,
        kind=kind,
        step_id=step_id,
        character_bible=bible,
        template_id=tid,
        start_mode=start_mode,
        tone=tone,
    )

    if kind_val in (StepKind.IMAGE.value, "image") or step_id == "Preview":
        prompt = video_prompt
        if use_reference:
            prompt = (
                f"{prompt}\n\n"
                "The provided image is the look lock. Keep the subject. "
                f"Reframe to {aspect_ratio}. Still only."
            )
            title = "Image · image-edit Preview.reference.png → Preview.png"
        else:
            title = "Image · text-to-image → Preview.png"
        calls.append(
            {"kind": "image", "title": title, "note": "", "prompt": prompt}
        )
        return calls

    if kind_val in (StepKind.IDLE.value, "idle") or step_id == "Idle":
        still_prompt = compose_idle_still_prompt(
            concept=concept,
            base_prompt=base_prompt,
            custom_guidance=custom_guidance,
            character_bible=bible,
            template_id=tid,
            extra_still=start_still_prompt,
        )
        calls.append(
            {
                "kind": "still",
                "title": "Still · image-edit Preview.png → IdleKeyframe.png",
                "note": "",
                "prompt": still_prompt,
            }
        )
        if idle_want_mp4:
            calls.append(
                {
                    "kind": "video",
                    "title": (
                        "Video · image-to-video IdleKeyframe.png → Idle.mp4"
                    ),
                    "note": "",
                    "prompt": video_prompt,
                }
            )
        return calls

    start_file = video_start_filename(
        step_id,
        start_mode,
        idle_last_exists=idle_last_exists,
    )
    from app.models.rally import clip_video_name

    dest_name = clip_video_name(step_id)
    stage = (start_still_prompt or "").strip()
    mode = (start_mode or "").strip().lower()
    seed_name = still_seed_filename(still_seed, step_id)
    if mode == "staged" or stage:
        if mode == "staged":
            start_file = staged_still_name(step_id)
            if staged_still_exists:
                note = (
                    f"{start_file} is on disk — Generate video will "
                    "animate that file. Generate still to replace it."
                )
            else:
                note = (
                    "No start still on disk yet. Generate still now, or "
                    "Generate video will stage this still first."
                )
        else:
            note = (
                "Not frame 1 yet — pick This clip or Generate still. "
                f"Video currently uses {start_file}."
            )
        calls.append(
            {
                "kind": "still",
                "title": (
                    f"Still · image-edit {seed_name} → "
                    f"{staged_still_name(step_id)}"
                ),
                "note": note,
                "prompt": compose_staged_still_prompt(
                    concept=concept,
                    start_still_prompt=stage,
                    character_bible=bible,
                    template_id=tid,
                ),
            }
        )
    calls.append(
        {
            "kind": "video",
            "title": f"Video · image-to-video {start_file} → {dest_name}",
            "note": (
                "Animates only what is already in the still. "
                "People not in frame 1 will be invented as extras."
            ),
            "prompt": video_prompt,
        }
    )
    return calls


def format_generation_preview(calls: list[dict[str, str]]) -> str:
    """Human-readable dump of every Imagine prompt that will be sent."""
    if not calls:
        return ""
    blocks: list[str] = []
    n = len(calls)
    for i, call in enumerate(calls, 1):
        title = (call.get("title") or call.get("kind") or "call").strip()
        note = (call.get("note") or "").strip()
        prompt = (call.get("prompt") or "").strip()
        head = f"── {title} ──" if n == 1 else f"── {i}/{n} {title} ──"
        parts = [head]
        if note:
            parts.append(f"({note})")
        if prompt:
            parts.append(prompt)
        blocks.append("\n".join(parts).strip())
    return "\n\n".join(blocks)


def apply_template_to_package_steps(
    steps: dict[str, StepState],
    template_id: str,
    *,
    overwrite_base: bool = True,
) -> None:
    """
    Re-apply template base prompts / labels / placeholders to existing steps.

    Does not wipe custom_guidance or status. Used when creating a rally or
    switching template intentionally.
    """
    defaults = default_step_states(template_id)
    for sid, default in defaults.items():
        if sid not in steps:
            steps[sid] = default
            continue
        st = steps[sid]
        if overwrite_base:
            st.base_prompt = default.base_prompt
            st.label = default.label
            st.role = default.role
            st.custom_placeholder = default.custom_placeholder
            st.outputs = list(default.outputs)
            # Preserve idle option keys if present
            if sid == "Idle":
                for k, v in default.options.items():
                    st.options.setdefault(k, v)


_STEP_NAME_RE = re.compile(
    r"\b(accept|decline|reschedule|preview|rally|message|idle)\b",
    re.IGNORECASE,
)


def imagine_tone(tone: str) -> str:
    """Motion register without pipeline vocabulary (those become captions)."""
    text = (tone or "").strip()
    if not text:
        return ""
    bits = re.split(r"(?<=[.!;])\s+", text)
    keep = [b for b in bits if b and not _STEP_NAME_RE.search(b)]
    return " ".join(keep).strip()


_STOCK_BEATS = frozenset(
    {
        "tiny reversible life — breath, sway, rotors at idle, a light.",
        "tiny reversible life — breath, sway, or blink.",
        "tiny reversible life — flicker, pulse, or drift.",
        "tiny reversible life — breath, sway, or a small idle motion.",
        "full-frame still of the subject at rest, vertical 9:16.",
        "one \"we're going\" moment from this opening.",
        "one incoming ping from this opening.",
        "one yes from this opening.",
        "one no / abort from this opening.",
        "one wait-one moment from this opening.",
    }
)


def _is_stock_beat(text: str) -> bool:
    return (text or "").strip().lower() in _STOCK_BEATS


_PACK_GRAPH_RE = re.compile(
    r"(?im)^(?:Then \w+ plays \(a cut is fine\)\.|"
    r"Transition: loops\. Last frame is reused as the start of .*|"
    r"This clip: .*)$"
)


def _drop_stock_beats(text: str) -> str:
    """Remove leftover skeleton base_prompt paragraphs (rotors, generic yes…)."""
    chunks = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    kept = [p for p in chunks if not _is_stock_beat(p)]
    return "\n\n".join(kept).strip()


def _drop_pack_graph(text: str) -> str:
    """Strip playback-pack sentences leftover in an old composed prompt."""
    chunks = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    kept_chunks: list[str] = []
    for p in chunks:
        lines = []
        for line in p.split("\n"):
            s = line.strip()
            low = s.lower()
            if _PACK_GRAPH_RE.match(s):
                continue
            if low.startswith("then ") and "plays" in low:
                continue
            if "last frame is reused as the start of" in low:
                continue
            if low.startswith("this clip:"):
                continue
            lines.append(line)
        joined = "\n".join(lines).strip()
        if joined:
            kept_chunks.append(joined)
    return "\n\n".join(kept_chunks).strip()


def _drop_tone_block(text: str) -> str:
    """Strip Tone: paragraphs so stills don't get rally register."""
    chunks = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    kept = [p for p in chunks if not p.lower().startswith("tone:")]
    kept = [p for p in kept if not p.lower().startswith("this clip:")]
    return "\n\n".join(kept).strip()


def compile_director_brief(pkg: RallyPackage, step_id: str) -> str:
    """One-line take + kit Sound: and voice tags. Does not restate the still."""
    st = pkg.step(step_id)
    if step_id == "Preview" or st.kind == StepKind.IMAGE:
        # The overlay is the request. Do not reassemble look+beat on top of
        # a user edit — that reintroduces Invent lines they deleted.
        edited = (st.composed_prompt or "").strip()
        look = (getattr(pkg, "skill_note", None) or "").strip()
        beat = (st.custom_guidance or "").strip()
        line = edited or "\n\n".join(p for p in (look, beat) if p)
        line = _drop_tone_block(line)
        line = _drop_stock_beats(line)
        if line and "No captions" not in line and "on-image text" not in line:
            line = (
                f"{line}\n\nSingle still. No captions, speech bubbles, logos, "
                "watermarks, or on-image text."
            )
        return line
    line = (st.composed_prompt or st.custom_guidance or "").strip()
    line = _drop_pack_graph(line)
    line = _drop_stock_beats(line)
    if step_id == "Idle" and not st.options.get("imagine_loop"):
        line = "\n\n".join(
            p
            for p in line.split("\n\n")
            if not p.strip().lower().startswith("loop:")
        )
    elif step_id == "Idle" and st.options.get("imagine_loop"):
        if "loop:" not in line.lower():
            line = (line + "\n\nLoop: last frame matches frame 1.").strip()
    kit = getattr(pkg, "kit", None) or {}
    sounds = [str(s).strip() for s in (kit.get("sounds") or []) if str(s).strip()]
    voices = kit.get("voices") or []
    speaker = str(st.options.get("speaker") or "").strip().lower()
    parts: list[str] = []
    if line:
        parts.append(line)
    if sounds and "Sound:" not in line:
        parts.append("Sound: " + ", ".join(sounds) + ".")
    if speaker and speaker not in {"invent", "none", "auto"}:
        idx = 0
        for i, v in enumerate(voices):
            if str(v.get("voice_id") or "").lower() == speaker:
                idx = i
                break
        tag = f"<AUDIO_{idx}>"
        if tag not in line:
            parts.append(f"Speech uses the voice from {tag}.")
    return "\n\n".join(parts).strip()

