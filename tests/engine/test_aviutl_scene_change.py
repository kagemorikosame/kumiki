"""AviUtl のシーンチェンジ（``.scn``）を場面切り替えのクリップで走らせる（#196）

前の場面をオブジェクト、後の場面をフレームバッファ（``frm``）に入れて走らせ、スクリプトが
``obj.setoption("dst","frm")`` でフレームバッファへ描いた物がその時刻の絵になる 進み具合は
``obj.getvalue("scenechange")`` 向きは配布物 4 本（sigma の ``@ディザσ.scn``）の書かれ方から
読んだ（どれも 0 でオブジェクトをそのまま描き、1 へ向けてオブジェクトを削る）
実物の AviUtl で並べて測ってはいない
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from sashimono.compat.aviutl import catalog as catalog_module
from sashimono.compat.aviutl.catalog import ScriptCatalog, set_script_catalog
from sashimono.compat.aviutl.objapi import ObjApi, ObjectState
from sashimono.compat.aviutl.report import CompatibilityReport, global_report
from sashimono.compat.aviutl.runtime import blank_image
from sashimono.core.model import (
    Clip,
    Effect,
    GeneratedSource,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.core.timebase import FrameRate
from sashimono.effects.sources import TRANSITION
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer

SETTINGS = ProjectSettings(width=64, height=36, frame_rate=FrameRate(30))
RED = (1.0, 0.0, 0.0, 1.0)
BLUE = (0.0, 0.0, 1.0, 1.0)

#: 試験のためのシーンチェンジ どれも ffi を使わない
SCRIPTS = """@薄れる
obj.alpha = 1 - obj.getvalue("scenechange")
obj.setoption("dst","frm")
obj.draw()
@後を写す
obj.copybuffer("obj","frm")
obj.setoption("dst","frm")
obj.draw()
@落ちる
local ffi = require"ffi"
ffi.cast("uint8_t*", 0)
"""


@pytest.fixture(scope="module")
def gl_context() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


@pytest.fixture
def scripts(tmp_path: Path) -> Iterator[ScriptCatalog]:
    root = tmp_path / "scripts"
    root.mkdir()
    (root / "@試し.scn").write_text(SCRIPTS, encoding="cp932")
    saved = catalog_module._catalog
    created = ScriptCatalog(roots=(root,))
    created.scan()
    set_script_catalog(created)
    yield created
    catalog_module._catalog = saved
    if saved is not None:
        saved.register_all()


def _fill(color: tuple[float, float, float, float]) -> GeneratedSource:
    return GeneratedSource(kind="shape", params={"shape": "background", "color": color})


def _project(scripts: ScriptCatalog, label: str) -> Project:
    """赤（0〜30）から青（30〜60）へ シーンチェンジは 20〜40 真ん中で入れ替わる形を既定にする"""
    (entry,) = [e for e in scripts.of_kind("scn") if e.label == label]
    base = Project.create(SETTINGS)
    scenes = Track(
        TrackKind.VIDEO,
        "V1",
        (
            Clip(timeline_start=0, duration=30, source=_fill(RED)),
            Clip(timeline_start=30, duration=30, source=_fill(BLUE)),
        ),
    )
    change = Clip(
        timeline_start=20,
        duration=20,
        source=TRANSITION.create(style="switch"),
        effects=(Effect(kind=entry.identifier, params=entry.definition().default_params()),),
    )
    tracks = (scenes, Track(TrackKind.VIDEO, "V2", (change,)))
    return base.with_timeline(replace(base.timeline, tracks=tracks))


def _render(
    project: Project, context: OffscreenGLContext, *frames: int
) -> tuple[list[np.ndarray], CompatibilityReport]:
    """描いた絵と、描く間に増えた記録 記録はアプリ全体で 1 つなので差だけを返す"""
    before = Counter(global_report.missing)
    renderer = FrameRenderer(project, context=context)
    try:
        drawn = [renderer.render(frame).copy() for frame in frames]
    finally:
        renderer.close()
    return drawn, CompatibilityReport(missing=Counter(global_report.missing) - before)


class TestObjApi:
    def test_the_progress_is_read_by_name(self) -> None:
        # 読めないと 0 が返り、どのシーンチェンジも最後まで前の場面のまま止まる
        state = ObjectState(image=blank_image(4, 4), scenechange=0.25)
        report = CompatibilityReport()
        assert ObjApi(state, report=report).lua_getvalue("scenechange") == 0.25
        assert not report.missing

    def test_drawing_to_the_framebuffer_is_not_counted(self) -> None:
        # フレームバッファへ描くのはふつうの描き方 数えると、配布物 4 本すべてが
        # 描けたのに穴として残る
        state = ObjectState(image=blank_image(4, 4))
        report = CompatibilityReport()
        ObjApi(state, report=report).lua_setoption("dst", "frm")
        assert not report.missing

    def test_the_short_temp_buffer_target_draws_into_the_temp_buffer(self) -> None:
        # sigma_lib は ``obj.setoption("dst","tmp",w,h)`` で仮想バッファへ描いて形を切り抜く
        # （時計のワイプの pizza_cut） 読まないと、切り抜く前の絵が画面へそのまま出る
        state = ObjectState(image=blank_image(4, 4))
        report = CompatibilityReport()
        api = ObjApi(state, report=report)
        api.lua_setoption("dst", "tmp", 8, 6)
        api.lua_draw()
        assert state.draws == []
        assert state.buffers["tmp"].shape == (6, 8, 4)
        assert not report.missing


class TestRender:
    def test_the_old_scene_fades_over_the_new_by_the_progress(
        self, scripts: ScriptCatalog, gl_context: OffscreenGLContext
    ) -> None:
        # 進み具合で前の場面を薄くするスクリプト 0 で赤、真ん中で赤と青が半々、終わり近くで青
        # 走らせないと、真ん中で入れ替えるだけ（20 で赤、38 で青、30 で青）になる
        project = _project(scripts, "薄れる")
        (start, middle, late), report = _render(project, gl_context, 20, 30, 38)
        assert start[18, 32, 0] > 200 and start[18, 32, 2] < 30, start[18, 32]
        assert 60 < middle[18, 32, 0] < 200 and 60 < middle[18, 32, 2] < 200, middle[18, 32]
        assert late[18, 32, 2] > 200 and late[18, 32, 0] < 60, late[18, 32]
        assert "場面切り替えに積んだ AviUtl スクリプト" not in report.missing

    def test_the_framebuffer_is_the_new_scene(
        self, scripts: ScriptCatalog, gl_context: OffscreenGLContext
    ) -> None:
        # フレームバッファを写してオブジェクトとして描くと、後の場面（青）が出る
        # 前の場面（切れ目の手前で止めた赤）をフレームバッファに入れると赤が出て、
        # 配布物の 4 本が後の場面から始まって前の場面で終わる
        project = _project(scripts, "後を写す")
        (drawn,), report = _render(project, gl_context, 35)
        assert drawn[18, 32, 2] > 200 and drawn[18, 32, 0] < 30, drawn[18, 32]
        assert not [line for line in report.missing if "frm" in line or "素通し" in line]

    def test_a_failing_script_just_switches_the_scenes(
        self, scripts: ScriptCatalog, gl_context: OffscreenGLContext
    ) -> None:
        # ffi が要るスクリプト（sigma のディザ 4 本）は走らない 素通しにして記録に残す
        # 失敗したときの絵（前の場面をそのまま）を出すと、終わりまで赤のまま切り替わらない
        project = _project(scripts, "落ちる")
        (early, late), report = _render(project, gl_context, 25, 35)
        assert early[18, 32, 0] > 200 and early[18, 32, 2] < 30, early[18, 32]
        assert late[18, 32, 2] > 200 and late[18, 32, 0] < 30, late[18, 32]
        assert report.missing["シーンチェンジのスクリプトが走らない（素通し）: 落ちる"] == 2
