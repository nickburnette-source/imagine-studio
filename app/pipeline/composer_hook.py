"""Optional import overlay for an older local venv hook.

The base modules (studio, prompts, generate, expand) already load the live
implementations. This hook remains so a machine that still has the venv
.pth keeps loading those same files. It never falls through to another repo.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import sys
from pathlib import Path


def _root() -> Path:
    here = Path(__file__).resolve().parents[2]
    cands = [here]
    try:
        cands.append(Path.cwd())
    except OSError:
        pass
    for cand in cands:
        try:
            if (cand / "app/pipeline/expand_next.py").is_file():
                return cand
        except OSError:
            continue
    return here


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
