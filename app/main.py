"""Entry point for Imagine Studio."""

from __future__ import annotations

import sys

from app.config import HOST, OUTPUT_DIR, PORT, REPO_ROOT
from app.ui.studio import mount


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Ensure repo root is importable when run as script
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    from nicegui import ui

    mount()
    ui.run(
        title="Imagine Studio",
        host=HOST,
        port=PORT,
        reload=False,
        # Don't open a browser tab on every restart; start.sh reuses one.
        show=False,
        favicon="🎮",
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
