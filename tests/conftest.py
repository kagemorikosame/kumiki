"""テスト全体で使う素材とプロジェクトの組み立て。"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from novaedit.core.model import (
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
from novaedit.core.timebase import FrameRate

RATE_30 = FrameRate(30)


@pytest.fixture
def video_media() -> MediaItem:
    """10 秒の 1080p30 素材。映像 1 本と音声 1 本を持つ。"""
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
    """30 秒の BGM。"""
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
    """3 つの発話区間を持つ起こし結果。時刻はすべてソース秒。"""
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
    """素材 1 つと空の映像トラック 1 本を持つプロジェクト。"""
    base = Project.create(ProjectSettings(frame_rate=RATE_30), media=(video_media,))
    from dataclasses import replace

    return base.with_timeline(replace(base.timeline, tracks=(video_track,)))


def make_clip(start: int, duration: int, media: MediaItem, source_in: int = 0) -> Clip:
    """テスト用のクリップを手短に作る。"""
    return Clip(
        timeline_start=start,
        duration=duration,
        media_id=media.id,
        source_in=Fraction(source_in),
    )
