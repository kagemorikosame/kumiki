"""AviUtl の図形まわり

AviUtl1 と AviUtl2 で書き方が違う AviUtl2 に置かせたエイリアスから読み取った
（``tests/fixtures/aviutl/probes/kumiki_shape1.object`` など）
"""

from __future__ import annotations

import pytest

from kumiki.compat.aviutl.exo import parse_exo
from kumiki.compat.aviutl.mapping import map_object
from kumiki.compat.aviutl.report import CompatibilityReport
from kumiki.compat.mapped import MappedObject
from kumiki.core.model import AnimatedValue, GeneratedSource
from kumiki.core.timebase import FrameRate
from kumiki.engine.sources import format_time, timer_text

RATE = FrameRate(60)


def _object(body: str) -> str:
    """中身 1 つと標準描画だけのオブジェクト ``body`` は ``名前\n項目=値``"""
    name, _, rest = body.partition("\n")
    lines = ["[Object]", "frame=0,59", "[Object.0]", f"effect.name={name}"]
    lines.extend(line for line in rest.splitlines() if line)
    lines += ["[Object.1]", "effect.name=標準描画", "X=0.00", "合成モード=通常"]
    return "\n".join(lines) + "\n"


def _mapped(body: str) -> tuple[MappedObject, CompatibilityReport]:
    report = CompatibilityReport()
    item = map_object(parse_exo(_object(body)).objects[0], RATE, report=report)
    assert item is not None
    return item, report


def _source(body: str) -> GeneratedSource:
    item, _ = _mapped(body)
    assert item.clip.source is not None
    return item.clip.source


def _value(source: GeneratedSource, name: str) -> float:
    value = source.params[name]
    assert isinstance(value, AnimatedValue)
    return value.static


class TestTheSecondGeneration:
    """AviUtl2 は種類を名前で、色を ``色`` で書く"""

    @pytest.mark.parametrize(
        ("named", "expected"),
        [
            ("背景", "background"),
            ("円", "ellipse"),
            ("四角形", "rect"),
            ("三角形", "triangle"),
            ("五角形", "pentagon"),
            ("六角形", "hexagon"),
            ("星型", "star"),
        ],
    )
    def test_the_kind_comes_from_the_name(self, named: str, expected: str) -> None:
        # 番号（AviUtl1 の書き方）だけを見ていたので、AviUtl2 の図形は全部 円 になっていた
        source = _source(f"図形\n図形の種類={named}\nサイズ=100\n色=ff0000\nライン幅=4000")
        assert source.params["shape"] == expected

    def test_the_colour_comes_from_the_japanese_key(self) -> None:
        # ``color`` だけを見ていたので、AviUtl2 の図形は必ず白かった
        source = _source("図形\n図形の種類=円\nサイズ=100\n色=ff0000\nライン幅=4000")
        assert source.params["color"] == (1.0, 0.0, 0.0, 1.0)

    def test_a_thick_line_means_filled(self) -> None:
        # ライン幅 4000 は塗りつぶし そのまま線の太さにすると画面を覆う輪郭になる
        source = _source("図形\n図形の種類=円\nサイズ=100\n色=ffffff\nライン幅=4000")
        assert _value(source, "line_width") == 0.0
        assert source.params["outline_only"] is False

    def test_a_thin_line_stays_an_outline(self) -> None:
        source = _source("図形\n図形の種類=円\nサイズ=100\n色=ffffff\nライン幅=8")
        assert _value(source, "line_width") == 8.0
        assert source.params["outline_only"] is True

    def test_rounded_corners_change_the_kind(self) -> None:
        source = _source("図形\n図形の種類=四角形\nサイズ=100\n色=ffffff\n角を丸くする=1")
        assert source.params["shape"] == "rounded"

    def test_an_unknown_kind_is_recorded(self) -> None:
        # ハート に当たる形はこちらに無い 黙って矩形にすると別の絵が出たまま気付けない
        item, report = _mapped("図形\n図形の種類=ハート\nサイズ=100\n色=ffffff")
        assert item.clip.source is not None
        assert item.clip.source.params["shape"] == "rect"
        assert any("図形の種類: ハート" in line for line in report.lines())


class TestTheFirstGeneration:
    def test_the_number_still_works(self) -> None:
        # AviUtl1 の書き方（番号と color）も読めること
        source = _source("図形\ntype=2\nサイズ=100\ncolor=00ff00\nライン幅=4000")
        assert source.params["shape"] == "triangle"
        assert source.params["color"] == (0.0, 1.0, 0.0, 1.0)


class TestConcentration:
    def test_the_custom_object_becomes_a_shape(self) -> None:
        source = _source("集中線\n濃さ=40.0\n速さ=25.0\n中心幅=0\n色=ffffff")
        assert source.kind == "shape"
        assert source.params["shape"] == "concentration"
        assert _value(source, "flicker") == 25.0

    def test_the_density_drives_both_the_count_and_the_width(self) -> None:
        # AviUtl2 に濃さ 40・80・160 を描かせて測ると、線の占める角度が
        # 18%・約 65%・100% と 2 乗で増える 本数だけに渡すと、
        # 濃くしたはずの画面が埋まらず、線が増えただけの絵になる
        source = _source("集中線\n濃さ=40.0\n速さ=25.0\n中心幅=0\n色=ffffff")
        assert _value(source, "density") == 64.0
        assert _value(source, "line_thickness") == 33.0
        thick = _source("集中線\n濃さ=80.0\n速さ=25.0\n中心幅=0\n色=ffffff")
        assert _value(thick, "line_thickness") == 132.0

    def test_the_centre_gap_is_the_radius_of_the_hole(self) -> None:
        # AviUtl2 の実物で 中心幅 300 の絵は半径 300 まで真っ暗だった
        # 直径として扱うと、空きが半分になって線が中心まで伸びる
        source = _source("集中線\n濃さ=40.0\n速さ=25.0\n中心幅=300.0\n色=ffffff")
        assert _value(source, "center_gap") == 300.0


class TestTheThingsReviewFound:
    def test_a_missing_line_width_means_filled(self) -> None:
        # AviUtl1 の図形に ライン幅 は無い 輪郭だけにすると、
        # 塗ってあった図形が中抜きになる
        source = _source("図形\ntype=1\nサイズ=100\ncolor=ffffff")
        assert source.params["outline_only"] is False
        assert _value(source, "line_width") == 0.0

    def test_a_zero_line_width_does_not_make_the_shape_hollow(self) -> None:
        # 0 を輪郭として扱うと、塗ってあった図形が中抜きになる
        source = _source("図形\n図形の種類=円\nサイズ=100\n色=ffffff\nライン幅=0")
        assert source.params["outline_only"] is False

    def test_the_concentration_keeps_its_motion(self) -> None:
        # 動きを落とすと、濃さが変わっていく集中線が最初の濃さで止まる
        source = _source("集中線\n濃さ=10,80,直線移動,0\n速さ=25\n中心幅=0\n色=ffffff")
        density = source.params["density"]
        assert isinstance(density, AnimatedValue)
        assert density.is_animated

    def test_a_moving_centre_gap_keeps_moving(self) -> None:
        # 動きを落とすと、広がっていく空きが最初の大きさで止まる
        source = _source("集中線\n濃さ=40\n速さ=25\n中心幅=0,300,直線移動,0\n色=ffffff")
        gap = source.params["center_gap"]
        assert isinstance(gap, AnimatedValue)
        assert gap.is_animated

    def test_a_moving_size_is_recorded(self) -> None:
        # 大きさは サイズ と 縦横比 から計算してから渡すので、動きを残せない
        _, report = _mapped("図形\n図形の種類=円\nサイズ=100,400,直線移動,0\n色=ffffff")
        assert any("図形の動くサイズ" in line for line in report.lines())

    def test_a_still_expression_on_a_size_is_recorded(self) -> None:
        # 値が同じでも式なら時間で変わる 記録しないと、大きさが止まったことに気付けない
        _, report = _mapped("図形\n図形の種類=円\nサイズ=100,100,瞬間移動,8|100+time\n色=ffffff")
        assert any("図形の動くサイズ" in line for line in report.lines())


class TestCustomObjects:
    """AviUtl2 の「カスタムオブジェクト」 効果ではなく中身として置かれる

    どれも AviUtl2 に置かせたエイリアスから項目名を読み取った
    """

    def test_a_fan_keeps_its_angle(self) -> None:
        # 中心角を落とすと、扇が必ず全円（360 度）になる
        source = _source("扇型\n中心角=270.0\nサイズ=100.0\nライン幅=2000.0\n色=ffffff")
        assert source.params["shape"] == "fan"
        assert _value(source, "span") == 270.0
        # ライン幅 2000 は図形より大きい 塗りつぶしとして扱う
        assert source.params["outline_only"] is False

    def test_a_polygon_becomes_a_line_through_its_points(self) -> None:
        # 座標は x と y を並べて書く（Y は下が正） 反転を忘れると上下逆の形になる
        source = _source("多角形\n色=ffffff\nライン幅=20\n頂点数=3\n座標=0,-150,130,75,-130,75")
        assert source.params["shape"] == "polyline"
        assert source.params["points"] == "0.0,150.0;130.0,-75.0;-130.0,-75.0"
        # 閉じないと、最後の頂点から最初へ戻る線が引かれず開いた折れ線になる
        assert source.params["closed"] is True

    def test_a_polygon_is_hollow_unless_told_otherwise(self) -> None:
        # 中抜きの指定を落とすと、線だけのはずの多角形が塗りつぶされる
        source = _source(
            "多角形\n色=ffffff\nライン幅=20\n座標=0,-150,130,75,-130,75\n簡易塗り潰し=0"
        )
        assert source.params["fill_color"] == (0.0, 0.0, 0.0, 0.0)

    def test_a_filled_polygon_uses_its_colour(self) -> None:
        # 塗り潰しの指定を落とすと、塗ってあるはずの中が透明になる
        source = _source(
            "多角形\n色=ff0000\nライン幅=20\n座標=0,-150,130,75,-130,75\n簡易塗り潰し=1"
        )
        assert source.params["fill_color"] == (1.0, 0.0, 0.0, 1.0)

    def test_a_broken_polygon_is_recorded(self) -> None:
        # 座標が読めないと線が 1 本も出ない 黙って空の図形を置くと気付けない
        _, report = _mapped("多角形\n色=ffffff\nライン幅=20\n座標=")
        assert any("多角形の座標" in line for line in report.lines())

    def test_a_counter_becomes_a_timer(self) -> None:
        source = _source(
            "カウンター\n初期値=5.0\n速度=2.0\nサイズ=34.0"
            + "\n表示形式=標準\nフォント名=MS UI Gothic\n文字色=ffffff"
        )
        assert source.kind == "text"
        assert _value(source, "timer_start") == 5.0
        # 速度は 1 秒あたりの進み方 こちらは百分率なので 100 倍する
        assert _value(source, "timer_rate") == 200.0
        assert source.params["font"] == "MS UI Gothic"

    def test_an_unknown_counter_format_is_recorded(self) -> None:
        # 書式を落とすと、時計のつもりの表示がただの秒数になる
        _, report = _mapped(
            "カウンター\n初期値=0\n速度=1\n表示形式=時分秒\nフォント名=MS UI Gothic"
        )
        assert any("表示形式" in line for line in report.lines())

    def test_the_motion_trail_is_recorded(self) -> None:
        # ライン(移動軌跡) は折れ線ではなく、動いた跡を描く別物
        # 折れ線として写すと、まったく違う絵が出たまま気付けない
        report = CompatibilityReport()
        document = parse_exo(_object("ライン(移動軌跡)\nライン幅=16.0\n先端=48.0\n色=ffffff"))
        map_object(document.objects[0], RATE, report=report)
        assert any("ライン(移動軌跡)" in line for line in report.lines())


class TestWhatTheSecondReviewFound:
    def test_the_counter_does_not_wrap_at_a_minute(self) -> None:
        # 書式 s は 60 で分へ繰り上がる カウンターは数を数えるだけなので、
        # そのまま使うと 60 で 0 に戻り、100 のつもりが 40 と出る
        source = _source("カウンター\n初期値=0\n速度=1\nフォント名=MS UI Gothic")
        assert source.params["timer_format"] == "n"
        assert format_time(100.0, "n") == "100"

    def test_the_counter_keeps_its_decoration(self) -> None:
        # 装飾タイプ を読まないと、縁取りのカウンターが素の文字になる
        source = _source(
            "カウンター\n初期値=0\n速度=1\n装飾タイプ=縁取り文字"
            + "\n影・縁色=000000\nフォント名=MS UI Gothic"
        )
        assert "border_width" in source.params or "shadow_x" in source.params

    def test_a_broken_coordinate_drops_the_whole_polygon(self) -> None:
        # 読めない値だけを捨てると、その後ろの x と y が入れ替わって別の形になる
        _, report = _mapped("多角形\n色=ffffff\nライン幅=20\n座標=0,だめ,130,75")
        assert any("多角形の座標" in line for line in report.lines())

    def test_an_odd_number_of_coordinates_is_refused(self) -> None:
        # x と y の組にできない並び 1 つ足りないまま組むと全部ずれる
        _, report = _mapped("多角形\n色=ffffff\nライン幅=20\n座標=0,-150,130")
        assert any("多角形の座標" in line for line in report.lines())

    def test_extra_coordinates_are_trimmed_to_the_corner_count(self) -> None:
        # 頂点数 より座標が多いファイル 余分を描くと形が変わる
        source = _source("多角形\n色=ffffff\nライン幅=20\n頂点数=2\n座標=0,0,10,0,20,0")
        assert source.params["points"] == "0.0,-0.0;10.0,-0.0"

    def test_a_moving_fan_angle_is_recorded(self) -> None:
        # 動きを落とすと、開いていく扇が開いたまま止まる
        _, report = _mapped("扇型\n中心角=0,270,直線移動,0\nサイズ=100\n色=ffffff")
        assert any("扇型の動く中心角" in line for line in report.lines())

    def test_a_moving_counter_speed_is_recorded(self) -> None:
        _, report = _mapped("カウンター\n初期値=0\n速度=1,5,直線移動,0\nフォント名=MS UI Gothic")
        assert any("カウンターの動く速度" in line for line in report.lines())

    def test_a_counter_can_go_below_zero(self) -> None:
        # 0 で止めると、下がっていくはずの数字が途中から動かなくなる
        assert format_time(-5.0, "n") == "-5"

    def test_a_coordinate_that_is_not_finite_is_refused(self) -> None:
        # NaN や無限大は描画の側で落ちる 受け取ると黙って別の形になる
        _, report = _mapped("多角形\n色=ffffff\nライン幅=20\n座標=0,nan,130,75")
        assert any("多角形の座標" in line for line in report.lines())

    def test_too_few_corners_are_recorded_without_trimming(self) -> None:
        # 頂点数 が座標より多いファイル 切り詰めないが、食い違いは残す
        _, report = _mapped("多角形\n色=ffffff\nライン幅=20\n頂点数=5\n座標=0,0,10,0")
        assert any("頂点数と座標の数が合わない" in line for line in report.lines())

    def test_a_clock_format_still_stops_at_zero(self) -> None:
        # 時計が負になることはない 通算の n だけ負の値を出す
        assert timer_text({"_seconds": 0.0, "timer_start": -5.0, "timer_format": "s"}) == "0"
        assert timer_text({"_seconds": 0.0, "timer_start": -5.0, "timer_format": "n"}) == "-5"

    def test_a_value_that_is_not_a_number_does_not_stop_the_drawing(self) -> None:
        # NaN を int() へ渡すと例外になり、そのフレームの描画ごと止まる
        assert format_time(float("nan"), "n") == "0"

    def test_a_corner_count_mismatch_is_recorded_even_with_no_points(self) -> None:
        # 座標を全部捨てたときも、頂点数の食い違いは残す
        _, report = _mapped("多角形\n色=ffffff\n頂点数=3\n座標=0,だめ")
        assert any("頂点数と座標の数が合わない" in line for line in report.lines())

    def test_an_escaped_n_is_not_the_total_format(self) -> None:
        # 逃がした（文字としての）n まで拾うと、時計の書式が 0 で止まらなくなる
        values = {"_seconds": 0.0, "timer_start": -5.0, "timer_format": "s\\n"}
        assert timer_text(values) == "0n"
