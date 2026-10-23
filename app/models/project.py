"""One Imagine project on disk.

A project is a prompt, a few look references, and a gallery of takes.
It is not a rally pack: there are no steps, gates, or publish tiers.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from app.config import OUTPUT_DIR
from app.pipeline.imagine import ALLOWED_ASPECT

_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
_VIDEO_EXT = {".mp4", ".webm", ".mov"}
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def lock_for(slug: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(slug)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[slug] = lock
        return lock


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def slugify(name: str) -> str:
    text = name.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "-", text).strip("-")
    return text or "project"


class Take(BaseModel):
    id: str
    at: str
    rel: str
    kind: str  # image | video | edit
    prompt: str = ""


class Project(BaseModel):
    name: str
    title: str
    prompt: str = ""
    mode: str = "image"  # image | video
    aspect_ratio: str = "9:16"
    duration: int = 6
    resolution: str = "720p"
    pin_still: bool = True
    refs: list[str] = Field(default_factory=list)
    takes: list[Take] = Field(default_factory=list)
    shown: str | None = None
    error: str | None = None
    status: str = "ready"  # ready | running

    def root(self) -> Path:
        return OUTPUT_DIR / self.name

    def save(self) -> None:
        root = self.root()
        root.mkdir(parents=True, exist_ok=True)
        tmp = root / "meta.json.tmp"
        tmp.write_text(
            json.dumps(self.model_dump(), indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(root / "meta.json")

    def resolve(self, rel: str) -> Path:
        root = self.root().resolve()
        path = (root / rel).resolve()
        if path != root and root not in path.parents:
            raise ValueError("path escapes project")
        return path

    def shown_file(self) -> Path | None:
        if not self.shown:
            return None
        try:
            path = self.resolve(self.shown)
        except ValueError:
            return None
        return path if path.is_file() else None

    def shown_kind(self) -> str:
        path = self.shown_file()
        if path is None:
            return "none"
        if path.suffix.lower() in _VIDEO_EXT:
            return "video"
        return "image"


def load(slug: str) -> Project:
    meta = OUTPUT_DIR / slug / "meta.json"
    if not meta.is_file():
        raise FileNotFoundError(slug)
    return Project.model_validate(json.loads(meta.read_text(encoding="utf-8")))


def list_projects() -> list[Project]:
    if not OUTPUT_DIR.is_dir():
        return []
    found: list[Project] = []
    for child in OUTPUT_DIR.iterdir():
        if not child.is_dir() or not (child / "meta.json").is_file():
            continue
        try:
            found.append(load(child.name))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    found.sort(key=lambda proj: proj.title.lower())
    return found


def create(title: str) -> Project:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base = slugify(title)
    slug = base
    n = 2
    while (OUTPUT_DIR / slug).exists():
        slug = f"{base}-{n}"
        n += 1
    proj = Project(name=slug, title=(title or slug).strip() or slug)
    with lock_for(slug):
        proj.save()
    return proj


def delete(slug: str) -> None:
    root = (OUTPUT_DIR / slug).resolve()
    base = OUTPUT_DIR.resolve()
    if root.parent != base or not root.is_dir():
        raise FileNotFoundError(slug)
    with lock_for(slug):
        shutil.rmtree(root)


def update(slug: str, **fields: object) -> Project:
    """Reload, change settings, and save. Takes and status are left alone."""
    with lock_for(slug):
        proj = load(slug)
        if "title" in fields and str(fields["title"] or "").strip():
            proj.title = str(fields["title"]).strip()
        if "prompt" in fields:
            proj.prompt = str(fields["prompt"] or "")
        if fields.get("mode") in {"image", "video"}:
            proj.mode = str(fields["mode"])
        if fields.get("aspect_ratio") in ALLOWED_ASPECT:
            proj.aspect_ratio = str(fields["aspect_ratio"])
        if "duration" in fields:
            try:
                proj.duration = max(1, min(15, int(fields["duration"])))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                pass
        if fields.get("resolution") in {"480p", "720p", "1080p"}:
            proj.resolution = str(fields["resolution"])
        if "pin_still" in fields:
            proj.pin_still = bool(fields["pin_still"])
        if "error" in fields:
            err = fields["error"]
            proj.error = str(err) if err else None
        proj.save()
        return proj


def add_ref(slug: str, raw: bytes, suffix: str) -> Project:
    ext = suffix.lower() if suffix.startswith(".") else f".{suffix.lower()}"
    if ext not in _IMAGE_EXT:
        ext = ".png"
    rel = f"refs/{secrets.token_hex(4)}{ext}"
    with lock_for(slug):
        proj = load(slug)
        if len(proj.refs) >= 7:
            raise RuntimeError("Seven references is the limit")
        dest = proj.resolve(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        proj.refs.append(rel)
        proj.save()
        return proj


def remove_ref(slug: str, rel: str) -> Project:
    with lock_for(slug):
        proj = load(slug)
        if rel not in proj.refs:
            return proj
        proj.refs = [item for item in proj.refs if item != rel]
        try:
            proj.resolve(rel).unlink(missing_ok=True)
        except ValueError:
            pass
        proj.save()
        return proj


def add_take(slug: str, src: Path, *, kind: str, prompt: str) -> Project:
    """Move ``src`` into the gallery and show it."""
    with lock_for(slug):
        proj = load(slug)
        ext = src.suffix.lower() if src.suffix else ".png"
        take_id = utc_stamp()
        rel = f"takes/{take_id}{ext}"
        dest = proj.resolve(rel)
        while dest.exists():
            take_id = f"{utc_stamp()}-{secrets.token_hex(2)}"
            rel = f"takes/{take_id}{ext}"
            dest = proj.resolve(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), dest)
        proj.takes.insert(
            0,
            Take(
                id=take_id,
                at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                rel=rel,
                kind=kind,
                prompt=prompt,
            ),
        )
        proj.shown = rel
        proj.save()
        return proj


def show_take(slug: str, rel: str) -> Project:
    with lock_for(slug):
        proj = load(slug)
        if any(take.rel == rel for take in proj.takes):
            proj.shown = rel
            proj.save()
        return proj


def delete_take(slug: str, rel: str) -> Project:
    with lock_for(slug):
        proj = load(slug)
        proj.takes = [take for take in proj.takes if take.rel != rel]
        if proj.shown == rel:
            proj.shown = proj.takes[0].rel if proj.takes else None
        try:
            proj.resolve(rel).unlink(missing_ok=True)
        except ValueError:
            pass
        proj.save()
        return proj


def try_mark_running(slug: str) -> bool:
    """Claim the project for one generation. False if one is already running."""
    with lock_for(slug):
        proj = load(slug)
        if proj.status == "running":
            return False
        proj.status = "running"
        proj.error = None
        proj.save()
        return True


def mark_ready(slug: str, error: str | None = None) -> Project:
    with lock_for(slug):
        proj = load(slug)
        proj.status = "ready"
        proj.error = error
        proj.save()
        return proj


def recover_interrupted() -> list[str]:
    """A restart kills an in-flight Imagine call. Don't leave those spinning."""
    fixed: list[str] = []
    for proj in list_projects():
        if proj.status != "running":
            continue
        mark_ready(
            proj.name,
            "Generation interrupted before the file was saved.",
        )
        fixed.append(proj.name)
    return fixed
