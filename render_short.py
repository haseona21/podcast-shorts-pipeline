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

Dependency-light: stdlib + ffmpeg/ffprobe + the repo's splice/ (cut/stack +
reframe) and caption/ (burn) modules. The cut/stack/concat ffmpeg logic lives in
splice/stack.py; reframe and caption run via subprocess against the active
interpreter.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from splice.stack import concat, cut_single, cut_stacked  # noqa: E402

REFRAME_SCRIPT = REPO_ROOT / "splice" / "reframe.py"
CAPTION_SCRIPT = REPO_ROOT / "caption" / "burn.py"


def run(cmd: List[str]) -> None:
    subprocess.run(cmd, check=True)


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
