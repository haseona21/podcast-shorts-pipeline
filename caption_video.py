#!/usr/bin/env python3
"""Burn word-by-word captions into a video using a pre-computed segments JSON.

The segments JSON should come from scripts/transcribe_video.py (it writes a
.json sidecar alongside the .srt). We use a forked copy of Captacity's
add_captions() so we can control vertical position — Captacity 0.3.1 hardcodes
captions to vertical center, which collides with YouTube Shorts' bottom UI.

Usage:
    python scripts/caption_video.py input.mp4 segments.json output.mp4

Brand styling is hardcoded below — edit constants to tune the look.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path


# ---- Brand styling --------------------------------------------------------
# These specs are now driven by environment variables (optionally via a repo-
# root .env file); see config.py. The defaults in config.py equal the values
# that used to be hardcoded here, so with no .env the output is unchanged.
# The module-level names below are kept (sourced from CFG) so the rest of this
# file — including the closure that reads EXTRA_WORD_GAP — is untouched.
from config import CFG  # noqa: E402

_C = CFG.caption

# SERIF font (Georgia regular ships on macOS). A plain .ttf is the most reliable
# path through moviepy/ImageMagick TextClip.
FONT = _C.font
FONT_SIZE = _C.font_size
# cream (not pure white) for a warmer look.
FONT_COLOR = _C.text_color
STROKE_WIDTH = _C.stroke_width
# stroke matches the fill (cream) so the glyphs have NO dark outline.
STROKE_COLOR = _C.stroke_color

# Active-word highlight: WHITE glyph on a solid RED box that moves word-by-word.
HIGHLIGHT_CURRENT_WORD = True
WORD_HIGHLIGHT_COLOR = _C.highlight_color  # crimson box behind the active word

LINE_COUNT = _C.line_count
PADDING = _C.padding

# Extra inter-word breathing room, as a fraction of a space's advance, applied
# identically to the rendered glyph layout (forked create_composite_text) AND to
# the red highlight-box geometry, so the box stays aligned with the active word.
EXTRA_WORD_GAP = _C.word_gap

# Max words visible at once. Captacity splits when fit_function returns False.
MAX_WORDS_PER_CAPTION = _C.max_words_per_caption

# No shadow at all (strength/blur 0 -> no shadow clip composited).
SHADOW_STRENGTH = _C.shadow_strength
SHADOW_BLUR = _C.shadow_blur

# Vertical position of the caption block's CENTER as a fraction of video height.
# 0.78 leaves room below for YouTube Shorts' UI overlay.
POSITION_Y_PERCENT = _C.position_y
# Vertical center used for "stacked" captions (between the two faces).
STACK_POSITION_Y_PERCENT = _C.stack_position_y
# ------------------------------------------------------------------------------


def dedupe_consecutive_words(segments):
    """Remove consecutive duplicate words caused by Whisper chunk overlap.

    Drop word N+1 when it matches N's normalized text AND either (a) starts
    within 0.5s of N's end, or (b) overlaps N in time. Applied across segment
    boundaries.
    """
    import re
    def norm(s):
        return re.sub(r"[^a-z0-9']", "", s.lower())

    prev_w = None
    for seg in segments:
        kept = []
        for w in seg.get("words", []):
            if prev_w is not None:
                same_text = norm(w["word"]) == norm(prev_w["word"])
                if same_text and norm(w["word"]):
                    gap = float(w["start"]) - float(prev_w["end"])
                    overlaps = float(w["start"]) < float(prev_w["end"])
                    if overlaps or gap <= 0.5:
                        # Skip duplicate (merge into prev by extending end if later)
                        if float(w["end"]) > float(prev_w["end"]):
                            prev_w["end"] = w["end"]
                        continue
            kept.append(w)
            prev_w = w
        seg["words"] = kept
    return [s for s in segments if s.get("words")]


def parse_with_breaks(segments, fit_function, break_points):
    """Local replacement for captacity.segment_parser.parse() that also forces
    a caption boundary whenever the next word's start is >= the next unmet
    break point.

    Mirrors captacity 0.3.1 segment_parser.parse exactly except for the extra
    break-point check; if break_points is empty, behavior is identical.
    """
    breaks = sorted(b for b in (break_points or []) if b is not None)
    b_idx = 0

    def has_partial_sentence(text):
        words = text.split()
        if len(words) >= 2 and words[-2].strip()[-1:] == ".":
            return True
        return False

    captions = []
    caption = {"start": None, "end": 0, "words": [], "text": ""}

    # Merge words not separated by spaces (matches upstream)
    for s, segment in enumerate(segments):
        for w, word in enumerate(segment["words"]):
            if w > 0 and word["word"][0] != " ":
                segments[s]["words"][w - 1]["word"] += word["word"]
                segments[s]["words"][w - 1]["end"] = word["end"]
                del segments[s]["words"][w]

    for segment in segments:
        for word in segment["words"]:
            if caption["start"] is None:
                caption["start"] = word["start"]

            text = caption["text"] + word["word"]
            caption_fits = not has_partial_sentence(text)
            caption_fits = caption_fits and fit_function(text)

            # Force a break if this word lands at or past the next break point
            # AND the caption already has at least one word.
            crosses_break = (
                b_idx < len(breaks)
                and caption["words"]
                and word["start"] >= breaks[b_idx]
            )
            if crosses_break:
                # Advance past any breaks this word skips
                while b_idx < len(breaks) and word["start"] >= breaks[b_idx]:
                    b_idx += 1
                caption_fits = False

            if caption_fits:
                caption["words"].append(word)
                caption["end"] = word["end"]
                caption["text"] = text
            else:
                captions.append(caption)
                caption = {
                    "start": word["start"],
                    "end": word["end"],
                    "words": [word],
                    "text": word["word"],
                }

    captions.append(caption)
    return captions


def add_captions_custom(
    video_file,
    output_file,
    segments,
    font,
    font_size,
    font_color,
    stroke_width,
    stroke_color,
    highlight_current_word,
    word_highlight_color,
    line_count,
    padding,
    shadow_strength,
    shadow_blur,
    position_y_percent,
    max_words_per_caption,
    print_info,
    break_points=None,
    layout_timeline=None,
    stacked_y_percent=STACK_POSITION_Y_PERCENT,
):
    """Forked from captacity.add_captions to control vertical position.

    Differences from upstream:
    - text_y_offset is anchored at `position_y_percent * video.h` instead of
      vertical center.
    - Caption grouping uses our local parse_with_breaks() so caption groups
      can be forced to close at specified break points (e.g. clip boundaries).
    """
    from moviepy.editor import VideoFileClip, CompositeVideoClip  # type: ignore
    from captacity import (  # type: ignore
        calculate_lines, create_shadow, fits_frame,
    )
    from captacity.text_drawer import create_text_ex, Word, get_text_size_ex  # type: ignore
    import captacity.text_drawer as _td  # type: ignore
    from PIL import ImageFont as _PILImageFont  # type: ignore
    from moviepy.editor import CompositeVideoClip as _CompositeVideoClip  # type: ignore

    # CUSTOM: fork create_composite_text to add EXTRA_WORD_GAP after each space
    # so words render with more breathing room. Mirrors upstream layout math
    # exactly except for the extra advance applied when a clip is a space.
    def _create_composite_text_wide(text_clips, cfont, cfont_size):
        pf = _PILImageFont.truetype(cfont, cfont_size)
        scale_factor = 3.012
        space_adv = pf.getlength(" ") * scale_factor
        extra = space_adv * EXTRA_WORD_GAP

        full_width = 0.0
        for clip in text_clips[:-1]:
            full_width += pf.getlength(clip.text) * scale_factor
            if clip.text == " ":
                full_width += extra
        full_width += text_clips[-1].size[0]

        clips = []
        offset_x = 0.0
        for clip in text_clips:
            clip.size = (int(full_width), clip.size[1])
            clip = clip.set_position((int(offset_x), 0))
            offset_x += pf.getlength(clip.text) * scale_factor
            if clip.text == " ":
                offset_x += extra
            clips.append(clip)
        return _CompositeVideoClip(clips)

    _td.create_composite_text = _create_composite_text_wide

    _start_time = time.time()

    if print_info:
        print("Extracting audio...")

    import subprocess
    temp_audio_file = tempfile.NamedTemporaryFile(suffix=".wav").name
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_file, temp_audio_file],
        capture_output=True, check=True,
    )

    if print_info:
        print("Generating video elements...")

    video = VideoFileClip(video_file)
    text_bbox_width = video.w - padding * 2
    clips = [video]

    base_fit = fits_frame(
        line_count, font, font_size, stroke_width, text_bbox_width,
    )

    def fit_function(text: str) -> bool:
        if max_words_per_caption and len(text.split()) > max_words_per_caption:
            return False
        return base_fit(text)

    captions = parse_with_breaks(
        segments=segments,
        fit_function=fit_function,
        break_points=break_points,
    )

    for caption in captions:
        captions_to_draw = []
        if highlight_current_word:
            for i, word in enumerate(caption["words"]):
                if i + 1 < len(caption["words"]):
                    end = caption["words"][i + 1]["start"]
                else:
                    end = word["end"]
                captions_to_draw.append({
                    "text": caption["text"],
                    "start": word["start"],
                    "end": end,
                })
        else:
            captions_to_draw.append(caption)

        for current_index, cap in enumerate(captions_to_draw):
            line_data = calculate_lines(
                cap["text"], font, font_size, stroke_width, text_bbox_width,
            )

            # CUSTOM: anchor block center at POSITION_Y_PERCENT instead of 0.5.
            # If a layout_timeline is provided and this caption's time falls in
            # a "stacked" segment, position at stacked_y_percent (vertical
            # center, between the two faces) instead.
            active_y = position_y_percent
            if layout_timeline:
                ct = cap["start"]
                for tl_seg in layout_timeline:
                    if tl_seg["start"] <= ct <= tl_seg["end"]:
                        if tl_seg["layout"] == "stacked":
                            active_y = stacked_y_percent
                        break
            block_center_y = int(video.h * active_y)
            text_y_offset = block_center_y - line_data["height"] // 2

            index = 0
            for line in line_data["lines"]:
                pos = ("center", text_y_offset)
                words = line["text"].split()
                # CUSTOM: line-local index of the active (currently-spoken)
                # word. `current_index` is the active word's position within the
                # whole caption; `index` is how many words preceded this line.
                line_active_index = current_index - index
                word_list = []
                # CUSTOM: keep ALL glyphs white. The active word is highlighted
                # by a red box composited *behind* it (added below), not by
                # recoloring the glyph. This yields white-text-on-red-box.
                for w in words:
                    word_obj = Word(w)
                    word_list.append(word_obj)

                # CUSTOM: build a moving RED highlight box behind the active
                # word. We reproduce captacity.create_composite_text's layout
                # math: the composite (full_width wide) is centered on screen,
                # and word N starts at sum(font.getlength(prior)*scale_factor).
                # We size/place a red ColorClip at that word's box and add it to
                # `clips` BEFORE the text clip so the white glyph sits on top.
                if highlight_current_word and 0 <= line_active_index < len(words):
                    from PIL import ImageFont as _ImageFont
                    from moviepy.editor import ColorClip as _ColorClip
                    _scale = 3.012  # matches create_composite_text
                    _pf = _ImageFont.truetype(font, font_size // 3)
                    # CUSTOM: same extra inter-word advance the forked
                    # create_composite_text applies, so the box stays aligned.
                    _extra_gap = _pf.getlength(" ") * _scale * EXTRA_WORD_GAP

                    # Full composite width = sum of all words+trailing-space
                    # advances except the last word, plus the last word's
                    # actual rendered size (mirrors create_composite_text where
                    # the final clip contributes its true size, not getlength).
                    line_text = line["text"]
                    full_width = 0.0
                    cum = []  # cumulative left-x advance per word (incl leading)
                    running = 0.0
                    for wi, w in enumerate(words):
                        cum.append(running)
                        if wi == len(words) - 1:
                            running += _pf.getlength(w) * _scale
                        else:
                            running += _pf.getlength(w + " ") * _scale + _extra_gap
                    # last word measured by its true rendered width
                    last_w_size = get_text_size_ex(
                        words[-1], font, font_size, stroke_width
                    )[0]
                    full_width = cum[-1] + last_w_size

                    # screen-space left edge of the centered composite
                    comp_left = (video.w - full_width) / 2.0
                    active_w = words[line_active_index]
                    active_left = comp_left + cum[line_active_index]
                    active_w_size = get_text_size_ex(
                        active_w, font, font_size, stroke_width
                    )
                    box_w = int(active_w_size[0])
                    box_h = int(line["height"])
                    pad_x = 14
                    pad_y = 6
                    # parse #RRGGBB highlight color to RGB tuple
                    hc = word_highlight_color.lstrip("#")
                    rgb = tuple(int(hc[i:i + 2], 16) for i in (0, 2, 4))
                    box = _ColorClip(
                        size=(box_w + pad_x * 2, box_h + pad_y * 2), color=rgb
                    )
                    box = box.set_start(cap["start"])
                    box = box.set_duration(cap["end"] - cap["start"])
                    box = box.set_position(
                        (int(active_left - pad_x), int(text_y_offset - pad_y))
                    )
                    clips.append(box)
                index += len(words)

                shadow_left = shadow_strength
                while shadow_left >= 1:
                    shadow_left -= 1
                    shadow = create_shadow(
                        line["text"], font_size, font, shadow_blur, opacity=1,
                    )
                    shadow = shadow.set_start(cap["start"])
                    shadow = shadow.set_duration(cap["end"] - cap["start"])
                    shadow = shadow.set_position(pos)
                    clips.append(shadow)
                if shadow_left > 0:
                    shadow = create_shadow(
                        line["text"], font_size, font, shadow_blur, opacity=shadow_left,
                    )
                    shadow = shadow.set_start(cap["start"])
                    shadow = shadow.set_duration(cap["end"] - cap["start"])
                    shadow = shadow.set_position(pos)
                    clips.append(shadow)

                text = create_text_ex(
                    word_list, font_size, font_color, font,
                    stroke_color=stroke_color, stroke_width=stroke_width,
                )
                text = text.set_start(cap["start"])
                text = text.set_duration(cap["end"] - cap["start"])
                text = text.set_position(pos)
                clips.append(text)

                text_y_offset += line["height"]

    if print_info:
        generation_time = time.time() - _start_time
        print(
            f"Generated in {generation_time//60:02.0f}:{generation_time%60:02.0f} "
            f"({len(clips)} clips)"
        )
        print("Rendering video...")

    video_with_text = CompositeVideoClip(clips)
    video_with_text.write_videofile(
        filename=output_file,
        codec="libx264",
        audio_codec="aac",
        audio_bitrate="192k",
        fps=video.fps,
        logger="bar" if print_info else None,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Input video (mp4)")
    parser.add_argument("segments", type=Path, help="Segments JSON from transcribe_video.py")
    parser.add_argument("output", type=Path, help="Captioned output video")
    parser.add_argument(
        "--break-points", default="",
        help="Comma-separated seconds at which to force a caption-group close. "
             "Use to align caption boundaries with intended clip cut points.",
    )
    parser.add_argument(
        "--layout-timeline", type=Path, default=None,
        help="JSON list of {start, end, layout} — captions in 'stacked' "
             "segments render at vertical center instead of 78%%.",
    )
    args = parser.parse_args()

    break_points = []
    if args.break_points:
        for tok in args.break_points.split(","):
            tok = tok.strip()
            if tok:
                break_points.append(float(tok))

    layout_timeline = None
    if args.layout_timeline and args.layout_timeline.exists():
        layout_timeline = json.loads(args.layout_timeline.read_text())
        print(f"Loaded layout timeline: {len(layout_timeline)} segments")

    if not args.input.exists():
        print(f"error: input video not found: {args.input}", file=sys.stderr)
        return 2
    if not args.segments.exists():
        print(f"error: segments JSON not found: {args.segments}", file=sys.stderr)
        return 2

    try:
        import captacity  # type: ignore
    except ImportError:
        print("error: captacity not installed. Run: pip install captacity", file=sys.stderr)
        return 2

    try:
        resolved_font = captacity.get_font_path(FONT)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    segments = json.loads(args.segments.read_text())
    if not segments:
        print("error: segments JSON is empty — nothing to caption", file=sys.stderr)
        return 2

    before = sum(len(s.get("words", [])) for s in segments)
    segments = dedupe_consecutive_words(segments)
    after = sum(len(s.get("words", [])) for s in segments)
    if before != after:
        print(f"  deduped {before - after} repeated words ({before} → {after})")

    # House style is sentence case (NOT forced ALL-CAPS); preserve the source
    # casing from the word-timed JSON so captions read as normal sentences.
    # (Previously this force-uppercased every word.)

    args.output.parent.mkdir(parents=True, exist_ok=True)

    print(f"Captioning {args.input.name} → {args.output.name}")
    print(f"  font: {resolved_font}  size: {FONT_SIZE}")
    print(f"  y-position: {POSITION_Y_PERCENT*100:.0f}% of height")
    print(f"  segments: {len(segments)} ({sum(len(s['words']) for s in segments)} words)")

    add_captions_custom(
        video_file=str(args.input),
        output_file=str(args.output),
        segments=segments,
        font=resolved_font,
        font_size=FONT_SIZE,
        font_color=FONT_COLOR,
        stroke_width=STROKE_WIDTH,
        stroke_color=STROKE_COLOR,
        highlight_current_word=HIGHLIGHT_CURRENT_WORD,
        word_highlight_color=WORD_HIGHLIGHT_COLOR,
        line_count=LINE_COUNT,
        padding=PADDING,
        shadow_strength=SHADOW_STRENGTH,
        shadow_blur=SHADOW_BLUR,
        position_y_percent=POSITION_Y_PERCENT,
        max_words_per_caption=MAX_WORDS_PER_CAPTION,
        print_info=True,
        break_points=break_points,
        layout_timeline=layout_timeline,
    )
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
