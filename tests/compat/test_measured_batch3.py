"""実物の測り 第 3 弾（#210 #216 #195）で測った値の意味

YMM4 は ``tools/ymm4_probes.py`` の 8 回目、AviUtl2 は ``tools/aviutl_filter_probes.py --third``
測った数をここへ定数で持ち、読み込みと描き方がその数を出すかを見る
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import numpy as np
import pytest

from sashimono.compat.aviutl.exo import ExoEntry
from sashimono.compat.aviutl.mapping import _content, _filter
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.ymm4.decorations import map_video_effects
from sashimono.core.model import (
    AnimatedValue,
    Clip,
    Effect,
    GeneratedSource,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.core.timebase import FrameRate
from sashimono.effects import registry
from sashimono.effects.sources import PREVIOUS_OBJECT, SHAPE
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer

WIDTH, HEIGHT = 400, 400


@pytest.fixture(scope="module")
def gl() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


def _render(gl: OffscreenGLContext, *clips: Clip, frame: int = 0) -> np.ndarray:
    """1 本ずつ別のトラックに置いたクリップを下から重ねて描く（400x400・30fps）"""
    project = Project.create(ProjectSettings(width=WIDTH, height=HEIGHT, frame_rate=FrameRate(30)))
    tracks = tuple(Track(kind=TrackKind.VIDEO, clips=(clip,)) for clip in clips)
    project = project.with_timeline(project.timeline.__class__(rate=project.rate, tracks=tracks))
    renderer = FrameRenderer(project, context=gl)
    try:
        return np.asarray(renderer.render(frame))[..., :3]
    finally:
        renderer.close()


def _square(size: int = 100) -> GeneratedSource:
    return SHAPE.create(shape="rect", width=size, height=size, color=(1.0, 1.0, 1.0, 1.0))


def _placed(x: float = 0.0, y: float = 0.0, scale: float = 100.0) -> Effect:
    """クリップが最初から持つ配置の欄（印付き）"""
    effect = registry.require("transform").create(pos_x=x, pos_y=y, scale=scale)
    return replace(effect, fixed=True)


def _box(image: np.ndarray, threshold: int = 128) -> tuple[int, int, int, int] | None:
    lit = image.max(axis=2) >= threshold
    rows = np.nonzero(lit.any(axis=1))[0]
    columns = np.nonzero(lit.any(axis=0))[0]
    if rows.size == 0:
        return None
    return int(columns[0]), int(rows[0]), int(columns[-1]) + 1, int(rows[-1]) + 1


def _still(amount: float) -> dict[str, Any]:
    return {"Values": [{"Value": amount}], "Span": 0.0, "AnimationType": "なし"}


class TestEnterFromOutside:
    """YMM4 の画面外から登場（#216 の 2） 右から直線 2 秒で入る 640 の四角"""

    def test_it_starts_a_screen_width_away(self, gl: OffscreenGLContext) -> None:
        # YMM4 は真ん中のコマ（残り 0.517）で 992 ずれていた 1920 x 0.517 絵の端から
        # 数えると 1280 x 0.517 = 662 しかずれず、画面外から跳ねて登場 が早く入ってくる
        # ここは 400 の画面に 100 の四角 60 フレームで入る登場の 30 フレーム目（残り半分）
        enter = registry.require("inout_move").create(
            direction="right", effect_in=True, effect_time=2.0, easing="linear"
        )
        clip = Clip(timeline_start=0, duration=60, source=_square(), effects=(enter,))
        box = _box(_render(gl, clip, frame=30))
        assert box is not None
        # 真ん中（150〜250）から画面の幅の半分（200）右 右の端は画面で切れる
        assert box[0] == pytest.approx(350, abs=2)


class TestNoiseDisplacement:
    def test_the_threshold_is_recorded_and_not_cut(self) -> None:
        # 小さい値を 0 に切っていたころは、ずれがしきい値の所で跳んで横の帯になった
        # （水の中風 #210） YMM4 のずれはしきい値 30 でも 0 にならなかった
        entry = {
            "$type": "N.NoiseDisplacementMapEffect, YukkuriMovieMaker",
            "IsEnabled": True,
            "Mode": "Move",
            "Transform": {"XScale": _still(0.0), "YScale": _still(200.0)},
            "NoiseType": "Perlin",
            "NoiseParameter": {"Threshold": _still(30.0), "Levels": _still(256.0)},
        }
        report = CompatibilityReport()
        (effect,) = map_video_effects([entry], report).effects
        threshold = effect.params["threshold"]
        assert isinstance(threshold, AnimatedValue)
        assert threshold.static == 0.0
        assert any("しきい値" in line for line in report.lines())

    def test_four_levels_move_in_three_steps(self, gl: OffscreenGLContext) -> None:
        # YMM4 は段階 4 でずれが -67・0・66 の 3 つだった（移動量 200 の半分 x -2/3・0・2/3）
        # 前の刻みは -0.5〜0.5 の 5 つで、0 の段が細く、どこかがいつも大きくずれていた
        wobble = registry.require("noise_displacement").create(
            amount_x=0.0, amount_y=200.0, levels=4.0, strength=100.0, scale_x=400.0, scale_y=400.0
        )
        line = SHAPE.create(shape="rect", width=380, height=2, color=(1.0, 1.0, 1.0, 1.0))
        image = _render(gl, Clip(timeline_start=0, duration=30, source=line, effects=(wobble,)))
        lit = image.max(axis=2) >= 128
        rows = sorted(
            {int(np.nonzero(lit[:, x])[0].mean()) for x in range(20, 380) if lit[:, x].any()}
        )
        # 段ごとのずれは 200 x 0.4 x 2/3 ≈ 53 画素離れる 3 段より多くは出ない
        groups: list[list[int]] = []
        for row in rows:
            if groups and row - groups[-1][-1] <= 2:
                groups[-1].append(row)
            else:
                groups.append([row])
        assert 1 <= len(groups) <= 3


class TestLensBlurFixedSize:
    def test_the_square_keeps_its_edge(self, gl: OffscreenGLContext) -> None:
        # AviUtl2 はサイズ固定のレンズブラー（範囲 30）で白い四角 300 の端が薄れず、300 のままだった
        # 固定を読まないと、端が 10 画素ほど暗くなって外へにじむ
        effect = _filter(
            ExoEntry(
                name="レンズブラー", params={"範囲": "30", "光の強さ": "0", "サイズ固定": "1"}
            ),
            (),
            CompatibilityReport(),
        )
        assert effect is not None
        image = _render(
            gl, Clip(timeline_start=0, duration=30, source=_square(200), effects=(effect,))
        )
        assert _box(image, 250) == pytest.approx((100, 100, 300, 300), abs=1)


class TestAviUtlFramebuffer:
    def test_the_empty_part_stays_transparent(self, gl: OffscreenGLContext) -> None:
        # AviUtl2 は画面の写しを半分に縮めて上へ置いても、写しの何も無い所が下の四角を
        # 隠さなかった（#195 の探り po05） 黒で写すと、下の四角が写しの黒に隠れる
        source, _, _ = _content(
            ExoEntry(name="フレームバッファ", params={}), (), CompatibilityReport()
        )
        assert source is not None
        square = Clip(timeline_start=0, duration=30, source=_square(), effects=(_placed(-100.0),))
        grab = Clip(
            timeline_start=0, duration=30, source=source, effects=(_placed(0.0, 50.0, 50.0),)
        )
        image = _render(gl, square, grab)
        # 下の四角（列 50〜150 行 150〜250）は写しの範囲（列 100〜300 行 50〜250）と重なる
        assert int(image[200, 120].max()) == 255

    def test_the_previous_object_copies_what_was_grabbed(self, gl: OffscreenGLContext) -> None:
        # 下のフレームバッファを直前オブジェクトで写すと、AviUtl2 は写し取ったときの絵（下の四角）
        # だけを元の大きさで自分の位置に出した 描き直すと、フレームバッファが置いた縮めた写しまで
        # 写し取って入る
        source, _, _ = _content(
            ExoEntry(name="フレームバッファ", params={}), (), CompatibilityReport()
        )
        assert source is not None
        square = Clip(timeline_start=0, duration=30, source=_square(), effects=(_placed(-100.0),))
        grab = Clip(
            timeline_start=0, duration=30, source=source, effects=(_placed(0.0, 125.0, 50.0),)
        )
        copy = Clip(
            timeline_start=0,
            duration=30,
            source=PREVIOUS_OBJECT.create(),
            effects=(_placed(100.0, -100.0),),
        )
        image = _render(gl, square, grab, copy)
        # 写しは下の四角を右へ 100、下へ 100 動かした所（列 150〜250 行 250〜350）にだけ出る
        assert int(image[300, 200].max()) == 255
        lit = image.max(axis=2) >= 128
        # 光るのは下の四角・縮めた写し・直前オブジェクトの写し の 3 つだけ
        assert int(lit.sum()) == 100 * 100 * 2 + 50 * 50
