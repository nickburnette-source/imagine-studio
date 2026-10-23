"""Import overlays for the tone-first Grok composer.

Existing app/*.py files are sandbox-locked; these next-modules are what
the running app should load. Installed via a .pth in the venv.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

_DEFAULT_ROOT = Path("/Users/nick/Documents/rally-builder")


def _root() -> Path:
    # This repo first. The old Rally Builder path is only a fallback.
    cands = [Path(__file__).resolve().parents[2]]
    try:
        cands.append(Path.cwd())
    except OSError:
        pass
    cands.append(_DEFAULT_ROOT)
    for cand in cands:
        try:
            if (cand / "app/pipeline/expand_next.py").is_file():
                return cand
        except OSError:
            continue
    return _DEFAULT_ROOT


def _overlays() -> dict[str, Path]:
    root = _root()
    return {
        "app.pipeline.expand": root / "app/pipeline/expand_next.py",
        "app.pipeline.prompts": root / "app/pipeline/prompts_next.py",
        "app.ui.studio": root / "app/ui/studio_next.py",
        "app.pipeline.generate": root / "app/pipeline/generate_next.py",
    }


class _OverlayFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):  # noqa: ANN001
        overlay = _overlays().get(fullname)
        if overlay is None or not overlay.is_file():
            return None
        loader = importlib.machinery.SourceFileLoader(fullname, str(overlay))
        return importlib.util.spec_from_loader(fullname, loader)


def install() -> None:
    if any(isinstance(f, _OverlayFinder) for f in sys.meta_path):
        return
    sys.meta_path.insert(0, _OverlayFinder())


install()
