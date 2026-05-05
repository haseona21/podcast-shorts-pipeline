# podcast-shorts-pipeline

Generic Python tooling for turning long-form 16:9 podcast/interview footage into vertical short-form clips. Three layers — reframe, clip selection / cutting, subtitle generation. CLI-first, no project-specific assumptions; designed to be vendored into a producer-side repo.

## Layers

### Reframe

| Script | Purpose |
| --- | --- |
| `reframe_9x16.py` | Single-camera face-tracked 16:9 → 9:16 reframe with EMA smoothing per scene, scene-cut detection, and chunked resumability. |
| `reframe_split_9x16.py` | Side-by-side recordings (1920×1080, left/right halves). Speaker-aware: solo runs ≥5s render as a cropped solo; rapid alternation renders as a stacked split. Speaker detection via MediaPipe FaceLandmarker mouth-openness. |

### Shorts clipper

| Script | Purpose |
| --- | --- |
| `srt_cut.py` | Word-aligned clip boundary finder. Snaps fuzzy `--start-text` / `--end-text` queries to the closest matching word boundaries in a transcribe.json file. |
| `approved_shorts.py` | Parser for "approved shorts" markdown docs. Resolves clip titles, source-angle (final/original × guest/both), timestamps, and transcript excerpts into structured `ApprovedShort` records. |
| `build_fcpxml.py` | Emits a DaVinci Resolve / FCPX-compatible FCPXML with primary stacked layout + alt video lanes for solo crops, plus markers at every approved-clip boundary. |
| `import_fcpxml.py` | Re-cuts shorts from a DaVinci-edited FCPXML, preserving any marker tweaks the editor made. |
| `audit_fcpxml.py` | Validates FCPXML round-trip integrity (timeline frames, markers, asset references). |

### Subtitling

| Script | Purpose |
| --- | --- |
| `transcribe_video.py` | Whisper transcription producing both human-editable SRT and Captacity-compatible word-level JSON. Chunks at 10-min boundaries, resumable. Supports `--initial-prompt` for proper-noun biasing. |
| `caption_video.py` | Burns word-by-word captions onto a reframed video via Captacity. |
| `reconcile_and_caption.py` | Reconciles edits made to the SRT file back into the word-level JSON before caption burning. |

## Requirements

- Python 3.10+
- `mediapipe`, `opencv-python`, `numpy`, `captacity`, `openai-whisper` (or OpenAI API for hosted Whisper)
- `ffmpeg` and `ffprobe` on `PATH`

```
pip install -r requirements.txt
```

The MediaPipe FaceLandmarker model is auto-downloaded on first run and cached.

## Usage as a submodule

In the consumer repo:

```
git submodule add https://github.com/haseona21/podcast-shorts-pipeline.git vendor/podcast-shorts-pipeline
```

Call the scripts via `python vendor/podcast-shorts-pipeline/<script>.py …` — no installation step beyond `pip install -r vendor/podcast-shorts-pipeline/requirements.txt`.

## Design notes

- **CLI-first.** Every script is invokable directly with argparse; no Python API needed.
- **Resumable.** Long-running steps (reframe, transcribe) chunk at safe boundaries and skip completed chunks on re-run.
- **Atomic.** Final outputs written via `.part` siblings, renamed on success.
- **No business logic.** All references to specific guests, brands, or producer-side state live in the consumer repo, never here.
