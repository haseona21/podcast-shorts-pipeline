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


# ---- Brand styling (edit to match house look) -------------------------------
FONT = os.path.expanduser("~/Library/Fonts/Montserrat-ExtraBold.ttf")

FONT_SIZE = 64
FONT_COLOR = "white"

STROKE_WIDTH = 3
STROKE_COLOR = "black"

HIGHLIGHT_CURRENT_WORD = True
WORD_HIGHLIGHT_COLOR = "#FFD600"

LINE_COUNT = 1
PADDING = 80

# Max words visible at once. Captacity splits segments when fit_function
# returns False; we return False whenever text exceeds this word count so each
# caption stays short and readable.
MAX_WORDS_PER_CAPTION = 5

SHADOW_STRENGTH = 1.0
SHADOW_BLUR = 0.1

# Vertical position of the caption block's CENTER as a fraction of video height.
# 0.5 = centered (Captacity default). Larger = lower on screen.
# 0.78 leaves room above for the video and below for YouTube Shorts' title/
# username/description overlay (which occupies roughly the bottom 15-20%).
POSITION_Y_PERCENT = 0.78
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
    stacked_y_percent=0.5,
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
    from captacity.text_drawer import create_text_ex, Word  # type: ignore

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
                word_list = []
                for w in words:
                    word_obj = Word(w)
                    if highlight_current_word and index == current_index:
                        word_obj.set_color(word_highlight_color)
                    index += 1
                    word_list.append(word_obj)

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
             "segments render at vertical center instead of 78%.",
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

    # All caps for on-brand readability and to sidestep mid-sentence cap issues.
    for seg in segments:
        for w in seg.get("words", []):
            w["word"] = w["word"].upper()

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
