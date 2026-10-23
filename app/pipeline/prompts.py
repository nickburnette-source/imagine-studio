"""Load prompt templates and compose Imagine prompts.

The harness describes clip connections (starts / ends / cuts), not product
copy. Content lives in the concept, the provided still, and custom guidance.
"""

from __future__ import annotations

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
DEFAULT_TEMPLATE_ID = "character"

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
    """Return [{id, label, description}, …] sorted by label."""
    found: list[dict[str, str]] = []
    if not TEMPLATES_DIR.is_dir():
        return [
            {
                "id": DEFAULT_TEMPLATE_ID,
                "label": "Character",
                "description": "Default character template",
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
                "label": "Character",
                "description": "",
            }
        )
    return found


def template_path(template_id: str) -> Path:
    tid = (template_id or DEFAULT_TEMPLATE_ID).strip().lower()
    p = TEMPLATES_DIR / f"{tid}.yaml"
    if p.is_file():
        return p
    # Fallbacks
    for candidate in (
        TEMPLATES_DIR / f"{DEFAULT_TEMPLATE_ID}.yaml",
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
    return str(
        load_template(template_id).get("concept_label", "Concept")
    ).strip()


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
    if step_id == "Adjust":
        return "Reschedule.start.png"
    return f"{step_id}.start.png"


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


def video_start_filename(
    step_id: str,
    start_mode: str,
    *,
    idle_last_exists: bool = False,
) -> str:
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
        return "Look lock still — later clips start from this"
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
    no_spawn = (
        "Animate only named motion. Do not add people, objects, weather, "
        "or particles that are not already in the still."
    )
    if start == "preview":
        return (
            f"Starts: the provided still is frame 1 (Preview). {no_spawn}"
        )
    if start == "idle":
        return (
            f"Starts: the provided still is frame 1 (Idle rest). Hold {lock}. "
            "Camera locked. Tiny life only."
        )
    if start == "idle_last":
        return (
            f"Starts: the provided still is the last frame of Idle. {no_spawn}"
        )
    if start == "staged":
        return (
            "Starts: the provided still is the staged opening pose (frame 1). "
            + no_spawn
        )
    return f"Starts: the provided still is frame 1. Hold {lock}."


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
    if SKILL_PATH.is_file():
        return SKILL_PATH.read_text(encoding="utf-8").strip()
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
        status = StepStatus.READY if step_id == "Preview" else StepStatus.LOCKED
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
        else:
            role = str(role_raw or "")
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
) -> str:
    """Build the prompt string that will be sent to Imagine.

    Stills: subject + look lock + still brief.
    Video: subject + start/end harness + shot brief. The still already
    carries look — do not re-describe composition.
    """
    parts: list[str] = []
    clabel = concept_label(template_id)
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
        parts.append(f"{clabel}: {concept}")

    bible = character_bible.strip()
    if bible and not is_motion:
        parts.append(bible)

    if step_id:
        harness = connection_harness(
            step_id,
            kind=kind,
            template_id=template_id,
            start_mode=start_mode,
        )
        if harness:
            parts.append(harness)

    custom = custom_guidance.strip()
    if custom:
        parts.append(custom)

    base = _beat_without_pitfalls(base_prompt, step_id, template_id)
    stock = default_base_prompt(step_id, template_id)
    if base and base not in custom:
        if custom and base == stock:
            pass
        else:
            parts.append(base)

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
    clabel = concept_label(template_id)
    if concept.strip():
        parts.append(f"{clabel}: {concept.strip()}")
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
        f"The provided image is the look lock. Keep {_lock_phrase(template_id)}. "
        "Still only."
    )
    if custom_guidance.strip():
        parts.append(custom_guidance.strip())
    return "\n\n".join(parts).strip()


def compile_director_brief(pkg, step_id: str) -> str:
    st = pkg.step(step_id)
    return (st.composed_prompt or st.custom_guidance or "").strip()


def compose_staged_still_prompt(
    *,
    concept: str,
    start_still_prompt: str,
    character_bible: str = "",
    template_id: str = DEFAULT_TEMPLATE_ID,
) -> str:
    """Image-edit Preview into a new frame-1 pose for a video clip."""
    parts: list[str] = []
    clabel = concept_label(template_id)
    if concept.strip():
        parts.append(f"{clabel}: {concept.strip()}")
    if character_bible.strip():
        parts.append(character_bible.strip())
    parts.append(
        f"The provided image is the look lock. Keep {_lock_phrase(template_id)}. "
        "Same livery and world. Still only."
    )
    stage = (start_still_prompt or "").strip()
    if stage:
        parts.append("Stage this as frame 1:\n" + stage)
    parts.append("Single still. No motion blur captions or logos.")
    return "\n\n".join(parts).strip()


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
