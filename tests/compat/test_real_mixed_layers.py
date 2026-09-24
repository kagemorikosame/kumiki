"""実物のテンプレートを分ける方式と混合の方式で置き、同じ絵・同じ音になるかを比べる（#27 P6）

``tests/fixtures/ymm4`` と ``tests/fixtures/aviutl`` に置いた配布物を全部通す 配布物なので
リポジトリには入れていない（無ければ飛ばす）

混合の方式はレイヤー番号をそのまま混合トラックの並びにし、動画アイテムを分けずに置く
分ける方式は YMM4 の書き出しと描き比べて合わせてきた（docs/development.md の
「値の意味は本体に描かせて読む」） 2 つが 1 画素・1 標本も違わなければ、混合の方式も
同じ重なり順（レイヤー 1 が一番奥）で同じ音を鳴らしている

素材を参照するテンプレートは、書かれた名前の素材をその場で作って隣に置く
（配布物に素材は入っていない） 書かれたパスが機械にあっても読まない 利用者の手元の
物を試料にすると、機械ごとに答えが変わる

2026-09-24 に数えた結果 500 本のうち中身を置ける 409 本（素材を参照する物 19 本・
音の鳴る物 2 本）を 4 枚ずつ 1636 枚描いて、どれも同じだった 未対応として数えた物は
写す段と置く段を合わせて 99 種 150 回で、2 つの方式で同じ
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path, PureWindowsPath

import numpy as np
import pytest

from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.compat.catalog import Probe, TemplateCatalog, TemplateEntry, gather_media, place
from sashimono.compat.mapped import MappedObject
from sashimono.core.commands import Command
from sashimono.core.model import LayerMode, MediaItem, Project, ProjectSettings, TrackKind
from sashimono.core.timebase import FrameRate
from tests.media_fixtures import ffmpeg_available

ROOT = Path(__file__).resolve().parents[1] / "fixtures"
FILES = [
    path
    for folder in (ROOT / "ymm4", ROOT / "aviutl")
    if folder.is_dir()
    for path in folder.rglob("*")
    if path.suffix.lower() in (".ymmt", ".exa", ".exa2", ".object", ".exo", ".exo2")
]

pytestmark = pytest.mark.skipif(
    not FILES, reason="tests/fixtures に配布物（.ymmt .exa .object）が置かれていない"
)

SETTINGS = ProjectSettings(width=320, height=180, frame_rate=FrameRate(30))

_IMAGES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
_SOUNDS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"}


@pytest.fixture(scope="module")
def entries() -> list[tuple[TemplateEntry, list[MappedObject]]]:
    shelf = TemplateCatalog()
    shelf.scan((ROOT / "ymm4", ROOT / "aviutl"))
    loaded = []
    for entry in shelf.all():
        try:
            loaded.append((entry, entry.load(report=CompatibilityReport())))
        except (ValueError, OSError):
            # 読めない物は test_real_ymm4.py などが見ている ここは置き方の比べだけ
            continue
    return loaded


@pytest.fixture(scope="module")
def near(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("media")


def _make(folder: Path, name: str) -> None:
    """書かれた名前の素材を作る 画像は赤い四角・音は 440Hz・動画は模様と 330Hz"""
    target = folder / name
    if target.exists():
        return
    lavfi = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i"]
    suffix = target.suffix.lower()
    if suffix in _IMAGES:
        command = [*lavfi, "color=c=red:size=64x48", "-frames:v", "1", str(target)]
    elif suffix in _SOUNDS:
        command = [*lavfi, "sine=frequency=440:duration=3", str(target)]
    else:
        command = [
            *lavfi,
            "testsrc=size=96x54:rate=30:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:duration=3",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            str(target),
        ]
    subprocess.run(command, check=True)


def _probe_near(folder: Path) -> Probe:
    from sashimono.engine.decode import ProbeError, probe_media

    def probe(path: Path) -> MediaItem | None:
        # 書かれたパスが機械にあっても、作った方を読む（モジュールの説明）
        try:
            return probe_media(folder / path.name)
        except ProbeError:
            return None

    return probe


def _placed(objects: list[MappedObject], mode: str, near: Path | None) -> Project:
    project = Project.create(replace(SETTINGS, layer_mode=mode))
    commands: list[Command] = []
    media: dict[str, MediaItem] = {}
    if near is not None:
        plan = gather_media(objects, project, _probe_near(near), near=near)
        commands.extend(plan.commands)
        media = dict(plan.media)
    commands.extend(place(objects, project, media=media, report=CompatibilityReport()))
    for command in commands:
        project = command.apply(project)
    return project


def _pairs(
    entries: list[tuple[TemplateEntry, list[MappedObject]]], near: Path | None
) -> Iterator[tuple[str, Project, Project]]:
    for entry, objects in entries:
        if near is not None:
            for item in objects:
                for inner in item.walk():
                    if inner.media_path and inner.clip.source is None:
                        _make(near, PureWindowsPath(inner.media_path).name)
        separated = _placed(objects, LayerMode.SEPARATED, near)
        mixed = _placed(objects, LayerMode.MIXED, near)
        yield entry.label, separated, mixed


@pytest.fixture(scope="module")
def media_folder(near: Path) -> Path | None:
    # ffmpeg が無ければ素材を作らずに比べる 素材を参照するクリップは空のまま置かれる
    return near if ffmpeg_available() else None


def test_mixed_projects_hold_only_layers(
    entries: list[tuple[TemplateEntry, list[MappedObject]]], media_folder: Path | None
) -> None:
    """混合の方式で読み込むと、トラックはどれもレイヤー シーンの中も同じ

    直す前は混合のプロジェクトでも映像トラックと音声トラックを作っていた
    """
    for label, _, mixed in _pairs(entries, media_folder):
        timelines = [mixed.timeline, *(scene.timeline for scene in mixed.scenes)]
        kinds = {track.kind for timeline in timelines for track in timeline.tracks}
        assert kinds <= {TrackKind.MIXED}, label


@pytest.mark.usefixtures("gpu")
def test_both_ways_draw_and_sound_the_same(
    entries: list[tuple[TemplateEntry, list[MappedObject]]], media_folder: Path | None
) -> None:
    """同じテンプレートを 2 つの方式で置くと、同じ絵・同じ音になる

    レイヤーの向きを裏返すと重なりが入れ替わり、動画アイテムの音を落とすか二重にすると
    音が変わる どちらもここで 1 画素・1 標本の違いとして出る
    """
    from sashimono.engine.audio import AudioMixer
    from sashimono.engine.gpu import OffscreenGLContext
    from sashimono.engine.render import FrameRenderer

    context = OffscreenGLContext()
    compared = 0
    try:
        for label, separated, mixed in _pairs(entries, media_folder):
            assert separated.duration == mixed.duration, label
            if separated.duration == 0:
                continue
            frame = separated.duration // 2
            images = []
            for project in (separated, mixed):
                renderer = FrameRenderer(project, context=context)
                try:
                    images.append(renderer.render(frame))
                finally:
                    renderer.close()
            assert np.array_equal(images[0], images[1]), label
            sounds = []
            for project in (separated, mixed):
                mixer = AudioMixer(project)
                try:
                    sounds.append(mixer.render_frames(0, project.duration))
                finally:
                    mixer.close()
            assert np.array_equal(sounds[0], sounds[1]), label
            compared += 1
    finally:
        context.release()
    assert compared > 0
