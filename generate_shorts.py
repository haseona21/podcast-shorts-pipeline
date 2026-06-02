#!/usr/bin/env python3
"""Draft-shorts doc -> finished vertical shorts (single command).

Chains the full pipeline:
    manifest/make_manifest.py  parse the draft doc into a manifest
    caption/transcribe.py      transcribe each cut + write words/captions
    render_short.py            cut -> reframe/stack -> burn captions

    python generate_shorts.py path/to/draft-shorts.md \\
        --guest path/to/guest.mp4 --ali path/to/host.mp4 --out out/ \\
        [--model small.en]

Writes the working manifest to <out>/manifest.json and the rendered
1080x1920 mp4s to <out>/ (one per approved short).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


def step(label: str, cmd: list[str]) -> None:
    print(f"\n########## {label} ##########")
    print("  " + " ".join(str(c) for c in cmd))
    subprocess.run(cmd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("draft_shorts", type=Path, help="draft-shorts .md")
    ap.add_argument("--guest", required=True, help="Guest source video")
    ap.add_argument("--ali", required=True, help="Host source video")
    ap.add_argument("--out", type=Path, required=True, help="Output directory")
    ap.add_argument("--model", default="small.en",
                    help="Local whisper model (when CAPTACITY_USE_LOCAL_WHISPER=1)")
    args = ap.parse_args()

    if not args.draft_shorts.exists():
        print(f"error: draft doc not found: {args.draft_shorts}", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    manifest = args.out / "manifest.json"
    py = sys.executable

    step("1/3 make_manifest", [
        py, str(REPO_ROOT / "manifest" / "make_manifest.py"), str(args.draft_shorts),
        "--guest", args.guest, "--ali", args.ali, "--out", str(manifest),
    ])
    step("2/3 transcribe_captions", [
        py, str(REPO_ROOT / "caption" / "transcribe.py"), str(manifest),
        "--model", args.model,
    ])
    step("3/3 render_short", [
        py, str(REPO_ROOT / "render_short.py"), str(manifest), str(args.out),
    ])

    print(f"\nDone. Manifest: {manifest}")
    print(f"Rendered shorts in: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
