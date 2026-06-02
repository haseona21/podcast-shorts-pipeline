#!/usr/bin/env python3
"""Cut + stack ffmpeg helpers for the renderer.

This module owns the low-level clip surgery used by render_short.py:

  - cut_single:  cut [start, end) from one source WITH audio (sample-accurate).
  - cut_stacked: build one two-up (Ali top / guest bottom) segment — each 16:9
                 source center-cropped to STACK_CROP_W x STACK_CROP_H (a
                 zoomed-out, undistorted window that keeps the speaker
                 on-frame), scaled to PANE_W x PANE_H, then vstacked into a
                 WIDTH x HEIGHT frame at FPS with the two audio tracks mixed.
  - concat:      concatenate same-codec mp4 parts in order via the concat
                 demuxer.

All geometry/quality knobs are env-driven via config.py; the defaults reproduce
the winning look (crop 1215:1080 -> scale 1080:960, fps 24, crf 18).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

# Make the repo root importable so `config` resolves no matter the CWD.
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import CFG  # noqa: E402

_R = CFG.render

# x264 quality factor for cut/stack re-encodes (env-driven; default 18).
CRF = str(_R.crf)

# Stacked two-up geometry (all env-driven; defaults reproduce the winning look).
# Each 16:9 source is center-cropped to STACK_CROP_W x STACK_CROP_H (a zoomed-out,
# undistorted window that keeps the speaker on-frame) then scaled to PANE_W x
# PANE_H so two panes stack into a WIDTH x HEIGHT frame at FPS.
STACKED_FILTER = (
    "[0:v]crop={cw}:{ch}:(iw-{cw})/2:0,scale={pw}:{ph},setsar=1,fps={fps}[t];"
    "[1:v]crop={cw}:{ch}:(iw-{cw})/2:0,scale={pw}:{ph},setsar=1,fps={fps}[b];"
    "[t][b]vstack=inputs=2,format=yuv420p[v];"
    "[0:a][1:a]amix=inputs=2:duration=longest:normalize=0[a]"
).format(
    cw=_R.stack_crop_w, ch=_R.stack_crop_h,
    pw=_R.pane_w, ph=_R.pane_h, fps=_R.fps,
)


def _run_quiet(cmd: List[str]) -> None:
    subprocess.run(
        cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def concat(parts: List[Path], out: Path, workdir: Path) -> None:
    """Concatenate same-codec mp4 parts in order via the ffmpeg concat demuxer."""
    if len(parts) == 1:
        _run_quiet(["cp", str(parts[0]), str(out)])
        return
    list_file = workdir / "concat.txt"
    list_file.write_text(
        "\n".join(f"file '{p.resolve()}'" for p in parts) + "\n"
    )
    _run_quiet([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_file), "-c", "copy", str(out),
    ])


def cut_single(src: Path, start: float, end: float, out: Path) -> None:
    """Cut [start, end) from a single source WITH audio (sample-accurate)."""
    _run_quiet([
        "ffmpeg", "-y", "-ss", f"{start}", "-to", f"{end}", "-i", str(src),
        "-c:v", "libx264", "-crf", CRF, "-c:a", "aac", "-ar", "48000",
        str(out),
    ])


def cut_stacked(ali: Path, guest: Path, start: float, end: float, out: Path) -> None:
    """Build one stacked (Ali top / guest bottom) segment for [start, end)."""
    _run_quiet([
        "ffmpeg", "-y",
        "-ss", f"{start}", "-to", f"{end}", "-i", str(ali),
        "-ss", f"{start}", "-to", f"{end}", "-i", str(guest),
        "-filter_complex", STACKED_FILTER,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-crf", CRF, "-c:a", "aac", "-ar", "48000",
        str(out),
    ])
