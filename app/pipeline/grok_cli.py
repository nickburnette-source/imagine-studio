"""
Grok CLI backend — uses your existing `grok login` session / subscription quota.

Shells out to headless `grok -p` with Imagine tools (image_gen, image_edit,
image_to_video) and asks the agent to copy results to an exact destination path.

This is the personal / single-user path. Prefer `api` when you need multi-user
billing later.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path

from app.config import (
    GROK_CLI_BIN,
    GROK_CLI_MAX_TURNS,
    GROK_CLI_TIMEOUT_IMAGE,
    GROK_CLI_TIMEOUT_VIDEO,
    VIDEO_DURATION_DEFAULT,
)
from app import imagine_settings


class GrokCliError(RuntimeError):
    """Raised when the Grok CLI invocation fails or produces no media."""

    def __init__(self, message: str, *, zdr: bool = False) -> None:
        super().__init__(message)
        self.zdr = zdr


def is_zdr_error(exc: BaseException) -> bool:
    if isinstance(exc, GrokCliError) and exc.zdr:
        return True
    text = str(exc)
    return (
        "Zero Data Retention" in text
        or "upload_url" in text
        or "ZDR" in text
    )


def which_grok() -> str | None:
    if GROK_CLI_BIN:
        p = Path(GROK_CLI_BIN).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return shutil.which("grok")


def auth_present() -> bool:
    """Best-effort: session auth lives in ~/.grok/auth.json."""
    auth = Path.home() / ".grok" / "auth.json"
    if not auth.is_file():
        return False
    try:
        return auth.stat().st_size > 10
    except OSError:
        return False


def _clamp_cli_duration(seconds: int | float | None) -> int:
    """CLI image_to_video only accepts 6 or 10."""
    d = int(seconds if seconds is not None else VIDEO_DURATION_DEFAULT)
    return 10 if d >= 8 else 6


def _cli_resolution() -> str:
    r = (imagine_settings.video_resolution() or "480p").lower()
    if r in ("1080p", "1080"):
        return "1080p"
    return "720p" if r in ("720p", "720") else "480p"


def _build_cmd(
    *,
    prompt: str,
    tools: str,
    cwd: Path,
    max_turns: int | None = None,
) -> list[str]:
    grok = which_grok()
    if not grok:
        raise GrokCliError(
            "grok CLI not found on PATH. Install Grok Build and ensure `grok` works."
        )
    return [
        grok,
        "-p",
        prompt,
        "--tools",
        tools,
        "--disallowed-tools",
        "Agent,web_search,web_fetch,spawn_subagent",
        "--always-approve",
        "--permission-mode",
        "bypassPermissions",
        "--max-turns",
        str(max_turns or GROK_CLI_MAX_TURNS),
        "--output-format",
        "json",
        "--cwd",
        str(cwd),
        "--no-plan",
        "--no-memory",
        "--no-subagents",
    ]


async def _run_grok(
    *,
    prompt: str,
    tools: str,
    cwd: Path,
    timeout: float,
    dest: Path,
    media_suffixes: tuple[str, ...],
) -> Path:
    """Run headless grok and ensure dest media file exists afterward."""
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Remove stale dest so we can detect success
    if dest.exists():
        dest.unlink()

    cwd = cwd.resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    cmd = _build_cmd(prompt=prompt, tools=tools, cwd=cwd)

    before = time.time()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=os.environ.copy(),
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise GrokCliError(
            f"grok CLI timed out after {timeout:.0f}s while generating {dest.name}"
        ) from None

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")

    # Prefer dest if the agent copied correctly
    if dest.is_file() and dest.stat().st_size > 0:
        return dest

    # Fallback: newest media under cwd created during this run
    recovered = _find_new_media(cwd, before, media_suffixes)
    if recovered is None:
        # Also scan session images/videos if mentioned in stdout
        recovered = _path_from_stdout(stdout, media_suffixes)

    if recovered and recovered.is_file() and recovered.stat().st_size > 0:
        if recovered.resolve() != dest:
            shutil.copy2(recovered, dest)
        if dest.is_file() and dest.stat().st_size > 0:
            return dest

    # Parse agent text for useful errors (e.g. ZDR video restriction)
    agent_text = _json_text(stdout) or stdout
    detail = agent_text.strip() or stderr.strip() or f"exit {proc.returncode}"
    detail = detail[:1200]
    if "Zero Data Retention" in detail or "upload_url" in detail:
        raise GrokCliError(
            "Grok CLI video blocked by Zero Data Retention (ZDR): "
            "image_to_video requires output.upload_url, which the CLI tool "
            "does not expose. Images via CLI still work.\n\n"
            f"Raw: {detail[:300]}",
            zdr=True,
        )
    raise GrokCliError(
        f"grok CLI did not produce {dest.name}.\n{detail}"
    )


def _json_text(stdout: str) -> str:
    stdout = stdout.strip()
    if not stdout:
        return ""
    try:
        data = json.loads(stdout)
        return str(data.get("text") or "")
    except json.JSONDecodeError:
        # Sometimes extra logs wrap JSON — find last object
        start = stdout.rfind("{")
        if start >= 0:
            try:
                data = json.loads(stdout[start:])
                return str(data.get("text") or "")
            except json.JSONDecodeError:
                pass
    return ""


def _find_new_media(
    root: Path, since: float, suffixes: tuple[str, ...]
) -> Path | None:
    newest: Path | None = None
    newest_mtime = since - 1
    try:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix.lower() not in suffixes:
                continue
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m >= since - 2 and m >= newest_mtime:
                newest = p
                newest_mtime = m
    except OSError:
        return None
    return newest


_PATH_RE = re.compile(
    r"(/[^\s\"']+\.(?:png|jpe?g|webp|mp4|webm|mov))",
    re.IGNORECASE,
)


def _path_from_stdout(stdout: str, suffixes: tuple[str, ...]) -> Path | None:
    text = _json_text(stdout) or stdout
    candidates = _PATH_RE.findall(text)
    for c in reversed(candidates):
        p = Path(c)
        if p.is_file() and p.suffix.lower() in suffixes:
            return p
    return None


async def generate_image(
    *,
    prompt: str,
    dest: Path,
    aspect_ratio: str = "9:16",
) -> Path:
    dest = dest.resolve()
    work = dest.parent
    ar = aspect_ratio if aspect_ratio in {"1:1", "9:16", "16:9", "3:4", "4:3"} else "9:16"
    instruction = f"""You are a media generation worker. Do exactly this:

1. Call the image_gen tool ONCE with:
   - prompt: {json.dumps(prompt)}
   - aspect_ratio: {json.dumps(ar)}
2. Immediately after, use run_terminal_command to copy the generated image file to:
   {dest}
   Example: cp "<path-from-tool>" {dest}
3. Reply with ONLY the absolute path: {dest}

Rules:
- Do not invent tools or parameters.
- Do not search the web.
- Do not spawn subagents.
- Do not write code to draw the image; you must use image_gen.
"""
    return await _run_grok(
        prompt=instruction,
        tools="image_gen,run_terminal_command",
        cwd=work,
        timeout=GROK_CLI_TIMEOUT_IMAGE,
        dest=dest,
        media_suffixes=(".png", ".jpg", ".jpeg", ".webp"),
    )


async def edit_image(
    *,
    prompt: str,
    source: Path,
    dest: Path,
    aspect_ratio: str = "9:16",
) -> Path:
    source = source.resolve()
    dest = dest.resolve()
    if not source.is_file():
        raise GrokCliError(f"Source image missing: {source}")
    work = dest.parent
    instruction = f"""You are a media generation worker. Do exactly this:

1. Call the image_edit tool ONCE with:
   - prompt: {json.dumps(prompt)}
   - image: [{json.dumps(str(source))}]
   (absolute path to the source image)
2. Immediately after, use run_terminal_command to copy the edited image to:
   {dest}
3. Reply with ONLY the absolute path: {dest}

Rules:
- Keep the same character identity and overall look; apply the prompt as directed.
- Do not search the web. Do not spawn subagents.
- You must use image_edit (not image_gen).
"""
    return await _run_grok(
        prompt=instruction,
        tools="image_edit,run_terminal_command",
        cwd=work,
        timeout=GROK_CLI_TIMEOUT_IMAGE,
        dest=dest,
        media_suffixes=(".png", ".jpg", ".jpeg", ".webp"),
    )


async def image_to_video(
    *,
    prompt: str,
    image_path: Path,
    dest: Path,
    duration: int | float | None = None,
    resolution: str | None = None,
) -> Path:
    image_path = image_path.resolve()
    dest = dest.resolve()
    if not image_path.is_file():
        raise GrokCliError(f"Keyframe image missing: {image_path}")
    work = dest.parent
    dur = _clamp_cli_duration(duration)
    res = resolution or _cli_resolution()
    if res not in ("480p", "720p"):
        res = "480p"

    instruction = f"""You are a media generation worker. Do exactly this:

1. Call the image_to_video tool ONCE with:
   - image: {json.dumps(str(image_path))}
   - prompt: {json.dumps(prompt)}
   - duration: {dur}
   - resolution_name: {json.dumps(res)}
2. Immediately after, use run_terminal_command to copy the generated video file to:
   {dest}
   Example: cp "<path-from-tool>" {dest}
3. Reply with ONLY the absolute path: {dest}

If image_to_video fails, reply with the FULL error message and do not invent a video file.

Rules:
- Do not search the web. Do not spawn subagents.
- You must use image_to_video (not invent ffmpeg animation unless the tool succeeds first).
"""
    return await _run_grok(
        prompt=instruction,
        tools="image_to_video,run_terminal_command",
        cwd=work,
        timeout=GROK_CLI_TIMEOUT_VIDEO,
        dest=dest,
        media_suffixes=(".mp4", ".webm", ".mov"),
    )
