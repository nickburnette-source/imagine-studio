"""Public import name for clip generation.

The implementation is generate_next. Importers keep using app.pipeline.generate.
"""

from __future__ import annotations

from app.pipeline import generate_next as _live


def __getattr__(name: str):
    return getattr(_live, name)


def __dir__() -> list[str]:
    return sorted(set(dir(_live)) | set(globals()))
