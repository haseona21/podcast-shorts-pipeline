#!/usr/bin/env python3
"""Emit a DaVinci-Resolve-importable FCPXML for an episode's approved shorts.

The editor lands in Resolve with the original recording on V1, three alt
crops (V2 stacked dup, V3 top-solo, V4 bottom-solo) stacked above, and
markers at every approved short's start/end. They blade and retrim per
short. The sidecar SRT imports separately via File > Import > Subtitle.

Timeline shape:
  - V1 always references the ORIGINAL recording (preferred: original-both).
    Final cuts have nonlinear edits removed; using one as primary would put
    the SRT minutes out of sync with audio. If a final source is provided
    it lands in <resources> as a draggable alternate but is never primary.
  - V2 = duplicate stacked layer (compositing scratch).
  - V3 (lane=2) = top-half crop of V1 → top speaker solo at 1080×1920.
  - V4 (lane=3) = bottom-half crop of V1 → bottom speaker solo.
  - All four lanes ship enabled; toggle visibility in the Inspector per
    clip.
  - Markers at each approved-short start/end land on V1.
  - Subtitles are NOT embedded. Use Resolve's File > Import > Subtitle on
    the sibling .srt after importing the FCPXML.

Final-vs-original tagging of clips is metadata that lives in the shorts
approved doc only — it tells the editor which moments to look for, not
which video to switch to. The FCPXML always works off the original.

Usage:
    python scripts/build_fcpxml.py \\
        --source-original-both PATH \\
        [--source-original-guest PATH] \\
        [--source-final-guest PATH] [--source-final-both PATH] \\
        --srt PATH --json PATH \\
        --approved-md PATH \\
        --slug NAME \\
        --output-dir PATH
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import approved_shorts  # noqa: E402

try:
    from Foundation import NSURL  # type: ignore
    HAVE_PYOBJC = True
except ImportError:
    HAVE_PYOBJC = False


def make_bookmark(path: Path) -> Optional[str]:
    """Return base64 of a macOS security-scoped bookmark for `path`, or None
    if PyObjC is unavailable. Resolve uses bookmarks to auto-link FCPXML
    media without manual Media Pool intervention.
    """
    if not HAVE_PYOBJC:
        return None
    url = NSURL.fileURLWithPath_(str(path.resolve()))
    # NSURLBookmarkCreationMinimalBookmark = 1 << 9. Strips the app-sandbox
    # security-scoped data and produces the format Resolve itself emits.
    data, err = url.bookmarkDataWithOptions_includingResourceValuesForKeys_relativeToURL_error_(
        1 << 9, None, None, None,
    )
    if data is None:
        print(f"warn: failed to make bookmark for {path}: {err}",
              file=sys.stderr)
        return None
    return base64.b64encode(bytes(data)).decode("ascii")

PAIR_PARTNER = {
    "final-guest": "final-both",
    "final-both": "final-guest",
    "original-guest": "original-both",
    "original-both": "original-guest",
}


@dataclass
class SourceInfo:
    key: str                # e.g. "final-guest"
    path: Path
    asset_id: str           # e.g. "a1"
    format_id: str          # e.g. "f1"
    fps_num: int
    fps_den: int
    width: int
    height: int
    duration_s: float

    @property
    def fps(self) -> float:
        return self.fps_num / self.fps_den

    def encode_time(self, seconds: float) -> str:
        return seconds_to_rational(seconds, self.fps_num, self.fps_den)

    def to_frames(self, seconds: float) -> int:
        return round(seconds * self.fps_num / self.fps_den)

    def encode_frames(self, frames: int) -> str:
        return f"{frames}/{self.fps_num}s"


def probe_source(path: Path) -> tuple[int, int, int, int, float]:
    """Returns (fps_num, fps_den, width, height, duration_s)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate,width,height",
         "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout

    fields: dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            fields[k.strip()] = v.strip()

    rate = fields.get("r_frame_rate", "30/1")
    if "/" in rate:
        n_str, d_str = rate.split("/")
        n, d = int(n_str), int(d_str)
    else:
        n, d = int(float(rate)), 1
    width = int(fields.get("width", 0))
    height = int(fields.get("height", 0))
    duration = float(fields.get("duration", 0.0))
    return n, d, width, height, duration


def seconds_to_rational(seconds: float, fps_num: int, fps_den: int) -> str:
    """Encode seconds as N/D s where D = fps_num and N = round(seconds * fps_num / fps_den)."""
    if seconds <= 0:
        return f"0/{fps_num}s"
    frames = round(seconds * fps_num / fps_den)
    return f"{frames}/{fps_num}s"


def file_uri(path: Path) -> str:
    return "file://" + quote(str(path.resolve()))


def collect_sources(args: argparse.Namespace) -> dict[str, SourceInfo]:
    """Probe every provided source. Returns dict keyed by source-type slug."""
    spec = {
        "final-guest": args.source_final_guest,
        "final-both": args.source_final_both,
        "original-guest": args.source_original_guest,
        "original-both": args.source_original_both,
    }
    out: dict[str, SourceInfo] = {}
    asset_idx = 0
    for key, path in spec.items():
        if not path:
            continue
        if not path.exists():
            print(f"warn: --source-{key} {path} not found, skipping",
                  file=sys.stderr)
            continue
        asset_idx += 1
        n, d, w, h, dur = probe_source(path)
        out[key] = SourceInfo(
            key=key, path=path,
            asset_id=f"a{asset_idx}",
            format_id=f"f{asset_idx}",
            fps_num=n, fps_den=d, width=w, height=h, duration_s=dur,
        )
    if not out:
        raise SystemExit("error: no sources provided")
    return out


def build_xml(
    sources: dict[str, SourceInfo],
    shorts_resolved: list[tuple[approved_shorts.ApprovedShort, float, float]],
    slug: str,
) -> ET.Element:
    fcpxml = ET.Element("fcpxml", version="1.10")
    resources = ET.SubElement(fcpxml, "resources")

    # Primary V1 reference must be the original (uncut) recording so the
    # sidecar SRT — which is generated from the same original — actually
    # aligns with what the editor sees on the timeline. Final cuts have
    # nonlinear edits removed and would put captions minutes out of sync.
    primary = next(
        (sources[k] for k in ("original-both", "original-guest")
         if k in sources),
        None,
    )
    if primary is None:
        raise SystemExit(
            "error: FCPXML must reference the original source. "
            "Pass --source-original-both or --source-original-guest."
        )

    # Per-asset formats (Resolve handles mixed by per-asset format declarations).
    for src in sources.values():
        ET.SubElement(
            resources, "format",
            id=src.format_id,
            name=f"FFVideoFormat{src.width}x{src.height}p{src.fps:.0f}",
            frameDuration=f"{src.fps_den}/{src.fps_num}s",
            width=str(src.width), height=str(src.height),
        )

    for src in sources.values():
        asset = ET.SubElement(
            resources, "asset",
            id=src.asset_id, name=src.path.name,
            start="0s",
            duration=src.encode_time(src.duration_s),
            hasVideo="1", videoSources="1",
            hasAudio="1", audioSources="1",
            audioChannels="2", audioRate="48000",
            format=src.format_id,
        )
        media_rep = ET.SubElement(asset, "media-rep",
                                  kind="original-media", src=file_uri(src.path))
        bookmark = make_bookmark(src.path)
        if bookmark is not None:
            ET.SubElement(media_rep, "bookmark").text = bookmark

    library = ET.SubElement(fcpxml, "library")
    event = ET.SubElement(library, "event", name=f"{slug} shorts")
    project = ET.SubElement(event, "project", name=slug)

    sequence = ET.SubElement(
        project, "sequence",
        format=primary.format_id,
        duration=primary.encode_time(primary.duration_s),
        tcStart="0/1s", tcFormat="NDF",
        audioLayout="stereo", audioRate="48k",
    )
    spine = ET.SubElement(sequence, "spine")

    # V1: full-frame stacked layout (both speakers visible) — primary clip
    # spans the whole source. Markers at every approved short's start/end
    # land inside this clip so you can jump between them and blade where you
    # actually want.
    # V2: duplicate full-frame for stacking/compositing experiments
    # V3 (lane=2): top-half crop = top speaker solo
    # V4 (lane=3): bottom-half crop = bottom speaker solo
    # All four are enabled by default so the editor sees four stacked tracks
    # and can solo whichever angle they want per clip. Toggle visibility on a
    # per-clip basis in the Resolve Inspector.
    primary_duration_f = primary.to_frames(primary.duration_s)
    duration_str = primary.encode_frames(primary_duration_f)

    clip = ET.SubElement(
        spine, "asset-clip",
        name=f"V1 stacked — {primary.path.stem}",
        ref=primary.asset_id,
        offset="0/1s",
        duration=duration_str,
        start="0/1s",
        format=primary.format_id,
        tcFormat="NDF", audioRole="dialogue",
    )

    def _add_alt(lane: str, name: str, transform: Optional[dict] = None,
                 crop: Optional[dict] = None, enabled: bool = True) -> ET.Element:
        attrs = {
            "name": name,
            "ref": primary.asset_id,
            "lane": lane,
            "offset": "0/1s",
            "duration": duration_str,
            "start": "0/1s",
        }
        if not enabled:
            attrs["enabled"] = "0"
        alt = ET.SubElement(clip, "asset-clip", **attrs)
        if crop:
            adj = ET.SubElement(alt, "adjust-crop", type="trim")
            ET.SubElement(adj, "trim-rect", **{k: str(v) for k, v in crop.items()})
        if transform:
            ET.SubElement(alt, "adjust-transform",
                          **{k: str(v) for k, v in transform.items()})
        return alt

    # V2: duplicate stacked for compositing
    _add_alt("1", f"V2 stacked dup — {primary.path.stem}")
    # V3: top speaker solo. Crop bottom 50% (trim-rect bottom=50), then
    # shift up + scale 2x so the top half fills the 9:16 frame.
    _add_alt(
        "2", f"V3 top solo — top crop",
        crop={"left": 0, "top": 0, "right": 0, "bottom": 50},
        transform={"position": "0 480", "scale": "2 2"},
    )
    # V4: bottom speaker solo. Crop top 50%, shift down + scale 2x.
    _add_alt(
        "3", f"V4 bottom solo — bottom crop",
        crop={"left": 0, "top": 50, "right": 0, "bottom": 0},
        transform={"position": "0 -480", "scale": "2 2"},
    )

    for short, start_s, end_s in sorted(shorts_resolved, key=lambda r: r[1]):
        sf = primary.to_frames(start_s)
        ef = primary.to_frames(end_s)
        if sf >= primary_duration_f:
            print(f"warn: short{short.number} ({short.title!r}) starts at "
                  f"{start_s:.1f}s, beyond source duration "
                  f"({primary.duration_s:.1f}s) — skipping marker.",
                  file=sys.stderr)
            continue
        ef = min(ef, primary_duration_f)
        ET.SubElement(
            clip, "marker",
            start=primary.encode_frames(sf),
            duration="1/24s",
            value=f"▶ Short {short.number}: {short.title}",
        )
        ET.SubElement(
            clip, "marker",
            start=primary.encode_frames(ef),
            duration="1/24s",
            value=f"■ end Short {short.number}",
        )

    return fcpxml


def write_xml(root: ET.Element, out_path: Path) -> None:
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    out_path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE fcpxml>\n'
        f'{body}\n'
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-final-guest", type=Path, default=None)
    parser.add_argument("--source-final-both", type=Path, default=None)
    parser.add_argument("--source-original-guest", type=Path, default=None)
    parser.add_argument("--source-original-both", type=Path, default=None)
    parser.add_argument("--srt", type=Path, default=None,
                        help="Sidecar SRT for File > Import > Subtitle in "
                             "Resolve (optional, just verifies existence).")
    parser.add_argument("--json", type=Path, default=None,
                        dest="json_path",
                        help="Word-level transcript JSON. If absent, "
                             "boundaries fall back to .md timestamps as-is.")
    parser.add_argument("--approved-md", type=Path, required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--snippet-words", type=int, default=7)
    parser.add_argument("--hint-radius", type=float, default=20.0)
    args = parser.parse_args()

    sources = collect_sources(args)
    print(f"Sources: {', '.join(s.key for s in sources.values())}")
    for src in sources.values():
        print(f"  {src.key}: {src.width}x{src.height} @ "
              f"{src.fps:.2f}fps, {src.duration_s/60:.1f}min")

    shorts = approved_shorts.parse_approved_md(args.approved_md)
    if not shorts:
        print("error: no approved shorts parsed", file=sys.stderr)
        return 1

    if args.json_path and args.json_path.exists():
        segments = json.loads(args.json_path.read_text())
    else:
        segments = []
    raw = [(s, *approved_shorts.resolve_boundaries(
        s, segments,
        snippet_words=args.snippet_words,
        hint_radius=args.hint_radius,
    )) for s in shorts]
    snapped = approved_shorts.snap_overlaps(raw)
    print(f"Approved shorts: {len(snapped)}"
          + ("" if segments else " (using .md timestamps as-is, no text-match)"))

    root = build_xml(sources, snapped, args.slug)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.output_dir / f"{args.slug}.fcpxml"
    write_xml(root, out_path)
    print(f"Wrote {out_path}")
    if args.srt and args.srt.exists():
        print(f"Subtitles: import {args.srt} via Resolve's "
              "File > Import > Subtitle (lands on its own ST track).")
    elif args.srt:
        print(f"warn: --srt {args.srt} not found", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
