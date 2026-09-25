"""キャラクターの動きのテンプレートが YMM4 と同じ所・同じ形に描かれるか（#205）

ぽよ登場・跳ねるびっくり登場・お辞儀・反復ゆらゆら などは、縮めた平均の差が 0.5 ほどでも、
縮めない絵の縁の差が 45〜51 あった 動きの位置・形・始まる向きがずれていた

期待の数は、YMM4 の書き出し（1920x1080 30fps に 640x360 の四角を置いた物）を 1 コマずつ
測った値 縁の位置は明るさの割合で画素より細かく読む 圧縮の揺れで 0.1〜0.2 画素は動く
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.catalog import place
from sashimono.compat.ymm4.decorations import map_video_effects
from sashimono.compat.ymm4.template import map_template
from sashimono.core.model import AnimatedValue, Project, ProjectSettings
from sashimono.core.timebase import FrameRate
from sashimono.effects.definition import turned_object
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer

WIDTH, HEIGHT = 1920, 1080


def _still(amount: float) -> dict[str, Any]:
    return {"Values": [{"Value": amount}], "Span": 0.0, "AnimationType": "なし"}


def _box(effects: list[dict[str, Any]], length: int = 60) -> dict[str, Any]:
    """画面の真ん中の 640x360 の四角（YMM4 と比べたときの下地と同じ）"""
    brush = {
        "$type": "YukkuriMovieMaker.Plugin.Brush.SolidColorBrushParameter, YukkuriMovieMaker",
        "Color": "#FFE08A2C",
    }
    return {
        "$type": "YukkuriMovieMaker.Project.Items.ShapeItem, YukkuriMovieMaker",
        "ShapeType2": "N.QuadrilateralShapePlugin, YukkuriMovieMaker",
        "ShapeParameter": {
            "SizeMode": "WidthHeight",
            "Width": _still(640.0),
            "Height": _still(360.0),
            "Brush": {"Type": "N.SolidColorBrushPlugin, YukkuriMovieMaker", "Parameter": brush},
        },
        "VideoEffects": effects,
        "Length": length,
        "Frame": 0,
        "Layer": 0,
    }


def _effect(name: str, **fields: Any) -> dict[str, Any]:
    return {"$type": f"N.{name}, YukkuriMovieMaker", "IsEnabled": True, **fields}


def _jump_in(
    height: float, stretch: float, period: float, distortion: float, interval: float
) -> dict[str, Any]:
    # 跳ねるびっくり登場 と ぽよ登場 の値の並び（登場だけ 横と縦のずれは 0）
    return _effect(
        "InOutJumpEffect",
        IsInEffect=True,
        IsOutEffect=False,
        EffectTimeSeconds=period + interval,
        JumpHeight=height,
        Stretch=stretch,
        Period=period,
        Distortion=distortion,
        Interval=interval,
        X=0.0,
        Y=0.0,
    )


def _frames(item: dict[str, Any], frames: list[int]) -> list[np.ndarray]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    try:
        settings = ProjectSettings(width=WIDTH, height=HEIGHT, frame_rate=FrameRate(30))
        project = Project.create(settings)
        for command in place(map_template([item], report=CompatibilityReport()), project):
            project = command.apply(project)
        renderer = FrameRenderer(project, context=context)
        try:
            return [renderer.render(frame) for frame in frames]
        finally:
            renderer.close()
    finally:
        context.release()


def _geometry(picture: np.ndarray) -> tuple[float, float, float]:
    """軸に沿った四角の 幅・高さ・下端（画素、画面の上が 0） 縁は明るさの割合で細かく読む"""
    luma = picture[..., :3].astype(np.float64) @ np.array([0.299, 0.587, 0.114])
    inside = np.median(luma[luma > luma.max() * 0.5])
    share = np.clip(luma / inside, 0.0, 1.0)
    rows, columns = np.nonzero(share > 0.5)
    middle_y, middle_x = int(rows.mean()), int(columns.mean())
    down = share[:, middle_x - 100 : middle_x + 100].mean(axis=1)
    across = share[middle_y - 60 : middle_y + 60, :].mean(axis=0)
    top = middle_y - down[:middle_y].sum()
    bottom = middle_y + down[middle_y:].sum()
    left = middle_x - across[:middle_x].sum()
    right = middle_x + across[middle_x:].sum()
    return float(right - left), float(bottom - top), float(bottom)


def _angle(picture: np.ndarray) -> float:
    """絵の傾き（度 画面で時計回りが正） 2 次の積率から読む"""
    luma = picture[..., :3].astype(np.float64) @ np.array([0.299, 0.587, 0.114])
    weight = np.clip(luma / luma.max(), 0.0, 1.0)
    ys, xs = np.mgrid[0 : weight.shape[0], 0 : weight.shape[1]]
    total = weight.sum()
    cx, cy = (weight * xs).sum() / total, (weight * ys).sum() / total
    mu20 = (weight * (xs - cx) ** 2).sum() / total
    mu02 = (weight * (ys - cy) ** 2).sum() / total
    mu11 = (weight * (xs - cx) * (ys - cy)).sum() / total
    return float(0.5 * math.degrees(math.atan2(2 * mu11, mu20 - mu02)))


@pytest.mark.usefixtures("gpu")
class TestJumpIn:
    """跳ねて登場（InOutJump）の 1 周期 値は 跳ねるびっくり登場 の物"""

    def test_it_rests_at_the_first_frame_stretches_in_the_air_and_squashes_after_landing(
        self,
    ) -> None:
        # 前は周期の頭（地面にいる間）で潰し、宙では伸ばさず、着いてからは潰さなかった
        item = _box([_jump_in(40.0, 1.0, 0.3, 1.5, 0.3)])
        rest, flying, landed, done = (_geometry(p) for p in _frames(item, [0, 4, 13, 18]))
        assert rest == pytest.approx((640.0, 360.0, 720.0), abs=0.2)
        # YMM4 は 幅 634.17 高さ 363.76 下端 682.50 幅は 0.5 画素足りない（読めた決まりの限り）
        assert flying[0] == pytest.approx(634.17, abs=0.7)
        assert flying[1] == pytest.approx(363.76, abs=0.3)
        assert flying[2] == pytest.approx(682.50, abs=0.3)
        # YMM4 は 幅 649.78 高さ 354.75 下端 720.07 着いた所（下端）を支点に潰れる
        assert landed == pytest.approx((649.78, 354.75, 720.07), abs=0.3)
        assert done == pytest.approx((640.0, 360.0, 720.0), abs=0.2)

    def test_a_smaller_stretch_matches_poyo(self) -> None:
        """ぽよ登場（伸び縮み 0.8 1 回 0.24 秒 間隔 0.14 秒）"""
        item = _box([_jump_in(25.0, 0.8, 0.24, 1.1, 0.14)])
        flying, pressed = (_geometry(p) for p in _frames(item, [4, 9]))
        # YMM4 は 幅 634.93 高さ 362.92 下端 696.82 と 幅 646.93 高さ 356.12 下端 719.98
        assert flying == pytest.approx((634.93, 362.92, 696.82), abs=0.3)
        assert pressed == pytest.approx((646.93, 356.12, 719.98), abs=0.3)


@pytest.mark.usefixtures("gpu")
def test_the_repeated_jump_rests_at_every_cycle_start() -> None:
    """跳ねる（JumpEffect）も周期の頭は元の形 前は周期の頭から伸ばしていた"""
    jump = _effect(
        "JumpEffect",
        JumpHeight=_still(40.0),
        Stretch=_still(1.0),
        Period=_still(0.53),
        Distortion=_still(0.6),
        Interval=_still(0.07),
        X=_still(0.0),
        Y=_still(0.0),
    )
    start, next_start, pressed = (_geometry(p) for p in _frames(_box([jump]), [0, 18, 17]))
    assert start == pytest.approx((640.0, 360.0, 720.0), abs=0.2)
    assert next_start == pytest.approx((640.0, 360.0, 720.0), abs=0.2)
    # YMM4（画面外から跳ねて登場）は 幅 643.9 高さ 357.9 下端 720.0
    assert pressed == pytest.approx((643.9, 357.9, 720.0), abs=0.3)


@pytest.mark.usefixtures("gpu")
def test_a_centred_in_out_swing_starts_from_the_original_angle() -> None:
    """中央揃えの InOut の反復回転は元の角度から振れ始める（反復ゆらゆら）

    前は振れ切った角度から始まり、動きが 4 分の 1 周期ずれていた
    """
    swing = _effect(
        "RepeatRotateEffect",
        Is3D=False,
        EasingType="Sine",
        EasingMode="InOut",
        IsCentering=True,
        X=_still(0.0),
        Y=_still(0.0),
        Z=_still(5.0),
        Span=_still(1.5),
    )
    first, fifth, quarter = (_angle(p) for p in _frames(_box([swing]), [0, 5, 11]))
    # YMM4 は 0.00 度・1.61 度・2.50 度
    assert first == pytest.approx(0.0, abs=0.05)
    assert fifth == pytest.approx(1.61, abs=0.05)
    assert quarter == pytest.approx(2.50, abs=0.05)


def test_the_zoom_effect_carries_its_horizontal_rate() -> None:
    """拡大率の ZoomX を読む 前は読まず、お辞儀(120F) の横 105% が掛からなかった"""
    report = CompatibilityReport()
    entry = _effect(
        "ZoomEffect",
        Zoom=_still(100.0),
        ZoomX={
            "Values": [{"Value": 100.0}, {"Value": 105.0}],
            "Span": 0.0,
            "AnimationType": "直線移動",
        },
        ZoomY=_still(98.0),
    )
    (transform,) = map_video_effects([entry], report, length=60).effects
    horizontal = transform.params["scale_x"]
    assert isinstance(horizontal, AnimatedValue)
    assert horizontal.at(0) == pytest.approx(100.0)
    assert horizontal.at(60) == pytest.approx(105.0)
    vertical = transform.params["scale_y"]
    assert isinstance(vertical, AnimatedValue)
    assert vertical.at(0) == pytest.approx(98.0)


@pytest.mark.usefixtures("gpu")
def test_a_zoom_widens_only_the_horizontal_side() -> None:
    """横の拡大率は横だけに掛かる 縦の比（scale_y）と重ねて縦に掛けない"""
    zoom = _effect("ZoomEffect", Zoom=_still(100.0), ZoomX=_still(110.0), ZoomY=_still(100.0))
    (drawn,) = (_geometry(p) for p in _frames(_box([zoom]), [0]))
    assert drawn[:2] == pytest.approx((704.0, 360.0), abs=0.3)


def test_a_turned_object_is_the_square_around_its_diagonal() -> None:
    """回しても収まる範囲は、対角線を直径とする円を囲む正方形 中心は動かない"""
    left, bottom, right, top = turned_object((640.0, 360.0, 1280.0, 720.0))
    half = math.hypot(640.0, 360.0) / 2.0
    assert (left + right) / 2.0 == pytest.approx(960.0)
    assert (bottom + top) / 2.0 == pytest.approx(540.0)
    assert right - left == pytest.approx(2.0 * half)
    assert top - bottom == pytest.approx(2.0 * half)


@pytest.mark.usefixtures("gpu")
def test_a_squash_after_a_spiral_presses_from_the_turned_square() -> None:
    """渦巻きの後ろの跳ねて登場は、回しても収まる正方形の下端を支点に潰れる（お辞儀(120F)）

    YMM4 は潰れた四角の下端が 722.3 まで下がった 四角の下端を支点にすると 719.5 で止まる
    """
    spiral = _effect("SpiralTransformEffect", IsRotateOuter=True, Angle=_still(0.0))
    item = _box([spiral, _jump_in(14.0, 1.0, 0.3, 1.5, 0.3)], length=120)
    (pressed,) = (_geometry(p) for p in _frames(item, [13]))
    assert pressed[2] == pytest.approx(720.0 + 187.2 * 0.015 * math.sin(math.pi * 4 / 9), abs=0.2)
