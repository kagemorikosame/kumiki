"""メディアプールの素材。

素材はタイムラインとは独立に存在する。同じ素材を何度タイムラインに置いても
実体は 1 つで、字幕もサムネイルも波形もこちらに紐付く。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from novaedit.core.model.ids import MediaId, new_media_id
from novaedit.core.model.transcript import Transcript
from novaedit.core.timebase import FrameRate

__all__ = ["AudioStreamInfo", "MediaItem", "VideoStreamInfo"]


@dataclass(frozen=True, slots=True)
class VideoStreamInfo:
    """素材に含まれる映像ストリーム 1 本の情報。"""

    index: int
    width: int
    height: int
    frame_rate: FrameRate
    time_base: Fraction
    codec: str
    pixel_format: str = ""
    #: コンテナに記録された回転角（0 / 90 / 180 / 270）。スマホ撮影で頻出する。
    rotation: int = 0

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"解像度が不正: {self.width}x{self.height}")
        if self.rotation not in (0, 90, 180, 270):
            raise ValueError(f"回転角が不正: {self.rotation}")

    @property
    def display_size(self) -> tuple[int, int]:
        """回転を適用した後の表示サイズ。"""
        if self.rotation in (90, 270):
            return self.height, self.width
        return self.width, self.height


@dataclass(frozen=True, slots=True)
class AudioStreamInfo:
    """素材に含まれる音声ストリーム 1 本の情報。

    多言語音声や 5.1ch の素材では複数本あり、読み込み時にそれぞれ別トラックへ
    展開する。
    """

    index: int
    sample_rate: int
    channels: int
    time_base: Fraction
    codec: str
    language: str | None = None

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError(f"サンプリングレートが不正: {self.sample_rate}")
        if self.channels <= 0:
            raise ValueError(f"チャンネル数が不正: {self.channels}")


@dataclass(frozen=True, slots=True)
class MediaItem:
    """メディアプールに登録された 1 つの素材。"""

    path: Path
    #: 素材全体の長さ（秒）。静止画では 0。
    duration: Fraction = Fraction(0)
    video_streams: tuple[VideoStreamInfo, ...] = ()
    audio_streams: tuple[AudioStreamInfo, ...] = ()
    #: 字幕起こしの結果。トラックではなくここに持たせるのが設計の要。
    transcript: Transcript | None = None
    #: 空ならファイル名を表示名として使う。
    display_name: str = ""
    id: MediaId = field(default_factory=new_media_id)

    def __post_init__(self) -> None:
        if self.duration < 0:
            raise ValueError(f"長さが負: {self.duration}")

    @property
    def name(self) -> str:
        return self.display_name or self.path.name

    @property
    def has_video(self) -> bool:
        return len(self.video_streams) > 0

    @property
    def has_audio(self) -> bool:
        return len(self.audio_streams) > 0

    @property
    def is_still(self) -> bool:
        """静止画のように、任意の長さで使える素材か。"""
        return self.duration == 0 and not self.has_audio

    def with_transcript(self, transcript: Transcript | None) -> MediaItem:
        """起こし結果を差し替えた新しい :class:`MediaItem` を返す。"""
        return MediaItem(
            path=self.path,
            duration=self.duration,
            video_streams=self.video_streams,
            audio_streams=self.audio_streams,
            transcript=transcript,
            display_name=self.display_name,
            id=self.id,
        )
