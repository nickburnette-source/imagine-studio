"""Shared asset library for one project folder.

Every still, take, frame grab, and upload in the project is one catalog.
New files land in ``library/``. Older files stay where they are and join
the same catalog, so a storyboard can pull any of them.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
from pathlib import Path

from app.models.rally import RallyPackage

STILL_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".m4v"}
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".ogg"}
MEDIA_EXT = STILL_EXT | VIDEO_EXT | AUDIO_EXT

_STAMP = re.compile(r"\.\d{8}T\d{6}Z")


def library_dir(pkg: RallyPackage) -> Path:
    path = pkg.dir() / "library"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manifest_path(pkg: RallyPackage) -> Path:
    return library_dir(pkg) / "manifest.json"


def _load_manifest(pkg: RallyPackage) -> list[dict]:
    path = _manifest_path(pkg)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data.get("assets") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        rel = str(row.get("rel") or "").replace("\\", "/").lstrip("/")
        ident = str(row.get("id") or "").strip()
        if not rel or not ident or ".." in Path(rel).parts:
            continue
        out.append(
            {
                "id": ident,
                "rel": rel,
                "name": str(row.get("name") or "")[:80],
                "kind": _kind_name(str(row.get("kind") or "")),
            }
        )
    return out


def _save_manifest(pkg: RallyPackage, rows: list[dict]) -> None:
    path = _manifest_path(pkg)
    payload = {
        "assets": [
            {
                "id": row["id"],
                "rel": row["rel"],
                "name": row["name"],
                "kind": row["kind"],
            }
            for row in rows
        ]
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _kind_for_suffix(suffix: str) -> str:
    ext = suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return "still"


def _kind_name(kind: str) -> str:
    return kind if kind in {"still", "video", "audio"} else "still"


def pretty_name(stem: str) -> str:
    text = _STAMP.sub("", stem or "")
    text = text.replace(".", " ").replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:48] or "Asset")


def _rel(pkg: RallyPackage, path: Path) -> str:
    return path.resolve().relative_to(pkg.dir().resolve()).as_posix()


def _walk(pkg: RallyPackage) -> list[Path]:
    root = pkg.dir().resolve()
    found: list[Path] = []
    if not root.is_dir():
        return found
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part.startswith(".") or part == "__pycache__" for part in rel_parts):
            continue
        if path.name in {"manifest.json", "meta.json"}:
            continue
        if path.suffix.lower() not in MEDIA_EXT:
            continue
        try:
            if path.stat().st_size < 32:
                continue
        except OSError:
            continue
        found.append(path)
    return found


def catalog(pkg: RallyPackage) -> list[dict]:
    """Every media file in the project, newest first within each kind."""
    manifest = _load_manifest(pkg)
    known = {row["rel"]: row for row in manifest}
    used_ids = {row["id"] for row in manifest}
    changed = False
    found: list[dict] = []
    for path in _walk(pkg):
        try:
            rel = _rel(pkg, path)
        except ValueError:
            continue
        row = known.get(rel)
        if row is None:
            digest = hashlib.sha1(rel.encode()).hexdigest()
            ident = digest[:8]
            if ident in used_ids:
                ident = digest[:12]
            row = {
                "id": ident,
                "rel": rel,
                "name": pretty_name(path.stem),
                "kind": _kind_for_suffix(path.suffix),
            }
            known[rel] = row
            used_ids.add(ident)
            changed = True
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        found.append({**row, "path": path, "mtime": mtime})
    rels = {row["rel"] for row in found}
    stored = [known[rel] for rel in rels if rel in known]
    if changed or len(stored) != len(manifest):
        _save_manifest(pkg, stored)
    kind_order = {"still": 0, "video": 1, "audio": 2}
    found.sort(key=lambda row: (kind_order.get(row["kind"], 9), -row["mtime"]))
    return found


def by_id(pkg: RallyPackage) -> dict[str, dict]:
    return {row["id"]: row for row in catalog(pkg)}


def next_name(rows: list[dict], kind: str, label: str) -> str:
    taken = {
        str(row.get("name") or "")
        for row in rows
        if row.get("kind") == kind
    }
    if label not in taken:
        return label
    n = 2
    while f"{label} {n}" in taken:
        n += 1
    return f"{label} {n}"


def ingest(
    pkg: RallyPackage,
    src: Path,
    *,
    name: str,
    kind: str | None = None,
    move: bool = False,
) -> dict:
    """Copy a file into ``library/`` and return its catalog row."""
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(f"Missing file: {src.name}")
    suffix = src.suffix.lower()
    if suffix not in MEDIA_EXT:
        raise ValueError(f"Unsupported file {suffix or src.name}")
    kind = _kind_name(kind or _kind_for_suffix(suffix))
    ident = secrets.token_hex(4)
    dest = library_dir(pkg) / f"{ident}{suffix}"
    if move:
        shutil.move(str(src), dest)
    else:
        shutil.copy2(src, dest)
    rel = _rel(pkg, dest)
    row = {"id": ident, "rel": rel, "name": (name or "Asset")[:80], "kind": kind}
    rows = _load_manifest(pkg)
    rows.append(row)
    _save_manifest(pkg, rows)
    return {**row, "path": dest, "mtime": dest.stat().st_mtime}


def asset_path(pkg: RallyPackage, asset_id: str) -> Path | None:
    row = by_id(pkg).get(asset_id)
    if row is None:
        return None
    path = row.get("path")
    return path if isinstance(path, Path) and path.is_file() else None
