#!/usr/bin/env python3
"""Reframe a 16:9 video to 9:16 using MediaPipe face tracking + ffmpeg.

Scene-aware (resets tracker at cuts), chunked (≤10 min, resumable), and safe to
re-run after a crash — completed chunks are skipped.

Usage:
    python scripts/reframe_9x16.py input.mp4 output.mp4 [--chunks-dir DIR]

If --chunks-dir is omitted, a sibling directory next to the output (with suffix
"_chunks") is used so repeat runs resume naturally.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import cv2  # type: ignore
import mediapipe as mp  # type: ignore
from mediapipe.tasks import python as mp_python  # type: ignore
from mediapipe.tasks.python import vision as mp_vision  # type: ignore

CHUNK_MAX_SECONDS = 10 * 60
SAMPLE_FPS = 3.0
EMA_ALPHA = 0.15
SCENE_THRESHOLD = 0.4
MIN_FACE_DETECTION_RATE = 0.10
CHUNK_SNAP_WINDOW_SECONDS = 30

# Crop width as a fraction of source height.
# 0.5625 = 9:16 direct crop (fills 1080x1920, tightest zoom, most face)
# 0.75   = 3:4 crop (810x1080), scaled to 1080x1440, letterboxed → more shoulder
# 0.80   = 4:5 crop (864x1080), even more body
# 1.0    = square crop, fullest shoulders/background
CROP_WIDTH_RATIO = 0.5625

FACE_MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/face_detector/"
    "blaze_face_short_range/float16/1/blaze_face_short_range.tflite"
)
FACE_MODEL_CACHE = (
    Path.home() / ".cache" / "mediapipe" / "blaze_face_short_range.tflite"
)


def ensure_face_model() -> Path:
    if FACE_MODEL_CACHE.exists() and FACE_MODEL_CACHE.stat().st_size > 0:
        return FACE_MODEL_CACHE
    FACE_MODEL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading face detection model → {FACE_MODEL_CACHE}")
    urllib.request.urlretrieve(FACE_MODEL_URL, FACE_MODEL_CACHE)
    return FACE_MODEL_CACHE


@dataclass
class Chunk:
    index: int
    start: float
    end: float
    scene_cuts: List[float]  # relative to chunk start

    @property
    def duration(self) -> float:
        return self.end - self.start


def run(cmd: List[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kwargs)


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def probe_dimensions(path: Path) -> Tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=s=x:p=0", str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    w, h = result.stdout.strip().split("x")
    return int(w), int(h)


def detect_scenes(input_path: Path, cache_path: Path) -> List[float]:
    if cache_path.exists():
        return json.loads(cache_path.read_text())

    proc = subprocess.run(
        [
            "ffmpeg", "-nostats", "-i", str(input_path),
            "-filter:v", f"select='gt(scene,{SCENE_THRESHOLD})',showinfo",
            "-f", "null", "-",
        ],
        capture_output=True, text=True, check=False,
    )
    cuts: List[float] = []
    for match in re.finditer(r"pts_time:([0-9]+\.?[0-9]*)", proc.stderr):
        cuts.append(float(match.group(1)))

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cuts))
    return cuts


def plan_chunks(duration: float, scene_cuts: List[float]) -> List[Chunk]:
    chunks: List[Chunk] = []
    start = 0.0
    idx = 0
    while start < duration:
        ideal_end = min(start + CHUNK_MAX_SECONDS, duration)
        snap_lo = max(start + 60, ideal_end - CHUNK_SNAP_WINDOW_SECONDS)
        snap_hi = min(duration, ideal_end + CHUNK_SNAP_WINDOW_SECONDS)
        candidates = [c for c in scene_cuts if snap_lo <= c <= snap_hi]
        end = min(candidates, key=lambda c: abs(c - ideal_end)) if candidates else ideal_end
        if end >= duration - 0.5:
            end = duration

        inside = [c - start for c in scene_cuts if start < c < end]
        chunks.append(Chunk(index=idx, start=start, end=end, scene_cuts=inside))
        start = end
        idx += 1
    return chunks


def sample_face_centers(chunk_path: Path) -> List[Tuple[float, Optional[float]]]:
    cap = cv2.VideoCapture(str(chunk_path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open chunk: {chunk_path}")

    video_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_stride = max(1, int(round(video_fps / SAMPLE_FPS)))
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    samples: List[Tuple[float, Optional[float]]] = []
    model_path = ensure_face_model()
    options = mp_vision.FaceDetectorOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(model_path)),
        min_detection_confidence=0.5,
    )
    detector = mp_vision.FaceDetector.create_from_options(options)

    try:
        for frame_idx in range(0, frame_count, frame_stride):
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            if not ok:
                continue
            t = frame_idx / video_fps
            frame_w = frame.shape[1]
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = detector.detect(mp_image)
            if not result.detections:
                samples.append((t, None))
                continue
            best = max(result.detections, key=lambda d: d.bounding_box.width)
            bbox = best.bounding_box
            center_px = bbox.origin_x + bbox.width / 2.0
            center_norm = center_px / max(frame_w, 1)
            samples.append((t, max(0.0, min(1.0, center_norm))))
    finally:
        detector.close()
        cap.release()

    return samples


def detection_rate(samples: List[Tuple[float, Optional[float]]]) -> float:
    if not samples:
        return 0.0
    hits = sum(1 for _, x in samples if x is not None)
    return hits / len(samples)


def median(values: List[float]) -> float:
    if not values:
        return 0.5
    sorted_vals = sorted(values)
    mid = len(sorted_vals) // 2
    if len(sorted_vals) % 2:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2


def compute_segment_crops(
    samples: List[Tuple[float, Optional[float]]],
    scene_cuts: List[float],
    chunk_duration: float,
    src_w: int,
    src_h: int,
) -> List[Tuple[float, float, float]]:
    """Return [(seg_start, seg_end, crop_x), ...].

    One segment per scene-cut-bounded region. Within each segment, face x-center
    is the median of valid detections (robust to speaker flip-flop). If no
    detections in a segment, fall back to center.
    """
    crop_w = src_h * CROP_WIDTH_RATIO
    max_x = src_w - crop_w

    cuts = sorted(set(c for c in scene_cuts if 0 < c < chunk_duration))
    boundaries = [0.0] + cuts + [chunk_duration]

    segments: List[Tuple[float, float, float]] = []
    for i in range(len(boundaries) - 1):
        seg_start = boundaries[i]
        seg_end = boundaries[i + 1]
        if seg_end - seg_start < 0.1:
            continue
        seg_centers = [
            x for t, x in samples
            if seg_start <= t < seg_end and x is not None
        ]
        if seg_centers:
            center_norm = median(seg_centers)
        else:
            center_norm = 0.5
        x = max(0.0, min(max_x, src_w * center_norm - crop_w / 2))
        segments.append((seg_start, seg_end, x))
    return segments


def reframe_chunk(
    src: Path,
    start: float,
    end: float,
    out: Path,
    scene_cuts_abs: List[float],
) -> None:
    """Extract [start, end) from src and reframe to 9:16 → out.

    Per-scene fixed crops, concatenated. Each scene segment gets a single
    median-derived crop x; within-scene motion is not tracked (podcast speakers
    are mostly stationary within a scene anyway).
    """
    with tempfile.TemporaryDirectory() as td:
        chunk_raw = Path(td) / "raw.mp4"
        run([
            "ffmpeg", "-y", "-ss", f"{start}", "-to", f"{end}",
            "-i", str(src), "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k", str(chunk_raw),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        src_w, src_h = probe_dimensions(chunk_raw)
        crop_w = src_h * CROP_WIDTH_RATIO
        samples = sample_face_centers(chunk_raw)
        rate = detection_rate(samples)

        cuts_rel = [c - start for c in scene_cuts_abs if start < c < end]

        if rate < MIN_FACE_DETECTION_RATE:
            print(
                f"  [chunk {out.stem}] face detection rate {rate:.1%} below threshold "
                f"— center-cropping this chunk",
                file=sys.stderr,
            )
            segments = [(0.0, end - start, (src_w - crop_w) / 2)]
        else:
            segments = compute_segment_crops(
                samples, cuts_rel, end - start, src_w, src_h,
            )

        # Render each segment with its fixed crop, then concat.
        seg_files: List[Path] = []
        for i, (seg_start, seg_end, crop_x) in enumerate(segments):
            seg_path = Path(td) / f"seg_{i:04d}.mp4"
            # Crop around the face, scale to fill 1080x1920.
            # If CROP_WIDTH_RATIO = 0.5625 (9:16), the crop fills exactly.
            # If larger (wider crop), scale fills width and letterboxes height.
            vf = (
                f"crop={crop_w:.2f}:{src_h}:{crop_x:.2f}:0,"
                f"scale=1080:-2,"
                f"pad=1080:1920:0:(1920-ih)/2:black"
            )
            run([
                "ffmpeg", "-y",
                "-ss", f"{seg_start}", "-to", f"{seg_end}",
                "-i", str(chunk_raw),
                "-vf", vf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
                str(seg_path),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            seg_files.append(seg_path)

        # Concat all segments into the chunk output.
        if len(seg_files) == 1:
            seg_files[0].replace(out)
        else:
            list_file = Path(td) / "concat.txt"
            list_file.write_text(
                "\n".join(f"file '{p.resolve()}'" for p in seg_files) + "\n"
            )
            run([
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(list_file), "-c", "copy", str(out),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def concat_chunks(chunk_paths: List[Path], output: Path) -> None:
    list_file = output.parent / "_concat.txt"
    list_file.write_text("\n".join(f"file '{p.resolve()}'" for p in chunk_paths) + "\n")
    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-c", "copy", str(output),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    list_file.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--chunks-dir", type=Path, default=None)
    args = parser.parse_args()

    if not args.input.exists():
        print(f"error: input not found: {args.input}", file=sys.stderr)
        return 2

    chunks_dir = args.chunks_dir or args.output.with_name(args.output.stem + "_chunks")
    chunks_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)

    duration = probe_duration(args.input)
    print(f"Input duration: {duration/60:.1f} min")

    scene_cuts = detect_scenes(args.input, chunks_dir / "scenes.json")
    print(f"Scene cuts detected: {len(scene_cuts)}")

    chunks = plan_chunks(duration, scene_cuts)
    print(f"Planned {len(chunks)} chunks")

    chunk_paths: List[Path] = []
    for chunk in chunks:
        out = chunks_dir / f"reframe_{chunk.index:03d}.mp4"
        chunk_paths.append(out)
        if out.exists() and out.stat().st_size > 0:
            print(f"  [chunk {chunk.index:03d}] exists, skipping")
            continue
        print(
            f"  [chunk {chunk.index:03d}] reframing "
            f"{chunk.start/60:.1f}-{chunk.end/60:.1f} min"
        )
        reframe_chunk(args.input, chunk.start, chunk.end, out, scene_cuts)

    print(f"Concatenating {len(chunk_paths)} chunks → {args.output}")
    concat_chunks(chunk_paths, args.output)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
