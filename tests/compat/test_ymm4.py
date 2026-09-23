"""YMM4 のアイテムテンプレート

ここに並んでいるのは全部、**ネットで配布されている実物の ``.ymmt`` を読ませて
見つかったもの** 最初は形式の推測で書いていて、実物では 1 本も読めなかった

見つかった食い違いはどれも「知らなければ当たらない」たぐいのもので、

* ``.ymmt`` は ZIP で、中の ``catalog.json`` が本体
* 1 ファイルに何本も入っている（実物は 17 本と 106 本）
* アニメーションの値に**フレーム番号が入っていない** 位置はアイテムの長さと
  「中間点」から決まる
* 文字装飾は ``Decorations``（実物では空）ではなく ``Style`` と
  ``VideoEffects`` の ``OutlineEffect`` に入っている

この検査は、実物と同じ形に組んだ ZIP を作って通す 実物そのものは配布物なので
リポジトリには置かない（``tests/fixtures/ymm4`` に置けば
``tests/compat/test_real_ymm4.py`` が拾う）
"""

from __future__ import annotations

import json
import zipfile
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.catalog import place, restyle
from sashimono.compat.ymm4.decorations import map_decorations, map_video_effects
from sashimono.compat.ymm4.template import Ymm4ParseError, load_template, map_template
from sashimono.compat.ymm4.values import (
    animated,
    brush_colour,
    colour,
    curve_of,
    frame_positions,
    interpolation_of,
    number,
    type_name,
)
from sashimono.core.commands import SetSource
from sashimono.core.model import AnimatedValue, Clip, GeneratedSource, Interpolation, Project

#: 実物と同じ書き方のブラシ
BRUSH = {
    "Type": "YukkuriMovieMaker.Plugin.Brush.SolidColorBrushPlugin, YukkuriMovieMaker, Version=4.32",
    "Parameter": {
        "$type": "YukkuriMovieMaker.Plugin.Brush.SolidColorBrushParameter, YukkuriMovieMaker",
        "Color": "#FFFFFFFF",
    },
}


def value_at(value: object, frame: int = 0) -> float:
    """数値パラメータの、その時刻での値"""
    assert isinstance(value, AnimatedValue)
    return value.at(frame)


def still(amount: float) -> dict[str, Any]:
    """動かないアニメーション 実物はこの形で 1 個だけ持つ"""
    return {"Values": [{"Value": amount}], "Span": 0.0, "AnimationType": "なし"}


def moving(*amounts: float, style: str = "直線移動") -> dict[str, Any]:
    return {"Values": [{"Value": a} for a in amounts], "Span": 0.0, "AnimationType": style}


def text_item(**fields: Any) -> dict[str, Any]:
    """実物の ``TextItem`` と同じ形"""
    base: dict[str, Any] = {
        "$type": "YukkuriMovieMaker.Project.Items.TextItem, YukkuriMovieMaker",
        "Text": "サンプルテキスト",
        "Decorations": [],
        "Font": "Noto Sans JP Black",
        "FontSize": still(120.0),
        "LineHeight2": still(100.0),
        "LetterSpacing2": still(0.0),
        "BasePoint": "CenterCenter",
        "FontColor": "#FF2B9FE2",
        "Style": "Normal",
        "StyleColor": "#FF000000",
        "Bold": False,
        "Italic": False,
        "X": still(0.0),
        "Y": still(0.0),
        "Opacity": still(100.0),
        "Zoom": still(100.0),
        "Rotation": still(0.0),
        "Blend": "Normal",
        "VideoEffects": [],
        "Frame": 0,
        "Layer": 4,
        "KeyFrames": {"Frames": [], "Count": 0},
        "Length": 300,
    }
    base.update(fields)
    return base


def group_item(**fields: Any) -> dict[str, Any]:
    """実物の ``GroupItem`` と同じ形 既定では :func:`text_item`（レイヤー 4）を範囲に含む

    グループが掛かるのは**自分より大きい番号**のレイヤー（YMM4 の画面では下の段）
    実物の配布物はどれもこの並び
    """
    base: dict[str, Any] = {
        "$type": "YukkuriMovieMaker.Project.Items.GroupItem, YukkuriMovieMaker",
        "GroupRange": 1,
        "X": still(0.0),
        "Y": still(0.0),
        "Zoom": still(100.0),
        "Rotation": still(0.0),
        "Opacity": still(100.0),
        "Blend": "Normal",
        "VideoEffects": [],
        "Frame": 0,
        "Layer": 3,
        "KeyFrames": {"Frames": [], "Count": 0},
        "Length": 300,
    }
    base.update(fields)
    return base


def shape_item(**fields: Any) -> dict[str, Any]:
    """実物の ``ShapeItem`` と同じ形 四角"""
    base: dict[str, Any] = {
        "$type": "YukkuriMovieMaker.Project.Items.ShapeItem, YukkuriMovieMaker",
        "ShapeType2": "YukkuriMovieMaker.Shape.QuadrilateralShapePlugin, YukkuriMovieMaker",
        "ShapeParameter": {
            "$type": "YukkuriMovieMaker.Project.Items.RectangleShapeParameter, YukkuriMovieMaker",
            "SizeMode": "WidthHeight",
            "Size": still(100.0),
            "AspectRate": still(0.0),
            "Width": still(200.0),
            "Height": still(100.0),
            "StrokeThickness": still(10000.0),
            "Brush": BRUSH,
        },
        "X": still(0.0),
        "Y": still(0.0),
        "Opacity": still(100.0),
        "Zoom": still(100.0),
        "Rotation": still(0.0),
        "Blend": "Normal",
        "VideoEffects": [],
        "Frame": 0,
        "Layer": 4,
        "KeyFrames": {"Frames": [], "Count": 0},
        "Length": 300,
    }
    base.update(fields)
    return base


def outline(thickness: float = 7.3) -> dict[str, Any]:
    return {
        "$type": "YukkuriMovieMaker.Project.Effects.OutlineEffect, YukkuriMovieMaker",
        "StrokeThickness": still(thickness),
        "Blur": still(0.0),
        "StrokeBrush": BRUSH,
        "IsEnabled": True,
    }


def write_ymmt(path: Path, *templates: dict[str, Any]) -> Path:
    """実物と同じ ZIP + ``catalog.json`` の形で書き出す"""
    catalog = {
        "FilePath": str(path),
        "ItemTemplates": list(templates),
        "VideoEffectTemplates": [],
        "AudioEffectTemplates": [],
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("catalog.json", json.dumps(catalog, ensure_ascii=False))
    return path


def template(name: str, *items: dict[str, Any], path: list[str] | None = None) -> dict[str, Any]:
    return {
        "Name": name,
        "Path": path if path is not None else name.split("/"),
        "Group": None,
        "KeyGesture": {},
        "Items": list(items),
    }


class TestTheContainer:
    def test_a_ymmt_is_a_zip(self, tmp_path: Path) -> None:
        # 素の JSON だと思って開くと、1 バイト目から落ちる
        path = write_ymmt(tmp_path / "束.ymmt", template("見出し", text_item()))
        assert zipfile.is_zipfile(path)
        assert len(load_template(path)) == 1

    def test_one_file_holds_many_templates(self, tmp_path: Path) -> None:
        # 実物は 1 ファイルに 17 本、106 本と入っていた
        path = write_ymmt(
            tmp_path / "束.ymmt",
            template("あ", text_item()),
            template("い", text_item()),
            template("う", text_item()),
        )
        assert [t.name for t in load_template(path)] == ["あ", "い", "う"]

    def test_the_classification_comes_from_path(self, tmp_path: Path) -> None:
        path = write_ymmt(tmp_path / "束.ymmt", template("アニメーション効果/振り子", text_item()))
        loaded = load_template(path)[0]
        assert loaded.path == ("アニメーション効果", "振り子")
        assert loaded.folder == "アニメーション効果"

    def test_a_bare_json_still_works(self, tmp_path: Path) -> None:
        # 古い書き方、あるいは手で書いたもの
        path = tmp_path / "素.ymmt"
        path.write_text(json.dumps(text_item()), "utf-8")
        assert len(load_template(path)[0].items) == 1

    def test_broken_json_says_so(self, tmp_path: Path) -> None:
        path = tmp_path / "壊れ.ymmt"
        path.write_text("{これは JSON ではない", "utf-8")
        with pytest.raises(Ymm4ParseError):
            load_template(path)


class TestTypeNames:
    def test_only_the_class_name_is_used(self) -> None:
        # 実物には Version も Culture も PublicKeyToken も入っている
        # 丸ごと突き合わせると、YMM4 が更新されただけで全部読めなくなる
        raw = "YukkuriMovieMaker.Project.Items.TextItem, YukkuriMovieMaker, Version=4.32.0.2"
        assert type_name({"$type": raw}) == "TextItem"

    def test_a_missing_type_is_empty(self) -> None:
        assert type_name({"Text": "x"}) == ""
        assert type_name("文字列") == ""


class TestAnimationPositions:
    """値の並びがどのフレームに置かれるか ここが今回いちばん効いた"""

    def test_the_values_carry_no_frame_number(self) -> None:
        # 実物の Values は {"Value": …} だけ 番号を探しても無い
        assert "Frame" not in moving(0.0, 100.0)["Values"][0]

    def test_positions_come_from_the_middle_points(self) -> None:
        # 中間点が 60 と 240、長さ 300 なら、区切りは 0/60/240/300 の 4 点
        assert frame_positions({"Frames": [60, 240], "Count": 2}, 300, 4) == [0, 60, 240, 300]

    def test_without_middle_points_it_spans_the_whole_item(self) -> None:
        assert frame_positions({"Frames": [], "Count": 0}, 300, 2) == [0, 300]

    def test_a_mismatch_falls_back_to_even_spacing(self) -> None:
        # 中間点の数と値の数が食い違っても、始点と終点は合わせる
        positions = frame_positions({"Frames": [60], "Count": 1}, 300, 4)
        assert positions[0] == 0
        assert positions[-1] == 300

    def test_the_animation_uses_the_whole_length(self) -> None:
        # 並び順をフレーム番号だと思うと、300 フレームの動きが 2 フレームで終わる
        value = animated(moving(0.0, 100.0), length=300, keyframes={"Frames": [], "Count": 0})
        assert [k.frame for k in value.keyframes] == [0, 300]
        assert value.at(150) == pytest.approx(50.0)

    def test_a_single_value_is_static(self) -> None:
        value = animated(still(42.0), length=300)
        assert value.is_animated is False
        assert value.at(0) == 42.0


class TestInterpolationNames:
    def test_japanese_names(self) -> None:
        assert interpolation_of("直線移動") is Interpolation.LINEAR
        assert interpolation_of("瞬間移動") is Interpolation.HOLD

    def test_english_easing_names(self) -> None:
        # 実物には Expo_Out / Sine_In / Quart_InOut のような名前が入っていた
        assert interpolation_of("Expo_Out") is Interpolation.EASE_OUT
        assert interpolation_of("Sine_In") is Interpolation.EASE_IN
        assert interpolation_of("Quart_InOut") is Interpolation.EASE_IN_OUT

    def test_an_unknown_name_falls_back_to_a_straight_line(self) -> None:
        # 動きの形は違っても、始点と終点は合う 止めるより良い
        assert interpolation_of("知らない曲線") is Interpolation.LINEAR

    def test_the_shape_of_an_english_easing_is_kept(self) -> None:
        """``Back_InOut`` は向き（InOut）だけでなく形（Back）も持つ

        形を捨てると、行き過ぎて戻る動きが 3 種の加減速に丸まり、Back_InOut で回る
        ローテンショントランジションが YMM4 と最大 30 度ずれた
        """
        assert curve_of("Back_InOut") == "back"
        assert curve_of("Expo_Out") == "expo"
        # Jump も形 瞬間移動（終わりで行き着く）に丸めると、Jump_Out が頭で行き着かない
        assert curve_of("Jump_Out") == "jump"
        # 日本語の移動方法は形を持たない
        assert curve_of("加減速") == ""
        value = animated(moving(0.0, 180.0, style="Back_InOut"), length=90)
        assert value.keyframes[0].curve == "back"
        assert value.keyframes[0].interpolation is Interpolation.EASE_IN_OUT
        # Back_InOut は頭で逆へ振れる（YMM4 の書き出しでも 6 度ほど逆へ回った）
        assert value.at(15) < 0.0

    def test_jump_out_arrives_at_the_start_not_the_end(self) -> None:
        # Jump は向きで行き着く時刻が変わる In は終わり、Out は頭、InOut は真ん中
        # 瞬間移動に丸めると、Jump_Out の動きが終わりまで止まったままになる
        out = animated(moving(0.0, 100.0, style="Jump_Out"), length=30)
        assert out.at(1) == pytest.approx(100.0)
        into = animated(moving(0.0, 100.0, style="Jump_In"), length=30)
        assert into.at(29) == pytest.approx(0.0)
        middle = animated(moving(0.0, 100.0, style="Jump_InOut"), length=30)
        assert middle.at(10) == pytest.approx(0.0)
        assert middle.at(20) == pytest.approx(100.0)

    def test_an_unknown_shape_goes_to_the_report_of_the_template_being_read(self) -> None:
        # 値を読む所の多くは記録を受け取らない 読み込みの入口の記録へ書かないと、
        # テンプレートを読んだ人の見る一覧に出ず、アプリ全体の記録へ紛れる
        report = CompatibilityReport()
        map_template([text_item(X=moving(0.0, 10.0, style="Magic_Out"))], report=report)
        assert any("Magic" in line for line in report.lines())

    def test_an_unknown_easing_shape_is_recorded_before_it_is_rounded(self) -> None:
        # 知らない形は向きだけの加減速で描く 記録しないと、YMM4 が形を足したときに
        # 動きが違うことに誰も気付けない 知っている形（Linear など）は記録しない
        report = CompatibilityReport()
        value = animated(moving(0.0, 1.0, style="Magic_In"), length=30, report=report)
        assert value.keyframes[0].curve == ""
        assert value.keyframes[0].interpolation is Interpolation.EASE_IN
        assert any("Magic" in line for line in report.lines())
        quiet = CompatibilityReport()
        animated(moving(0.0, 1.0, style="Linear_In"), length=30, report=quiet)
        assert not quiet.lines()


class TestValues:
    def test_a_bare_number(self) -> None:
        assert number(60) == 60.0

    def test_an_animation_reads_as_its_first_value(self) -> None:
        assert number(still(60.0)) == 60.0

    def test_alpha_comes_first_in_a_colour(self) -> None:
        # YMM4 は #AARRGGBB 後ろだと思って読むと、不透明のつもりが透明になる
        assert colour("#80FF0000") == pytest.approx((1.0, 0.0, 0.0, 128 / 255))

    def test_a_colour_without_alpha_is_opaque(self) -> None:
        assert colour("#00FF00") == pytest.approx((0.0, 1.0, 0.0, 1.0))

    def test_a_brush_hides_its_colour_one_level_down(self) -> None:
        assert brush_colour(BRUSH) == pytest.approx((1.0, 1.0, 1.0, 1.0))

    def test_a_brush_that_is_not_a_single_colour_keeps_the_default(self) -> None:
        # 格子やノイズのブラシは色 1 つで表せない
        grid = {"Type": "…GridLineBrushPlugin", "Parameter": {"$type": "…GridLineBrushParameter"}}
        assert brush_colour(grid, (0.0, 0.0, 0.0, 1.0)) == (0.0, 0.0, 0.0, 1.0)


class TestTextFields:
    def mapped(self, **fields: Any) -> Any:
        source = map_template([text_item(**fields)], report=CompatibilityReport())[0].clip.source
        assert source is not None
        return source

    def test_the_field_is_bold_not_is_bold(self) -> None:
        # 実物のキーは Bold / Italic IsBold では永久に太字にならない
        assert self.mapped(Bold=True).params["bold"] is True
        assert self.mapped(Italic=True).params["italic"] is True

    def test_the_font_and_colour(self) -> None:
        source = self.mapped()
        assert source.params["font"] == "Noto Sans JP Black"
        assert source.params["color"] == pytest.approx((0x2B / 255, 0x9F / 255, 0xE2 / 255, 1.0))

    def test_the_line_height_is_a_percentage(self) -> None:
        # LineHeight2 は 100 が標準 画素数だと思って渡すと、標準のつもりが
        # 100px の行間になる
        assert value_at(self.mapped().params["line_spacing"]) == 0.0
        wide = self.mapped(LineHeight2=still(150.0))
        assert value_at(wide.params["line_spacing"]) == pytest.approx(120.0 * 0.5)

    def test_the_base_point_splits_into_two(self) -> None:
        source = self.mapped(BasePoint="LeftBottom")
        assert (source.params["align"], source.params["valign"]) == ("left", "bottom")

    def test_the_default_base_point(self) -> None:
        source = self.mapped()
        assert (source.params["align"], source.params["valign"]) == ("center", "middle")

    def test_the_style_becomes_a_decoration(self) -> None:
        source = self.mapped(Style="ThickBorder", StyleColor="#FF112233")
        assert value_at(source.params["border_width"]) > 0
        assert source.params["border_color"] == pytest.approx(
            (0x11 / 255, 0x22 / 255, 0x33 / 255, 1.0)
        )

    def test_an_unknown_style_is_recorded(self) -> None:
        report = CompatibilityReport()
        map_template([text_item(Style="キラキラ")], report=report)
        assert any("キラキラ" in line for line in report.lines())


class TestVideoEffects:
    """実物の飾りは ``Decorations`` ではなくここに入っていた"""

    def test_the_outline_effect_becomes_the_text_border(self) -> None:
        item = text_item(VideoEffects=[outline(7.3)])
        source = map_template([item], report=CompatibilityReport())[0].clip.source
        assert source is not None
        assert value_at(source.params["border_width"]) == pytest.approx(7.3)
        assert source.params["border_color"] == pytest.approx((1.0, 1.0, 1.0, 1.0))

    def test_two_outlines_stack(self) -> None:
        item = text_item(VideoEffects=[outline(4.0), outline(12.0)])
        mapped = map_template([item], report=CompatibilityReport())[0]
        source = mapped.clip.source
        assert source is not None
        # 太いほうを文字に、細いほうをエフェクトとして外側に積む
        assert value_at(source.params["border_width"]) == pytest.approx(12.0)
        assert [e.kind for e in mapped.clip.effects] == ["border"]

    def test_a_disabled_effect_is_skipped(self) -> None:
        effect = outline(9.0)
        effect["IsEnabled"] = False
        item = text_item(VideoEffects=[effect])
        source = map_template([item], report=CompatibilityReport())[0].clip.source
        assert source is not None
        assert "border_width" not in source.params

    def test_the_zoom_effect_keeps_moving(self) -> None:
        # 素の数で読むと、登場アニメーションが止まったまま出る
        effect = {
            "$type": "YukkuriMovieMaker.Project.Effects.ZoomEffect, YukkuriMovieMaker",
            "Zoom": moving(0.0, 100.0, style="Expo_Out"),
            "ZoomY": still(100.0),
            "IsEnabled": True,
        }
        result = map_video_effects(
            [effect], CompatibilityReport(), length=300, keyframes={"Frames": [], "Count": 0}
        )
        scale = result.effects[0].params["scale"]
        assert isinstance(scale, AnimatedValue)
        assert [k.frame for k in scale.keyframes] == [0, 300]
        assert scale.keyframes[0].interpolation is Interpolation.EASE_OUT

    def test_the_rotate_effect_uses_the_z_axis(self) -> None:
        effect = {
            "$type": "YukkuriMovieMaker.Project.Effects.RotateEffect, YukkuriMovieMaker",
            "X": still(0.0),
            "Y": still(0.0),
            "Z": still(60.0),
            "IsEnabled": True,
        }
        result = map_video_effects([effect], CompatibilityReport(), length=300)
        assert value_at(result.effects[0].params["rotation"]) == 60.0

    def test_the_tilt_axes_are_reversed(self) -> None:
        # YMM4 の X が正だと上の辺が手前へ来る Sashimono の X 軸の正は上の辺が奥へ倒れる
        effect = {
            "$type": "YukkuriMovieMaker.Project.Effects.RotateEffect, YukkuriMovieMaker",
            "X": still(30.0),
            "Y": still(-20.0),
            "Z": still(0.0),
            "IsEnabled": True,
        }
        result = map_video_effects([effect], CompatibilityReport(), length=300)
        assert value_at(result.effects[0].params["rotation_x"]) == -30.0
        assert value_at(result.effects[0].params["rotation_y"]) == 20.0

    @pytest.mark.parametrize(
        ("name", "fields", "kind", "expected"),
        [
            ("InOutFadeEffect", {"Value": 20.0}, "inout_fade", {"opacity": 20.0}),
            (
                "InOutRotateEffect",
                {"ValueX": 90.0, "ValueY": 0.0, "ValueZ": 45.0, "Is3D": True},
                "inout_rotate",
                {"angle_x": -90.0, "angle_z": 45.0, "three_d": True},
            ),
            (
                "InOutMoveEffect",
                {"Value": 600.0, "Value2": 90.0, "Value3": 0.0},
                "inout_offset",
                {"offset_x": 600.0, "offset_y": -90.0},
            ),
            (
                "InOutSkewEffect",
                {"AngleX": 30.0, "AngleY": 10.0, "CenterPoint": "Center"},
                "inout_skew",
                {"angle_x": 30.0, "angle_y": -10.0},
            ),
            ("InOutGaussianBlurEffect", {"Value": 20.0}, "inout_blur", {"radius": 20.0}),
        ],
    )
    def test_the_in_out_effects(
        self, name: str, fields: dict[str, Any], kind: str, expected: dict[str, Any]
    ) -> None:
        effect = {
            "$type": f"YukkuriMovieMaker.Project.Effects.{name}, YukkuriMovieMaker",
            "IsInEffect": True,
            "IsOutEffect": True,
            "EffectTimeSeconds": 1.5,
            "EasingType": "Linear",
            "EasingMode": "In",
            "IsEnabled": True,
            **fields,
        }
        report = CompatibilityReport()
        result = map_video_effects([effect], report, length=300)
        assert not report.lines()
        (mapped,) = result.effects
        assert mapped.kind == kind
        assert value_at(mapped.params["effect_time"]) == 1.5
        assert mapped.params["effect_out"] is True
        for key, value in expected.items():
            actual = mapped.params[key]
            assert (actual if isinstance(value, bool) else value_at(actual)) == value

    def test_the_halftone_inner_shadow(self) -> None:
        # 色はブラシでなく Color に直に入る Y は下が正
        effect = {
            "$type": "N.InnerHalfToneShadowEffect, YukkuriMovieMaker",
            "X": -12.0,
            "Y": 5.0,
            "Opacity": 100.0,
            "Blur": 20.0,
            "BlendMode": "PinLight",
            "Layout": "Rhombus",
            "Distance": 7.0,
            "Size": 100.0,
            "Color": "#FFFF0000",
            "Strength": 100.0,
            "IsEnabled": True,
        }
        report = CompatibilityReport()
        (mapped,) = map_video_effects([effect], report, length=60).effects
        assert not report.lines()
        assert mapped.kind == "inner_halftone"
        assert value_at(mapped.params["offset_y"]) == -5.0
        assert mapped.params["color"] == (1.0, 0.0, 0.0, 1.0)
        assert mapped.params["blend"] == "pin_light"
        assert value_at(mapped.params["spacing"]) == 7.0

    def test_the_inner_outline(self) -> None:
        effect = {
            "$type": "N.InnerOutline.InnerOutlineEffect, YukkuriMovieMaker.Plugin.Community",
            "Thickness": 3.0,
            "Opacity": 100.0,
            "Blur": 2.5,
            "Blend": "Normal",
            "IsOutlineOnly": True,
            "IsAngular": False,
            "Brush": BRUSH,
            "IsEnabled": True,
        }
        (mapped,) = map_video_effects([effect], CompatibilityReport(), length=60).effects
        assert mapped.kind == "inner_outline"
        assert value_at(mapped.params["thickness"]) == 3.0
        assert mapped.params["outline_only"] is True

    def test_the_fill_effect_takes_its_colour_from_the_brush(self) -> None:
        # ブラシの模様・合成モード・濃さを 1 つの塗りに写す（色だけの塗りでは合成が消える）
        effect = {
            "$type": "YukkuriMovieMaker.Project.Effects.FillForegroundEffect, YukkuriMovieMaker",
            "Opacity": still(50.0),
            "BlendMode": "Multiply",
            "Brush": BRUSH,
            "IsEnabled": True,
        }
        result = map_video_effects([effect], CompatibilityReport(), length=300)
        assert result.effects[0].kind == "brush_fill"
        assert result.effects[0].params["blend"] == "multiply"
        assert value_at(result.effects[0].params["opacity"]) == 50.0

    def test_the_colour_correction_is_re_centred(self) -> None:
        # YMM4 は 100 が「変化なし」 こちらは 0 が変化なし
        effect = {
            "$type": "YukkuriMovieMaker.Project.Effects.ColorCorrectionEffect, YukkuriMovieMaker",
            "Lightness": still(110.0),
            "Contrast": still(130.0),
            "Saturation": still(100.0),
            "IsEnabled": True,
        }
        result = map_video_effects([effect], CompatibilityReport(), length=300)
        params = result.effects[0].params
        assert value_at(params["brightness"]) == pytest.approx(10.0)
        assert value_at(params["contrast"]) == pytest.approx(30.0)
        assert value_at(params["saturation"]) == pytest.approx(0.0)

    def test_the_colour_correction_brightness_is_read(self) -> None:
        """輝度（Brightness）は明るさ（Lightness）と別の項目で、動かせる

        読まずにいると、明るく飛ばしてから戻す場面切り替え（ペイントトランジション）の
        真ん中が YMM4 より 30 ほど暗く出た
        """
        effect = {
            "$type": "YukkuriMovieMaker.Project.Effects.ColorCorrectionEffect, YukkuriMovieMaker",
            "Lightness": still(100.0),
            "Brightness": moving(150.0, 100.0, style="Sine_Out"),
            "IsEnabled": True,
        }
        result = map_video_effects([effect], CompatibilityReport(), length=60)
        gain = result.effects[0].params["gain"]
        assert isinstance(gain, AnimatedValue)
        assert gain.at(0) == pytest.approx(150.0)
        assert gain.at(60) == pytest.approx(100.0)
        assert value_at(result.effects[0].params["brightness"]) == pytest.approx(0.0)

    def test_a_custom_centre_point_is_measured_from_the_origin(self) -> None:
        """中心点の「任意」は、絵の原点から X と Y だけずらした点

        絵の中央から取ると、場面切り替えの場面（原点は画面の中央、中身は右へ寄った図形）で
        支点が図形の分だけずれ、ページめくり風その2 の後の場面が 200 画素ずれて回った
        """
        centre = {
            "$type": "YukkuriMovieMaker.Project.Effects.CenterPointEffect, YukkuriMovieMaker",
            "Horizontal": "Custom",
            "Vertical": "Custom",
            "X": still(680.0),
            "Y": still(-1722.9),
            "IsKeepPosition": True,
            "IsEnabled": True,
        }
        spin = {
            "$type": "YukkuriMovieMaker.Project.Effects.RotateEffect, YukkuriMovieMaker",
            "Z": moving(-50.0, 0.0),
            "IsEnabled": True,
        }
        (mapped,) = map_video_effects([centre, spin], CompatibilityReport(), length=30).effects
        assert mapped.params["pivot_h"] == "origin"
        assert mapped.params["pivot_v"] == "origin"
        assert value_at(mapped.params["anchor_x"]) == pytest.approx(680.0)
        # YMM4 の Y は下が正 こちらは上が正
        assert value_at(mapped.params["anchor_y"]) == pytest.approx(1722.9)

    def test_random_move_flips_y(self) -> None:
        # YMM4 は下が正 そのまま渡すと上下の揺れの向きが逆になる
        effect = {
            "$type": "YukkuriMovieMaker.Project.Effects.RandomMoveEffect, YukkuriMovieMaker",
            "X": still(10.0),
            "Y": still(20.0),
            "IsEnabled": True,
        }
        result = map_video_effects([effect], CompatibilityReport(), length=30)
        assert value_at(result.effects[0].params["range_y"]) == -20.0

    def test_the_pivot_reaches_the_effects_behind_it(self) -> None:
        # 中心点は後ろの回転や拡大の支点になる 渡らないと絵の中央で回る
        report = CompatibilityReport()
        centre = {
            "$type": "YukkuriMovieMaker.Project.Effects.CenterPointEffect, YukkuriMovieMaker",
            "Horizontal": "Left",
            "IsEnabled": True,
        }
        spin = {
            "$type": "YukkuriMovieMaker.Project.Effects.RepeatRotateEffect, YukkuriMovieMaker",
            "IsEnabled": True,
        }
        (mapped,) = map_video_effects([centre, spin], report, length=30).effects
        assert not report.lines()
        assert mapped.params["pivot_h"] == "left"

    def test_a_count_that_is_not_a_number_is_recorded(self) -> None:
        # int(NaN) の例外で、同じアイテムの後ろのエフェクトまで読めなくなる
        report = CompatibilityReport()
        duplicate = {
            "$type": "YukkuriMovieMaker.Project.Effects.CircularDuplicatorEffect, A",
            "Count": still(float("nan")),
            "IsEnabled": True,
        }
        result = map_video_effects([duplicate], report, length=30)
        assert result.effects[0].params["count"] == 8
        assert any("Count" in line for line in report.lines())

    def test_an_unknown_effect_is_recorded_not_dropped(self) -> None:
        report = CompatibilityReport()
        map_video_effects(
            [{"$type": "N.MeshDeformationEffect, A", "IsEnabled": True}], report, length=1
        )
        assert any("MeshDeformationEffect" in line for line in report.lines())


class TestGroups:
    def test_a_group_moves_its_effects_onto_the_content(self) -> None:
        # GroupItem は入れ物で、それ自体は絵を持たない こちらに入れ子は無いので
        # 中身へ移して平らにする
        group = group_item(VideoEffects=[outline(6.0)])
        mapped = map_template([text_item(), group], report=CompatibilityReport())
        assert len(mapped) == 1
        assert [e.kind for e in mapped[0].clip.effects] == ["border"]

    def test_a_group_on_its_own_becomes_an_effects_only_template(self) -> None:
        # 「アニメーション効果/振り子」のような、中身を持たないテンプレート
        group = group_item(Rotation=moving(0.0, 30.0))
        mapped = map_template([group], report=CompatibilityReport())
        assert len(mapped) == 1
        assert mapped[0].clip.source is None
        assert mapped[0].kind == "effects"
        assert [e.kind for e in mapped[0].clip.effects] == ["transform"]

    def test_an_effects_only_template_keeps_the_group_length(self) -> None:
        """中身が無くても**入れ物の長さ**を残す

        動く値のキーフレームはこの長さの上に並んでいる 1 にしてしまうと、
        着せるときに尺を合わせられず、動きが着せた先の途中で止まる
        """
        group = group_item(Rotation=moving(0.0, 30.0), Length=300)
        mapped = map_template([group], report=CompatibilityReport())
        assert mapped[0].clip.duration == 300
        # 長さを持っていないことは変わらない 置くときは既定の長さを使う
        assert not mapped[0].has_span

    def test_a_shorter_group_is_stretched_onto_the_content(self) -> None:
        """入れ物と中身で長さが違うとき、動きを**中身の長さへ揃えて**移す

        揃えずに移すと、入れ物の終わり（18）に置いた点が 300 フレームの
        中身の先頭近くに残り、エフェクトの終わりの見た目が出ないまま止まる
        手元の配布物 97 本のうち 6 本がこの形（例: 入れ物 18 中身 300）
        """
        group = group_item(Rotation=moving(0.0, 30.0), Length=18)
        mapped = map_template([text_item(Length=300), group], report=CompatibilityReport())
        assert len(mapped) == 1
        transform = next(e for e in mapped[0].clip.effects if e.kind == "transform")
        value = transform.params["rotation"]
        assert isinstance(value, AnimatedValue)
        assert [k.frame for k in value.keyframes] == [0, 300]

    def test_a_content_without_a_length_borrows_the_group_length(self) -> None:
        """中身が長さを持たないときは、**入れ物の長さ**を借りる

        長さを 1 として揃えると 90 フレームの動きが 2 フレームに潰れ、
        置くときに既定の長さまで伸ばされても動きは戻らない
        """
        content = text_item()
        content.pop("Length")
        group = group_item(Rotation=moving(0.0, 30.0), Length=90)
        mapped = map_template([content, group], report=CompatibilityReport())
        assert len(mapped) == 1
        assert mapped[0].clip.duration == 90
        assert mapped[0].has_span, "長さが分かったのに、置くときに既定の長さを使ってしまう"
        transform = next(e for e in mapped[0].clip.effects if e.kind == "transform")
        value = transform.params["rotation"]
        assert isinstance(value, AnimatedValue)
        assert [k.frame for k in value.keyframes] == [0, 90]

    def test_groups_of_different_lengths_stay_apart(self) -> None:
        """長さの違う入れ物は**別々に**返す

        1 つにまとめると長さが 1 つしか持てず、短い方の動きが長い方の尺で
        伸び縮みして、着せたときに違う時刻へ着く
        """
        short = group_item(Rotation=moving(0.0, 30.0), Length=60)
        long = group_item(Zoom=moving(100.0, 200.0), Length=300)
        mapped = map_template([short, long], report=CompatibilityReport())
        assert sorted(item.clip.duration for item in mapped) == [60, 300]

    def test_a_group_with_nothing_to_give_produces_nothing(self) -> None:
        assert map_template([group_item()], report=CompatibilityReport()) == []

    def test_an_item_outside_the_range_gets_nothing_from_the_group(self) -> None:
        """グループのエフェクトは ``GroupRange`` の段の中身にだけ掛かる

        全部へ配ると、リボンのテロップ（配布物）の文字が範囲の外なのに吹き出しの
        登場の動きをもう 1 度受け、自分の動きと合わせて倍の距離を飛んでくる
        """
        group = group_item(Layer=0, GroupRange=2, VideoEffects=[outline(6.0)])
        inside = shape_item(Layer=2)
        outside = text_item(Layer=3)
        mapped = map_template([group, inside, outside], report=CompatibilityReport())
        kinds = {item.kind: [e.kind for e in item.clip.effects] for item in mapped}
        assert kinds == {"shape": ["border"], "text": []}


class TestCompositeGroups:
    """「合成する」（``IsComposite``）グループ 範囲の中身を 1 枚の絵にしてから掛ける"""

    def test_the_members_are_gathered_into_one_picture(self) -> None:
        """合成するグループは、範囲の中身を子に持つ 1 つのオブジェクトになる

        中身を 1 つずつ置くと、グループの反転や拡大が中身ごとの中心で掛かる
        SFっぽい吹き出し（配布物）は、反転と縮小が画面の中心で掛かって吹き出しが
        画面の左へ寄る絵だった
        """
        group = group_item(Layer=0, GroupRange=2, IsComposite=True, IsInverted=True)
        mapped = map_template(
            [group, shape_item(Layer=1), text_item(Layer=2)], report=CompatibilityReport()
        )
        assert len(mapped) == 1
        scene = mapped[0]
        assert scene.kind == "scene"
        assert scene.has_picture
        assert sorted(child.kind for child in scene.children) == ["shape", "text"]
        assert [e.kind for e in scene.clip.effects] == ["flip"]

    def test_the_group_effects_are_not_copied_onto_each_member(self) -> None:
        # 移すと縁取りが中身 1 つずつの外側に付き、重なった所に内側の線が出る
        group = group_item(Layer=0, GroupRange=2, IsComposite=True, VideoEffects=[outline(6.0)])
        mapped = map_template(
            [group, shape_item(Layer=1), shape_item(Layer=2)], report=CompatibilityReport()
        )
        assert [e.kind for e in mapped[0].clip.effects] == ["border"]
        assert all(child.clip.effects == () for child in mapped[0].children)

    def test_the_members_are_placed_relative_to_the_group(self) -> None:
        """中身の段と時刻はグループからの相対になる

        そのままの段で置くと、シーンの下の段が空のトラックで埋まる 時刻をずらさないと、
        グループが 30 フレーム目から始まるとき中身がシーンの中で 30 フレーム遅れて出る
        """
        group = group_item(Layer=2, GroupRange=3, IsComposite=True, Frame=30, Length=100)
        member = shape_item(Layer=4, Frame=40, Length=50)
        scene = map_template([group, member], report=CompatibilityReport())[0]
        assert (scene.layer, scene.clip.timeline_start, scene.clip.duration) == (3, 30, 100)
        child = scene.children[0]
        assert (child.layer, child.clip.timeline_start) == (2, 10)

    def test_opacity_blend_and_clipping_go_to_the_picture(self) -> None:
        # どれもまとめた絵に掛かる 捨てると、半透明の吹き出しが不透明のまま出る
        group = group_item(
            Layer=0,
            GroupRange=1,
            IsComposite=True,
            Opacity=still(40.0),
            Blend="Multiply",
            IsClippingWithObjectAbove=True,
        )
        clip = map_template([group, shape_item(Layer=1)], report=CompatibilityReport())[0].clip
        assert clip.opacity.at(0) == pytest.approx(0.4)
        assert clip.blend_mode == "multiply"
        assert clip.clip_to_below

    def test_an_item_above_the_range_stays_outside(self) -> None:
        # 範囲の外の文字までまとめると、グループの縁取りや登場の動きが文字にも掛かる
        # リボンのテロップ（配布物）では、自分の動きを持つ文字が倍の距離を飛んでくる
        group = group_item(Layer=0, GroupRange=1, IsComposite=True)
        mapped = map_template(
            [group, shape_item(Layer=1), text_item(Layer=2)], report=CompatibilityReport()
        )
        assert sorted(item.kind for item in mapped) == ["scene", "text"]

    def test_an_empty_composite_group_adds_no_scene(self) -> None:
        """範囲に中身の無い合成するグループは、何も置かない

        ペイントトランジション（配布物）は「この範囲に次の場面を置いてください」という
        空の枠を持つ 空のシーンを置くと、何も映らないトラックとシーンが増えるだけ
        """
        group = group_item(Layer=5, GroupRange=3, IsComposite=True)
        mapped = map_template([shape_item(Layer=0), group], report=CompatibilityReport())
        assert [item.kind for item in mapped] == ["shape"]

    def test_a_group_inside_a_composite_group_works_inside_the_picture(self) -> None:
        """合成するグループの中の合成しないグループは、まとめた絵の中で中身へ配る

        リボンのテロップ（配布物）の形 内側のグループの縁取りが外へ漏れると、
        まとめた絵の外側にもう 1 本縁取りが付く
        """
        outer = group_item(Layer=0, GroupRange=3, IsComposite=True)
        inner = group_item(Layer=1, GroupRange=1, VideoEffects=[outline(6.0)])
        mapped = map_template(
            [outer, inner, shape_item(Layer=2), shape_item(Layer=3)],
            report=CompatibilityReport(),
        )
        assert len(mapped) == 1
        assert mapped[0].clip.effects == ()
        effects = sorted(len(child.clip.effects) for child in mapped[0].children)
        assert effects == [0, 1]

    def test_a_nested_composite_group_becomes_a_scene_inside_the_scene(self) -> None:
        # 内側の合成するグループを平らにすると、内側の反転が中身 1 つずつの中心で掛かり、
        # 内側でまとめた絵ごと裏返るはずの形が別の配置になる
        outer = group_item(Layer=0, GroupRange=3, IsComposite=True)
        inner = group_item(Layer=1, GroupRange=2, IsComposite=True, IsInverted=True)
        mapped = map_template(
            [outer, inner, shape_item(Layer=2), shape_item(Layer=3)],
            report=CompatibilityReport(),
        )
        assert [child.kind for child in mapped[0].children] == ["scene"]
        assert len(mapped[0].children[0].children) == 2

    def test_placing_puts_the_members_into_a_scene(self) -> None:
        """置くと、中身はシーンの中へ、グループはそのシーンのクリップとして置かれる

        グループのエフェクトはシーンのクリップが持つ 中身を平らに置くと、まとめた絵に
        掛けるはずの反転が掛からないまま、中身だけが並ぶ
        """
        group = group_item(Layer=0, GroupRange=2, IsComposite=True, IsInverted=True, Frame=30)
        objects = map_template(
            [group, shape_item(Layer=1, Frame=30), text_item(Layer=2, Frame=45)],
            report=CompatibilityReport(),
        )
        project = Project.create()
        for command in place(objects, project, at_frame=100):
            project = command.apply(project)

        assert len(project.scenes) == 1
        scene = project.scenes[0]
        placed = [clip for track in project.timeline.tracks for clip in track.clips]
        assert len(placed) == 1
        assert placed[0].scene_id == scene.id
        assert placed[0].timeline_start == 100
        assert [e.kind for e in placed[0].effects] == ["flip"]
        inside = sorted(
            (clip.timeline_start, clip.source.kind if clip.source else "")
            for track in scene.timeline.tracks
            for clip in track.clips
        )
        # 遅れて出る中身は、シーンの中でも同じだけ遅れる
        assert inside == [(0, "shape"), (15, "text")]

    def test_restyling_finds_the_text_inside_the_picture(self) -> None:
        # 吹き出しの字幕テンプレートは文字がまとめた絵の中にある 上だけを見ると
        # 「文字の無いテンプレート」として着せられなくなる
        group = group_item(Layer=0, GroupRange=2, IsComposite=True)
        objects = map_template(
            [group, shape_item(Layer=1), text_item(Layer=2, Text="見本")],
            report=CompatibilityReport(),
        )
        clip = Clip(
            timeline_start=0,
            duration=60,
            source=GeneratedSource(kind="text", params={"text": "自分の字幕"}),
        )
        commands = restyle(objects, clip)
        sources = [command for command in commands if isinstance(command, SetSource)]
        assert len(sources) == 1
        restyled = sources[0].source
        assert restyled is not None
        assert restyled.params["text"] == "自分の字幕"

    @pytest.mark.usefixtures("gpu")
    def test_the_outline_goes_around_the_whole_picture(self) -> None:
        """縁取りは重ねた絵の外側にだけ付く

        白い四角 2 つを横にずらして重ね、グループに赤い縁取りを掛ける 中身 1 つずつに
        配ると、上の四角の縁が下の四角の上に赤い線として出る（リボンのテロップの
        吹き出しの中に線が走る）
        """
        from sashimono.core.model import ProjectSettings
        from sashimono.core.timebase import FrameRate
        from sashimono.engine.render import FrameRenderer

        red = outline(10.0)
        red["StrokeBrush"] = {
            "Type": BRUSH["Type"],
            "Parameter": {
                "$type": "YukkuriMovieMaker.Plugin.Brush.SolidColorBrushParameter, "
                "YukkuriMovieMaker",
                "Color": "#FFFF0000",
            },
        }
        group = group_item(Layer=0, GroupRange=2, IsComposite=True, VideoEffects=[red], Length=30)
        lower = shape_item(Layer=1, X=still(-50.0), Length=30)
        upper = shape_item(Layer=2, X=still(50.0), Length=30)
        objects = map_template([group, lower, upper], report=CompatibilityReport())
        project = Project.create(ProjectSettings(width=1920, height=1080, frame_rate=FrameRate(30)))
        for command in place(objects, project):
            project = command.apply(project)
        renderer = FrameRenderer(project)
        try:
            image = renderer.render(10)
        finally:
            renderer.close()
        # 上の四角の左の縁（画面の 910）のすぐ外は、下の四角の中
        red_part, green_part, _ = (int(v) for v in image[540, 905, :3])
        assert red_part > 200
        assert green_part > 200, "重なった所に縁取りの線が出た 縁取りが中身ごとに掛かっている"
        # 絵全体の外側にはちゃんと縁取りが付く（下の四角の左の縁は画面の 810）
        outside = image[540, 805, :3]
        assert int(outside[0]) > 200
        assert int(outside[1]) < 60

    def test_a_member_starting_before_the_group_keeps_its_elapsed_time(self) -> None:
        """グループより先に始まった中身は、グループが始まった時点の続きから映る

        グループの頭へ詰める（0 に丸める）と、20 フレーム先に始まっていた動きが
        グループの頭から描き直される シーンの頭を一番早い中身に合わせ、グループの
        クリップはシーンの 20 フレーム目から映す
        """
        group = group_item(Layer=0, GroupRange=1, IsComposite=True, Frame=50, Length=100)
        member = shape_item(Layer=1, Frame=30, Length=120)
        scene = map_template([group, member], report=CompatibilityReport())[0]
        assert scene.clip.timeline_start == 50
        assert scene.children[0].clip.timeline_start == 0
        assert scene.scene_offset == 20

        project = Project.create()
        for command in place([scene], project, at_frame=0):
            project = command.apply(project)
        placed = next(clip for track in project.timeline.tracks for clip in track.clips)
        assert placed.source_in == 20 * project.rate.frame_duration

    def test_a_member_outside_the_group_time_stays_outside(self) -> None:
        # グループが終わった後に始まるアイテムまでまとめると、シーンのクリップの
        # 長さで切られて消える
        group = group_item(Layer=0, GroupRange=2, IsComposite=True, Frame=0, Length=60)
        mapped = map_template(
            [group, shape_item(Layer=1, Length=60), text_item(Layer=2, Frame=90, Length=30)],
            report=CompatibilityReport(),
        )
        assert sorted(item.kind for item in mapped) == ["scene", "text"]

    def test_an_inner_group_reaching_past_the_outer_range_is_recorded(self) -> None:
        """合成するグループの範囲を越えて掛かる内側のグループは、記録に残す

        内側のグループはまとめた絵の中でしか働かないので、範囲の外の段（ここでは 3 段目）
        には何も掛からない YMM4 がどう描くかは確かめていないので、黙って捨てない
        """
        report = CompatibilityReport()
        outer = group_item(Layer=0, GroupRange=2, IsComposite=True)
        inner = group_item(Layer=1, GroupRange=2, VideoEffects=[outline(6.0)])
        map_template([outer, inner, shape_item(Layer=2), shape_item(Layer=3)], report=report)
        assert any("範囲を越える" in line for line in report.lines())

    def test_an_inner_group_without_a_range_is_recorded_too(self) -> None:
        # GroupRange の無いグループは上の段すべてに掛かる扱い 範囲のある外側の中に
        # 入ると必ず越えるのに、範囲が無いからと見逃すと記録から漏れる
        report = CompatibilityReport()
        outer = group_item(Layer=0, GroupRange=2, IsComposite=True)
        inner = group_item(Layer=1, VideoEffects=[outline(6.0)])
        inner.pop("GroupRange")
        map_template([outer, inner, shape_item(Layer=2)], report=report)
        assert any("範囲を越える" in line for line in report.lines())

    def test_an_inner_group_inside_the_range_is_not_recorded(self) -> None:
        # 範囲に収まる内側のグループまで記録すると、リボンのテロップのように正しく
        # 写せているテンプレートが互換性レポートで未対応として利用者に見える
        report = CompatibilityReport()
        outer = group_item(Layer=0, GroupRange=3, IsComposite=True)
        inner = group_item(Layer=1, GroupRange=2, VideoEffects=[outline(6.0)])
        map_template([outer, inner, shape_item(Layer=2), shape_item(Layer=3)], report=report)
        assert not any("範囲を越える" in line for line in report.lines())

    def test_another_composite_center_is_recorded(self) -> None:
        # 手元の配布物は画面の中心だけ ほかの中心は確かめていないので、黙って画面の
        # 中心で掛けずに記録へ残す
        report = CompatibilityReport()
        group = group_item(Layer=0, IsComposite=True, CompositeCenter="ItemCenter")
        map_template([group, shape_item(Layer=1)], report=report)
        assert any("ItemCenter" in line for line in report.lines())


class TestPlacement:
    def test_the_layer_shifts_by_one(self) -> None:
        # YMM4 のレイヤーは 0 始まり、こちらのトラックは 1 始まり
        assert map_template([text_item(Layer=2)], report=CompatibilityReport())[0].layer == 3

    def test_the_span_comes_from_frame_and_length(self) -> None:
        clip = map_template([text_item(Frame=30, Length=120)], report=CompatibilityReport())[0].clip
        assert (clip.timeline_start, clip.duration) == (30, 120)

    def test_the_opacity_is_a_percentage(self) -> None:
        clip = map_template([text_item(Opacity=still(40.0))], report=CompatibilityReport())[0].clip
        assert clip.opacity.at(0) == pytest.approx(0.4)

    def test_the_y_axis_is_flipped(self) -> None:
        mapped = map_template([text_item(Y=still(200.0))], report=CompatibilityReport())[0]
        assert mapped.clip.effects[0].kind == "transform"
        assert value_at(mapped.clip.effects[0].params["pos_y"]) == pytest.approx(-200.0)

    def test_a_still_item_adds_no_transform(self) -> None:
        assert map_template([text_item()], report=CompatibilityReport())[0].clip.effects == ()

    def test_an_unsupported_blend_is_recorded(self) -> None:
        report = CompatibilityReport()
        mapped = map_template([text_item(Blend="Lighten")], report=report)
        assert mapped[0].clip.blend_mode == "normal"
        assert any("Lighten" in line for line in report.lines())

    def test_the_photoshop_style_blends_are_kept(self) -> None:
        # 焼き込みカラーやハードミックスも、塗りのエフェクトと同じ名前で写す
        report = CompatibilityReport()
        mapped = map_template([text_item(Blend="HardMix")], report=report)
        assert mapped[0].clip.blend_mode == "hard_mix"
        assert not report.lines()

    def test_an_item_shown_only_in_the_preview_is_left_out(self) -> None:
        # YMM4 は書き出した動画に映さない 読み込むと目印が映り込む
        hidden = {
            "$type": "YukkuriMovieMaker.Project.Effects.ShowOnlyPreviewEffect, YukkuriMovieMaker",
            "IsEnabled": True,
        }
        report = CompatibilityReport()
        mapped = map_template([text_item(VideoEffects=[hidden]), text_item()], report=report)
        assert len(mapped) == 1
        assert not report.lines()


class TestOtherItems:
    def test_a_transition_splits_its_effects_between_the_scenes(self) -> None:
        rotate = {
            "$type": "YukkuriMovieMaker.Project.Effects.RotateEffect, YukkuriMovieMaker",
            "X": still(0.0),
            "Y": still(0.0),
            "Z": moving(0.0, 90.0),
            "IsEnabled": True,
        }
        item = {
            "$type": "YukkuriMovieMaker.Project.Items.TransitionItem, YukkuriMovieMaker",
            "TransitionType": "N.SlideTransitionPlugin, YukkuriMovieMaker",
            "TransitionParameter": {
                "Target": "Before",
                "Angle": 90.0,
                "EasingType": "Back",
                "EasingMode": "InOut",
            },
            "BeforeVideoEffects": [rotate],
            "AfterVideoEffects": [],
            "VideoEffects": [],
            "Frame": 30,
            "Length": 60,
            "Layer": 3,
        }
        report = CompatibilityReport()
        (mapped,) = map_template([item], report=report)
        assert not report.lines()
        clip = mapped.clip
        assert clip.source is not None and clip.source.kind == "transition"
        assert clip.source.params["style"] == "slide"
        assert clip.source.params["target"] == "before"
        assert clip.source.params["easing"] == "back"
        assert value_at(clip.source.params["angle"]) == 90.0
        assert [e.kind for e in clip.effects] == ["transform"]
        assert clip.after_effects == ()
        assert (clip.timeline_start, clip.duration, mapped.layer) == (30, 60, 4)

    def test_a_push_ignores_its_angle(self) -> None:
        # YMM4 は押し出しの角度を見ていなかった（90 にしても 0 と同じ絵）
        item = {
            "$type": "YukkuriMovieMaker.Project.Items.TransitionItem, YukkuriMovieMaker",
            "TransitionType": "N.PushTransitionPlugin, YukkuriMovieMaker",
            "TransitionParameter": {"Angle": 90.0},
            "Frame": 0,
            "Length": 30,
        }
        (mapped,) = map_template([item], report=CompatibilityReport())
        assert mapped.clip.source is not None
        assert value_at(mapped.clip.source.params["angle"]) == 0.0

    def test_a_media_item_returns_its_path(self) -> None:
        item = {
            "$type": "YukkuriMovieMaker.Project.Items.VideoItem, YukkuriMovieMaker",
            "FilePath": "C:/素材/映像.mp4",
            "Length": 60,
        }
        mapped = map_template([item], report=CompatibilityReport())
        assert mapped[0].media_path == "C:/素材/映像.mp4"


class TestTheContentOffset:
    """素材のどこから再生するか（``ContentOffset``）

    切り出して使っているテンプレートは、ここを落とすと**絵も音も違う所から始まる**
    実物（この機械の YMM4 プロジェクト 16 本）では、動画 214 個・音声 125 個のうち
    240 個が 0 以外だった 形は ``"00:09:24.1999999"`` ``"00:00:06"`` など
    """

    def video(self, offset: Any) -> dict[str, Any]:
        return {
            "$type": "YukkuriMovieMaker.Project.Items.VideoItem, YukkuriMovieMaker",
            "FilePath": "C:/素材/映像.mp4",
            "ContentOffset": offset,
            "Length": 60,
        }

    def test_a_trimmed_item_starts_where_ymm4_starts_it(self) -> None:
        """切り出した位置から再生する 落とすと素材の頭から鳴り、別の場面が映る"""
        (mapped,) = map_template([self.video("00:00:39.6000000")], report=CompatibilityReport())
        assert mapped.clip.source_in == Fraction(198, 5)

    def test_the_offset_keeps_every_digit(self) -> None:
        """7 桁の小数を丸めない 丸めると長い素材で数フレームずれる"""
        (mapped,) = map_template([self.video("00:09:24.1999999")], report=CompatibilityReport())
        assert mapped.clip.source_in == Fraction(5641999999, 10000000)

    def test_days_in_the_offset_are_not_dropped(self) -> None:
        """``日.時:分:秒`` の日を落とさない 落とすと 1 日ぶん手前から再生する"""
        (mapped,) = map_template([self.video("1.00:00:06")], report=CompatibilityReport())
        assert mapped.clip.source_in == Fraction(86406)

    def test_no_offset_starts_at_the_head(self) -> None:
        # 既定を 0 以外にすると、切り出していないアイテムまでずれて始まる
        (mapped,) = map_template([self.video("00:00:00")], report=CompatibilityReport())
        assert mapped.clip.source_in == Fraction(0)

    def test_an_item_without_media_is_left_alone(self) -> None:
        """素材を読まないアイテムには効かせない

        配布物 230 本で 0 以外だったのはテキスト 33・図形 25・フレームバッファ 6・
        グループ 4 と、素材を読まない物ばかりだった（書き出しに残る既定の値で、
        YMM4 でも絵は動かない） 効かせると、グループ（入れ子のシーン）の時刻がずれる
        """
        item = {
            "$type": "YukkuriMovieMaker.Project.Items.ShapeItem, YukkuriMovieMaker",
            "ContentOffset": "00:00:06",
            "Length": 60,
        }
        (mapped,) = map_template([item], report=CompatibilityReport())
        assert mapped.clip.source_in == Fraction(0)

    @pytest.mark.parametrize(
        "offset",
        [
            "00:60:00",
            "00:00:60",
            "24:00:00",
            "1.24:00:00",
            "00:00:01.12345678",
            "99999999999999999999.00:00:00",
            "0:00:06",
            "0:0:0",
            "10675200.00:00:00",
            # TimeSpan.MaxValue の 100 ナノ秒 1 つ先 日だけを見ると通ってしまう
            "10675199.02:48:05.4775808",
            "10675199.23:59:59",
            # アラビア数字 ``\d`` は Unicode の十進数字も拾う
            "٠٠:٠٠:٠٦",
        ],
    )
    def test_a_shape_dot_net_never_writes_is_not_taken(self, offset: str) -> None:
        """.NET が書かない形は受けない

        受けると、書き間違い（``00:60:00`` や ``0:0:0`` など）から別の場面が再生される
        桁を無制限にすると、長い数字で ``int`` が桁数の上限に当たって投げ、
        テンプレートの読み込みごと止まる ``TimeSpan.MaxValue`` を超える長さや、
        ASCII でない数字（``٠٠:٠٠:٠٦``）も .NET からは出てこない
        """
        report = CompatibilityReport()
        (mapped,) = map_template([self.video(offset)], report=report)
        assert mapped.clip.source_in == Fraction(0)
        assert any("ContentOffset" in line for line in report.lines())

    @pytest.mark.parametrize("offset", ["まもなく", "00:00", "-00:00:01"])
    def test_an_offset_that_cannot_be_read_is_counted(self, offset: str) -> None:
        """読めない形と負の値は、頭から再生して数える

        黙って 0 にすると「全部写せている」と言いながら別の場面が映る
        こちらの :class:`Clip` は負の開始位置を受け取らない
        """
        report = CompatibilityReport()
        (mapped,) = map_template([self.video(offset)], report=report)
        assert mapped.clip.source_in == Fraction(0)
        assert any("ContentOffset" in line for line in report.lines())


class TestItemSound:
    """アイテムの音の設定（Issue #89）

    項目の並びは YMM4 が書いたものを数えて決めた 手元の YMM4（4.48.0.3）の
    ``user/setting/…/ItemSettings.json`` にある既定の動画アイテムと、実物の
    プロジェクト 16 本に入っていた動画アイテム 214 個・音声アイテム 125 個
    ``Volume`` は百分率で、音を消したものは 0 だった（音を消す印は別に無い）
    """

    def video(self, **values: Any) -> dict[str, Any]:
        item: dict[str, Any] = {
            "$type": "YukkuriMovieMaker.Project.Items.VideoItem, YukkuriMovieMaker",
            "FilePath": "C:/素材/映像.mp4",
            "Volume": still(100.0),
            "Pan": still(0.0),
            "PlaybackRate": 100.0,
            "ContentOffset": "00:00:00",
            "IsLooped": False,
            "AudioTrackIndex": 0,
            "AudioEffects": [],
            "Length": 60,
        }
        item.update(values)
        return item

    def test_a_video_item_asks_for_its_sound(self) -> None:
        """動画アイテムは音も鳴らす印を持つ

        立てないと映像トラックへ 1 本置くだけになり、置いた動画の音が鳴らない
        """
        (mapped,) = map_template([self.video()], report=CompatibilityReport())
        assert mapped.with_sound is True

    def test_a_still_item_does_not_ask_for_sound(self) -> None:
        # 画像アイテムで立てると、音を持たない素材の分まで音声トラックを探しに行く
        item = {
            "$type": "YukkuriMovieMaker.Project.Items.ImageItem, YukkuriMovieMaker",
            "FilePath": "C:/素材/絵.png",
            "Length": 60,
        }
        (mapped,) = map_template([item], report=CompatibilityReport())
        assert mapped.with_sound is False

    def test_the_volume_becomes_an_audio_effect(self) -> None:
        """音量は ``audio_volume`` へ 写さないと、絞ったはずの音が原寸で鳴る"""
        (mapped,) = map_template([self.video(Volume=still(50.0))], report=CompatibilityReport())
        (effect,) = mapped.audio_effects
        assert effect.kind == "audio_volume"
        assert value_at(effect.params["volume"]) == 50.0

    def test_a_muted_item_keeps_its_zero(self) -> None:
        """YMM4 に音を消す印は無く、音を消したアイテムは ``Volume`` が 0

        0 を既定へ読み替えると、消したはずの音が原寸で鳴る
        """
        (mapped,) = map_template([self.video(Volume=still(0.0))], report=CompatibilityReport())
        (effect,) = mapped.audio_effects
        assert value_at(effect.params["volume"]) == 0.0

    def test_the_default_volume_adds_nothing(self) -> None:
        # 既定のままで音量のエフェクトが並ぶと、何を変えたテンプレートなのか読めない
        (mapped,) = map_template([self.video()], report=CompatibilityReport())
        assert mapped.audio_effects == ()

    @pytest.mark.parametrize(
        ("values", "word"),
        [
            ({"Pan": still(50.0)}, "Pan"),
            ({"PlaybackRate": 150.0}, "PlaybackRate"),
            ({"AudioTrackIndex": 1}, "AudioTrackIndex"),
            ({"IsLooped": True}, "IsLooped"),
            ({"AudioEffects": [{"$type": "N.VibratoEffect, A"}]}, "VibratoEffect"),
        ],
    )
    def test_what_cannot_be_carried_is_counted(self, values: dict[str, Any], word: str) -> None:
        """写せない音の設定は数えて残す 握り潰すと、直す順番を決められない

        どれも実物に出てくる 再生速度は 0 のものが 5 個あり（こちらの ``speed`` は
        正の数しか取らない） 開始位置（``ContentOffset``）は写せるようになった
        """
        report = CompatibilityReport()
        map_template([self.video(**values)], report=report)
        assert any(word in line for line in report.lines())

    def test_a_setting_that_only_moves_later_is_counted(self) -> None:
        """途中から動き出す定位も数える

        先頭の値だけを見ると、0 から始まって途中で振り切れる定位を数え落とし、
        互換性レポートが「全部写せている」と言う
        """
        moving = {
            "Values": [{"Value": 0.0}, {"Value": 100.0}],
            "Span": 0.0,
            "AnimationType": "直線移動",
        }
        report = CompatibilityReport()
        map_template([self.video(Pan=moving)], report=report)
        assert any("Pan" in line for line in report.lines())

    def test_a_switched_off_audio_effect_is_not_counted(self) -> None:
        """切ってある音声エフェクトは数えない

        鳴り方に関わらないものが数に混ざると、直す順番を多い順で決められなくなる
        実物の音声エフェクトはどれも ``IsEnabled`` を持っていた
        """
        off = [{"$type": "N.VibratoEffect, A", "IsEnabled": False}]
        report = CompatibilityReport()
        map_template([self.video(AudioEffects=off)], report=report)
        assert not any("VibratoEffect" in line for line in report.lines())

    def test_an_unknown_item_is_recorded_and_skipped(self) -> None:
        report = CompatibilityReport()
        assert map_template([{"$type": "N.TachieItem, A"}], report=report) == []
        assert any("TachieItem" in line for line in report.lines())

    def test_an_effect_item_works_on_what_is_below(self) -> None:
        # 図形として読むと、範囲の背景が画面を塗りつぶす（YMM4 の絵で確かめた）
        item = {
            "$type": "YukkuriMovieMaker.Project.Items.EffectItem, YukkuriMovieMaker",
            "ShapeType2": "YukkuriMovieMaker.Shape.BackgroundShapePlugin, YukkuriMovieMaker",
            "ShapeParameter": {
                "$type": "YukkuriMovieMaker.Project.Items.BackgroundShapeParameter, YMM",
                "Color": "#FF00FF00",
            },
            "Length": 300,
            "Layer": 6,
        }
        source = map_template([item], report=CompatibilityReport())[0].clip.source
        assert source is not None
        assert source.kind == "framebuffer"


class TestDecorationsList:
    """``Decorations`` は実物では空だったが、形式にはあるので読めるままにする"""

    def test_a_border_decoration(self) -> None:
        result = map_decorations(
            [{"$type": "N.BorderDecoration, A", "Thickness": 6, "Color": "#FF000000"}],
            CompatibilityReport(),
        )
        assert value_at(result.params["border_width"]) == 6.0

    def test_something_that_is_not_a_list_is_ignored(self) -> None:
        assert map_decorations(None, CompatibilityReport()).params == {}
