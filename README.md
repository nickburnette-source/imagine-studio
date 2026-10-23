# Rally Builder

Local GUI for creating **DutyCall Rally** skins with a **human-gated pipeline**:

```
Concept → Preview (approve) → Idle still+loop (approve) → branch clips → Save (ffmpeg + S3)
```

Creative rules live in [`SKILL.md`](./SKILL.md). Product plan: [`plan.md`](./plan.md).

## Quick start

```bash
cd rally-builder
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# optional
cp .env.example .env

rally-builder
# or: python -m app.main
```

Open **http://127.0.0.1:8080**.

## Generation backends

| Backend | How media is made | Credentials | When |
|---------|-------------------|-------------|------|
| **`cli`** (default) | **Direct** Imagine SDK/REST in-process (no Grok agent, no Terminal) | `grok login` session (or `XAI_API_KEY` if set) | Solo / subscription |
| `api` | Same direct Imagine path | Prefer `XAI_API_KEY` | Metered / multi-user |
| `stub` | Pillow + ffmpeg placeholders | none | Offline UI testing |

There is **no interpretive Grok CLI session** for generate. `grok login` is only used
to obtain a bearer token stored in `~/.grok/auth.json`.

### Video + ZDR (content store)

Under Zero Data Retention, Imagine will not host video URLs. The app always:

1. Mints an S3 **presigned PUT** under `CONTENT_S3_PREFIX` (default `staging/`)
2. Calls `POST /v1/videos/generations` with `output.upload_url`
3. Downloads the object into the rally folder

Requires `CONTENT_S3_BUCKET` and AWS credentials that can Put/Get/Delete on
that prefix. An IAM access key in `.env` is enough. See
[`.env.example`](./.env.example) and [`content_store/README.md`](content_store/README.md).

### CLI backend (recommended for solo use)

```bash
# once
grok login

cp .env.example .env   # set CONTENT_S3_BUCKET and an AWS access key
source .venv/bin/activate
pip install -e .
python -m app.main
```

All generation runs **behind the scenes** in the web app process: progress banner
updates in place, then the stage refreshes with cache-busted media. No Terminal
windows.

### API backend (optional)

```bash
# .env
RALLY_GENERATE_BACKEND=api
XAI_API_KEY=xai-...
CONTENT_S3_BUCKET=…
```

| Step | Call |
|------|------|
| **Preview** | Text → image |
| **Idle** | Edit from Preview → still; then image→video (S3 staging); last frame seeds reactions |
| **Branch** | Rally/Message: image→video from Preview, ending on Idle rest. Reactions: image→video from Idle’s last frame |

## Pipeline (human mode)

| Stage | What you do |
|-------|-------------|
| **Invent scenes** | Type a concept, pick Character / Scene / Craft. Grok writes Imagine shot briefs per clip. Review and edit **before** anything generates |
| **Preview** | Edit base + custom prompts → Generate → review still → **Approve** |
| **Idle** | Lock ready pose (`IdleKeyframe.png` + `Idle.mp4`) → **Approve** |
| **Branch** | Rally / Message / Accept / Decline / Adjust — each with its own prompts |
| **Final review** | When every step is approved: fullscreen autoplay of all assets (no gaps) |
| **Publish** | After final review: compress + S3 upload as **Free** or **Premium** |

Each step has:

- **Concept** — what the rally is about; **Compose beats** asks Grok to write on-theme custom guidance for every clip (send / incoming / wait / yes / no / later as in-world action)
- **Base prompt** — short optional beat (from `defaults/templates/`, editable)
- **Custom guidance** — what happens between start and end
- **Composed prompt** — subject + look lock + START/END/TRANSITION harness + beat + custom

## Requirements

| Tool | Why |
|------|-----|
| Python 3.11+ | App runtime |
| `ffmpeg` | Stub videos + iOS compress scripts |
| `aws` CLI | Save → S3 (optional until you save) |
| `grok` CLI + `grok login` | Required for `cli` backend |
| `XAI_API_KEY` | Required only for `api` backend |

Check the **Doctor** button in the header.

## Config

See [`.env.example`](./.env.example):

| Variable | Meaning |
|----------|---------|
| `RALLY_GENERATE_BACKEND` | `cli` (default), `api`, or `stub` |
| `RALLY_GROK_BIN` | Optional path to `grok` |
| `XAI_API_KEY` | Only for `api` |
| `RALLY_IMAGE_MODEL` / `RALLY_VIDEO_MODEL` | API models |
| `RALLY_VIDEO_RESOLUTION` | `480p` or `720p` |
| `RALLY_VIDEO_DURATION` | seconds (cli maps to 6 or 10) |
| `RALLY_OUTPUT_DIR` | Working packages (default `./rallies`) |
| `CONTENT_S3_BUCKET` | Private bucket used to stage video |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | IAM key for that bucket. Optional if `aws configure` already ran |
| `AWS_REGION` | Bucket region (default `us-east-1`) |
| `RALLY_HOST` / `RALLY_PORT` | GUI bind address |

## Project layout

```
app/
  main.py              # entry
  config.py
  models/rally.py      # meta.json package model
  pipeline/            # gates, prompts, generate, compress, upload, doctor
  ui/studio.py         # NiceGUI studio
defaults/step_prompts.yaml
cmprssRally.sh         # existing iOS compressor
SKILL.md               # creative director rules
rallies/               # local packages (gitignored)
```

## Status

- [x] Phase 0–1: gated UI, prompts, stub generate, approve chain, compress/upload wiring  
- [x] Phase 2: Grok Imagine **API** (`api`) and **CLI subscription** (`cli`, default)  
- [ ] Phase 4: auto mode (same graph, skip human gates)  
