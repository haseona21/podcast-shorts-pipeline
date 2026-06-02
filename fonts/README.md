# Fonts

Captions are rendered with the font pointed to by `SHORTS_FONT` (see
[`config.py`](../config.py) and the README's "Using your own font" section).

## Bundled default

This repo ships **EB Garamond** (`EBGaramond-Regular.ttf`), a serif under the
[SIL Open Font License 1.1](OFL.txt). It's the default so captions work
out-of-the-box on any OS, with no system fonts assumed.

## Bring your own

Drop a `.ttf`/`.ttc` here and point `SHORTS_FONT` at it:

```
SHORTS_FONT=fonts/YourFont.ttf
```

`SHORTS_FONT` also accepts an absolute path. Relative paths resolve against the
repo root. If the file doesn't exist you get a clear error telling you to set
`SHORTS_FONT`.
