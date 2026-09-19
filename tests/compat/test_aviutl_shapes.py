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
        assert _value(source, "density") == 40.0
        assert _value(source, "flicker") == 25.0

    def test_the_centre_gap_is_recorded(self) -> None:
        # 真ん中の空きに当たる項目が無い 黙って落とすと線が中心まで伸びる
        _, report = _mapped("集中線\n濃さ=40.0\n速さ=25.0\n中心幅=300.0\n色=ffffff")
        assert any("中心幅" in line for line in report.lines())


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

    def test_a_moving_centre_gap_is_recorded(self) -> None:
        # 0 から動く中心幅も「使っている」 記録しないと落としたことに気付けない
        _, report = _mapped("集中線\n濃さ=40\n速さ=25\n中心幅=0,300,直線移動,0\n色=ffffff")
        assert any("中心幅" in line for line in report.lines())

    def test_a_moving_size_is_recorded(self) -> None:
        # 大きさは サイズ と 縦横比 から計算してから渡すので、動きを残せない
        _, report = _mapped("図形\n図形の種類=円\nサイズ=100,400,直線移動,0\n色=ffffff")
        assert any("図形の動くサイズ" in line for line in report.lines())

    def test_a_still_expression_on_a_size_is_recorded(self) -> None:
        # 値が同じでも式なら時間で変わる 記録しないと、大きさが止まったことに気付けない
        _, report = _mapped("図形\n図形の種類=円\nサイズ=100,100,瞬間移動,8|100+time\n色=ffffff")
        assert any("図形の動くサイズ" in line for line in report.lines())
