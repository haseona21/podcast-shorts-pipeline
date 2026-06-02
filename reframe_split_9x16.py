#!/usr/bin/env python3
"""Reframe a side-by-side 16:9 recording to dynamic 9:16 with speaker-aware layout.

Source assumption: 1920x1080 with left half = person A, right half = person B.
Output: 1080x1920 mp4.

Layout rules:
- Contiguous speaker run >= 5s -> solo: crop to that half, scale-and-crop to 9:16.
- Rapid alternation (any run < 5s) -> stacked split: top = left half, bottom = right half.

Speaker detection: MediaPipe FaceLandmarker 478-point mesh run on each half
separately, sampling at SAMPLE_FPS. Mouth openness = distance between inner
upper/lower lip landmarks (13 / 14), normalized by inter-eye distance.

Usage:
    python scripts/reframe_split_9x16.py input.mp4 output.mp4 \\
        [--sample-fps 5] [--solo-threshold 5.0]

Output is written atomically via a sibling .part file, then renamed on success.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import cv2  # type: ignore
import mediapipe as mp  # type: ignore
import numpy as np  # type: ignore
from mediapipe.tasks import python as mp_python  # type: ignore
from mediapipe.tasks.python import vision as mp_vision  # type: ignore


LANDMARKER_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
    "face_landmarker/float16/1/face_landmarker.task"
)
LANDMARKER_CACHE = (
    Path.home() / ".cache" / "mediapipe" / "face_landmarker.task"
)

# Landmark indices for mouth / eyes in the 478-point MediaPipe mesh.
LIP_UPPER = 13
LIP_LOWER = 14
EYE_R = 33
EYE_L = 263

# Face landmarker returns mouth openness as one of its blendshapes; but we'd
# need to enable blendshapes. Simpler to compute directly from landmarks.


@dataclass
class Sample:
    t: float           # timestamp in seconds
    left_open: float   # left-half speaker mouth openness (normalized)
    right_open: float  # right-half speaker mouth openness (normalized)


@dataclass
class Segment:
    start: float
    end: float
    layout: str  # "solo_left" | "solo_right" | "stacked"


def ensure_landmarker_model() -> Path:
    if LANDMARKER_CACHE.exists() and LANDMARKER_CACHE.stat().st_size > 0:
        return LANDMARKER_CACHE
    LANDMARKER_CACHE.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading face landmarker model → {LANDMARKER_CACHE}")
    urllib.request.urlretrieve(LANDMARKER_URL, LANDMARKER_CACHE)
    return LANDMARKER_CACHE


def probe_dims(path: Path) -> tuple[int, int, float, float]:
    out = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ])
    j = json.loads(out)
    s = j["streams"][0]
    w, h = int(s["width"]), int(s["height"])
    num, den = s["r_frame_rate"].split("/")
    fps = float(num) / float(den) if float(den) != 0 else 24.0
    dur = float(j["format"]["duration"])
    return w, h, fps, dur


def mouth_openness(landmarks, img_h: int) -> Optional[float]:
    """Return normalized mouth openness, or None if face not detected well."""
    if not landmarks:
        return None
    try:
        up = landmarks[LIP_UPPER]
        lo = landmarks[LIP_LOWER]
        er = landmarks[EYE_R]
        el = landmarks[EYE_L]
    except IndexError:
        return None
    eye_dist = ((er.x - el.x) ** 2 + (er.y - el.y) ** 2) ** 0.5
    if eye_dist < 1e-4:
        return None
    lip_dist = ((up.x - lo.x) ** 2 + (up.y - lo.y) ** 2) ** 0.5
    return lip_dist / eye_dist


def sample_speakers(
    video: Path, sample_fps: float, model_path: Path, duration: float
) -> list[Sample]:
    """Run MediaPipe FaceLandmarker on each half at sample_fps; return samples."""
    base_options = mp_python.BaseOptions(model_asset_path=str(model_path))
    options = mp_vision.FaceLandmarkerOptions(
        base_options=base_options,
        num_faces=1,
        running_mode=mp_vision.RunningMode.IMAGE,
    )
    landmarker = mp_vision.FaceLandmarker.create_from_options(options)

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open {video}")

    src_fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    step = max(int(round(src_fps / sample_fps)), 1)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    samples: list[Sample] = []
    frame_idx = 0
    last_log = 0.0

    while True:
        # Seek to next sample frame
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()
        if not ok:
            break
        h, w = frame.shape[:2]
        mid = w // 2
        left_half = frame[:, :mid]
        right_half = frame[:, mid:]

        left_open = None
        right_open = None
        for which, half in (("L", left_half), ("R", right_half)):
            rgb = cv2.cvtColor(half, cv2.COLOR_BGR2RGB)
            mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = landmarker.detect(mp_img)
            if result.face_landmarks:
                o = mouth_openness(result.face_landmarks[0], half.shape[0])
                if which == "L":
                    left_open = o
                else:
                    right_open = o

        t = frame_idx / src_fps
        samples.append(Sample(
            t=t,
            left_open=left_open if left_open is not None else 0.0,
            right_open=right_open if right_open is not None else 0.0,
        ))

        if t - last_log >= 30:
            pct = 100.0 * frame_idx / total if total else 0.0
            print(f"  speaker-detect: {t:.0f}s / {duration:.0f}s ({pct:.0f}%)")
            last_log = t

        frame_idx += step

    cap.release()
    landmarker.close()
    return samples


def smooth(values: list[float], window: int) -> list[float]:
    """Rolling median smoothing."""
    out = []
    half = window // 2
    for i in range(len(values)):
        lo = max(0, i - half)
        hi = min(len(values), i + half + 1)
        out.append(statistics.median(values[lo:hi]))
    return out


def classify(samples: list[Sample], sample_fps: float) -> list[str]:
    """Per-sample label: 'L' or 'R' (no silent state — always pick the side
    with more mouth opening). Smoothed over ~0.5s.
    """
    window = max(3, int(round(0.5 * sample_fps)))  # ~0.5s smoothing
    left_s = smooth([s.left_open for s in samples], window)
    right_s = smooth([s.right_open for s in samples], window)

    labels = []
    for l, r in zip(left_s, right_s):
        labels.append("L" if l >= r else "R")
    return labels


def sticky_fill(labels: list[str]) -> list[str]:
    """No-op now — classify() never emits 'silent'."""
    return labels


def count_words_per_side(
    words: list[dict],
    t_start: float,
    t_end: float,
    labels: list[str],
    times: list[float],
) -> tuple[int, int]:
    """For words within [t_start, t_end], count how many align with each
    side based on the speaker label at the word's start time.
    """
    if not words:
        return (0, 0)
    left = 0
    right = 0
    for w in words:
        ws = float(w["start"])
        if ws < t_start or ws > t_end:
            continue
        # Find label index for this timestamp
        # labels[i] corresponds to times[i]
        idx = min(
            range(len(times)),
            key=lambda i: abs(times[i] - ws),
        )
        if labels[idx] == "L":
            left += 1
        else:
            right += 1
    return (left, right)


def build_segments(
    labels: list[str],
    times: list[float],
    solo_threshold: float,
    duration: float,
    transcript_words: Optional[list[dict]] = None,
    min_minority_words: int = 3,
) -> list[Segment]:
    """Group contiguous labels into runs; apply the 5-second rule."""
    # First, compute runs of same label
    runs: list[tuple[str, float, float]] = []  # (label, start_t, end_t)
    i = 0
    while i < len(labels):
        j = i
        while j + 1 < len(labels) and labels[j + 1] == labels[i]:
            j += 1
        runs.append((labels[i], times[i], times[j + 1] if j + 1 < len(times) else duration))
        i = j + 1

    # Suppress micro-flicker: drop runs shorter than 0.3s by extending the
    # previous run through them. Do NOT fold short opposite-speaker runs into
    # the previous segment — that delays the visual switch.
    merged: list[tuple[str, float, float]] = []
    for lab, s, e in runs:
        if merged and (e - s) < 0.3:
            # Tiny run of either label — absorbed into previous (anti-flicker).
            prev = merged[-1]
            merged[-1] = (prev[0], prev[1], e)
        elif merged and merged[-1][0] == lab:
            prev = merged[-1]
            merged[-1] = (prev[0], prev[1], e)
        else:
            merged.append((lab, s, e))

    # Apply stacked rule:
    # - Runs >= solo_threshold → solo layout
    # - Two or more consecutive short runs (rapid trade) → stacked
    # - An isolated short run (one short run between two longs) → absorb into
    #   the longer adjacent run of its opposite label (the interrupted speaker)
    segs: list[Segment] = []
    i = 0
    while i < len(merged):
        lab, s, e = merged[i]
        if (e - s) >= solo_threshold:
            segs.append(Segment(s, e, "solo_left" if lab == "L" else "solo_right"))
            i += 1
            continue

        # Short run — look at consecutive short runs
        j = i
        while j + 1 < len(merged) and (merged[j + 1][2] - merged[j + 1][1]) < solo_threshold:
            j += 1
        # merged[i:j+1] are all short runs
        short_count = j - i + 1

        if short_count >= 2:
            zone_start = merged[i][1]
            zone_end = merged[j][2]
            # Gate stacked on actual transcribed words per side — if the
            # minority side has < min_minority_words in the window, the short
            # "runs" are likely just mouth artifacts; fall back to solo of
            # the majority speaker instead.
            layout = "stacked"
            if transcript_words is not None:
                left_w, right_w = count_words_per_side(
                    transcript_words, zone_start, zone_end, labels, times,
                )
                minority = min(left_w, right_w)
                if minority < min_minority_words:
                    majority_side = "L" if left_w >= right_w else "R"
                    layout = "solo_left" if majority_side == "L" else "solo_right"
            segs.append(Segment(zone_start, zone_end, layout))
            i = j + 1
        else:
            # Single isolated short run — absorb into the surrounding dominant
            # speaker. If there's a solo segment already, extend it. Otherwise
            # defer and let the next long run's leading edge swallow it.
            if segs:
                segs[-1].end = e
                i = j + 1
            elif j + 1 < len(merged):
                # Short run at the start; prepend a solo segment with the
                # next (long) run's label, extending back to include this one.
                nxt_lab = merged[j + 1][0]
                nxt_start = merged[j + 1][1]
                nxt_end = merged[j + 1][2]
                segs.append(Segment(
                    s, nxt_end,
                    "solo_left" if nxt_lab == "L" else "solo_right",
                ))
                i = j + 2
            else:
                # Entire video is one short run — shouldn't happen, but handle
                segs.append(Segment(s, e, "solo_left" if lab == "L" else "solo_right"))
                i = j + 1

    # Coalesce adjacent same-layout segments
    coalesced: list[Segment] = []
    for seg in segs:
        if coalesced and coalesced[-1].layout == seg.layout:
            coalesced[-1].end = seg.end
        else:
            coalesced.append(seg)

    # Absorb any stacked segment shorter than 2s into the adjacent solo.
    MIN_STACK = 2.0
    tightened: list[Segment] = []
    for seg in coalesced:
        if (
            seg.layout == "stacked"
            and (seg.end - seg.start) < MIN_STACK
            and tightened
        ):
            tightened[-1].end = seg.end
        else:
            tightened.append(seg)
    # Re-coalesce after absorption
    coalesced = []
    for seg in tightened:
        if coalesced and coalesced[-1].layout == seg.layout:
            coalesced[-1].end = seg.end
        else:
            coalesced.append(seg)
    return coalesced


def protect_break_ends(
    segs: list[Segment],
    break_points: list[float],
    labels: list[str],
    times: list[float],
    protect_seconds: float = 1.5,
) -> list[Segment]:
    """For each break point, ensure the last `protect_seconds` before the
    break is solo (not stacked). If a stacked segment covers that window,
    trim it back and insert a solo segment for the protected tail.
    """
    out: list[Segment] = [Segment(s.start, s.end, s.layout) for s in segs]
    for bp in sorted(break_points):
        # Find segment containing the window [bp - protect_seconds, bp]
        changed = True
        while changed:
            changed = False
            for idx, seg in enumerate(out):
                if seg.layout != "stacked":
                    continue
                if seg.start >= bp or seg.end <= bp - protect_seconds:
                    continue
                # Stacked overlaps the protect window.
                window_start = max(seg.start, bp - protect_seconds)
                window_end = min(seg.end, bp)
                # Determine dominant speaker in the window via label majority.
                left = right = 0
                for i, t in enumerate(times):
                    if window_start <= t <= window_end:
                        if labels[i] == "L":
                            left += 1
                        else:
                            right += 1
                solo_layout = "solo_left" if left >= right else "solo_right"

                new_segs: list[Segment] = []
                if seg.start < window_start:
                    new_segs.append(Segment(seg.start, window_start, "stacked"))
                new_segs.append(Segment(window_start, window_end, solo_layout))
                if seg.end > window_end:
                    new_segs.append(Segment(window_end, seg.end, "stacked"))
                out = out[:idx] + new_segs + out[idx + 1:]
                changed = True
                break
    # Coalesce
    coalesced: list[Segment] = []
    for seg in out:
        if coalesced and coalesced[-1].layout == seg.layout:
            coalesced[-1].end = seg.end
        else:
            coalesced.append(seg)
    return coalesced


def ffmpeg_filter_for(layout: str) -> str:
    """Return the ffmpeg video filter graph for a given layout.
    Source assumed 1920x1080; output 1080x1920.
    """
    if layout == "solo_left":
        # Crop 0..960 wide, full height; scale up; center-crop to 9:16.
        return (
            "crop=960:1080:0:0,"
            "scale=1707:1920:flags=lanczos,"
            "crop=1080:1920:313:0"
        )
    if layout == "solo_right":
        return (
            "crop=960:1080:960:0,"
            "scale=1707:1920:flags=lanczos,"
            "crop=1080:1920:313:0"
        )
    if layout == "stacked":
        # Central 936x832 from each half (aspect 9:8, matches target 1080:960).
        # Margin of 12px off the outer edges and 12px off the inner seam
        # eliminates any edge/seam pixel artifacts.
        return (
            "[0:v]split=2[a][b];"
            "[a]crop=936:832:12:124,scale=1080:960:flags=lanczos[top];"
            "[b]crop=936:832:972:124,scale=1080:960:flags=lanczos[bot];"
            "[top][bot]vstack=inputs=2"
        )
    raise ValueError(f"unknown layout: {layout}")


def render_segment(source: Path, seg: Segment, out_path: Path) -> None:
    flt = ffmpeg_filter_for(seg.layout)
    if seg.layout == "stacked":
        flag = "-filter_complex"
    else:
        flag = "-vf"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(source),
        "-ss", f"{seg.start:.3f}", "-to", f"{seg.end:.3f}",
        flag, flt,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


def concat_segments(segment_files: list[Path], out_path: Path) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False
    ) as f:
        for p in segment_files:
            f.write(f"file '{p.as_posix()}'\n")
        list_file = Path(f.name)
    try:
        # Re-encode during concat so streams align cleanly.
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", str(out_path),
        ]
        subprocess.run(cmd, check=True)
    finally:
        list_file.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--sample-fps", type=float, default=10.0,
                        help="Speaker-detection sample rate (Hz).")
    parser.add_argument("--samples-cache", type=Path, default=None,
                        help="Save/load raw mouth-openness samples as JSON.")
    parser.add_argument("--transcript-json", type=Path, default=None,
                        help="Word-level transcript JSON. Used to gate stacked "
                             "segments: if the minority side has < 3 words in "
                             "a candidate stacked window, fall back to solo.")
    parser.add_argument("--min-minority-words", type=int, default=3)
    parser.add_argument("--break-points", default="",
                        help="Comma-separated seconds. No stacked segment will "
                             "cover the last 1.5s before any break point.")
    parser.add_argument("--solo-threshold", type=float, default=5.0,
                        help="Run length (s) required for solo layout.")
    parser.add_argument("--timeline-json", type=Path, default=None,
                        help="Write segment timeline JSON for debugging.")
    parser.add_argument("--segments-dir", type=Path, default=None,
                        help="Where to cache per-segment renders.")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"error: input not found: {args.input}", file=sys.stderr)
        return 2

    w, h, fps, duration = probe_dims(args.input)
    print(f"Source: {w}x{h} @ {fps:.2f}fps, {duration:.1f}s")
    if w != 1920 or h != 1080:
        print(f"warning: expected 1920x1080, got {w}x{h} — layout math may be off",
              file=sys.stderr)

    model = ensure_landmarker_model()

    cache = args.samples_cache or args.output.parent / (args.output.stem + "_samples.json")
    if cache.exists() and cache.stat().st_size > 0:
        print(f"Loading cached samples from {cache.name}")
        data = json.loads(cache.read_text())
        if data.get("sample_fps") == args.sample_fps:
            samples = [Sample(**s) for s in data["samples"]]
        else:
            print(f"  cache sample_fps mismatch, re-detecting")
            samples = []
    else:
        samples = []

    if not samples:
        print(f"Detecting speakers at {args.sample_fps}Hz...")
        samples = sample_speakers(args.input, args.sample_fps, model, duration)
        cache.write_text(json.dumps({
            "sample_fps": args.sample_fps,
            "samples": [{"t": s.t, "left_open": s.left_open, "right_open": s.right_open}
                        for s in samples],
        }))
    print(f"  {len(samples)} samples collected")

    labels = classify(samples, args.sample_fps)
    labels = sticky_fill(labels)
    times = [s.t for s in samples]

    transcript_words = None
    if args.transcript_json and args.transcript_json.exists():
        tjson = json.loads(args.transcript_json.read_text())
        transcript_words = [
            w for seg in tjson for w in seg.get("words", [])
        ]
        print(f"Loaded {len(transcript_words)} transcript words for gating")

    segments = build_segments(
        labels, times, args.solo_threshold, duration,
        transcript_words=transcript_words,
        min_minority_words=args.min_minority_words,
    )

    if args.break_points:
        bps = [float(x) for x in args.break_points.split(",") if x.strip()]
        if bps:
            segments = protect_break_ends(segments, bps, labels, times)
            print(f"Protected {len(bps)} break-point tails (1.5s solo before each)")
    print(f"Segments: {len(segments)}")
    for s in segments:
        print(f"  {s.start:7.2f} -> {s.end:7.2f}  ({s.end - s.start:5.1f}s)  {s.layout}")

    if args.timeline_json:
        args.timeline_json.write_text(json.dumps(
            [{"start": s.start, "end": s.end, "layout": s.layout} for s in segments],
            indent=2,
        ))

    seg_dir = args.segments_dir or args.output.parent / (args.output.stem + "_segments")
    seg_dir.mkdir(parents=True, exist_ok=True)

    seg_files: list[Path] = []
    for i, seg in enumerate(segments):
        p = seg_dir / f"seg_{i:03d}_{seg.layout}.mp4"
        if p.exists() and p.stat().st_size > 0:
            print(f"  seg {i:03d}: cached")
        else:
            print(f"  seg {i:03d}: rendering {seg.layout} {seg.start:.2f}-{seg.end:.2f}")
            render_segment(args.input, seg, p)
        seg_files.append(p)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = args.output.with_suffix(".part.mp4")
    concat_segments(seg_files, tmp_out)
    tmp_out.rename(args.output)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
