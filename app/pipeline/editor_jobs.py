"""In-memory jobs started from the full-screen editor.

Several clip jobs can run on one step. Each one owns a placeholder on
the timeline. Rally Make still waits, so it does not write the same
take while those clips are rendering.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

_STUDIO_BUSY = None


def set_studio_busy(fn) -> None:
    """Studio reports its own Make / Nudge tasks. Must not call back here."""
    global _STUDIO_BUSY
    _STUDIO_BUSY = fn


@dataclass
class EditorJob:
    id: str
    rally: str
    step: str
    scene_id: str = ""
    running: bool = True
    detail: str = "Queued…"
    error: str = ""
    result: dict | None = None
    task: object = field(default=None, repr=False)


_jobs: dict[str, EditorJob] = {}


def _key(rally: str, step: str) -> str:
    return f"{rally}/{step}"


def _studio_busy(rally: str, step: str) -> bool:
    fn = _STUDIO_BUSY
    if fn is None:
        return False
    try:
        return bool(fn(rally, step))
    except Exception:  # noqa: BLE001
        return False


def _step_jobs(rally: str, step: str) -> list[EditorJob]:
    return [job for job in _jobs.values() if job.rally == rally and job.step == step]


def local_busy(rally: str, step: str) -> bool:
    return any(job.running for job in _step_jobs(rally, step))


def busy(rally: str, step: str) -> bool:
    if local_busy(rally, step):
        return True
    return _studio_busy(rally, step)


def running_jobs() -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for job in _jobs.values():
        if not job.running:
            continue
        key = _key(job.rally, job.step)
        if key in seen:
            continue
        seen.add(key)
        out.append((key, job.rally, job.step))
    return out


def detail_for(key: str) -> str:
    rally, _, step = key.partition("/")
    running = [job for job in _step_jobs(rally, step) if job.running]
    if not running:
        return ""
    if len(running) == 1:
        return running[0].detail or ""
    return f"{len(running)} clips generating"


def detail_for_scene(rally: str, step: str, scene_id: str) -> str:
    scene_id = str(scene_id or "")
    for job in _step_jobs(rally, step):
        if job.scene_id == scene_id and job.running:
            return job.detail or "Queued…"
    return ""


def public_state(rally: str, step: str) -> dict:
    jobs = _step_jobs(rally, step)
    rows = [
        {
            "id": job.id,
            "sceneId": job.scene_id,
            "running": job.running,
            "detail": job.detail,
            "error": job.error,
            "result": job.result,
        }
        for job in jobs
    ]
    running = [job for job in jobs if job.running]
    latest = jobs[-1] if jobs else None
    return {
        "running": bool(running),
        "id": running[-1].id if running else (latest.id if latest else ""),
        "detail": detail_for(_key(rally, step)),
        "error": "",
        "result": None,
        "jobs": rows,
    }


def begin(rally: str, step: str, scene_id: str = "") -> EditorJob:
    if _studio_busy(rally, step):
        raise RuntimeError("Already generating this step")
    job = EditorJob(
        id=uuid.uuid4().hex[:12],
        rally=rally,
        step=step,
        scene_id=str(scene_id or ""),
    )
    _jobs[job.id] = job
    return job


def set_detail(job_id: str, text: str) -> None:
    job = _jobs.get(job_id)
    if job is not None and job.running:
        job.detail = (text or "")[:240]


def abandon(job_id: str) -> None:
    """Drop a job that never started, so a rejected request leaves no error."""
    job = _jobs.get(job_id)
    if job is None:
        return
    if job.running and job.task is None:
        _jobs.pop(job_id, None)


def finish(
    job_id: str,
    *,
    error: str = "",
    result: dict | None = None,
) -> None:
    job = _jobs.get(job_id)
    if job is None:
        return
    job.running = False
    job.error = (error or "")[:400]
    job.result = result
    if error:
        job.detail = job.error
    elif result and result.get("note"):
        job.detail = str(result["note"])[:240]
    else:
        job.detail = "Done"
