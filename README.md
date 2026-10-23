# Imagine Studio

Local editor for one Imagine clip. You write a concept, shape the shot, cut a timeline, then play the finished take full screen and download it.

This is not a multi-step pack and it does not publish anywhere. Projects on disk still live in `rallies/` so existing folders keep working.

## Quick start

```bash
cd imagine-studio
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env   # set CONTENT_S3_BUCKET and AWS credentials for real video

python -m app.main
# or: imagine-studio
```

Open **http://127.0.0.1:8070**.

`./start.sh` runs it in the foreground. `./start.sh --bg` backgrounds it and writes `.imagine-studio.pid`. `./stop.sh` stops that process.

## What you do

1. **New project** — name, concept, and aspect (9:16 or 1:1).
2. **Editor** — one shot. Prompt rows, reference images, length, quality, and the scene timeline (cut, join, reorder, regenerate, nudge, new scene).
3. **Final** — full-screen playback of the stitched take, and a download.

There is no step stepper and no approve-to-advance gate.

## Generation backends

| Backend | How media is made | Credentials | When |
|---------|-------------------|-------------|------|
| **`cli`** (default) | Imagine REST in-process | `grok login` session (or `XAI_API_KEY` if set) | Solo / subscription |
| `api` | Same Imagine path | `XAI_API_KEY` | Metered / multi-user |
| `stub` | Pillow + ffmpeg placeholders | none | Offline UI testing |

`grok login` is only used to obtain a bearer token in `~/.grok/auth.json`. Generation does not open a Grok agent or a Terminal window.

### Video staging

Under Zero Data Retention, Imagine will not host video URLs. The app:

1. Mints an S3 presigned PUT under `CONTENT_S3_PREFIX` (default `staging/`)
2. Calls `POST /v1/videos/generations` with `output.upload_url`
3. Downloads the object into the project folder

Requires `CONTENT_S3_BUCKET` and AWS credentials that can Put/Get/Delete on that prefix. An IAM access key in `.env` is enough. See [`.env.example`](./.env.example) and [`content_store/README.md`](content_store/README.md).

## Requirements

| Tool | Why |
|------|-----|
| Python 3.11+ | App runtime |
| `ffmpeg` | Stub videos and timeline cuts |
| AWS credentials | Required for real video (staging bucket) |
| `grok` CLI + `grok login` | Required for the `cli` backend |
| `XAI_API_KEY` | Required only for the `api` backend |

Check the **Doctor** button in the header.

## Config

See [`.env.example`](./.env.example). The variable names are the ones the app already reads.

| Variable | Meaning |
|----------|---------|
| `RALLY_GENERATE_BACKEND` | `cli` (default), `api`, or `stub` |
| `XAI_API_KEY` | Only for `api`, or to override the `grok login` session |
| `CONTENT_S3_BUCKET` | Private bucket used to stage video |
| `CONTENT_S3_PREFIX` | Key prefix (default `staging/`) |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | IAM key for that bucket. Optional if `aws configure` already ran |
| `AWS_REGION` | Bucket region (default `us-east-1`) |
| `AWS_PROFILE` | Optional named profile. Not required when the access key is set |
| `RALLY_HOST` / `RALLY_PORT` | GUI bind address (default `127.0.0.1:8070`) |
| `RALLY_OUTPUT_DIR` | Project folders (default `./rallies`) |
| `RALLY_OPEN_BROWSER` | `0` skips opening a browser tab (`./start.sh`) |

Image model, video model, and resolution are stored in `rallies/.imagine-settings.json`. Doctor reports them. The header does not edit them.

## Project layout

```
app/
  main.py                 # entry
  config.py
  ui/studio_next.py       # the editor you see
  ui/scenes_ui.py         # timeline editor
  pipeline/generate_next.py
  pipeline/prompts_next.py
rallies/                  # local projects (gitignored)
```

`app.ui.studio`, `app.pipeline.generate`, `app.pipeline.prompts`, and `app.pipeline.expand` load the `*_next` modules. A fresh clone does not need a venv `.pth` file.
