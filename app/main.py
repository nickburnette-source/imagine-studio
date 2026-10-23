"""Entry point for the Imagine studio."""

from __future__ import annotations

import sys

from app.config import HOST, OUTPUT_DIR, PORT, REPO_ROOT
from app.models.project import recover_interrupted
from app.ui.studio import mount


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    recover_interrupted()

    from nicegui import ui

    mount()
    ui.run(
        title="Imagine",
        host=HOST,
        port=PORT,
        reload=False,
        show=False,
        favicon="✦",
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
