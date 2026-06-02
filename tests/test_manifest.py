"""Unit tests for manifest/make_manifest.py (pure parsing logic)."""
from manifest.make_manifest import (
    derive_layout,
    parse_doc,
    parse_timestamp,
    slugify,
)


def test_parse_timestamp_mm_ss():
    assert parse_timestamp("01:30.500") == 90.5
    assert parse_timestamp("00:30.000") == 30.0


def test_parse_timestamp_hh_mm_ss():
    assert parse_timestamp("1:02:03.250") == 3723.25


def test_parse_timestamp_bad():
    import pytest
    with pytest.raises(ValueError):
        parse_timestamp("90")


def test_slugify():
    assert slugify("The One Big Idea") == "the_one_big_idea"
    assert slugify("Back-and-Forth!! Exchange") == "back_and_forth_exchange"
    assert slugify("  Leading/Trailing  ") == "leading_trailing"


def test_derive_layout():
    assert derive_layout("guest-only throughout") == "guest_only"
    assert derive_layout("... only ...") == "guest_only"
    assert derive_layout("Both faces stacked") == "stacked"
    assert derive_layout("split screen") == "stacked"
    assert derive_layout("") == "guest_only"  # default


APPROVED_SINGLE = """
### Approved 1: The One Big Idea

Render segment: `00:30.000 to 00:50.000`
Visual plan: `guest-only throughout`
"""


def test_approved_single_segment():
    secs = parse_doc(APPROVED_SINGLE)
    assert len(secs) == 1
    s = secs[0]
    assert s["number"] == 1
    assert s["title"] == "The One Big Idea"
    assert s["id"] == "approved01_the_one_big_idea"
    assert s["layout"] == "guest_only"
    assert s["render_segments"] == [{"start": 30.0, "end": 50.0}]


APPROVED_LIST = """
### Approved 2: Multi Beat

Render segments:
- `00:10.000 to 00:20.000` -- first beat
- `01:00.000 to 01:05.500` -- second beat
Visual plan: `Both faces stacked`
"""


def test_approved_segments_list_and_stacked():
    secs = parse_doc(APPROVED_LIST)
    assert len(secs) == 1
    s = secs[0]
    assert s["layout"] == "stacked"
    assert s["render_segments"] == [
        {"start": 10.0, "end": 20.0},
        {"start": 60.0, "end": 65.5},
    ]


CANDIDATE_SKIPPED = """
### Approved 1: Keep Me

Render segment: `00:00.000 to 00:05.000`
Visual plan: `guest-only`

### Candidate 2: Drop Me

Render segment: `00:10.000 to 00:15.000`
Visual plan: `guest-only`
"""


def test_candidate_section_skipped():
    secs = parse_doc(CANDIDATE_SKIPPED)
    assert [s["number"] for s in secs] == [1]
    assert secs[0]["title"] == "Keep Me"


def test_approved_without_segments_is_dropped():
    doc = """
### Approved 1: No Segments Here

Just some prose, no render segment line.
Visual plan: `guest-only`
"""
    assert parse_doc(doc) == []
