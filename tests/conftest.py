"""テスト全体で使う素材とプロジェクトの組み立て"""

from __future__ import annotations

import functools
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path

import pytest
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtWidgets import QApplication

from kumiki.core.model import (
    AudioStreamInfo,
    Clip,
    MediaItem,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
    Transcript,
    TranscriptSegment,
    VideoStreamInfo,
)
from kumiki.core.timebase import FrameRate
from kumiki.effects import registry
from kumiki.engine.gpu import GLContextError, OffscreenGLContext, preferred_surface_format
from tests.media_fixtures import SampleMedia, ffmpeg_available, make_sample

RATE_30 = FrameRate(30)


@pytest.fixture(scope="session", autouse=True)
def qt_application() -> Iterator[QApplication]:
    """テスト全体で 1 つだけ QApplication を用意する

    ウィジェットには QApplication が要るが、GL のテストが先に走ると
    QGuiApplication だけが作られ、あとから QApplication を作れなくなる
    （Qt の制約） ここで最初に上位の QApplication を作っておけば、
    どちらのテストも同じインスタンスを使える
    """
    existing = QApplication.instance()
    application = existing if isinstance(existing, QApplication) else QApplication([])
    QSurfaceFormat.setDefaultFormat(preferred_surface_format())
    yield application


@pytest.fixture(autouse=True, scope="module")
def forget_scripts() -> Iterator[None]:
    """テストが登録した AviUtl スクリプトを、モジュールごとに片付ける

    スクリプトの定義はエフェクトの登録簿というアプリ全体の状態に入る
    残したままにすると、別のテストが「シェーダの無いエフェクト」を見つけて
    落ちる
    """
    yield
    for definition in registry.all():
        if definition.kind.startswith("aviutl:"):
            registry.unregister(definition.kind)


@pytest.fixture
def video_media() -> MediaItem:
    """10 秒の 1080p30 素材 映像 1 本と音声 1 本を持つ"""
    return MediaItem(
        path=Path("C:/素材/本編.mp4"),
        duration=Fraction(10),
        video_streams=(
            VideoStreamInfo(
                index=0,
                width=1920,
                height=1080,
                frame_rate=RATE_30,
                time_base=Fraction(1, 15360),
                codec="h264",
                pixel_format="yuv420p",
            ),
        ),
        audio_streams=(
            AudioStreamInfo(
                index=1,
                sample_rate=48000,
                channels=2,
                time_base=Fraction(1, 48000),
                codec="aac",
            ),
        ),
    )


@pytest.fixture
def audio_media() -> MediaItem:
    """30 秒の BGM"""
    return MediaItem(
        path=Path("C:/素材/bgm.wav"),
        duration=Fraction(30),
        audio_streams=(
            AudioStreamInfo(
                index=0,
                sample_rate=48000,
                channels=2,
                time_base=Fraction(1, 48000),
                codec="pcm_s16le",
            ),
        ),
    )


@pytest.fixture
def transcript() -> Transcript:
    """3 つの発話区間を持つ起こし結果 時刻はすべてソース秒"""
    return Transcript(
        segments=(
            TranscriptSegment(start=Fraction(1), end=Fraction(3), text="今日は"),
            TranscriptSegment(start=Fraction(4), end=Fraction(6), text="編集ソフトを"),
            TranscriptSegment(start=Fraction(7), end=Fraction(9), text="作ります"),
        ),
        language="ja",
        model="large-v3",
    )


@pytest.fixture
def video_track() -> Track:
    return Track(kind=TrackKind.VIDEO, name="V1")


@pytest.fixture
def audio_track() -> Track:
    return Track(kind=TrackKind.AUDIO, name="A1")


@pytest.fixture
def project(video_media: MediaItem, video_track: Track) -> Project:
    """素材 1 つと空の映像トラック 1 本を持つプロジェクト"""
    base = Project.create(ProjectSettings(frame_rate=RATE_30), media=(video_media,))
    from dataclasses import replace

    return base.with_timeline(replace(base.timeline, tracks=(video_track,)))


def make_clip(start: int, duration: int, media: MediaItem, source_in: int = 0) -> Clip:
    """テスト用のクリップを手短に作る"""
    return Clip(
        timeline_start=start,
        duration=duration,
        media_id=media.id,
        source_in=Fraction(source_in),
    )


@functools.cache
def gpu_available() -> bool:
    """OpenGL 4.3 が本当に使えるか 1 セッションで 1 度だけ確かめる

    GPU の無い環境（CI など）でも Qt はコンテキストを「作れて」しまう
    :class:`OffscreenGLContext` は作った直後に関数が呼べるかまで確かめて
    :class:`GLContextError` を出すので、それを見て判断する

    書き出しのように**内部で**コンテキストを作るテストは、自前の ``gl``
    フィクスチャを持たない そういうテストはこれで飛ばす
    """
    try:
        context = OffscreenGLContext()
    except GLContextError:
        return False
    context.release()
    return True


@pytest.fixture
def gpu() -> None:
    """GPU が要るテストに付ける 無い環境では失敗ではなく飛ばす

    ``pytestmark = pytest.mark.usefixtures("gpu")`` でモジュールごと付けられる
    """
    if not gpu_available():
        pytest.skip("OpenGL 4.3 が使えない（GPU ドライバが無い環境）")


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """生成した素材を置く場所 セッション内で使い回す"""
    if not ffmpeg_available():
        pytest.skip("ffmpeg が PATH に無いので実素材のテストを飛ばす")
    return tmp_path_factory.mktemp("media")


@pytest.fixture(scope="session")
def sample_av(media_dir: Path) -> SampleMedia:
    """映像 + 音声、320x240 / 30fps / 2 秒"""
    return make_sample(media_dir, "av.mp4")


@pytest.fixture(scope="session")
def sample_long(media_dir: Path) -> SampleMedia:
    """4 秒・GOP 12 の映像のみ素材 シークが GOP をまたぐ様子を見るため"""
    return make_sample(
        media_dir,
        "long.mp4",
        duration=4.0,
        audio=False,
        keyframe_interval=12,
    )


@pytest.fixture(scope="session")
def sample_ntsc(media_dir: Path) -> SampleMedia:
    """29.97fps の素材 分数フレームレートの扱いを確かめるため"""
    return make_sample(media_dir, "ntsc.mp4", fps="30000/1001", duration=2.0)
