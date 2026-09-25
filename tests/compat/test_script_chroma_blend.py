"""仮想バッファへ 色差 の重ね方で描く ``obj.setoption("blend", 9)``（Issue #212）

sigma の アクリル化・磨りガラス化 は「着色で輝度を保持」が入っていると、板の上へ着色の
色を 色差 で重ねる 下の輝度はそのままに色だけを乗せる物で、通常の重ね方で描くと着色の灰色で
板の明るさまで塗り替わる 前は未対応として記録して通常で描いていた
"""

from __future__ import annotations

import numpy as np

from sashimono.compat.aviutl import raster
from sashimono.compat.aviutl.objapi import ObjectState
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.aviutl.runtime import LuaScriptRuntime

LUMA = np.array([0.299, 0.587, 0.114])


def _luma(pixel: np.ndarray) -> float:
    return float(pixel[:3].astype(float) @ LUMA)


def _cb_cr(pixel: np.ndarray) -> tuple[float, float]:
    rgb = pixel[:3].astype(float)
    y = rgb @ LUMA
    return float(rgb[2] - y), float(rgb[0] - y)


def test_chroma_keeps_the_brightness_below_and_takes_the_colour_above() -> None:
    """下の輝度に上の色差を合わせる

    通常で重ねると、上の色の輝度になって下の明るさが消える
    """
    below = np.array([[[200, 200, 200, 255]]], np.uint8)
    # 下の輝度へ寄せても 0〜255 に収まる色 収まらない分は切れて輝度も下がる
    above = np.array([[[120, 60, 40, 255]]], np.uint8)
    drawn = below.copy()
    raster.draw_image(drawn, above, blend="chroma")
    assert abs(_luma(drawn[0, 0]) - _luma(below[0, 0])) < 1.5
    cb, cr = _cb_cr(drawn[0, 0])
    expected_cb, expected_cr = _cb_cr(above[0, 0])
    assert abs(cb - expected_cb) < 1.5 and abs(cr - expected_cr) < 1.5
    assert drawn[0, 0, 3] == 255


def test_chroma_over_nothing_is_the_colour_above() -> None:
    # 下が透明な所には合わせる輝度が無い 上の色のまま置く 黒として輝度を合わせると真っ黒になる
    drawn = np.zeros((1, 1, 4), np.uint8)
    above = np.array([[[200, 60, 40, 255]]], np.uint8)
    raster.draw_image(drawn, above, blend="chroma")
    assert tuple(drawn[0, 0]) == (200, 60, 40, 255)


def test_the_script_draws_with_chroma_without_a_record() -> None:
    """``obj.setoption("blend", 9)`` の旧形式の番号で色差を選べる 記録には残さない"""
    state = ObjectState(image=np.full((2, 2, 4), 200, np.uint8), screen_w=32, screen_h=18)
    state.image[..., 3] = 255
    report = CompatibilityReport()
    runtime = LuaScriptRuntime(report=report)
    result = runtime.run(
        'obj.setoption("drawtarget", "tempbuffer", 2, 2) obj.draw()'
        " obj.putpixel(0, 0, 0x783c28, 1) obj.putpixel(1, 0, 0x783c28, 1)"
        " obj.putpixel(0, 1, 0x783c28, 1) obj.putpixel(1, 1, 0x783c28, 1)"
        ' obj.setoption("blend", 9) obj.draw(0, 0, 0, 1, 0.5)',
        state,
    )
    assert not result.failed, result.message
    assert not report.missing
    drawn = state.buffers["tmp"][0, 0]
    # 半分の不透明度で重ねても、下の輝度（200）は変わらず色だけが寄る
    assert abs(_luma(drawn) - 200.0) < 1.5
    assert drawn[0] > drawn[2]
