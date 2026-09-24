"""書き出し範囲を決めるコマンド（Issue #27）

範囲はタイムラインごとに持ち、取り消せる 書き出しに使う所はタイムラインの長さで切る
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sashimono.core.commands import (
    AddClip,
    AddScene,
    AddTrack,
    Document,
    InScene,
    SetWorkArea,
    export_range,
    new_scene,
)
from sashimono.core.io import load_project, save_project
from sashimono.core.model import Clip, Project, Track, TrackKind
from sashimono.effects.sources import TEXT


def _project(frames: int = 90) -> Project:
    """``frames`` コマのテキストを 1 本置いた作品"""
    track = Track(TrackKind.VIDEO, "V1")
    project = AddTrack(track).apply(Project.create())
    clip = Clip(timeline_start=0, duration=frames, source=TEXT.create())
    return AddClip(track.id, clip).apply(project)


class TestSetWorkArea:
    def test_the_range_is_set_and_undone(self) -> None:
        """決めた範囲が取り消しで消える 残ると、取り消したのに書き出しが範囲だけになる"""
        document = Document(_project())
        document.execute(SetWorkArea((10, 40)))
        assert document.project.timeline.work_area == (10, 40)
        document.undo()
        assert document.project.timeline.work_area is None

    def test_none_clears_the_range(self) -> None:
        project = SetWorkArea((10, 40)).apply(_project())
        assert SetWorkArea(None).apply(project).timeline.work_area is None

    @pytest.mark.parametrize("area", [(-5, 10), (20, 20), (30, 10)])
    def test_a_broken_range_is_refused(self, area: tuple[int, int]) -> None:
        """負の始まりや幅 0 の範囲を断る 通すと、書き出しが空か負のフレームを描きに行く"""
        with pytest.raises(ValueError, match="書き出し範囲"):
            SetWorkArea(area).apply(_project())

    def test_the_labels_say_what_happened(self) -> None:
        # 履歴に同じ名前が並ぶと、どちらへ戻ればよいのか分からない
        assert SetWorkArea((0, 10)).label != SetWorkArea(None).label

    def test_inside_a_scene_only_that_scene_changes(self) -> None:
        """シーンを開いて決めた範囲は、そのシーンの物 メインの範囲は動かない

        メインが動くと、シーンの中の帯を引いただけで書き出す所が変わる
        """
        project = _project()
        scene = new_scene(project, "OP")
        project = AddScene(scene).apply(project)
        project = SetWorkArea((0, 30)).apply(project)
        changed = InScene(scene.id, SetWorkArea((5, 15))).apply(project)
        assert changed.timeline.work_area == (0, 30)
        assert changed.require_scene(scene.id).timeline.work_area == (5, 15)

    def test_the_range_survives_saving(self, tmp_path: Path) -> None:
        """保存して開き直しても範囲が残る 消えると、開くたびに引き直すことになる"""
        path = tmp_path / "range.sme"
        save_project(SetWorkArea((12, 48)).apply(_project()), path)
        assert load_project(path).timeline.work_area == (12, 48)


class TestExportRange:
    def test_no_range_means_the_whole(self) -> None:
        assert export_range(_project().timeline) is None

    def test_a_range_inside_is_kept(self) -> None:
        timeline = replace(_project().timeline, work_area=(10, 40))
        assert export_range(timeline) == (10, 40)

    def test_the_part_after_the_end_is_cut(self) -> None:
        """終わりより後ろは切る 切らないと、黒と無音が後ろに付いた動画になる"""
        timeline = replace(_project(90).timeline, work_area=(60, 200))
        assert export_range(timeline) == (60, 90)

    def test_a_range_wholly_after_the_end_is_unusable(self) -> None:
        # 使える形で返すと、何も映らない所だけを書き出す
        timeline = replace(_project(90).timeline, work_area=(120, 200))
        assert export_range(timeline) is None
