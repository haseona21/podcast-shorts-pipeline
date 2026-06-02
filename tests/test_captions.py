"""Unit tests for caption/transcribe.py cleaning + grouping (pure logic)."""
from caption.transcribe import clean_words, group_words


def _w(text, start, end):
    return {"word": text, "start": start, "end": end}


def words(*pairs):
    """Build a word list from (text, start, end) tuples."""
    return [_w(*p) for p in pairs]


def test_verbatim_keeps_fillers_and_repeats():
    raw = words(
        ("well", 0.0, 0.3),
        ("like", 0.3, 0.6),
        ("I", 0.6, 0.7),
        ("I", 0.7, 0.8),
        ("think", 0.8, 1.1),
    )
    out = clean_words(raw)
    texts = [w["word"] for w in out]
    # No filler dropped, no stutter collapse.
    assert texts == ["Well", "like", "I", "I", "think"]


def test_mistranscription_bigram_supercycle():
    raw = words(("super", 0.0, 0.4), ("cycle", 0.4, 0.9))
    out = clean_words(raw)
    assert [w["word"] for w in out] == ["Supercycle"]
    # merged word spans both timings
    assert out[0]["start"] == 0.0 and out[0]["end"] == 0.9


def test_mistranscription_word_seeding_to_ceding():
    raw = words(("seeding", 0.0, 0.5), ("power", 0.5, 0.9))
    out = clean_words(raw)
    assert [w["word"] for w in out] == ["Ceding", "power"]


def test_mistranscription_preserves_trailing_punctuation():
    raw = words(("hello", 0.0, 0.3), ("seeding.", 0.3, 0.8))
    out = clean_words(raw)
    assert out[1]["word"] == "ceding."


def test_sentence_start_capitalization():
    raw = words(
        ("first", 0.0, 0.3),
        ("sentence.", 0.3, 0.7),
        ("second", 0.7, 1.0),
        ("one", 1.0, 1.3),
    )
    out = clean_words(raw)
    texts = [w["word"] for w in out]
    assert texts[0] == "First"          # first word capitalized
    assert texts[2] == "Second"         # after sentence end capitalized
    assert texts[3] == "one"            # mid-sentence stays lower


def test_clean_words_empty():
    assert clean_words([]) == []


def test_group_words_char_limit():
    # 9 short words, each ~5 chars; the 32-char limit forces a break before 7.
    raw = words(*[(f"word{i}", float(i), float(i) + 0.5) for i in range(9)])
    cleaned = clean_words(raw)
    lines = group_words(cleaned)
    assert all(len(ln["text"]) <= 32 for ln in lines)
    assert len(lines) >= 2


def test_group_words_word_limit():
    # 8 one-char words: 32-char cap not hit, so the 7-word cap drives the break.
    raw = words(*[("a", float(i), float(i) + 0.2) for i in range(8)])
    cleaned = clean_words(raw)
    lines = group_words(cleaned)
    # first line: capitalized "A" + 6 lowercase a = 7 words, then 1 leftover
    assert [len(ln["text"].split()) for ln in lines] == [7, 1]


def test_group_words_sentence_break():
    raw = words(
        ("Hello", 0.0, 0.3),
        ("there.", 0.3, 0.6),
        ("Next", 0.6, 0.9),
        ("line", 0.9, 1.2),
    )
    cleaned = clean_words(raw)
    lines = group_words(cleaned)
    assert len(lines) == 2
    assert lines[0]["text"] == "Hello there."
    assert lines[1]["text"] == "Next line"


def test_group_words_timings():
    raw = words(("Hi", 0.0, 0.4), ("there.", 0.4, 0.9))
    lines = group_words(clean_words(raw))
    assert lines[0]["start"] == 0.0
    assert lines[0]["end"] == 0.9
