"""Post-process Explicit-E badge on Preview.png.

The stage shows Preview.png. Imagine edits that file. When the E overlay is
on, Preview.clean.png is the same photo without the badge, and Imagine uses
clean so the letter is not baked into the next still. When the E is off,
clean can be an older picture — Imagine must not prefer it over the stage.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.models.rally import RallyPackage

CLEAN_NAME = "Preview.clean.png"
DISPLAY_NAME = "Preview.png"

# DutyCall packs (C130 / Humvee): top-left square ~31% of width, ~1.2% inset.
_MARGIN_RATIO = 0.012
_SIDE_RATIO = 0.312
_LETTER_PAD_RATIO = 0.16

_PRESETS: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "black": ((0, 0, 0), (245, 245, 245)),
    "white": ((209, 209, 209), (20, 18, 16)),
}

_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/Library/Fonts/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)


class ExplicitMarkError(RuntimeError):
    pass


def preview_source_path(pkg: RallyPackage) -> Path:
    """Still Imagine should edit or animate. Matches the stage when E is off."""
    display = pkg.path_for(DISPLAY_NAME)
    clean = pkg.path_for(CLEAN_NAME)
    e_on = False
    try:
        e_on = bool(pkg.step("Preview").options.get("explicit_e"))
    except Exception:  # noqa: BLE001
        e_on = False
    if e_on and clean.is_file():
        return clean
    if display.is_file():
        return display
    return clean


def as_imagine_still(pkg: RallyPackage, path: Path | None) -> Path | None:
    """If ``path`` is the Preview display file, swap in the clean source."""
    if path is None:
        return None
    if path.name in {DISPLAY_NAME, CLEAN_NAME}:
        src = preview_source_path(pkg)
        return src if src.is_file() else path
    return path


def is_clean_preview_name(name: str) -> bool:
    n = Path(name).name
    return n == CLEAN_NAME or n.startswith("Preview.clean.")


def preview_display_history_name(clean_name: str) -> str | None:
    """Preview.clean.STAMP.png → Preview.STAMP.png"""
    n = Path(clean_name).name
    if not n.startswith("Preview.clean."):
        return None
    return "Preview." + n[len("Preview.clean.") :]


def parse_hex_color(raw: str) -> tuple[int, int, int] | None:
    s = (raw or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return None
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return None


def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _contrast_letter(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
    r, g, b = rgb
    lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
    return (20, 18, 16) if lum > 0.55 else (245, 245, 245)


def _square_and_letter(
    style: str, hex_color: str
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    custom = parse_hex_color(hex_color)
    if custom is not None:
        return custom, _contrast_letter(custom)
    key = (style or "black").strip().lower()
    if key not in _PRESETS:
        key = "black"
    return _PRESETS[key]


def stamp_explicit_e(
    src: Path,
    dest: Path,
    *,
    style: str = "black",
    hex_color: str = "",
) -> Path:
    """Draw a top-left E badge onto a copy of ``src``. Does not modify ``src``."""
    src = Path(src)
    dest = Path(dest)
    if not src.is_file():
        raise ExplicitMarkError(f"Missing still: {src}")
    img = Image.open(src).convert("RGBA")
    w, h = img.size
    margin = max(8, round(w * _MARGIN_RATIO))
    side = max(64, round(w * _SIDE_RATIO))
    if margin + side > w:
        side = max(64, w - 2 * margin)
    if margin + side > h:
        side = max(64, h - 2 * margin)
    square, letter = _square_and_letter(style, hex_color)

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    x0, y0 = margin, margin
    x1, y1 = x0 + side - 1, y0 + side - 1
    draw.rectangle([x0, y0, x1, y1], fill=(*square, 255))

    pad = max(6, round(side * _LETTER_PAD_RATIO))
    max_w = max(8, side - 2 * pad)
    max_h = max(8, side - 2 * pad)
    lo, hi = 8, side
    best = lo
    for _ in range(14):
        mid = (lo + hi + 1) // 2
        font = _font(mid)
        bbox = draw.textbbox((0, 0), "E", font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= max_w and th <= max_h:
            best = mid
            lo = mid
        else:
            hi = mid - 1
        if lo >= hi:
            break
    font = _font(best)
    cx = x0 + side / 2
    cy = y0 + side / 2
    draw.text((cx, cy), "E", font=font, fill=(*letter, 255), anchor="mm")

    out = Image.alpha_composite(img, overlay).convert("RGB")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".stamp-tmp.png")
    out.save(tmp, format="PNG")
    tmp.replace(dest)
    return dest


def snapshot_clean_preview(pkg: RallyPackage) -> Path | None:
    """Copy live Preview.png → Preview.clean.png. Call before stamping."""
    preview = pkg.path_for(DISPLAY_NAME)
    if not preview.is_file():
        return None
    clean = pkg.path_for(CLEAN_NAME)
    shutil.copy2(preview, clean)
    return clean


def apply_explicit_mark(pkg: RallyPackage) -> str:
    """Rebuild Preview.png from the clean still ± E. Never calls Imagine."""
    st = pkg.step("Preview")
    preview = pkg.path_for(DISPLAY_NAME)
    clean = pkg.path_for(CLEAN_NAME)
    enabled = bool(st.options.get("explicit_e"))
    style = str(st.options.get("explicit_e_style") or "black")
    hex_color = str(st.options.get("explicit_e_hex") or "")

    if not clean.is_file():
        if not preview.is_file():
            st.options["explicit_e_applied"] = False
            return "No Preview still to stamp"
        shutil.copy2(preview, clean)

    if not enabled:
        if clean.resolve() != preview.resolve():
            shutil.copy2(clean, preview)
        st.options["explicit_e_applied"] = False
        return "Preview.png is the clean still (E off)"

    stamp_explicit_e(clean, preview, style=style, hex_color=hex_color)
    st.options["explicit_e_applied"] = True
    return "E stamped on Preview.png — videos still use the clean still"


def finish_preview_generate(pkg: RallyPackage) -> str:
    """After Imagine writes Preview.png: keep a clean copy, then stamp if on."""
    if snapshot_clean_preview(pkg) is None:
        return "No Preview.png to keep as clean still"
    return apply_explicit_mark(pkg)


def clean_history_sibling(hist_path: Path) -> Path | None:
    """history/Preview.STAMP.png → history/Preview.clean.STAMP.png if present."""
    hist_path = Path(hist_path)
    name = hist_path.name
    if name.startswith("Preview.clean."):
        return hist_path if hist_path.is_file() else None
    if name.startswith("Preview.") and name.lower().endswith(".png"):
        sibling = hist_path.with_name("Preview.clean." + name[len("Preview.") :])
        return sibling if sibling.is_file() else None
    return None
