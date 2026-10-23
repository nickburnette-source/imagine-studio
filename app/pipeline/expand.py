"""Grok pass: turn a user concept into on-theme beats for every clip.

The product is a squad invite + replies. This module translates those roles
into in-world scenes. Imagine never sees invite/app/UI copy — only the beats.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.config import OUTPUT_DIR, SKILL_PATH, TEXT_MODEL
from app.models.rally import PIPELINE_STEPS, RallyPackage
from app.pipeline.auth_token import resolve_bearer_token
from app.pipeline.prompts import has_prompt_pitfall, refresh_package_composed

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
    template: str = "character"
    concept: str = ""
    aspect_ratio: str = "9:16"
    look: str = ""
    beats: dict[str, str] = Field(default_factory=dict)
    # step_id → "preview" | "idle" | staged still description
    starts: dict[str, str] = Field(default_factory=dict)

    def apply_pack(self, pack: ConceptPack) -> None:
        self.look = (pack.look or "").strip()
        data = pack.model_dump()
        for sid, field in _STEP_FIELDS.items():
            self.beats[sid] = str(data.get(field) or "").strip()
        for sid, field in _START_FIELDS.items():
            self.starts[sid] = str(data.get(field) or "").strip()

    def apply_to_package(self, pkg: RallyPackage) -> None:
        pkg.concept = self.concept.strip()
        pkg.template = (self.template or "character").strip().lower()
        pkg.aspect_ratio = self.aspect_ratio or pkg.aspect_ratio
        pkg.skill_note = (self.look or "").strip()
        for sid, beat in self.beats.items():
            if sid in pkg.steps and beat:
                pkg.steps[sid].custom_guidance = beat
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
    look: str = Field(
        description=(
            "2–3 sentences locking the home subject, palette, lighting, and a "
            "default camera. No product, UI, or phone words."
        )
    )
    preview: str = Field(
        description="Still only: the recognition frame. Vertical, full-frame, no motion."
    )
    idle: str = Field(
        description=(
            "The rest loop while waiting. Tiny living detail only "
            "(lights, heat, breath, a breeze). This one stays put."
        )
    )
    rally_start: str = Field(
        description=(
            "Frame 1 for Rally: exactly 'preview', or a still of a different "
            "opening pose of the SAME subject (e.g. already in a low hover)."
        )
    )
    rally: str = Field(
        description=(
            "Motion from that frame 1. One action, one camera or locked, "
            "Sound: line. No people/dust that aren't in frame 1."
        )
    )
    message_start: str = Field(
        description=(
            "Frame 1 for Message: 'preview', 'idle', or a staged still "
            "(e.g. already airborne, same craft and world)."
        )
    )
    message: str = Field(
        description=(
            "Motion from that frame 1. One in-frame action + Sound:."
        )
    )
    accept_start: str = Field(
        description="Frame 1 for Accept: usually 'idle', or a staged still."
    )
    accept: str = Field(
        description="Motion from that frame 1. One commit beat + Sound:."
    )
    decline_start: str = Field(
        description=(
            "Frame 1 for Decline: 'idle', or a staged still if abort "
            "should open already in the air."
        )
    )
    decline: str = Field(
        description="Motion from that frame 1. One abort beat + Sound:."
    )
    reschedule_start: str = Field(
        description="Frame 1 for Reschedule: usually 'idle', or a staged still."
    )
    reschedule: str = Field(
        description="Motion from that frame 1. One wait-one beat + Sound:."
    )


def _kind_note(template: str) -> str:
    tid = (template or "character").strip().lower()
    if tid == "craft":
        return (
            "Kind: Craft — photoreal vehicle or machine. Preview is the rest "
            "still on its real surface. If a clip should OPEN already airborne "
            "or in another pose, put that pose in the *_start still (same "
            "craft, same world) and write motion FROM that pose. Do not ask "
            "the grounded Preview to 'already be flying'. No brownout/dust "
            "explosions on painted concrete. Sound: named mechanical cues."
        )
    if tid == "scene":
        return (
            "Kind: Scene — place, terminal, object, or tableau. No new faces. "
            "Video beats animate lights, screens, weather already in the still."
        )
    return (
        "Kind: Character — face/body readable. Video beats: one gesture or "
        "expression on the person in the still. Do not spawn extra characters."
    )


def _system_prompt() -> str:
    skill = ""
    if SKILL_PATH.is_file():
        skill = SKILL_PATH.read_text(encoding="utf-8").strip()
    return f"""You write Imagine shot briefs for a 7-clip rally pack.

Image-to-video always starts on a still (frame 1). It cannot begin "already
in the air" if frame 1 is on the ground. If a moment needs a different
opening pose, STAGE that pose as a still — then write motion from there.

Product meaning — pick the *kind* of moment, never those words in the beats:
- Preview = recognition still (the poster frame, usually rest)
- Rally = sending a squad invite → we're going
- Message = the invite arriving → attention / incoming
- Idle = waiting on replies → rest loop on the hub pose
- Accept = I'm in → commit
- Decline = can't make it → abort / stand-down
- Reschedule = not now → wait-one

For each video clip you output TWO fields: {{clip}}_start and {{clip}}.
- {{clip}}_start: exactly "preview" or "idle", OR a short still of the SAME
  subject in a different opening pose (low hover, already turning, etc.).
- {{clip}}: motion + one camera or locked + Sound: from THAT frame 1.

Creative liberties live in the staged still and the one motion beat.
Do not invent a new location, extra people, or particles that aren't in
frame 1. Dust only if that still shows loose dirt/sand — never on a clean
pad. No captions, logos, UI, phones, apps.

Preview is a still (no Sound). Idle is tiny looping life, camera locked,
usually the grounded rest.

Example (craft on a concrete night pad) — do not copy unless it fits:
- message_start: Same craft in a low hover, skids off the pad, rotors
  blurred, pad and floods still in frame below, same livery and night.
- message: The pilot's visor turns toward a strobe on the horizon.
  Camera locked. Sound: headset crackle, rotor thump, a callsign on the net.

{skill}
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
    template: str = "character",
    aspect_ratio: str = "9:16",
) -> ConceptPack:
    """One Grok call → look lock + a beat for every pipeline step."""
    concept = (concept or "").strip()
    if not concept:
        raise ValueError("Concept is empty — type what this rally is about first")

    from xai_sdk import AsyncClient
    from xai_sdk.chat import system, user

    token, _src = resolve_bearer_token()
    client = AsyncClient(api_key=token, timeout=90)
    try:
        chat = client.chat.create(model=TEXT_MODEL)
        chat.append(system(_system_prompt()))
        chat.append(
            user(
                f"{_kind_note(template)}\n"
                f"Aspect: {aspect_ratio}\n"
                f"Concept: {concept}\n\n"
                "Write the look lock, Preview, Idle, and each video clip as "
                "start still + motion brief. Stage frame 1 when the shot "
                "should open off the rest pose (hover, airborne, etc.)."
            )
        )
        _resp, pack = await chat.parse(ConceptPack)
        hits = _pack_has_pitfalls(pack)
        if hits:
            chat.append(
                user(
                    "Rewrite. These fields still sound like product/UI copy: "
                    + "; ".join(hits)
                    + ". Keep the cinematic in-world moments. No app chrome."
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
    """Fill look, motion beats, and frame-1 locks; refresh composed prompts."""
    brief = RallyBrief(
        concept=pkg.concept,
        template=getattr(pkg, "template", "character") or "character",
        aspect_ratio=pkg.aspect_ratio,
    )
    brief.apply_pack(pack)
    brief.apply_to_package(pkg)
