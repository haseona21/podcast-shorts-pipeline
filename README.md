# podcast-shorts-pipeline

Generic Python tooling for turning long-form 16:9 podcast/interview footage into vertical short-form clips. CLI-first, no project-specific assumptions; designed to be vendored into a producer-side repo.

## Layout

```
generate_shorts.py          # entrypoint: draft-shorts doc -> finished shorts
render_short.py             # per-short orchestrator (manifest entry -> mp4)
config.py                   # shared env-driven config (styling + geometry)
manifest/make_manifest.py   # draft-shorts doc -> manifest JSON
splice/reframe.py           # single-pane face-tracked 16:9 -> 9:16
splice/stack.py             # cut / stacked two-up / concat ffmpeg helpers
caption/transcribe.py       # cut audio -> word-level Whisper -> words/captions
caption/burn.py             # burn word-by-word captions onto a video
fonts/                      # caption fonts (bundled OFL default + your own)
examples/draft-shorts.md    # generic sample input doc
tests/                      # unit tests for the pure logic (pytest)
```

A worked example input lives at [`examples/draft-shorts.md`](examples/draft-shorts.md).

## End-to-end: draft-shorts doc → rendered shorts (`generate_shorts.py`)

One command takes a producer-side **draft-shorts markdown doc** (the per-episode
shorts plan — `### Approved N: <title>` sections with `Render segment(s):`
timestamps and a `Visual plan:` line) plus the raw source videos and produces
all the finished vertical shorts:

```
python generate_shorts.py path/to/draft-shorts.md \
    --guest path/to/guest.mp4 --ali path/to/host.mp4 --out out/ \
    [--model small.en]
```

It writes the working manifest to `<dir>/manifest.json` and one
`1080x1920` mp4 per approved short to `<dir>/`. Internally it chains the three
steps below; run them individually for finer control.

### Step 1 — `manifest/make_manifest.py` (doc → manifest)

Parses every `### Approved N: <Title>` section into a manifest entry:
`id` (slug from title), `title`, `layout`, `render_segments` (parsed from the
`MM:SS.mmm to MM:SS.mmm` ranges; supports a single `Render segment:` or a
bulleted `Render segments:` list), and `canonical_output`. `words`/`captions`
are left empty for step 2. Emits ONE manifest with all approved shorts. See
[`examples/draft-shorts.md`](examples/draft-shorts.md) for the input format.

```
python manifest/make_manifest.py path/to/draft-shorts.md --guest path/to/guest.mp4 [--ali path/to/host.mp4] [--out manifest.json]
```

Layout is derived from the `Visual plan:` line: `<guest>-only` / `… only …` →
`guest_only`; `Both faces` / `stacked` → `stacked`.

### Step 2 — `caption/transcribe.py` (manifest → words + captions)

For each short: cuts + concats its `render_segments` into one audio clip
(`guest_video` for `guest_only`; the **mixed** Ali+guest tracks via `amix` for
`stacked`, since those are dialogue), runs word-level Whisper on the clip,
then writes clip-local `words` + `captions` back into the manifest in place.

**Captions are VERBATIM** — transcribe everything actually said, including
dillydallying (filler words, hesitations, false starts, repeated words,
"well", "I think", "I'll take a step back"). Do **not** drop or smooth them.
The only transforms applied are: (1) fix obvious mistranscriptions via a small
map (`super cycle`→`supercycle`, `seeding`→`ceding`), and (2) capitalize
sentence starts. Word timings stay exactly as Whisper produced them. The
verbatim words are then grouped into `<=32`-char / `<=7`-word /
sentence-break caption lines.

```
python caption/transcribe.py <manifest.json> [--model small.en]
```

Whisper backend: the **local `whisper` CLI** (no API key needed) — resolved from
`PATH`, with `--model` selecting the model (default `small.en`). Install it with
`pip install openai-whisper`.

### Step 3 — `render_short.py` (manifest → mp4s)

`render_short.py` is the self-contained entrypoint that reproduces the finished
shorts from a manifest with zero external state — clip cutting, reframe/stack,
and captioning all happen in-repo.

```
python render_short.py path/to/manifest.json out/
```

For each approved short in the manifest it produces
`<output_dir>/<canonical_output>` at **1080x1920 with audio**. Each short in the
manifest declares a `layout`, `render_segments` (start/end in source seconds),
and clip-local word timings.

A manifest is generated from your episode's draft-shorts doc (steps 1–2 above),
then rendered:

```
python manifest/make_manifest.py path/to/draft-shorts.md \
    --guest path/to/guest.mp4 --ali path/to/host.mp4 --out manifest.json
python caption/transcribe.py manifest.json
python render_short.py manifest.json out/
```

A freshly generated manifest embeds your real source paths and the clip
transcripts, so it is gitignored (see `manifests/` in `.gitignore`) and should
never be committed.

### Layouts

- **single** (`guest_only` / `ali_only`): each `render_segment` is cut WITH
  audio (`-c:v libx264 -crf 18 -c:a aac -ar 48000`) from the relevant source
  (`guest_video` for `guest_only`, `ali_video` for `ali_only`), concatenated in
  order, face-tracked-reframed to 9:16 via `splice/reframe.py`, then captioned in
  the **default lower-third** position (`POSITION_Y_PERCENT 0.78`).
- **stacked** (two-up, Ali top / guest bottom): each `render_segment` pairs both
  sources at the same timecode. Each 16:9 source is **center-cropped to
  1215x1080 (zoomed-out, undistorted — keeps the speaker on-frame) then scaled
  to 1080x960**, and the two panes are `vstack`ed into 1080x1920 with the two
  audio tracks mixed (`amix … normalize=0`) — the cut/stack/concat ffmpeg logic
  lives in `splice/stack.py`. Segments concat in order, then the
  clip is captioned with a layout-timeline marking the whole clip `stacked` so
  **captions center on the divider** between the two faces instead of the
  lower-third.

### Caption look

`caption/burn.py` owns the styling: serif font, cream `#F5EFE0` text, deep-red
`#B11226` moving highlight box, stroke = fill (no dark outline), no shadow,
`SHORTS_WORD_GAP 0.35`. `render_short.py` only chooses the vertical position
(lower-third vs divider) via the layout timeline; it never touches the styling.

### Using your own font

Captions render with the font at `SHORTS_FONT`. The repo bundles
[EB Garamond](fonts/EBGaramond-Regular.ttf) (SIL OFL — see
[`fonts/OFL.txt`](fonts/OFL.txt)) as the cross-platform default, so it works
out-of-the-box on any OS with no system fonts assumed.

To use a different font, drop a `.ttf`/`.ttc` into [`fonts/`](fonts/) and point
`SHORTS_FONT` at it (absolute or repo-relative):

```
SHORTS_FONT=fonts/YourFont.ttf python generate_shorts.py ...
```

If the configured font file doesn't exist, the pipeline fails with a clear
message telling you to fix `SHORTS_FONT`, rather than crashing deep in the
rendering stack.

## Modules

| Concern | Module | Purpose |
| --- | --- | --- |
| manifest | `manifest/make_manifest.py` | Parse a draft-shorts doc into a render manifest (sources, layout, segments, slug). |
| splice | `splice/reframe.py` | Single-camera face-tracked 16:9 → 9:16 reframe with per-scene EMA smoothing, scene-cut detection, and chunked resumability. |
| splice | `splice/stack.py` | Cut/concat helpers + the stacked two-up (top/bottom, undistorted, audio-mixed) ffmpeg filter used by `render_short.py`. |
| caption | `caption/transcribe.py` | Cut each short's audio, run word-level local Whisper, and write verbatim `words` + grouped `captions` into the manifest. |
| caption | `caption/burn.py` | Burn word-by-word captions onto a video via a forked Captacity (vertical-position aware). |

## Requirements

- Python 3.10+
- `mediapipe`, `opencv-python`, `numpy`, `captacity`, `openai-whisper`
- `ffmpeg` and `ffprobe` on `PATH`
- the local `whisper` CLI on `PATH` (from `openai-whisper`)

```
pip install -r requirements.txt
```

The MediaPipe FaceLandmarker model is auto-downloaded on first run and cached.

### Tests

The pure parsing/cleaning/config logic has unit tests (no ffmpeg/whisper/moviepy
needed):

```
pip install -r requirements-dev.txt
python -m pytest -q
```

## Configuration (.env)

The production "specs" — caption styling and render geometry — are driven by
environment variables, resolved in [`config.py`](config.py). **Every default
equals the current winning value**, so with no `.env` file and no env vars set
the output is identical to the hardcoded look.

To override, copy [`.env.example`](.env.example) to a repo-root `.env` and edit
only the lines you want to change:

```
cp .env.example .env
# edit .env, e.g. SHORTS_HIGHLIGHT_COLOR=#1E90FF
```

`config.py` ships a tiny dependency-free `KEY=VALUE` parser (no python-dotenv).
A real shell `export` always wins over a value in `.env`. The real `.env` is
**gitignored and must never be committed** — only `.env.example` is tracked.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SHORTS_FONT` | `fonts/EBGaramond-Regular.ttf` | Caption font (.ttf/.ttc; absolute or repo-relative) |
| `SHORTS_FONT_SIZE` | `64` | Caption glyph size (px) |
| `SHORTS_TEXT_COLOR` | `#F5EFE0` | Cream fill color |
| `SHORTS_STROKE_COLOR` | = text color | Stroke color (no dark outline) |
| `SHORTS_STROKE_WIDTH` | `3` | Glyph stroke width (px) |
| `SHORTS_HIGHLIGHT_COLOR` | `#B11226` | Active-word highlight box color |
| `SHORTS_WORD_GAP` | `0.35` | Extra inter-word gap (fraction of a space) |
| `SHORTS_POSITION_Y` | `0.78` | Caption center as fraction of height (lower-third) |
| `SHORTS_STACK_POSITION_Y` | `0.5` | Caption center for stacked layout |
| `SHORTS_SHADOW_STRENGTH` | `0` | Drop-shadow strength (0 = none) |
| `SHORTS_SHADOW_BLUR` | `0` | Drop-shadow blur (0 = none) |
| `SHORTS_MAX_WORDS_PER_LINE` | `7` | `group_words` max words per caption line |
| `SHORTS_MAX_CHARS` | `32` | `group_words` max chars per caption line |
| `SHORTS_PADDING` | `80` | Horizontal text bbox padding (px) |
| `SHORTS_LINE_COUNT` | `1` | Captacity line_count for fit calc |
| `SHORTS_MAX_WORDS_PER_CAPTION` | `5` | Max words visible at once (caption/burn.py) |
| `SHORTS_WIDTH` | `1080` | Final frame width |
| `SHORTS_HEIGHT` | `1920` | Final frame height |
| `SHORTS_FPS` | `24` | Output frame rate |
| `SHORTS_CRF` | `18` | x264 quality factor for cut/stack re-encodes |
| `SHORTS_STACK_CROP_W` | `1215` | Stacked per-source center-crop width |
| `SHORTS_STACK_CROP_H` | `1080` | Stacked per-source center-crop height |
| `SHORTS_PANE_W` | `1080` | Stacked scaled pane width |
| `SHORTS_PANE_H` | `960` | Stacked scaled pane height |

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
