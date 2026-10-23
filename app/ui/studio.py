"""Public import name for the Imagine Studio page.

A normal install loads this module. The editor itself is studio_next, so a
clone does not need a venv import hook.
"""

from __future__ import annotations

from app.ui import studio_next as _live


def __getattr__(name: str):
    return getattr(_live, name)


def __dir__() -> list[str]:
    return sorted(set(dir(_live)) | set(globals()))
