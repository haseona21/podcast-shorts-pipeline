#!/usr/bin/env python3
"""Transcribe each short's cut audio and write clip-local words + captions.

For every approved short in a manifest this:
  1. Cuts + concats the short's `render_segments` into one audio clip.
     - guest_only / ali_only: audio from the single relevant source.
     - stacked: BOTH sources mixed (amix) — these are dialogue, so we want
       both speakers in the transcript.
  2. Runs word-level Whisper on the clip audio (clip-local timings, t=0 at the
     start of the cut).
  3. Cleans the words VERBATIM. Captions transcribe everything actually said,
     including dillydallying (filler words, hesitations, false starts,
     repeated words, "well", "I think", "I'll take a step back"). Do NOT drop
     or smooth them. The ONLY transforms allowed are: fix obvious
     mistranscriptions via a small map (`super cycle`->`supercycle`,
     `seeding`->`ceding`) and capitalize sentence starts. Word timings stay
     exactly as Whisper produced them.
  4. Groups the cleaned words into caption lines (<=32 chars / <=7 words /
     sentence break) and writes `words` + `captions` back into the manifest
     in place.

    python transcribe_captions.py <manifest.json> [--model small.en]

Whisper backend:
  - Default: LOCAL `whisper` CLI (openai-whisper) resolved from PATH. Needs NO
    API key. `--model` sets the local model name (default small.en).
    Install: `pip install openai-whisper`.
  - Optional: OpenAI hosted Whisper (`whisper-1`) via `--backend openai` (or
    TRANSCRIBE_BACKEND=openai); requires OPENAI_API_KEY.

Reuses the transcription engine in transcribe_video.py (same repo) for the
optional hosted-API path only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

from config import CFG

REPO_ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# VERBATIM caption clean.
#
# Captions are VERBATIM — transcribe everything actually said, including
# dillydallying (filler words, hesitations, false starts, repeated words,
# "well", "I think", "I'll take a step back"). Do NOT drop or smooth them; the
# ONLY transforms below are (1) fix obvious mistranscriptions via a small map
# and (2) capitalize sentence starts. Word timings stay exactly as Whisper
# produced them. (Folded in from _respoken/build.py + regen_approved02.py.)
# ---------------------------------------------------------------------------

# Known mistranscription fixes (whole-word, case-insensitive). Punctuation on
# the token is preserved.
WORD_FIXES = {
    "seeding": "ceding",
    "supercycle": "supercycle",  # canonical form (handles capitalized too)
}

# Two-token collapses: when these adjacent lowercased word pairs appear, merge
# them into the single corrected token (e.g. "super cycle" -> "supercycle").
BIGRAM_FIXES = {
    ("super", "cycle"): "supercycle",
}

SENTENCE_END = (".", "?", "!")


def _bare(word: str) -> str:
    return word.strip().strip(".,!?;:\"'").lower()


def _apply_word_fix(word: str) -> str:
    """Fix a single token, preserving leading space + trailing punctuation."""
    lead = " " if word.startswith(" ") else ""
    core = word.strip()
    # split trailing punctuation
    trail = ""
    while core and core[-1] in ".,!?;:\"'":
        trail = core[-1] + trail
        core = core[:-1]
    fix = WORD_FIXES.get(core.lower())
    if fix is not None:
        # preserve capitalization of original first letter
        if core[:1].isupper():
            fix = fix[:1].upper() + fix[1:]
        core = fix
    return lead + core + trail


def clean_words(words: List[Dict]) -> List[Dict]:
    """words: [{"word","start","end"}, ...] with bare words (no leading space).

    VERBATIM policy: keep every spoken word — filler, hesitations, false
    starts, repeated words, dillydallying. Do NOT drop leading filler and do
    NOT collapse immediate stutter duplicates. The only transforms are obvious
    mistranscription fixes and sentence-start capitalization. Word timings are
    left exactly as Whisper produced them (no re-zeroing).
    """
    if not words:
        return []

    work = [dict(w) for w in words]

    # 1. bigram mistranscription merges (e.g. "super cycle" -> "supercycle")
    merged: List[Dict] = []
    i = 0
    while i < len(work):
        if i + 1 < len(work):
            pair = (_bare(work[i]["word"]), _bare(work[i + 1]["word"]))
            if pair in BIGRAM_FIXES:
                fixed = BIGRAM_FIXES[pair]
                trail = ""
                w2 = work[i + 1]["word"].strip()
                while w2 and w2[-1] in ".,!?;:\"'":
                    trail = w2[-1] + trail
                    w2 = w2[:-1]
                merged.append({
                    "word": fixed + trail,
                    "start": work[i]["start"],
                    "end": work[i + 1]["end"],
                })
                i += 2
                continue
        merged.append(dict(work[i]))
        i += 1
    work = merged

    # 2. single-word mistranscription fixes
    for w in work:
        w["word"] = _apply_word_fix(w["word"])

    # NOTE: no stutter collapse and no leading-filler drop — captions are
    # verbatim, so repeated/dillydallying words are kept.

    # 3. capitalize the first word, and the first word after a sentence end.
    cap_next = True
    for w in work:
        core = w["word"].strip()
        if cap_next and core:
            w["word"] = core[:1].upper() + core[1:]
        cap_next = core.endswith(SENTENCE_END)

    # 4. emit words verbatim with Whisper's original timings (no re-zeroing).
    out = [{
        "word": w["word"].strip(),
        "start": round(w["start"], 3),
        "end": round(w["end"], 3),
    } for w in work]
    return out


def group_words(words: List[Dict]) -> List[Dict]:
    """Group cleaned words into caption lines.

    Break to a new line when adding the next word would exceed SHORTS_MAX_CHARS
    chars or SHORTS_MAX_WORDS_PER_LINE words (defaults 32 / 7), or after any word
    that ends a sentence. Caps are env-driven via config.py. (From
    gen_manifests.py.)
    """
    max_chars = CFG.caption.max_chars
    max_words = CFG.caption.max_words_per_line
    lines: List[List[Dict]] = []
    cur: List[Dict] = []
    for w in words:
        cand = (" ".join([x["word"] for x in cur] + [w["word"]])).strip()
        if cur and (len(cand) > max_chars or len(cur) >= max_words):
            lines.append(cur)
            cur = [w]
        else:
            cur.append(w)
        if w["word"].rstrip().endswith(SENTENCE_END):
            lines.append(cur)
            cur = []
    if cur:
        lines.append(cur)

    return [{
        "start": round(ln[0]["start"], 3),
        "end": round(ln[-1]["end"], 3),
        "text": " ".join(x["word"] for x in ln).strip(),
    } for ln in lines]


# ---------------------------------------------------------------------------
# Audio cutting
# ---------------------------------------------------------------------------

def _cut_single_audio(src: Path, start: float, end: float, out: Path) -> None:
    # Pre-input seek (fast). Timings get re-zeroed to the first kept word, and
    # this mirrors the keyframe-aligned cut render_short.py makes, so the audio
    # we transcribe matches the audio that ends up in the rendered clip.
    subprocess.run([
        "ffmpeg", "-y", "-ss", f"{start}", "-to", f"{end}", "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libmp3lame", "-b:a", "96k", str(out),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _cut_mixed_audio(ali: Path, guest: Path, start: float, end: float, out: Path) -> None:
    """Mix both speaker tracks for [start,end) into one mono clip."""
    subprocess.run([
        "ffmpeg", "-y",
        "-ss", f"{start}", "-to", f"{end}", "-i", str(ali),
        "-ss", f"{start}", "-to", f"{end}", "-i", str(guest),
        "-filter_complex",
        "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[a]",
        "-map", "[a]",
        "-ac", "1", "-ar", "16000",
        "-c:a", "libmp3lame", "-b:a", "96k", str(out),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _concat_audio(parts: List[Path], out: Path, workdir: Path) -> None:
    if len(parts) == 1:
        subprocess.run(["cp", str(parts[0]), str(out)], check=True)
        return
    lst = workdir / "concat.txt"
    lst.write_text("\n".join(f"file '{p.resolve()}'" for p in parts) + "\n")
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(lst), "-c", "copy", str(out),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def build_clip_audio(short: Dict, sources: Dict, workdir: Path) -> Path:
    layout = short["layout"]
    segs = short["render_segments"]
    parts: List[Path] = []

    if layout in ("guest_only", "ali_only"):
        key = "guest_video" if layout == "guest_only" else "ali_video"
        src = Path(sources[key])
        for i, s in enumerate(segs):
            p = workdir / f"a_{i:03d}.mp3"
            _cut_single_audio(src, float(s["start"]), float(s["end"]), p)
            parts.append(p)
    elif layout == "stacked":
        ali = Path(sources["ali_video"])
        guest = Path(sources["guest_video"])
        for i, s in enumerate(segs):
            p = workdir / f"a_{i:03d}.mp3"
            _cut_mixed_audio(ali, guest, float(s["start"]), float(s["end"]), p)
            parts.append(p)
    else:
        raise ValueError(f"unknown layout: {layout!r}")

    clip = workdir / "clip.mp3"
    _concat_audio(parts, clip, workdir)
    return clip


# ---------------------------------------------------------------------------

def _transcribe_local_cli(audio: Path, model: str) -> List[Dict]:
    """Word-level transcription via the system `whisper` CLI on PATH.

    This deliberately shells out to the `whisper` binary (which carries its own
    Python) instead of importing whisper in-process, so it does NOT depend on
    this repo's .venv (which lacks whisper). No API key required.

    Runs the CLI in a TemporaryDirectory, reads the produced JSON
    (<wav-basename>.json with segments[].words[]) and flattens it to
    [{"word","start","end"}, ...] with the leading space whisper adds stripped.
    """
    whisper_bin = shutil.which("whisper")
    if not whisper_bin:
        raise SystemExit(
            "error: local `whisper` CLI not found on PATH. "
            "Install with `pip install openai-whisper`."
        )

    with tempfile.TemporaryDirectory(prefix="whisper_cli_") as td:
        out_dir = Path(td)
        subprocess.run(
            [
                whisper_bin, str(audio),
                "--model", model,
                "--language", "en",
                "--word_timestamps", "True",
                "--output_format", "json",
                "--output_dir", str(out_dir),
                "--verbose", "False",
            ],
            check=True,
        )
        json_path = out_dir / (audio.stem + ".json")
        if not json_path.exists():
            raise SystemExit(f"error: whisper produced no JSON at {json_path}")
        data = json.loads(json_path.read_text())

    words: List[Dict] = []
    for seg in data.get("segments", []):
        for w in seg.get("words", []):
            words.append({
                "word": str(w["word"]).strip(),  # whisper prefixes a space
                "start": float(w["start"]),
                "end": float(w["end"]),
            })
    return words


def transcribe_clip(audio: Path, model: str, backend: str) -> List[Dict]:
    """Word-level transcription of a clip -> [{"word","start","end"}, ...]."""
    if backend == "openai":
        if not os.getenv("OPENAI_API_KEY"):
            raise SystemExit(
                "error: --backend openai requires OPENAI_API_KEY to be set"
            )
        import transcribe_video as tv  # lazy: only the hosted-API path needs it
        segments = tv.transcribe_api(audio, "")
        words: List[Dict] = []
        for seg in segments:
            for w in seg.words:
                words.append({
                    "word": w.word.strip(),
                    "start": float(w.start),
                    "end": float(w.end),
                })
        return words

    # Default: local whisper CLI (no API key needed).
    return _transcribe_local_cli(audio, model)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--model", default="small.en",
                    help="Local whisper model name. Default small.en.")
    ap.add_argument("--backend",
                    choices=["local", "openai"],
                    default=os.getenv("TRANSCRIBE_BACKEND", "local"),
                    help="Transcription backend. 'local' (default) shells out "
                         "to the `whisper` CLI on PATH (no API key). 'openai' "
                         "uses the hosted Whisper API (needs OPENAI_API_KEY).")
    args = ap.parse_args()

    if not args.manifest.exists():
        print(f"error: manifest not found: {args.manifest}", file=sys.stderr)
        return 2

    manifest = json.loads(args.manifest.read_text())
    sources = manifest["sources"]
    shorts = [s for s in manifest.get("shorts", [])
              if s.get("status", "approved") == "approved"]

    for short in shorts:
        print(f"=== Transcribing {short['id']} (layout={short['layout']}) ===")
        with tempfile.TemporaryDirectory(prefix="transcribe_caps_") as td:
            workdir = Path(td)
            audio = build_clip_audio(short, sources, workdir)
            raw_words = transcribe_clip(audio, args.model, args.backend)

        cleaned = clean_words(raw_words)
        captions = group_words(cleaned)
        short["words"] = cleaned
        short["captions"] = captions
        excerpt = " ".join(c["text"] for c in captions)
        short["transcript_excerpt"] = [{"speaker": "clip", "text": excerpt}]
        print(f"    {len(raw_words)} raw words -> {len(cleaned)} cleaned, "
              f"{len(captions)} caption lines")

    args.manifest.write_text(json.dumps(manifest, indent=2))
    print(f"Updated {args.manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
