"""Unit tests for config.py (defaults, env override, .env precedence, parsing)."""
import os

import pytest

import config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Strip SHORTS_* from the environment so each test starts clean."""
    for key in [k for k in os.environ if k.startswith("SHORTS_")]:
        monkeypatch.delenv(key, raising=False)
    yield


def test_defaults_exist():
    cfg = config.load_config()
    assert cfg.caption.font == config.DEFAULT_FONT
    assert cfg.caption.font_size == 64
    assert cfg.caption.text_color == "#F5EFE0"
    assert cfg.caption.max_chars == 32
    assert cfg.caption.max_words_per_line == 7
    assert cfg.render.width == 1080
    assert cfg.render.height == 1920


def test_stroke_defaults_to_text_color():
    monkeypatch_color = "#123456"
    os.environ["SHORTS_TEXT_COLOR"] = monkeypatch_color
    try:
        cfg = config.load_config()
        assert cfg.caption.text_color == monkeypatch_color
        assert cfg.caption.stroke_color == monkeypatch_color
    finally:
        del os.environ["SHORTS_TEXT_COLOR"]


def test_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("SHORTS_FONT_SIZE", "99")
    monkeypatch.setenv("SHORTS_HIGHLIGHT_COLOR", "#1E90FF")
    cfg = config.load_config()
    assert cfg.caption.font_size == 99
    assert cfg.caption.highlight_color == "#1E90FF"


def test_dotenv_sets_unset_value(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("SHORTS_FONT_SIZE=72\n")
    config.load_dotenv(env)
    assert os.environ["SHORTS_FONT_SIZE"] == "72"


def test_dotenv_does_not_override_existing_env(monkeypatch, tmp_path):
    # A real export must win over .env.
    monkeypatch.setenv("SHORTS_FONT_SIZE", "10")
    env = tmp_path / ".env"
    env.write_text("SHORTS_FONT_SIZE=99\n")
    config.load_dotenv(env)
    assert os.environ["SHORTS_FONT_SIZE"] == "10"


def test_dotenv_color_hash_not_stripped(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SHORTS_HIGHLIGHT_COLOR=#B11226  # crimson box\n")
    config.load_dotenv(env)
    assert os.environ["SHORTS_HIGHLIGHT_COLOR"] == "#B11226"


def test_dotenv_strips_quotes_and_export(tmp_path):
    env = tmp_path / ".env"
    env.write_text('export SHORTS_FONT="fonts/My Font.ttf"\n')
    config.load_dotenv(env)
    assert os.environ["SHORTS_FONT"] == "fonts/My Font.ttf"


def test_number_parsing(monkeypatch):
    monkeypatch.setenv("SHORTS_WORD_GAP", "0.5")
    monkeypatch.setenv("SHORTS_PADDING", "100")
    cfg = config.load_config()
    assert cfg.caption.word_gap == 0.5
    assert isinstance(cfg.caption.word_gap, float)
    assert cfg.caption.padding == 100
    assert isinstance(cfg.caption.padding, int)


def test_empty_env_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SHORTS_FONT_SIZE", "")
    cfg = config.load_config()
    assert cfg.caption.font_size == 64


# ---- resolve_font ---------------------------------------------------------

def test_resolve_font_default_bundled():
    # The bundled default ships in the repo and must resolve.
    path = config.resolve_font()
    assert path.endswith(".ttf")
    assert os.path.exists(path)


def test_resolve_font_relative_to_repo_root():
    path = config.resolve_font(config.DEFAULT_FONT)
    assert os.path.exists(path)


def test_resolve_font_missing_raises_friendly_error():
    with pytest.raises(FileNotFoundError) as exc:
        config.resolve_font("fonts/definitely_missing_xyz.ttf")
    msg = str(exc.value)
    assert "SHORTS_FONT" in msg
    assert ".ttf" in msg
