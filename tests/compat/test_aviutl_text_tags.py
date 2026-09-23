"""AviUtl2 のテキストの制御文字を読む（``compat/aviutl/text_tags.py``）

振る舞いは AviUtl2 v2.1.6a に描かせて測った物（Issue #108 ``tools/aviutl_tag_probes.py``）
読み違えると、合成フォントで組み替えた字幕がタグの文字のまま出るか、書体や大きさが
AviUtl2 と違う所で切り替わる
"""

from __future__ import annotations

from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.aviutl.text_tags import (
    DROPPED_DECORATION,
    DROPPED_SIZE_STYLE,
    TextStyle,
    parse_tags,
)


def runs(text: str, size: float = 100.0) -> list[list[tuple[str, TextStyle]]]:
    return [[(run.text, run.style) for run in line.runs] for line in parse_tags(text, size)]


class TestFont:
    def test_the_composite_font_output_becomes_runs_without_the_tags(self) -> None:
        # comfont.aux2 が実際に返した形 読まないと <@游明朝> がそのまま字として出る
        text = "<@游明朝>ここに<@><@メイリオ>テキスト<@><@游明朝>を<@><@ＭＳ ゴシック>入力<@>"
        (line,) = runs(text)
        assert [(part, style.font) for part, style in line] == [
            ("ここに", "游明朝"),
            ("テキスト", "メイリオ"),
            ("を", "游明朝"),
            ("入力", "ＭＳ ゴシック"),
        ]

    def test_reset_returns_to_the_object_font_not_the_previous_one(self) -> None:
        # AviUtl2 の見本 tag05 では、入れ子の後の <@> で 4 文字目が設定欄の Arial に戻った
        # 1 つ前へ戻す作りにすると 4 文字目が前の書体のまま残る
        (line,) = runs("H<@A>H<@B>H<@>H<@>H")
        assert [style.font for _part, style in line] == [None, "A", "B", None, None]

    def test_a_font_carries_over_a_line_break(self) -> None:
        # 見本 tag06 改行の後の 2 文字もメイリオの送り幅で描かれた
        first, second = runs("H<@メイリオ>H\nHH<@>H")
        assert [style.font for _part, style in first] == [None, "メイリオ"]
        assert [(part, style.font) for part, style in second] == [("HH", "メイリオ"), ("H", None)]

    def test_the_decoration_number_is_not_taken_for_the_font_name(self) -> None:
        # 書体名に ,3 まで含めると、見つからない書体として既定の書体で描いてしまう
        (line,) = runs("<@メイリオ,3>H")
        assert line[0][1].font == "メイリオ"

    def test_style_changes_stay_as_text(self) -> None:
        # <@+B> は測っていない 黙って捨てると、描けていないことに気付けない
        (line,) = runs("A<@+B>B<@-B>C")
        assert [part for part, _style in line] == ["A<@+B>B<@-B>C"]


class TestSize:
    def test_relative_sizes_follow_the_current_size(self) -> None:
        # 見本 tag09 <s*2> は設定欄の 100 の倍、続く <s+20> はその 200 に足して 220
        (line,) = runs("H<s50>H<s>H<s*2>H<s+20>H")
        assert [style.size for _part, style in line] == [None, 50.0, None, 200.0, 220.0]

    def test_a_size_carries_over_a_line_break(self) -> None:
        # 見本 tag10 2 行目も 200 のまま描かれた
        first, second, third = runs("H<s200>H\nHH\n<s>HH")
        assert first[-1][1].size == 200.0
        assert second[0][1].size == 200.0
        assert third[0][1].size is None

    def test_the_size_tag_can_change_the_font_and_reset_takes_it_back(self) -> None:
        # 見本 tag11 <s80,Meiryo> の字は Meiryo で探され、<s> の後は設定欄の書体に戻った
        (line,) = runs("H<s80,メイリオ,B,3>H<s>H")
        assert [(style.size, style.font) for _part, style in line] == [
            (None, None),
            (80.0, "メイリオ"),
            (None, None),
        ]

    def test_an_empty_line_remembers_its_size(self) -> None:
        # 見本 tag22 文字の無い真ん中の行は、その行で指定した 200 の高さだった
        lines = parse_tags("H\n<s200>\nH", 100.0)
        assert lines[1].runs == []
        assert lines[1].end.size == 200.0

    def test_a_size_never_drops_to_zero(self) -> None:
        # 0 以下の大きさで書体を作ると、Qt が既定の大きさで描き、指定と逆に大きくなる
        (line,) = runs("<s-500>H")
        assert line[0][1].size == 1.0

    def test_words_starting_with_s_are_not_tags(self) -> None:
        (line,) = runs("<span>x")
        assert line == [("<span>x", TextStyle())]


class TestColor:
    def test_text_and_edge_colours_and_reset(self) -> None:
        # 見本 tag08 2 文字目は赤で縁は設定欄の灰、3 文字目は緑で縁は青、4 文字目は元の色
        (line,) = runs("H<#ff0000>H<#00ff00,0000ff>H<#>H")
        assert [(style.color, style.edge) for _part, style in line] == [
            (None, None),
            ("ff0000", None),
            ("00ff00", "0000ff"),
            (None, None),
        ]

    def test_preset_names_use_the_default_palette(self) -> None:
        # 見本 tag23 <#red> は ff0000 で描かれた
        (line,) = runs("H<#red>H<#>H")
        assert line[1][1].color == "ff0000"

    def test_an_unreadable_colour_stays_as_text(self) -> None:
        (line,) = runs("A<#zzz>B")
        assert [part for part, _style in line] == ["A<#zzz>B"]


class TestDroppedParts:
    """読まずに捨てた所を互換性の記録に数える

    数えないと、縁取りの付くはずの字が素の字で出ていても本人が気付けない
    """

    def test_a_decoration_number_is_counted(self) -> None:
        report = CompatibilityReport()
        parse_tags("<@メイリオ,3>H<@>H<@メイリオ,6BI>H", 100.0, report)
        assert report.missing[DROPPED_DECORATION] == 2
        assert any(DROPPED_DECORATION in line for line in report.lines())

    def test_a_size_style_and_outline_are_counted(self) -> None:
        report = CompatibilityReport()
        parse_tags("<s80,メイリオ,B,3>H<s>H<s80,メイリオ>H<s50>H", 100.0, report)
        # 書体までの指定は読めているので数えない
        assert report.missing[DROPPED_SIZE_STYLE] == 1

    def test_tags_that_are_read_whole_leave_no_record(self) -> None:
        report = CompatibilityReport()
        parse_tags("<@メイリオ>H<@><#ff0000,0000ff>H<#><s*2>H<s>", 100.0, report)
        assert report.is_empty
