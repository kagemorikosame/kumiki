"""時間で絵が決まる AviUtl2 のカスタムオブジェクト（ライン(移動軌跡) と 星）の読み込み

項目名は AviUtl2 に置かせたエイリアス（``kumiki_p5_line_x`` ``kumiki_p5_star_n30``）から
読み取った 名前に反して、ライン は折れ線ではなく通った跡、星 は星形ではなく星空
"""

from __future__ import annotations

from kumiki.compat.aviutl.exo import parse_exo
from kumiki.compat.aviutl.mapping import map_object
from kumiki.compat.aviutl.report import CompatibilityReport
from kumiki.compat.mapped import MappedObject
from kumiki.core.model import AnimatedValue
from kumiki.core.timebase import FrameRate

LINE = """[Object]
frame=0,80
[Object.0]
effect.name=ライン(移動軌跡)
ライン幅=16.0
先端=48.0
先端角度=0.00
先端位置補正=70.0
先端図形=三角形
固定速度=0.00
描画間隔=10.0
最小間隔=2
主線描画(%)=100.0
補助描画(%)=0.0
色=ffffff
[Object.1]
effect.name=標準描画
X=-600.00,600.00,直線移動,0
Y=-300.00,300.00,直線移動,0
Z=0.00
拡大率=100.000
透明度=0.00
合成モード=通常
"""

STAR = """[Object]
frame=0,80
[Object.0]
effect.name=星
個数=30.0
速度=12.0
広がり=12.0
奥行き=20.0
サイズ=30.0
色=ddddff
形状=円
フェードイン時間=0.15
フェードアウト時間=0.15
[Object.1]
effect.name=標準描画
X=0.00
Y=0.00
合成モード=通常
"""


def _mapped(text: str) -> tuple[MappedObject, CompatibilityReport]:
    report = CompatibilityReport()
    item = map_object(parse_exo(text).objects[0], FrameRate(60), report=report)
    assert item is not None
    return item, report


def _static(value: object) -> float:
    assert isinstance(value, AnimatedValue)
    return value.static


class TestMotionTrail:
    def test_it_becomes_a_trail_that_follows_the_position(self) -> None:
        # 読めないと 中身の無いクリップになり、何も描かれない
        item, report = _mapped(LINE)
        source = item.clip.source
        assert source is not None
        assert source.params["shape"] == "motion_trail"
        x = source.params["pos_x"]
        assert isinstance(x, AnimatedValue)
        assert x.at(0) == -600.0
        assert x.at(80) == 600.0
        y = source.params["pos_y"]
        # Y は上が正へ直す AviUtl の 300（下）はこちらの -300
        assert isinstance(y, AnimatedValue)
        assert y.at(80) == -300.0
        assert not report.missing

    def test_the_position_is_not_applied_twice(self) -> None:
        # 変形にも位置を残すと、軌跡を描いた絵がもう一度ずれて 2 倍の所に出る
        item, _ = _mapped(LINE)
        for effect in item.clip.effects:
            if effect.kind == "transform":
                assert _static(effect.params.get("pos_x", AnimatedValue(0.0))) == 0.0
                assert not effect.params["pos_x"].is_animated  # type: ignore[union-attr]

    def test_the_items_are_read(self) -> None:
        item, _ = _mapped(LINE)
        source = item.clip.source
        assert source is not None
        assert _static(source.params["line_width"]) == 16.0
        assert _static(source.params["trail_head_size"]) == 48.0
        assert _static(source.params["trail_head_offset"]) == 70.0
        assert _static(source.params["trail_interval"]) == 10.0
        # 実物の三角形は円に内接する形（大きさ 48 で高さ 36）
        assert source.params["trail_head_shape"] == "inscribed_triangle"

    def test_an_unknown_figure_is_recorded(self) -> None:
        # 自分で足した図形を黙って三角形にすると、違う先端に気付けない
        _, report = _mapped(LINE.replace("先端図形=三角形", "先端図形=ハート"))
        assert any("ハート" in entry for entry in report.missing)


class TestStarField:
    def test_it_becomes_a_star_field(self) -> None:
        # 星形の図形として読むと、流れない星が 1 つ出るだけになる
        item, report = _mapped(STAR)
        source = item.clip.source
        assert source is not None
        assert source.params["shape"] == "starfield"
        assert _static(source.params["star_count"]) == 30.0
        assert _static(source.params["star_speed"]) == 12.0
        assert _static(source.params["star_spread"]) == 12.0
        assert _static(source.params["star_depth"]) == 20.0
        assert _static(source.params["star_size"]) == 30.0
        assert _static(source.params["star_fade_in"]) == 0.15
        assert source.params["star_shape"] == "ellipse"
        assert source.params["color"][2] == 1.0  # type: ignore[index]
        assert not report.missing
