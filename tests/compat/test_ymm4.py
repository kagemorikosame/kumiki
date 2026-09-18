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
from pathlib import Path
from typing import Any

import pytest

from kumiki.compat.aviutl.report import CompatibilityReport
from kumiki.compat.ymm4.decorations import map_decorations, map_video_effects
from kumiki.compat.ymm4.template import Ymm4ParseError, load_template, map_template
from kumiki.compat.ymm4.values import (
    animated,
    brush_colour,
    colour,
    frame_positions,
    interpolation_of,
    number,
    type_name,
)
from kumiki.core.model import AnimatedValue, Interpolation

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
        "Layer": 5,
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
        # YMM4 の X が正だと上の辺が手前へ来る Kumiki の X 軸の正は上の辺が奥へ倒れる
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

    def test_a_group_with_nothing_to_give_produces_nothing(self) -> None:
        assert map_template([group_item()], report=CompatibilityReport()) == []


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
