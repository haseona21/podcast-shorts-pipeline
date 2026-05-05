#!/usr/bin/env python3
"""Generate a minimal FCPXML 1.10 test file for Resolve round-trip auditing.

Writes /tmp/resolve-audit.fcpxml referencing two test sources (auto-generated
color-bar videos if not provided). Import to Resolve, solo a lane, retrim a
through-edit, edit a subtitle, then File > Export > FCPXML 1.10 to
/tmp/resolve-audit-out.fcpxml. Inspect the re-export to determine:

  1. Did <title lane="-1"> import as editable subtitles, or flat overlays?
  2. What does Resolve write when a lane is soloed/muted?
     - enabled="0" on the disabled siblings? <mute>? Lane reorder?
  3. Did through-edits survive (still asset-clips referencing same asset
     with continuous start times), or were they collapsed/reshaped?

Usage:
    python scripts/audit_fcpxml.py
    python scripts/audit_fcpxml.py --source-a path/to/a.mp4 --source-b path/to/b.mp4

This is a one-off auditing tool. Delete after the audit is complete.
"""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

try:
    from Foundation import NSURL  # type: ignore
    HAVE_PYOBJC = True
except ImportError:
    HAVE_PYOBJC = False


def make_bookmark(path: Path) -> str:
    if not HAVE_PYOBJC:
        return ""
    url = NSURL.fileURLWithPath_(str(path.resolve()))
    data, _ = url.bookmarkDataWithOptions_includingResourceValuesForKeys_relativeToURL_error_(
        1 << 9, None, None, None,
    )
    if data is None:
        return ""
    return base64.b64encode(bytes(data)).decode("ascii")

OUT_PATH = Path("/tmp/resolve-audit.fcpxml")
DEFAULT_A = Path("/tmp/audit-source-a.mp4")
DEFAULT_B = Path("/tmp/audit-source-b.mp4")

FRAMES_PER_SEC = 24


def f(seconds: float) -> str:
    """Encode seconds as FCPXML rational time at 24fps."""
    frames = round(seconds * FRAMES_PER_SEC)
    return f"{frames}/{FRAMES_PER_SEC}s"


def gen_color_bars(out: Path, label: str, color_seed: int) -> None:
    if out.exists() and out.stat().st_size > 0:
        return
    print(f"Generating test source: {out}")
    pattern = "testsrc" if color_seed == 0 else "testsrc2"
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"{pattern}=size=1080x1920:rate=24:duration=60",
            "-f", "lavfi",
            "-i", f"sine=frequency={440 + color_seed * 220}:duration=60",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest",
            str(out),
        ],
        check=True,
    )


def file_uri(path: Path) -> str:
    return "file://" + quote(str(path.resolve()))


def build_xml(src_a: Path, src_b: Path) -> str:
    src_a_uri = file_uri(src_a)
    src_b_uri = file_uri(src_b)
    bookmark_a = make_bookmark(src_a)
    bookmark_b = make_bookmark(src_b)
    bk_a = f"<bookmark>{bookmark_a}</bookmark>" if bookmark_a else ""
    bk_b = f"<bookmark>{bookmark_b}</bookmark>" if bookmark_b else ""

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.10">
  <resources>
    <format id="r1" name="FFVideoFormat1080x1920p24" frameDuration="1/24s" width="1080" height="1920"/>
    <effect id="r2" name="Basic Title" uid=".../Titles.localized/Basic Text.localized/Basic Title.moti"/>
    <asset id="a1" name="source-a" start="0s" hasVideo="1" videoSources="1" hasAudio="1" audioSources="1" audioChannels="2" audioRate="48000" duration="{f(60)}">
      <media-rep kind="original-media" src="{src_a_uri}">{bk_a}</media-rep>
    </asset>
    <asset id="a2" name="source-b" start="0s" hasVideo="1" videoSources="1" hasAudio="1" audioSources="1" audioChannels="2" audioRate="48000" duration="{f(60)}">
      <media-rep kind="original-media" src="{src_b_uri}">{bk_b}</media-rep>
    </asset>
  </resources>
  <library>
    <event name="Resolve Audit">
      <project name="Audit Project">
        <sequence format="r1" duration="{f(60)}" tcStart="0/1s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
          <spine>
            <asset-clip name="seg1-0to20" ref="a1" offset="0/24s" duration="{f(20)}" start="0/24s" format="r1" tcFormat="NDF" audioRole="dialogue">
              <asset-clip name="alt-A" ref="a2" lane="1" offset="0/1s" duration="{f(20)}" start="0/24s"/>
              <title ref="r2" lane="-1" offset="{f(5)}" duration="{f(10)}" start="0/24s">
                <text>
                  <text-style ref="ts1">first subtitle: edit me</text-style>
                </text>
                <text-style-def id="ts1">
                  <text-style font="Helvetica" fontSize="48" alignment="center"/>
                </text-style-def>
              </title>
            </asset-clip>
            <asset-clip name="seg2-20to40" ref="a1" offset="{f(20)}" duration="{f(20)}" start="{f(20)}" format="r1" tcFormat="NDF" audioRole="dialogue">
              <asset-clip name="alt-B" ref="a2" lane="1" offset="0/1s" duration="{f(20)}" start="{f(20)}"/>
            </asset-clip>
            <asset-clip name="seg3-40to60" ref="a1" offset="{f(40)}" duration="{f(20)}" start="{f(40)}" format="r1" tcFormat="NDF" audioRole="dialogue">
              <asset-clip name="alt-C" ref="a2" lane="1" offset="0/1s" duration="{f(20)}" start="{f(40)}"/>
              <title ref="r2" lane="-1" offset="{f(5)}" duration="{f(10)}" start="0/24s">
                <text>
                  <text-style ref="ts2">second subtitle: try editing this</text-style>
                </text>
                <text-style-def id="ts2">
                  <text-style font="Helvetica" fontSize="48" alignment="center"/>
                </text-style-def>
              </title>
            </asset-clip>
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-a", type=Path, default=DEFAULT_A)
    parser.add_argument("--source-b", type=Path, default=DEFAULT_B)
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args()

    if args.source_a == DEFAULT_A:
        gen_color_bars(args.source_a, "A", 0)
    if args.source_b == DEFAULT_B:
        gen_color_bars(args.source_b, "B", 1)

    if not args.source_a.exists():
        print(f"error: source-a not found: {args.source_a}", file=sys.stderr)
        return 2
    if not args.source_b.exists():
        print(f"error: source-b not found: {args.source_b}", file=sys.stderr)
        return 2

    args.out.write_text(build_xml(args.source_a, args.source_b))
    print(f"Wrote {args.out}")
    print()
    print("Next steps:")
    print(f"  1. Open Resolve. File > Import > Timeline > {args.out}")
    print("  2. Verify on the timeline:")
    print("     - V1 has three segments at 0-20s, 20-40s, 40-60s (through-edits)")
    print("     - V2 has the alternate angle (try soloing a track)")
    print("     - Subtitle/title track shows 'first subtitle' and 'second subtitle'")
    print("       (note whether they're editable subtitles or flat overlays)")
    print("  3. Edit one subtitle's text.")
    print("  4. Solo V2 on the middle segment only (so V1 stays for outer two).")
    print("  5. Drag the through-edit at 20s to ~22s.")
    print("  6. File > Export > Final Cut Pro XML > 1.10")
    print("     Save as /tmp/resolve-audit-out.fcpxml")
    print("  7. Report back: structure of the re-exported file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
