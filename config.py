#!/usr/bin/env python3
"""Env-driven config for the shorts pipeline (caption styling + render geometry).

Values come from environment variables, optionally supplied via a ``.env`` file
in the repo root. A shell export always wins over ``.env``. The ``.env`` parser
is a tiny ``KEY=VALUE`` reader so there's no python-dotenv dependency.

    from config import CFG
    CFG.caption.font     # caption styling
    CFG.render.width     # render geometry
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = REPO_ROOT / ".env"

# Default caption serif. Prefer Georgia (the house-style bakeoff font) when it's
# present as a system font; otherwise fall back to the bundled SIL OFL EB Garamond
# so captions still work on any OS. Override with SHORTS_FONT. See fonts/README.md.
_GEORGIA = "/System/Library/Fonts/Supplemental/Georgia.ttf"
_BUNDLED_SERIF = "fonts/EBGaramond-Regular.ttf"
DEFAULT_FONT = _GEORGIA if Path(_GEORGIA).exists() else _BUNDLED_SERIF

# Named output-format presets: (width, height). Pick one with SHORTS_FORMAT;
# individual SHORTS_WIDTH / SHORTS_HEIGHT still override the preset. The reframe
# crop and the stacked panes derive from these dims, so all four shapes render
# correctly. 16:9 yields a full-width crop (= original framing, no vertical
# reframe), so the same code path covers vertical and landscape.
FORMATS = {
    "youtube_9x16": (1080, 1920),   # vertical short (default)
    "linkedin_4x5": (1080, 1350),   # portrait
    "square_1x1": (1080, 1080),     # square
    "linkedin_16x9": (1920, 1080),  # landscape
}
DEFAULT_FORMAT = "youtube_9x16"

# Caption styles (a base style + per-platform overrides) live as data in
# styles/styles.json, so a posting spot's look — especially its font — can change
# without code edits. Selected with SHORTS_PLATFORM; unset = base only.
STYLES_PATH = REPO_ROOT / "styles" / "styles.json"

# Two-stage clip-seek preroll (seconds). A clip cut fast-seeks to a keyframe this
# far before the target start, then decode-then-seeks the remainder — frame-exact
# without decoding the whole preceding source. The render cut (splice/stack.py)
# and the transcribe cut (caption/transcribe.py) MUST share this value so the
# transcribed audio stays aligned with the rendered clip.
SEEK_PREROLL = 10.0


# ---------------------------------------------------------------------------
# .env loading (dependency-free)
# ---------------------------------------------------------------------------

def load_dotenv(path: Path = DEFAULT_ENV_PATH) -> None:
    """Load ``KEY=VALUE`` lines from ``path`` into ``os.environ``.

    Ignores blanks and ``#`` comments, tolerates a leading ``export``, strips
    surrounding quotes, and never overwrites a key already in ``os.environ`` (a
    shell export wins). Missing file is a no-op.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        val = val.strip()
        # Strip a trailing inline comment, but keep a '#' that's part of a color
        # literal like #B11226 — only ' #' (space then hash) is a comment.
        if val[:1] not in ("'", '"'):
            hash_at = val.find(" #")
            if hash_at != -1:
                val = val[:hash_at].rstrip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key not in os.environ:
            os.environ[key] = val


# ---------------------------------------------------------------------------
# caption styles (base + per-platform overrides, dependency-free)
# ---------------------------------------------------------------------------

def load_styles(path: Path = STYLES_PATH) -> dict:
    """Load caption styles from styles/styles.json.

    Returns ``{"base": {...}, "platforms": {name: {...}}}``. A missing or empty
    file yields empty dicts, so the built-in defaults govern and behavior is
    unchanged (backward-compatible).
    """
    import json
    if not path.exists():
        return {"base": {}, "platforms": {}}
    data = json.loads(path.read_text()) or {}
    return {
        "base": data.get("base") or {},
        "platforms": data.get("platforms") or {},
    }


def resolve_style(platform: str = "") -> dict:
    """Merge the base style with the selected platform's override (override wins).

    ``platform`` comes from SHORTS_PLATFORM; an empty/unknown platform returns the
    base style alone. Per-field env vars (SHORTS_<FIELD>) still override this at
    build time in ``load_config``.
    """
    styles = load_styles()
    base = styles["base"]
    override = styles["platforms"].get(platform, {}) if platform else {}
    return {**base, **override}


# ---------------------------------------------------------------------------
# typed getters
# ---------------------------------------------------------------------------

def _get_str(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v is not None and v != "" else default


def _get_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return int(v)


def _get_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return float(v)


# ---------------------------------------------------------------------------
# font resolution
# ---------------------------------------------------------------------------

def resolve_font(font: str = "") -> str:
    """Resolve the caption font to an existing .ttf/.ttc path.

    Accepts an absolute path or a path relative to the repo root. Falls back to
    SHORTS_FONT / the bundled default. Raises FileNotFoundError with a friendly
    message if nothing resolves, so callers don't crash cryptically deep in PIL.
    """
    candidate = font or _get_str("SHORTS_FONT", DEFAULT_FONT)
    p = Path(candidate)
    tries = [p] if p.is_absolute() else [p, REPO_ROOT / p]
    for t in tries:
        if t.exists():
            return str(t)
    raise FileNotFoundError(
        f"Caption font not found: {candidate!r}.\n"
        f"Set SHORTS_FONT to a .ttf/.ttc file, e.g. "
        f"SHORTS_FONT=fonts/YourFont.ttf, or drop a font into the fonts/ "
        f"directory. A bundled default ({DEFAULT_FONT}) ships with the repo."
    )


# ---------------------------------------------------------------------------
# config schema (defaults == current hardcoded values)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CaptionConfig:
    font: str
    font_size: int
    text_color: str
    stroke_color: str
    stroke_width: int
    highlight_color: str
    word_gap: float
    position_y: float
    stack_position_y: float
    shadow_strength: float
    shadow_blur: float
    max_words_per_line: int
    max_chars: int
    padding: int
    line_count: int
    # "max words visible at once" in caption/burn.py (distinct from the
    # group_words per-line cap).
    max_words_per_caption: int


@dataclass(frozen=True)
class RenderConfig:
    width: int
    height: int
    fps: int
    crf: int
    stack_crop_w: int
    stack_crop_h: int
    pane_w: int
    pane_h: int


@dataclass(frozen=True)
class Config:
    caption: CaptionConfig
    render: RenderConfig


def load_config() -> Config:
    """Load .env (if present) then build the typed config from env/defaults."""
    load_dotenv()

    # Caption style: base <- selected platform override (<- per-field env vars below).
    style = resolve_style(_get_str("SHORTS_PLATFORM", ""))

    text_color = _get_str("SHORTS_TEXT_COLOR", style.get("text_color", "#F5EFE0"))
    caption = CaptionConfig(
        font=_get_str("SHORTS_FONT", style.get("font") or DEFAULT_FONT),
        font_size=_get_int("SHORTS_FONT_SIZE", int(style.get("font_size", 64))),
        text_color=text_color,
        # stroke defaults to the text color (no dark outline)
        stroke_color=_get_str("SHORTS_STROKE_COLOR", style.get("stroke_color", text_color)),
        stroke_width=_get_int("SHORTS_STROKE_WIDTH", int(style.get("stroke_width", 3))),
        highlight_color=_get_str("SHORTS_HIGHLIGHT_COLOR", style.get("highlight_color", "#B11226")),
        word_gap=_get_float("SHORTS_WORD_GAP", float(style.get("word_gap", 0.35))),
        position_y=_get_float("SHORTS_POSITION_Y", float(style.get("position_y", 0.78))),
        stack_position_y=_get_float("SHORTS_STACK_POSITION_Y", float(style.get("stack_position_y", 0.5))),
        shadow_strength=_get_float("SHORTS_SHADOW_STRENGTH", float(style.get("shadow_strength", 0.0))),
        shadow_blur=_get_float("SHORTS_SHADOW_BLUR", float(style.get("shadow_blur", 0.0))),
        max_words_per_line=_get_int("SHORTS_MAX_WORDS_PER_LINE", int(style.get("max_words_per_line", 7))),
        max_chars=_get_int("SHORTS_MAX_CHARS", int(style.get("max_chars", 32))),
        padding=_get_int("SHORTS_PADDING", int(style.get("padding", 80))),
        line_count=_get_int("SHORTS_LINE_COUNT", int(style.get("line_count", 1))),
        max_words_per_caption=_get_int("SHORTS_MAX_WORDS_PER_CAPTION", int(style.get("max_words_per_caption", 5))),
    )
    fmt = _get_str("SHORTS_FORMAT", DEFAULT_FORMAT)
    fmt_w, fmt_h = FORMATS.get(fmt, FORMATS[DEFAULT_FORMAT])
    width = _get_int("SHORTS_WIDTH", fmt_w)
    height = _get_int("SHORTS_HEIGHT", fmt_h)
    render = RenderConfig(
        width=width,
        height=height,
        fps=_get_int("SHORTS_FPS", 24),
        crf=_get_int("SHORTS_CRF", 18),
        stack_crop_w=_get_int("SHORTS_STACK_CROP_W", 1215),
        stack_crop_h=_get_int("SHORTS_STACK_CROP_H", 1080),
        # stacked panes derive from the output frame: two panes vstack to height
        pane_w=_get_int("SHORTS_PANE_W", width),
        pane_h=_get_int("SHORTS_PANE_H", height // 2),
    )
    return Config(caption=caption, render=render)


CFG = load_config()
