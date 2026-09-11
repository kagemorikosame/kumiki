"""プロジェクト全体 モデルツリーの根"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction

from kumiki.core.model.ids import MediaId
from kumiki.core.model.media import MediaItem
from kumiki.core.model.timeline import Timeline
from kumiki.core.timebase import FrameRate

__all__ = ["Project", "ProjectSettings"]


@dataclass(frozen=True, slots=True)
class ProjectSettings:
    """出力の基準となる設定

    タイムライン上のすべての時刻はここの :attr:`frame_rate` を基準に解釈される
    素材のフレームレートがこれと違っても構わない（読み込み時に秒へ正規化される）
    """

    width: int = 1920
    height: int = 1080
    frame_rate: FrameRate = field(default_factory=lambda: FrameRate(30))
    sample_rate: int = 48000
    channels: int = 2
    #: 当面は sRGB / Rec.709 のみ HDR は将来対応
    color_space: str = "rec709"

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"解像度が不正: {self.width}x{self.height}")
        if self.sample_rate <= 0:
            raise ValueError(f"サンプリングレートが不正: {self.sample_rate}")
        if self.channels <= 0:
            raise ValueError(f"チャンネル数が不正: {self.channels}")

    @property
    def resolution(self) -> tuple[int, int]:
        return self.width, self.height


@dataclass(frozen=True, slots=True)
class Project:
    """編集中のプロジェクト

    このオブジェクトを差し替えることが「編集した」という意味になる UI も AI も
    直接書き換えず、:mod:`kumiki.core.commands` を通して新しい :class:`Project` を
    受け取る

    既定値付きで作りたい場合は :meth:`create` を使う タイムラインのフレームレートは
    常に :attr:`settings` と一致していなければならないので、素の構築では両方を渡す
    """

    settings: ProjectSettings
    timeline: Timeline
    #: メディアプール 表示順を保つため辞書ではなくタプル
    media: tuple[MediaItem, ...] = ()
    name: str = "無題"

    def __post_init__(self) -> None:
        if self.timeline.rate != self.settings.frame_rate:
            raise ValueError(
                "タイムラインのフレームレートがプロジェクト設定と一致しない: "
                f"{self.timeline.rate} と {self.settings.frame_rate}"
            )
        ids = [m.id for m in self.media]
        if len(set(ids)) != len(ids):
            raise ValueError("素材 ID が重複している")

    @classmethod
    def create(
        cls,
        settings: ProjectSettings | None = None,
        *,
        media: tuple[MediaItem, ...] = (),
        name: str = "無題",
    ) -> Project:
        """設定に合ったフレームレートの空タイムラインを用意して生成する"""
        resolved = settings if settings is not None else ProjectSettings()
        return cls(
            settings=resolved,
            timeline=Timeline(rate=resolved.frame_rate),
            media=media,
            name=name,
        )

    @property
    def rate(self) -> FrameRate:
        return self.settings.frame_rate

    @property
    def duration(self) -> int:
        """タイムラインの長さ（フレーム）"""
        return self.timeline.duration

    @property
    def duration_seconds(self) -> Fraction:
        return self.duration * self.rate.frame_duration

    def find_media(self, media_id: MediaId) -> MediaItem | None:
        for item in self.media:
            if item.id == media_id:
                return item
        return None

    def require_media(self, media_id: MediaId) -> MediaItem:
        """素材を引く 無ければ :class:`KeyError`"""
        item = self.find_media(media_id)
        if item is None:
            raise KeyError(f"素材が見つからない: {media_id}")
        return item

    def with_timeline(self, timeline: Timeline) -> Project:
        return replace(self, timeline=timeline)

    def with_media(self, media: tuple[MediaItem, ...]) -> Project:
        return replace(self, media=media)

    def replace_media(self, item: MediaItem) -> Project:
        """同じ ID の素材を差し替えた新しい :class:`Project` を返す"""
        for index, existing in enumerate(self.media):
            if existing.id == item.id:
                return self.with_media((*self.media[:index], item, *self.media[index + 1 :]))
        raise KeyError(f"素材が見つからない: {item.id}")

    def renamed(self, name: str) -> Project:
        return replace(self, name=name)
