"""シーン（入れ子のタイムライン）を扱うコマンド

シーンの中を編集するときは、ふつうのコマンドを :class:`InScene` で包む ほかの
コマンドはどれも ``project.timeline`` を相手に書かれているので、シーンのタイムラインを
いったんメインの位置に差し込んで走らせ、結果を元のシーンへ書き戻す こうすると
コマンドを 1 つも書き直さずに、シーンの中でも同じ編集ができる
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from sashimono.core.commands.base import Command
from sashimono.core.commands.insert import insert_clip
from sashimono.core.model import Clip, MediaId, Project, Scene, SceneId, Timeline, TrackId

__all__ = [
    "DEFAULT_SCENE_FRAMES",
    "AddScene",
    "InScene",
    "RemoveScene",
    "RenameScene",
    "insert_scene",
    "new_scene",
]

#: 空のシーンを置くときの長さ（フレーム） 30fps で 5 秒
DEFAULT_SCENE_FRAMES = 150


def new_scene(project: Project, name: str) -> Scene:
    """プロジェクトと同じフレームレートの、空のシーン"""
    return Scene(name=name, timeline=Timeline(rate=project.rate))


@dataclass(frozen=True, slots=True)
class AddScene(Command):
    scene: Scene

    @property
    def label(self) -> str:
        return f"シーンを追加: {self.scene.name}"

    def apply(self, project: Project) -> Project:
        if project.find_scene(self.scene.id) is not None:
            raise ValueError(f"すでにあるシーン: {self.scene.id}")
        return project.with_scenes((*project.scenes, self.scene))


@dataclass(frozen=True, slots=True)
class RemoveScene(Command):
    """シーンを消す どこかに置かれていれば断る

    置いたまま消すと、置いた場所が何も映らない穴になり、原因が分からない
    """

    scene_id: SceneId

    @property
    def label(self) -> str:
        return "シーンを削除"

    def apply(self, project: Project) -> Project:
        scene = project.require_scene(self.scene_id)
        users = [
            other.name
            for other in project.scenes
            if self.scene_id in other.timeline.scene_references()
        ]
        if self.scene_id in project.timeline.scene_references():
            users.insert(0, "メイン")
        if users:
            raise ValueError(f"シーン {scene.name!r} は {'、'.join(users)} に置かれている")
        return project.with_scenes(tuple(s for s in project.scenes if s.id != self.scene_id))


@dataclass(frozen=True, slots=True)
class RenameScene(Command):
    scene_id: SceneId
    name: str

    @property
    def label(self) -> str:
        return "シーンの名前を変更"

    def apply(self, project: Project) -> Project:
        name = self.name.strip()
        if not name:
            raise ValueError("シーンの名前が空")
        scene = project.require_scene(self.scene_id)
        return project.replace_scene(replace(scene, name=name))


@dataclass(frozen=True, slots=True)
class InScene(Command):
    """``command`` をシーンの中で実行する

    素材・設定・ほかのシーンへの変更は、中のコマンドの結果をそのまま採る
    （素材の追加や解像度の変更は、どのシーンで行ってもプロジェクト全体に効く）
    自分自身を入れ子にする置き方は、書き戻した時点でプロジェクトの検査が止める
    """

    scene_id: SceneId
    command: Command

    @property
    def label(self) -> str:
        return self.command.label

    def apply(self, project: Project) -> Project:
        scene = project.require_scene(self.scene_id)
        inner = replace(project, timeline=scene.timeline)
        result = self.command.apply(inner)
        # 中のコマンドがシーンを足し引きしていても、この名前のシーンが残っていれば書き戻す
        current = result.find_scene(self.scene_id)
        if current is None:
            raise ValueError("編集中のシーンが消された")
        scenes = tuple(
            replace(current, timeline=result.timeline) if s.id == self.scene_id else s
            for s in result.scenes
        )
        merged = replace(result, timeline=project.timeline, scenes=scenes)
        # 中のコマンドは開いているシーンしか見ていない 素材を外すコマンドなら、
        # メインやほかのシーンのクリップが消えた素材を指していないかをここで確かめる
        known = {item.id for item in merged.media}
        lost = {item.id for item in project.media} - known
        if lost and any(_uses_media(timeline, lost) for timeline in _timelines(merged)):
            raise ValueError("ほかのタイムラインで使われている素材は削除できない")
        return merged


def _timelines(project: Project) -> list[Timeline]:
    return [project.timeline, *(scene.timeline for scene in project.scenes)]


def _uses_media(timeline: Timeline, media_ids: set[MediaId]) -> bool:
    return any(clip.media_id in media_ids for track in timeline.tracks for clip in track.clips)


def insert_scene(
    project: Project,
    scene_id: SceneId,
    *,
    at_frame: int,
    duration: int | None = None,
    track_id: TrackId | None = None,
) -> list[Command]:
    """シーンを 1 本のクリップとしてタイムラインへ置く

    長さを省くとシーンの長さ（空なら既定の長さ） 置き先は、指定位置に空きのある
    映像トラック（無ければ新しく作る） テキストを置くときと同じ決まりで、``track_id`` も同じ
    """
    scene = project.require_scene(scene_id)
    length = duration if duration is not None else scene.timeline.duration
    length = max(1, length or DEFAULT_SCENE_FRAMES)
    return insert_clip(
        project,
        Clip(timeline_start=0, duration=length, scene_id=scene_id),
        at_frame=max(0, at_frame),
        track_id=track_id,
    )
