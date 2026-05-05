#!/usr/bin/env python3
"""Reconcile an edited SRT into the word-level JSON, then run caption_video.

Recovers the --review path when the original preprocess run skipped the SRT
edit pause (e.g. because it ran without a TTY).

Usage:
    python scripts/reconcile_and_caption.py <masters_dir> <slug>
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from transcribe_video import (  # noqa: E402
    dicts_to_segments,
    parse_srt,
    reconcile,
    segments_to_dicts,
)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2

    masters_dir = Path(sys.argv[1])
    slug = sys.argv[2]

    srt_path = masters_dir / f"{slug}.srt"
    json_path = masters_dir / f"{slug}.json"
    reframed = masters_dir / f"{slug}-9x16.mp4"
    final_path = masters_dir / f"{slug}-9x16-captioned.mp4"

    for p in (srt_path, json_path, reframed):
        if not p.exists():
            print(f"error: missing {p}", file=sys.stderr)
            return 2

    segments = dicts_to_segments(json.loads(json_path.read_text()))
    edited = parse_srt(srt_path.read_text())
    reconciled = reconcile(segments, edited)
    json_path.write_text(json.dumps(segments_to_dicts(reconciled), indent=2))
    print(f"Reconciled {len(edited)} SRT entries into {json_path.name}")

    cmd = [
        sys.executable,
        str(SCRIPTS_DIR / "caption_video.py"),
        str(reframed),
        str(json_path),
        str(final_path),
    ]
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    print(f"\nDone. Captioned master: {final_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
