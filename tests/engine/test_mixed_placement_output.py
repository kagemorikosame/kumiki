"""同じ素材を混合の方式と分ける方式で置いて、同じ絵と音になること（Issue #27）

混合の方式は、絵と音を 1 本のクリップにまとめて置く まとめたときに音のストリームや
音量の固定の項目を取り落とすと、分ける方式で作った作品と見た目か音が変わる
置くところから書き出すところまでを通して、画素と音の値を比べる
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import av
import numpy as np
import pytest

from sashimono.core.commands import Command, insert_generated, insert_media
from sashimono.core.model import (
    GeneratedSource,
    LayerMode,
    Project,
    ProjectSettings,
    TrackKind,
)
from sashimono.core.timebase import FrameRate
from sashimono.engine.audio import AudioMixer
from sashimono.engine.decode import probe_media
from sashimono.engine.encode import ExportSettings, export_project
from sashimono.engine.gpu import GLContextError, OffscreenGLContext
from sashimono.engine.render import FrameRenderer
from tests.media_fixtures import SampleMedia

pytestmark = pytest.mark.usefixtures("gpu")

#: 動画の上に重ねる四角 重なり順が方式で変わると、四角が動画の後ろに隠れる
_SQUARE = GeneratedSource(kind="shape", params={"shape": "rect", "color": (0.0, 1.0, 0.0, 1.0)})


@pytest.fixture(scope="module")
def gl_context() -> Iterator[OffscreenGLContext]:
    try:
        context = OffscreenGLContext()
    except GLContextError as exc:
        pytest.skip(f"OpenGL コンテキストを作れない: {exc}")
    yield context
    context.release()


def _apply(project: Project, commands: list[Command]) -> Project:
    for command in commands:
        project = command.apply(project)
    return project


def _placed(sample: SampleMedia, mode: str) -> Project:
    """``mode`` の方式で、音付きの動画を置き、その上に四角を重ねたプロジェクト"""
    settings = ProjectSettings(width=320, height=240, frame_rate=FrameRate(30), layer_mode=mode)
    project = Project.create(settings)
    project = _apply(project, insert_media(project, probe_media(sample.path)))
    return _apply(project, insert_generated(project, _SQUARE, at_frame=0, duration=60))


@pytest.fixture
def both(sample_av: SampleMedia) -> tuple[Project, Project]:
    separated = _placed(sample_av, LayerMode.SEPARATED)
    mixed = _placed(sample_av, LayerMode.MIXED)
    # 混合の側が本当にレイヤーだけで組まれていることを先に確かめる 分ける方式で
    # 置かれていたら、比べても同じになるのは当たり前で、何も確かめていない
    assert {t.kind for t in mixed.timeline.tracks} == {TrackKind.MIXED}
    assert {t.kind for t in separated.timeline.tracks} == {TrackKind.VIDEO, TrackKind.AUDIO}
    return separated, mixed


def _frames(project: Project, context: OffscreenGLContext, frames: list[int]) -> list[np.ndarray]:
    renderer = FrameRenderer(project, context=context)
    try:
        return [renderer.render(frame) for frame in frames]
    finally:
        renderer.close()


def _sound(project: Project) -> np.ndarray:
    mixer = AudioMixer(project)
    try:
        return mixer.render(0, 48000)
    finally:
        mixer.close()


def test_the_pictures_match(both: tuple[Project, Project], gl_context: OffscreenGLContext) -> None:
    # 四角が動画の後ろへ入る・動画の絵が出ない、のどちらでも画素が食い違う
    separated, mixed = both
    frames = [0, 15, 45]
    rendered = _frames(mixed, gl_context, frames)
    # 真ん中は手前の四角（緑） 両方で四角が隠れていても、比べるだけでは同じに見える
    assert rendered[1][120, 160, 1] > 200 and rendered[1][120, 160, 0] < 60
    for number, (left, right) in enumerate(
        zip(_frames(separated, gl_context, frames), rendered, strict=True)
    ):
        assert np.array_equal(left, right), f"{frames[number]} フレーム目の絵が違う"


def test_the_sound_matches(both: tuple[Project, Project]) -> None:
    # 音のストリームや音量の固定の項目を取り落とすと、無音になるか大きさが変わる
    separated, mixed = both
    left, right = _sound(separated), _sound(mixed)
    assert float(np.abs(left).max()) > 0.01, "分ける方式の側が無音で、比べる意味が無い"
    assert np.allclose(left, right, atol=1e-6)


def test_the_exports_match(both: tuple[Project, Project], tmp_path: Path) -> None:
    # 書き出しだけ別の道（絵を描く判定・音声の有無）を通る 片方だけ音が付かない、を拾う
    outputs = []
    for name, project in zip(("separated", "mixed"), both, strict=True):
        path = tmp_path / f"{name}.mp4"
        export_project(project, ExportSettings(path=path, video_codec="libx264"))
        outputs.append(path)

    def decoded(path: Path) -> tuple[list[np.ndarray], np.ndarray]:
        with av.open(str(path)) as container:
            pictures = [f.to_ndarray(format="rgb24") for f in container.decode(video=0)]
        with av.open(str(path)) as container:
            chunks = [f.to_ndarray() for f in container.decode(audio=0)]
        return pictures, np.concatenate(chunks, axis=1)

    (left_pictures, left_sound), (right_pictures, right_sound) = map(decoded, outputs)
    assert len(left_pictures) == len(right_pictures) > 0
    for number, (a, b) in enumerate(zip(left_pictures, right_pictures, strict=True)):
        assert np.array_equal(a, b), f"{number} 枚目の絵が違う"
    assert left_sound.shape == right_sound.shape
    assert np.array_equal(left_sound, right_sound)
