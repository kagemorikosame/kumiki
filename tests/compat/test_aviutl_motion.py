"""AviUtl のトラックバーに付く「移動方法」

書き方は AviUtl2（v2.1.6a）に実際に作らせたエイリアスから読み取った
ここに並ぶ文字列は、その実物から取った行そのもの
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from kumiki.compat.aviutl.exo import parse_exo
from kumiki.compat.aviutl.mapping import map_object
from kumiki.compat.aviutl.motion import Motion, animated_value, parse_motion
from kumiki.compat.aviutl.report import CompatibilityReport
from kumiki.core.model import AnimatedValue, Interpolation
from kumiki.core.timebase import FrameRate

RATE = FrameRate(60)


def _object(draw: str, *, frame: str = "0,59", content: str = "テキスト=あ") -> str:
    """1 オブジェクトぶんの本文 ``draw`` は標準描画に入れる行"""
    return f"""[Object]
frame={frame}
[Object.0]
effect.name=テキスト
サイズ=64.00
{content}
[Object.1]
effect.name=標準描画
{draw}
合成モード=通常
"""


def _mapped(draw: str, *, frame: str = "0,59") -> tuple[object, CompatibilityReport]:
    report = CompatibilityReport()
    document = parse_exo(_object(draw, frame=frame))
    item = map_object(document.objects[0], RATE, report=report)
    assert item is not None
    return item, report


def _transform(draw: str, *, frame: str = "0,59") -> tuple[dict[str, object], CompatibilityReport]:
    item, report = _mapped(draw, frame=frame)
    effects = item.clip.effects  # type: ignore[attr-defined]
    assert effects, "変形エフェクトが作られていない"
    return dict(effects[0].params), report


def _animated(value: object) -> AnimatedValue:
    assert isinstance(value, AnimatedValue)
    return value


class TestParsing:
    def test_a_plain_number_has_no_method(self) -> None:
        assert parse_motion("0.000") == Motion(values=(0.0,))

    def test_values_method_and_flags(self) -> None:
        motion = parse_motion("-0.01,149.16,300.00,直線移動,0")
        assert motion == Motion(values=(-0.01, 149.16, 300.0), method="直線移動", flags=0)

    def test_the_script_setting_comes_after_the_bar(self) -> None:
        # 反復移動 と 回転 は .tra2 のスクリプト 設定値が旗の後ろに付く
        assert parse_motion("0,0,回転,4|360") == Motion(
            values=(0.0, 0.0), method="回転", flags=4, extra="360"
        )

    def test_an_expression_keeps_its_commas(self) -> None:
        # 参照式にはカンマが入りうる 先に割ると式が途中で切れる
        motion = parse_motion("100.000,200.000,瞬間移動,8|max(0,time)")
        assert motion is not None
        assert motion.extra == "max(0,time)"
        assert motion.flags == 8

    def test_text_is_not_a_number(self) -> None:
        assert parse_motion("Rounded Mplus 1c Black") is None
        assert parse_motion(None) is None


class TestKeyframes:
    def test_a_middle_point_becomes_a_keyframe(self) -> None:
        params, _ = _transform("X=0,150,300,直線移動,0", frame="244,333,423")
        pos_x = _animated(params["pos_x"])
        placed = [(k.frame, k.value) for k in pos_x.keyframes]
        assert placed == [(0, 0.0), (89, 150.0), (179, 300.0)]
        assert pos_x.at(89) == 150.0

    def test_two_values_span_the_whole_clip(self) -> None:
        # 時間制御やスクリプトの移動方法は中間点を見ない 値は 2 つのまま来る
        params, _ = _transform("X=0,300,直線移動(時間制御),0", frame="244,333,423")
        pos_x = _animated(params["pos_x"])
        assert [k.frame for k in pos_x.keyframes] == [0, 179]

    def test_an_instant_move_holds_its_value(self) -> None:
        params, _ = _transform("拡大率=100,200,瞬間移動,0")
        scale = _animated(params["scale"])
        assert scale.keyframes[0].interpolation is Interpolation.HOLD
        assert scale.at(30) == 100.0
        assert scale.at(59) == 200.0

    def test_the_y_axis_is_flipped(self) -> None:
        # AviUtl の Y は下が正 こちらは上が正
        params, _ = _transform("Y=0,100,直線移動,0")
        assert _animated(params["pos_y"]).at(59) == -100.0

    def test_a_still_value_stays_still(self) -> None:
        # 同じ値が並ぶだけならキーフレームは要らない
        params, _ = _transform("X=50,50,50,直線移動,0", frame="0,30,59")
        assert _animated(params["pos_x"]) == AnimatedValue(50.0)


class TestAcceleration:
    """加速と減速は旗で付く 補間移動でだけ選べる"""

    @pytest.mark.parametrize(
        ("flags", "expected"),
        [
            (0, Interpolation.LINEAR),
            (1, Interpolation.EASE_IN),
            (2, Interpolation.EASE_OUT),
            (3, Interpolation.EASE_IN_OUT),
        ],
    )
    def test_the_flags_choose_the_curve(self, flags: int, expected: Interpolation) -> None:
        params, _ = _transform(f"X=0,300,補間移動,{flags}")
        assert _animated(params["pos_x"]).keyframes[0].interpolation is expected


class TestWhatCannotBeCopied:
    def test_a_script_move_is_recorded_and_frozen(self) -> None:
        # ランダム移動は乱数で動く 直線で動かすと実物と違う動きが出たまま気付けない
        params, report = _transform("X=10,300,ランダム移動,4|15")
        assert _animated(params["pos_x"]) == AnimatedValue(10.0)
        assert any("ランダム移動" in line for line in report.lines())

    def test_an_expression_is_recorded_and_frozen(self) -> None:
        params, report = _transform("拡大率=150,200,瞬間移動,8|100+time*10")
        assert _animated(params["scale"]) == AnimatedValue(150.0)
        assert any("参照式" in line for line in report.lines())

    def test_time_control_keeps_the_values_and_is_recorded(self) -> None:
        params, report = _transform("X=0,300,直線移動(時間制御),0")
        assert _animated(params["pos_x"]).is_animated
        assert any("時間制御" in line for line in report.lines())

    def test_an_unknown_method_is_recorded(self) -> None:
        params, report = _transform("X=10,300,未知の移動,0")
        assert _animated(params["pos_x"]) == AnimatedValue(10.0)
        assert any("未知の移動" in line for line in report.lines())

    def test_a_spline_says_it_is_an_approximation(self) -> None:
        _, report = _transform("X=0,300,補間移動,3")
        assert any("補間移動" in line for line in report.lines())


class TestTheObjectSpan:
    def test_a_middle_point_does_not_shorten_the_object(self) -> None:
        # ``frame=244,333,423`` の 2 つ目を終了として読んでいた
        document = parse_exo(_object("X=0", frame="244,333,423"))
        obj = document.objects[0]
        assert (obj.start, obj.end) == (244, 423)
        assert obj.duration == 180
        assert obj.relative_points() == (0, 89, 179)

    def test_without_a_middle_point_the_points_are_the_two_ends(self) -> None:
        document = parse_exo(_object("X=0", frame="0,59"))
        assert document.objects[0].relative_points() == (0, 59)


class TestPlayback:
    def test_the_playback_range_becomes_the_source_position(self) -> None:
        # 実物の書き方 ``再生位置=0.967,6.151,再生範囲,0`` 値は秒
        body = """[Object]
frame=0,310
[Object.0]
effect.name=動画ファイル
再生位置=0.967,6.151,再生範囲,0
再生速度=200.00
ファイル=D:\\録画先\\a.mp4
[Object.1]
effect.name=映像再生
X=0.00
"""
        item = map_object(parse_exo(body).objects[0], RATE, report=CompatibilityReport())
        assert item is not None
        assert item.clip.source_in == Fraction(967, 1000)
        assert item.clip.speed == Fraction(2)

    def test_a_missing_speed_means_normal(self) -> None:
        item, _ = _mapped("X=0")
        assert item.clip.speed == Fraction(1)  # type: ignore[attr-defined]
        assert item.clip.source_in == Fraction(0)  # type: ignore[attr-defined]


class TestTheOldReaders:
    def test_number_falls_back_to_the_first_value(self) -> None:
        # 以前はここで既定値へ落ちて、動きの付いた項目が黙って消えていた
        document = parse_exo(_object("X=0,300,直線移動,0"))
        draw = document.objects[0].find("標準描画")
        assert draw is not None
        assert draw.number("X") == 0.0
        assert draw.motion("X") is not None


def test_a_value_without_points_stays_still() -> None:
    # 区間が 1 フレームだと置き場が無い 先頭の値で止める
    report = CompatibilityReport()
    value = animated_value("0,300,直線移動,0", points=(0,), log=report, label="試し")
    assert value == AnimatedValue(0.0)
