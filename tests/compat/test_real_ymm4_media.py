"""配布テンプレートの画像・音声のアイテムを、実際の素材で描いて鳴らす（#71）

あおもや式テンプレート集（``tests/fixtures/ymm4/aomoya``）を置くと走る 配布物なので
リポジトリには入れていない（無ければ飛ばす）

配布物 230 本のうち、素材を参照するのは ``ボイス字幕_9色ポップおこ.ymmt`` の
``画面効果_集中線(黒)`` だけで、画像 1 つと効果音 1 つを一番上の段に持つ
書かれたパスは作者の機械のもの（``C:\\Users\\skki7\\…\\syuutyuu.png``）なので、
同じ名前の素材をその場で作ってテンプレートの隣に置いた形で確かめる
（素材そのものは配られていない）
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.catalog import gather_media, place
from sashimono.compat.mapped import MappedObject
from sashimono.compat.ymm4.template import load_template, map_template
from sashimono.core.model import MediaItem, Project, ProjectSettings, TrackKind
from sashimono.core.timebase import FrameRate
from sashimono.engine.decode import ProbeError, probe_media
from tests.media_fixtures import ffmpeg_available

TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "ymm4"
    / "aomoya"
    / "ボイス字幕_9色ポップおこ.ymmt"
)
NAME = "画面効果/画面効果_集中線(黒)"


def _mapped() -> list[MappedObject]:
    if not TEMPLATE.is_file():
        pytest.skip(f"{TEMPLATE.name} が置かれていない（配布物）")
    template = next(t for t in load_template(TEMPLATE) if t.name == NAME)
    return map_template(list(template.items), report=CompatibilityReport())


def _probe(path: Path) -> MediaItem | None:
    try:
        return probe_media(path)
    except ProbeError:
        return None


@pytest.fixture
def near(tmp_path: Path) -> Path:
    """テンプレートが書いている名前の素材を作って置く 赤い画像と 440Hz の音"""
    if not ffmpeg_available():
        pytest.skip("ffmpeg が PATH に無い")
    ffmpeg = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i"]
    subprocess.run(
        [*ffmpeg, "color=c=red:size=64x64", "-frames:v", "1", str(tmp_path / "syuutyuu.png")],
        check=True,
    )
    subprocess.run(
        [*ffmpeg, "sine=frequency=440:duration=2", str(tmp_path / "coin04.mp3")],
        check=True,
    )
    return tmp_path


def _placed(objects: list[MappedObject], near: Path | None) -> Project:
    project = Project.create(ProjectSettings(width=320, height=180, frame_rate=FrameRate(30)))
    plan = gather_media(objects, project, _probe, near=near)
    for command in [*plan.commands, *place(objects, project, media=plan.media)]:
        project = command.apply(project)
    return project


def test_the_template_names_one_image_and_one_sound() -> None:
    # 数え直したときの前提 ここが変わったら配布物の版が変わっている
    kinds = sorted(item.kind for item in _mapped() if item.media_path)
    assert kinds == ["画像ファイル", "音声ファイル"]


def test_the_image_and_the_sound_become_media(near: Path) -> None:
    """置くと画像と効果音が素材一覧に載り、クリップがそれを指す

    直す前は素材が 0 個で、画像も効果音も ``media_id`` の無いクリップになっていた
    効果音は音声トラックに置く（映像トラックの素材の音は鳴らない）
    """
    project = _placed(_mapped(), near)

    names = {item.path.name: item.id for item in project.media}
    assert sorted(names) == ["coin04.mp3", "syuutyuu.png"]
    by_kind = {track.kind: clip for track in project.timeline.tracks for clip in track.clips}
    assert by_kind[TrackKind.VIDEO].media_id == names["syuutyuu.png"]
    assert by_kind[TrackKind.AUDIO].media_id == names["coin04.mp3"]


def test_placing_it_twice_keeps_two_media(near: Path) -> None:
    # 同じテンプレートを 2 度置いても、画像と効果音の 2 つのまま
    objects = _mapped()
    project = _placed(objects, near)
    later = project.timeline.duration
    plan = gather_media(objects, project, _probe, near=near)
    assert plan.commands == ()
    for command in place(objects, project, at_frame=later, media=plan.media):
        project = command.apply(project)
    assert len(project.media) == 2


def test_the_sound_is_heard(near: Path) -> None:
    """置いた効果音が鳴る 素材を結ばないと、ミキサは何も読まず無音になる"""
    from sashimono.engine.audio import AudioMixer

    heard = _placed(_mapped(), near)
    silent = _placed(_mapped(), None)

    def loudness(project: Project) -> float:
        mixer = AudioMixer(project)
        try:
            samples = mixer.render_frames(0, 20)
        finally:
            mixer.close()
        return float(np.sqrt(np.mean(samples**2)))

    assert loudness(silent) == 0.0
    assert loudness(heard) > 0.01


@pytest.mark.usefixtures("gpu")
def test_the_image_is_drawn(near: Path) -> None:
    """置いた画像が描かれる 素材を結ばないと、画面は透明のまま"""
    from sashimono.engine.gpu import OffscreenGLContext
    from sashimono.engine.render import FrameRenderer

    def red_pixels(project: Project) -> int:
        context = OffscreenGLContext()
        try:
            renderer = FrameRenderer(project, context=context)
            try:
                image = renderer.render(10)
            finally:
                renderer.close()
        finally:
            context.release()
        red = (image[..., 0] > 200) & (image[..., 1] < 60) & (image[..., 3] > 200)
        return int(np.count_nonzero(red))

    assert red_pixels(_placed(_mapped(), None)) == 0
    assert red_pixels(_placed(_mapped(), near)) > 100
