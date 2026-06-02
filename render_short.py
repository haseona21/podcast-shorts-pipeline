#!/usr/bin/env python3
"""Self-contained renderer for the approved shorts.

Reads a manifest JSON (sources + approved shorts), and for each approved short
produces a finished 1080x1920 vertical clip with burned-in captions.

    python render_short.py <manifest.json> <output_dir>

Pipeline per short (see module docstring sections):
  - single layout (guest_only / ali_only): cut the relevant source for each
    render_segment WITH audio, concat in order, face-track reframe to 9:16,
    then caption in the default lower-third position.
  - stacked layout: per render_segment, build a two-up (Ali top / guest bottom,
    zoomed-out, undistorted) frame, concat in order, then caption with a
    layout-timeline marking the whole clip "stacked" so captions center on the
    divider between the two faces.

Caption styling is owned entirely by caption_video.py (Georgia, cream text,
deep-red highlight box, no stroke, no shadow). This script only chooses the
vertical position via the layout timeline.

Dependency-light: stdlib + ffmpeg/ffprobe + the repo's reframe_9x16.py and
caption_video.py (run via subprocess against the active interpreter).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

from config import CFG

REPO_ROOT = Path(__file__).resolve().parent
REFRAME_SCRIPT = REPO_ROOT / "reframe_9x16.py"
CAPTION_SCRIPT = REPO_ROOT / "caption_video.py"

_R = CFG.render

# x264 quality factor for cut/stack re-encodes (env-driven; default 18).
CRF = str(_R.crf)

# Stacked two-up geometry (all env-driven; defaults reproduce the winning look).
# Each 16:9 source is center-cropped to STACK_CROP_W x STACK_CROP_H (a zoomed-out,
# undistorted window that keeps the speaker on-frame) then scaled to PANE_W x
# PANE_H so two panes stack into a WIDTH x HEIGHT frame at FPS.
STACKED_FILTER = (
    "[0:v]crop={cw}:{ch}:(iw-{cw})/2:0,scale={pw}:{ph},setsar=1,fps={fps}[t];"
    "[1:v]crop={cw}:{ch}:(iw-{cw})/2:0,scale={pw}:{ph},setsar=1,fps={fps}[b];"
    "[t][b]vstack=inputs=2,format=yuv420p[v];"
    "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[a]"
).format(
    cw=_R.stack_crop_w, ch=_R.stack_crop_h,
    pw=_R.pane_w, ph=_R.pane_h, fps=_R.fps,
)


def run(cmd: List[str]) -> None:
    subprocess.run(cmd, check=True)


def run_quiet(cmd: List[str]) -> None:
    subprocess.run(
        cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def concat(parts: List[Path], out: Path, workdir: Path) -> None:
    """Concatenate same-codec mp4 parts in order via the ffmpeg concat demuxer."""
    if len(parts) == 1:
        run_quiet(["cp", str(parts[0]), str(out)])
        return
    list_file = workdir / "concat.txt"
    list_file.write_text(
        "\n".join(f"file '{p.resolve()}'" for p in parts) + "\n"
    )
    run_quiet([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-c", "copy", str(out),
    ])


def cut_single(src: Path, start: float, end: float, out: Path) -> None:
    """Cut [start, end) from a single source WITH audio (sample-accurate)."""
    run_quiet([
        "ffmpeg", "-y", "-ss", f"{start}", "-to", f"{end}", "-i", str(src),
        "-c:v", "libx264", "-crf", CRF, "-c:a", "aac", "-ar", "48000",
        str(out),
    ])


def cut_stacked(ali: Path, guest: Path, start: float, end: float, out: Path) -> None:
    """Build one stacked (Ali top / guest bottom) segment for [start, end)."""
    run_quiet([
        "ffmpeg", "-y",
        "-ss", f"{start}", "-to", f"{end}", "-i", str(ali),
        "-ss", f"{start}", "-to", f"{end}", "-i", str(guest),
        "-filter_complex", STACKED_FILTER,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-crf", CRF, "-c:a", "aac", "-ar", "48000",
        str(out),
    ])


def words_to_segments(words: List[Dict]) -> List[Dict]:
    """Convert clip-local manifest words → a single Captacity segment.

    Each word's text is space-prefixed (Captacity's word-merge convention).
    """
    last_end = max((float(w["end"]) for w in words), default=0.0)
    return [{
        "start": 0.0,
        "end": last_end,
        "words": [
            {"word": " " + w["word"], "start": float(w["start"]), "end": float(w["end"])}
            for w in words
        ],
    }]


def reframe(base_in: Path, base_out: Path, workdir: Path) -> None:
    run([
        sys.executable, str(REFRAME_SCRIPT), str(base_in), str(base_out),
        "--chunks-dir", str(workdir / "reframe_chunks"),
    ])


def caption(base: Path, segments_json: Path, out: Path,
            layout_timeline: Path | None = None) -> None:
    cmd = [
        sys.executable, str(CAPTION_SCRIPT),
        str(base), str(segments_json), str(out),
    ]
    if layout_timeline is not None:
        cmd += ["--layout-timeline", str(layout_timeline)]
    run(cmd)


def render_short(short: Dict, sources: Dict, output_dir: Path) -> Path:
    layout = short["layout"]
    out_path = output_dir / short["canonical_output"]
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="render_short_") as td:
        workdir = Path(td)
        segments = words_to_segments(short["words"])
        segments_json = workdir / "segments.json"
        segments_json.write_text(json.dumps(segments))

        seg_parts: List[Path] = []
        render_segments = short["render_segments"]

        if layout in ("guest_only", "ali_only"):
            src_key = "guest_video" if layout == "guest_only" else "ali_video"
            src = Path(sources[src_key])
            for i, rseg in enumerate(render_segments):
                seg_out = workdir / f"seg_{i:03d}.mp4"
                cut_single(src, float(rseg["start"]), float(rseg["end"]), seg_out)
                seg_parts.append(seg_out)

            base = workdir / "base.mp4"
            concat(seg_parts, base, workdir)

            reframed = workdir / "reframed.mp4"
            reframe(base, reframed, workdir)

            # Default lower-third caption (no layout timeline).
            caption(reframed, segments_json, out_path)

        elif layout == "stacked":
            ali = Path(sources["ali_video"])
            guest = Path(sources["guest_video"])
            for i, rseg in enumerate(render_segments):
                seg_out = workdir / f"seg_{i:03d}.mp4"
                cut_stacked(ali, guest, float(rseg["start"]), float(rseg["end"]), seg_out)
                seg_parts.append(seg_out)

            base = workdir / "stacked_base.mp4"
            concat(seg_parts, base, workdir)

            # Whole clip is stacked → captions center on the divider.
            timeline = workdir / "layout_timeline.json"
            timeline.write_text(json.dumps(
                [{"start": 0, "end": 100000, "layout": "stacked"}]
            ))
            caption(base, segments_json, out_path, layout_timeline=timeline)

        else:
            raise ValueError(f"unknown layout: {layout!r}")

    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="Manifest JSON")
    parser.add_argument("output_dir", type=Path, help="Output directory")
    args = parser.parse_args()

    if not args.manifest.exists():
        print(f"error: manifest not found: {args.manifest}", file=sys.stderr)
        return 2

    manifest = json.loads(args.manifest.read_text())
    sources = manifest["sources"]
    shorts = manifest.get("shorts", [])
    approved = [s for s in shorts if s.get("status", "approved") == "approved"]

    if not approved:
        print("no approved shorts in manifest — nothing to render", file=sys.stderr)
        return 0

    for short in approved:
        print(f"\n=== Rendering {short.get('id', short['canonical_output'])} "
              f"(layout={short['layout']}) ===")
        out = render_short(short, sources, args.output_dir)
        print(f"Wrote {out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
