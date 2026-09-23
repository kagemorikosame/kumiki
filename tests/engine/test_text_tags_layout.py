"""制御文字で書体・大きさ・色が混ざった AviUtl2 の組み方のテキスト（Issue #108）

数は AviUtl2 v2.1.6a に書き出させた見本（``tools/aviutl_tag_probes.py``）で測った物
書体が入っていない機械では数が合わないので飛ばす
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pytest
from PySide6.QtGui import QFont

from sashimono.core.model import AnimatedValue, GeneratedSource, ParamValue
from sashimono.engine import sources
from sashimono.engine.sources import aviutl_font_family, render_source_framed

SCREEN = (1920, 1080)


@pytest.fixture(autouse=True)
def japanese_names(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """見本を測った機械と同じく、書体を日本語の名前で引く 英語の Windows でも数を揃える"""
    for family in ("Arial", "メイリオ", "游明朝", "ＭＳ ゴシック", "Yu Gothic UI"):
        if not QFont(family).exactMatch():
            pytest.skip(f"{family} が入っていない")
    monkeypatch.setattr(sources, "_system_is_japanese", lambda: True)
    yield


def text(body: str, **params: ParamValue) -> GeneratedSource:
    base: dict[str, ParamValue] = {
        "text": body,
        "font": "Arial",
        "size": AnimatedValue(100.0),
        "color": (1.0, 1.0, 1.0, 1.0),
        "layout": "aviutl",
    }
    base.update(params)
    return GeneratedSource(kind="text", params=base)


def glyph_bottoms(image: np.ndarray) -> list[int]:
    """左から順に、字ごとの下端（画素） 字の間の空いた列で区切る"""
    ink = image[..., 3] > 128
    columns = ink.any(axis=0)
    bottoms: list[int] = []
    start = None
    for x, filled in enumerate([*columns.tolist(), False]):
        if filled and start is None:
            start = x
        elif not filled and start is not None:
            rows = np.nonzero(ink[:, start:x].any(axis=1))[0]
            bottoms.append(int(rows.max()) + 1)
            start = None
    return bottoms


def draw(body: str, **params: ParamValue) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    image, framed = render_source_framed(text(body, **params), *SCREEN)
    assert image is not None and framed is not None
    return image, framed


class TestFontNames:
    def test_english_names_of_japanese_fonts_fall_back_to_the_default(self) -> None:
        # AviUtl2 は <@Meiryo> をメイリオで描かず、既定の Yu Gothic UI で描いた
        # Qt に任せるとメイリオで描き、送り幅が 1 文字 3 画素ずれる
        assert aviutl_font_family("Meiryo", japanese=True) == "Yu Gothic UI"
        assert aviutl_font_family("メイリオ", japanese=True) == "メイリオ"
        assert aviutl_font_family("ＭＳ ゴシック", japanese=True) == "ＭＳ ゴシック"

    def test_fonts_with_only_an_english_name_are_found_by_it(self) -> None:
        assert aviutl_font_family("Arial", japanese=True) == "Arial"
        assert aviutl_font_family("Yu Gothic UI", japanese=True) == "Yu Gothic UI"

    def test_an_unknown_name_falls_back_to_the_default(self) -> None:
        assert aviutl_font_family("存在しない書体", japanese=True) == "Yu Gothic UI"

    def test_the_object_font_follows_the_same_rule(self) -> None:
        # 見本 tag19 設定欄の フォント=Meiryo も Yu Gothic UI の行の高さ（133）で組まれた
        _, english = draw("HH\nHH", font="Meiryo")
        _, japanese = draw("HH\nHH", font="メイリオ")
        assert english[3] - english[1] == pytest.approx(2 * 133.0, abs=1.0)
        assert japanese[3] - japanese[1] == pytest.approx(2 * 150.0, abs=1.0)


class TestMixedLine:
    def test_the_tags_are_not_drawn_as_text(self) -> None:
        # 読まないと <@メイリオ> の 8 文字が字として並び、枠が 4 倍近く広がる
        _, framed = draw("H<@メイリオ>H<@>")
        _, plain = draw("HH")
        assert framed[2] - framed[0] < (plain[2] - plain[0]) * 1.2

    def test_fonts_share_one_baseline(self) -> None:
        # 見本 tag14 Arial・メイリオ・ＭＳ ゴシックの H の下端はどれも 559 だった
        image, _ = draw("H<@メイリオ>H<@><@ＭＳ ゴシック>H<@>H")
        bottoms = glyph_bottoms(image)
        assert len(bottoms) == 4
        assert max(bottoms) - min(bottoms) <= 1

    def test_the_line_takes_the_tallest_font_whole(self) -> None:
        # 見本 tag14 游明朝の行送り（上 99.5 + 下と余白 60.7）で、枠は 460..620 だった
        # 上の高さの最大（メイリオ 106）と下の深さの最大を足すと 150 で、ベースラインが 7 画素下がる
        image, framed = draw("H<@メイリオ>H<@><@ＭＳ ゴシック>H<@><@游明朝>H<@>H")
        assert framed[3] - framed[1] == pytest.approx(160.2, abs=1.0)
        baseline = glyph_bottoms(image)[0]
        assert baseline - framed[1] == pytest.approx(99.5, abs=1.5)

    def test_a_bigger_line_pitch_beats_a_bigger_ascent(self) -> None:
        # 見本 tag17 Yu Gothic UI 100（上 108）とメイリオ 90（上 95.4 行送り 135）
        # 1 行目はメイリオで組まれ、ベースラインは枠の上から 95 だった
        image, framed = draw("H<s90,メイリオ>H<s>H", font="Yu Gothic UI")
        assert framed[3] - framed[1] == pytest.approx(135.0, abs=1.0)
        assert glyph_bottoms(image)[0] - framed[1] == pytest.approx(95.4, abs=1.5)

    def test_each_line_has_its_own_height(self) -> None:
        # 見本 tag15 メイリオ・Arial・ＭＳ ゴシックの 3 行で 364
        # 最初の行の書体で全部の行を組むと 450 になる
        _, framed = draw("<@メイリオ>HH<@>\nHH\n<@ＭＳ ゴシック>HH<@>")
        assert framed[3] - framed[1] == pytest.approx(150.0 + 115.3 + 100.0, abs=1.5)

    def test_an_empty_line_uses_the_size_set_on_it(self) -> None:
        # 見本 tag22 枠は 253..827 の 574 大きさ 100 の行と 200 の行 2 つ
        # 空の行を設定欄の大きさで組むと 459 になり、3 行目が 115 画素上へずれる
        _, framed = draw("H\n<s200>\nH")
        assert framed[3] - framed[1] == pytest.approx(574.0, abs=1.5)

    def test_sizes_share_one_baseline(self) -> None:
        # 見本 tag09 大きさ 50〜220 の H の下端はどれも 612 だった
        image, _ = draw("H<s50>H<s>H<s*2>H<s+20>H")
        bottoms = glyph_bottoms(image)
        assert len(bottoms) == 5
        assert max(bottoms) - min(bottoms) <= 1


class TestColours:
    def test_colour_tags_paint_each_run(self) -> None:
        # 見本 tag08 2 文字目は ff0000 で塗られ、<#> の後は設定欄の白に戻った
        image, _ = draw("H<#ff0000>H<#>H")
        columns = np.nonzero((image[..., 3] > 250).any(axis=0))[0]
        middle = image[:, (columns.min() + columns.max()) // 2]
        solid = middle[middle[:, 3] == 255]
        assert (solid[:, :3] == (255, 0, 0)).all(axis=1).any()
        left = image[:, columns.min() + 3]
        assert (left[left[:, 3] == 255][:, :3] == (255, 255, 255)).all(axis=1).any()

    def test_the_second_colour_changes_the_edge(self) -> None:
        # 縁取りの色だけを変える 縁の色が変わらないと、字幕の縁取りの色分けが消える
        image, _ = draw(
            "<#ffffff,00ff00>H",
            border_width=AnimatedValue(6.0),
            border_color=(0.0, 0.0, 0.0, 1.0),
        )
        opaque = image[image[..., 3] == 255][:, :3]
        assert ((opaque == (0, 255, 0)).all(axis=1)).any()
        assert not ((opaque == (0, 0, 0)).all(axis=1)).any()


def test_native_text_keeps_tags_as_written() -> None:
    # 読むのは AviUtl2 の組み方だけ YMM4 や Sashimono の文字に <@ と書いた人の字を消さない
    native, _ = render_source_framed(text("<@メイリオ>H", layout="native"), *SCREEN)
    plain, _ = render_source_framed(text("H", layout="native"), *SCREEN)
    assert native is not None and plain is not None
    assert int((native[..., 3] > 128).sum()) > int((plain[..., 3] > 128).sum()) * 3


def test_reveal_counts_the_letters_not_the_tags() -> None:
    # 文字送り 50% で 4 文字のうち 2 文字 タグを文字として数えると 0 文字か途中のタグが出る
    half, _ = draw("<@メイリオ>HH<@>HH", reveal=AnimatedValue(50.0))
    assert len(glyph_bottoms(half)) == 2


class TestRevealLayout:
    """文字送りの途中は、出た文字だけで組む（制御文字を読む前の組み方と同じ）

    AviUtl2 で測った決まりではない 文字送りは Sashimono だけの設定で、前の版の組み方を
    変えないための試験 まだ出ていない行まで高さに数えると、中央揃えの字が出る途中で
    前の版より上へずれる
    """

    def test_trailing_unrevealed_lines_are_not_laid_out(self) -> None:
        body = "<@メイリオ>HH<@>\nHH\n<@ＭＳ ゴシック>HH<@>"
        # 6 文字のうち 3 文字 1 行目と 2 行目の 1 文字だけが出る
        half, half_frame = draw(body, reveal=AnimatedValue(50.0))
        shorter, shorter_frame = draw("<@メイリオ>HH<@>\nH")
        assert half_frame == pytest.approx(shorter_frame, abs=0.01)
        assert np.array_equal(half[..., 3] > 128, shorter[..., 3] > 128)

    def test_an_exact_line_end_keeps_one_empty_line_like_before(self) -> None:
        # 前の組み方は、行末でちょうど尽きると次の行を空の行として 1 つ残した
        # 残す行の高さは止まった所の見た目（ここではＭＳ ゴシック）
        body = "HH\n<@ＭＳ ゴシック>HH<@>\nHH"
        _, exact = draw(body, reveal=AnimatedValue(100.0 * 2 / 6))
        _, two_lines = draw("HH\n<@ＭＳ ゴシック> <@>")
        assert exact[3] - exact[1] == pytest.approx(two_lines[3] - two_lines[1], abs=0.01)
