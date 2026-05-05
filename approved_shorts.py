#!/usr/bin/env python3
"""Parse `shorts-approved-for-mae.md` and resolve clip boundaries.

Pulls each approved short with title, timestamp range, transcript excerpt, and
source_type (from the `## Section Header`). Boundaries are resolved by feeding
the first/last few words of the excerpt into srt_cut.find_phrase, with the
parsed timestamp as the hint. Falls back to the raw timestamp if no excerpt is
present or the phrase doesn't match.

Usage as CLI (test/dry-run):
    python scripts/approved_shorts.py <approved.md> <transcript.json>
        [--hint-radius 20] [--snippet-words 7]

Usage as module:
    from approved_shorts import parse_approved_md, resolve_boundaries
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# srt_cut is in the same scripts dir; allow direct import
sys.path.insert(0, str(Path(__file__).resolve().parent))
import srt_cut  # noqa: E402

SOURCE_TYPES = {
    "final (guest only)", "final guest-only",
    "final (both)", "final both",
    "original (guest only)", "original guest-only",
    "original (both)", "original both",
}

SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")
# Two clip-header formats supported:
#   bold inline:    **Short 1: title**
#   markdown h2:    ## Clip 1: title | source | extra
SHORT_RE = re.compile(r"^\*\*Short\s+(\d+)\s*:\s*(.+?)\*\*\s*$", re.IGNORECASE)
CLIP_HEADING_RE = re.compile(
    r"^##\s+Clip\s+(\d+)\s*:\s*(.+?)(?:\s*\|.*)?\s*$", re.IGNORECASE,
)
# Two formats: "Timestamp: ~3:17 - 5:02" or "**Timestamps:** 03:34 - 05:10"
TS_RE = re.compile(
    r"^\**\s*Timestamps?\s*:?\s*\**\s*~?\s*"
    r"(\d+):(\d{2})(?::(\d{2}))?\s*[–—\-]\s*"
    r"~?\s*(\d+):(\d{2})(?::(\d{2}))?",
    re.IGNORECASE,
)
EXCERPT_HEADER_RE = re.compile(r"^\*\*Transcript\s+excerpt\*?\*?\s*:?\s*\*?\*?\s*$",
                               re.IGNORECASE)
FIELD_RE = re.compile(r"^\*\*[^*]+\*\*\s*:?")
SPEAKER_LINE_RE = re.compile(r"^>\s*[A-Z][\w'\-\.]*(\s+[\w'\-\.]+)*"
                             r"(\s*\([^)]+\))?\s*$")


@dataclass
class ApprovedShort:
    number: int                 # "Short N" number
    title: str                  # human-readable title (no numbering)
    ts_start: float             # seconds, parsed from md doc
    ts_end: float
    excerpt: str                # transcript excerpt body, joined paragraphs
    source_type: str            # normalized, e.g. "final-guest-only"

    @property
    def slug(self) -> str:
        slug_title = re.sub(r"[^a-z0-9]+", "-", self.title.lower()).strip("-")
        return f"short{self.number:02d}-{slug_title}"


def _parse_ts(h_or_m: str, m_or_s: str, s: Optional[str]) -> float:
    """Parse mm:ss or hh:mm:ss into seconds. ss is optional (then h_or_m=mm)."""
    if s:
        return int(h_or_m) * 3600 + int(m_or_s) * 60 + int(s)
    return int(h_or_m) * 60 + int(m_or_s)


def _normalize_source(label: str) -> str:
    """Normalize section header to a canonical source_type slug."""
    s = label.lower().strip()
    s = s.replace("(", " ").replace(")", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.replace("guest only", "guest")
    parts = s.split()
    # Expected shape: ["final", "guest"] or ["original", "both"]
    if len(parts) < 2:
        return s.replace(" ", "-")
    base = parts[0]              # final | original
    angle = "both" if "both" in parts else "guest"
    return f"{base}-{angle}"


def parse_approved_md(path: Path) -> list[ApprovedShort]:
    text = path.read_text()
    lines = text.splitlines()

    out: list[ApprovedShort] = []
    current_source: Optional[str] = None
    i = 0

    while i < len(lines):
        line = lines[i]

        # Section header (## Source Type) — not a clip heading
        m = SECTION_RE.match(line)
        clip_m = CLIP_HEADING_RE.match(line) if m else None
        if m and not clip_m:
            label = m.group(1).strip()
            if label.lower().strip() in SOURCE_TYPES:
                current_source = _normalize_source(label)
            i += 1
            continue

        # Clip header — either "**Short N: ...**" or "## Clip N: ..."
        m = SHORT_RE.match(line) or clip_m
        if not m:
            i += 1
            continue

        number = int(m.group(1))
        title = m.group(2).strip().strip('"').strip("'")

        # Look for Timestamp line within the next ~10 lines
        ts_start = ts_end = None
        for j in range(i + 1, min(i + 12, len(lines))):
            tm = TS_RE.match(lines[j].strip())
            if tm:
                ts_start = _parse_ts(tm.group(1), tm.group(2), tm.group(3))
                ts_end = _parse_ts(tm.group(4), tm.group(5), tm.group(6))
                break

        # Look for Transcript excerpt blockquote
        excerpt = ""
        for j in range(i + 1, min(i + 200, len(lines))):
            if EXCERPT_HEADER_RE.match(lines[j].strip()):
                # Collect blockquote lines until next field header or section
                excerpt_lines: list[str] = []
                k = j + 1
                while k < len(lines):
                    raw = lines[k].rstrip()
                    stripped = raw.strip()
                    if not stripped:
                        excerpt_lines.append("")
                        k += 1
                        continue
                    if stripped.startswith(">"):
                        body = stripped[1:].strip()
                        # Skip "> Speaker (~3:17)" attribution lines
                        if SPEAKER_LINE_RE.match(stripped):
                            k += 1
                            continue
                        excerpt_lines.append(body)
                        k += 1
                        continue
                    # End of blockquote
                    break
                excerpt = " ".join(s for s in excerpt_lines if s).strip()
                excerpt = re.sub(r"\s+", " ", excerpt)
                break

        if ts_start is None or ts_end is None:
            print(f"warn: skipping Short {number} {title!r} — no timestamp",
                  file=sys.stderr)
            i += 1
            continue

        if current_source is None:
            print(f"warn: Short {number} {title!r} has no preceding source "
                  f"section header; defaulting to final-guest", file=sys.stderr)
            source = "final-guest"
        else:
            source = current_source

        out.append(ApprovedShort(
            number=number, title=title,
            ts_start=ts_start, ts_end=ts_end,
            excerpt=excerpt, source_type=source,
        ))
        i += 1

    return out


def _extract_phrase(excerpt: str, take: int, from_end: bool = False) -> str:
    if not excerpt:
        return ""
    words = excerpt.split()
    if from_end:
        return " ".join(words[-take:])
    return " ".join(words[:take])


def resolve_boundaries(
    short: ApprovedShort,
    transcript_segments: list[dict],
    snippet_words: int = 7,
    hint_radius: float = 20.0,
    tail_pad_max: float = 0.3,
) -> tuple[float, float]:
    """Resolve word-aligned (start, end) seconds for an approved short.

    Strategy: extract first/last `snippet_words` from excerpt, feed to
    srt_cut.find_phrase with the doc timestamp as hint. Fall back to the raw
    doc timestamp if either match fails.
    """
    if not transcript_segments or not short.excerpt:
        return short.ts_start, short.ts_end

    toks = srt_cut.flatten_tokens(transcript_segments)
    start_phrase_text = _extract_phrase(short.excerpt, snippet_words)
    end_phrase_text = _extract_phrase(short.excerpt, snippet_words, from_end=True)

    start_phrase = srt_cut.tokenize(start_phrase_text)
    end_phrase = srt_cut.tokenize(end_phrase_text)

    start_match = srt_cut.find_phrase(toks, start_phrase, short.ts_start, hint_radius)
    if start_match is None:
        start_match = srt_cut.find_phrase(toks, start_phrase, short.ts_start,
                                          hint_radius * 2)

    end_match = srt_cut.find_phrase(toks, end_phrase, short.ts_end, hint_radius)
    if end_match is None:
        end_match = srt_cut.find_phrase(toks, end_phrase, short.ts_end,
                                        hint_radius * 2)

    if start_match is None or end_match is None:
        return short.ts_start, short.ts_end

    start_s = toks[start_match[0]]["start"]
    end_s = srt_cut.find_pause_after(toks, end_match[1], tail_pad_max)
    if end_s <= start_s:
        return short.ts_start, short.ts_end
    return start_s, end_s


def snap_overlaps(resolved: list[tuple[ApprovedShort, float, float]]
                  ) -> list[tuple[ApprovedShort, float, float]]:
    """Snap each clip's end down to the earliest later clip's start that falls
    inside its range, so adjacent clips don't overlap.
    """
    starts = sorted(s for _, s, _ in resolved)
    out: list[tuple[ApprovedShort, float, float]] = []
    for short, start, end in resolved:
        snap_to = next((s for s in starts if start < s < end), None)
        if snap_to is not None:
            end = snap_to
        out.append((short, start, end))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("approved_md", type=Path)
    parser.add_argument("transcript_json", type=Path,
                        help="Word-level transcript JSON from transcribe_video.py")
    parser.add_argument("--hint-radius", type=float, default=20.0)
    parser.add_argument("--snippet-words", type=int, default=7)
    args = parser.parse_args()

    if not args.approved_md.exists():
        print(f"error: {args.approved_md} not found", file=sys.stderr)
        return 2
    if not args.transcript_json.exists():
        print(f"error: {args.transcript_json} not found", file=sys.stderr)
        return 2

    shorts = parse_approved_md(args.approved_md)
    if not shorts:
        print("warn: no approved shorts parsed", file=sys.stderr)
        return 1

    segments = json.loads(args.transcript_json.read_text())

    raw = []
    for s in shorts:
        rs, re_ = resolve_boundaries(
            s, segments,
            snippet_words=args.snippet_words,
            hint_radius=args.hint_radius,
        )
        raw.append((s, rs, re_))

    snapped = snap_overlaps(raw)

    print(f"{'#':<3} {'SOURCE':<14} {'TITLE':<40} "
          f"{'DOC':>17}  {'RESOLVED':>17}  DUR")
    for s, start, end in snapped:
        doc_range = f"{s.ts_start:6.1f}-{s.ts_end:6.1f}"
        res_range = f"{start:6.1f}-{end:6.1f}"
        delta_s = start - s.ts_start
        delta_e = end - s.ts_end
        marker = "" if abs(delta_s) < 1 and abs(delta_e) < 5 else " *"
        print(f"{s.number:<3} {s.source_type:<14} {s.title[:38]:<40} "
              f"{doc_range:>17}  {res_range:>17}  {end - start:5.1f}s{marker}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
