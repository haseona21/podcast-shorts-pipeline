#!/usr/bin/env python3
"""Re-cut shorts from a DaVinci-edited FCPXML using ffmpeg.

Walks each spine asset-clip on the timeline, picks the active video asset
(soloed lane or top-most enabled), and ffmpeg-cuts the corresponding source
between the clip's in/out points. Optionally uploads finished MP4s to Google
Drive via `gws drive`.

This is the "Path B" round-trip out of DaVinci. After the user trims/swaps
angles in Resolve, they File > Export > FCPXML 1.10 and feed the result here.

Usage:
    python scripts/import_fcpxml.py edited.fcpxml \\
        --output-dir PATH \\
        [--slug SLUG] \\
        [--upload --drive-folder-name "Guest Shorts Drafts" \\
                  --drive-parent-id 1gTPQIDVKuTknsv2osrSENY8Ftm9BetFi]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse


@dataclass
class ClipSpec:
    name: str           # asset-clip name attribute (used as output filename)
    source_path: Path   # resolved source media path
    in_seconds: float   # ffmpeg -ss
    out_seconds: float  # ffmpeg -to
    lane: int           # which video lane was active (0 = primary, 1+ = above)


def rational_to_seconds(value: str) -> float:
    """Parse FCPXML rational time like '480/24s' or '0/1s' or '5s'."""
    s = value.strip().rstrip("s").strip()
    if not s:
        return 0.0
    if "/" in s:
        n, d = s.split("/")
        return float(Fraction(int(n), int(d)))
    return float(s)


def build_asset_index(root: ET.Element) -> dict[str, Path]:
    """Map asset id → resolved source Path from the <resources> table."""
    out: dict[str, Path] = {}
    resources = root.find("resources")
    if resources is None:
        return out
    for asset in resources.findall("asset"):
        aid = asset.get("id")
        media = asset.find("media-rep")
        if aid is None or media is None:
            continue
        src = media.get("src", "")
        if src.startswith("file://"):
            path = Path(unquote(urlparse(src).path))
        else:
            path = Path(src)
        out[aid] = path
    return out


def is_disabled(elem: ET.Element) -> bool:
    val = elem.get("enabled", "")
    return val.lower() in {"0", "false", "no"}


def gather_video_candidates(parent: ET.Element) -> list[tuple[int, ET.Element]]:
    """Return [(lane, asset-clip)] for parent + nested video asset-clips.

    Parent itself counts as lane 0 (primary storyline). Children with lane>0
    are stacked above it; lane<0 are below (subtitles/audio territory).
    """
    out: list[tuple[int, ET.Element]] = [(0, parent)]
    for child in parent:
        if child.tag != "asset-clip":
            continue
        lane = child.get("lane")
        if lane is None:
            continue
        try:
            lane_n = int(lane)
        except ValueError:
            continue
        if lane_n <= 0:
            continue
        out.append((lane_n, child))
    return out


def pick_active(candidates: list[tuple[int, ET.Element]]
                ) -> Optional[tuple[int, ET.Element]]:
    """Pick the highest-lane enabled candidate. Returns None if all disabled."""
    enabled = [(lane, c) for lane, c in candidates if not is_disabled(c)]
    if not enabled:
        return None
    enabled.sort(key=lambda x: x[0], reverse=True)
    return enabled[0]


def parse_fcpxml(path: Path) -> list[ClipSpec]:
    tree = ET.parse(path)
    root = tree.getroot()
    assets = build_asset_index(root)

    spine = root.find(".//spine")
    if spine is None:
        raise SystemExit("error: no <spine> in FCPXML")

    out: list[ClipSpec] = []
    for clip in spine.findall("asset-clip"):
        name = clip.get("name") or "unnamed"
        candidates = gather_video_candidates(clip)
        active = pick_active(candidates)
        if active is None:
            print(f"warn: {name}: all video lanes disabled, skipping",
                  file=sys.stderr)
            continue

        lane, active_clip = active
        ref = active_clip.get("ref")
        if ref is None or ref not in assets:
            print(f"warn: {name}: lane {lane} ref {ref!r} not in resources, "
                  f"skipping", file=sys.stderr)
            continue

        # Each asset-clip has its own start (source time) + duration (timeline
        # span). The parent's offset is timeline position; we don't need it
        # for ffmpeg cuts — only the source-time start + duration.
        src_path = assets[ref]
        start_s = rational_to_seconds(active_clip.get("start", "0/1s"))
        duration_s = rational_to_seconds(active_clip.get("duration", "0/1s"))
        out.append(ClipSpec(
            name=name, source_path=src_path,
            in_seconds=start_s,
            out_seconds=start_s + duration_s,
            lane=lane,
        ))
    return out


def sanitize_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")


def ffmpeg_cut(spec: ClipSpec, out_path: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{spec.in_seconds:.3f}",
        "-to", f"{spec.out_seconds:.3f}",
        "-i", str(spec.source_path),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)


def upload_to_drive(folder_name: str, parent_id: str,
                    files: list[Path]) -> str:
    """Mirror the Step 6 pattern: create a folder, upload each file. Returns
    the folder id so the caller can print a share link."""
    print(f"\nCreating Drive folder {folder_name!r} under parent {parent_id}")
    create_proc = subprocess.run(
        [
            "gws", "drive", "files", "create",
            "--json", json.dumps({
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }),
            "--params", json.dumps({"supportsAllDrives": True}),
        ],
        check=True, capture_output=True, text=True,
    )
    folder = json.loads(create_proc.stdout)
    folder_id = folder["id"]
    print(f"  folder id: {folder_id}")

    for f in files:
        print(f"  uploading {f.name}")
        subprocess.run(
            [
                "gws", "drive", "files", "create",
                "--json", json.dumps({"name": f.name, "parents": [folder_id]}),
                "--upload", str(f),
                "--upload-content-type", "video/mp4",
                "--params", json.dumps({"supportsAllDrives": True}),
            ],
            check=True,
        )
    return folder_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fcpxml", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--slug", default=None,
                        help="Optional prefix for output filenames.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse and print clip plan without cutting.")
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--drive-folder-name", default=None)
    parser.add_argument("--drive-parent-id", default=None)
    args = parser.parse_args()

    if not args.fcpxml.exists():
        print(f"error: {args.fcpxml} not found", file=sys.stderr)
        return 2

    clips = parse_fcpxml(args.fcpxml)
    if not clips:
        print("error: no clips parsed from FCPXML", file=sys.stderr)
        return 1

    print(f"{'CLIP':<48} {'LANE':>4} {'IN':>10} {'OUT':>10}  DUR  SOURCE")
    for c in clips:
        print(f"{c.name[:46]:<48} {c.lane:>4} {c.in_seconds:>10.3f} "
              f"{c.out_seconds:>10.3f}  {c.out_seconds - c.in_seconds:5.1f}s  "
              f"{c.source_path.name}")

    if args.dry_run:
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for c in clips:
        fname = sanitize_filename(c.name) + ".mp4"
        out_path = args.output_dir / fname
        print(f"\nCutting {fname}")
        ffmpeg_cut(c, out_path)
        written.append(out_path)

    print(f"\nWrote {len(written)} clips to {args.output_dir}")

    if args.upload:
        if not args.drive_folder_name or not args.drive_parent_id:
            print("error: --upload requires --drive-folder-name and "
                  "--drive-parent-id", file=sys.stderr)
            return 2
        folder_id = upload_to_drive(
            args.drive_folder_name, args.drive_parent_id, written,
        )
        print(f"\nDrive folder: https://drive.google.com/drive/folders/{folder_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
