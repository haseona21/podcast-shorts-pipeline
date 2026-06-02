#!/usr/bin/env python3
"""Find word-aligned (start, end) seconds for a clip in a master JSON.

Greedy in-order token match: locates the first word of the start-text and the
last word of the end-text, using a hint window to disambiguate identical
phrases elsewhere in the episode.

Usage:
    python scripts/srt_cut.py <master.json> \\
        --start-text "so what happened was the maintainer of axios" \\
        --end-text "put these back doors into the packages" \\
        --hint-start 197 --hint-end 302 \\
        [--hint-radius 15] [--tail-pad-max 1.0]

Prints:
    start=<float>
    end=<float>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Keep apostrophes so "we're" tokenizes as one token (not "we" + "re").
WORD_RE = re.compile(r"[a-z0-9']+")


def tokenize(text: str) -> list[str]:
    return WORD_RE.findall(text.lower())


def flatten_tokens(segments: list[dict]) -> list[dict]:
    """Return a flat list of tokens across all segments, each with its
    source word's start/end times. JSON words that contain multiple tokens
    (e.g. "we're" → ["we're"], or edge cases with punctuation) are split into
    multiple entries; times are distributed evenly across sub-tokens.
    """
    out = []
    for seg in segments:
        for w in seg.get("words", []):
            toks = tokenize(w["word"])
            if not toks:
                continue
            w_start = float(w["start"])
            w_end = float(w["end"])
            span = max(w_end - w_start, 0.001)
            n = len(toks)
            for i, t in enumerate(toks):
                out.append({
                    "token": t,
                    "start": w_start + span * (i / n),
                    "end": w_start + span * ((i + 1) / n),
                })
    return out


def greedy_match(
    toks: list[dict],
    phrase: list[str],
    start_i: int,
    max_gap: int = 2,
) -> tuple[int, int, int] | None:
    """Try to match phrase tokens in order, starting at toks[start_i].
    Allow up to max_gap non-matching tokens between phrase tokens.
    Return (first_matched_i, last_matched_i, score) or None.
    """
    j = start_i
    p = 0
    first = None
    last = None
    gap = 0
    while j < len(toks) and p < len(phrase):
        if toks[j]["token"] == phrase[p]:
            if first is None:
                first = j
            last = j
            p += 1
            gap = 0
        else:
            # Only count gap once we've started matching
            if first is not None:
                gap += 1
                if gap > max_gap:
                    break
        j += 1
    if first is None or last is None or p == 0:
        return None
    return first, last, p


def find_phrase(
    toks: list[dict],
    phrase: list[str],
    hint_center: float,
    hint_radius: float,
    min_match_ratio: float = 0.7,
) -> tuple[int, int] | None:
    """Strict in-order match. Returns (first_matched_i, last_matched_i)
    of best candidate, or None.
    """
    if not phrase:
        return None

    starts = [
        i for i, t in enumerate(toks)
        if abs(t["start"] - hint_center) <= hint_radius
    ]
    if not starts:
        return None

    best = None  # (-score, distance, first_i, last_i)
    for i in starts:
        m = greedy_match(toks, phrase, i)
        if m is None:
            continue
        first_i, last_i, score = m
        if score / len(phrase) < min_match_ratio:
            continue
        # Prefer higher score, then closer to hint, then earlier position.
        distance = abs(toks[first_i]["start"] - hint_center)
        cand = (-score, distance, first_i, last_i)
        if best is None or cand < best:
            best = cand

    if best is None:
        return None
    return best[2], best[3]


def find_pause_after(
    toks: list[dict],
    last_i: int,
    max_pad: float,
    min_pause: float = 0.4,
) -> float:
    """If there's a natural pause after the matched last word, extend the cut
    to preserve the breath — but cap the extension well short of the next
    word so we never catch its caption.

    The cap is min(last.end + max_pad, nxt.start - 0.15). Requires gap
    >= min_pause before any padding applies.
    """
    last = toks[last_i]
    if last_i + 1 >= len(toks):
        return last["end"]
    nxt = toks[last_i + 1]
    gap = nxt["start"] - last["end"]
    if gap < min_pause:
        return last["end"]
    # Preserve some breathing room but leave a safety margin before next word.
    return min(last["end"] + max_pad, nxt["start"] - 0.15)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_path", type=Path)
    parser.add_argument("--start-text", required=True)
    parser.add_argument("--end-text", required=True)
    parser.add_argument("--hint-start", type=float, required=True)
    parser.add_argument("--hint-end", type=float, required=True)
    parser.add_argument("--hint-radius", type=float, default=15.0)
    parser.add_argument("--tail-pad-max", type=float, default=0.3)
    args = parser.parse_args()

    if not args.json_path.exists():
        print(f"error: json not found: {args.json_path}", file=sys.stderr)
        return 2

    segments = json.loads(args.json_path.read_text())
    toks = flatten_tokens(segments)

    start_phrase = tokenize(args.start_text)
    end_phrase = tokenize(args.end_text)

    start_match = find_phrase(toks, start_phrase, args.hint_start, args.hint_radius)
    if start_match is None:
        start_match = find_phrase(
            toks, start_phrase, args.hint_start, args.hint_radius * 2,
        )
    if start_match is None:
        print(f"error: start-text not found near {args.hint_start}s: "
              f"{args.start_text!r}", file=sys.stderr)
        return 1

    end_match = find_phrase(toks, end_phrase, args.hint_end, args.hint_radius)
    if end_match is None:
        end_match = find_phrase(
            toks, end_phrase, args.hint_end, args.hint_radius * 2,
        )
    if end_match is None:
        print(f"error: end-text not found near {args.hint_end}s: "
              f"{args.end_text!r}", file=sys.stderr)
        return 1

    start_s = toks[start_match[0]]["start"]
    end_s = find_pause_after(toks, end_match[1], args.tail_pad_max)

    if end_s <= start_s:
        print(f"error: resolved end ({end_s}) <= start ({start_s})",
              file=sys.stderr)
        return 1

    print(f"start={start_s:.3f}")
    print(f"end={end_s:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
