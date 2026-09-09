"""AviUtl スクリプトが実際に描画へ届くこと。

スクリプトが書き換えた位置・回転・拡大が、GPU の合成結果にそのまま出るかを
画素で見る。ここが通れば、配布されているアニメーション効果がそのまま動く土台が
できている。
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np
import pytest

from novaedit.compat.aviutl.catalog import ScriptCatalog, set_script_catalog
from novaedit.core.commands import AddClip, AddEffect, AddTrack
from novaedit.core.model import (
    Clip,
    GeneratedSource,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from novaedit.core.timebase import FrameRate
from novaedit.effects.definition import registry
from novaedit.engine.gpu import GLContextError, OffscreenGLContext
from novaedit.engine.render import FrameRenderer

#: 位置と回転をスライダーで動かすだけのスクリプト。
MOVE = """--track0:X,-500,500,0,1
--track1:回転,-360,360,0,1
--track2:拡大,10,400,100,1
obj.ox = obj.track0
obj.rz = obj.track1
obj.zoom = obj.track2 / 100
"""

#: 残像。1 回の実行で何枚も描くスクリプトの代表。
TRAIL = """--track0:枚数,1,8,4,1
for i = 0, obj.track0 - 1 do
  obj.draw(i * 30, 0, 0, 1, 1 - i * 0.2)
end
"""

SCREEN = (320, 240)


@pytest.fixture(scope="module")
def gl_context() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


@pytest.fixture(scope="module")
def catalog() -> ScriptCatalog:
    created = ScriptCatalog(roots=())
    created.add_text("aviutl:試験.anm:移動", MOVE)
    created.add_text("aviutl:試験.anm:残像", TRAIL)
    set_script_catalog(created)
    return created


def build(identifier: str, **params: float) -> Project:
    """40x40 の白い四角に、スクリプトを 1 本積んだプロジェクト。"""
    width, height = SCREEN
    project = Project.create(ProjectSettings(width=width, height=height, frame_rate=FrameRate(30)))
    track = Track(kind=TrackKind.VIDEO, name="V1")
    project = AddTrack(track).apply(project)

    shape = GeneratedSource(kind="shape", params={"shape": "rect"})
    clip = Clip(timeline_start=0, duration=30, source=shape)
    # 図形の既定は 400x400。画面より小さくして位置を見やすくする。
    clip = Clip(
        timeline_start=0,
        duration=30,
        source=GeneratedSource(
            kind="shape",
            params={"shape": "rect", "width": 40.0, "height": 40.0},  # type: ignore[dict-item]
        ),
    )
    project = AddClip(track.id, clip).apply(project)

    definition = registry.get(identifier)
    assert definition is not None
    return AddEffect(clip.id, definition.create(**params)).apply(project)


def bounds(image: np.ndarray) -> tuple[int, int, int, int]:
    """明るい画素の外接矩形。``(左, 右, 上, 下)``。"""
    bright = image[..., :3].max(axis=2) > 100
    ys, xs = np.nonzero(bright)
    assert len(xs), "何も描かれていない"
    return int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())


def render(project: Project, context: OffscreenGLContext) -> np.ndarray:
    renderer = FrameRenderer(project, context=context)
    try:
        return renderer.render(0)
    finally:
        renderer.close()


class TestTransform:
    def test_without_a_script_the_shape_is_centred(
        self, gl_context: OffscreenGLContext, catalog: ScriptCatalog
    ) -> None:
        del catalog
        left, right, top, bottom = bounds(render(build("aviutl:試験.anm:移動"), gl_context))
        assert (left + right) // 2 == pytest.approx(SCREEN[0] // 2, abs=2)
        assert (top + bottom) // 2 == pytest.approx(SCREEN[1] // 2, abs=2)

    def test_ox_moves_the_object(
        self, gl_context: OffscreenGLContext, catalog: ScriptCatalog
    ) -> None:
        del catalog
        left, right, _, _ = bounds(render(build("aviutl:試験.anm:移動", track0=80), gl_context))
        assert (left + right) // 2 == pytest.approx(SCREEN[0] // 2 + 80, abs=2)

    def test_rz_rotates_around_the_centre(
        self, gl_context: OffscreenGLContext, catalog: ScriptCatalog
    ) -> None:
        del catalog
        left, right, top, bottom = bounds(
            render(build("aviutl:試験.anm:移動", track1=45), gl_context)
        )
        # 40px の正方形を 45 度回すと、外接矩形は約 56px になる。
        assert right - left == pytest.approx(56, abs=3)
        assert bottom - top == pytest.approx(56, abs=3)
        assert (left + right) // 2 == pytest.approx(SCREEN[0] // 2, abs=2)

    def test_zoom_scales_the_object(
        self, gl_context: OffscreenGLContext, catalog: ScriptCatalog
    ) -> None:
        del catalog
        left, right, _, _ = bounds(render(build("aviutl:試験.anm:移動", track2=200), gl_context))
        assert right - left == pytest.approx(80, abs=3)

    def test_rotation_does_not_leak_into_the_next_draw(
        self, gl_context: OffscreenGLContext, catalog: ScriptCatalog
    ) -> None:
        del catalog
        # 回転を掛けた直後に、掛けていないものを描いても回らないこと。
        render(build("aviutl:試験.anm:移動", track1=30), gl_context)
        left, right, top, bottom = bounds(render(build("aviutl:試験.anm:移動"), gl_context))
        assert right - left == pytest.approx(40, abs=2)
        assert bottom - top == pytest.approx(40, abs=2)


class TestMultipleDraws:
    def test_a_script_can_draw_several_times(
        self, gl_context: OffscreenGLContext, catalog: ScriptCatalog
    ) -> None:
        del catalog
        image = render(build("aviutl:試験.anm:残像", track0=3), gl_context)
        left, right, _, _ = bounds(image)
        # 0, 30, 60 の 3 枚。左端は中央 -20、右端は中央 +60+20。
        assert left == pytest.approx(SCREEN[0] // 2 - 20, abs=2)
        assert right == pytest.approx(SCREEN[0] // 2 + 80, abs=3)

    def test_the_alpha_of_each_draw_is_honoured(
        self, gl_context: OffscreenGLContext, catalog: ScriptCatalog
    ) -> None:
        del catalog
        image = render(build("aviutl:試験.anm:残像", track0=3), gl_context)
        middle = SCREEN[1] // 2
        first = int(image[middle, SCREEN[0] // 2, 0])
        third = int(image[middle, SCREEN[0] // 2 + 60, 0])
        # 後ろの枚ほど薄くなる。
        assert first > third > 0
