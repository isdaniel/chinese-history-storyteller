import pytest

import visual_style as vs

LEGACY_INK = (", traditional Chinese ink painting style, scroll painting aesthetic, "
              "muted earth tones, atmospheric, no text, no characters writing, "
              "cinematic composition, 16:9 aspect")


def test_ink_suffix_is_identical_to_the_legacy_one():
    assert vs.style_suffix("ink") == LEGACY_INK


def test_cinematic_suffix_forbids_text():
    assert "cinematic" in vs.style_suffix("cinematic") and "no text" in vs.style_suffix("cinematic")


def test_current_style_reads_env_and_defaults_to_ink(monkeypatch):
    monkeypatch.delenv("VISUAL_STYLE", raising=False)
    assert vs.current_style() == "ink"
    monkeypatch.setenv("VISUAL_STYLE", "")
    assert vs.current_style() == "ink"
    monkeypatch.setenv("VISUAL_STYLE", "cinematic")
    assert vs.current_style() == "cinematic"


def test_unknown_style_is_rejected(monkeypatch):
    monkeypatch.setenv("VISUAL_STYLE", "neon")
    with pytest.raises(ValueError):
        vs.current_style()
    with pytest.raises(ValueError):
        vs.style_suffix("neon")
