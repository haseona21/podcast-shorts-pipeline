#!/usr/bin/env python3
"""Transcribe a video to SRT + word-level JSON for caption burning.

Outputs two files:
  <output>.srt   — human-readable, editable for proper-noun fixes
  <output>.json  — word-level segments in Captacity-compatible format

The JSON is what caption_video.py feeds to Captacity.add_captions(segments=...).
If --review is used, edits to the .srt are reconciled back into the .json
before we exit.

Usage:
    python scripts/transcribe_video.py input.mp4 output.srt \\
        [--review] [--chunks-dir DIR] [--initial-prompt "names"]

Env:
    OPENAI_API_KEY                Required unless CAPTACITY_USE_LOCAL_WHISPER=1.
    CAPTACITY_USE_LOCAL_WHISPER=1 Use local openai-whisper instead of API.
    WHISPER_LOCAL_MODEL           Local model name (default: medium).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

CHUNK_SECONDS = 10 * 60


@dataclass
class Word:
    word: str
    start: float
    end: float


@dataclass
class Segment:
    start: float
    end: float
    words: List[Word]

    @property
    def text(self) -> str:
        return "".join(w.word for w in self.words).strip()


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    return float(result.stdout.strip())


def extract_audio_chunk(src: Path, start: float, end: float, out: Path) -> None:
    # -ss / -to AFTER -i forces ffmpeg to decode from frame 0 and stop
    # at the exact seek point. The fast pre-input seek (-ss before -i)
    # is keyframe-aligned and drifts by up to a few seconds per chunk,
    # which compounds across a 6-chunk transcription and makes the SRT
    # gradually fall out of sync with the audio. Decode-then-seek is
    # slower but sample-accurate.
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", str(src),
            "-ss", f"{start}", "-to", f"{end}",
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "libmp3lame", "-b:a", "64k", str(out),
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def transcribe_api(audio_path: Path, prompt: str) -> List[Segment]:
    import openai  # type: ignore

    with open(audio_path, "rb") as f:
        kwargs = {
            "model": "whisper-1",
            "file": f,
            "response_format": "verbose_json",
            "timestamp_granularities": ["segment", "word"],
        }
        if prompt:
            kwargs["prompt"] = prompt
        transcript = openai.audio.transcriptions.create(**kwargs)
    all_words = [
        Word(
            word=" " + (w["word"] if isinstance(w, dict) else w.word).lstrip(),
            start=float(w["start"] if isinstance(w, dict) else w.start),
            end=float(w["end"] if isinstance(w, dict) else w.end),
        )
        for w in transcript.words
    ]
    segments_raw = transcript.segments
    segments: List[Segment] = []
    for s in segments_raw:
        s_start = float(s["start"] if isinstance(s, dict) else s.start)
        s_end = float(s["end"] if isinstance(s, dict) else s.end)
        seg_words = [w for w in all_words if s_start - 0.01 <= w.start < s_end + 0.01]
        if not seg_words:
            continue
        segments.append(Segment(start=s_start, end=s_end, words=seg_words))
    if not segments and all_words:
        segments = [Segment(
            start=all_words[0].start, end=all_words[-1].end, words=all_words,
        )]
    return segments


def transcribe_local(audio_path: Path, prompt: str) -> List[Segment]:
    import whisper  # type: ignore

    model_name = os.getenv("WHISPER_LOCAL_MODEL", "medium")
    model = whisper.load_model(model_name)
    kwargs = {"word_timestamps": True, "fp16": False}
    if prompt:
        kwargs["initial_prompt"] = prompt
    result = model.transcribe(str(audio_path), **kwargs)

    segments: List[Segment] = []
    for seg in result["segments"]:
        words = [
            Word(
                word=w["word"],
                start=float(w["start"]),
                end=float(w["end"]),
            )
            for w in seg.get("words", [])
        ]
        if not words:
            continue
        segments.append(Segment(
            start=float(seg["start"]), end=float(seg["end"]), words=words,
        ))
    return segments


def offset_segments(segments: List[Segment], offset: float) -> List[Segment]:
    out: List[Segment] = []
    for s in segments:
        out.append(Segment(
            start=s.start + offset,
            end=s.end + offset,
            words=[Word(word=w.word, start=w.start + offset, end=w.end + offset)
                   for w in s.words],
        ))
    return out


def segments_to_dicts(segments: List[Segment]) -> List[Dict]:
    return [
        {
            "start": s.start,
            "end": s.end,
            "words": [
                {"word": w.word, "start": w.start, "end": w.end}
                for w in s.words
            ],
        }
        for s in segments
    ]


def dicts_to_segments(data: List[Dict]) -> List[Segment]:
    return [
        Segment(
            start=float(d["start"]),
            end=float(d["end"]),
            words=[Word(word=w["word"], start=float(w["start"]), end=float(w["end"]))
                   for w in d["words"]],
        )
        for d in data
    ]


def format_ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments: List[Segment]) -> str:
    lines: List[str] = []
    for i, s in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(f"{format_ts(s.start)} --> {format_ts(s.end)}")
        lines.append(s.text)
        lines.append("")
    return "\n".join(lines)


SRT_TS_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})"
)


@dataclass
class SrtEntry:
    start: float
    end: float
    text: str


def parse_srt(text: str) -> List[SrtEntry]:
    entries: List[SrtEntry] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        ts_line = lines[1] if SRT_TS_RE.search(lines[0]) is None else lines[0]
        body_start = 2 if ts_line is lines[1] else 1
        match = SRT_TS_RE.search(ts_line)
        if not match:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = match.groups()
        start = int(h1) * 3600 + int(m1) * 60 + int(s1) + int(ms1) / 1000
        end = int(h2) * 3600 + int(m2) * 60 + int(s2) + int(ms2) / 1000
        body = " ".join(lines[body_start:])
        entries.append(SrtEntry(start=start, end=end, text=body.strip()))
    return entries


def reconcile(segments: List[Segment], edited: List[SrtEntry]) -> List[Segment]:
    """Merge edited SRT text back into word-level segments.

    Match by approximate (start, end) overlap. For each matched segment, retokenize
    the edited text and preserve original word timestamps when counts match;
    otherwise redistribute evenly across the segment duration.
    """
    if not edited:
        return segments

    out: List[Segment] = []
    for seg in segments:
        match = None
        for e in edited:
            if abs(e.start - seg.start) < 0.5 and abs(e.end - seg.end) < 0.5:
                match = e
                break
        if match is None or match.text == seg.text:
            out.append(seg)
            continue

        new_tokens = match.text.split()
        if len(new_tokens) == len(seg.words):
            new_words = [
                Word(
                    word=(" " + tok) if old.word.startswith(" ") else tok,
                    start=old.start,
                    end=old.end,
                )
                for tok, old in zip(new_tokens, seg.words)
            ]
        else:
            duration = max(seg.end - seg.start, 0.01)
            step = duration / max(len(new_tokens), 1)
            new_words = [
                Word(
                    word=" " + tok,
                    start=seg.start + i * step,
                    end=seg.start + (i + 1) * step,
                )
                for i, tok in enumerate(new_tokens)
            ]
        out.append(Segment(start=seg.start, end=seg.end, words=new_words))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path, help="Output SRT path; .json sidecar written alongside.")
    parser.add_argument("--chunks-dir", type=Path, default=None)
    parser.add_argument("--initial-prompt", default="")
    parser.add_argument("--review", action="store_true")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"error: input not found: {args.input}", file=sys.stderr)
        return 2

    chunks_dir = args.chunks_dir or args.output.with_name(args.output.stem + "_chunks")
    chunks_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    json_path = args.output.with_suffix(".json")

    use_local = os.getenv("CAPTACITY_USE_LOCAL_WHISPER", "").lower() in {"1", "true", "yes"}
    if not use_local and not os.getenv("OPENAI_API_KEY"):
        print("error: OPENAI_API_KEY not set and CAPTACITY_USE_LOCAL_WHISPER is off", file=sys.stderr)
        return 2

    duration = probe_duration(args.input)
    num_chunks = max(1, math.ceil(duration / CHUNK_SECONDS))
    print(f"Input duration: {duration/60:.1f} min — {num_chunks} chunks")
    print(f"Whisper mode: {'local' if use_local else 'API'}")

    all_segments: List[Segment] = []
    for i in range(num_chunks):
        start = i * CHUNK_SECONDS
        end = min(duration, start + CHUNK_SECONDS)
        cache_path = chunks_dir / f"transcript_{i:03d}.json"

        if cache_path.exists() and cache_path.stat().st_size > 0:
            print(f"  [chunk {i:03d}] cached, loading")
            chunk_segments = dicts_to_segments(json.loads(cache_path.read_text()))
        else:
            print(f"  [chunk {i:03d}] transcribing {start/60:.1f}-{end/60:.1f} min")
            with tempfile.TemporaryDirectory() as td:
                audio_path = Path(td) / "chunk.mp3"
                extract_audio_chunk(args.input, start, end, audio_path)
                if use_local:
                    chunk_segments = transcribe_local(audio_path, args.initial_prompt)
                else:
                    chunk_segments = transcribe_api(audio_path, args.initial_prompt)
            cache_path.write_text(json.dumps(segments_to_dicts(chunk_segments)))

        all_segments.extend(offset_segments(chunk_segments, offset=start))

    args.output.write_text(segments_to_srt(all_segments))
    json_path.write_text(json.dumps(segments_to_dicts(all_segments), indent=2))
    print(f"Wrote SRT:  {args.output}")
    print(f"Wrote JSON: {json_path}")

    if args.review:
        print()
        print("=" * 60)
        print(f"SRT written to: {args.output}")
        print("Open it in your editor, fix any proper-noun mistakes, save,")
        print("then return here and press enter to continue...")
        print("=" * 60)
        try:
            input()
        except EOFError:
            pass
        edited = parse_srt(args.output.read_text())
        reconciled = reconcile(all_segments, edited)
        json_path.write_text(json.dumps(segments_to_dicts(reconciled), indent=2))
        print(f"Re-synced JSON from reviewed SRT: {json_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
