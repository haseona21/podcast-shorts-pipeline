#!/usr/bin/env python3
"""Parse a draft-shorts doc into a render manifest JSON.

Emits one manifest entry per `### Approved N: <Title>` section (other headers,
e.g. `### Candidate`, are skipped) with its sources, layout, render segments,
and slug. `words`/`captions` are left empty for caption/transcribe.py to fill.

    python manifest/make_manifest.py <draft_shorts.md> \\
        --guest <guest_video.mp4> [--ali <ali_video.mp4>] [--out manifest.json]

Section format:

    ### Approved N: <Title>
    Render segment: `MM:SS.mmm to MM:SS.mmm`     # single
      -- or --
    Render segments:
    - `MM:SS.mmm to MM:SS.mmm` -- note           # list
    Visual plan: `guest-only` / `Both faces` / `... stacked ...`

Layout from the Visual plan line: "Both faces" / "stacked" / "split" -> stacked
(checked first, so "Try X-only first; otherwise both" -> stacked); explicit
"Ali-only" -> ali_only; any other "... only ..." -> guest_only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

APPROVED_HEADER_RE = re.compile(r"^###\s+Approved\s+(\d+)\s*:\s*(.+?)\s*$")
ANY_H3_RE = re.compile(r"^###\s+")  # section boundary
# `MM:SS.mmm to MM:SS.mmm` (also accepts H:MM:SS and integer seconds)
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
    # "both"/"stacked"/"split" win first, so "Try X-only first; otherwise both
    # faces" deterministically renders the safe both-faces fallback.
    if "both" in vp or "stacked" in vp or "split" in vp:
        return "stacked"
    # Honor an explicit Ali-only plan (don't fold it into guest_only).
    if "ali-only" in vp or "ali only" in vp:
        return "ali_only"
    if "only" in vp:
        return "guest_only"
    return "guest_only"  # default: simplest single-pane


def parse_section(lines: List[str]) -> Dict:
    """Pull render segments, visual plan, and transcript excerpt out of one
    Approved section's lines. The excerpt (the human-approved words) is the gold
    the caption QA stage reconciles names against."""
    segments: List[Dict] = []
    visual_plan = ""
    excerpt_parts: List[str] = []

    in_segments_list = False
    in_excerpt = False
    # labels that end a transcript-excerpt block
    FIELDS = ("visual plan", "render segment", "render segments", "linkedin caption",
              "why linkedin", "hashtags", "hook", "title", "caption", "duration",
              "timestamp", "x caption", "post copy")

    for raw in lines:
        line = raw.rstrip("\n")
        low = line.strip().lower()

        # "Transcript excerpt:" — inline text and/or following blockquote/prose.
        if low.startswith("transcript excerpt"):
            after = (line.split(":", 1)[1] if ":" in line else "").strip().strip("`")
            after = after.lstrip(">").strip()
            if after:
                excerpt_parts.append(after)
            in_excerpt = True
            in_segments_list = False
            continue

        if in_excerpt:
            s = line.strip()
            if s.startswith(">"):
                excerpt_parts.append(s.lstrip(">").strip())
                continue
            if s == "":
                in_excerpt = False
                continue
            if any(low.startswith(p) for p in FIELDS):
                in_excerpt = False  # fall through; handled as a field below
            else:
                excerpt_parts.append(s.strip("`").strip())
                continue

        if low.startswith("visual plan"):
            after = line.split(":", 1)[1] if ":" in line else ""
            visual_plan = after.strip().strip("`").strip()
            in_segments_list = False
            continue

        # "Render segment:" (inline range) or "Render segments:" (list follows).
        if low.startswith("render segment"):
            inline = RANGE_RE.search(line)
            if inline:
                segments.append({
                    "start": round(parse_timestamp(inline.group(1)), 3),
                    "end": round(parse_timestamp(inline.group(2)), 3),
                })
                in_segments_list = False
            else:
                in_segments_list = True
            continue

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

    excerpt = " ".join(p for p in excerpt_parts if p).strip()
    return {"render_segments": segments, "visual_plan": visual_plan,
            "transcript_excerpt": excerpt}


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
                "transcript_excerpt": parsed.get("transcript_excerpt", ""),
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
            "transcript_excerpt": sec.get("transcript_excerpt", ""),
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
