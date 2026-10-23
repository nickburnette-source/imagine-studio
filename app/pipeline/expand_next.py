"""Grok pass: invent a scene for every rally clip from a concept + tone.

The product is a squad ping and the replies. This module translates those
roles into in-world scenes. Imagine never sees invite/app/UI copy.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.config import OUTPUT_DIR, TEXT_MODEL
from app.models.rally import RallyPackage
from app.pipeline.auth_token import resolve_bearer_token
from app.pipeline.prompts import (
    STEP_ROLES,
    has_prompt_pitfall,
    refresh_package_composed,
)

_STEP_FIELDS = {
    "Preview": "preview",
    "Idle": "idle",
    "Rally": "rally",
    "Message": "message",
    "Accept": "accept",
    "Decline": "decline",
    "Adjust": "reschedule",
}

# Frame-1 lock vs staged still. Grok writes "preview" / "idle" or a still.
_START_FIELDS = {
    "Rally": "rally_start",
    "Message": "message_start",
    "Accept": "accept_start",
    "Decline": "decline_start",
    "Adjust": "reschedule_start",
}

_LOCK_TOKENS = {
    "preview": "preview",
    "idle": "idle",
    "idle rest": "idle",
    "idle_last": "idle_last",
    "idle last": "idle_last",
    "idle last frame": "idle_last",
}

def parse_start_lock(raw: str, step_id: str) -> tuple[str, str]:
    """Return (start_mode, staged_still_prompt)."""
    t = (raw or "").strip()
    low = " ".join(t.lower().split())
    if not t:
        if step_id in ("Rally", "Message"):
            return "preview", ""
        if step_id in ("Accept", "Decline", "Adjust"):
            return "idle_last", ""
        return "preview", ""
    if low in _LOCK_TOKENS:
        return _LOCK_TOKENS[low], ""
    first = low.split()[0].rstrip(".,:")
    if first in _LOCK_TOKENS and len(t) < 28:
        return _LOCK_TOKENS[first], ""
    return "staged", t


class RallyBrief(BaseModel):
    """Pre-pipeline draft: concept + invented beats, before any generate."""

    name: str = ""
    display_name: str = ""
    template: str = "rally"
    concept: str = ""
    aspect_ratio: str = "9:16"
    tone: str = ""
    look: str = ""
    beats: dict[str, str] = Field(default_factory=dict)
    # step_id → "preview" | "idle" | staged still description
    starts: dict[str, str] = Field(default_factory=dict)

    def apply_pack(self, pack: ConceptPack) -> None:
        self.tone = (pack.tone or "").strip()
        self.look = (pack.look or "").strip()
        data = pack.model_dump()
        for sid, field in _STEP_FIELDS.items():
            self.beats[sid] = str(data.get(field) or "").strip()
        for sid, field in _START_FIELDS.items():
            self.starts[sid] = str(data.get(field) or "").strip()

    def to_pack(self) -> ConceptPack:
        data = {field: self.beats.get(sid, "") for sid, field in _STEP_FIELDS.items()}
        data.update(
            {
                field: self.starts.get(sid, "")
                for sid, field in _START_FIELDS.items()
            }
        )
        return ConceptPack(
            tone=self.tone or "",
            look=self.look or "",
            **data,
        )

    def apply_to_package(self, pkg: RallyPackage) -> None:
        pkg.concept = self.concept.strip()
        pkg.template = (self.template or "rally").strip().lower()
        pkg.aspect_ratio = self.aspect_ratio or pkg.aspect_ratio
        pkg.skill_note = (self.look or "").strip()
        tone = (self.tone or "").strip()
        for sid, beat in self.beats.items():
            if sid in pkg.steps and beat:
                pkg.steps[sid].custom_guidance = beat
        for sid, st in pkg.steps.items():
            # Tone is motion register. Preview is a photograph — never attach it.
            if sid == "Preview":
                st.options.pop("tone", None)
            elif tone:
                st.options["tone"] = tone
            else:
                st.options.pop("tone", None)
        for sid in _START_FIELDS:
            if sid not in pkg.steps:
                continue
            mode, prompt = parse_start_lock(self.starts.get(sid, ""), sid)
            pkg.steps[sid].options["start_mode"] = mode
            pkg.steps[sid].options["start_still_prompt"] = prompt
        refresh_package_composed(pkg)


class BriefStore(BaseModel):
    """On-disk draft so Invent scenes survives reload / restart."""

    open: bool = True
    brief: RallyBrief


BRIEF_STORE_PATH = OUTPUT_DIR / ".brief.json"


def persist_brief(brief: RallyBrief, *, open: bool) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = BriefStore(open=open, brief=brief).model_dump_json(indent=2)
    tmp = BRIEF_STORE_PATH.with_suffix(".json.tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    tmp.replace(BRIEF_STORE_PATH)


def load_brief_store() -> BriefStore | None:
    if not BRIEF_STORE_PATH.is_file():
        return None
    try:
        return BriefStore.model_validate_json(
            BRIEF_STORE_PATH.read_text(encoding="utf-8")
        )
    except Exception:  # noqa: BLE001
        return None


def clear_brief_store() -> None:
    BRIEF_STORE_PATH.unlink(missing_ok=True)
    tmp = BRIEF_STORE_PATH.with_suffix(".json.tmp")
    tmp.unlink(missing_ok=True)


class ConceptPack(BaseModel):
    tone: str = Field(
        description=(
            "Register for motion and speech only: how serious, funny, or mean, "
            "and who it is aimed at. Do not name pipeline steps (no Preview, "
            "Rally, Accept, Decline, Idle). Never mention apps, invites, UI, "
            "phones, or skins."
        )
    )
    look: str = Field(
        description=(
            "Photograph of the subject: who or what, the world, the light, "
            "the material. Visual only. No speech, no captions, no Accept or "
            "Decline, no product verbs. This is the still bible."
        )
    )
    preview: str = Field(
        description=(
            "Recognition still only. Full-frame, vertical, no motion, no Sound, "
            "no words in the frame, no speech bubbles, no Accept/Decline. "
            "The poster of this world at rest."
        )
    )
    idle: str = Field(
        description=(
            "Waiting loop. Tiny living detail only, camera locked, same pose "
            "it can return to. Sound: line. This is the hub while nobody has "
            "answered."
        )
    )
    rally_start: str = Field(
        description=(
            "Opening still for Rally (the send). Write preview, idle, or a "
            "still of a different opening of the same subject in the same "
            "world — wherever 'we're going' actually begins."
        )
    )
    rally: str = Field(
        description=(
            "The send: the squad is being called. One action from "
            "rally_start, one camera or locked, Sound: line. In-world speech "
            "is fine."
        )
    )
    message_start: str = Field(
        description=(
            "Opening still for Message (the ping arriving). preview, idle, or "
            "a still of a different opening of the same subject."
        )
    )
    message: str = Field(
        description=(
            "Incoming: attention in-world. One action from message_start, one "
            "camera or locked, Sound: line."
        )
    )
    accept_start: str = Field(
        description=(
            "Opening still for Accept (yes, I'm in). idle, preview, or a "
            "still of a different opening of the same subject."
        )
    )
    accept: str = Field(
        description=(
            "Yes: commit to what Rally started. One action from accept_start, "
            "one camera or locked, Sound: line. Match the tone."
        )
    )
    decline_start: str = Field(
        description=(
            "Opening still for Decline (no). idle, preview, or a still of a "
            "different opening — including already somewhere else in the "
            "action if that's where the no happens."
        )
    )
    decline: str = Field(
        description=(
            "No: abort, stand-down, walk away, or roast the squadmate. Tone "
            "lives here. One action from decline_start, one camera or locked, "
            "Sound: line."
        )
    )
    reschedule_start: str = Field(
        description=(
            "Opening still for Reschedule (not now). idle, preview, or a "
            "still of a different opening of the same subject."
        )
    )
    reschedule: str = Field(
        description=(
            "Wait-one: hold the plan without a yes or a no. One action from "
            "reschedule_start, one camera or locked, Sound: line."
        )
    )


def _system_prompt() -> str:
    roles = "\n".join(f"- {sid}: {blurb}" for sid, blurb in STEP_ROLES.items())
    return f"""You invent the seven clips of a rally pack from a concept.

A rally is how a squad pings each other. You write in-world scenes that mean those moments. Never mention the product, the app, invites, UI, phones, or skins.

Split the work:
- `look` and `preview` are PHOTOGRAPHS. Subject, material, world, light. No speech, no captions, no on-image text, no pipeline step names (do not write Accept, Decline, Rally, Preview, Idle).
- `tone` is register for MOTION and SPEECH only. How serious, funny, mean. Do not name pipeline steps in `tone`. A roast belongs in the Decline beat as in-world speech, not as a label on the poster.
- Motion beats (idle, rally, message, accept, decline, reschedule) carry action, camera, Sound, and any in-world line.

What each step does (for you — do not paste these labels into look or preview):
{roles}

Bake the visual approach into look/preview (person, vehicle, place, object). Do not classify a kind.

Idle is usually the rest pose. Other clips do not have to start there. Invent the opening that makes the beat true. When the opening is not Preview or Idle rest, describe that still in the matching *_start field (same subject, same world). Motion fields describe only what happens FROM that opening.

How to write:
- Preview: still only. No motion, no Sound, no words in the frame.
- Idle: tiny loopable life + Sound. Camera locked.
- Each story clip: *_start is the word preview, the word idle, or a still of the invented opening; the motion field is one action, one camera or locked, and a Sound: line.
- Short in-world speech is fine on motion clips. Captions, logos, UI, speech bubbles are not.
- Do not give examples. Do not reuse a stock beat. Invent from this concept.
"""


def _pack_has_pitfalls(pack: ConceptPack) -> list[str]:
    return [
        name
        for name, value in pack.model_dump().items()
        if has_prompt_pitfall(value or "")
    ]


async def expand_concept(
    concept: str,
    *,
    template: str = "rally",
    aspect_ratio: str = "9:16",
    notes: str = "",
    previous: ConceptPack | None = None,
) -> ConceptPack:
    """One Grok call → tone + look + a beat for every pipeline step."""
    _ = template
    concept = (concept or "").strip()
    if not concept:
        raise ValueError("Concept is empty — type what this rally is about first")
    notes = (notes or "").strip()

    from xai_sdk import AsyncClient
    from xai_sdk.chat import system, user

    token, _src = resolve_bearer_token()
    client = AsyncClient(api_key=token, timeout=120)
    try:
        chat = client.chat.create(model=TEXT_MODEL)
        chat.append(system(_system_prompt()))
        if previous is not None and notes:
            chat.append(
                user(
                    f"Aspect: {aspect_ratio}\n"
                    f"Concept: {concept}\n\n"
                    "Here is the current draft. Keep what still works. "
                    "Revise only according to the notes. Same constraints: "
                    "look/preview are photographs with no on-image text; "
                    "tone is motion/speech only and must not name pipeline steps.\n\n"
                    f"Current draft (JSON):\n{previous.model_dump_json(indent=2)}\n\n"
                    f"Notes from the director:\n{notes}"
                )
            )
        else:
            extra = ""
            if notes:
                extra = f"\n\nAdditional direction:\n{notes}"
            chat.append(
                user(
                    f"Aspect: {aspect_ratio}\n"
                    f"Concept: {concept}\n\n"
                    "Infer tone for motion/speech only — do not put Accept/"
                    "Decline into look or preview. Preview is a still with "
                    "no on-image text. Invent all seven clips. Open each "
                    "story clip wherever the beat needs to open."
                    + extra
                )
            )
        _resp, pack = await chat.parse(ConceptPack)
        hits = _pack_has_pitfalls(pack)
        if hits:
            chat.append(
                user(
                    "Rewrite. These fields still sound like product/UI copy: "
                    + "; ".join(hits)
                    + ". Keep the in-world moments and the tone. No app chrome."
                )
            )
            _resp, pack = await chat.parse(ConceptPack)
        return pack
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result


def apply_concept_pack(pkg: RallyPackage, pack: ConceptPack) -> None:
    """Fill tone, look, motion beats, and openings; refresh composed prompts."""
    brief = RallyBrief(
        concept=pkg.concept,
        template=getattr(pkg, "template", "rally") or "rally",
        aspect_ratio=pkg.aspect_ratio,
    )
    brief.apply_pack(pack)
    brief.apply_to_package(pkg)
