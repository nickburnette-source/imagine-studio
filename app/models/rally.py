"""Rally package model and on-disk meta.json persistence."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class StepStatus(str, Enum):
    LOCKED = "locked"
    READY = "ready"
    RUNNING = "running"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    ERROR = "error"


class GateState(str, Enum):
    LOCKED = "locked"
    OPEN = "open"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"


class StepKind(str, Enum):
    IMAGE = "image"
    IDLE = "idle"
    VIDEO = "video"


class PromptHistoryEntry(BaseModel):
    at: str
    base_prompt: str
    custom_guidance: str
    composed_prompt: str
    paths: list[str] = Field(default_factory=list)


class FavoriteEntry(BaseModel):
    """Hearted take kept as a fallback while hunting for a better gen."""

    id: str
    at: str
    note: str = ""
    # Paths relative to the rally root, e.g. favorites/Rally/20260821T120000Z/Rally.mp4
    paths: list[str] = Field(default_factory=list)
    base_prompt: str = ""
    custom_guidance: str = ""
    composed_prompt: str = ""

    def primary_path(self) -> str | None:
        if not self.paths:
            return None
        for p in self.paths:
            if p.lower().endswith((".mp4", ".webm", ".mov")):
                return p
        return self.paths[0]


class StepState(BaseModel):
    id: str
    label: str
    role: str = ""
    kind: StepKind = StepKind.VIDEO
    status: StepStatus = StepStatus.LOCKED
    base_prompt: str = ""
    custom_guidance: str = ""
    custom_placeholder: str = ""
    composed_prompt: str = ""
    outputs: list[str] = Field(default_factory=list)
    error: str | None = None
    history: list[PromptHistoryEntry] = Field(default_factory=list)
    favorites: list[FavoriteEntry] = Field(default_factory=list)
    # Step-specific UI options (e.g. Idle: generate_mp4 / generate_png)
    options: dict[str, Any] = Field(default_factory=dict)

    def primary_output(self) -> str | None:
        if not self.outputs:
            return None
        # Prefer video for display when both still + video exist
        for name in self.outputs:
            if name.lower().endswith((".mp4", ".webm", ".mov")):
                return name
        return self.outputs[0]

    def idle_want_mp4(self) -> bool:
        return bool(self.options.get("generate_mp4", True))

    def idle_want_png(self) -> bool:
        return bool(self.options.get("generate_png", False))

    def idle_want_boomerang(self) -> bool:
        """Post-process Idle.mp4 into forward+reverse (audio stripped)."""
        return bool(self.options.get("boomerang", False))

    def idle_selected_outputs(self) -> list[str]:
        """Outputs the user asked to produce for Idle this run."""
        if self.kind != StepKind.IDLE:
            return list(self.outputs)
        out: list[str] = []
        if self.idle_want_png():
            out.append("IdleKeyframe.png")
        if self.idle_want_mp4():
            out.append("Idle.mp4")
        # Always keep keyframe path available when making video (working asset)
        if self.idle_want_mp4() and "IdleKeyframe.png" not in out:
            # Keyframe is still written as source for i2v / branch lock
            out.insert(0, "IdleKeyframe.png")
        return out or ["Idle.mp4", "IdleKeyframe.png"]


PIPELINE_STEPS: list[str] = [
    "Preview",
    "Idle",
    "Rally",
    "Message",
    "Accept",
    "Decline",
    "Adjust",
]

# Product-facing playback order for final review only.
# Idle sits after Message and before Accept; after Adjust we hold on Idle looping.
FINAL_REVIEW_STEPS: list[str] = [
    "Preview",
    "Rally",
    "Message",
    "Idle",
    "Accept",
    "Decline",
    "Adjust",  # Reschedule in the app
]

BRANCH_STEPS: list[str] = ["Rally", "Message", "Accept", "Decline", "Adjust"]
GATE_ORDER: list[str] = ["preview", "idle", "branch"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def opening_still_name(step_id: str) -> str:
    """On-disk opening still. The one clip is start.png, not Rally.start.png."""
    if step_id == "Preview":
        return "Preview.png"
    if step_id == "Idle":
        return "IdleKeyframe.png"
    if step_id == "Adjust":
        return "Reschedule.start.png"
    if step_id == "Rally":
        return "start.png"
    return f"{step_id}.start.png"


def clip_video_name(step_id: str) -> str:
    """On-disk take. The one clip is take.mp4, not Rally.mp4."""
    if step_id == "Adjust":
        return "Reschedule.mp4"
    if step_id == "Idle":
        return "Idle.mp4"
    if step_id == "Rally":
        return "take.mp4"
    return f"{step_id}.mp4"


def clip_label(step_id: str) -> str:
    """Name shown on the edit screen. The step id stays Rally internally."""
    if step_id == "Adjust":
        return "Reschedule"
    if step_id == "Rally":
        return "Take"
    return step_id


def _is_clip_working_still(name: str) -> bool:
    n = Path(name).name.lower()
    return n.endswith(".start.png") or n in {
        "idlekeyframe.png",
        "idle.last.png",
        "preview.clean.png",
    }


# history/Rally.20261019T200809Z.mp4 → Rally.mp4
_ARCHIVE_STAMP = re.compile(r"\.\d{8}T\d{6}Z(?=\.[^.]+$)")


def file_fingerprint(path: Path) -> str:
    """Cheap identity for take files. copy2 preserves mtime, so hash size."""
    path = Path(path)
    if not path.is_file():
        return ""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    digest = hashlib.md5()
    with path.open("rb") as f:
        digest.update(f.read(65536))
        if size > 65536:
            f.seek(max(0, size - 4096))
            digest.update(f.read(4096))
    return f"{size}:{digest.hexdigest()}"


def slugify(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[-\s]+", "-", s).strip("-")
    return s or "rally"


class RallyPackage(BaseModel):
    name: str
    concept: str = ""
    mode: str = "human"  # human | auto
    # Prompt pack: character | scene | … (defaults/templates/{id}.yaml)
    template: str = "character"
    aspect_ratio: str = "9:16"
    duration_default: int = 6
    skill_note: str = ""
    gates: dict[str, GateState] = Field(
        default_factory=lambda: {
            "preview": GateState.OPEN,
            "idle": GateState.OPEN,
            "branch": GateState.OPEN,
        }
    )
    steps: dict[str, StepState] = Field(default_factory=dict)
    s3: dict[str, Any] = Field(default_factory=lambda: {"last_upload": None})
    # Full-sequence review after every pipeline step is approved
    final_review_approved: bool = False
    # free | premium | private
    publish_tier: str | None = None
    # Public-facing folder name under free|premium/rallies/ (e.g. "DC Knight")
    display_name: str | None = None
    # Voices + room-tone tags that ride every clip (Direct canvas).
    kit: dict[str, Any] = Field(
        default_factory=lambda: {
            "voices": [
                {"voice_id": "eve", "role": "Voice 1"},
                {"voice_id": "leo", "role": "Voice 2"},
            ],
            "sounds": [],
        }
    )
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)

    # Runtime-only (not always needed in JSON)
    root: Path | None = Field(default=None, exclude=True)

    def rally_title(self) -> str:
        """Human name for chrome, cards, and S3. Not the folder slug."""
        slug = (self.name or "").strip()
        label = (self.display_name or "").strip()
        if label and label.lower().replace(" ", "-") != slug.lower():
            return label
        if label and label != slug:
            return label
        return slug.replace("-", " ").title() or "Untitled Rally"

    def ensure_display_name(self) -> str:
        title = self.rally_title()
        if self.display_name != title:
            self.display_name = title
        return title

    def touch(self) -> None:
        self.updated_at = utc_now_iso()

    def step(self, step_id: str) -> StepState:
        if step_id not in self.steps:
            raise KeyError(f"Unknown step: {step_id}")
        return self.steps[step_id]

    def dir(self) -> Path:
        if self.root is None:
            raise RuntimeError("RallyPackage.root is not set")
        return self.root

    def path_for(self, filename: str) -> Path:
        return self.dir() / filename

    def existing_outputs(self, step_id: str) -> list[Path]:
        st = self.step(step_id)
        return [self.path_for(o) for o in st.outputs if self.path_for(o).is_file()]

    def clip_still_name(self, step_id: str) -> str:
        return opening_still_name(step_id)

    def review_media(self, step_id: str) -> list[Path]:
        """Stills or takes that count as 'this step has something to approve'."""
        seen: set[str] = set()
        found: list[Path] = []

        def add(name: str) -> None:
            p = self.path_for(name)
            if not p.is_file():
                return
            try:
                key = str(p.resolve())
            except OSError:
                key = str(p)
            if key in seen:
                return
            seen.add(key)
            found.append(p)

        for o in self.step(step_id).outputs:
            add(o)
        add(self.clip_still_name(step_id))
        if step_id == "Idle":
            add("Message.last.png")
            add("Idle.mp4")
        elif step_id == "Message":
            add("Message.last.png")
            add("Message.mp4")
        elif step_id == "Preview":
            add("Preview.png")
        return found

    def is_package_complete(self) -> bool:
        return bool(self.steps) and all(
            self.review_media(sid) for sid in self.steps
        )

    def all_steps_approved(self) -> bool:
        return bool(self.steps) and all(
            self.step(sid).status == StepStatus.APPROVED for sid in self.steps
        )

    def primary_media_for_step(self, step_id: str) -> Path | None:
        """Best on-disk asset for sequential review playback."""
        st = self.step(step_id)
        # Prefer video when listed
        for name in st.outputs:
            p = self.path_for(name)
            if p.is_file() and p.suffix.lower() in {".mp4", ".webm", ".mov"}:
                return p
        for name in st.outputs:
            p = self.path_for(name)
            if p.is_file():
                return p
        # Fallbacks
        for name in (
            f"{step_id}.mp4",
            self.clip_still_name(step_id),
            "Message.last.png",
            "Preview.png",
            "Idle.mp4",
            "IdleKeyframe.png",
        ):
            p = self.path_for(name)
            if p.is_file():
                return p
        return None

    def save(self) -> None:
        self.touch()
        root = self.dir()
        root.mkdir(parents=True, exist_ok=True)
        (root / "history").mkdir(exist_ok=True)
        (root / "favorites").mkdir(exist_ok=True)
        meta_path = root / "meta.json"
        payload = self.model_dump(mode="json")
        meta_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, root: Path) -> RallyPackage:
        meta_path = root / "meta.json"
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        pkg = cls.model_validate(data)
        pkg.root = root
        # Migrate legacy Adjust.mp4 → Reschedule.mp4 (product name)
        legacy = root / "Adjust.mp4"
        modern = root / "Reschedule.mp4"
        if legacy.is_file() and not modern.is_file():
            legacy.rename(modern)
        # Keep step outputs list in sync for older meta.json
        if "Adjust" in pkg.steps:
            outs = pkg.steps["Adjust"].outputs
            if "Adjust.mp4" in outs:
                pkg.steps["Adjust"].outputs = [
                    "Reschedule.mp4" if o == "Adjust.mp4" else o for o in outs
                ]
                if "Reschedule.mp4" not in pkg.steps["Adjust"].outputs:
                    pkg.steps["Adjust"].outputs = ["Reschedule.mp4"]
            elif not outs:
                pkg.steps["Adjust"].outputs = ["Reschedule.mp4"]
        return pkg

    def archive_outputs(self, step_id: str) -> list[str]:
        """Move current outputs into history/ before regenerating. Returns relative paths."""
        st = self.step(step_id)
        hist_dir = self.dir() / "history"
        hist_dir.mkdir(exist_ok=True)
        archived: list[str] = []
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for name in st.outputs:
            # Clip opening stills are working files. Archiving them before
            # image-to-video made Imagine fall back to Preview.
            if step_id != "Preview" and _is_clip_working_still(name):
                continue
            src = self.path_for(name)
            if not src.is_file():
                continue
            dest_name = f"{Path(name).stem}.{stamp}{Path(name).suffix}"
            dest = hist_dir / dest_name
            shutil.move(str(src), str(dest))
            archived.append(f"history/{dest_name}")
        # Keep the unbadged Preview sidecar with the same stamp so restores
        # can re-apply the E overlay without feeding it into Imagine.
        if step_id == "Preview":
            clean = self.path_for("Preview.clean.png")
            if clean.is_file():
                dest_name = f"Preview.clean.{stamp}.png"
                dest = hist_dir / dest_name
                shutil.move(str(clean), str(dest))
                archived.append(f"history/{dest_name}")
        return archived

    def restore_archived_outputs(self, step_id: str, archived: list[str]) -> list[str]:
        """Copy a failed generate's backups back when the live file is gone.

        History keeps its copy. A file that is already on disk is left alone.
        Scene-attempt backups are not step outputs, so they are skipped.
        """
        st = self.step(step_id)
        allowed = set(st.outputs or [])
        if step_id == "Preview":
            allowed.add("Preview.clean.png")
        root = self.dir().resolve()
        restored: list[str] = []
        for rel in archived:
            rel_path = Path(str(rel).replace("\\", "/"))
            if rel_path.is_absolute() or ".." in rel_path.parts:
                continue
            src = (root / rel_path).resolve()
            if root not in src.parents or not src.is_file():
                continue
            live_name = _ARCHIVE_STAMP.sub("", src.name, count=1)
            if live_name == src.name or live_name not in allowed:
                continue
            dest = self.path_for(live_name)
            if dest.is_file():
                continue
            shutil.copy2(src, dest)
            restored.append(live_name)
        return restored

    def prune_duplicate_history(self, step_id: str) -> int:
        """Drop extra copies of the same take. Never eat a unique Preview."""
        hist = self.dir() / "history"
        if not hist.is_dir():
            return 0
        if step_id == "Preview":
            return self._recover_preview_history()
        st = self.step(step_id)
        removed = 0
        for name in st.outputs or [step_id]:
            stem = Path(name).stem
            suf = Path(name).suffix.lower()
            files = [
                p
                for p in hist.glob(f"{stem}.*")
                if p.is_file()
                and p.suffix.lower() == suf
                and ".clean." not in p.name
            ]
            files.sort(key=lambda p: p.stat().st_mtime)
            seen: set[str] = set()
            for p in files:
                fp = file_fingerprint(p)
                if not fp:
                    continue
                if fp in seen:
                    p.unlink(missing_ok=True)
                    removed += 1
                else:
                    seen.add(fp)
        return removed

    def _recover_preview_history(self) -> int:
        """If a take exists only as Preview.clean.STAMP, restore Preview.STAMP.

        Prune used to treat the clean sidecar as a duplicate and keep it,
        which hid the still from the take strip.
        """
        from app.pipeline.explicit_mark import preview_display_history_name

        hist = self.dir() / "history"
        recovered = 0
        for clean in list(hist.glob("Preview.clean.*.png")):
            disp_name = preview_display_history_name(clean.name)
            if not disp_name:
                continue
            sibling = hist / disp_name
            if sibling.is_file():
                continue
            shutil.copy2(clean, sibling)
            recovered += 1
        return recovered

    def _history_has_fingerprint(self, step_id: str, fp: str) -> bool:
        if not fp:
            return False
        st = self.step(step_id)
        hist = self.dir() / "history"
        if not hist.is_dir():
            return False
        for name in st.outputs or [step_id]:
            stem = Path(name).stem
            suf = Path(name).suffix.lower()
            for p in hist.glob(f"{stem}.*"):
                if p.suffix.lower() != suf or not p.is_file():
                    continue
                if ".clean." in p.name:
                    continue
                if file_fingerprint(p) == fp:
                    return True
        return False

    def _swap_live_from(self, step_id: str, src: Path, dest: Path) -> None:
        """Replace live dest with src. Archive live only if that content is new."""
        if dest.is_file() and dest.resolve() == src.resolve():
            return
        live_fp = file_fingerprint(dest) if dest.is_file() else ""
        src_fp = file_fingerprint(src)
        if dest.is_file() and live_fp and live_fp == src_fp:
            return
        if dest.is_file():
            if self._history_has_fingerprint(step_id, live_fp):
                dest.unlink(missing_ok=True)
            else:
                self.archive_outputs(step_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    def favorite_dir(self, step_id: str, fav_id: str) -> Path:
        return self.dir() / "favorites" / step_id / fav_id

    def save_favorite(
        self, step_id: str, *, note: str = ""
    ) -> FavoriteEntry:
        """Copy current step outputs into favorites/ (does not remove the live files)."""
        st = self.step(step_id)
        existing = self.existing_outputs(step_id)
        if not existing:
            # Preview sometimes only has Preview.png without being listed yet
            if step_id == "Preview":
                preview = self.path_for("Preview.png")
                if preview.is_file():
                    existing = [preview]
            if not existing:
                raise ValueError(f"No media to heart for {step_id}")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        # Avoid id collisions if hearting twice in the same second
        fav_id = stamp
        dest_root = self.favorite_dir(step_id, fav_id)
        n = 1
        while dest_root.exists():
            fav_id = f"{stamp}-{n}"
            dest_root = self.favorite_dir(step_id, fav_id)
            n += 1
        dest_root.mkdir(parents=True, exist_ok=True)

        rel_paths: list[str] = []
        for src in existing:
            dest = dest_root / src.name
            shutil.copy2(src, dest)
            rel_paths.append(
                str(Path("favorites") / step_id / fav_id / src.name)
            )
        if step_id == "Preview":
            clean = self.path_for("Preview.clean.png")
            if clean.is_file() and clean not in existing:
                dest = dest_root / clean.name
                shutil.copy2(clean, dest)
                rel_paths.append(
                    str(Path("favorites") / step_id / fav_id / clean.name)
                )

        entry = FavoriteEntry(
            id=fav_id,
            at=utc_now_iso(),
            note=(note or "").strip(),
            paths=rel_paths,
            base_prompt=st.base_prompt,
            custom_guidance=st.custom_guidance,
            composed_prompt=st.composed_prompt,
        )
        st.favorites.insert(0, entry)
        self.save()
        return entry

    def get_favorite(
        self, step_id: str, fav_id: str
    ) -> FavoriteEntry | None:
        for fav in self.step(step_id).favorites:
            if fav.id == fav_id:
                return fav
        return None

    def restore_favorite(self, step_id: str, fav_id: str) -> FavoriteEntry:
        """Archive live outputs, then copy a hearted take back to canonical names."""
        fav = self.get_favorite(step_id, fav_id)
        if fav is None:
            raise ValueError(f"Favorite {fav_id} not found for {step_id}")
        missing = [
            p for p in fav.paths if not self.path_for(p).is_file()
        ]
        if missing:
            raise FileNotFoundError(
                f"Favorite files missing: {', '.join(missing)}"
            )

        st = self.step(step_id)
        restored_names: list[str] = []
        for rel in fav.paths:
            src = self.path_for(rel)
            dest_name = Path(rel).name
            dest = self.path_for(dest_name)
            self._swap_live_from(step_id, src, dest)
            restored_names.append(dest_name)
        self.prune_duplicate_history(step_id)

        # Prefer canonical output list order when possible
        if restored_names:
            st.outputs = list(dict.fromkeys([*restored_names, *st.outputs]))
        if fav.base_prompt:
            st.base_prompt = fav.base_prompt
        if fav.custom_guidance is not None:
            st.custom_guidance = fav.custom_guidance
        if fav.composed_prompt:
            st.composed_prompt = fav.composed_prompt
        if st.status != StepStatus.LOCKED:
            st.status = StepStatus.NEEDS_REVIEW
            st.error = None
        if step_id == "Preview":
            from app.pipeline.explicit_mark import (
                apply_explicit_mark,
                snapshot_clean_preview,
            )

            snapshot_clean_preview(self)
            apply_explicit_mark(self)
        self.save()
        return fav

    def restore_history_take(self, step_id: str, hist_path: Path) -> Path:
        """Make a history (or other) file the live output for this step."""
        hist_path = Path(hist_path)
        if not hist_path.is_file():
            raise FileNotFoundError(str(hist_path))
        st = self.step(step_id)
        suffix = hist_path.suffix.lower()
        canonical: str | None = None
        for name in st.outputs:
            stem = Path(name).stem
            if Path(name).suffix.lower() != suffix:
                continue
            if hist_path.name == name or hist_path.name.startswith(f"{stem}."):
                canonical = name
                break
        if canonical is None:
            if step_id == "Preview":
                canonical = "Preview.png"
            elif step_id == "Idle" and suffix == ".png":
                canonical = "IdleKeyframe.png"
            elif suffix in {".mp4", ".webm", ".mov"}:
                canonical = next(
                    (
                        n
                        for n in st.outputs
                        if n.lower().endswith((".mp4", ".webm", ".mov"))
                    ),
                    f"{step_id}.mp4",
                )
            else:
                if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
                    from app.pipeline.prompts import staged_still_name

                    canonical = (
                        "IdleKeyframe.png"
                        if step_id == "Idle"
                        else staged_still_name(step_id)
                    )
                else:
                    canonical = (
                        st.outputs[0] if st.outputs else hist_path.name
                    )

        dest = self.path_for(canonical)
        from app.pipeline.explicit_mark import (
            apply_explicit_mark,
            clean_history_sibling,
            is_clean_preview_name,
        )

        if step_id == "Preview" and is_clean_preview_name(hist_path.name):
            shutil.copy2(hist_path, self.path_for("Preview.clean.png"))
            canonical = "Preview.png"
            dest = self.path_for(canonical)
        # Same bytes are already the approved take. Different bytes are not.
        changed = not (
            dest.is_file()
            and file_fingerprint(dest) == file_fingerprint(hist_path)
        )
        if step_id == "Preview" and is_clean_preview_name(hist_path.name):
            self._swap_live_from(step_id, hist_path, dest)
        else:
            self._swap_live_from(step_id, hist_path, dest)
            if step_id == "Preview":
                sibling = clean_history_sibling(hist_path)
                clean_dest = self.path_for("Preview.clean.png")
                if sibling is not None and sibling.is_file():
                    shutil.copy2(sibling, clean_dest)
                elif dest.is_file():
                    shutil.copy2(dest, clean_dest)
        if canonical not in st.outputs:
            if canonical.lower().endswith((".mp4", ".webm", ".mov")):
                st.outputs = [canonical, *st.outputs]
            elif step_id == "Preview" or not _is_clip_working_still(canonical):
                st.outputs = [*st.outputs, canonical]
        if st.status != StepStatus.LOCKED and (
            changed or st.status != StepStatus.APPROVED
        ):
            st.status = StepStatus.NEEDS_REVIEW
            st.error = None
        if changed:
            self.final_review_approved = False
            self.publish_tier = None
        if step_id == "Preview" and bool(st.options.get("explicit_e")):
            # Re-stamp from the clean sidecar. When E is off, leave the
            # clicked take on Preview.png — copying clean over it made
            # stamped history thumbs look unclickable.
            apply_explicit_mark(self)
        if suffix in {".png", ".jpg", ".jpeg", ".webp"} and step_id != "Preview":
            st.options["start_file"] = Path(canonical).name
            if step_id != "Idle":
                st.options["start_join"] = "still"
            st.options["work_surface"] = "still"
        elif suffix in {".mp4", ".webm", ".mov"}:
            st.options["work_surface"] = "take"
            from app.pipeline.scenes import clear_timeline

            clear_timeline(self, step_id)
        self.prune_duplicate_history(step_id)
        self.save()
        return dest

    def delete_take(self, step_id: str, path: Path) -> str:
        """Remove a still or video take. Live files fall back to another history take."""
        path = Path(path)
        if not path.is_file():
            return "Already gone"
        suffix = path.suffix.lower()
        from app.pipeline.prompts import staged_still_name

        if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            if step_id == "Preview":
                live = self.path_for("Preview.png")
                stem = "Preview"
            elif step_id == "Idle":
                live = self.path_for("IdleKeyframe.png")
                stem = "IdleKeyframe"
            else:
                live = self.path_for(staged_still_name(step_id))
                stem = Path(live.name).stem
        else:
            live_name = next(
                (
                    n
                    for n in self.step(step_id).outputs
                    if n.lower().endswith((".mp4", ".webm", ".mov"))
                ),
                f"{step_id}.mp4",
            )
            live = self.path_for(live_name)
            stem = Path(live_name).stem

        def _unlink_with_sidecar(p: Path) -> None:
            if not p.is_file():
                return
            name = p.name
            p.unlink(missing_ok=True)
            if name == "Preview.png":
                self.path_for("Preview.clean.png").unlink(missing_ok=True)
            elif name.startswith("Preview.") and not name.startswith(
                "Preview.clean."
            ):
                side = p.with_name("Preview.clean." + name[len("Preview.") :])
                side.unlink(missing_ok=True)

        try:
            is_live = live.is_file() and live.resolve() == path.resolve()
        except OSError:
            is_live = False
        hist = self.dir() / "history"

        if is_live:
            replacement = None
            live_fp = file_fingerprint(path)
            if hist.is_dir():
                cands = [
                    p
                    for p in hist.glob(f"{stem}.*")
                    if p.is_file()
                    and p.suffix.lower() == suffix
                    and ".clean." not in p.name
                ]
                cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                for cand in cands:
                    if file_fingerprint(cand) != live_fp:
                        replacement = cand
                        break
            _unlink_with_sidecar(path)
            if replacement is not None:
                self.restore_history_take(step_id, replacement)
                return f"Deleted live take · restored {replacement.name}"
            return f"Deleted {path.name}"

        fp = file_fingerprint(path)
        _unlink_with_sidecar(path)
        # Same bytes under another history name is the same attempt.
        if fp and hist.is_dir():
            extra = list(hist.glob(f"{stem}.*"))
            extra.extend(hist.glob(f"scene-{step_id}-*"))
            for cand in extra:
                if not cand.is_file():
                    continue
                try:
                    if live.is_file() and cand.resolve() == live.resolve():
                        continue
                except OSError:
                    continue
                if cand.suffix.lower() != suffix and not cand.name.startswith(
                    f"scene-{step_id}-"
                ):
                    continue
                if file_fingerprint(cand) == fp:
                    _unlink_with_sidecar(cand)
        return f"Deleted {path.name}"

    def delete_favorite(self, step_id: str, fav_id: str) -> None:
        st = self.step(step_id)
        fav = self.get_favorite(step_id, fav_id)
        if fav is None:
            return
        # Remove files for this favorite folder
        folder = self.favorite_dir(step_id, fav_id)
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
        st.favorites = [f for f in st.favorites if f.id != fav_id]
        self.save()


def delete_rally(root: Path) -> None:
    """Remove a rally folder from disk. ``root`` must contain meta.json."""
    root = Path(root)
    if not root.is_dir() or not (root / "meta.json").is_file():
        raise ValueError(f"Not a rally folder: {root}")
    shutil.rmtree(root)


def list_rallies(output_dir: Path) -> list[Path]:
    """Rally folders newest-first (by meta.updated_at, then mtime)."""
    if not output_dir.is_dir():
        return []
    found: list[Path] = []
    for child in output_dir.iterdir():
        if child.is_dir() and (child / "meta.json").is_file():
            found.append(child)

    def _sort_key(p: Path) -> str:
        try:
            data = json.loads((p / "meta.json").read_text(encoding="utf-8"))
            return str(data.get("updated_at") or data.get("created_at") or "")
        except Exception:  # noqa: BLE001
            try:
                return str(p.stat().st_mtime)
            except OSError:
                return ""

    # Newest first
    found.sort(key=_sort_key, reverse=True)
    return found
