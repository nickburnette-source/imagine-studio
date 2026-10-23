"""
Grok Imagine helpers — direct SDK / REST only (no Grok CLI agent).

Auth: XAI_API_KEY if set, else `grok login` session (~/.grok/auth.json).
Images use xai_sdk (base64). Video uses S3 staging (see video_gen.py).
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

import httpx
from xai_sdk import AsyncClient

from app.config import VIDEO_DURATION_DEFAULT
from app import imagine_settings
from app.pipeline.auth_token import resolve_bearer_token

ALLOWED_ASPECT = frozenset(
    {"1:1", "3:4", "4:3", "9:16", "16:9", "2:3", "3:2"}
)


def require_api_key() -> str:
    """Back-compat: any usable bearer (API key or grok session)."""
    token, _src = resolve_bearer_token()
    return token


def normalize_aspect(aspect: str | None) -> str:
    a = (aspect or "9:16").strip()
    if a not in ALLOWED_ASPECT:
        return "9:16"
    return a


def clamp_duration(seconds: int | float | None) -> int:
    d = int(seconds if seconds is not None else VIDEO_DURATION_DEFAULT)
    return max(1, min(15, d))


def identity_plate(aspect: str, dest_dir: Path) -> Path:
    """Blank frame so look-lock stills are not image-edits of a posed photo.

    Imagine treats the first ``image_urls`` item as the canvas (pose lock).
    Sending this plate first keeps character refs as identity only.
    """
    a = normalize_aspect(aspect)
    w, h = (720, 720) if a == "1:1" else (720, 1280)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f".identity-plate-{w}x{h}.png"
    if path.is_file() and path.stat().st_size > 800:
        return path
    from PIL import Image

    Image.new("RGB", (w, h), (18, 18, 22)).save(path, format="PNG")
    return path


def _sniff_image_mime(raw: bytes) -> str | None:
    """Many pack stills are JPEG bytes saved as .png. The API trusts the mime."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def path_to_data_url(path: Path) -> str:
    raw = path.read_bytes()
    mime = _sniff_image_mime(raw)
    if not mime:
        mime, _ = mimetypes.guess_type(str(path))
    if not mime:
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _client() -> AsyncClient:
    token, _src = resolve_bearer_token()
    return AsyncClient(api_key=token)


async def download_to_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=180.0, follow_redirects=True) as http:
        resp = await http.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)


def _save_image_response(response, dest: Path) -> None:
    """Write ImageResponse to disk (prefer base64)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    b64 = getattr(response, "base64", None)
    if b64:
        data = b64
        if isinstance(data, str) and data.startswith("data:"):
            data = data.split(",", 1)[1]
        dest.write_bytes(base64.b64decode(data))
        return
    raise ValueError("no_base64")


async def generate_image(
    *,
    prompt: str,
    dest: Path,
    aspect_ratio: str = "9:16",
    image_path: Path | None = None,
    model: str | None = None,
    reference_paths: list[Path] | None = None,
) -> Path:
    """
    Text-to-image, image-edit, or multi-ref edit.

    ``image_path`` is the pose/composition seed (image-edit / frame 1).
    ``reference_paths`` are character/look stills. A lone reference must
    not go out as ``image_url`` — that pins the source pose.
    """
    client = _client()
    kwargs: dict = {
        "prompt": prompt,
        "model": model or imagine_settings.image_model(),
        "aspect_ratio": normalize_aspect(aspect_ratio),
        "resolution": imagine_settings.image_resolution(),
        "image_format": "base64",
    }
    chosen = kwargs["model"]
    if "2.0" in str(chosen):
        kwargs["quality"] = "medium"
    pose_seed = image_path is not None and image_path.is_file()
    images: list[Path] = []
    if pose_seed:
        images.append(image_path)
    elif reference_paths:
        # First slot is the canvas. A posed photo here locks that pose.
        images.append(identity_plate(aspect_ratio, dest.parent))
    seen = {str(p.resolve()) for p in images}
    for extra in reference_paths or []:
        if not extra.is_file():
            continue
        ident = str(extra.resolve())
        if ident in seen:
            continue
        seen.add(ident)
        images.append(extra)
        if len(images) >= 5:
            break
    if pose_seed and len(images) == 1:
        kwargs["image_url"] = path_to_data_url(images[0])
    elif images:
        kwargs["image_urls"] = [path_to_data_url(p) for p in images]

    response = await client.image.sample(**kwargs)

    try:
        _save_image_response(response, dest)
    except ValueError:
        url = getattr(response, "url", None) or getattr(response, "public_url", None)
        if not url:
            raise RuntimeError("Image response had neither base64 nor url") from None
        await download_to_file(url, dest)

    return dest


async def generate_video_from_image(
    *,
    prompt: str,
    image_path: Path,
    dest: Path,
    duration: int | float | None = None,
    aspect_ratio: str = "9:16",
    model: str | None = None,
    resolution: str | None = None,
) -> Path:
    """Image-to-video via S3 staging (ZDR-safe). No Grok CLI."""
    from app.pipeline.video_gen import generate_video_from_image as _s3_video

    return await _s3_video(
        prompt=prompt,
        image_path=image_path,
        dest=dest,
        duration=duration,
        aspect_ratio=aspect_ratio,
        model=model or imagine_settings.video_model(),
        resolution=resolution or imagine_settings.video_resolution(),
    )
