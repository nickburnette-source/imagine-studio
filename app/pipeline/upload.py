"""Upload compressed rally packages to the DutyCall drive bucket."""

from __future__ import annotations

import os
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Literal

from app.config import (
    AWS_PROFILE,
    AWS_REGION,
    DUTYCALL_PRIVATE_FOLDER,
    DUTYCALL_S3_BUCKET,
)
from app.models.rally import RallyPackage, utc_now_iso

PublishTier = Literal["free", "premium", "private"]


def sanitize_display_name(name: str) -> str:
    """Public-facing rally folder name (e.g. 'DC Knight')."""
    s = (name or "").strip()
    s = re.sub(r"[/\\]+", "-", s)
    s = re.sub(r"\s+", " ", s)
    s = s.strip(" .")
    return s or "Untitled Rally"


def s3_uri_for(
    *,
    tier: PublishTier | str,
    display_name: str,
    private_folder: str | None = None,
) -> str:
    """
    free/premium: s3://bucket/{tier}/rallies/{Display Name}/
    private:      s3://bucket/private/{user_id}/rallies/{Display Name}.zip
    """
    bucket = DUTYCALL_S3_BUCKET
    if not bucket:
        raise ValueError(
            "AWS_DUTYCALL_S3_BUCKET_ARN / DUTYCALL_S3_BUCKET is not set"
        )
    t = (tier or "free").strip().lower()
    label = sanitize_display_name(display_name)
    if t in ("free", "premium"):
        return f"s3://{bucket}/{t}/rallies/{label}/"
    if t == "private":
        uid = (private_folder or DUTYCALL_PRIVATE_FOLDER or "").strip()
        if not uid:
            raise ValueError(
                "AWS_DUTYCALL_PRIVATE_FOLDER is not set (private user identity)"
            )
        return f"s3://{bucket}/private/{uid}/rallies/{label}.zip"
    raise ValueError(f"Unknown publish tier: {tier}")


def _aws_env() -> dict[str, str]:
    run_env = os.environ.copy()
    if AWS_PROFILE:
        run_env["AWS_PROFILE"] = AWS_PROFILE
    if AWS_REGION:
        run_env["AWS_DEFAULT_REGION"] = AWS_REGION
    return run_env


def _run_aws(cmd: list[str]) -> None:
    result = subprocess.run(
        cmd, capture_output=True, text=True, env=_aws_env()
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"aws failed ({result.returncode}):\n{result.stderr or result.stdout}"
        )


def zip_compressed_dir(compressed_dir: Path, display_name: str) -> Path:
    """Zip folder contents into sibling ``{Display_Name}.zip``."""
    label = sanitize_display_name(display_name)
    # Underscore spaces for zip filename on disk is optional — user asked
    # "{Display_Name}.zip" which may include spaces.
    zip_path = compressed_dir.parent / f"{label}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(compressed_dir.rglob("*")):
            if p.is_file() and p.name != ".DS_Store":
                zf.write(p, arcname=p.name)
    return zip_path


def upload_rally(
    compressed_dir: Path,
    pkg: RallyPackage,
    *,
    tier: PublishTier | str,
    display_name: str,
) -> str:
    """
    Upload iOS-compressed deliverables to the DutyCall drive bucket.

    free/premium → sync directory under {{tier}}/rallies/{{name}}/
    private → zip then put object under private/{{user}}/rallies/{{name}}.zip
    """
    if not compressed_dir.is_dir():
        raise FileNotFoundError(compressed_dir)

    t = (tier or "free").strip().lower()
    label = sanitize_display_name(display_name)
    dest = s3_uri_for(tier=t, display_name=label)

    if t in ("free", "premium"):
        _run_aws(
            [
                "aws",
                "s3",
                "sync",
                str(compressed_dir) + "/",
                dest,
                "--exclude",
                ".DS_Store",
            ]
        )
    elif t == "private":
        zip_path = zip_compressed_dir(compressed_dir, label)
        _run_aws(
            [
                "aws",
                "s3",
                "cp",
                str(zip_path),
                dest,
            ]
        )
    else:
        raise ValueError(f"Unknown tier: {tier}")

    pkg.publish_tier = t
    pkg.display_name = label
    pkg.s3["last_upload"] = {
        "at": utc_now_iso(),
        "uri": dest,
        "tier": t,
        "display_name": label,
        "source": str(compressed_dir),
    }
    pkg.save()
    return dest


def _aws_capture(cmd: list[str]) -> str:
    result = subprocess.run(
        cmd, capture_output=True, text=True, env=_aws_env()
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"aws failed ({result.returncode}):\n{result.stderr or result.stdout}"
        )
    return result.stdout or ""


def list_published_rallies(tier: str) -> list[str]:
    """Display names under s3://bucket/{tier}/rallies/."""
    bucket = DUTYCALL_S3_BUCKET
    if not bucket:
        raise ValueError("DutyCall S3 bucket is not set")
    t = tier.strip().lower()
    out = _aws_capture(
        ["aws", "s3", "ls", f"s3://{bucket}/{t}/rallies/"]
    )
    names: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if "PRE " in line:
            name = line.split("PRE ", 1)[-1].rstrip("/").strip()
            if name:
                names.append(name)
    return names


_STEP_FILES: dict[str, tuple[str, ...]] = {
    "Preview": ("Preview.png",),
    "Idle": ("Idle.mp4", "IdleKeyframe.png", "Idle.png"),
    "Rally": ("Rally.mp4",),
    "Message": ("Message.mp4",),
    "Accept": ("Accept.mp4",),
    "Decline": ("Decline.mp4",),
    "Adjust": ("Reschedule.mp4",),
}


def ensure_idle_working_stills(pkg: RallyPackage) -> list[str]:
    """Mint IdleKeyframe / Idle.last from published Idle media."""
    import shutil

    from app.pipeline.frames import (
        FrameError,
        extract_first_frame,
        extract_last_frame,
    )

    notes: list[str] = []
    kf = pkg.path_for("IdleKeyframe.png")
    last = pkg.path_for("Idle.last.png")
    idle_mp4 = pkg.path_for("Idle.mp4")
    idle_png = pkg.path_for("Idle.png")
    if not kf.is_file():
        if idle_png.is_file():
            shutil.copy2(idle_png, kf)
            notes.append("IdleKeyframe.png ← Idle.png")
        elif idle_mp4.is_file():
            extract_first_frame(idle_mp4, kf)
            notes.append("IdleKeyframe.png ← Idle.mp4")
    if not last.is_file() and idle_mp4.is_file():
        try:
            extract_last_frame(idle_mp4, last)
            notes.append("Idle.last.png ← Idle.mp4")
        except FrameError as exc:
            notes.append(f"Idle.last.png skipped: {exc}")
    return notes


def mark_imported_package_ready(pkg: RallyPackage) -> list[str]:
    """Approve every step that already has media so Generate is unlocked."""
    from app.models.rally import PIPELINE_STEPS, GateState, StepStatus

    notes = ensure_idle_working_stills(pkg)
    idle_st = pkg.step("Idle")
    if pkg.path_for("IdleKeyframe.png").is_file():
        if "IdleKeyframe.png" not in idle_st.outputs:
            idle_st.outputs = list(
                dict.fromkeys(["IdleKeyframe.png", *idle_st.outputs])
            )

    for sid in PIPELINE_STEPS:
        st = pkg.step(sid)
        names = _STEP_FILES.get(sid) or ()
        if any(pkg.path_for(n).is_file() for n in names):
            st.status = StepStatus.APPROVED
            st.error = None
        elif st.status == StepStatus.LOCKED:
            st.status = StepStatus.READY

    if pkg.path_for("Preview.png").is_file():
        pkg.gates["preview"] = GateState.APPROVED
    if any(
        pkg.path_for(n).is_file()
        for n in ("Idle.mp4", "IdleKeyframe.png", "Idle.png")
    ):
        pkg.gates["idle"] = GateState.APPROVED
        pkg.gates["branch"] = GateState.OPEN
        for bid in ("Accept", "Decline", "Adjust"):
            b = pkg.step(bid)
            if b.status == StepStatus.LOCKED:
                b.status = StepStatus.READY
    else:
        pkg.gates["idle"] = GateState.OPEN
        pkg.gates["branch"] = GateState.OPEN

    pkg.final_review_approved = False
    pkg.save()
    return notes


def pull_published_rallies(
    dest_root: Path,
    *,
    tiers: tuple[str, ...] = ("free", "premium"),
    skip_existing: bool = True,
) -> list[str]:
    """
    One-shot sync of free/premium published folders into local rally dirs.

    Folder slug is derived from the S3 display name. Existing local packages
    with meta.json are skipped so in-progress work is not overwritten.
    """
    from app.models.rally import slugify
    from app.pipeline import graph
    from app.pipeline.prompts import refresh_package_composed

    dest_root = Path(dest_root)
    dest_root.mkdir(parents=True, exist_ok=True)
    log: list[str] = []
    bucket = DUTYCALL_S3_BUCKET
    if not bucket:
        raise ValueError("DutyCall S3 bucket is not set")

    file_to_step = {
        "Preview.png": "Preview",
        "Idle.mp4": "Idle",
        "IdleKeyframe.png": "Idle",
        "Rally.mp4": "Rally",
        "Message.mp4": "Message",
        "Accept.mp4": "Accept",
        "Decline.mp4": "Decline",
        "Reschedule.mp4": "Adjust",
    }

    for tier in tiers:
        for display in list_published_rallies(tier):
            slug = slugify(display)
            local = dest_root / slug
            if skip_existing and (local / "meta.json").is_file():
                log.append(f"skip {tier}/{display} (local {slug} exists)")
                continue
            local.mkdir(parents=True, exist_ok=True)
            uri = f"s3://{bucket}/{tier}/rallies/{display}/"
            _run_aws(
                [
                    "aws",
                    "s3",
                    "sync",
                    uri,
                    str(local) + "/",
                    "--exclude",
                    ".DS_Store",
                ]
            )
            pkg = graph.new_rally(
                name=display,
                concept=f"Imported from {tier}/rallies/{display}",
                root=local,
                template="rally",
            )
            pkg.display_name = display
            pkg.publish_tier = tier
            present: set[str] = set()
            for fname, sid in file_to_step.items():
                if (local / fname).is_file():
                    present.add(sid)
                    st = pkg.step(sid)
                    if fname not in st.outputs:
                        st.outputs.append(fname)
            refresh_package_composed(pkg)
            mark_imported_package_ready(pkg)
            pkg.save()
            files = sorted(
                p.name for p in local.iterdir() if p.is_file() and p.name != "meta.json"
            )
            log.append(
                f"pull {tier}/{display} → {slug} ({len(files)} files)"
            )
    return log

