"""Environment health checks."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from app.config import (
    COMPRESS_SCRIPT_IOS,
    DUTYCALL_PRIVATE_FOLDER,
    DUTYCALL_S3_BUCKET,
    GENERATE_BACKEND,
    OUTPUT_DIR,
    S3_BUCKET,
    SKILL_PATH,
    STEP_PROMPTS_PATH,
    XAI_API_KEY,
)
from app import imagine_settings
from app.pipeline import grok_cli


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def run_doctor() -> list[Check]:
    checks: list[Check] = []

    ffmpeg = shutil.which("ffmpeg")
    checks.append(
        Check(
            "ffmpeg",
            bool(ffmpeg),
            ffmpeg or "not found on PATH",
        )
    )

    aws = shutil.which("aws")
    profile = os.getenv("AWS_PROFILE", "") or "default"
    if aws:
        try:
            cmd = ["aws", "sts", "get-caller-identity"]
            if profile and profile != "default":
                cmd.extend(["--profile", profile])
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if r.returncode == 0:
                checks.append(
                    Check(
                        "aws SSO",
                        True,
                        f"profile={profile} · identity ok",
                    )
                )
            else:
                err = (r.stderr or r.stdout or "").strip()
                hint = ""
                if "Token has expired" in err or "refresh failed" in err or "sso" in err.lower():
                    hint = (
                        f" — run: aws sso login --profile {profile}"
                        "  then restart the app (./stop.sh && ./start.sh)"
                    )
                checks.append(
                    Check(
                        "aws SSO",
                        False,
                        f"profile={profile} · {(err[:100] or 'auth failed')}{hint}",
                    )
                )
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("aws SSO", False, f"profile={profile} · {exc}"))
    else:
        checks.append(Check("aws SSO", False, "aws CLI not found on PATH"))

    if GENERATE_BACKEND == "api":
        checks.append(
            Check(
                "XAI_API_KEY",
                bool(XAI_API_KEY),
                "set" if XAI_API_KEY else "missing — required for backend=api",
            )
        )
        checks.append(
            Check(
                "Imagine models",
                True,
                f"image={imagine_settings.image_model()} "
                f"{imagine_settings.image_resolution()} · "
                f"video={imagine_settings.video_model()} "
                f"{imagine_settings.video_resolution()}",
            )
        )
    elif GENERATE_BACKEND == "cli":
        # Generation no longer shells out to `grok` agent — only needs session token
        checks.append(
            Check(
                "grok login session",
                grok_cli.auth_present(),
                (
                    "~/.grok/auth.json present (Imagine bearer)"
                    if grok_cli.auth_present()
                    else "missing — run `grok login` once for credentials"
                ),
            )
        )
        checks.append(
            Check(
                "XAI_API_KEY",
                True,
                (
                    "set — preferred over session for Imagine"
                    if XAI_API_KEY
                    else "unset — using grok session token (no CLI agent)"
                ),
            )
        )
    else:
        checks.append(
            Check(
                "XAI_API_KEY",
                True,
                f"not required for backend={GENERATE_BACKEND}",
            )
        )

    checks.append(
        Check(
            "generate backend",
            GENERATE_BACKEND in ("stub", "cli", "api"),
            GENERATE_BACKEND,
        )
    )
    s = imagine_settings.load()
    checks.append(
        Check(
            "Imagine",
            True,
            f"{s['image_model']} {s['image_resolution']} · "
            f"{s['video_model']} {s['video_resolution']} "
            f"{s.get('video_duration', '6')}s",
        )
    )

    # S3 staging for ZDR video
    try:
        from content_store.config import CONTENT_S3_BUCKET, CONTENT_S3_PREFIX
        from content_store import s3 as s3lib

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
                    "CONTENT_S3_BUCKET not set — video gen will fail",
                )
            )
    except Exception as exc:  # noqa: BLE001
        checks.append(Check("content S3 (video staging)", False, str(exc)))

    checks.append(
        Check(
            "DutyCall drive bucket",
            bool(DUTYCALL_S3_BUCKET),
            DUTYCALL_S3_BUCKET
            or "AWS_DUTYCALL_S3_BUCKET_ARN not set (publish will fail)",
        )
    )
    checks.append(
        Check(
            "Private folder id",
            bool(DUTYCALL_PRIVATE_FOLDER),
            DUTYCALL_PRIVATE_FOLDER
            or "AWS_DUTYCALL_PRIVATE_FOLDER unset (private publish disabled)",
        )
    )
    checks.append(
        Check(
            "S3 bucket (legacy/content)",
            bool(S3_BUCKET),
            S3_BUCKET or "RALLY_S3_BUCKET not set (optional staging)",
        )
    )

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        writable = os.access(OUTPUT_DIR, os.W_OK)
    except OSError as exc:
        writable = False
        detail = str(exc)
    else:
        detail = str(OUTPUT_DIR)
    checks.append(Check("output dir", writable, detail))

    checks.append(
        Check(
            "iOS compress script",
            COMPRESS_SCRIPT_IOS.is_file(),
            str(COMPRESS_SCRIPT_IOS),
        )
    )
    checks.append(
        Check(
            "step prompts",
            STEP_PROMPTS_PATH.is_file(),
            str(STEP_PROMPTS_PATH),
        )
    )
    checks.append(
        Check(
            "SKILL.md",
            SKILL_PATH.is_file(),
            str(SKILL_PATH),
        )
    )
    return checks
