#!/usr/bin/env python3
"""Parse a draft-shorts doc into a render manifest.

Reads a "draft-shorts" markdown doc (the kind produced for an episode's shorts
plan) and emits ONE manifest JSON describing every approved short: its sources,
layout, render segments, and slug/title. The `words` and `captions` fields are
left empty — they are filled in by `transcribe_captions.py`, which transcribes
the actual cut audio.

    python make_manifest.py <draft_shorts.md> \\
        --guest <guest_video.mp4> [--ali <ali_video.mp4>] [--out manifest.json]

Doc structure parsed (the per-episode shorts plan):

    ### Approved N: <Title>
    ...
    Render segment: `MM:SS.mmm to MM:SS.mmm`           # single segment
      -- or --
    Render segments:
    - `MM:SS.mmm to MM:SS.mmm` -- note...              # multi-segment list
    - `MM:SS.mmm to MM:SS.mmm` -- note...
    ...
    Visual plan: `<guest>-only throughout` / `Both faces` / `... stacked ...`

Only `### Approved N: ...` sections are emitted (e.g. `### Candidate 2:` is
skipped). Layout is derived from the Visual plan line:
  - "<guest>-only" / "... only ..."                 -> guest_only
  - "Both faces" / "stacked"                        -> stacked
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

# e.g. `### Approved 4: <Title>`
APPROVED_HEADER_RE = re.compile(r"^###\s+Approved\s+(\d+)\s*:\s*(.+?)\s*$")
# any `### ...` header (used as a section boundary)
ANY_H3_RE = re.compile(r"^###\s+")
# `MM:SS.mmm to MM:SS.mmm`  (also accepts H:MM:SS and integer seconds)
RANGE_RE = re.compile(
    r"`?\s*(\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)\s+to\s+"
    r"(\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?)\s*`?"
)


def slugify(title: str) -> str:
    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def parse_timestamp(ts: str) -> float:
    """`MM:SS.mmm` or `HH:MM:SS.mmm` -> seconds (float)."""
    parts = ts.split(":")
    parts = [float(p) for p in parts]
    if len(parts) == 2:
        m, s = parts
        return m * 60 + s
    if len(parts) == 3:
        h, m, s = parts
        return h * 3600 + m * 60 + s
    raise ValueError(f"unparseable timestamp: {ts!r}")


def derive_layout(visual_plan: str) -> str:
    vp = visual_plan.lower()
    if "both" in vp or "stacked" in vp or "split" in vp:
        return "stacked"
    if "only" in vp:
        return "guest_only"
    # Default to the simplest single-pane treatment.
    return "guest_only"


def parse_section(lines: List[str]) -> Dict:
    """Pull render segments + visual plan out of one Approved section's lines."""
    segments: List[Dict] = []
    visual_plan = ""

    in_segments_list = False
    for raw in lines:
        line = raw.rstrip("\n")
        low = line.strip().lower()

        # Visual plan line.
        if low.startswith("visual plan"):
            after = line.split(":", 1)[1] if ":" in line else ""
            visual_plan = after.strip().strip("`").strip()
            in_segments_list = False
            continue

        # "Render segment:" (single, inline) or "Render segments:" (list follows).
        if low.startswith("render segment"):
            inline = RANGE_RE.search(line)
            if inline:
                segments.append({
                    "start": round(parse_timestamp(inline.group(1)), 3),
                    "end": round(parse_timestamp(inline.group(2)), 3),
                })
                in_segments_list = False
            else:
                # header for a following bulleted list of ranges
                in_segments_list = True
            continue

        # Bulleted ranges following a "Render segments:" header.
        if in_segments_list:
            if line.strip().startswith(("-", "*")):
                m = RANGE_RE.search(line)
                if m:
                    segments.append({
                        "start": round(parse_timestamp(m.group(1)), 3),
                        "end": round(parse_timestamp(m.group(2)), 3),
                    })
                continue
            elif line.strip() == "":
                continue
            else:
                in_segments_list = False

    return {"render_segments": segments, "visual_plan": visual_plan}


def parse_doc(text: str) -> List[Dict]:
    """Return a list of approved-short dicts in document order."""
    lines = text.splitlines()
    sections: List[Dict] = []

    i = 0
    n = len(lines)
    while i < n:
        m = APPROVED_HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        number = int(m.group(1))
        title = m.group(2).strip()

        # Collect lines until the next H3 header.
        body: List[str] = []
        j = i + 1
        while j < n and not ANY_H3_RE.match(lines[j]):
            body.append(lines[j])
            j += 1

        parsed = parse_section(body)
        if not parsed["render_segments"]:
            print(
                f"warning: Approved {number} ({title!r}) has no parseable "
                f"render segments — skipping",
                file=sys.stderr,
            )
        else:
            slug = f"approved{number:02d}_{slugify(title)}"
            sections.append({
                "number": number,
                "id": slug,
                "title": title,
                "layout": derive_layout(parsed["visual_plan"]),
                "visual_plan": parsed["visual_plan"],
                "render_segments": parsed["render_segments"],
            })
        i = j

    return sections


def build_manifest(sections: List[Dict], guest: str, ali: Optional[str]) -> Dict:
    sources: Dict[str, str] = {"guest_video": guest}
    if ali:
        sources["ali_video"] = ali

    shorts: List[Dict] = []
    for sec in sections:
        layout = sec["layout"]
        if layout == "stacked" and not ali:
            print(
                f"warning: {sec['id']} is stacked but no --ali video was given; "
                f"transcribe/render will fail for it.",
                file=sys.stderr,
            )
        duration = round(
            sum(s["end"] - s["start"] for s in sec["render_segments"]), 3
        )
        shorts.append({
            "id": sec["id"],
            "status": "approved",
            "title": sec["title"],
            "layout": layout,
            "canonical_output": f"{sec['id']}.mp4",
            "expected_duration_seconds": [
                round(duration - 2.5, 1),
                round(duration + 2.5, 1),
            ],
            "render_segments": sec["render_segments"],
            "captions": [],
            "words": [],
            "forbidden_phrases": [],
        })

    return {
        "version": 1,
        "sources": sources,
        "house_style": {"captions": {"max_chars": 40, "min_seconds": 0.05}},
        "shorts": shorts,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("draft_shorts", type=Path, help="draft-shorts .md")
    ap.add_argument("--guest", required=True, help="Guest (single-pane) source video")
    ap.add_argument("--ali", default=None, help="Host source video (needed for stacked shorts)")
    ap.add_argument("--out", type=Path, default=None, help="Output manifest JSON (default: stdout)")
    args = ap.parse_args()

    if not args.draft_shorts.exists():
        print(f"error: draft doc not found: {args.draft_shorts}", file=sys.stderr)
        return 2

    sections = parse_doc(args.draft_shorts.read_text())
    if not sections:
        print("error: no `### Approved N:` sections with render segments found",
              file=sys.stderr)
        return 1

    manifest = build_manifest(sections, args.guest, args.ali)

    out_text = json.dumps(manifest, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(out_text)
        print(f"Wrote {len(manifest['shorts'])} approved shorts -> {args.out}")
        for s in manifest["shorts"]:
            segs = s["render_segments"]
            print(f"  {s['id']:36} {s['layout']:10} "
                  f"{len(segs)} seg(s)  ~{s['expected_duration_seconds']}s")
    else:
        print(out_text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
