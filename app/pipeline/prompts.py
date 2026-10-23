"""Public import name for the shot prompt compiler.

The implementation is prompts_next. Importers keep using app.pipeline.prompts.
"""

from __future__ import annotations

from app.pipeline import prompts_next as _live


def __getattr__(name: str):
    return getattr(_live, name)


def __dir__() -> list[str]:
    return sorted(set(dir(_live)) | set(globals()))
