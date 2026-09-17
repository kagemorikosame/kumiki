"""YMM4 の残りの映像エフェクトと図形の写し方

値の意味は YMM4 本体に描かせた試験（``tools/ymm4_probes.py`` の 3〜5 回目）の絵から
読み取った ここで見るのは「読み取った決まりどおりに写せているか」
"""

from __future__ import annotations

from typing import Any

import pytest

from kumiki.compat.aviutl.report import CompatibilityReport
from kumiki.compat.ymm4.decorations import map_video_effects
from kumiki.compat.ymm4.template import map_template
from kumiki.core.model import AnimatedValue, Effect


def _still(amount: float) -> dict[str, Any]:
    return {"Values": [{"Value": amount}], "Span": 0.0, "AnimationType": "なし"}


def _value(value: object, frame: int = 0) -> float:
    assert isinstance(value, AnimatedValue)
    return value.at(frame)


def _map(name: str, **fields: Any) -> tuple[Effect, CompatibilityReport]:
    entry = {"$type": f"N.{name}, YukkuriMovieMaker", "IsEnabled": True, **fields}
    report = CompatibilityReport()
    result = map_video_effects([entry], report, length=60)
    (mapped,) = result.effects
    return mapped, report


def _shape_item(plugin: str, parameter: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {
        "$type": "YukkuriMovieMaker.Project.Items.ShapeItem, YukkuriMovieMaker",
        "ShapeType2": f"N.{plugin}, YukkuriMovieMaker",
        "ShapeParameter": parameter,
        "Length": 60,
        "Frame": 0,
        "Layer": 0,
        **fields,
    }


class TestLightAndLens:
    def test_reflection_reads_the_nested_lighting(self) -> None:
        effect, report = _map(
            "ReflectionAndExtrusionEffect",
            LightingMode="DistantDiffuse",
            HeightmapMode="Bevel",
            Lighting={
                "LightSource": {"Azimuth": _still(-85.0), "Elevation": _still(30.0)},
                "Highlight": {
                    "Exponent": _still(2.0),
                    "Constant": _still(45.0),
                    "Color": "#FFFF0000",
                    "Blend": "Add",
                },
                "SurfaceScale": _still(8.0),
            },
            Heightmap={"BevelMode": "InvertedRound", "Thickness": _still(12.0)},
            Blur=_still(4.0),
            IsInvert=True,
        )
        assert not report.lines()
        assert effect.kind == "bevel_light"
        assert effect.params["lighting"] == "diffuse"
        assert _value(effect.params["azimuth"]) == -85.0
        assert _value(effect.params["constant"]) == 45.0
        assert effect.params["profile"] == "inverted_round"
        assert effect.params["color"] == (1.0, 0.0, 0.0, 1.0)
        assert effect.params["inverted"] is True

    def test_lens_blur(self) -> None:
        effect, _ = _map("LensBlurEffect", BlurRadius=_still(20.0), Brightness=_still(150.0))
        assert effect.kind == "lens_blur"
        assert _value(effect.params["radius"]) == 20.0

    def test_bloom_can_colour_the_light(self) -> None:
        effect, report = _map(
            "BloomEffect",
            Strength=_still(120.0),
            Threshold=_still(50.0),
            Blur=_still(30.0),
            IsColorizationEnabled=True,
            Color="#FF00FF00",
        )
        assert not report.lines()
        assert effect.kind == "glow"
        assert effect.params["tinted"] is True
        assert effect.params["tint"] == (0.0, 1.0, 0.0, 1.0)


class TestDistortion:
    def test_fish_eye(self) -> None:
        effect, _ = _map(
            "FishEyeLensEffect", Projection="Equidistant", Angle=_still(120.0), Zoom=_still(90.0)
        )
        assert effect.kind == "fish_eye"
        assert effect.params["projection"] == "equidistant"
        assert _value(effect.params["angle"]) == 120.0

    def test_ripple_flips_y(self) -> None:
        effect, _ = _map(
            "RippleEffect",
            X=_still(100.0),
            Y=_still(40.0),
            Amplitude=_still(-20.0),
            WaveLength=_still(300.0),
            Period=_still(3.0),
        )
        assert effect.kind == "ripple"
        assert _value(effect.params["center_y"]) == -40.0

    def test_stretch(self) -> None:
        effect, _ = _map(
            "StretchEffect",
            X=_still(0.0),
            Y=_still(0.0),
            Angle=_still(45.0),
            StretchLength=_still(300.0),
            Range=_still(50.0),
            IsCentering=False,
        )
        assert effect.kind == "stretch"
        assert _value(effect.params["stretch"]) == 300.0
        assert effect.params["centering"] is False

    def test_polar_and_tiling(self) -> None:
        polar, _ = _map("PolarTransformEffect", CoreWidth=_still(200.0), TwistAngle=_still(90.0))
        assert (polar.kind, _value(polar.params["core"])) == ("polar", 200.0)
        tiling, _ = _map("TilingEffect", X=_still(2.0), Y=_still(41.0))
        assert (tiling.kind, _value(tiling.params["count_y"])) == ("tile", 41.0)


class TestTiming:
    def test_repeat_zoom(self) -> None:
        effect, _ = _map(
            "RepeatZoomEffect",
            Zoom=_still(150.0),
            ZoomX=_still(100.0),
            ZoomY=_still(100.0),
            Span=_still(2.0),
            EasingType="Sine",
            EasingMode="InOut",
            IsCentering=True,
        )
        assert effect.kind == "repeat_zoom"
        assert _value(effect.params["interval"]) == 2.0
        assert effect.params["easing"] == "sine"
        assert effect.params["centering"] is True

    def test_jump(self) -> None:
        effect, _ = _map(
            "JumpEffect",
            JumpHeight=_still(40.0),
            Stretch=_still(1.0),
            Period=_still(0.53),
            Distortion=_still(0.6),
            Interval=_still(0.07),
        )
        assert effect.kind == "jump"
        assert _value(effect.params["height"]) == 40.0
        assert _value(effect.params["period"]) == 0.53

    def test_the_jump_easing_waits_until_the_end(self) -> None:
        effect, report = _map(
            "RepeatRotateEffect",
            X=_still(0.0),
            Y=_still(0.0),
            Z=_still(90.0),
            Is3D=False,
            Span=_still(1.0),
            EasingType="Jump",
            EasingMode="In",
        )
        assert not report.lines()
        assert effect.params["easing"] == "jump"

    def test_after_image(self) -> None:
        effect, _ = _map("AfterImageEffect", Strength=_still(53.6), Mode="Front")
        assert effect.kind == "after_image"
        assert _value(effect.params["strength"]) == pytest.approx(53.6)
        assert effect.params["mode"] == "front"

    def test_the_three_dimensional_effect_adds_nothing(self) -> None:
        # YMM4 に描かせても絵が変わらなかった（記録にも残さない）
        entry = {
            "$type": "N.ThreeDimensionalEffect, YukkuriMovieMaker",
            "IsEnabled": True,
            "X": _still(0.0),
            "Y": _still(300.0),
            "Length": _still(4.0),
        }
        report = CompatibilityReport()
        assert not map_video_effects([entry], report, length=60).effects
        assert not report.lines()


class TestWipeAndKey:
    @pytest.mark.parametrize(
        ("name", "pattern"),
        [
            ("ワイプ横", "horizontal"),
            ("円", "circle"),
            ("四角", "square"),
            ("時計回り", "clockwise"),
        ],
    )
    def test_the_bundled_wipe_images_become_patterns(self, name: str, pattern: str) -> None:
        effect, report = _map(
            "InOutTransitionEffect",
            File=f"D:\\Program\\YukkuriMovieMaker_v4\\Resources\\Transition\\{name}.png",
            Tolerance=_still(3.0),
            Angle=_still(0.0),
            IsInEffect=True,
            IsOutEffect=False,
            EffectTimeSeconds=1.0,
            EasingType="Quart",
            EasingMode="InOut",
        )
        assert not report.lines()
        assert effect.kind == "inout_wipe"
        assert effect.params["pattern"] == pattern
        assert effect.params["easing"] == "quart"

    def test_an_unknown_wipe_image_falls_back_to_a_fade(self) -> None:
        effect, report = _map("InOutTransitionEffect", File="C:\\素材\\自作ワイプ.png")
        assert effect.params["pattern"] == "fade"
        assert any("自作ワイプ" in line for line in report.lines())

    def test_directional_key(self) -> None:
        effect, _ = _map(
            "DirectionalColorKeyEffect",
            BackgroundColor="#FF282828",
            ForegroundColor="#FFE7E7E7",
            EdgeSoftness=_still(3.0),
            NoiseThreshold=_still(0.02),
            OutputForeground=True,
        )
        assert effect.kind == "directional_key"
        background = effect.params["background"]
        assert isinstance(background, tuple)
        assert background[0] == pytest.approx(0x28 / 255)
        assert effect.params["output_foreground"] is True


class TestParticles:
    def test_the_emitter_keeps_the_screen_direction(self) -> None:
        effect, report = _map(
            "ParticleOutputEffect",
            Rate=_still(400.0),
            Lifetime=_still(2.0),
            Preroll=_still(10.0),
            Size=_still(86.0),
            X=_still(0.0),
            Y=_still(-2000.0),
            EmitRange=_still(1300.0),
            EmitAngle=_still(-106.0),
            Speed=_still(1000.0),
            Gravity=_still(18000.0),
            EndScale=_still(67.0),
            Fade=_still(100.0),
            Randomness=_still(100.0),
        )
        assert effect.kind == "particles"
        # 放つ位置の Y は画面と同じ下向き（パーティクルのシェーダの中で裏返す）
        assert _value(effect.params["emitter_y"]) == -2000.0
        assert _value(effect.params["gravity"]) == 18000.0
        assert not report.lines()


class TestShapes:
    def test_the_pen_points_move_to_the_centre(self) -> None:
        item = _shape_item(
            "PenShapePlugin",
            {
                "Strokes": [
                    {
                        "StylusPoints": [{"X": 960.0, "Y": 540.0}, {"X": 1060.0, "Y": 640.0}],
                        "DrawingAttributes": {"Color": "#FF74C7CB", "Width": 640.0},
                    }
                ],
                "Thickness": _still(50.0),
                "Length": _still(80.0),
                "Offset": _still(10.0),
            },
        )
        report = CompatibilityReport()
        source = map_template([item], report=report)[0].clip.source
        assert source is not None
        assert source.params["points"] == "0,0;100,100"
        assert _value(source.params["line_width"]) == pytest.approx(320.0)
        assert _value(source.params["trim_end"]) == pytest.approx(90.0)

    def test_the_timer_counts_down_to_its_first_value(self) -> None:
        item = _shape_item(
            "TimerShapePlugin",
            {
                "Format": "s",
                "Direction": "CountDown",
                "InitialValue": _still(0.99),
                "PlaybackRate": _still(100.0),
                "FontSize": 520.0,
                "FontColor": "#FFFFFFFF",
                "BasePoint": "CenterCenter",
            },
        )
        source = map_template([item], report=CompatibilityReport())[0].clip.source
        assert source is not None and source.kind == "text"
        assert source.params["timer_format"] == "s"
        assert source.params["timer_countdown"] is True
        assert source.params["timer_length"] == 60

    def test_the_concentration_lines(self) -> None:
        item = _shape_item(
            "ConcentrationLineShapePlugin",
            {
                "Size": _still(1000.0),
                "Density": _still(90.0),
                "Thickness": _still(60.0),
                "Length": _still(70.0),
                "CenterWidth": _still(50.0),
                "Speed": _still(4.0),
                "Stroke": "#FFFFEBC4",
            },
        )
        report = CompatibilityReport()
        source = map_template([item], report=report)[0].clip.source
        assert source is not None
        assert source.params["shape"] == "concentration"
        assert _value(source.params["density"]) == 90.0
        assert _value(source.params["softness"]) == 50.0
        assert not report.lines()

    def test_an_inverted_item_is_flipped_before_it_is_placed(self) -> None:
        item = _shape_item(
            "QuadrilateralShapePlugin",
            {"Width": _still(100.0), "Height": _still(50.0)},
            IsInverted=True,
            X=_still(200.0),
        )
        clip = map_template([item], report=CompatibilityReport())[0].clip
        kinds = [effect.kind for effect in clip.effects]
        assert kinds == ["flip", "transform"]
        assert clip.effects[0].params["horizontal"] is True


class TestBrokenValues:
    def test_a_noise_brush_with_a_broken_octave_still_loads(self) -> None:
        # NaN を round へ渡すと例外になり、同じテンプレートのほかのアイテムまで読めなくなる
        item = _shape_item(
            "QuadrilateralShapePlugin",
            {
                "Width": _still(100.0),
                "Height": _still(100.0),
                "Brush": {
                    "Type": "YukkuriMovieMaker.Brush.NoiseBrushPlugin, YukkuriMovieMaker",
                    "Parameter": {
                        "NoiseType": "Perlin",
                        "Color1": "#FF000000",
                        "Color2": "#FFFFFFFF",
                        "NoiseParameter": {"Octaves": float("nan")},
                    },
                },
            },
        )
        report = CompatibilityReport()
        (mapped,) = map_template([item], report=report)
        painted = next(e for e in mapped.clip.effects if e.kind == "brush_fill")
        assert painted.params["noise_octaves"] == 5
        assert any("重ね数" in line for line in report.lines())

    def test_an_unknown_transition_setting_is_recorded(self) -> None:
        item = {
            "$type": "YukkuriMovieMaker.Project.Items.TransitionItem, YukkuriMovieMaker",
            "TransitionType": "N.FadeTransitionPlugin, YukkuriMovieMaker",
            "TransitionParameter": {"EasingType": "ぬるっと", "EasingMode": "Middle"},
            "Frame": 0,
            "Length": 30,
        }
        report = CompatibilityReport()
        (mapped,) = map_template([item], report=report)
        assert mapped.clip.source is not None
        assert mapped.clip.source.params["easing"] == "linear"
        lines = report.lines()
        assert any("イージング" in line for line in lines)
        assert any("向き" in line for line in lines)
