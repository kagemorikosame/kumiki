"""YMM4 の線の図形の塗りに模様のブラシを置いたときの描き方（#199）

YMM4（Direct2D）は、閉じた線の中を塗りのブラシで塗り、その上へ線を線のブラシで引く
こちらの図形は塗りを 1 色でしか描けないので、塗りを目印の色で描いておき、あとから
目印の所だけを模様に替える 目印の見分け方を誤ると、線の色が模様に食われたり、
模様の透明な所から目印の色が透けたりする ここではそれを実際に描いて確かめる
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
import pytest

from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.ymm4.template import map_template
from sashimono.core.model import Clip, Project, ProjectSettings, Track, TrackKind
from sashimono.core.timebase import FrameRate
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer

WIDTH, HEIGHT = 240, 240
#: 四角の線の半分の幅（中心からの画素） 半端にして、線の内側の縁を画素の途中に置く
HALF = 80.5
#: 線の太さ
THICKNESS = 12


def _still(value: float) -> dict[str, Any]:
    return {"Values": [{"Value": value}], "Span": 0.0, "AnimationType": "なし"}


def _solid(colour: str) -> dict[str, Any]:
    return {
        "Type": "YukkuriMovieMaker.Plugin.Brush.SolidColorBrushPlugin, YukkuriMovieMaker",
        "Parameter": {"Color": colour},
    }


def _gradient(first: str, last: str) -> dict[str, Any]:
    """左から右へ first から last へ変わる線形グラデーション 長さは四角の幅と同じ"""
    return {
        "Type": "YukkuriMovieMaker.Brush.LinearGradientBrushPlugin, YukkuriMovieMaker",
        "Parameter": {
            "Stops": [{"Offset": 0.0, "Color": first}, {"Offset": 1.0, "Color": last}],
            "CoordinateMode": "Pixel",
            "Size": _still(HALF * 2.0),
            "Offset": _still(0.0),
            "Angle": _still(0.0),
            "ExtendMode": "Clamp",
        },
    }


def _stripes() -> dict[str, Any]:
    return {
        "Type": "YukkuriMovieMaker.Brush.StripeBrushPlugin, YukkuriMovieMaker",
        "Parameter": {"Color1": "#FF00FF00", "Color2": "#FF0000FF"},
    }


def _square(brush: dict[str, Any], fill: dict[str, Any]) -> dict[str, Any]:
    corners = ((-HALF, -HALF), (HALF, -HALF), (HALF, HALF), (-HALF, HALF))
    return {
        "$type": "YukkuriMovieMaker.Project.Items.ShapeItem, YukkuriMovieMaker",
        "ShapeType2": "YukkuriMovieMaker.Shape.LineShapePlugin, YukkuriMovieMaker",
        "ShapeParameter": {
            "LineType": "Straight",
            "DashStyle": "Solid",
            "Thickness": _still(THICKNESS),
            "LengthRate": _still(100.0),
            "IsClosed": True,
            "Points": [{"X": _still(x), "Y": _still(y)} for x, y in corners],
            "Brush": brush,
            "FillBrush": fill,
        },
        "Frame": 0,
        "Length": 30,
        "Layer": 0,
    }


@pytest.fixture(scope="module")
def gl() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


@pytest.fixture
def draw(gl: OffscreenGLContext) -> Callable[[dict[str, Any]], np.ndarray]:
    """YMM4 のアイテムを 1 つ写して、黒い画面の真ん中に描く"""

    def run(item: dict[str, Any]) -> np.ndarray:
        (mapped,) = map_template([item], report=CompatibilityReport())
        clip: Clip = mapped.clip
        project = Project.create(
            ProjectSettings(width=WIDTH, height=HEIGHT, frame_rate=FrameRate(30))
        )
        track = Track(kind=TrackKind.VIDEO, clips=(clip,))
        project = project.with_timeline(
            project.timeline.__class__(rate=project.rate, tracks=(track,))
        )
        renderer = FrameRenderer(project, context=gl)
        try:
            return renderer.render(0).astype(int)
        finally:
            renderer.close()

    return run


def _at(image: np.ndarray, x: float, y: float) -> list[int]:
    """中心からの画素（Y は上が正）で 1 画素の RGBA を読む"""
    return [int(v) for v in image[int(HEIGHT / 2 - y), int(WIDTH / 2 + x)]]


class TestLineFillPattern:
    def test_a_magenta_line_is_not_eaten_by_the_fill_pattern(
        self, draw: Callable[[dict[str, Any]], np.ndarray]
    ) -> None:
        """線の色がマゼンタでも、線は線の色のまま残り、模様は塗りの中だけに載る

        目印の色をマゼンタに決め打ちすると、マゼンタの線まで塗りの模様に替わる
        """
        image = draw(_square(_solid("#FFFF00FF"), _gradient("#FFFF0000", "#FF0000FF")))
        red, green, blue, _ = _at(image, 0, HALF)
        assert red > 230 and blue > 230 and green < 25, "マゼンタの線が模様に替わった"
        red, green, blue, _ = _at(image, -HALF + 20, 0)
        assert red > 150 and blue < 110 and green < 25, "塗りの左端が模様の始まりの色でない"
        red, green, blue, _ = _at(image, HALF - 20, 0)
        assert blue > 150 and red < 110 and green < 25, "塗りの右端が模様の終わりの色でない"

    def test_a_transparent_fill_pattern_shows_no_marker(
        self, draw: Callable[[dict[str, Any]], np.ndarray]
    ) -> None:
        """模様の色が透明なら、塗りの中は透けて下が見える 目印の色は出ない

        模様の不透明度で目印と混ぜると、透明な模様の所に目印のマゼンタが出る
        """
        image = draw(_square(_solid("#FFFFFFFF"), _gradient("#00FF0000", "#000000FF")))
        red, green, blue, _ = _at(image, 0, 0)
        assert max(red, green, blue) < 10, f"透明な塗りに色が出た: {(red, green, blue)}"
        red, green, blue, _ = _at(image, 0, HALF)
        assert min(red, green, blue) > 230, "白い線が残っていない"

    def test_the_line_keeps_its_colour_at_the_inner_edge(
        self, draw: Callable[[dict[str, Any]], np.ndarray]
    ) -> None:
        """線の内側の縁（塗りとの境）も、線の色と模様の色の間の色になる

        目印からの近さで割合を決めると、縁の中間色の割合がずれて、目印に寄った色が残る
        """
        image = draw(_square(_solid("#FF00FF00"), _gradient("#FF00FF00", "#FF00FF00")))
        # 塗りも線も緑 境目のどこを取っても緑以外の色が混ざらない
        inner = HALF - THICKNESS / 2
        for offset in np.linspace(-2.0, 2.0, 9):
            red, green, blue, _ = _at(image, 0, inner + offset)
            assert red < 25 and blue < 25 and green > 200, f"境目に目印が残った: {offset}"

    def test_both_patterned_is_recorded(self) -> None:
        """線と塗りの両方が模様のときは、塗りを線の模様と分けて描けないので記録に残す"""
        report = CompatibilityReport()
        map_template([_square(_stripes(), _gradient("#FFFF0000", "#FF0000FF"))], report=report)
        assert any("線と塗りの両方" in line for line in report.lines())
