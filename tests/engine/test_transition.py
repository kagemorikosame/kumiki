"""場面切り替え（YMM4 の ``TransitionItem``）

決まりは YMM4 に描かせた試験から読んだ 前の場面は範囲の中の切れ目の直前で止まり、
後の場面はいまの時刻の絵 進み具合は範囲の頭から終わりまで
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import numpy as np
import pytest

from sashimono.core.commands import (
    AddEffect,
    ParamPath,
    RemoveEffect,
    SetParam,
    resolve_param,
)
from sashimono.core.io import project_from_dict, project_to_dict
from sashimono.core.model import (
    AnimatedValue,
    Clip,
    ClipId,
    GeneratedSource,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
)
from sashimono.core.timebase import FrameRate
from sashimono.effects import registry
from sashimono.effects.easing import ease
from sashimono.effects.sources import TRANSITION
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer

SETTINGS = ProjectSettings(width=64, height=36, frame_rate=FrameRate(30))
RED = (1.0, 0.0, 0.0, 1.0)
BLUE = (0.0, 0.0, 1.0, 1.0)


@pytest.fixture(scope="module")
def gl_context() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


def _fill(color: tuple[float, float, float, float]) -> GeneratedSource:
    return GeneratedSource(kind="shape", params={"shape": "background", "color": color})


def _project(style: str, **params: object) -> Project:
    """赤（0〜30）から青（30〜60）へ 切り替えは 20〜40"""
    base = Project.create(SETTINGS)
    scenes = Track(
        TrackKind.VIDEO,
        "V1",
        (
            Clip(timeline_start=0, duration=30, source=_fill(RED)),
            Clip(timeline_start=30, duration=30, source=_fill(BLUE)),
        ),
    )
    transition = Clip(
        timeline_start=20,
        duration=20,
        source=TRANSITION.create(style=style, **params),  # type: ignore[arg-type]
    )
    tracks = (scenes, Track(TrackKind.VIDEO, "V2", (transition,)))
    return base.with_timeline(replace(base.timeline, tracks=tracks))


def _render(project: Project, context: OffscreenGLContext, frame: int) -> np.ndarray:
    renderer = FrameRenderer(project, context=context)
    try:
        return renderer.render(frame)
    finally:
        renderer.close()


def test_switch_changes_at_the_cut_not_the_middle(gl_context: OffscreenGLContext) -> None:
    project = _project("switch")
    assert _render(project, gl_context, 29)[18, 32, 0] > 200
    assert _render(project, gl_context, 30)[18, 32, 2] > 200


def test_fade_mixes_the_encoded_values(gl_context: OffscreenGLContext) -> None:
    # 真ん中（30）で前と後が半分ずつ YMM4 は sRGB の値のまま混ぜるので 127 前後
    pixel = _render(_project("fade"), gl_context, 30)[18, 32]
    assert abs(int(pixel[0]) - 128) <= 3
    assert abs(int(pixel[2]) - 128) <= 3


def test_before_the_cut_the_fade_shows_the_old_scene(gl_context: OffscreenGLContext) -> None:
    # 切れ目より前は前も後も同じ絵なので、混ぜても変わらない
    pixel = _render(_project("fade"), gl_context, 25)[18, 32]
    assert pixel[0] > 250 and pixel[2] < 5


def test_push_moves_the_old_scene_out_to_the_right(gl_context: OffscreenGLContext) -> None:
    # 3/4 まで進むと、前の場面は画面の 3/4 だけ右へ 左の 3/4 には後の場面が入ってくる
    image = _render(_project("push", angle=0.0), gl_context, 35)
    assert image[18, 60, 0] > 200
    assert image[18, 10, 2] > 200


def test_the_old_scene_lasts_until_the_end(gl_context: OffscreenGLContext) -> None:
    # 赤のクリップは 30 で終わるが、重ねるだけの切り替えでは終わりまで手前に残る
    image = _render(_project("overlay", target="before"), gl_context, 38)
    assert image[18, 32, 0] > 200


def test_effects_go_to_their_own_scene(gl_context: OffscreenGLContext) -> None:
    opacity = registry.get("opacity")
    assert opacity is not None
    project = _project("overlay", target="before")
    track = project.timeline.tracks[1]
    hidden = replace(track.clips[0], effects=(opacity.create(amount=0.0),))
    tracks = (project.timeline.tracks[0], replace(track, clips=(hidden,)))
    project = project.with_timeline(replace(project.timeline, tracks=tracks))
    # 前の場面を消したので、後の場面（青）が見える
    assert _render(project, gl_context, 35)[18, 32, 2] > 200


def test_after_effects_survive_saving() -> None:
    opacity = registry.get("opacity")
    assert opacity is not None
    project = _project("fade")
    track = project.timeline.tracks[1]
    clip = replace(track.clips[0], after_effects=(opacity.create(amount=50.0),))
    tracks = (project.timeline.tracks[0], replace(track, clips=(clip,)))
    project = project.with_timeline(replace(project.timeline, tracks=tracks))
    loaded = project_from_dict(project_to_dict(project))
    restored = loaded.timeline.tracks[1].clips[0]
    assert [e.kind for e in restored.after_effects] == ["opacity"]


@pytest.mark.parametrize("mode", ["in", "out", "inout"])
@pytest.mark.parametrize(
    "kind",
    ["linear", "sine", "quad", "cubic", "quart", "quint", "expo", "circ", "back", "bounce"],
)
def test_easing_starts_at_zero_and_ends_at_one(kind: str, mode: str) -> None:
    assert ease(0.0, kind, mode) == pytest.approx(0.0, abs=1e-3)
    assert ease(1.0, kind, mode) == pytest.approx(1.0, abs=1e-3)


class TestEditingTheScenes:
    """前の場面と後の場面のエフェクトを、別々に積んで触れること"""

    def _clip(self) -> tuple[Project, ClipId]:
        project = _project("fade")
        clip = project.timeline.tracks[1].clips[0]
        return project, clip.id

    def test_effects_go_to_the_stack_you_asked_for(self) -> None:
        project, clip_id = self._clip()
        opacity = registry.get("opacity")
        assert opacity is not None
        project = AddEffect(clip_id, opacity.create(amount=10.0)).apply(project)
        project = AddEffect(clip_id, opacity.create(amount=90.0), after=True).apply(project)
        clip = project.timeline.locate_clip(clip_id)[1]  # type: ignore[index]
        assert [e.kind for e in clip.effects] == ["opacity"]
        assert [e.kind for e in clip.after_effects] == ["opacity"]
        assert clip.after_effects[0].params["amount"].static == 90.0  # type: ignore[union-attr]

    def test_a_parameter_change_finds_the_after_stack(self) -> None:
        project, clip_id = self._clip()
        opacity = registry.get("opacity")
        assert opacity is not None
        effect = opacity.create(amount=90.0)
        project = AddEffect(clip_id, effect, after=True).apply(project)
        path = ParamPath.of_effect(clip_id, effect.id, "amount", after=True)
        project = SetParam(path, AnimatedValue(40.0)).apply(project)
        assert resolve_param(project, path) == AnimatedValue(40.0)

    def test_removing_from_the_after_stack_leaves_the_front_alone(self) -> None:
        project, clip_id = self._clip()
        opacity = registry.get("opacity")
        assert opacity is not None
        front = opacity.create(amount=10.0)
        back = opacity.create(amount=90.0)
        project = AddEffect(clip_id, front).apply(project)
        project = AddEffect(clip_id, back, after=True).apply(project)
        project = RemoveEffect(clip_id, back.id, after=True).apply(project)
        clip = project.timeline.locate_clip(clip_id)[1]  # type: ignore[index]
        assert [e.id for e in clip.effects] == [front.id]
        assert clip.after_effects == ()
