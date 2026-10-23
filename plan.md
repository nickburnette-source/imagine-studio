# Archived pipeline notes

> Not the Imagine Studio product. Imagine Studio is one clip: an editor, a timeline, and a final take you can play and download. See [README.md](./README.md). The sections below are history from an older multi-step pack. They are not how this repo works.

A **local desktop GUI** that runs a **gated generation pipeline** for DutyCall Rally skins: human-approved steps by default, with a future **auto mode** once one-shot quality is good enough.

Creative rules for *what* a Rally is live in [`SKILL.md`](./SKILL.md). That skill will expand before production use; the app loads it at runtime as the creative source of truth.

---

## Product framing

### Pipeline with human intervention (default)

This is not “fire all seven assets and hope.” It is a **pipeline**: each stage produces a gateable artifact. A human reviews, edits the step’s prompt, regenerates if needed, then **approves** to unlock the next stage.

```
Concept
   │
   ▼
┌──────────────┐     approve      ┌─────────────────────┐     approve
│  Preview.png │ ───────────────► │  Idle (png + mp4)   │ ───────────────┐
│  character   │   (or regen)     │  locked ready pose  │   (or regen)   │
│  look lock   │                  │  + strongest loop   │                │
└──────────────┘                  └─────────────────────┘                │
                                                                         ▼
                                                    ┌────────────────────────────────┐
                                                    │  Branch: remaining phase files │
                                                    │  Rally · Message · Accept ·    │
                                                    │  Decline · Adjust              │
                                                    │  (each: prompt → gen → review) │
                                                    └────────────────────────────────┘
                                                                         │
                                                                         ▼
                                                              Save → ffmpeg → S3
```

**Why this order**

| Stage | Why first |
|-------|-----------|
| **Preview** | Locks the face / character look cheaply (still only). Bad character = everything else is waste. |
| **Idle** | Locks the neutral ready pose + the loop that everything else must match. Produces still (keyframe) + `Idle.mp4`. |
| **Branch** | Only after look + pose are good: phase clips that share the Idle keyframe. |

Early on, quality will **not** one-shot. The UI is built for intervention. Later, the same graph can run with gates set to auto-approve.

### Auto mode (later, not v1 priority)

Once prompts, skill, and models reliably produce high-quality rallies:

- Toggle: **Human gates** (default) vs **Auto**
- Auto runs Preview → Idle → all branch steps without waiting
- Still keeps per-step prompts + history so a human can open a finished rally and reprompt weak steps
- Optional: auto-save to S3 when the full package is ready

The pipeline graph stays the same; only the “wait for approve” policy changes.

---

## What a Rally is (from SKILL.md)

A Rally is a short, high-energy call-to-action skin: themed looping videos + one static preview for DutyCall.

### Fixed package (deliverable)

| File | Role |
|------|------|
| `Preview.png` | Static recognition image (face of the rally) |
| `Rally.mp4` | “Send” animation |
| `Message.mp4` | Incoming notification |
| `Idle.mp4` | Looping wait state (strongest loop) |
| `Accept.mp4` | “I’m in!” |
| `Decline.mp4` | “Can’t make it” |
| `Adjust.mp4` | Reschedule / thinking-about-time |

**Working artifacts** (pipeline, may ship or fold into Preview):

| File | Role |
|------|------|
| `IdleKeyframe.png` / idle still | Locked neutral pose used as start frame for all videos |
| `CharacterBible.md` | Optional text expansion from concept + skill (editable) |

Constraints from the skill (defaults the app applies when composing final prompts):

- Same character, lighting, camera, palette across clips
- Videos start and end in the **exact same ready pose** (loop rule)
- Short clips (≈5–8s; Idle can be longer)
- **9:16** or **1:1**, phone-readable
- Fun, energetic, game-friendly

---

## Per-step prompts (core UX concept)

Every pipeline step has:

1. **Base prompt** — vague, role-shaped default (editable template from skill / app defaults)
2. **Custom guidance** — user-filled field in the UI (voice line, pose, mood, prop, timing, etc.)
3. **Composed prompt** — what actually goes to Imagine (base + concept/bible + custom + system rules like loop rule)

The user always sees and can edit **base** and **custom** (and usually the composed preview) **before** generating that step.

### Default base prompts (starting point — refine with skill)

These are intentionally **vague** so the character concept and custom field do the heavy lifting:

| Step | Base prompt intent (vague) |
|------|----------------------------|
| **Preview** | Clear character portrait / recognition still; readable face; theme readable on a phone |
| **Idle** (still + video) | Neutral ready pose; subtle loopable life (breath, blink, idle energy); strongest seamless loop |
| **Rally** | Energetic “sending” / fire-off moment; celebratory call-to-action energy |
| **Message** | Incoming alert / “you’ve been rallied” reaction; attention-grabbing but on-brand |
| **Accept** | Happy yes / “I’m in!” reaction |
| **Decline** | Polite no / can’t make it |
| **Adjust** | Thinking / reschedule / “how about another time?” |

### Custom prompt per step (UI fields)

Each step panel includes a **Custom guidance** text area. Examples of what users type:

- Voice / lip vibe: `says "Let's go!" with a big grin` (if model audio/lips support it)
- Pose: `points at viewer with both hands`
- Prop: `holds a glowing game controller`
- Intensity: `subtle` vs `big cartoon squash and stretch`
- Timing: `slow nod then freeze back to ready pose`

Custom is **optional**. Empty custom → run with base + concept/bible + skill rules only.

### Prompt composition (implementation sketch)

```
composed = join(
  skill_system_rules,          # from SKILL.md
  character_bible_or_concept,
  step.base_prompt,
  step.custom_guidance,        # if non-empty
  step.fixed_suffix,           # e.g. loop rule for videos
)
```

Store all three layers in `meta.json` so regenerations are explainable and editable.

---

## Pipeline stages (detailed)

### Stage 0 — New rally

- Rally name / slug
- **Concept** (global): “happy neon robot”, “cozy cat gamer”, …
- Aspect ratio, default duration
- Mode: **Human gates** | **Auto** (auto can be stubbed until later)
- Optional: expand Character Bible from concept + skill (text), editable before Stage 1

### Stage 1 — Preview (gate)

1. Show Preview step UI: base prompt + custom guidance + Generate
2. Generate `Preview.png` (text-to-image)
3. User reviews image
4. Actions:
   - **Regenerate** (edit prompts first)
   - **Approve Preview → continue to Idle**
5. Until approved, Idle and branch steps stay locked / disabled

### Stage 2 — Idle (gate)

Idle establishes the **pose lock** for the whole rally.

1. Show Idle step UI: base + custom (pose / subtle motion / voice vibe)
2. Generate:
   - Still: `IdleKeyframe.png` (may derive from Preview or a dedicated still gen guided by Preview look — TBD; see open questions)
   - Video: `Idle.mp4` via image-to-video from that still + loop rule
3. User reviews still + looping video
4. Actions:
   - Regen still only / video only / both
   - **Approve Idle → unlock branch**
5. Approving Idle **locks** the keyframe used for all later videos (changing it later invalidates branch assets — warn)

### Stage 3 — Branch (per-file gates or batch-with-review)

After Idle is approved, create remaining files. Each has its own base + custom prompts:

- `Rally.mp4`
- `Message.mp4`
- `Accept.mp4`
- `Decline.mp4`
- `Adjust.mp4`

**v1 human mode (recommended):**

- Show all branch steps as cards; user can fill customs, then generate **one at a time** or **all remaining**
- Each finishes into reviewable state; user can reprompt any single file without redoing others
- No hard gate between branch files (order free), but none start until Idle is approved

**Auto mode (later):**

- Generate all branch files in parallel (rate-limited) after Idle auto-approves

### Stage 4 — Save

When package is complete (or force-save with warnings):

1. Validate expected files present
2. `cmprssRally.sh` → `"<name> (iOS Compressed)"`
3. `aws s3 sync` to configured bucket/prefix
4. Record receipt in `meta.json`

---

## User interface

### Layout (pipeline studio)

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Rally: neon-robot    Concept: happy neon robot    [Human ▾]  [Save]     │
├────────────┬─────────────────────────────────────────────────────────────┤
│  PIPELINE  │  CURRENT STEP                                              │
│            │                                                             │
│  ● Preview │  [ large image / looping video player ]                     │
│    ✓ ready │                                                             │
│            │  Base prompt                                                │
│  ◐ Idle    │  ┌─────────────────────────────────────────────────────┐   │
│    running │  │ (vague default — editable)                          │   │
│            │  └─────────────────────────────────────────────────────┘   │
│  ○ Rally   │                                                             │
│  ○ Message │  Custom guidance (pose, voice line, mood…)                  │
│  ○ Accept  │  ┌─────────────────────────────────────────────────────┐   │
│  ○ Decline │  │ user text                                           │   │
│  ○ Adjust  │  └─────────────────────────────────────────────────────┘   │
│            │                                                             │
│            │  [Regenerate]   [Approve & continue →]                      │
│            │                                                             │
│            │  History: take 1 · take 2 · …                               │
└────────────┴─────────────────────────────────────────────────────────────┘
```

### UI requirements (must-have)

| Area | Behavior |
|------|----------|
| **Pipeline sidebar** | Ordered stages; status: locked / pending / running / needs review / approved / error |
| **Per-step base prompt** | Prefilled, fully editable |
| **Per-step custom prompt** | Empty by default; multi-line; placeholder examples per step |
| **Generate / Regenerate** | Runs only the current (or selected) step |
| **Approve & continue** | Advances gate (Preview → Idle → Branch unlock) |
| **Media viewer** | Image for Preview/still; looping video for mp4s |
| **Global concept** | Always visible; changing it does not auto-regen (user must regen steps) |
| **Save** | Compress + upload when ready |

### Branch section

Once Idle is approved, branch steps appear enabled. Selecting a branch step shows the **same** base + custom + regen controls. Optional **Generate all remaining** with a progress list (still reviewable afterward).

---

## What already exists

| Asset | Role |
|-------|------|
| `SKILL.md` | Creative director rules; expand before production |
| `cmprssRally.sh` | Compress one rally folder (H.264 + AAC + faststart) |
| `cmprssRally-iOS.sh` | Same encode; batch subdirs |

Save wraps these; do not reimplement ffmpeg in v1.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Local GUI                                                  │
│  pipeline gates · per-step prompts · preview · save         │
└────────────────────────────┬────────────────────────────────┘
                             │
         ┌───────────────────┼───────────────────┐
         ▼                   ▼                   ▼
   Imagine (API)        ffmpeg scripts        aws cli
   text→image           cmprssRally.sh        s3 sync
   image→video
         │
         ▼
   rallies/<slug>/
     Preview.png
     IdleKeyframe.png
     Idle.mp4
     Rally.mp4 …
     meta.json          # stages, prompts, history, approvals
```

### Project layout (proposed)

```
imagine-studio/
  plan.md
  SKILL.md
  cmprssRally.sh
  cmprssRally-iOS.sh
  app/
    main.py
    ui/                 # pipeline sidebar, step editor, player
    pipeline/
      graph.py          # stages, gates, auto vs human policy
      prompts.py        # base defaults, compose(base, custom, skill)
      imagine.py
      compress.py
      upload.py
    models/
      rally.py          # RallyPackage, StepState, PromptLayers
  defaults/
    step_prompts.yaml   # vague base prompts per step (editable)
  rallies/              # gitignored
```

### Stack

| Layer | Choice |
|-------|--------|
| GUI | Python + **NiceGUI** or **Flet** (spike in Phase 0) |
| Backend | Python 3.11+, Imagine via `XAI_API_KEY` |
| State | Per-rally folder + `meta.json` |

### Imagine usage

| Step | API shape |
|------|-----------|
| Preview | Text → image |
| Idle still | Text → image (and/or image-edit from Preview for consistency) |
| Idle + branch videos | Image-to-video from **approved Idle keyframe** + composed motion prompt + loop suffix |

---

## Data model

```
rallies/neon-robot/
  Preview.png
  IdleKeyframe.png
  Idle.mp4
  Rally.mp4
  Message.mp4
  Accept.mp4
  Decline.mp4
  Adjust.mp4
  CharacterBible.md      # optional
  meta.json
  history/
    Preview.1.png
    Idle.1.mp4
    …
```

`meta.json` sketch:

```json
{
  "name": "neon-robot",
  "concept": "happy neon robot",
  "mode": "human",
  "aspect_ratio": "9:16",
  "gates": {
    "preview": "approved",
    "idle": "needs_review",
    "branch": "locked"
  },
  "steps": {
    "Preview": {
      "status": "approved",
      "base_prompt": "Clear character recognition portrait…",
      "custom_guidance": "big friendly smile, neon rim light",
      "composed_prompt": "…",
      "output": "Preview.png",
      "history": []
    },
    "Idle": {
      "status": "needs_review",
      "base_prompt": "Neutral ready pose, subtle loopable life…",
      "custom_guidance": "soft bounce on heels, blinks once",
      "outputs": { "still": "IdleKeyframe.png", "video": "Idle.mp4" },
      "history": []
    },
    "Rally": {
      "status": "locked",
      "base_prompt": "Energetic send / call-to-action moment…",
      "custom_guidance": "",
      "output": "Rally.mp4"
    }
  }
}
```

---

## Config & secrets

```bash
XAI_API_KEY=xai-...
RALLY_S3_BUCKET=
RALLY_S3_PREFIX=rallies/
AWS_PROFILE=default
AWS_REGION=us-east-1
RALLY_OUTPUT_DIR=./rallies
```

Doctor panel: ffmpeg, aws identity, API key, writable output dir.

---

## Implementation phases

### Phase 0 — Foundations
- [ ] README, `.gitignore`, `.env.example`
- [ ] Python app shell + pipeline sidebar UI (no API)
- [ ] `step_prompts.yaml` vague defaults
- [ ] `meta.json` load/save, gate state machine (locked / ready / running / needs_review / approved)
- [ ] Doctor checks

### Phase 1 — Human pipeline UX (stubs)
- [ ] Per-step **base** + **custom** editors
- [ ] Fake generate → placeholder media; Approve unlocks next stage
- [ ] Preview → Idle → branch enablement proven in UI
- [ ] History list UI (even if stub takes)

### Phase 2 — Real Imagine on the gated path
- [ ] Stage 1: Preview image gen + regen
- [ ] Stage 2: Idle still + Idle video from keyframe + loop rule
- [ ] Stage 3: Branch videos from locked keyframe; per-step regen
- [ ] Compose prompts from skill + base + custom
- [ ] Persist history takes on disk

### Phase 3 — Save
- [ ] Validate package
- [ ] `cmprssRally.sh` + `aws s3 sync`
- [ ] Upload receipt in `meta.json`

### Phase 4 — Auto mode + quality
- [ ] Auto policy: skip approve waits, same graph
- [ ] Generate-all-remaining with concurrency limits
- [ ] Prompt/skill tuning as `SKILL.md` expands
- [ ] Compare takes, sequence playback polish

---

## Non-goals (v1)

- Auto mode as the default (build human gates first)
- Generating all seven assets in one unattended shot as the primary UX
- Hosted multi-user product
- Replacing ffmpeg scripts
- Pixel-perfect DutyCall client chrome

---

## Risks & open questions

| Topic | Notes |
|-------|--------|
| Preview → Idle consistency | Does Idle still re-gen from text only, or **image-edit / i2v from Preview** so the face matches? Prefer image-conditioned Idle when API allows. |
| Preview vs Idle still | Skill lists both; may keep distinct files or set Preview as the marketing crop of the keyframe after Idle exists |
| When to write Character Bible | Before Preview, or only after Preview approve? Suggest: optional before Preview, refine after |
| Branch parallelism | Human mode: user-driven. Auto: parallel with rate limits |
| Voice lines | Custom field may request speech; depends on Imagine audio support — treat as guidance, not guarantee |
| Invalidating gates | Re-approving Preview after Idle exists should warn / require re-approval of Idle and clear branch |
| Skill evolution | Base prompts in `defaults/step_prompts.yaml` + `SKILL.md` suffixes; both editable without code deploys |

---

## Success criteria

1. User cannot waste a full six-video run on a bad character: **Preview must be approved first**.
2. User cannot branch until **Idle still + loop** look right.
3. Every step shows **base + custom** prompts; regen uses the edited values.
4. Human mode feels like a pipeline with checkpoints; auto mode can later reuse the same steps.
5. Save produces compressed assets on S3 via existing scripts + aws cli.

---

## Immediate next step

1. Spike GUI toolkit (NiceGUI vs Flet) with: sidebar pipeline, two text areas per step, image/video player, approve button.
2. Implement gate state machine + `meta.json` with stub generators.
3. Wire Preview (real Imagine) as the first paid integration.

---

## References

- [`SKILL.md`](./SKILL.md) — Rally definition & creative process
- `cmprssRally.sh`, `cmprssRally-iOS.sh` — iOS package
- Imagine API — image + image-to-video (`XAI_API_KEY`)
- AWS CLI — `s3 sync` on Save
