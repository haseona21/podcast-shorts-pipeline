#!/usr/bin/env python3
"""Centralized, env-var-driven configuration for the shorts pipeline.

The production "specs" — caption styling and render geometry — used to live as
hardcoded literals inside ``caption_video.py``, ``render_short.py``, and
``transcribe_captions.py``. They now live here, driven by environment variables
that can optionally be supplied via a ``.env`` file in the repo root.

Design constraints:
  * **Defaults equal the current winning values.** With NO ``.env`` file and no
    env vars set, every value below equals the literal it replaced, so output
    is byte-for-byte identical to before this change.
  * **No third-party dependency.** We ship a tiny ``KEY=VALUE`` parser instead
    of pulling in python-dotenv. The parser never overwrites a variable that is
    already present in ``os.environ`` (so a real shell export wins over .env).

Usage::

    from config import CFG
    CFG.caption.font          # caption styling
    CFG.render.width          # render geometry
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = REPO_ROOT / ".env"


# ---------------------------------------------------------------------------
# .env loading (dependency-free)
# ---------------------------------------------------------------------------

def load_dotenv(path: Path = DEFAULT_ENV_PATH) -> None:
    """Load ``KEY=VALUE`` lines from ``path`` into ``os.environ``.

    * Blank lines and lines whose first non-space char is ``#`` are ignored.
    * A leading ``export `` prefix is tolerated.
    * Surrounding single/double quotes around the value are stripped.
    * Does NOT overwrite a key already present in ``os.environ`` (a real shell
      export wins). Missing file is a silent no-op.
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
        # strip a trailing inline comment only if the value is unquoted
        if val[:1] not in ("'", '"'):
            # keep '#' that is part of a color literal like #B11226 — only treat
            # ' #' (space then hash) as a comment delimiter.
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
    # caption_video.py's own "max words visible at once" knob (distinct from the
    # group_words per-line cap); kept env-driven so output stays identical.
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
        font=_get_str("SHORTS_FONT", "/System/Library/Fonts/Supplemental/Georgia.ttf"),
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
