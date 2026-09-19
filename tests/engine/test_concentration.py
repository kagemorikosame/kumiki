"""集中線の描き方

AviUtl2 に ``中心幅`` と ``濃さ`` を変えた 3 本を描かせ、その絵を測って
合わせた ここが落ちたときに疑うのは実測の値ではなく、描き方の側
"""

from __future__ import annotations

import numpy as np
import pytest

from kumiki.core.model import AnimatedValue, GeneratedSource
from kumiki.engine.sources import render_source

WIDTH, HEIGHT = 960, 540


def _drawn(**params: object) -> np.ndarray:
    """黒い背景に重ねた明るさ 透けたぶんまで見たいので、不透明度を掛ける"""
    values: dict[str, object] = {"shape": "concentration", "color": (1.0, 1.0, 1.0, 1.0)}
    values.update(params)
    image = render_source(GeneratedSource(kind="shape", params=values), WIDTH, HEIGHT)  # type: ignore[arg-type]
    assert image is not None
    lit = image[:, :, :3].max(axis=2).astype(np.float32)
    return lit * (image[:, :, 3].astype(np.float32) / 255.0)


def _radius(image: np.ndarray) -> np.ndarray:
    rows, columns = np.mgrid[0:HEIGHT, 0:WIDTH]
    distance: np.ndarray = np.hypot(columns - WIDTH / 2.0, rows - HEIGHT / 2.0)
    return distance


@pytest.fixture(scope="module")
def with_gap() -> np.ndarray:
    return _drawn(
        center_gap=AnimatedValue(120.0),
        density=AnimatedValue(64.0),
        line_thickness=AnimatedValue(33.0),
        flicker=AnimatedValue(0.0),
    )


def test_the_middle_is_empty(with_gap: np.ndarray) -> None:
    # 空きの中に線が入ると、AviUtl の集中線の抜けた真ん中が潰れる
    inside = with_gap[_radius(with_gap) < 110.0]
    assert inside.max() <= 8.0


def test_the_lines_reach_the_edge(with_gap: np.ndarray) -> None:
    # 画面の端まで届かないと、小さな円が真ん中に浮いただけの絵になる
    assert with_gap[:, 0].max() > 8.0
    assert with_gap[:, -1].max() > 8.0
    assert with_gap[0, :].max() > 8.0
    assert with_gap[-1, :].max() > 8.0


def test_one_line_is_not_pure_white(with_gap: np.ndarray) -> None:
    # 実物は線 1 枚ぶんが 255 中の 67 ほど 不透明で描くと、
    # 同じ本数でも真っ白なギラついた絵になる
    lit = with_gap[with_gap > 8.0]
    assert lit.size > 0
    assert float(np.median(lit)) < 140.0


def test_a_thicker_line_covers_more(with_gap: np.ndarray) -> None:
    # 太さが効かないと、濃さを上げても画面が埋まらない
    thick = _drawn(
        center_gap=AnimatedValue(120.0),
        density=AnimatedValue(64.0),
        line_thickness=AnimatedValue(200.0),
        flicker=AnimatedValue(0.0),
    )
    ring = _radius(with_gap) > 200.0
    assert (thick[ring] > 8.0).mean() > (with_gap[ring] > 8.0).mean() * 1.5


def test_without_a_gap_it_stays_the_ymm4_shape() -> None:
    # 空きを持たない（YMM4 から来た）ものは、大きさの円の中に収まったまま
    # ここが端まで届くようになると、YMM4 の絵が別物になる
    image = _drawn(
        center_gap=AnimatedValue(0.0),
        width=AnimatedValue(200.0),
        density=AnimatedValue(64.0),
        softness=AnimatedValue(0.0),
        flicker=AnimatedValue(0.0),
    )
    assert image[:, 0].max() <= 8.0
    assert image[0, :].max() <= 8.0
