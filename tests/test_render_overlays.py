import logging
import math

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

import render_overlays as ro


@pytest.fixture
def cjk_font():
    try:
        ro.find_font(True)
        ro.find_font(False)
    except FileNotFoundError:
        pytest.skip("no CJK font installed")


def test_find_font_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBTITLE_FONT_FILE", str(tmp_path / "x.ttc"))
    monkeypatch.setenv("SUBTITLE_FONT_INDEX", "3")
    assert ro.find_font(True) == (str(tmp_path / "x.ttc"), 3)


def test_find_font_reports_a_clear_error_when_nothing_is_installed(monkeypatch):
    monkeypatch.delenv("SUBTITLE_FONT_FILE", raising=False)
    monkeypatch.setattr(ro, "BOLD_CANDIDATES", [("/no/such/font.ttc", 0)])
    with pytest.raises(FileNotFoundError, match="SUBTITLE_FONT_FILE"):
        ro.find_font(True)


def test_card_has_an_accent_bar_and_visible_text(cjk_font):
    card = ro.render_card("1974年", "年代")
    assert card.mode == "RGBA" and card.width > 150 and card.height == 150
    assert card.getpixel((3, 75)) == ro.ACCENT + (255,)
    white = np.asarray(card)[..., :3].min(axis=2) > 240
    assert white.sum() > 200


def test_subtitle_is_drawn_and_long_text_shrinks_to_fit(cjk_font):
    short = ro.render_subtitle("測試字幕")
    long = ro.render_subtitle("這是一段非常長的字幕" * 5)
    assert short.height == 80 and np.asarray(short)[..., 3].max() == 255
    assert long.width <= ro.SUBTITLE_MAX_WIDTH + 40
    assert long.height < short.height


def test_shade_factor_is_darker_at_the_bottom_and_edges():
    f = ro.shade_factor(1920, 1080)
    assert f.shape == (1080, 1920, 1) and f.dtype == np.float32
    assert f[-1, 960, 0] < f[10, 960, 0] == pytest.approx(1.0)
    assert f[540, 0, 0] < f[540, 960, 0]
    assert 0.3 <= f.min() and f.max() <= 1.0


def test_paste_rgba_respects_the_fade():
    overlay = Image.new("RGBA", (10, 10), (255, 255, 255, 255))
    frame = Image.new("RGB", (20, 20), (0, 0, 0))
    ro.paste_rgba(frame, overlay, (0, 0), fade=0.0)
    assert frame.getpixel((5, 5)) == (0, 0, 0)
    ro.paste_rgba(frame, overlay, (0, 0), fade=1.0)
    assert frame.getpixel((5, 5)) == (255, 255, 255)
    half = Image.new("RGB", (20, 20), (0, 0, 0))
    ro.paste_rgba(half, overlay, (0, 0), fade=0.5)
    assert 100 < half.getpixel((5, 5))[0] < 155


# ---------------------------------------------------------------------------
# Extra coverage: constants, wiring, boundaries. Font-independent where possible.
# ---------------------------------------------------------------------------

FILL = (14, 12, 10, 175)
LABEL_COLOUR = (214, 190, 150, 255)
WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)


@pytest.fixture(autouse=True)
def fresh_font_cache():
    # load_font is cached on (size, bold) while find_font reads the environment on every call.
    ro.load_font.cache_clear()
    yield
    ro.load_font.cache_clear()


class FixedWidthFont:
    """Every character is `size` px wide; glyph drawing is delegated to Pillow's bundled font."""

    def __init__(self, size, bold):
        self.size, self.bold = size, bold
        self._glyphs = ImageFont.load_default(size)

    def getlength(self, text, *args, **kwargs):
        return len(text) * self.size

    def __getattr__(self, name):
        return getattr(self._glyphs, name)


@pytest.fixture
def fixed_fonts(monkeypatch):
    requested = []

    def fake_load_font(size, bold):
        requested.append((size, bold))
        return FixedWidthFont(size, bold)

    monkeypatch.setattr(ro, "load_font", fake_load_font)
    return requested


@pytest.fixture
def text_calls(monkeypatch):
    calls = []
    original = ImageDraw.ImageDraw.text

    def spy(self, xy, text, fill=None, font=None, anchor=None, *args, **kwargs):
        calls.append({"xy": xy, "text": text, "fill": fill, "font": font, "anchor": anchor, **kwargs})
        return original(self, xy, text, fill, font, anchor, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", spy)
    return calls


def bbox(mask):
    rows, cols = np.where(mask.any(axis=1))[0], np.where(mask.any(axis=0))[0]
    return cols[0], rows[0], cols[-1], rows[-1]


# --- find_font / load_font -------------------------------------------------


def test_find_font_env_index_defaults_to_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBTITLE_FONT_FILE", str(tmp_path / "x.ttc"))
    monkeypatch.delenv("SUBTITLE_FONT_INDEX", raising=False)
    assert ro.find_font(False) == (str(tmp_path / "x.ttc"), 0)


def test_find_font_blank_env_index_means_zero(monkeypatch, tmp_path):
    # An unset GitHub Actions variable arrives as an empty string.
    monkeypatch.setenv("SUBTITLE_FONT_FILE", str(tmp_path / "x.ttc"))
    monkeypatch.setenv("SUBTITLE_FONT_INDEX", "")
    assert ro.find_font(False) == (str(tmp_path / "x.ttc"), 0)


def test_find_font_blank_env_file_falls_back_to_the_candidates(monkeypatch, tmp_path):
    installed = tmp_path / "bold.ttc"
    installed.touch()
    monkeypatch.setenv("SUBTITLE_FONT_FILE", "")
    monkeypatch.setattr(ro, "BOLD_CANDIDATES", [(str(installed), 4)])
    assert ro.find_font(True) == (str(installed), 4)


@pytest.mark.parametrize("blank", [" ", "\t", " \n "])
def test_find_font_whitespace_only_env_file_counts_as_unset(monkeypatch, tmp_path, blank):
    installed = tmp_path / "bold.ttc"
    installed.touch()
    monkeypatch.setenv("SUBTITLE_FONT_FILE", blank)
    monkeypatch.setattr(ro, "BOLD_CANDIDATES", [(str(installed), 4)])
    assert ro.find_font(True) == (str(installed), 4)


def test_find_font_strips_whitespace_around_the_configured_file_and_index(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBTITLE_FONT_FILE", f"  {tmp_path / 'x.ttc'}\t")
    monkeypatch.setenv("SUBTITLE_FONT_INDEX", " 3 ")
    assert ro.find_font(True) == (str(tmp_path / "x.ttc"), 3)


@pytest.mark.parametrize("blank", [" ", "\t", " \n "])
def test_find_font_whitespace_only_env_index_means_zero(monkeypatch, tmp_path, blank):
    monkeypatch.setenv("SUBTITLE_FONT_FILE", str(tmp_path / "x.ttc"))
    monkeypatch.setenv("SUBTITLE_FONT_INDEX", blank)
    assert ro.find_font(False) == (str(tmp_path / "x.ttc"), 0)


@pytest.mark.parametrize("bad", ["abc", "1.5", "-", "3 4"])
def test_find_font_non_numeric_env_index_names_the_setting(monkeypatch, tmp_path, bad):
    monkeypatch.setenv("SUBTITLE_FONT_FILE", str(tmp_path / "x.ttc"))
    monkeypatch.setenv("SUBTITLE_FONT_INDEX", bad)
    with pytest.raises(ValueError, match="SUBTITLE_FONT_INDEX") as excinfo:
        ro.find_font(False)
    assert repr(bad) in str(excinfo.value)


def test_find_font_env_file_wins_over_installed_candidates(monkeypatch, tmp_path):
    installed = tmp_path / "bold.ttc"
    installed.touch()
    monkeypatch.setenv("SUBTITLE_FONT_FILE", "custom.ttc")
    monkeypatch.setenv("SUBTITLE_FONT_INDEX", "2")
    monkeypatch.setattr(ro, "BOLD_CANDIDATES", [(str(installed), 4)])
    assert ro.find_font(True) == ("custom.ttc", 2)


def test_find_font_returns_the_first_installed_candidate_of_the_requested_weight(monkeypatch, tmp_path):
    first, second, regular = tmp_path / "a.ttc", tmp_path / "b.ttc", tmp_path / "r.ttc"
    for path in (first, second, regular):
        path.touch()
    monkeypatch.delenv("SUBTITLE_FONT_FILE", raising=False)
    monkeypatch.setattr(ro, "BOLD_CANDIDATES", [(str(tmp_path / "missing.ttc"), 9), (str(first), 5), (str(second), 6)])
    monkeypatch.setattr(ro, "REGULAR_CANDIDATES", [(str(regular), 7)])
    assert ro.find_font(True) == (str(first), 5)
    assert ro.find_font(False) == (str(regular), 7)


def test_default_candidates_point_at_the_traditional_chinese_faces():
    assert ro.BOLD_CANDIDATES == [("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 3),
                                  (r"C:\Windows\Fonts\msjhbd.ttc", 0)]
    assert ro.REGULAR_CANDIDATES == [("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 3),
                                     (r"C:\Windows\Fonts\msjh.ttc", 0)]


def test_load_font_opens_the_found_font_with_size_and_index(monkeypatch):
    opened, sentinel = [], object()

    def fake_truetype(path, size, index=0):
        opened.append((path, size, index))
        return sentinel

    monkeypatch.setattr(ro.ImageFont, "truetype", fake_truetype)
    monkeypatch.setenv("SUBTITLE_FONT_FILE", "x.ttc")
    monkeypatch.setenv("SUBTITLE_FONT_INDEX", "3")
    assert ro.load_font(40, True) is sentinel
    assert opened == [("x.ttc", 40, 3)]


def test_load_font_is_cached_per_size_and_weight(monkeypatch, tmp_path):
    bold, regular = tmp_path / "b.ttc", tmp_path / "r.ttc"
    bold.touch()
    regular.touch()
    monkeypatch.delenv("SUBTITLE_FONT_FILE", raising=False)
    monkeypatch.setattr(ro, "BOLD_CANDIDATES", [(str(bold), 1)])
    monkeypatch.setattr(ro, "REGULAR_CANDIDATES", [(str(regular), 2)])
    opened = []
    monkeypatch.setattr(ro.ImageFont, "truetype",
                        lambda path, size, index=0: opened.append((path, size, index)) or object())
    first = ro.load_font(40, True)
    assert ro.load_font(40, True) is first
    ro.load_font(40, False)
    ro.load_font(41, True)
    assert opened == [(str(bold), 40, 1), (str(regular), 40, 2), (str(bold), 41, 1)]


def test_load_font_returns_a_real_font_of_the_requested_size(cjk_font):
    assert ro.load_font(40, True).size == 40
    assert ro.load_font(30, False).size == 30


def assert_names_the_font_settings(excinfo, path, index):
    message = str(excinfo.value)
    assert repr(path) in message and f"(index {index})" in message
    assert "SUBTITLE_FONT_FILE" in message and "SUBTITLE_FONT_INDEX" in message
    assert isinstance(excinfo.value.__cause__, OSError)
    assert str(excinfo.value.__cause__) in message


def test_load_font_names_the_settings_when_the_configured_file_is_missing(monkeypatch, tmp_path):
    missing = str(tmp_path / "missing.ttc")
    monkeypatch.setenv("SUBTITLE_FONT_FILE", missing)
    monkeypatch.setenv("SUBTITLE_FONT_INDEX", "2")
    with pytest.raises(OSError) as excinfo:
        ro.load_font(40, True)
    assert_names_the_font_settings(excinfo, missing, 2)


def test_load_font_names_the_settings_when_the_index_is_out_of_range_for_a_real_font(cjk_font, monkeypatch):
    path, _ = ro.find_font(True)
    monkeypatch.setenv("SUBTITLE_FONT_FILE", path)
    monkeypatch.setenv("SUBTITLE_FONT_INDEX", "99")
    with pytest.raises(OSError) as excinfo:
        ro.load_font(40, True)
    assert_names_the_font_settings(excinfo, path, 99)


def test_rendering_without_any_font_raises_the_clear_error(monkeypatch):
    monkeypatch.delenv("SUBTITLE_FONT_FILE", raising=False)
    monkeypatch.setattr(ro, "BOLD_CANDIDATES", [])
    monkeypatch.setattr(ro, "REGULAR_CANDIDATES", [])
    with pytest.raises(FileNotFoundError, match="SUBTITLE_FONT_FILE"):
        ro.render_card("1974年", "年代")
    with pytest.raises(FileNotFoundError, match="SUBTITLE_FONT_FILE"):
        ro.render_subtitle("測試字幕")


# --- constants --------------------------------------------------------------


def test_public_constants():
    assert ro.ACCENT == (178, 54, 40)
    assert ro.SUBTITLE_MAX_WIDTH == 1700


# --- render_card ------------------------------------------------------------


def test_card_width_follows_the_wider_of_title_and_label(fixed_fonts):
    # title chars are 64 px wide, label chars 30 px wide; width = widest + 2 * 30 pad + 10 bar + 8
    assert ro.render_card("12345", "ab").size == (5 * 64 + 78, 150)
    assert ro.render_card("1", "x" * 20).size == (20 * 30 + 78, 150)
    assert set(fixed_fonts) == {(64, True), (30, False)}


def test_card_draws_the_label_and_the_title_at_fixed_positions(fixed_fonts, text_calls):
    ro.render_card("12345", "ab")
    by_text = {call["text"]: call for call in text_calls}
    assert set(by_text) == {"ab", "12345"}
    label, title = by_text["ab"], by_text["12345"]
    assert (label["xy"], label["fill"]) == ((40, 22), LABEL_COLOUR)
    assert (label["font"].size, label["font"].bold) == (30, False)
    assert (title["xy"], title["fill"]) == ((40, 62), WHITE)
    assert (title["font"].size, title["font"].bold) == (64, True)


def test_card_has_a_translucent_dark_body_an_accent_bar_and_rounded_right_corners(fixed_fonts):
    card = ro.render_card("12345", "ab")
    width, height = card.size
    accent = ro.ACCENT + (255,)
    for xy in [(0, 0), (10, 0), (0, height - 1), (10, height - 1), (5, 75), (10, 75)]:
        assert card.getpixel(xy) == accent
    assert card.getpixel((11, 75)) == FILL
    assert card.getpixel((width - 5, 75)) == FILL
    alpha = np.asarray(card)[..., 3]
    top_right, bottom_right = alpha[:14, -14:], alpha[-14:, -14:]
    assert 22 <= (top_right == 0).sum() <= 25
    assert (top_right == 0).sum() == (bottom_right[::-1] == 0).sum()
    assert alpha[0, -1] == 0 and alpha[-1, -1] == 0


def test_card_text_is_coloured_and_stays_inside_its_rows(cjk_font):
    card = np.asarray(ro.render_card("1974年", "年代"))
    # A thin 30 px Regular face leaves few pixels at exactly LABEL_COLOUR, so accept near-solid ones.
    near_label = np.abs(card[..., :3].astype(int) - LABEL_COLOUR[:3]).sum(axis=2) < 40
    label = near_label & (card[..., 3] == 255)
    title = (card == WHITE).all(axis=2)
    label_cols, label_rows = np.where(label.any(axis=0))[0], np.where(label.any(axis=1))[0]
    title_cols, title_rows = np.where(title.any(axis=0))[0], np.where(title.any(axis=1))[0]
    assert label.sum() > 20 and title.sum() > 200
    assert label_rows.min() >= 22 and label_rows.max() < 62
    assert title_rows.min() >= 62 and title_rows.max() < 150
    assert label_cols.min() >= 40 and title_cols.min() >= 40
    assert (card[12:138, 11:40] == FILL).all()


def test_card_flattens_line_breaks_and_whitespace(fixed_fonts, text_calls):
    card = ro.render_card("秦\n始 皇\t陵\u3000", "人\r\n名")
    assert card.size == (4 * 64 + 78, 150)
    assert {call["text"] for call in text_calls} == {"秦始皇陵", "人名"}


def test_widths_truncate_fractional_text_lengths(monkeypatch):
    class FractionalFont(FixedWidthFont):
        def getlength(self, text, *args, **kwargs):
            return super().getlength(text) + 0.9

    monkeypatch.setattr(ro, "load_font", FractionalFont)
    assert ro.render_card("12345", "ab").width == 5 * 64 + 78
    assert ro.render_subtitle("x" * 10).width == 10 * 50 + 24


def test_card_with_blank_text_still_renders(fixed_fonts):
    assert ro.render_card("", "").size == (78, 150)
    assert ro.render_card("  \n", "年代").size == (2 * 30 + 78, 150)


def test_card_with_line_breaks_renders_with_the_real_font(cjk_font):
    card = ro.render_card("秦始\n皇陵", "人\n名")
    assert card.height == 150 and (np.asarray(card) == WHITE).all(axis=2).sum() > 200


# --- render_subtitle --------------------------------------------------------


def test_subtitle_shrinks_two_px_at_a_time_until_it_fits(fixed_fonts):
    image = ro.render_subtitle("x" * 40)
    assert fixed_fonts == [(50, True), (48, True), (46, True), (44, True), (42, True)]
    assert image.size == (40 * 42 + 24, 42 + 30)


def test_subtitle_is_not_shrunk_when_it_exactly_fits(fixed_fonts):
    assert ro.render_subtitle("x" * 34).size == (34 * 50 + 24, 80)
    assert fixed_fonts == [(50, True)]
    assert ro.render_subtitle("x" * 35).size == (35 * 48 + 24, 78)


def test_subtitle_is_centred_on_the_floor_of_half_an_odd_width(monkeypatch, text_calls):
    class OddWidthFont(FixedWidthFont):
        def getlength(self, text, *args, **kwargs):
            return super().getlength(text) + 1

    monkeypatch.setattr(ro, "load_font", OddWidthFont)
    image = ro.render_subtitle("x")
    assert image.size == (75, 80)
    (call,) = text_calls
    assert call["xy"] == (37, 40)


def test_subtitle_never_shrinks_below_thirty_px(fixed_fonts):
    image = ro.render_subtitle("x" * 100)
    assert fixed_fonts == [(size, True) for size in range(50, 29, -2)]
    assert image.size == (100 * 30 + 24, 60)


def test_subtitle_is_centred_white_text_with_a_black_outline(fixed_fonts, text_calls):
    image = ro.render_subtitle("x" * 40)
    (call,) = text_calls
    assert call["xy"] == (image.width // 2, image.height // 2) == (852, 36)
    assert call["text"] == "x" * 40
    assert (call["fill"], call["anchor"], call["stroke_width"], call["stroke_fill"]) == (WHITE, "mm", 4, BLACK)
    assert (call["font"].size, call["font"].bold) == (42, True)


@pytest.mark.parametrize("text", ["田", "測試字幕"])
def test_subtitle_outline_is_four_px_thick_around_centred_glyphs(cjk_font, text):
    pixels = np.asarray(ro.render_subtitle(text))
    height, width = pixels.shape[:2]
    white = (pixels == WHITE).all(axis=2)
    solid = pixels[..., 3] == 255
    left, top, right, bottom = bbox(white)
    s_left, s_top, s_right, s_bottom = bbox(solid)
    # The exact-white bbox can sit 1 px inside the glyph when its outermost column is anti-aliased.
    # The exact stroke width is pinned by test_subtitle_is_centred_white_text_with_a_black_outline.
    for thickness in (left - s_left, top - s_top, s_right - right, s_bottom - bottom):
        assert 4 <= thickness <= 5
    assert abs((left + right) / 2 - width / 2) < 4 and abs((top + bottom) / 2 - height / 2) < 8
    assert pixels[0, :, 3].max() == pixels[-1, :, 3].max() == 0
    assert pixels[:, 0, 3].max() == pixels[:, -1, 3].max() == 0
    assert ((pixels == BLACK).all(axis=2)).sum() > white.sum() // 2


def test_subtitle_of_a_normal_cue_keeps_the_full_size(cjk_font):
    image = ro.render_subtitle("揭開公元前246至前208年間秦始皇陵之謎。")
    assert image.height == 80 and 22 * 50 * 0.8 < image.width < 22 * 50 + 24 + 100


def test_subtitle_distinct_traditional_glyphs_render_real_distinct_glyphs(cjk_font):
    alphas = {ch: np.asarray(ro.render_subtitle(ch))[..., 3].tobytes() for ch in "秦始皇陵之謎骨直說"}
    assert len(set(alphas.values())) == len(alphas)
    assert all(max(data) == 255 for data in alphas.values())


def test_subtitle_flattens_line_breaks_and_whitespace(fixed_fonts, text_calls):
    image = ro.render_subtitle("a\nb c\r\nd\t\u3000e")
    assert image.size == (5 * 50 + 24, 80)
    assert [call["text"] for call in text_calls] == ["abcde"]


def test_subtitle_with_blank_text_is_a_transparent_strip(fixed_fonts):
    for blank in ["", " ", "\n", " \r\n\t"]:
        image = ro.render_subtitle(blank)
        assert image.size == (24, 80) and np.asarray(image)[..., 3].max() == 0


def test_subtitle_with_line_breaks_renders_with_the_real_font(cjk_font):
    image = ro.render_subtitle("揭開公元前\n246至前208年間")
    assert image.height == 80 and np.asarray(image)[..., 3].max() == 255


# --- shade_factor -----------------------------------------------------------


@pytest.fixture(scope="module")
def shade_1080p():
    return ro.shade_factor(1920, 1080)


@pytest.mark.parametrize("y, x, expected", [
    (0, 960, 1.0), (647, 960, 1.0), (540, 960, 1.0),
    (700, 960, 0.97112), (900, 960, 0.74019), (1079, 960, 0.45),
    (540, 0, 0.75), (540, 1919, 0.75), (540, 216, 0.93757),
    (900, 100, 0.59258), (1079, 216, 0.38757), (1079, 0, 0.3), (1079, 1919, 0.3),
])
def test_shade_factor_golden_values(shade_1080p, y, x, expected):
    assert float(shade_1080p[y, x, 0]) == pytest.approx(expected, abs=1e-3)


def test_shade_factor_is_mirror_symmetric_clamped_and_flat_in_the_middle(shade_1080p):
    f = shade_1080p
    assert np.allclose(f, f[:, ::-1])
    assert f.min() == pytest.approx(0.3) and f.max() == 1.0
    assert (f[:648, 600:1320] == 1.0).all()
    assert f[648, 960, 0] < 1.0


@pytest.mark.parametrize("size", [(1, 1), (1, 7), (7, 1), (2, 2), (3, 2)])
def test_shade_factor_handles_tiny_frames(size):
    f = ro.shade_factor(*size)
    assert f.shape == (size[1], size[0], 1) and f.dtype == np.float32
    assert np.isfinite(f).all() and 0.3 <= f.min() and f.max() <= 1.0


# --- paste_rgba -------------------------------------------------------------


def white_overlay(size=10):
    return Image.new("RGBA", (size, size), WHITE)


@pytest.mark.parametrize("fade, expected", [
    (0.0, 0), (-0.5, 0), (-math.inf, 0), (math.nan, 0),
    (0.25, 63), (0.5, 127), (0.75, 191), (1.0, 255), (1.5, 255), (math.inf, 255),
])
def test_paste_rgba_clamps_the_fade_and_truncates_the_alpha(fade, expected):
    frame = Image.new("RGB", (20, 20), (0, 0, 0))
    ro.paste_rgba(frame, white_overlay(), (0, 0), fade)
    assert frame.getpixel((5, 5)) == (expected,) * 3


def test_paste_rgba_multiplies_the_overlay_alpha_by_the_fade_over_any_background():
    overlay = Image.new("RGBA", (10, 10), (200, 100, 50, 128))
    for fade, expected in [(1.0, (150, 100, 75)), (0.5, (125, 100, 87))]:
        frame = Image.new("RGB", (20, 20), (100, 100, 100))
        ro.paste_rgba(frame, overlay, (0, 0), fade)
        assert frame.getpixel((5, 5)) == expected
        assert frame.getpixel((15, 15)) == (100, 100, 100)


def test_paste_rgba_leaves_the_overlay_untouched_so_every_frame_can_reuse_it():
    overlay = Image.new("RGBA", (10, 10), (200, 100, 50, 128))
    frame = Image.new("RGB", (20, 20), (0, 0, 0))
    assert ro.paste_rgba(frame, overlay, (0, 0), 0.5) is None
    assert overlay.getpixel((5, 5)) == (200, 100, 50, 128)
    assert overlay.getchannel("A").getextrema() == (128, 128)


def test_paste_rgba_places_the_top_left_corner_at_pos():
    frame = Image.new("RGB", (20, 20), (0, 0, 0))
    ro.paste_rgba(frame, white_overlay(), (3, 4))
    expected = np.zeros((20, 20), dtype=np.uint8)
    expected[4:14, 3:13] = 255
    assert np.array_equal(np.asarray(frame)[..., 0], expected)


@pytest.mark.parametrize("pos, rows, cols", [
    ((-5, -5), slice(0, 5), slice(0, 5)),
    ((15, 15), slice(15, 20), slice(15, 20)),
    ((15, -8), slice(0, 2), slice(15, 20)),
])
def test_paste_rgba_clips_an_overlay_that_hangs_over_the_frame_edge(pos, rows, cols):
    frame = Image.new("RGB", (20, 20), (0, 0, 0))
    ro.paste_rgba(frame, white_overlay(), pos)
    expected = np.zeros((20, 20), dtype=np.uint8)
    expected[rows, cols] = 255
    assert np.array_equal(np.asarray(frame)[..., 0], expected)


@pytest.mark.parametrize("pos", [(100, 100), (-100, 0), (0, -100), (20, 0), (0, 20), (-10, -10)])
def test_paste_rgba_ignores_an_overlay_that_is_wholly_off_frame(pos):
    frame = Image.new("RGB", (20, 20), (7, 8, 9))
    ro.paste_rgba(frame, white_overlay(), pos)
    assert (np.asarray(frame) == (7, 8, 9)).all()


def test_paste_rgba_pastes_a_rendered_card_over_the_frame(fixed_fonts):
    card = ro.render_card("12345", "ab")
    frame = Image.new("RGB", (800, 400), (100, 100, 100))
    ro.paste_rgba(frame, card, (50, 60), 1.0)
    assert frame.getpixel((50 + 5, 60 + 75)) == ro.ACCENT
    assert frame.getpixel((10, 10)) == (100, 100, 100)


# --- the face that was actually loaded is logged ------------------------------


class NamedFont:
    def __init__(self, names):
        self.names = names

    def getname(self):
        if isinstance(self.names, Exception):
            raise self.names
        return self.names


def use_fake_font(monkeypatch, font, path="x.ttc", index="3"):
    monkeypatch.setenv("SUBTITLE_FONT_FILE", path)
    monkeypatch.setenv("SUBTITLE_FONT_INDEX", index)
    monkeypatch.setattr(ro.ImageFont, "truetype", lambda path, size, index=0: font)


def font_records(caplog):
    return [record for record in caplog.records if record.name == "render_overlays"]


def test_load_font_logs_which_face_was_loaded(monkeypatch, caplog):
    use_fake_font(monkeypatch, NamedFont(("Noto Sans CJK TC", "Bold")))
    with caplog.at_level(logging.INFO):
        ro.load_font(50, True)
    [record] = font_records(caplog)
    assert record.levelno == logging.INFO
    assert record.getMessage() == "字型 x.ttc (index 3) → Noto Sans CJK TC Bold"


def test_the_font_log_names_the_path_and_the_index_that_were_opened(monkeypatch, caplog):
    use_fake_font(monkeypatch, NamedFont(("Microsoft JhengHei", "Regular")), path="C:/f/other.ttc", index="0")
    with caplog.at_level(logging.INFO):
        ro.load_font(30, False)
    assert [record.getMessage() for record in font_records(caplog)] == [
        "字型 C:/f/other.ttc (index 0) → Microsoft JhengHei Regular"]


def test_a_cached_font_is_not_logged_again(monkeypatch, caplog):
    use_fake_font(monkeypatch, NamedFont(("Noto Sans CJK TC", "Bold")))
    with caplog.at_level(logging.INFO):
        ro.load_font(50, True)
        ro.load_font(50, True)
    assert len(font_records(caplog)) == 1


@pytest.mark.parametrize("names", [AttributeError("no getname"), OSError("no names table"), ValueError("bad")])
def test_a_font_without_names_still_loads_and_is_still_logged(monkeypatch, caplog, names):
    font = NamedFont(names)
    use_fake_font(monkeypatch, font)
    with caplog.at_level(logging.INFO):
        assert ro.load_font(50, True) is font
    assert [record.getMessage() for record in font_records(caplog)] == ["字型 x.ttc (index 3) → 未知"]


def test_a_font_object_with_no_getname_at_all_still_loads(monkeypatch, caplog):
    sentinel = object()
    use_fake_font(monkeypatch, sentinel)
    with caplog.at_level(logging.INFO):
        assert ro.load_font(50, True) is sentinel
    assert len(font_records(caplog)) == 1


def test_nothing_is_logged_when_the_font_cannot_be_opened(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SUBTITLE_FONT_FILE", str(tmp_path / "missing.ttc"))
    with caplog.at_level(logging.INFO), pytest.raises(OSError):
        ro.load_font(50, True)
    assert font_records(caplog) == []


def test_load_font_logs_the_real_face_name(cjk_font, caplog):
    path, index = ro.find_font(True)
    with caplog.at_level(logging.INFO):
        font = ro.load_font(40, True)
    family, style = font.getname()
    assert [record.getMessage() for record in font_records(caplog)] == [f"字型 {path} (index {index}) → {family} {style}"]
