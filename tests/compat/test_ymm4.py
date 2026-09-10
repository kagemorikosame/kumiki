"""YMM4 のアイテムテンプレートと文字装飾。

YMM4 は .NET のシリアライザで書き出すので、型が ``$type`` に入り、数値は
「アニメーション」の形を取りうる。ここで検査しているのは、その 2 つの癖と、
装飾の列をこちらの持ち物へどう分けるか。

このマシンに YMM4 は入っていないので、ここにあるのは**実ファイルではなく
形式に沿って組んだもの**。だから振り分けはクラス名だけで行い、名前空間の
違いで落ちないことも一緒に検査している。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novaedit.compat.aviutl.report import CompatibilityReport
from novaedit.compat.ymm4.decorations import map_decorations
from novaedit.compat.ymm4.template import Ymm4ParseError, load_template, map_template
from novaedit.compat.ymm4.values import animated, colour, number, type_name
from novaedit.core.model import AnimatedValue, Interpolation


def value_at(value: object, frame: int = 0) -> float:
    """数値パラメータの、その時刻での値。

    :data:`~novaedit.core.model.ParamValue` は数値とは限らないので、
    数値であることをここで 1 度だけ確かめる。
    """
    assert isinstance(value, AnimatedValue)
    return value.at(frame)


def text_item(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "$type": "YukkuriMovieMaker.Project.Items.TextItem, YukkuriMovieMaker",
        "Text": "字幕",
        "Font": "Noto Sans JP",
        "FontSize": 60,
        "FontColor": "#FFFFEE00",
        "Frame": 0,
        "Length": 90,
        "Layer": 0,
    }
    base.update(fields)
    return base


class TestTypeNames:
    def test_only_the_class_name_is_used(self) -> None:
        # 名前空間もアセンブリ名も版で変わる。丸ごと突き合わせると、
        # YMM4 が更新されただけで全部読めなくなる。
        assert type_name({"$type": "A.B.C.TextItem, Assembly, Version=4.0"}) == "TextItem"

    def test_a_missing_type_is_empty(self) -> None:
        assert type_name({"Text": "x"}) == ""
        assert type_name("文字列") == ""


class TestValues:
    def test_a_bare_number(self) -> None:
        assert number(60) == 60.0

    def test_an_animation_with_one_value_is_static(self) -> None:
        value = animated({"Values": [{"Value": 42, "Frame": 0}]})
        assert value.is_animated is False
        assert value.at(0) == 42.0

    def test_an_animation_with_several_values_becomes_keyframes(self) -> None:
        value = animated(
            {
                "Values": [{"Value": 0, "Frame": 0}, {"Value": 100, "Frame": 30}],
                "AnimationType": "直線移動",
            }
        )
        assert value.is_animated is True
        assert value.at(15) == pytest.approx(50.0)

    def test_the_interpolation_name_is_japanese(self) -> None:
        value = animated(
            {"Values": [{"Value": 0, "Frame": 0, "Type": "瞬間移動"}, {"Value": 9, "Frame": 10}]}
        )
        assert value.keyframes[0].interpolation is Interpolation.HOLD

    def test_values_out_of_order_are_sorted(self) -> None:
        value = animated({"Values": [{"Value": 5, "Frame": 20}, {"Value": 1, "Frame": 0}]})
        assert [k.frame for k in value.keyframes] == [0, 20]

    def test_alpha_comes_first_in_a_colour(self) -> None:
        # YMM4 は #AARRGGBB。後ろだと思って読むと、不透明のつもりが透明になる。
        assert colour("#80FF0000") == pytest.approx((1.0, 0.0, 0.0, 128 / 255))

    def test_a_colour_without_alpha_is_opaque(self) -> None:
        assert colour("#00FF00") == pytest.approx((0.0, 1.0, 0.0, 1.0))


class TestDecorations:
    def test_a_border_lands_on_the_text(self) -> None:
        result = map_decorations(
            [{"$type": "N.BorderDecoration, A", "Thickness": 6, "Color": "#FF000000"}],
            CompatibilityReport(),
        )
        assert value_at(result.params["border_width"], 0) == 6.0
        assert result.effects == []

    def test_two_borders_keep_the_thicker_one_on_the_text(self) -> None:
        # 細いほうをテキストに載せると、太いほうをエフェクトで足したときに
        # 二重の縁の間隔が変わる。
        result = map_decorations(
            [
                {"$type": "N.BorderDecoration, A", "Thickness": 4, "Color": "#FFFFFFFF"},
                {"$type": "N.BorderDecoration, A", "Thickness": 12, "Color": "#FF000000"},
            ],
            CompatibilityReport(),
        )
        assert value_at(result.params["border_width"], 0) == 12.0
        assert [e.kind for e in result.effects] == ["border"]
        assert value_at(result.effects[0].params["width"]) == 4.0

    def test_a_shadow_lands_on_the_text(self) -> None:
        result = map_decorations(
            [{"$type": "N.ShadowDecoration, A", "X": 5, "Y": 5, "Blur": 3}],
            CompatibilityReport(),
        )
        assert value_at(result.params["shadow_x"], 0) == 5.0
        assert value_at(result.params["shadow_blur"], 0) == 3.0

    def test_a_gradient_becomes_an_effect(self) -> None:
        result = map_decorations(
            [{"$type": "N.GradationDecoration, A", "Colors": ["#FFFF0000", "#FF0000FF"]}],
            CompatibilityReport(),
        )
        assert [e.kind for e in result.effects] == ["gradient"]

    def test_an_unknown_decoration_is_recorded_not_dropped(self) -> None:
        report = CompatibilityReport()
        map_decorations([{"$type": "N.SparkleDecoration, A"}], report)
        assert any("SparkleDecoration" in line for line in report.lines())

    def test_something_that_is_not_a_list_is_ignored(self) -> None:
        assert map_decorations(None, CompatibilityReport()).params == {}


class TestTemplate:
    def test_a_text_item_becomes_a_text_clip(self) -> None:
        mapped = map_template([text_item()], report=CompatibilityReport())
        assert len(mapped) == 1
        source = mapped[0].clip.source
        assert source is not None
        assert source.kind == "text"
        assert source.params["text"] == "字幕"
        assert source.params["font"] == "Noto Sans JP"

    def test_the_layer_shifts_by_one(self) -> None:
        # YMM4 のレイヤーは 0 始まり、こちらのトラックは 1 始まり。
        assert map_template([text_item(Layer=2)], report=CompatibilityReport())[0].layer == 3

    def test_the_span_comes_from_frame_and_length(self) -> None:
        clip = map_template([text_item(Frame=30, Length=120)], report=CompatibilityReport())[0].clip
        assert (clip.timeline_start, clip.duration) == (30, 120)

    def test_the_position_becomes_a_transform(self) -> None:
        mapped = map_template([text_item(X=100, Y=200)], report=CompatibilityReport())[0]
        assert mapped.clip.effects[0].kind == "transform"
        # YMM4 の Y は下向き。こちらは上向き。
        assert value_at(mapped.clip.effects[0].params["pos_y"]) == pytest.approx(-200.0)

    def test_an_animated_position_keeps_its_keyframes(self) -> None:
        item = text_item(X={"Values": [{"Value": 0, "Frame": 0}, {"Value": 300, "Frame": 60}]})
        effect = map_template([item], report=CompatibilityReport())[0].clip.effects[0]
        assert value_at(effect.params["pos_x"], 30) == pytest.approx(150.0)

    def test_a_still_item_adds_no_transform(self) -> None:
        mapped = map_template([text_item()], report=CompatibilityReport())[0]
        assert mapped.clip.effects == ()

    def test_decorations_reach_the_text(self) -> None:
        item = text_item(
            Decorations=[{"$type": "N.BorderDecoration, A", "Thickness": 8, "Color": "#FF000000"}]
        )
        source = map_template([item], report=CompatibilityReport())[0].clip.source
        assert source is not None
        assert value_at(source.params["border_width"], 0) == 8.0

    def test_a_media_item_returns_its_path(self) -> None:
        item = {
            "$type": "YukkuriMovieMaker.Project.Items.VideoItem, YukkuriMovieMaker",
            "FilePath": "C:/素材/映像.mp4",
            "Length": 60,
        }
        mapped = map_template([item], report=CompatibilityReport())
        assert mapped[0].media_path == "C:/素材/映像.mp4"

    def test_an_unknown_item_is_recorded_and_skipped(self) -> None:
        report = CompatibilityReport()
        mapped = map_template([{"$type": "N.TachieItem, A"}], report=report)
        assert mapped == []
        assert any("TachieItem" in line for line in report.lines())


class TestLoading:
    def test_a_bare_item(self, tmp_path: Path) -> None:
        path = tmp_path / "字幕.ymmt"
        path.write_text(json.dumps(text_item()), "utf-8")
        assert len(load_template(path)) == 1

    def test_items_in_a_wrapper(self, tmp_path: Path) -> None:
        path = tmp_path / "束.ymmt"
        path.write_text(json.dumps({"Items": [text_item(), text_item()]}), "utf-8")
        assert len(load_template(path)) == 2

    def test_a_byte_order_mark_is_tolerated(self, tmp_path: Path) -> None:
        path = tmp_path / "bom.ymmt"
        path.write_text(json.dumps(text_item()), "utf-8-sig")
        assert len(load_template(path)) == 1

    def test_broken_json_says_so(self, tmp_path: Path) -> None:
        path = tmp_path / "壊れ.ymmt"
        path.write_text("{これは JSON ではない", "utf-8")
        with pytest.raises(Ymm4ParseError):
            load_template(path)
