"""点を足しても形を変えない（:meth:`AnimatedValue.with_keyframe_at`）

タイムラインの線の Ctrl+クリックで使う 足しただけで形が変わると、フェードの途中へ点を
打って後半だけ直す、ということができない（打った瞬間に前半まで別の動きになる）
"""

from __future__ import annotations

import pytest

from sashimono.core.model import AnimatedValue, Interpolation, Keyframe


def _fade(first: Keyframe) -> AnimatedValue:
    return AnimatedValue(0.0, (first, Keyframe(60, 1.0), Keyframe(90, 0.3)))


@pytest.mark.parametrize(
    "first",
    [
        Keyframe(0, 0.0),
        Keyframe(0, 0.0, Interpolation.HOLD),
        Keyframe(0, 0.0, Interpolation.EASE_IN),
        Keyframe(0, 0.0, Interpolation.EASE_OUT),
        Keyframe(0, 0.0, Interpolation.EASE_IN_OUT),
        Keyframe(0, 0.0, Interpolation.BEZIER, (0.1, 0.9, 0.3, -0.2)),
    ],
    ids=["linear", "hold", "ease_in", "ease_out", "ease_in_out", "bezier"],
)
@pytest.mark.parametrize("frame", [7, 30, 55])
def test_every_frame_keeps_its_value(first: Keyframe, frame: int) -> None:
    # 新しい点を直線で足すと、瞬間移動は途中から動き出し、イージングは直線に変わる
    before = _fade(first)
    after = before.with_keyframe_at(frame)
    assert frame in [k.frame for k in after.keyframes]
    for sample in range(-5, 100):
        assert after.at(sample) == pytest.approx(before.at(sample), abs=1e-4), sample


@pytest.mark.parametrize("frame", [-3, 95])
def test_outside_the_points_the_value_stays(frame: int) -> None:
    # 端の外は値が止まっている 同じ値の点を足すだけで形は変わらない
    before = _fade(Keyframe(0, 0.0, Interpolation.EASE_IN))
    after = before.with_keyframe_at(frame)
    for sample in range(-10, 100):
        assert after.at(sample) == pytest.approx(before.at(sample), abs=1e-9)


def test_a_frame_that_already_has_a_point_is_left_alone() -> None:
    # 同じ所へ足し直すと、点の出方（イージング）が既定の直線で上書きされる
    before = _fade(Keyframe(0, 0.0, Interpolation.EASE_IN))
    assert before.with_keyframe_at(0) is before


def test_a_static_value_gets_one_point_at_the_same_value() -> None:
    # 別の値で点を打つと、動かしていない線へ点を足しただけで、クリップ全体の値が変わる
    assert AnimatedValue(0.4).with_keyframe_at(12).keyframes == (Keyframe(12, 0.4),)
