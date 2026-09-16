"""プロジェクト全体 モデルツリーの根"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from fractions import Fraction

from kumiki.core.model.ids import MediaId, SceneId, new_scene_id
from kumiki.core.model.media import MediaItem
from kumiki.core.model.timeline import Timeline
from kumiki.core.timebase import FrameRate

__all__ = ["Project", "ProjectSettings", "Scene"]


@dataclass(frozen=True, slots=True)
class Scene:
    """メインとは別のタイムライン（AviUtl のシーン）

    ほかのタイムラインへ 1 本のクリップとして置ける（:attr:`Clip.scene_id`）
    オープニングのように何度も使う部分を 1 か所で直せる
    """

    name: str
    timeline: Timeline
    id: SceneId = field(default_factory=new_scene_id)


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
    #: メイン（:attr:`timeline`）とは別のシーン 表示順を保つためタプル
    scenes: tuple[Scene, ...] = ()

    def __post_init__(self) -> None:
        if self.timeline.rate != self.settings.frame_rate:
            raise ValueError(
                "タイムラインのフレームレートがプロジェクト設定と一致しない: "
                f"{self.timeline.rate} と {self.settings.frame_rate}"
            )
        ids = [m.id for m in self.media]
        if len(set(ids)) != len(ids):
            raise ValueError("素材 ID が重複している")
        scene_ids = [s.id for s in self.scenes]
        if len(set(scene_ids)) != len(scene_ids):
            raise ValueError("シーン ID が重複している")
        for scene in self.scenes:
            if scene.timeline.rate != self.settings.frame_rate:
                # シーンだけ別のフレームレートにすると、置いたときの時刻の換算が要る
                raise ValueError(f"シーン {scene.name!r} のフレームレートがプロジェクトと違う")
        cycle = self.scene_cycle()
        if cycle is not None:
            raise ValueError(f"シーンが自分自身を入れ子にしている: {cycle}")

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

    def find_scene(self, scene_id: SceneId) -> Scene | None:
        for scene in self.scenes:
            if scene.id == scene_id:
                return scene
        return None

    def require_scene(self, scene_id: SceneId) -> Scene:
        scene = self.find_scene(scene_id)
        if scene is None:
            raise KeyError(f"シーンが見つからない: {scene_id}")
        return scene

    def replace_scene(self, scene: Scene) -> Project:
        """同じ ID のシーンを差し替えた新しい :class:`Project` を返す"""
        for index, existing in enumerate(self.scenes):
            if existing.id == scene.id:
                scenes = (*self.scenes[:index], scene, *self.scenes[index + 1 :])
                return replace(self, scenes=scenes)
        raise KeyError(f"シーンが見つからない: {scene.id}")

    def with_scenes(self, scenes: tuple[Scene, ...]) -> Project:
        return replace(self, scenes=scenes)

    def scene_cycle(self) -> str | None:
        """入れ子が自分へ戻るシーンの名前 無ければ ``None``

        描くときに無限に潜り続けるので、作らせない（コマンドはこれで止まる）
        """
        edges = {scene.id: scene.timeline.scene_references() for scene in self.scenes}
        names = {scene.id: scene.name for scene in self.scenes}
        visiting: set[SceneId] = set()
        done: set[SceneId] = set()

        def visit(scene_id: SceneId) -> SceneId | None:
            if scene_id in done:
                return None
            if scene_id in visiting:
                return scene_id
            visiting.add(scene_id)
            for target in edges.get(scene_id, set()):
                found = visit(target)
                if found is not None:
                    return found
            visiting.discard(scene_id)
            done.add(scene_id)
            return None

        for scene_id in edges:
            found = visit(scene_id)
            if found is not None:
                return names.get(found, str(found))
        return None
