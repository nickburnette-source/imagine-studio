# Imagine

A local studio for Grok Imagine. One project is a prompt, look references, and a gallery of images and videos.

This repo started as a copy of Rally Builder. The rally pack is gone: no Preview / Idle / Rally / Message / Accept / Decline / Reschedule steps, no approval gates, no final review, and no DutyCall publish. The clip timeline still lives in `/Users/nick/Documents/clip-editor`.

## What you can do

- Create a project and write a prompt
- Generate an image, or edit the image on stage
- Attach up to seven reference images (look, not a pose lock)
- Generate a video from text, from those references, or by animating the still on stage
- Edit the video on stage (Classic `grok-imagine-video`; clips longer than 8.7s are trimmed for the request)
- Pick length (1–15 seconds, or 1–10 when Classic is using references) and 480p / 720p / 1080p when the shot allows it
- Keep every take. Click one to put it back on stage

There is no negative prompt and no per-object motion lock. The prompt is what should happen.

## Run

ffmpeg and ffprobe have to be on your PATH. Real video also needs `XAI_API_KEY` or `grok login`, plus `CONTENT_S3_BUCKET` for staging. See `.env.example` and `content_store/README.md`.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
./start.sh
```

Then open [http://127.0.0.1:8070](http://127.0.0.1:8070). The app does not open a browser unless `IMAGINE_OPEN_BROWSER=1`.

`./start.sh --bg` runs in the background. `./stop.sh` stops it.

```bash
IMAGINE_GENERATE_BACKEND=stub
```

`stub` writes a local placeholder and does not call Imagine. The default, `cli`, does call Imagine and spends credit.

Projects are folders under `projects/` and are not committed.
