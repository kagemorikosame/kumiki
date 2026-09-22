"""制御文字からパラメータ定義へ

ここが通れば、AviUtl のスクリプトも自前のエフェクトとまったく同じ設定 UI に
載る P2 でパラメータ定義を AviUtl 互換の 1 形式に統一した狙いがこれ
"""

from __future__ import annotations

from sashimono.compat.aviutl.control import parse_control, split_scripts
from sashimono.effects.spec import (
    CheckSpec,
    ColorSpec,
    FileSpec,
    FontSpec,
    SelectSpec,
    TextSpec,
    TrackSpec,
    ValueSpec,
)


class TestTrack:
    def test_a_slider_becomes_a_track_spec(self) -> None:
        header = parse_control("--track0:移動量,-100,100,20,0.1")
        spec = header.parameters[0]
        assert isinstance(spec, TrackSpec)
        assert (spec.name, spec.label) == ("track0", "移動量")
        assert (spec.minimum, spec.maximum, spec.default) == (-100.0, 100.0, 20.0)

    def test_all_four_slots(self) -> None:
        header = parse_control(
            "--track0:A,0,1,0\n--track1:B,0,1,0\n--track2:C,0,1,0\n--track3:D,0,1,0"
        )
        assert [spec.name for spec in header.parameters] == [
            "track0",
            "track1",
            "track2",
            "track3",
        ]

    def test_a_reversed_range_is_straightened(self) -> None:
        spec = parse_control("--track0:逆,100,-100,0").parameters[0]
        assert isinstance(spec, TrackSpec)
        assert (spec.minimum, spec.maximum) == (-100.0, 100.0)

    def test_a_default_outside_the_range_is_clamped(self) -> None:
        spec = parse_control("--track0:はみ出し,0,10,999").parameters[0]
        assert isinstance(spec, TrackSpec)
        assert spec.default == 10.0

    def test_the_named_form_from_the_newer_generation(self) -> None:
        spec = parse_control("--track@speed:速度,0,10,1").parameters[0]
        assert spec.name == "speed"
        assert spec.label == "速度"


class TestCheckAndColor:
    def test_a_checkbox(self) -> None:
        spec = parse_control("--check0:反転,1").parameters[0]
        assert isinstance(spec, CheckSpec)
        assert (spec.name, spec.label, spec.default) == ("check0", "反転", True)

    def test_a_colour(self) -> None:
        spec = parse_control("--color:線の色,0xff8000").parameters[0]
        assert isinstance(spec, ColorSpec)
        assert spec.default[0] == 1.0
        assert abs(spec.default[1] - 128 / 255) < 0.01
        assert spec.default[2] == 0.0

    def test_a_file_and_a_font(self) -> None:
        header = parse_control("--file:読み込むもの\n--font:書体")
        assert isinstance(header.parameters[0], FileSpec)
        assert isinstance(header.parameters[1], FontSpec)


class TestDialog:
    def test_plain_numbers_become_sliders(self) -> None:
        # AviUtl のダイアログは入力欄だが、こちらではキーフレームを打てる方が
        # 使い出がある
        spec = parse_control("--dialog:回数,count=3;").parameters[0]
        assert isinstance(spec, TrackSpec)
        assert (spec.name, spec.default) == ("count", 3.0)

    def test_a_quoted_default_becomes_text(self) -> None:
        spec = parse_control('--dialog:本文,body="こんにちは";').parameters[0]
        assert isinstance(spec, TextSpec)
        assert spec.default == "こんにちは"

    def test_the_chk_suffix_makes_a_checkbox(self) -> None:
        spec = parse_control("--dialog:影を付ける/chk,shadow=1;").parameters[0]
        assert isinstance(spec, CheckSpec)
        assert (spec.label, spec.default) == ("影を付ける", True)

    def test_the_col_suffix_makes_a_colour(self) -> None:
        spec = parse_control("--dialog:色/col,col=0x00ff00;").parameters[0]
        assert isinstance(spec, ColorSpec)
        assert spec.default[:3] == (0.0, 1.0, 0.0)

    def test_the_fig_suffix_lists_the_aviutl_figures(self) -> None:
        spec = parse_control('--dialog:図形/fig,fig="星形";').parameters[0]
        assert isinstance(spec, SelectSpec)
        assert spec.default == "星形"
        assert "六角形" in dict(spec.choices)

    def test_several_items_in_one_line(self) -> None:
        header = parse_control("--dialog:X,x=10;Y,y=20;色/col,c=0xffffff;")
        assert [spec.name for spec in header.parameters] == ["x", "y", "c"]

    def test_items_without_an_assignment_are_skipped(self) -> None:
        assert parse_control("--dialog:ラベルだけ;").parameters == ()


class TestOtherControls:
    def test_param_is_kept_as_setup_code(self) -> None:
        header = parse_control("--param:a=1;b=2;")
        assert header.setup == "a=1;b=2;"

    def test_labels_are_collected(self) -> None:
        assert parse_control("--label:見出し").labels == ("見出し",)

    def test_the_newer_select_control(self) -> None:
        spec = parse_control("--select@mode:種類,直線=line/曲線=curve,curve").parameters[0]
        assert isinstance(spec, SelectSpec)
        assert dict(spec.choices) == {"line": "直線", "curve": "曲線"}
        assert spec.default == "curve"

    def test_value_stays_a_plain_number(self) -> None:
        spec = parse_control("--value@seed:シード,42").parameters[0]
        assert isinstance(spec, ValueSpec)
        assert spec.default == 42

    def test_unsupported_controls_are_recorded(self) -> None:
        # 記録が残っていれば、次に何を実装すべきかをデータで決められる
        header = parse_control("--twopoint\n--speed:10,20")
        assert len(header.unknown) == 2

    def test_ordinary_comments_are_not_recorded(self) -> None:
        header = parse_control("-- ただのコメント\n--[[ ブロック ]]")
        assert header.unknown == ()

    def test_lua_code_is_ignored(self) -> None:
        header = parse_control("local x = 1\nobj.ox = x")
        assert header.parameters == ()


class TestSplitScripts:
    def test_a_file_without_markers_is_one_script(self) -> None:
        sections = split_scripts("--track0:A,0,1,0\nobj.ox = obj.track0")
        assert len(sections) == 1
        assert sections[0].header.parameters[0].name == "track0"

    def test_markers_split_the_file(self) -> None:
        text = (
            "@ふわふわ\n--track0:幅,0,100,10\nobj.oy = 1\n"
            "@ぐるぐる\n--track0:速さ,0,10,1\nobj.rz = 2"
        )
        sections = split_scripts(text)
        assert [section.header.name for section in sections] == ["ふわふわ", "ぐるぐる"]
        assert sections[0].header.parameters[0].label == "幅"
        assert sections[1].header.parameters[0].label == "速さ"

    def test_each_section_keeps_only_its_own_code(self) -> None:
        sections = split_scripts("@あ\nobj.ox = 1\n@い\nobj.oy = 2")
        assert "obj.oy" not in sections[0].source
        assert "obj.ox" not in sections[1].source
