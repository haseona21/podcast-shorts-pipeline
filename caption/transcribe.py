#!/usr/bin/env python3
"""Transcribe each short's cut audio and write clip-local words + captions.

Per approved short:
  1. Cut + concat the short's `render_segments` into one audio clip
     (single source for guest_only/ali_only; both sources amix-ed for stacked,
     since those are dialogue).
  2. Run word-level Whisper on the clip (clip-local timings, t=0 at cut start).
  3. Clean the words VERBATIM — see clean_words().
  4. Group into caption lines and write `words` + `captions` into the manifest.

    python caption/transcribe.py <manifest.json> [--model small.en]

Whisper backend: the local `whisper` CLI (openai-whisper) on PATH; no API key.
`--model` sets the model (default small.en). Install: `pip install openai-whisper`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

# Make the repo root importable so `config` resolves no matter the CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import CFG  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent

# Whole-word mistranscription fixes (case-insensitive; punctuation preserved).
WORD_FIXES = {
    "seeding": "ceding",
    "supercycle": "supercycle",  # canonical form (also normalizes casing)
}

# Adjacent-pair merges, e.g. "super cycle" -> "supercycle".
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
    """Clean Whisper words verbatim: [{"word","start","end"}, ...] in, same out.

    VERBATIM policy: keep every spoken word — filler, hesitations, false starts,
    repeats. The only transforms are mistranscription fixes (WORD_FIXES /
    BIGRAM_FIXES) and sentence-start capitalization. Timings are untouched.
    """
    if not words:
        return []

    work = [dict(w) for w in words]

    # 1. bigram merges (e.g. "super cycle" -> "supercycle")
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

    # 2. single-word fixes
    for w in work:
        w["word"] = _apply_word_fix(w["word"])

    # 3. capitalize the first word and the first word after each sentence end.
    cap_next = True
    for w in work:
        core = w["word"].strip()
        if cap_next and core:
            w["word"] = core[:1].upper() + core[1:]
        cap_next = core.endswith(SENTENCE_END)

    out = [{
        "word": w["word"].strip(),
        "start": round(w["start"], 3),
        "end": round(w["end"], 3),
    } for w in work]
    return out


def group_words(words: List[Dict]) -> List[Dict]:
    """Group cleaned words into caption lines.

    Break before a word that would exceed max_chars or max_words (env-driven,
    defaults 32 / 7), and after any word that ends a sentence.
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
    # Pre-input seek (fast); mirrors render_short.py's cut so transcribed audio
    # matches what ends up in the rendered clip.
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

    Shells out to the `whisper` binary (with its own Python) so this doesn't
    depend on the repo's .venv. Reads the CLI's JSON output and flattens it to
    [{"word","start","end"}, ...].
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


def transcribe_clip(audio: Path, model: str) -> List[Dict]:
    """Word-level transcription of a clip -> [{"word","start","end"}, ...].

    Uses the local whisper CLI (no API key needed).
    """
    return _transcribe_local_cli(audio, model)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", type=Path)
    ap.add_argument("--model", default="small.en",
                    help="Local whisper model name. Default small.en.")
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
            raw_words = transcribe_clip(audio, args.model)

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
