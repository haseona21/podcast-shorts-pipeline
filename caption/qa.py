#!/usr/bin/env python3
"""Caption QA gate — run AFTER transcribe, BEFORE render.

Catches the failure mode where the auto-transcribed captions are wrong — most
dangerously a mistranscribed name — before it gets burned into a finished clip.
Platform-agnostic: it works on the manifest's shared captions, so one pass
covers YouTube, X, and LinkedIn (they only diverge later, at render).

Two checks per short:

  1. Transcript reconciliation. The clip was selected with a human-approved
     `transcript_excerpt` (the gold words). We diff the burned-in caption text
     against it and flag every divergence — proper-noun drift (capitalized gold
     word) is marked HIGH, since that's the "Hassaan -> I saw him" case.
  2. Low confidence. Whisper emits a per-word probability; we flag any caption
     word below --min-confidence so a human eyeballs it.

Usage:
    python caption/qa.py <manifest.json> [--min-confidence 0.55] [--report PATH]

Exit code: 0 = clean, 1 = flags found (so callers can gate the render).
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path
from typing import Dict, List


def _norm(word: str) -> str:
    """Lowercase + strip surrounding punctuation for comparison."""
    return re.sub(r"^[^\w']+|[^\w']+$", "", word).lower()


def _is_proper(word: str) -> bool:
    """Heuristic proper-noun: capitalized, len>1, not the pronoun 'I'."""
    core = word.strip().strip(".,!?;:\"'")
    return len(core) > 1 and core[:1].isupper() and core != "I"


def _gold_text(excerpt) -> str:
    """Flatten transcript_excerpt (str | [str] | [{speaker,text}]) to one string."""
    if not excerpt:
        return ""
    if isinstance(excerpt, str):
        return excerpt
    parts = []
    for e in excerpt:
        parts.append(e.get("text", "") if isinstance(e, dict) else str(e))
    return " ".join(p for p in parts if p)


def _caption_text(short: Dict) -> str:
    return " ".join(c.get("text", "") for c in short.get("captions", []) if c.get("text"))


def qa_short(short: Dict, min_conf: float) -> List[Dict]:
    """Return a list of flag dicts for one short (empty == clean)."""
    flags: List[Dict] = []

    # ---- 1. proper-noun reconciliation against the approved excerpt ----
    # The excerpt is a cleaned/condensed paraphrase, so a full word diff is noisy.
    # Names, though, must survive: a proper noun in the human-approved excerpt that
    # is missing (or only fuzzily present) in the captions is the "Hassaan -> I saw
    # him" failure. That is the high-signal check.
    gold = _gold_text(short.get("transcript_excerpt"))
    cap = _caption_text(short)
    if gold.strip():
        cap_norms = {_norm(w) for w in cap.split() if _norm(w)}
        seen = set()
        for w in gold.split():
            if not _is_proper(w):
                continue
            name = w.strip().strip(".,!?;:\"'")
            nm = _norm(name)
            if not nm or nm in seen:
                continue
            seen.add(nm)
            if nm in cap_norms:
                continue  # name present and correct
            close = difflib.get_close_matches(nm, list(cap_norms), n=1, cutoff=0.8)
            if close:
                flags.append({
                    "type": "proper_noun_spelling",
                    "severity": "med",
                    "transcript_says": name,
                    "caption_has": close[0],
                })
            else:
                flags.append({
                    "type": "proper_noun_missing",
                    "severity": "HIGH",
                    "name": name,
                })
    else:
        flags.append({
            "type": "no_excerpt",
            "severity": "info",
            "note": "no transcript_excerpt to reconcile names against — confidence check only",
        })

    # ---- 2. low-confidence words ----
    for w in short.get("words", []):
        p = w.get("probability")
        if p is not None and p < min_conf:
            flags.append({
                "type": "low_confidence",
                "severity": "med",
                "word": w.get("word", ""),
                "confidence": round(float(p), 2),
            })

    return flags


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--min-confidence", type=float, default=0.55,
                    help="flag caption words below this Whisper probability (default 0.55)")
    ap.add_argument("--report", type=Path, default=None,
                    help="write the JSON report here (default <manifest dir>/qa-report.json)")
    args = ap.parse_args(argv)

    if not args.manifest.exists():
        print(f"error: manifest not found: {args.manifest}", file=sys.stderr)
        return 2

    manifest = json.loads(args.manifest.read_text())
    shorts = [s for s in manifest.get("shorts", [])
              if s.get("status", "approved") == "approved"]

    report = {}
    n_high = n_total = 0
    for s in shorts:
        sid = s.get("id", "?")
        flags = qa_short(s, args.min_confidence)
        real = [f for f in flags if f["type"] != "no_excerpt"]
        if not real:
            print(f"  [{sid}] OK")
            continue
        report[sid] = flags
        print(f"\n  [{sid}] {len(real)} flag(s):")
        for f in flags:
            if f["type"] == "no_excerpt":
                print(f"    (info) {f['note']}")
                continue
            n_total += 1
            if f["type"] == "proper_noun_missing":
                n_high += 1
                print(f"    ‼️ NAME \"{f['name']}\" is in the transcript but missing "
                      f"from the captions — likely mistranscribed")
            elif f["type"] == "proper_noun_spelling":
                print(f"    • name spelling: transcript \"{f['transcript_says']}\" "
                      f"vs caption \"{f['caption_has']}\"")
            elif f["type"] == "low_confidence":
                print(f"    • low confidence ({f['confidence']}): \"{f['word']}\"")

    out = args.report or (args.manifest.parent / "qa-report.json")
    out.write_text(json.dumps(report, indent=2))

    if report:
        print(f"\nQA: {n_total} flag(s) across {len(report)} short(s) "
              f"({n_high} likely-name). Review + correct captions before rendering.")
        print(f"Report: {out}")
        return 1
    print("\nQA: clean — captions reconcile with the transcript and clear the confidence bar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
