"""スクリプトの ``obj.effect`` を呼んだ順に掛けること（Issue #176）

AviUtl の ``obj.effect("ぼかし")`` はその場で絵を変える こちらはエフェクトを積んでおき、
描くときに GPU でまとめて掛ける そのままだと、後から来た ``obj.effect("リサイズ")`` のように
その場で絵を変える呼び出しより**後に**掛かり、ぼかしてから広げた絵が、広げてからぼかした
絵と同じになる 壊れると、sigma の 単純図形σ のように 1 画素の絵を効果ごと広げる配布物の
ぼけ幅や縁の太さが、AviUtl の何倍も細く出る
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import numpy as np
import pytest

from sashimono.compat.aviutl.catalog import ScriptCatalog, set_script_catalog
from sashimono.core.model import (
    AnimatedValue,
    Clip,
    GeneratedSource,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.core.timebase import FrameRate
from sashimono.effects import registry
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer

SETTINGS = ProjectSettings(width=96, height=96, frame_rate=FrameRate(30))

#: 8 画素の白い四角を作る AviUtl の図形は 1 回の読み込みで絵の大きさちょうどになる
LOAD = 'obj.load("figure", "四角形", 0xffffff, 8)'
BLUR = 'obj.effect("ぼかし", "範囲", 4)'
RESIZE = 'obj.effect("リサイズ", "拡大率", 400)'


@pytest.fixture(scope="module")
def gl_context() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


def _render(source: str, gl_context: OffscreenGLContext) -> np.ndarray:
    """``source`` を積んだクリップを 1 枚描く 画面は黒 絵は真ん中"""
    catalog = ScriptCatalog(roots=())
    catalog.add_text("aviutl:試験.anm:順", source)
    set_script_catalog(catalog)
    definition = registry.get("aviutl:試験.anm:順")
    assert definition is not None
    clip = Clip(
        timeline_start=0,
        duration=30,
        source=GeneratedSource(
            kind="shape",
            params={"shape": "rect", "width": AnimatedValue(8.0), "height": AnimatedValue(8.0)},
        ),
        effects=(definition.create(),),
    )
    project = Project.create(SETTINGS)
    track = Track(TrackKind.VIDEO, "V1", (clip,))
    project = replace(project, timeline=replace(project.timeline, tracks=(track,)))
    renderer = FrameRenderer(project, context=gl_context)
    try:
        return renderer.render(5)
    finally:
        renderer.close()


def _row(image: np.ndarray) -> np.ndarray:
    """真ん中の行の明るさ（赤） 四角は白なので 1 つの色で足りる"""
    return image[48, :, 0].astype(int)


class TestOrder:
    def test_blur_then_resize_widens_the_blur(self, gl_context: OffscreenGLContext) -> None:
        # ぼかしてから 4 倍にすると、ぼけの幅も 4 倍になる 広げた後にぼかすと元のぼけ幅のまま
        # 同じ絵になるなら、先に積んだぼかしがリサイズの後へ回っている
        blurred_first = _row(_render(f"{LOAD} {BLUR} {RESIZE}", gl_context))
        resized_first = _row(_render(f"{LOAD} {RESIZE} {BLUR}", gl_context))
        assert not np.array_equal(blurred_first, resized_first)
        # 四角は 32..64 縁から 6 画素内側は、広げてからぼかせばほぼ白、
        # ぼかしてから広げれば 4 倍に広がったぼけの中で暗い
        assert resized_first[38] > 240
        assert blurred_first[38] < resized_first[38] - 40
        # 縁から 6 画素外側も、ぼかしてから広げた方だけ明るさが届く
        assert resized_first[26] < 16
        assert blurred_first[26] > resized_first[26] + 16

    def test_resize_then_blur_is_unchanged(self, gl_context: OffscreenGLContext) -> None:
        # リサイズの後に積んだぼかしは、これまでどおり描くときに掛かる 広げた四角の真ん中は白、
        # 四角から遠い所は黒 焼き込みの仕組みを入れても、こちらの順は変わらない
        row = _row(_render(f"{LOAD} {RESIZE} {BLUR}", gl_context))
        assert row[48] > 250
        assert row[4] < 4
