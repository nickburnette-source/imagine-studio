"""Environment health checks for Imagine generation."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app import imagine_settings
from app.config import GENERATE_BACKEND, OUTPUT_DIR, XAI_API_KEY


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _session_present() -> bool:
    auth = Path.home() / ".grok" / "auth.json"
    try:
        return auth.is_file() and auth.stat().st_size > 10
    except OSError:
        return False


def run_doctor() -> list[Check]:
    checks: list[Check] = []
    ffmpeg = shutil.which("ffmpeg")
    checks.append(Check("ffmpeg", bool(ffmpeg), ffmpeg or "not on PATH"))
    ffprobe = shutil.which("ffprobe")
    checks.append(Check("ffprobe", bool(ffprobe), ffprobe or "not on PATH"))

    checks.append(
        Check(
            "generate backend",
            GENERATE_BACKEND in {"stub", "cli", "api"},
            GENERATE_BACKEND,
        )
    )
    if GENERATE_BACKEND == "stub":
        checks.append(
            Check(
                "credentials",
                True,
                "stub writes local placeholders and does not call Imagine",
            )
        )
    elif GENERATE_BACKEND == "api":
        checks.append(
            Check(
                "XAI_API_KEY",
                bool(XAI_API_KEY.strip()),
                "set" if XAI_API_KEY.strip() else "missing — required for backend=api",
            )
        )
    else:
        has_key = bool(XAI_API_KEY.strip())
        has_session = _session_present()
        checks.append(
            Check(
                "Imagine credentials",
                has_key or has_session,
                (
                    "XAI_API_KEY is set"
                    if has_key
                    else (
                        "grok login session in ~/.grok/auth.json"
                        if has_session
                        else "set XAI_API_KEY or run `grok login`"
                    )
                ),
            )
        )

    settings = imagine_settings.load()
    checks.append(
        Check(
            "Imagine models",
            True,
            f"{settings['image_model']} {settings['image_resolution']} · "
            f"{settings['video_model']} {settings['video_resolution']}",
        )
    )

    if GENERATE_BACKEND == "stub":
        checks.append(
            Check(
                "content S3 (video staging)",
                True,
                "not used while backend=stub",
            )
        )
    else:
        try:
            from content_store import s3 as s3lib
            from content_store.config import CONTENT_S3_BUCKET, CONTENT_S3_PREFIX

            if CONTENT_S3_BUCKET:
                try:
                    info = s3lib.ensure_bucket_access()
                    checks.append(
                        Check(
                            "content S3 (video staging)",
                            True,
                            f"{info['bucket']} prefix={CONTENT_S3_PREFIX}",
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    checks.append(
                        Check(
                            "content S3 (video staging)",
                            False,
                            f"{CONTENT_S3_BUCKET}: {exc}",
                        )
                    )
            else:
                checks.append(
                    Check(
                        "content S3 (video staging)",
                        False,
                        "CONTENT_S3_BUCKET is not set — video generation will fail",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("content S3 (video staging)", False, str(exc)))

    profile = os.getenv("AWS_PROFILE", "") or "default"
    aws = shutil.which("aws")
    if GENERATE_BACKEND != "stub":
        if not aws:
            checks.append(Check("aws", False, "aws CLI not on PATH"))
        else:
            cmd = ["aws", "sts", "get-caller-identity"]
            if profile and profile != "default":
                cmd.extend(["--profile", profile])
            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=15
                )
            except Exception as exc:  # noqa: BLE001
                checks.append(Check("aws", False, str(exc)))
            else:
                if result.returncode == 0:
                    checks.append(Check("aws", True, f"profile={profile}"))
                else:
                    err = (result.stderr or result.stdout or "").strip()
                    checks.append(
                        Check("aws", False, f"profile={profile} · {err[:140]}")
                    )

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        writable = os.access(OUTPUT_DIR, os.W_OK)
        detail = str(OUTPUT_DIR)
    except OSError as exc:
        writable = False
        detail = str(exc)
    checks.append(Check("projects dir", writable, detail))
    return checks
