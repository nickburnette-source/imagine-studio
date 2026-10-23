"""Public import name for concept expansion.

The implementation is expand_next. Importers keep using app.pipeline.expand.
"""

from __future__ import annotations

from app.pipeline import expand_next as _live


def __getattr__(name: str):
    return getattr(_live, name)


def __dir__() -> list[str]:
    return sorted(set(dir(_live)) | set(globals()))
