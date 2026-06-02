# Draft Shorts — Example Episode

A minimal, generic draft-shorts plan. Each `### Approved N: <Title>` section is
parsed into one manifest entry. A section needs a `Render segment:` (single
range) or a `Render segments:` bulleted list, plus a `Visual plan:` line that
decides the layout (`... only ...` -> guest_only; `Both faces` / `stacked` ->
stacked). Sections that aren't `### Approved N:` (e.g. `### Candidate`) are
skipped.

Timestamps are `MM:SS.mmm to MM:SS.mmm` (or `H:MM:SS.mmm`) in source seconds.

---

### Approved 1: The One Big Idea

A single guest-only beat — the guest makes one clean point, no host on screen.

Render segment: `00:30.000 to 00:50.000`
Visual plan: `guest-only throughout`

### Approved 2: Back-and-Forth Exchange

A short dialogue moment rendered with both speakers on screen, stacked.

Render segment: `01:10.000 to 01:20.000`
Visual plan: `Both faces stacked`

### Candidate 3: A Maybe (skipped)

This section is a candidate, not approved, so make_manifest.py ignores it.

Render segment: `05:00.000 to 05:30.000`
Visual plan: `guest-only`
