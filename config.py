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

# Bundled SIL OFL serif (EB Garamond), so captions work out-of-the-box on any
# OS. Override with SHORTS_FONT. See fonts/README.md.
DEFAULT_FONT = "fonts/EBGaramond-Regular.ttf"


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

    text_color = _get_str("SHORTS_TEXT_COLOR", "#F5EFE0")
    caption = CaptionConfig(
        font=_get_str("SHORTS_FONT", DEFAULT_FONT),
        font_size=_get_int("SHORTS_FONT_SIZE", 64),
        text_color=text_color,
        # stroke defaults to the text color (no dark outline)
        stroke_color=_get_str("SHORTS_STROKE_COLOR", text_color),
        stroke_width=_get_int("SHORTS_STROKE_WIDTH", 3),
        highlight_color=_get_str("SHORTS_HIGHLIGHT_COLOR", "#B11226"),
        word_gap=_get_float("SHORTS_WORD_GAP", 0.35),
        position_y=_get_float("SHORTS_POSITION_Y", 0.78),
        stack_position_y=_get_float("SHORTS_STACK_POSITION_Y", 0.5),
        shadow_strength=_get_float("SHORTS_SHADOW_STRENGTH", 0.0),
        shadow_blur=_get_float("SHORTS_SHADOW_BLUR", 0.0),
        max_words_per_line=_get_int("SHORTS_MAX_WORDS_PER_LINE", 7),
        max_chars=_get_int("SHORTS_MAX_CHARS", 32),
        padding=_get_int("SHORTS_PADDING", 80),
        line_count=_get_int("SHORTS_LINE_COUNT", 1),
        max_words_per_caption=_get_int("SHORTS_MAX_WORDS_PER_CAPTION", 5),
    )
    render = RenderConfig(
        width=_get_int("SHORTS_WIDTH", 1080),
        height=_get_int("SHORTS_HEIGHT", 1920),
        fps=_get_int("SHORTS_FPS", 24),
        crf=_get_int("SHORTS_CRF", 18),
        stack_crop_w=_get_int("SHORTS_STACK_CROP_W", 1215),
        stack_crop_h=_get_int("SHORTS_STACK_CROP_H", 1080),
        pane_w=_get_int("SHORTS_PANE_W", 1080),
        pane_h=_get_int("SHORTS_PANE_H", 960),
    )
    return Config(caption=caption, render=render)


CFG = load_config()
